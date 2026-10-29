"""Real TaskRunner/HTTP/SQLite lifecycle; only Windows/DLL facts are synthetic."""
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
import hashlib
import json
import time
import zipfile
import pytest
from test_clock_diagnostics import ZONE
from test_hook_worker_services import runtime, lab, backend_peer, until, callback, projected_count
from chejin_worker_client.clock_diagnostics import ClockDiagnostics
from chejin_worker_client.diagnostic_bundle import export_bundle


def stopped(runner):
    runner.stop()
    until(lambda:not runner.thread.is_alive(), seconds=6)
    for thread in (runner.hook_intake_thread, runner.thread_monitor):
        if thread:
            thread.join(5)
            assert not thread.is_alive()


def test_formal_runner_rebinds_keep_new_heartbeats_failures_and_zip(runtime, backend_peer, tmp_path):
    runner,services,binding,profiles,errors,statuses,posted,_=runtime
    root=tmp_path/'diagnostic-home'
    journal=root/'transactions/original.json';journal.parent.mkdir(parents=True)
    journal.write_text('{"state":"unknown"}')
    original_hash=hashlib.sha256(journal.read_bytes()).hexdigest()
    monitor=ClockDiagnostics(root,zone_reader=lambda:ZONE)
    services.connection.backend=replace(services.connection.backend,clock_diagnostics=monitor)
    observed=[]
    for iteration in range(3):
        count=len(backend_peer.requests)
        runner.start(binding)
        until(lambda:services.connection.prepared is not None and len(backend_peer.requests)>count
              and monitor.snapshot()['comparison_status']=='aligned')
        observed.append(monitor.snapshot()['observed_monotonic'])
        if iteration==2:
            backend_peer.status=503
            until(lambda:monitor.snapshot().get('reason')=='heartbeat_failed')
            assert binding.run_status=='paused'
            backend_peer.status=200
            until(lambda:monitor.snapshot()['comparison_status']=='aligned')
            # Exercise callback ingestion after the final rebind. On macOS the
            # existing exclusive receiver port may remain in TIME_WAIT after
            # a callback; this test does not alter its ownership protection.
            callback(services.connection)
            until(lambda:projected_count()==1)
        stopped(runner)
        services.close()  # repeated close remains reusable
    assert observed==sorted(set(observed))
    assert projected_count()==1 and profiles and not errors and set(posted)<={'paused'}
    assert monitor.flush()
    bundle=export_bundle(root,tmp_path/'rebound.zip')
    with zipfile.ZipFile(bundle) as archive:
        saved=json.loads(archive.read('logs/runtime/clock-check.json'))
    assert saved['session_id']==monitor.session
    assert saved['latest']['observed_monotonic'] >= observed[-1]
    assert any(item.get('reason')=='heartbeat_failed' for item in saved['recent_checks'])
    assert saved['latest']['comparison_status']=='aligned'
    assert hashlib.sha256(journal.read_bytes()).hexdigest()==original_hash


def test_slow_old_writer_rebind_reuses_owner_and_update_deadline_stays_closed(runtime, backend_peer, tmp_path, monkeypatch):
    runner,services,binding,profiles,errors,statuses,posted,_=runtime
    entered,release=Event(),Event();lock=Lock();active=[0];peak=[0]
    original=ClockDiagnostics._write_output
    def blocked(self,output):
        with lock:
            active[0]+=1;peak[0]=max(peak[0],active[0])
        try:
            entered.set();assert release.wait(20)
            original(self,output)
        finally:
            with lock:active[0]-=1
    monkeypatch.setattr(ClockDiagnostics,'_write_output',blocked)
    monitor=ClockDiagnostics(tmp_path/'diagnostic-home',zone_reader=lambda:ZONE)
    services.connection.backend=replace(services.connection.backend,clock_diagnostics=monitor)
    try:
        runner.start(binding)
        until(lambda:services.connection.prepared is not None and monitor.snapshot()['comparison_status']=='aligned')
        assert entered.wait(1)
        stopped(runner)  # optional diagnostics still writing after bounded close
        prior=monitor.snapshot()['observed_monotonic']
        with pytest.raises(RuntimeError,match='UPDATE_WRITERS_NOT_STOPPED'):
            runner.stop_for_update(.1)
        assert not monitor.flush(0)
        runner.start(binding)
        until(lambda:services.connection.prepared is not None
              and monitor.snapshot()['observed_monotonic']>prior)
        assert peak[0]==1 and monitor._writer_running
        assert binding.run_status=='paused' and not errors and set(posted)<={'paused'}
    finally:
        release.set();assert monitor.flush()
        runner.stop_for_update(5)
    saved=json.loads(monitor.path.read_text())
    assert saved['latest']['observed_monotonic']>prior
    assert peak[0]==1
