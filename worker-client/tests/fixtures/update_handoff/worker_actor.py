"""Local protocol actor. Real coordinator/startup validation; no Qt/WeChat acceptance."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0, os.environ['HANDOFF_TEST_SOURCE'])
from chejin_worker_client import storage

parser=argparse.ArgumentParser()
parser.add_argument('--post-update-plan')
parser.add_argument('--post-rollback-plan')
parser.add_argument('--post-update-token')
args=parser.parse_args()
if args.post_rollback_plan:
    time.sleep(2)
    raise SystemExit(0)
if args.post_update_plan:
    from chejin_worker_client import post_update_health as health
    from chejin_worker_client.update_diagnostics import record_update_startup_failure
    path=Path(args.post_update_plan)
    plan=json.loads(path.read_text())
    # Source tests run Python scripts in place of EXEs. Package hashes and all data
    # validation are real; only the frozen executable location is represented here.
    health.sys.executable=str(Path(__file__).resolve())
    if os.environ.get('HANDOFF_TEST_DAMAGE') == 'binding':
        import sqlite3
        with sqlite3.connect(Path(plan['data_dir'])/'worker_client.sqlite3') as c:
            c.execute("UPDATE binding SET worker_token='synthetic-tamper'")
    migration = os.environ.get('HANDOFF_TEST_DAMAGE', '')
    if migration.startswith('init-'):
        original_init = storage.init_db
        def injected_init(conn):
            original_init(conn)
            if migration == 'init-insert':
                conn.execute("INSERT INTO client_settings VALUES ('audit-only','faulty-migration','synthetic')")
            elif migration == 'init-delete':
                conn.execute('DELETE FROM binding')
            else:
                conn.execute("ALTER TABLE binding ADD COLUMN compatible_note TEXT NOT NULL DEFAULT ''")
            conn.commit()
        storage.init_db = injected_init
    try:
        health.verify_post_update_startup(path, args.post_update_token)
    except Exception as exc:
        record_update_startup_failure(path, phase='post_update_verification', exc=exc, exit_code=3)
        raise SystemExit(3)
    schedule=storage.load_accept_schedule()
    (path.parent/'verified-data.json').write_text(json.dumps({'schedule':schedule,'verified_at':time.time()}))
    # Real health gate, with explicitly synthetic GUI/thread liveness for local data tests.
    # Formal Windows gate must supply the actual application's liveness evidence.
    gate=health.RuntimeHealthGate(plan,args.post_update_token)
    while gate.observe({'ready':True,'ui_event_loop_alive':True,'required_threads':[],
                        'threads':{},'startup_failures':[]}) is None:
        time.sleep(.25)
    time.sleep(2)
    raise SystemExit(0)

from chejin_worker_client.models import ClientRelease
from chejin_worker_client.client_update import UpdateStateStore
from chejin_worker_client.update_coordinator import UpdateCoordinator
import chejin_worker_client.update_coordinator as coordinator_module
config=json.loads(Path(os.environ['HANDOFF_TEST_CONFIG']).read_text())
# This is a protocol-2 old-process fixture, never a claim about the released old EXE.
coordinator_module.__version__='0.9.59'
release=ClientRelease.from_api(config['release'])
state=config['state']
ready=Path(config['control'])/'updater-ready.json'
events=Path(config['events'])
def event(phase):
    with events.open('a') as f: f.write(json.dumps({'phase':phase,'time':time.time()})+'\n')
if os.environ.get('HANDOFF_TEST_DAMAGE') == 'shutdown-timeout':
    # Real UI slot body with a queue event in place of Qt signal delivery.
    # Real coordinator, TaskRunner stop, SQLite writer and updater subprocess.
    import ast
    from types import SimpleNamespace
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.incident_evidence import stop_incident_worker
    tree=ast.parse((Path(os.environ['HANDOFF_TEST_SOURCE'])/'chejin_worker_client/web_ui.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='WorkerWebWindow')
    fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_quit_for_update')
    fn.decorator_list=[]
    ns={}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<production-ui-slot>','exec'),ns)
    api=SimpleNamespace(set_run_status=lambda binding,status:SimpleNamespace(run_status=status,inflight_flow_state={}))
    runner=TaskRunner(api,SimpleNamespace(),on_profile=lambda v:None,on_status=lambda v:None,
        on_task=lambda v:None,on_step=lambda v:None,on_result=lambda v:None,on_error=lambda v:None)
    runner.binding=storage.load_binding()
    release_writer=threading.Event()
    def slow_writer():
        runner.stop_event.wait(5)
        release_writer.wait(5)
        storage.save_accept_schedule(enabled=True,start='12:00',end='20:00')
        event('late_writer_committed')
    runner.thread=threading.Thread(target=slow_writer)
    runner.thread.start()
    exit_requested=threading.Event()
    launches=[]
    states=[]
    def launch(executable,path,token):
        plan=json.loads(path.read_text())
        plan.update(old_exit_timeout_seconds=1,health_timeout_seconds=3,result_timeout_seconds=10)
        path.write_text(json.dumps(plan))
        launches.append(True)
        return UpdateCoordinator._launch_updater(executable,path,token)
    def identity(pid):
        value=UpdateCoordinator._read_process_identity(pid)
        if value and value.get('pid')==pid:value['executable_path']=str(Path(config['control'])/'CheJinUpdater.exe')
        return value
    coordinator=UpdateCoordinator(api,runner,binding_provider=storage.load_binding,on_state=lambda s:states.append(s),
        request_normal_exit=exit_requested.set,state_store=UpdateStateStore(Path(config['update_root'])),
        formal_package=True,current_program_dir=Path(config['current']),updater_launcher=launch,process_identity=identity)
    preparing=threading.Thread(target=coordinator._start_install,args=(state,release,Path(config['request_root']),config['boundary']))
    coordinator._worker=preparing
    preparing.start()
    assert exit_requested.wait(10)
    closed=[]
    ui=SimpleNamespace(runner=SimpleNamespace(stop_for_update=lambda:runner.stop_for_update(.01)),
        update_coordinator=coordinator,close=lambda:closed.append(True))
    ns['_quit_for_update'](ui)
    assert runner.thread.is_alive() and not closed
    release_writer.set()
    runner.thread.join(3)
    coordinator._worker.join(15)
    assert not coordinator._worker.is_alive()
    final=coordinator.state()
    assert final['state']=='failed' and final['result_reconciled'] is True and not final['in_progress']
    assert final['result_code']=='UPDATE_WRITERS_NOT_STOPPED'
    assert storage.load_runtime_control()['update_no_new_work'] is False
    assert storage.load_binding().run_status=='faulted'
    assert launches==[True] and not closed
    assert storage.load_accept_schedule()['start']=='12:00'
    (Path(config['control'])/'shutdown-ui-result.json').write_text(json.dumps({'state':final,
        'launch_count':len(launches),'window_closed':bool(closed),'binding_status':storage.load_binding().run_status,
        'gate_blocked':storage.load_runtime_control()['update_no_new_work'],'slow_writer_stopped':not runner.thread.is_alive()}))
    assert stop_incident_worker(wait=True)
    raise SystemExit(0)
def last_writes():
    deadline=time.monotonic()+15
    while not ready.exists():
        if time.monotonic()>deadline: raise RuntimeError('TEST_READY_TIMEOUT')
        time.sleep(.01)
    storage.save_accept_schedule(enabled=True,start='10:00',end='18:00')
    event('write_after_updater_ready')
thread=threading.Thread(target=last_writes)
thread.start()
def normal_exit():
    thread.join(5)
    assert not thread.is_alive()
    storage.save_accept_schedule(enabled=True,start='11:00',end='19:00')
    event('final_exit_commit')
def script_identity(pid):
    result=UpdateCoordinator._read_process_identity(pid)
    if result and result.get('pid')==pid:
        # Interpreter identity is real; the script location represents a frozen
        # updater executable only in this source-process fixture.
        result['executable_path']=str(Path(config['control'])/'CheJinUpdater.exe')
    return result
coordinator=UpdateCoordinator(None,None,binding_provider=storage.load_binding,on_state=lambda state:None,
    request_normal_exit=normal_exit,state_store=UpdateStateStore(Path(config['update_root'])),
    formal_package=True,current_program_dir=Path(config['current']),process_identity=script_identity)
coordinator._start_install(state,release,Path(config['request_root']),config['boundary'])
assert coordinator.state()['install_started'] is True
