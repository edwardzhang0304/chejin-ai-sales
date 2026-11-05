"""Local protocol/producer/export checks. Not Windows ACL or real DLL UAT."""
import json
import os
from pathlib import Path
import threading
import time
import tracemalloc
from types import SimpleNamespace
import zipfile

import pytest
from chejin_worker_client import memory_capture as mc, memory_capture_channel as cp
from chejin_worker_client import python_memory_profile as pm


def wait(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():return
        time.sleep(.01)
    raise AssertionError('condition timed out')


@pytest.fixture
def live(tmp_path, monkeypatch):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/session.json').write_text(json.dumps({'session_id': 'runtime-synthetic',
        'pid': os.getpid(), 'status': 'running'}))
    monkeypatch.setattr(cp, 'CAPTURE_SECONDS', 1)
    monkeypatch.setattr(cp, 'POLL_SECONDS', .02)
    worker = mc.install(tmp_path, SimpleNamespace(capture_pressure=lambda: None))
    assert worker
    yield worker
    worker.close()
    if worker.profile and worker.profile.thread:worker.profile.thread.join(3)
    assert not tracemalloc.is_tracing()


def test_request_actual_profile_and_readonly_new_exporter(live):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'finished', result
    assert result['target']['pid'] == os.getpid()
    report = json.loads((live.channel.root / 'artifacts/runtime/python-memory' / result['report_file']).read_text())
    assert report['request_id'] == result['request_id']
    assert report['runtime_session'] == result['target']['runtime_session']
    path = export_bundle(live.channel.root, live.channel.root / 'live.zip', capture_request=result)
    with zipfile.ZipFile(path) as z:
        request = json.loads(z.read('memory/request.json'))
        data = json.loads(z.read('logs/runtime/python-memory/' + result['report_file']))
        assert request['capture_id'] == data['capture_id'] == report['capture_id']
    assert not tracemalloc.is_tracing()


def test_concurrent_requests_join_and_cooldown(live):
    results = []
    threads = [threading.Thread(target=lambda: results.append(mc.request_capture(live.channel.root))) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(5)
    assert len(results) == 2 and all(r['status'] == 'finished' for r in results), results
    assert len({r['capture_id'] for r in results}) == 1
    assert len({r['request_id'] for r in results}) == 2
    again = mc.request_capture(live.channel.root)
    assert again['status'] == 'cooldown' and again['new_capture'] is False
    assert again['capture_id'] == results[0]['capture_id']
    assert again['capture_age_seconds'] >= 1


def test_pressure_fails_only_diagnostic(live):
    live.performance.capture_pressure = lambda: {'reason': 'cpu_pressure', 'value': 95, 'threshold': 90}
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'failed'
    assert result['reason'] == 'profile_business_pressure'
    gap = json.loads((live.channel.root / 'artifacts/runtime/python-memory-gap.json').read_text())
    assert gap['pressure']['value'] == 95
    assert not tracemalloc.is_tracing()
    assert json.loads((live.channel.root / 'runtime/session.json').read_text())['status'] == 'running'


def test_no_listener_still_exports_and_records_missing(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    result = mc.request_capture(tmp_path)
    assert result['status'] == 'not_running'
    archive = export_bundle(tmp_path, tmp_path / 'missing.zip', capture_request=result)
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('export.json'))
        assert any(r['source'] == 'memory/request.json' and r['status'] == 'missing' for r in manifest['sources'])
        assert manifest['partial']


def test_no_consumer_cannot_be_reported_success(live, monkeypatch):
    live.close()
    monkeypatch.setattr(cp, 'ACK_SECONDS', .1)
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'timeout' and result['reason'] == 'acceptance_timeout'
    assert not tracemalloc.is_tracing()


def test_original_process_generation_required(live):
    endpoint = live.channel.read(live.channel.folder / 'endpoint.json')
    endpoint['target']['created'] -= 1
    live.channel.write(live.channel.folder / 'endpoint.json', endpoint)
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'failed'
    assert live.profile is None


def test_symlink_and_oversized_records_rejected(live, tmp_path):
    external = tmp_path / 'outside.json'
    external.write_text('{}')
    link = live.channel.requests / ('a' * 32 + '.json')
    link.symlink_to(external)
    with pytest.raises(ValueError, match='link_boundary'):live.channel.read(link)
    link.unlink()
    link.write_bytes(b' ' * (cp.MAX_BYTES + 1))
    with pytest.raises(ValueError, match='record_budget'):live.channel.read(link)


def test_request_identity_expiry_and_command_allowlist(live):
    now = time.monotonic()
    value = {'schema': cp.SCHEMA, 'operation': 'memory_capture', 'request_id': 'a'*32,
             'target': live.target, 'requester': cp.identity(), 'requested_mono': now,
             'expires_mono': now+cp.ACK_SECONDS, 'status': 'queued'}
    assert cp.request_valid(value, live.target)
    assert not cp.request_valid({**value, 'command': 'anything'}, live.target)
    assert not cp.request_valid({**value, 'expires_mono': now-1}, live.target)
    assert not cp.request_valid({**value, 'target': {**live.target, 'runtime_session': 'old'}}, live.target)


def test_other_tracer_owned_elsewhere_survives_request(live):
    tracemalloc.start(1)
    try:
        result = mc.request_capture(live.channel.root)
        assert result['status'] == 'failed' and result['reason'] == 'another_tracer_active'
        assert tracemalloc.is_tracing()
    finally:tracemalloc.stop()


def test_worker_session_replaced_during_capture_is_not_success(live, monkeypatch):
    # This case exercises a complete new identity, not a concurrent-file-read
    # race (which correctly fails earlier with channel_record_changed).
    marker_lock = threading.Lock()
    endpoint = cp.Channel.endpoint
    def serialized_endpoint(channel):
        with marker_lock:
            return endpoint(channel)
    monkeypatch.setattr(cp.Channel, 'endpoint', serialized_endpoint)
    results = []
    t = threading.Thread(target=lambda: results.append(mc.request_capture(live.channel.root)))
    t.start()
    wait(lambda: tracemalloc.is_tracing())
    marker = live.channel.read(live.channel.root/'runtime/session.json')
    marker['session_id'] = 'different-runtime'
    with marker_lock:
        live.channel.write(live.channel.root/'runtime/session.json', marker)
    t.join(5)
    assert results[0]['status'] == 'failed'
    assert results[0]['reason'] == 'worker_identity_unavailable'
    live.profile.thread.join(3)
    assert not tracemalloc.is_tracing()


def test_one_bad_json_does_not_stop_another_request(live):
    bad = live.channel.requests / ('b'*32 + '.json')
    bad.write_text('{broken')
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'finished', result
    import base64
    index = live.channel.read(live.channel.root/'artifacts/runtime/memory-request-retirement.json')
    item = next(r for r in index['records'] if r['source_name'] == bad.name)
    assert item['reason'] == 'JSONDecodeError'
    retained = live.channel.read(live.channel.folder/'retired'/(item['key']+'.json'))
    assert base64.b64decode(retained['raw_base64']) == b'{broken'
    assert not bad.exists()


@pytest.mark.parametrize('phase', ['request', 'receipt'])
def test_channel_write_failure_stays_local_and_exports_gap(live, monkeypatch, phase):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    original = cp.Channel.write
    def fail(self, path, value):
        if path.parent == self.requests and ((phase == 'request' and value.get('status') == 'queued')
                or (phase == 'receipt' and value.get('status') != 'queued')):
            raise PermissionError('synthetic-private-value')
        return original(self, path, value)
    monkeypatch.setattr(cp.Channel, 'write', fail)
    monkeypatch.setattr(cp, 'ACK_SECONDS', .15)
    result = mc.request_capture(live.channel.root)
    assert result['status'] in ('failed', 'timeout')
    path = export_bundle(live.channel.root, live.channel.root/f'{phase}.zip', capture_request=result)
    with zipfile.ZipFile(path) as z:
        assert any(e['status'] in ('read_failed','incomplete') for e in json.loads(z.read('export.json'))['sources'])
        assert b'synthetic-private-value' not in z.read('memory/request.json')
    assert live.channel.read(live.channel.root/'runtime/session.json')['status'] == 'running'


def test_request_retention_and_budget_are_bounded(live, monkeypatch):
    live.close()  # Admission limit is checked without consumer draining the requests.
    now = time.monotonic()
    for n in range(cp.MAX_REQUESTS):
        rid = f'{n:032x}'
        live.channel.write(live.channel.requests/(rid+'.json'), {'schema':cp.SCHEMA, 'request_id':rid,
            'target':live.target, 'requested_wall':time.time(), 'requested_mono':now,
            'expires_mono':now+5, 'status':'queued'})
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'busy' and live.profile is None
    with live.channel.lock():
        for path in live.channel.paths():
            record = live.channel.read(path)
            record.update(requested_wall=time.time() - cp.KEEP_SECONDS - 20,
                          requested_mono=now - cp.KEEP_SECONDS - 20,
                          expires_mono=now - cp.KEEP_SECONDS - 10)
            live.channel.write(path, record)
    live.stop.clear()
    live.thread = threading.Thread(target=live._run, daemon=True)
    live.thread.start()
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'finished'
    assert len(live.channel.paths()) <= cp.MAX_REQUESTS


def test_large_report_has_distinct_limit_from_protocol(live):
    path = live.channel.root / 'report.json'
    path.write_text(json.dumps({'value':'x'*cp.MAX_BYTES}))
    with pytest.raises(ValueError, match='record_budget'):live.channel.read(path)
    assert live.channel.read(path, limit=pm.MAX_REPORT_BYTES)['value']


def test_mid_read_replace_is_not_accepted(live, monkeypatch):
    path = live.channel.requests/('a'*32+'.json')
    live.channel.write(path, {'schema':1})
    real = os.fstat
    calls = []
    def change(fd):
        info = real(fd)
        calls.append(1)
        if len(calls) == 2:
            other = path.with_suffix('.new'); other.write_text('{"schema":2}'); os.replace(other,path)
        return info
    monkeypatch.setattr(os, 'fstat', change)
    with pytest.raises(ValueError, match='record_changed'):live.channel.read(path)


def test_current_manifest_never_claims_dll_probe_ran(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    path = export_bundle(tmp_path, tmp_path/'matrix.zip')
    with zipfile.ZipFile(path) as z:
        matrix = json.loads(z.read('memory/collection.json'))
        assert matrix['worker_python']['status'] == 'not_requested'
        assert matrix['dll_calls']['status'] == matrix['wechat_resources']['status'] == 'missing'
        assert matrix['DiySceneStats']['status'] == 'deferred_not_implemented_for_running_capture'
        assert matrix['DiySceneStats']['reason'] == 'deferred_by_user_current_release'
        assert not matrix['SwapMemory']['executed_this_request']


def test_sampler_load_pressure_reads_existing_signals_only(tmp_path):
    from chejin_worker_client.performance_evidence import Monitor
    m = Monitor(tmp_path)
    now = time.monotonic()
    assert m.capture_pressure()['reason'] == 'load_unavailable'
    m.capture_load = {'at':now, 'cpu_percent':float('nan')}
    assert m.capture_pressure()['reason'] == 'load_unavailable'
    m.capture_load = {'at':now,'cpu_percent':0}
    assert m.capture_pressure()['reason'] == 'ui_signal_unavailable'
    m.state['ui_monotonic'] = now
    assert m.capture_pressure() is None
    m.capture_load['cpu_percent'] = m.policy.cpu_percent + 1
    assert m.capture_pressure()['reason'] == 'cpu_pressure'
    m.capture_load['cpu_percent'] = 0
    m.state['ui_monotonic'] = now - m.policy.ui_delay_seconds - 1
    assert m.capture_pressure()['reason'] == 'ui_response_gap'


def test_worker_exit_keeps_partial_request_and_original_report(live):
    results = []
    t = threading.Thread(target=lambda:results.append(mc.request_capture(live.channel.root)))
    t.start(); wait(lambda:tracemalloc.is_tracing())
    live.profile.close(); live.profile.thread.join(3)
    t.join(5)
    assert results[0]['status'] == 'failed' and results[0]['reason'] == 'worker_exit'
    assert live.profile.path.exists()
