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
def test_real_runner_stops_repairs_reconnects_fresh_time_keeps_unknown_and_pause(runtime, backend_peer, tmp_path, status):
    runner, services, binding, _, _, _, posted, _ = runtime
    root = tmp_path/'home'
    journal = root/'transactions/original.json'; journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"state":"unknown","do_not_resend":true}')
    digest = hashlib.sha256(journal.read_bytes()).hexdigest()
    body = json.loads(backend_peer.body); body['data']['clock_reference'] = REFERENCE
    backend_peer.body = json.dumps(body).encode()
    monitor = monitor_for(services, root)
    runner.start(binding)
    until(lambda: services.connection.prepared is not None and monitor.snapshot()['comparison_status']=='aligned')
    binding.run_status = status; storage.save_binding(binding)
    initial = monitor.snapshot()['observed_monotonic']
    repaired = []
    def repair(op, ticket):
        assert storage.load_runtime_control()['update_request_id'] == op
        assert runner.stop_event.is_set()
        assert all(t is None or not t.is_alive() for t in (runner.thread, runner.hook_intake_thread, runner.hook_business_thread, runner.thread_monitor))
        assert services.connection.prepared is None
        assert Path(ticket).read_text() == op
        assert binding.run_status == ('faulted' if status == 'faulted' else 'paused')
        repaired.append(op)
        return {'result':'requested', 'helper_exit_confirmed':True}
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
    sync=ClockSync(runner,tmp_path,repair=lambda *args:{'result':'requested'},verify_seconds=.25)
    sync.start();finish(sync)
    assert sync.snapshot()['state']=='unconfirmed'
    assert binding.run_status=='paused'


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
    assert len(called)==1 and called[0][0]==owned
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
    assert 'ntp.aliyun.com,0x8' in script and 'China Standard Time' in script
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
        return {'result':'requested'}
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
