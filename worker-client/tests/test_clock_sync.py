"""Repair lifecycle tests; Windows UAC/NTP commands are substituted, never run here."""
from dataclasses import replace
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid
import zipfile

import pytest
from chejin_worker_client import storage
from chejin_worker_client.clock_sync import ClockSync
from chejin_worker_client.clock_sync_ticket import ClockSyncTicket, valid_operation
from chejin_worker_client import windows_clock_sync as windows
from chejin_worker_client.clock_diagnostics import ClockDiagnostics
from chejin_worker_client.diagnostic_bundle import export_bundle
from test_clock_diagnostics import ZONE, REFERENCE
from test_hook_worker_services import runtime, lab, backend_peer, until


@pytest.fixture(autouse=True)
def isolated_bridge(request):
    if "runtime" not in request.fixturenames:
        return
    runtime = request.getfixturevalue("runtime")
    runner = runtime[0]
    runner.bridge.sidecar_active = lambda: False
    runner.api.task_lease_fencing_tokens = {}


def operation():
    return 'clock-sync-' + str(uuid.uuid4())


def monitor_for(services, root):
    monitor = ClockDiagnostics(root, zone_reader=lambda: ZONE)
    services.connection.backend = replace(services.connection.backend, clock_diagnostics=monitor)
    return monitor


def finish(sync):
    until(lambda: not sync.snapshot()['busy'], seconds=12)


@pytest.mark.parametrize('status', ['paused', 'faulted', 'running'])
@pytest.mark.parametrize('reference_source', ['primary', 'fallback'])
def test_real_runner_stops_repairs_reconnects_fresh_time_keeps_unknown_and_pause(runtime, backend_peer, tmp_path, status, reference_source):
    runner, services, binding, _, _, _, posted, _ = runtime
    root = tmp_path/'home'
    journal = root/'transactions/original.json'; journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"state":"unknown","do_not_resend":true}')
    digest = hashlib.sha256(journal.read_bytes()).hexdigest()
    reference = REFERENCE if reference_source == 'primary' else {
        **REFERENCE, 'source': 'windows_ntp', 'host': 'time.windows.com'}
    body = json.loads(backend_peer.body); body['data']['clock_reference'] = reference
    backend_peer.body = json.dumps(body).encode()
    monitor = monitor_for(services, root)
    runner.start(binding)
    until(lambda: services.connection.prepared is not None and monitor.snapshot()['comparison_status']=='aligned')
    binding.run_status = status; storage.save_binding(binding)
    initial = monitor.snapshot()['observed_monotonic']
    repaired = []
    def repair(op, ticket):
        assert valid_operation(storage.load_runtime_control()['update_request_id'])
        assert storage.load_runtime_control()['update_request_id'] != op
        assert runner.stop_event.is_set()
        assert all(t is None or not t.is_alive() for t in (runner.thread, runner.hook_intake_thread, runner.hook_business_thread, runner.thread_monitor))
        assert services.connection.prepared is None
        assert Path(ticket).read_text() == op
        assert binding.run_status == ('faulted' if status == 'faulted' else 'paused')
        repaired.append(op)
        return {'resync_completed':True,'helper_exit_confirmed':True,'result':'requested'}
    sync = ClockSync(runner, root, repair=repair, verify_seconds=3)
    assert sync.start(); finish(sync)
    assert sync.snapshot()['state'] == 'verified'
    assert len(repaired)==1 and monitor.snapshot()['observed_monotonic'] > initial
    assert storage.load_runtime_control()['update_no_new_work'] is False
    assert binding.run_status == ('faulted' if status=='faulted' else 'paused')
    assert 'running' not in posted
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == digest
    assert sync.ticket.path.read_text() == 'revoked'
    assert monitor.flush()
    bundle = export_bundle(root, tmp_path/'evidence.zip')
    with zipfile.ZipFile(bundle) as archive:
        saved = json.loads(archive.read('logs/runtime/clock-sync.json'))
    assert saved['latest']['state']=='verified'
    assert any(e.get('helper',{}).get('helper_exit_confirmed') for e in saved['events'])
    assert any(e.get('clock_check',{}).get('backend_reference',{}).get('status')=='aligned' for e in saved['events'])


@pytest.mark.parametrize('result', ['cancelled','managed','service_disabled','configure_failed','resync_failed','elevation_failed'])
def test_failed_helper_never_claims_success_and_resumes_only_paused_connection(runtime, tmp_path, result):
    runner, services, binding, *_ = runtime
    monitor_for(services, tmp_path)
    runner.start(binding); until(lambda: services.connection.prepared is not None)
    sync = ClockSync(runner, tmp_path, repair=lambda *args:{'result':result,'helper_exit_confirmed':True})
    sync.start(); finish(sync)
    assert sync.snapshot()['state']==result
    assert binding.run_status=='paused' and not storage.load_runtime_control()['update_no_new_work']
    until(lambda:services.connection.prepared is not None)


def test_missing_reference_is_unconfirmed_even_when_helper_succeeded(runtime, tmp_path):
    runner,services,binding,*_=runtime
    monitor_for(services,tmp_path)
    runner.start(binding); until(lambda:services.connection.prepared is not None)
    sync=ClockSync(runner,tmp_path,repair=lambda *args:{'resync_completed':True,'helper_exit_confirmed':True,'result':'requested'},verify_seconds=.25)
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='unconfirmed'
    assert binding.run_status=='paused'


def test_reference_outage_does_not_fault_startup_or_run_clock_repair(runtime, backend_peer, tmp_path, monkeypatch):
    runner, services, binding, _, errors, _, posted, _ = runtime
    body = json.loads(backend_peer.body)
    body['data']['clock_reference'] = {**REFERENCE, 'status': 'unconfirmed', 'attempts': [
        {'host': host, 'status': 'unconfirmed', 'reason': 'timeout', 'elapsed_seconds': 2}
        for host in ('ntp.aliyun.com', 'time.windows.com')]}
    backend_peer.body = json.dumps(body).encode()
    monkeypatch.setattr(windows.subprocess, 'run', lambda *a, **k: pytest.fail('startup launched repair'))
    monitor_for(services, tmp_path)
    runner.binding = binding
    services.attach(binding, runner._hook_persistence_failed)
    runner.tick_once()
    state = services.startup_check_state()
    assert state['canStart']
    assert state['clockCheck']['verification']['status'] == 'unconfirmed'
    assert state['clockCheck']['notice'] == ''
    assert binding.run_status == 'paused' and not errors and not posted


def test_competing_update_and_duplicate_click_cannot_launch_helpers(runtime, tmp_path):
    runner,services,binding,*_=runtime
    monitor_for(services,tmp_path)
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    called=[];entered=threading.Event();release=threading.Event()
    def repair(*args):
        called.append(args);entered.set();assert release.wait(5)
        return {'result':'cancelled'}
    sync=ClockSync(runner,tmp_path,repair=repair)
    runner.set_update_new_work_gate(True,update_request_id='existing-update')
    sync.start();finish(sync)
    assert not called and storage.load_runtime_control()['update_request_id']=='existing-update'
    runner.set_update_new_work_gate(False,update_request_id='existing-update')
    try:
        sync.start();assert entered.wait(5)
        assert not sync.start()
        assert runner.begin_update_check(lambda:pytest.fail('must not reserve update'))[1] is None
        runner.set_update_new_work_gate(False,update_request_id='late-other-update')
        assert storage.load_runtime_control()['update_no_new_work']
    finally:
        release.set();finish(sync)
    assert len(called)==1


def test_undrained_writer_never_changes_clock_and_explicit_retry_can_finish(runtime,tmp_path,monkeypatch):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    original=runner.stop_for_update;called=[]
    def blocked(*args):raise RuntimeError('UPDATE_WRITERS_NOT_STOPPED')
    monkeypatch.setattr(runner,'stop_for_update',blocked)
    sync=ClockSync(runner,tmp_path,repair=lambda *args:called.append(args) or {'result':'cancelled'})
    sync.start();finish(sync)
    assert not called and sync.snapshot()['state']=='stop_failed'
    owned=storage.load_runtime_control()['update_request_id']
    assert owned.startswith('clock-sync-') and binding.run_status=='paused'
    monkeypatch.setattr(runner,'stop_for_update',original)
    sync.start();finish(sync)
    assert len(called)==1 and called[0][0]!=owned and valid_operation(called[0][0])
    assert not storage.load_runtime_control()['update_no_new_work']


def test_crash_recovery_revokes_pending_authorization_without_clearing_unknown(tmp_path, monkeypatch):
    control={'update_request_id':operation(),'update_no_new_work':True}
    monkeypatch.setattr(storage,'load_runtime_control',lambda:dict(control))
    monkeypatch.setattr(storage,'request_runtime_pause',lambda:control.update(pause_requested=True))
    monkeypatch.setattr(storage,'save_binding',lambda b:None)
    def clear(blocked,update_request_id):
        assert update_request_id==control['update_request_id']
        control['update_no_new_work']=blocked
    runner=SimpleNamespace(set_update_new_work_gate=clear)
    sync=ClockSync(runner,tmp_path)
    sync.ticket.authorize(control['update_request_id'])
    binding=SimpleNamespace(run_status='faulted')
    # Another process holds the same lock, simulating the active elevated helper.
    script="import fcntl,sys; f=open(sys.argv[1],'r+b'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read(1)"
    proc=subprocess.Popen([sys.executable,'-c',script,str(sync.ticket.path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert proc.stdout.readline().strip()=='locked'
        assert not sync.runtime_ready(binding)
        assert sync.snapshot()['state']=='recovering' and control['update_no_new_work']
    finally:
        proc.communicate('x',timeout=3)
    assert sync.runtime_ready(binding)
    assert sync.ticket.path.read_text()=='revoked'
    assert not control['update_no_new_work'] and binding.run_status=='faulted'


def test_fixed_helper_guards_policy_before_changes_and_uses_safe_literals(monkeypatch,tmp_path):
    op=operation();path=tmp_path/"quotes'$(bad).ticket"
    script=windows.helper_script(op,path)
    assert script.index('$ticket.Lock') < script.index('Set-TimeZone')
    assert script.index('PartOfDomain') < script.index('Set-TimeZone')
    assert script.index("'Disabled'") < script.index('Set-TimeZone')
    assert "quotes''$(bad).ticket'" in script
    assert "'/manualpeerlist:ntp.aliyun.com,0x8 time.windows.com,0xa'" in script
    assert 'China Standard Time' in script
    assert script.index("'Disabled'") < script.index('Set-Service W32Time -StartupType Automatic')
    assert '/nowait' not in script and '& $exe /resync /rediscover | Out-Null' in script
    assert '-ExecutionPolicy' not in script and 'Set-Date' not in script
    assert not valid_operation('clock-sync-invalid')
    with pytest.raises(ValueError):windows.helper_script("';anything",path)
    monkeypatch.setattr(windows.sys,'platform','win32')
    monkeypatch.setattr(windows,'powershell_path',lambda:Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'))
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs));return SimpleNamespace(returncode=25)
    monkeypatch.setattr(windows.subprocess,'run',run)
    result=windows.synchronize_windows(op,path)
    assert result['result']=='cancelled'
    args,kwargs=calls[0]
    outer=base64.b64decode(args[-1]).decode('utf-16-le')
    assert '-Verb RunAs -Wait -PassThru' in outer
    assert kwargs.get('shell',False) is False and 'timeout' not in kwargs


def test_live_sidecar_and_evidence_failure_do_not_launch_helper(runtime,tmp_path,monkeypatch):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    called=[]
    sync=ClockSync(runner,tmp_path,repair=lambda *a:called.append(a) or {'result':'cancelled'})
    runner.bridge.sidecar_active=lambda:True
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='stop_failed' and not called
    runner.bridge.sidecar_active=lambda:False
    def broken(*args,**kwargs):raise OSError('synthetic_disk_full')
    monkeypatch.setattr(sync,'_record',broken)
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='evidence_failed' and not called
    assert storage.load_runtime_control()['update_no_new_work']


def test_parent_failure_revokes_ticket_and_does_not_leave_runner_dead(runtime,tmp_path):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    def broken(*args):raise OSError('synthetic_parent_failure')
    sync=ClockSync(runner,tmp_path,repair=broken)
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='failed' and sync.ticket.path.read_text()=='revoked'
    until(lambda:services.connection.prepared is not None)
    assert binding.run_status=='paused' and not storage.load_runtime_control()['update_no_new_work']


def test_offline_after_repair_does_not_claim_verified(runtime,backend_peer,tmp_path):
    runner,services,binding,*_=runtime
    monitor_for(services,tmp_path)
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    def repaired(*args):
        backend_peer.status=503
        return {'resync_completed':True,'helper_exit_confirmed':True,'result':'requested'}
    sync=ClockSync(runner,tmp_path,repair=repaired,verify_seconds=.2)
    try:
        sync.start();finish(sync)
        assert sync.snapshot()['state']=='unconfirmed' and binding.run_status=='paused'
    finally:backend_peer.status=200


@pytest.mark.parametrize('sample_changes', [dict(observed_monotonic=1),dict(comparison_status='skewed'),
    dict(backend_reference={'status':'skewed'}),dict(timezone={'beijing_timezone':False})])
def test_verification_rejects_old_samples_clock_jumps_or_wrong_zone(tmp_path,sample_changes):
    sample={'observed_monotonic':3,'comparison_status':'aligned','backend_reference':{'status':'aligned'},'timezone':ZONE,**sample_changes}
    services=SimpleNamespace(connection=SimpleNamespace(backend=SimpleNamespace(clock_diagnostics=SimpleNamespace(snapshot=lambda:sample))),startup_check_state=lambda:{'canStart':False})
    runner=SimpleNamespace(hook_services=services)
    tick=[2]
    def monotonic():tick[0]+=.1;return tick[0]
    sync=ClockSync(runner,tmp_path,monotonic=monotonic,verify_seconds=.2)
    assert sync._verify(2)=='unconfirmed'


def test_qt_bridge_blocks_update_rebind_resume_and_close_during_sync():
    from test_web_ui_binding_behavior import _headless_web_ui_module
    from unittest.mock import Mock
    with _headless_web_ui_module() as module:
        window=module.WorkerWebWindow.__new__(module.WorkerWebWindow)
        window._runtime_services_started=True
        window._shutdown_in_progress=False
        window.clock_sync=SimpleNamespace(start=Mock(),blocks_actions=lambda:True,
            snapshot=lambda:{'busy':True,'state':'repairing','message':'正在同步'})
        window.update_coordinator=SimpleNamespace(note_operator_pause=Mock(),check_for_updates=Mock())
        window._publish=Mock()
        window.api=SimpleNamespace(bind=Mock())
        window.runner=SimpleNamespace(set_run_status=Mock())
        bridge=module.WorkerWebBridge(window)
        bridge.syncComputerTime()
        window.clock_sync.start.assert_called_once()
        window.check_for_updates();window.bind_worker('synthetic','synthetic');window.set_accepting(True)
        window.update_coordinator.check_for_updates.assert_not_called()
        window.api.bind.assert_not_called();window.runner.set_run_status.assert_not_called()
        event=SimpleNamespace(ignore=Mock())
        window.closeEvent(event);event.ignore.assert_called_once()
        assert window._startup_check_state()['canStart'] is False


@pytest.mark.parametrize('result', ['requested', 'resync_failed'])
def test_delayed_resync_keeps_writers_stopped_until_synchronous_result(runtime, backend_peer, tmp_path, result):
    runner,services,binding,*_=runtime
    body=json.loads(backend_peer.body);body['data']['clock_reference']=REFERENCE
    backend_peer.body=json.dumps(body).encode()
    monitor_for(services,tmp_path)
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    entered=threading.Event();release=threading.Event()
    def repair(attempt,ticket):
        # External command stands for the blocking w32tm contract, not /nowait.
        with ClockSyncTicket(tmp_path).locked():
            entered.set();assert release.wait(5)
        return {'result':result,'helper_exit_confirmed':True,'resync_completed':result=='requested'}
    sync=ClockSync(runner,tmp_path,repair=repair,verify_seconds=3)
    try:
        sync.start();assert entered.wait(5)
        assert sync.snapshot()['busy'] and sync.snapshot()['state']=='repairing'
        assert not runner.thread.is_alive() and services.connection.prepared is None
        assert storage.load_runtime_control()['update_no_new_work']
        assert sync.blocks_actions() and not sync.start()
    finally:release.set();finish(sync)
    assert sync.snapshot()['state']==('verified' if result=='requested' else result)
    assert binding.run_status=='paused'


def test_asynchronous_acceptance_is_never_treated_as_completed_sync(runtime,tmp_path):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    sync=ClockSync(runner,tmp_path,repair=lambda *a:{'result':'requested','helper_exit_confirmed':True})
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='unconfirmed'
    assert not runner.thread.is_alive() and storage.load_runtime_control()['update_no_new_work']


@pytest.mark.parametrize('failure_stage', ['record','restart'])
def test_retry_keeps_owner_but_never_reauthorizes_late_old_helper(runtime,tmp_path,monkeypatch,failure_stage):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    release=threading.Event();done=threading.Event();attempts=[];old_authorized=[];threads=[]
    def late_helper(old_attempt):
        assert release.wait(8)
        with ClockSyncTicket(tmp_path).locked() as stream:
            stream.seek(0);old_authorized.append(stream.read(128).decode()==old_attempt)
        done.set()
    def repair(attempt,ticket):
        attempts.append(attempt)
        if len(attempts)==1:
            t=threading.Thread(target=late_helper,args=(attempt,),daemon=True);t.start();threads.append(t)
            return {'result':'elevation_failed','helper_exit_confirmed':False}
        release.set();assert done.wait(3)
        return {'result':'cancelled','helper_exit_confirmed':True}
    sync=ClockSync(runner,tmp_path,repair=repair)
    original_write=sync._write_record;original_start=runner.start;fail=[True]
    def write(state,**kwargs):
        if failure_stage=='record' and state=='verifying' and fail[0]:
            fail[0]=False;raise PermissionError(13,'private-secret')
        return original_write(state,**kwargs)
    def restart(*args,**kwargs):
        if failure_stage=='restart' and fail[0]:
            fail[0]=False;raise OSError('private-secret')
        return original_start(*args,**kwargs)
    monkeypatch.setattr(sync,'_write_record',write);monkeypatch.setattr(runner,'start',restart)
    try:
        sync.start();finish(sync)
        owner=storage.load_runtime_control()['update_request_id']
        assert storage.load_runtime_control()['update_no_new_work']
        assert sync.ticket.path.read_text()=='revoked'
        sync.start();finish(sync)
        assert len(set(attempts))==2 and owner not in attempts
        assert old_authorized==[False]
        assert sync.snapshot()['state']=='cancelled'
        assert not storage.load_runtime_control()['update_no_new_work']
    finally:
        release.set()
        for t in threads:t.join(3)


def fallback_in_zip(root,target):
    with zipfile.ZipFile(export_bundle(root,target)) as archive:
        files=[name for name in archive.namelist() if name.endswith('evidence-recorder-failures.jsonl')]
        assert files
        raw=archive.read(files[0]).decode()
    assert 'private-secret' not in raw
    return [json.loads(line) for line in raw.splitlines()]


@pytest.mark.parametrize('kind', ['permission','full','serialization'])
def test_actual_evidence_write_errors_reach_export_and_preserve_first_cause(runtime,tmp_path,monkeypatch,kind):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    target=tmp_path/'artifacts/runtime/clock-sync.tmp';original=Path.open
    def fail(path,*args,**kwargs):
        if path==target:
            if kind=='permission':raise PermissionError(13,'private-secret')
            if kind=='full':raise OSError(28,'private-secret')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',fail)
    if kind=='serialization':
        import chejin_worker_client.clock_sync as module
        monkeypatch.setattr(module.json,'dump',lambda *a,**k:(_ for _ in ()).throw(TypeError('private-secret')))
    called=[];sync=ClockSync(runner,tmp_path,repair=lambda *a:called.append(a))
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='evidence_failed' and not called
    assert storage.load_runtime_control()['update_no_new_work']
    records=fallback_in_zip(tmp_path,tmp_path/'errors.zip')
    assert any(r['context']['stage']=='record_stopping' for r in records)
    assert all(r['context']['first_failure']['stage']=='record_stopping' for r in records)
    assert all(valid_operation(r['context']['attempt_id']) for r in records)


@pytest.mark.parametrize('stage', ['drain','restart','ticket_authorize','ticket_revoke','helper'])
def test_lifecycle_exceptions_use_safe_fallback_in_actual_zip(runtime,tmp_path,monkeypatch,stage):
    runner,services,binding,*_=runtime
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    def failure(*a,**k):raise OSError(5,'private-secret')
    sync=ClockSync(runner,tmp_path,repair=lambda *a:{'result':'cancelled'})
    with monkeypatch.context() as patch:
        if stage=='drain':patch.setattr(runner,'stop_for_update',failure)
        elif stage=='restart':patch.setattr(runner,'start',failure)
        elif stage=='ticket_authorize':patch.setattr(sync.ticket,'authorize',failure)
        elif stage=='ticket_revoke':patch.setattr(sync.ticket,'revoke',failure)
        else:sync.repair=failure
        sync.start();finish(sync)
    records=fallback_in_zip(tmp_path,tmp_path/'lifecycle.zip')
    assert any(r['context']['stage']==stage for r in records)
    assert all(r['context']['first_failure']['stage']==stage for r in records)
    assert binding.run_status=='paused'


def test_completely_unwritable_disk_has_bounded_safe_stderr_fallback(tmp_path,monkeypatch,capsys):
    sync=ClockSync(SimpleNamespace(),tmp_path)
    sync._operation=operation();sync._attempt=operation()
    def fail(*a,**k):raise OSError(28,'private-secret')
    monkeypatch.setattr(Path,'open',fail)
    with pytest.raises(OSError):sync._record('stopping')
    err=capsys.readouterr().err
    assert len(err)<8192 and 'private-secret' not in err
    assert json.loads(err)['context']['stage']=='record_stopping'


def test_actual_uncertain_hook_call_is_not_settled_or_replayed_by_clock_sync(runtime,tmp_path):
    from chejin_worker_client import hook_active_calls
    runner,services,binding,*_=runtime
    monitor_for(services,tmp_path)
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    generation=services.connection.prepared.process.generation
    hook_active_calls.reserve(generation,'synthetic-original-send','send_text')
    hook_active_calls.finish(generation,'synthetic-original-send',False)
    def row():
        with storage.db_connection() as conn:
            return dict(conn.execute('SELECT * FROM hook_active_calls WHERE action_id=?',('synthetic-original-send',)).fetchone())
    before=row();calls=[]
    sync=ClockSync(runner,tmp_path,repair=lambda *args:calls.append(args) or {'result':'requested','helper_exit_confirmed':True,'resync_completed':True},verify_seconds=.3)
    sync.start();finish(sync)
    assert len(calls)==1
    assert row()==before and hook_active_calls.unresolved(generation)
    assert sync.snapshot()['state']=='unconfirmed'
    assert not services.startup_check_state()['canStart']
    assert binding.run_status in {'paused','faulted'}


@pytest.mark.parametrize('offset', [-86400,86400])
def test_large_clock_correction_uses_new_heartbeat_not_old_wall_time(runtime,backend_peer,tmp_path,monkeypatch,offset):
    from chejin_worker_client import hook_backend
    runner,services,binding,*_=runtime
    body=json.loads(backend_peer.body);body['data']['clock_reference']=REFERENCE
    backend_peer.body=json.dumps(body).encode()
    delta=[offset]
    monkeypatch.setattr(hook_backend,'time',SimpleNamespace(time=lambda:time.time()+delta[0],monotonic=time.monotonic))
    monitor=monitor_for(services,tmp_path)
    runner.start(binding)
    until(lambda:monitor.snapshot().get('comparison_status')=='skewed')
    old=monitor.snapshot()['observed_monotonic']
    def repair(*args):
        assert not runner.thread.is_alive()
        delta[0]=0
        return {'result':'requested','helper_exit_confirmed':True,'resync_completed':True}
    sync=ClockSync(runner,tmp_path,repair=repair,verify_seconds=3)
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='verified'
    assert monitor.snapshot()['observed_monotonic']>old
    assert binding.run_status=='paused'


def test_real_clock_producer_to_qt_and_shipped_ui_unknown_states_then_recovery(runtime,backend_peer,tmp_path):
    from test_web_ui_binding_behavior import _headless_web_ui_module
    runner,services,binding,profiles,errors,statuses,*_=runtime
    body=json.loads(backend_peer.body);body['data']['clock_reference']=REFERENCE
    backend_peer.body=json.dumps(body).encode()
    monitor=monitor_for(services,tmp_path)
    runner.start(binding);until(lambda:services.connection.prepared is not None)
    sync=ClockSync(runner,tmp_path,repair=lambda *a:{'result':'requested','helper_exit_confirmed':True,'resync_completed':True},verify_seconds=3)
    sync.start();finish(sync);assert sync.snapshot()['state']=='verified'
    observed=[]
    def collect(case,expected):
        with _headless_web_ui_module() as ui:
            projection=ui.WorkerWebWindow._startup_check_state(SimpleNamespace(clock_sync=sync,connection_status=statuses[-1],runner=runner))
        assert projection['clockCheck']['verification']['status']==expected
        assert projection['clockCheck']['notice']==''
        observed.append({'case':case,'clockSync':sync.snapshot(),'startupCheck':projection})
    collect('verified','verified')
    try:
        backend_peer.status=503
        until(lambda:monitor.snapshot().get('reason')=='heartbeat_failed' and statuses[-1]=='connecting')
        collect('http_failure','unconfirmed')
        backend_peer.status=200
        until(lambda:monitor.snapshot()['comparison_status']=='aligned')
        collect('network_recovered','verified')
        runner.stop_for_update(5)
        age=[0]
        monitor=ClockDiagnostics(tmp_path,zone_reader=lambda:ZONE,monotonic=lambda:time.monotonic()+age[0])
        services.connection.backend=replace(services.connection.backend,clock_diagnostics=monitor)
        collect('startup_pending','pending')
        runner.start(binding);until(lambda:monitor.snapshot()['comparison_status']=='aligned')
        runner.stop_for_update(5);age[0]=121
        collect('sample_expired','stale')
        age[0]=0
        body['data'].pop('clock_reference');backend_peer.body=json.dumps(body).encode()
        runner.start(binding)
        until(lambda:monitor.snapshot()['comparison_status']=='aligned' and monitor.snapshot()['backend_reference']['status']=='unconfirmed')
        collect('reference_unknown','unconfirmed')
        body['data']['clock_reference']=REFERENCE;backend_peer.body=json.dumps(body).encode()
        until(lambda:monitor.snapshot()['backend_reference']['status']=='aligned')
        collect('reference_recovered','verified')
        data=tmp_path/'producer-ui.json';data.write_text(json.dumps(observed,ensure_ascii=False))
        repo=Path(__file__).resolve().parents[2]
        probe=repo/'packages/worker-ui-baseline/tests/clock-producer-ui.mjs'
        result=subprocess.run(['node',str(probe),str(data)],capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stdout+result.stderr
        assert '"actions":0' in result.stdout
        assert binding.run_status=='paused'
    finally:backend_peer.status=200
