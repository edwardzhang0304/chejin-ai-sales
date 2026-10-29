from datetime import datetime, timezone
from types import SimpleNamespace
import json
import zipfile
import pytest
from chejin_worker_client.clock_diagnostics import ClockDiagnostics, compare_clock, clock_notice, reference_summary
from chejin_worker_client.hook_clock import HookBackendClock
from chejin_worker_client.diagnostic_bundle import export_bundle

STAMP = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
ZONE = {'utc_offset_seconds': 28800, 'windows_zone': 'China Standard Time', 'beijing_timezone': True}
REFERENCE = {'source': 'aliyun_ntp', 'host': 'ntp.aliyun.com', 'status': 'aligned',
    'authenticated': False, 'business_timezone': 'Asia/Shanghai', 'utc_offset_seconds': 28800,
    'sample_age_seconds': 2, 'offset_lower_seconds': -.01, 'offset_upper_seconds': .01, 'round_trip_seconds': .02}


def receipt(offset=0, rtt=.05):
    return SimpleNamespace(server_time_utc=STAMP.isoformat(), request_started_monotonic=10,
        response_received_monotonic=10+rtt, request_started_wall=STAMP.timestamp()+offset,
        response_received_wall=STAMP.timestamp()+offset+rtt, clock_reference=REFERENCE)


@pytest.mark.parametrize('offset', [0, 16, -16, 28800, -28800])
def test_offset_and_beijing_display_without_changing_original_or_permission(offset):
    sample = receipt(offset)
    before = vars(sample).copy()
    result = compare_clock(sample, ZONE)
    assert result['comparison_status'] == ('aligned' if offset == 0 else 'skewed')
    assert result['offset_lower_seconds'] <= offset+.025 <= result['offset_upper_seconds']
    assert result['server_time_beijing'] == '2026-10-29T12:00:00+08:00'
    assert vars(sample) == before
    business = HookBackendClock(monotonic=lambda:10.05)
    business.observe(sample)
    assert abs(business.now().timestamp()-STAMP.timestamp()) < .01


def test_slow_network_does_not_accuse_computer_and_wall_jump_is_explicit():
    result = compare_clock(receipt(30, rtt=3), ZONE)
    assert result['comparison_status'] == 'unconfirmed'
    assert not clock_notice(result)
    sample = receipt(); sample.response_received_wall += 3600
    result = compare_clock(sample, ZONE)
    assert result['reason'] == 'computer_clock_changed_during_request'
    assert '发生变化' in clock_notice(result)


def test_timezone_is_separate_from_clock_and_unknown_ali_reference():
    sample = receipt(); sample.clock_reference = None
    result = compare_clock(sample, {**ZONE, 'beijing_timezone': False, 'utc_offset_seconds': 0})
    assert result['comparison_status'] == 'aligned'
    assert result['backend_reference']['status'] == 'unconfirmed'
    assert '时区' in clock_notice(result)
    assert '同步电脑时间' not in clock_notice(result)


@pytest.mark.parametrize('changes', [dict(status='invented'), dict(host='private-secret.example'),
    dict(sample_age_seconds=301), dict(round_trip_seconds=float('nan')), dict(offset_upper_seconds=-2)])
def test_reference_rejects_unconfirmed_or_invalid_metadata(changes):
    assert reference_summary({**REFERENCE, **changes}) == {'status': 'unconfirmed'}


def test_desktop_zip_contains_startup_failure_recovery_and_latest_without_touching_journal(tmp_path):
    root = tmp_path/'home'; root.mkdir()
    journal = root/'transactions'/'original.json'; journal.parent.mkdir(); journal.write_text('{"state":"unknown"}')
    tick = [0]
    monitor = ClockDiagnostics(root, monotonic=lambda:tick[0], zone_reader=lambda:ZONE)
    monitor.failed()
    tick[0] = 10.05; monitor.observe(receipt(16))
    tick[0] = 70.05; monitor.observe(receipt())
    assert monitor.flush()
    output = export_bundle(root, tmp_path/'evidence.zip')
    with zipfile.ZipFile(output) as archive:
        result = json.loads(archive.read('logs/runtime/clock-check.json'))
    assert result['startup']['comparison_status'] == 'pending'
    assert [r['comparison_status'] for r in result['recent_checks']] == ['pending','unconfirmed','skewed','aligned']
    assert result['latest']['backend_reference']['source'] == 'aliyun_ntp'
    assert result['latest']['round_trip_seconds'] > 0
    assert journal.read_text() == '{"state":"unknown"}'
    tick[0] += 121
    assert monitor.snapshot()['comparison_status'] == 'stale'
    assert monitor.snapshot()['backend_reference']['status'] == 'unconfirmed'
    new = ClockDiagnostics(root, monotonic=lambda:tick[0], zone_reader=lambda:ZONE)
    assert new.snapshot()['comparison_status'] == 'pending'  # no old success on restart
    assert new.flush()
    restarted_zip = export_bundle(root, tmp_path/'restarted.zip')
    with zipfile.ZipFile(restarted_zip) as archive:
        previous = json.loads(archive.read('logs/runtime/clock-check.previous-1.json'))
        current = json.loads(archive.read('logs/runtime/clock-check.json'))
    assert previous['session_id'] == monitor.session
    assert 'skewed' in [r['comparison_status'] for r in previous['recent_checks']]
    assert current['session_id'] == new.session and current['latest']['comparison_status'] == 'pending'
    assert journal.read_text() == '{"state":"unknown"}'


def test_evidence_is_bounded_and_ui_snapshot_does_not_read_disk(tmp_path, monkeypatch):
    tick = [0]; monitor = ClockDiagnostics(tmp_path, monotonic=lambda:tick[0], zone_reader=lambda:ZONE)
    for i in range(100):
        tick[0] += 61; monitor.observe(receipt(i % 2 * 10))
    assert monitor.flush()
    value = json.loads(monitor.path.read_text())
    assert len(value['recent_checks']) == 32
    assert monitor.path.stat().st_size < 65536
    monkeypatch.setattr(type(monitor.path), 'read_text', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('UI disk read')))
    assert monitor.snapshot()['notice']


def test_real_heartbeat_feeds_monitor_and_failures_do_not_change_error_contract(tmp_path, monkeypatch):
    from dataclasses import replace
    from chejin_worker_client.hook_backend import HookIdentityClient
    from chejin_worker_client.api import ApiError
    monitor = ClockDiagnostics(tmp_path, zone_reader=lambda:ZONE)
    client = HookIdentityClient('http://127.0.0.1/api', 2, 65536, monitor)
    sample = receipt(10)
    monkeypatch.setattr(HookIdentityClient, '_heartbeat', lambda *args:sample)
    assert client.heartbeat(None,None,None) is sample
    assert monitor.snapshot()['comparison_status'] == 'skewed'
    def broken(*args): raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'offline', 0)
    monkeypatch.setattr(HookIdentityClient, '_heartbeat', broken)
    with pytest.raises(ApiError, match='offline'): client.heartbeat(None,None,None)
    assert monitor.snapshot()['reason'] == 'heartbeat_failed'
    def diagnostic_failure(*args): raise OSError('synthetic')
    monkeypatch.setattr(monitor, 'observe', diagnostic_failure)
    monkeypatch.setattr(HookIdentityClient, '_heartbeat',lambda *args:sample)
    assert client.heartbeat(None,None,None) is sample
    assert monitor.flush()
    assert monitor.snapshot()['reason']=='diagnostic_observation_failed'
    assert json.loads(monitor.path.read_text())['latest']['exception_type']=='OSError'


def test_hook_display_always_beijing_even_when_computer_timezone_is_utc(monkeypatch):
    import time
    from chejin_worker_client.hook_progress import emit_progress
    old = __import__('os').environ.get('TZ')
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    try:
        events=[]
        runner=SimpleNamespace(on_runtime_process=events.append)
        emit_progress(runner,'connection','environment','done',occurred_at=STAMP.isoformat())
        # Display conversion preserves the original instant, regardless of OS zone.
        assert events[0]['time'].endswith('12:00') or events[0]['time']=='12:00:00'
    finally:
        if old is None: __import__('os').environ.pop('TZ',None)
        else: __import__('os').environ['TZ']=old
        time.tzset()


def test_slow_evidence_writer_never_delays_heartbeat_and_queue_is_bounded(tmp_path, monkeypatch):
    import threading
    from chejin_worker_client.hook_backend import HookIdentityClient
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = ClockDiagnostics._write_output
    def slow(self, output):
        entered.set()
        release.wait(5)
        original(self, output)
    monkeypatch.setattr(ClockDiagnostics, '_write_output', slow)
    tick = [0]
    monitor = ClockDiagnostics(tmp_path, monotonic=lambda:tick[0], zone_reader=lambda:ZONE)
    assert entered.wait(1)
    client = HookIdentityClient('http://127.0.0.1/api', 2, 65536, monitor)
    sample = receipt()
    monkeypatch.setattr(HookIdentityClient, '_heartbeat', lambda *args:sample)
    results=[]
    def heartbeat():
        results.append(client.heartbeat(None,None,None))
        returned.set()
    thread=threading.Thread(target=heartbeat)
    thread.start()
    try:
        assert returned.wait(.5), 'disk write held authenticated heartbeat'
        assert results == [sample]
        from chejin_worker_client.api import ApiError
        error=ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic-offline',0)
        def failed(*args): raise error
        monkeypatch.setattr(HookIdentityClient, '_heartbeat', failed)
        failures=[]
        def failed_heartbeat():
            try: client.heartbeat(None,None,None)
            except ApiError as exc: failures.append(exc)
        failed_thread=threading.Thread(target=failed_heartbeat)
        failed_thread.start(); failed_thread.join(.5)
        assert failures == [error], 'disk write delayed original network error'
        assert monitor.snapshot()['reason']=='heartbeat_failed'
        business=HookBackendClock(monotonic=lambda:10.05)
        business.observe(results[0])
        assert abs(business.now().timestamp()-STAMP.timestamp()) < .01
        for i in range(100):
            tick[0] += 61
            monitor.observe(receipt(i % 2 * 16))
        assert monitor.snapshot()['comparison_status']=='skewed'
        assert len(monitor._pending_write['recent_checks']) == 32
        assert monitor._pending_write['persistence']['omitted_checks'] > 0
        assert monitor._pending_write['persistence']['coalesced_snapshots'] > 0
        assert not monitor.flush(timeout=0)
        assert monitor.snapshot()['evidence_write_status']=='drain_timeout'
    finally:
        release.set(); thread.join(2)
        assert monitor.flush()
    saved=json.loads(monitor.path.read_text())
    assert saved['latest']['comparison_status']=='skewed'
    assert saved['startup']['comparison_status']=='pending'


def test_restart_evidence_retention_is_bounded(tmp_path):
    sessions=[]
    for _ in range(6):
        monitor=ClockDiagnostics(tmp_path,zone_reader=lambda:ZONE)
        monitor.observe(receipt(16)); assert monitor.flush()
        sessions.append(monitor.session)
    files=list(monitor.path.parent.glob('clock-check*.json'))
    assert len(files)==4
    assert {json.loads(f.read_text())['session_id'] for f in files} == set(sessions[-4:])


def test_bad_previous_json_preserved_with_hash_and_export_gap(tmp_path):
    import hashlib
    root=tmp_path/'home'
    path=root/'artifacts/runtime/clock-check.json'
    path.parent.mkdir(parents=True)
    raw=b'{corrupt clock evidence'
    path.write_bytes(raw)
    monitor=ClockDiagnostics(root,zone_reader=lambda:ZONE)
    assert monitor.flush()
    assert path.with_name('clock-check.previous-1.json').read_bytes()==raw
    output=export_bundle(root,tmp_path/'evidence.zip')
    with zipfile.ZipFile(output) as archive:
        value=json.loads(archive.read('logs/runtime/clock-check.json'))
    prior=value['persistence']['previous_files']
    assert prior==[{'file':'clock-check.previous-1.json','status':'invalid_json_preserved',
                   'sha256':hashlib.sha256(raw).hexdigest()}]


def test_failed_rotation_preserves_previous_and_retry_recovers(tmp_path, monkeypatch):
    import os,hashlib
    original=ClockDiagnostics(tmp_path,zone_reader=lambda:ZONE)
    original.observe(receipt(16)); assert original.flush()
    raw=original.path.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
    real_replace=os.replace; failures=[]
    blocked=[True]
    def replace(source,destination):
        if blocked[0] and source==original.path: raise OSError('synthetic write failure')
        return real_replace(source,destination)
    monkeypatch.setattr('chejin_worker_client.clock_diagnostics.os.replace',replace)
    monkeypatch.setattr('chejin_worker_client.failure_evidence.record_capture_failure',lambda *a:failures.append(a))
    tick=[0]
    monitor=ClockDiagnostics(tmp_path,monotonic=lambda:tick[0],zone_reader=lambda:ZONE)
    assert monitor.flush()
    assert failures and monitor.snapshot()['evidence_write_status']=='failed'
    assert hashlib.sha256(monitor.path.read_bytes()).hexdigest()==digest
    blocked[0]=False; tick[0]=61; monitor.observe(receipt()); assert monitor.flush()
    assert monitor.snapshot()['evidence_write_status']=='written'
    assert monitor.path.with_name('clock-check.previous-1.json').read_bytes()==raw


def test_bad_timezone_read_does_not_prevent_monitor_start(tmp_path):
    def failed(): raise OSError('synthetic registry failure')
    monitor=ClockDiagnostics(tmp_path,zone_reader=failed)
    monitor.observe(receipt()); assert monitor.flush()
    assert monitor.snapshot()['timezone']=={'beijing_timezone':None,'status':'unconfirmed'}
    assert not monitor.snapshot()['notice']


def test_failed_write_reports_gap_without_replacing_last_good_file(tmp_path, monkeypatch):
    from pathlib import Path
    monitor=ClockDiagnostics(tmp_path,zone_reader=lambda:ZONE)
    assert monitor.flush(); before=monitor.path.read_bytes()
    real_write=Path.write_text; failures=[]
    def failed_write(path,*args,**kwargs):
        if path==monitor.path.with_suffix('.tmp'): raise OSError('synthetic disk full')
        return real_write(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',failed_write)
    monkeypatch.setattr('chejin_worker_client.failure_evidence.record_capture_failure',lambda *a:failures.append(a))
    monitor.observe(receipt(16)); assert monitor.flush()
    assert monitor.path.read_bytes()==before
    assert monitor.snapshot()['evidence_write_status']=='failed' and failures


def test_update_close_preserves_existing_writer_gate_and_drains_diagnostics(tmp_path):
    import time
    from chejin_worker_client.hook_worker_services import HookWorkerServices
    monitor=ClockDiagnostics(tmp_path,zone_reader=lambda:ZONE)
    calls=[]
    service=SimpleNamespace(connection=SimpleNamespace(
        backend=SimpleNamespace(clock_diagnostics=monitor),close=lambda **kw:calls.append(kw)))
    HookWorkerServices.close(service,deadline=time.monotonic()+2)
    assert calls and monitor.flush(0)
    assert service._credentials is None
    # Binding loss can close a service that will be reused by the next binding.
    monitor.observe(receipt(16)); assert monitor.flush()
    assert monitor.snapshot()['comparison_status']=='skewed'
    assert json.loads(monitor.path.read_text())['latest']['comparison_status']=='skewed'
    # A still-running diagnostic writer prevents taking update ownership.
    service.connection.backend.clock_diagnostics=SimpleNamespace(flush=lambda **kw:False)
    with pytest.raises(RuntimeError,match='UPDATE_WRITERS_NOT_STOPPED'):
        HookWorkerServices.close(service,deadline=time.monotonic()+.01)
