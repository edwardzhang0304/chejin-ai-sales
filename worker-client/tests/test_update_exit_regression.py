"""Synthetic regressions for opaque argv, retained writers and normal cancellation."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from chejin_worker_client import chejin_updater as updater
from chejin_worker_client import update_coordinator as coordinator
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime, HookRuntimeStopping
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_connection import HookConnectionManager


@pytest.mark.parametrize('mode', ['install','recovery','new','rollback'])
@pytest.mark.parametrize('token', ['-synthetic_secret', '--synthetic_secret', 'ordinary_synthetic'])
def test_opaque_token_survives_every_process_boundary(mode, token, monkeypatch, tmp_path):
    captured=[]
    monkeypatch.setattr(updater.subprocess,'Popen',lambda argv,**kw:captured.append(argv))
    exe=tmp_path/'worker';plan=tmp_path/'plan.json'
    if mode=='install':coordinator.UpdateCoordinator._launch_updater(exe,plan,token)
    elif mode=='recovery':coordinator.UpdateCoordinator._launch_missing_result_recovery(exe,plan,token,1)
    else:updater._start_worker(exe,plan_path=plan,token=token,rollback=mode=='rollback')
    parser=argparse.ArgumentParser()
    for opt in ['--plan','--token','--post-update-plan','--post-rollback-plan','--post-update-token','--current-pid']:parser.add_argument(opt)
    parser.add_argument('--recover-missing-result',action='store_true')
    args=parser.parse_args(captured[0][1:])
    assert (args.token or args.post_update_token)==token


def test_normal_stop_does_not_create_business_fault_but_real_fault_still_wins():
    rt=HookFormalRuntime.__new__(HookFormalRuntime)
    rt.fault_check=lambda:False;rt.stop_check=lambda:True
    with pytest.raises(HookRuntimeStopping):rt.barrier()
    with pytest.raises(HookRuntimeStopping):rt._binding_barrier({'peer_wxid':'synthetic'})
    rt.fault_check=lambda:True
    with pytest.raises(ValueError,match='HOOK_LOCAL_PERSISTENCE_FAULT'):rt.barrier()


def test_business_cancellation_preserves_paused_status(monkeypatch):
    stop=threading.Event()
    runner=SimpleNamespace(stop_event=stop,binding=SimpleNamespace(run_status='paused'))
    def tick(*a):stop.set();raise HookRuntimeStopping()
    services=HookWorkerServices(None,None,intake_interval_seconds=.1,projection_batch_size=1,
                               dispatcher=SimpleNamespace(tick=tick))
    monkeypatch.setattr(services,'_fault',lambda *a,**kw:pytest.fail('normal stop became a fault'))
    services.business_loop(runner,lambda _:None)
    assert runner.binding.run_status=='paused'


def test_connection_lock_wait_is_bounded_without_stealing_owner():
    manager=HookConnectionManager.__new__(HookConnectionManager)
    manager._operation_lock=threading.RLock()
    ready=threading.Event();release=threading.Event()
    def holder():
        with manager._operation_lock:ready.set();release.wait(2)
    thread=threading.Thread(target=holder);thread.start();assert ready.wait(1)
    try:
        started=time.monotonic()
        with pytest.raises(Exception,match='HOOK_RECEIVER_STOP_UNCONFIRMED'):
            manager.close(deadline=started+.03)
        assert time.monotonic()-started<.5 and thread.is_alive()
    finally:release.set();thread.join(1)


def test_timeout_evidence_lists_all_planned_processes_without_paths(monkeypatch,tmp_path):
    diagnostic=tmp_path/'updater-startup.jsonl'
    monkeypatch.setenv('CHEJIN_UPDATER_DIAGNOSTIC_PATH',str(diagnostic))
    monkeypatch.setattr(updater,'identity_alive',lambda ident:ident['pid']==20)
    plan={'old_exit_timeout_seconds':.001,'old_process_identity':{'pid':10,'create_time':1,'exe':'secret-path'},
          'old_child_identities':[{'pid':20,'create_time':2,'exe':'secret-path'}]}
    assert not updater.wait_for_old_writers(plan)
    text=diagnostic.read_text();assert 'secret-path' not in text
    sample=json.loads(text.splitlines()[-1])
    assert [(p['pid'],p['state']) for p in sample['processes']]==[(10,'exited'),(20,'alive')]


def test_unreadable_identity_never_counts_as_exited(monkeypatch,tmp_path):
    path=tmp_path/'updater-startup.jsonl';monkeypatch.setenv('CHEJIN_UPDATER_DIAGNOSTIC_PATH',str(path))
    def fail(_):raise RuntimeError('UPDATE_PROCESS_IDENTITY_UNVERIFIABLE')
    monkeypatch.setattr(updater,'identity_alive',fail)
    with pytest.raises(RuntimeError):
        updater.wait_for_old_writers({'old_exit_timeout_seconds':1,'old_process_identity':{'pid':10,'create_time':1},'old_child_identities':[]})
    assert json.loads(path.read_text().splitlines()[-1])['processes'][0]['state']=='unverifiable'


def test_managed_stop_owns_callback_cleanup_once():
    from chejin_worker_client.task_runner import TaskRunner
    runner=TaskRunner.__new__(TaskRunner)
    runner.stop_event=threading.Event();runner._task_wake_event=threading.Event()
    runner.c2_manual_scan_requested=threading.Event();runner.poll_interval_seconds=.01
    runner.c2_thread=runner.hook_business_thread=runner.thread_monitor=None
    runner._mark_background_loop_entered=lambda _:None
    runner.tick_once=lambda:runner.stop_event.wait(.01)
    closed=[]
    runner.hook_services=SimpleNamespace(close=lambda **kw:closed.append(kw['deadline']))
    runner.thread=threading.Thread(target=runner._hook_loop)
    runner.thread.start()
    try:
        runner.stop_for_update(1)
        assert not runner.thread.is_alive() and len(closed)==1
    finally:
        runner.stop_event.set();runner.thread.join(1)


def test_bad_arguments_leave_exit_code_without_opaque_token(monkeypatch,tmp_path):
    path=tmp_path/'updater-startup.jsonl'
    monkeypatch.setenv('CHEJIN_UPDATER_DIAGNOSTIC_PATH',str(path))
    with pytest.raises(SystemExit) as exc:
        updater.main(['--plan',str(tmp_path/'plan'),'--token','-private-synthetic'])
    assert exc.value.code==2
    raw=path.read_text();assert 'private-synthetic' not in raw
    record=json.loads(raw.splitlines()[-1]);assert record['phase']=='argument_parse_failed' and record['exit_code']==2


def test_shutdown_records_in_one_click_export(tmp_path,monkeypatch):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    from test_diagnostic_bundle import seed,read_zip
    from chejin_worker_client.update_diagnostics import record_update_handoff
    root=seed(tmp_path/'home');updates=tmp_path/'updates'
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(updates))
    path=updates/'requests/synthetic/control/worker-shutdown.jsonl'
    record_update_handoff(path,'updater_exited_before_ready',exit_code=2,token_leading_hyphen=True,token='private-synthetic')
    entries=read_zip(export_bundle(root,tmp_path/'out.zip',lambda _:None))
    content=entries['logs/update/requests/synthetic/control/worker-shutdown.jsonl']
    assert b'updater_exited_before_ready' in content and b'private-synthetic' not in content


def test_cancellation_marker_survives_state_save_failure_and_blocks_updater(monkeypatch,tmp_path):
    c=coordinator.UpdateCoordinator.__new__(coordinator.UpdateCoordinator)
    c._lock=threading.RLock();c._exit_plan_path=tmp_path/'update-plan.json'
    c._exit_request_token='synthetic-only';c._exit_request_id='synthetic-request'
    def fail():raise OSError('synthetic state disk failure')
    c.store=SimpleNamespace(load=fail)
    with pytest.raises(OSError):c.report_normal_exit_result(stopped=False)
    assert c.ensure_shutdown_cancelled()
    with pytest.raises(updater.ClientUpdateError,match='本次更新已取消'):
        updater._check_shutdown_outcome({'update_request_id':'synthetic-request'},c._exit_plan_path,c._exit_request_token)
    # Existing authenticated cancellation needs no further successful write.
    monkeypatch.setattr(coordinator,'_atomic_json_write',lambda *a:fail())
    assert c.ensure_shutdown_cancelled()
    (tmp_path/'worker-shutdown-failed.json').unlink()
    assert not c.ensure_shutdown_cancelled()


@pytest.mark.parametrize('failure', ['state_load', 'state_save', 'cancel_write'])
def test_missing_result_recovery_cancellation_survives_io_failure(failure, monkeypatch, tmp_path):
    """Real recovery entry, shared close and recovery consumer; no directory swap."""
    from test_update_coordinator import _missing_result_plan, FakeApi, FakeRunner, FakeProcess, _release
    from chejin_worker_client.update_shutdown import prepare_window_close
    token = 'synthetic-recovery-only'
    store, path, plan = _missing_result_plan(tmp_path, request_id='synthetic-recovery', token=token)
    launches, exits = [], []

    def launch(exe, plan_path, supplied_token, pid):
        assert supplied_token == token and plan_path == path
        launches.append(1)
        (path.parent / 'missing-result-recovery-ready.json').write_text(
            json.dumps({'ready': True, 'update_request_id': plan['update_request_id']}))
        return FakeProcess()

    c = coordinator.UpdateCoordinator(
        FakeApi(_release()), FakeRunner(None, []), binding_provider=lambda: None,
        on_state=lambda _: None, request_normal_exit=lambda: exits.append(1),
        state_store=store, formal_package=True, recovery_updater_launcher=launch,
        process_identity=lambda _: None, current_program_dir=Path(plan['current_program_dir']))
    c.set_post_update_context(plan, token)
    assert c._recover_missing_update_result(store.load(), plan, path, path.parent / 'update-result.json') == 'exit_requested'
    assert launches == exits == [1]
    assert c._exit_plan_path == path and c._exit_request_id == plan['update_request_id']
    busy, stops = [True], []

    def stop(budget, **kwargs):
        stops.append(budget)
        if busy[0]:
            raise RuntimeError('UPDATE_WRITERS_NOT_STOPPED')

    def disk_failure(*args, **kwargs):
        raise OSError('synthetic IO failure')

    window = SimpleNamespace(
        _closing_for_update=True, update_coordinator=c,
        runner=SimpleNamespace(hook_services=object(), stop_for_update=stop),
        error_signal=SimpleNamespace(emit=lambda _: None))
    original_write = coordinator._atomic_json_write
    with monkeypatch.context() as io:
        # Fail after the real handoff, not during context validation/launch.
        io.setattr(store, 'save' if failure == 'state_save' else 'load', disk_failure)
        if failure == 'cancel_write':
            io.setattr(coordinator, '_atomic_json_write', disk_failure)
        assert prepare_window_close(window) is False
        busy[0] = False
        if failure == 'cancel_write':
            assert prepare_window_close(window) is False
            assert len(stops) == 1  # Cancellation failure blocks even stopping again.
            io.setattr(coordinator, '_atomic_json_write', original_write)
        assert prepare_window_close(window) is True
    assert len(stops) == 2 and not window._update_cancellation_pending
    assert launches == exits == [1]
    marker = path.parent / 'worker-shutdown-failed.json'
    assert marker.is_file()
    with monkeypatch.context() as io:
        io.setattr(coordinator, '_atomic_json_write', disk_failure)
        assert c.ensure_shutdown_cancelled()  # Existing authenticated marker is idempotent.
    # Execute the actual recovery consumer through validation and cancellation.
    monkeypatch.setattr(updater, 'wait_for_pid_exit', lambda *args: True)
    real_replace = updater.os.replace
    def guarded_replace(source, target):
        if Path(source).is_dir():
            pytest.fail('cancelled recovery swapped directories')
        return real_replace(source, target)
    monkeypatch.setattr(updater.os, 'replace', guarded_replace)
    assert updater.run_missing_result_recovery(path, token, 12345) == 1
    result = json.loads((path.parent / 'update-result.json').read_text())
    assert result['failure_code'] == 'UPDATE_WRITERS_NOT_STOPPED'
    assert Path(plan['current_program_dir']).is_dir() and Path(plan['previous_program_dir']).is_dir()
    assert not Path(plan['failed_program_dir']).exists()
