import json
import zipfile
import base64
import hashlib
import time
from pathlib import Path
import pytest
from test_memory_capture_live import live
from chejin_worker_client import memory_capture as mc, memory_capture_channel as cp
from chejin_worker_client.diagnostic_bundle import export_bundle
from chejin_worker_client.memory_capture_outcome import resolve
from chejin_worker_client.memory_request_retention import MAX_RETIRED, MAX_RETAINED_BYTES


def test_export_job_unwritable_destination_keeps_business_proof(tmp_path):
    """Use the desktop job and an actually invalid destination, not a ZIP stub."""
    from PySide6.QtCore import QCoreApplication
    from chejin_worker_client.diagnostic_ui import ExportJob
    app = QCoreApplication.instance() or QCoreApplication([])
    proof = tmp_path / 'transactions/original.json'
    proof.parent.mkdir()
    original = b'{"synthetic_original_receipt":true}'
    proof.write_bytes(original)
    blocked = tmp_path / 'artifacts/exports'
    blocked.parent.mkdir()
    blocked.write_bytes(b'synthetic existing file, not an export directory')
    job = ExportJob(tmp_path)
    completed, failures = [], []
    job.completed.connect(completed.append)
    job.failed.connect(failures.append)
    assert job.start()
    deadline = time.monotonic() + 5
    while not failures and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert failures == ['FileExistsError'] and not completed
    assert not job.running and proof.read_bytes() == original
    assert not list(tmp_path.rglob('*.zip'))


def test_report_write_failure_reaches_current_request_with_reason(live, monkeypatch):
    original=Path.write_bytes
    attempts=[]
    def fail_later(path, data):
        if path.parent == live.channel.root / 'artifacts/runtime/python-memory':
            attempts.append(1)
            if len(attempts)>=2:raise PermissionError('synthetic-not-for-export')
        return original(path,data)
    monkeypatch.setattr(Path,'write_bytes',fail_later)
    result=mc.request_capture(live.channel.root)
    assert result['status']=='failed'
    archive=export_bundle(live.channel.root,live.channel.root/'write-failed.zip',capture_request=result)
    with zipfile.ZipFile(archive) as z:
        request=json.loads(z.read('memory/request.json'))
        gap=json.loads(z.read('logs/runtime/python-memory-gap.json'))
        status=json.loads(z.read('logs/runtime/python-memory-status.json'))
        report=json.loads(z.read('logs/runtime/python-memory/'+request['report_file']))
    print({'request':request,'gap':gap,'status':status,'report_status':report['status']})
    assert gap['capture_id']==request['capture_id']==status['capture_id']
    assert gap['reason']=='PermissionError'
    assert request['reason']==gap['reason'], 'stale unfinished report masked matching current failure'


def test_corrupt_old_requests_do_not_permanently_exhaust_live_capacity(live):
    # Real interrupted/corrupt protocol input, not manufactured capture output.
    evidence={}
    for n in range(cp.MAX_REQUESTS):
        p=live.channel.requests/(f'{n:032x}'+'.json')
        p.write_bytes(b'{broken');evidence[p.name]=p.read_bytes()
    result=mc.request_capture(live.channel.root)
    print(result)
    assert result['status']=='finished', 'unreadable request files occupy all admission slots without expiry/recovery'


@pytest.mark.parametrize('foreign', ['capture_id', 'runtime_session', 'created'])
def test_foreign_failure_cannot_override_original_capture(live, foreign):
    live.close()
    root = live.channel.root / 'artifacts/runtime'
    (root / 'python-memory').mkdir(exist_ok=True)
    value = {**live.target, 'capture_id': 'a'*32, 'started_monotonic': time.monotonic(),
             'status': 'finished', 'end_reason': 'duration_complete'}
    live.channel.write(root / 'python-memory' / ('a'*32 + '.json'), value)
    live.channel.write(root / 'python-memory-gap.json', {
        **value, 'status': 'capture_failed', 'reason': 'PermissionError', foreign: 'foreign'})
    result = resolve(live.channel, 'a'*32, live.target)
    assert result['status'] == 'finished'
    assert len(result['outcome_sources']) == 1


def test_interrupted_report_is_not_success_and_first_cause_survives_secondary_write_failure(live):
    live.close()
    root = live.channel.root / 'artifacts/runtime'
    (root / 'python-memory').mkdir(exist_ok=True)
    value = {**live.target, 'capture_id': 'b'*32, 'started_monotonic': time.monotonic(),
             'status': 'starting'}
    path = root / 'python-memory' / ('b'*32 + '.json')
    live.channel.write(path, value)
    original = path.read_bytes()
    interrupted = resolve(live.channel, 'b'*32, live.target)
    assert interrupted['status'] == 'failed'
    assert interrupted['reason'] == 'capture_interrupted_before_terminal_record'
    live.channel.write(root / 'python-memory-gap.json', {
        **value, 'status': 'capture_failed', 'reason': 'PermissionError',
        'first_failure': 'profile_capture_time_budget'})
    result = resolve(live.channel, 'b'*32, live.target)
    assert result['reason'] == 'profile_capture_time_budget'
    assert path.read_bytes() == original


def test_report_and_status_write_failure_still_resolves_gap(live, monkeypatch):
    from chejin_worker_client import python_memory_profile as pm
    write = Path.write_bytes
    def fail_report(path, data):
        if path.parent.name == 'python-memory':
            raise PermissionError('synthetic private context')
        return write(path, data)
    original_status = pm._write_current
    def fail_terminal(root, state):
        if state['status'] == 'capture_failed':
            raise OSError('synthetic status write error')
        return original_status(root, state)
    monkeypatch.setattr(Path, 'write_bytes', fail_report)
    monkeypatch.setattr(pm, '_write_current', fail_terminal)
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'failed' and result['reason'] == 'PermissionError'
    assert result['outcome_source'] == 'gap'


def test_bad_expired_wrong_generation_and_atomic_temps_retire_with_bounded_evidence(live):
    live.close()  # Simulate files left behind while a listener is absent.
    root, channel = live.channel.root, live.channel
    business = root / 'transactions/original.json'
    business.parent.mkdir()
    business.write_bytes(b'original recovery proof')
    for n in range(MAX_RETIRED + 3):
        path = channel.requests / (f'{n:032x}.json')
        raw = b'{' + b'x' * (MAX_RETAINED_BYTES + 50) if n == MAX_RETIRED + 2 else b'{broken'
        path.write_bytes(raw)
        with channel.lock():
            channel.prune(live.target)
    expired = {'schema': cp.SCHEMA, 'request_id': 'e'*32, 'target': live.target,
        'status': 'finished', 'requested_mono': time.monotonic()-500,
        'expires_mono': time.monotonic()-450, 'requested_wall': time.time()-500}
    channel.write(channel.requests / ('e'*32 + '.json'), expired)
    channel.write(channel.requests / ('d'*32 + '.json'), {
        **expired, 'request_id': 'd'*32, 'target': {**live.target, 'runtime_session': 'old'}})
    (channel.requests / ('c'*32 + '.json.' + 'a'*32 + '.tmp')).write_bytes(b'{partial')
    with channel.lock():
        channel.prune(live.target)
    retired = list((channel.folder / 'retired').glob('*.json'))
    assert len(retired) == MAX_RETIRED
    assert not channel.paths(include_temporary=True)
    index_path = root / 'artifacts/runtime/memory-request-retirement.json'
    index = json.loads(index_path.read_text())
    assert index['evicted_count'] == 6 and index['retired_count'] == MAX_RETIRED+6
    assert {r['reason'] for r in index['records']} >= {
        'JSONDecodeError', 'request_retention_expired', 'request_old_or_wrong_worker', 'orphan_atomic_write'}
    for path in retired:
        record = json.loads(path.read_text())
        raw = base64.b64decode(record['raw_base64'])
        assert len(raw) <= MAX_RETAINED_BYTES
        assert hashlib.sha256(raw).hexdigest() == record['sha256']
    assert any(r['hash_scope'] == 'retained_prefix_only' for r in index['records'])
    # Restart listener; no manual removal and same original business bytes.
    worker = mc.install(root, live.performance)
    try:
        result = mc.request_capture(root)
        assert result['status'] == 'finished', result
        archive = export_bundle(root, root / 'retired.zip', capture_request=result)
        with zipfile.ZipFile(archive) as z:
            exported = json.loads(z.read('logs/runtime/memory-request-retirement.json'))
            assert exported['retired_count'] == index['retired_count']
            assert not any('/retired/' in name for name in z.namelist())
            assert b'raw_base64' not in z.read('logs/runtime/memory-request-retirement.json')
    finally:
        worker.close()
    assert business.read_bytes() == b'original recovery proof'


def test_failed_retirement_keeps_original_and_new_export_recovers_after_storage_recovers(live, monkeypatch):
    original = cp.Channel.write
    bad = live.channel.requests / ('f'*32 + '.json')
    bad.write_bytes(b'{original-broken')
    def fail_retirement(channel, path, value):
        if path.parent.name == 'retired':
            raise PermissionError('synthetic disk denial')
        return original(channel, path, value)
    with monkeypatch.context() as patch:
        patch.setattr(cp.Channel, 'write', fail_retirement)
        result = mc.request_capture(live.channel.root)
        assert result['status'] == 'failed' and result['reason'] == 'PermissionError'
        assert bad.read_bytes() == b'{original-broken'
        archive = export_bundle(live.channel.root, live.channel.root/'failed-retirement.zip', capture_request=result)
        with zipfile.ZipFile(archive) as z:
            assert json.loads(z.read('export.json'))['partial']
    # No manual file edits/deletion: retry through the same user action.
    result = mc.request_capture(live.channel.root)
    assert result['status'] == 'finished', result
    assert not bad.exists()


@pytest.mark.parametrize('failure', ['new_raw', 'intent_manifest', 'final_manifest', 'old_unlink', 'source_unlink'])
def test_full_retirement_failures_are_declared_and_recover_on_restart(live, monkeypatch, failure):
    live.close()
    channel = live.channel
    index_path = channel.root / 'artifacts/runtime/memory-request-retirement.json'
    folder = channel.folder / 'retired'
    for n in range(MAX_RETIRED):
        path = channel.requests / f'{n:032x}.json'
        path.write_bytes(b'{old input')
        with channel.lock(): channel.prune(live.target)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.json')}
    original_input = channel.requests / ('e'*32 + '.json')
    original_input.write_bytes(b'{new input')
    original_write, original_unlink = cp.Channel.write, Path.unlink
    def write(ch, path, value):
        if ((failure == 'new_raw' and path.parent == folder)
            or (failure == 'intent_manifest' and path == index_path and value.get('pending'))
            or (failure == 'final_manifest' and path == index_path and not value.get('pending'))):
            raise OSError('synthetic storage failure')
        return original_write(ch, path, value)
    def unlink(path, *args, **kwargs):
        if ((failure == 'old_unlink' and path.parent == folder)
            or (failure == 'source_unlink' and path == original_input)):
            raise OSError('synthetic deletion failure')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(cp.Channel, 'write', write)
        patch.setattr(Path, 'unlink', unlink)
        result = mc.request_capture(channel.root)
        assert result['status'] == 'failed' and result['reason'] == 'OSError'
        assert original_input.read_bytes() == b'{new input'
        archive = export_bundle(channel.root, channel.root / 'interrupted-retirement.zip', capture_request=result)
        with zipfile.ZipFile(archive) as z:
            exported = json.loads(z.read('logs/runtime/memory-request-retirement.json'))
        for record in exported['records']:
            assert (folder / (record['key'] + '.json')).is_file(), 'false retention claim'
        assert len(list(folder.glob('*.json'))) <= MAX_RETIRED
        if failure != 'intent_manifest':
            assert exported['evicted_count'] == 1
            assert exported['last_eviction']['reason'] == 'diagnostic_retention_capacity'
        else:
            assert exported['evicted_count'] == 0
    # New listener plus the original desktop action resumes the bounded journal.
    worker = mc.install(channel.root, live.performance)
    try:
        result = mc.request_capture(channel.root)
        assert result['status'] == 'finished', result
        archive = export_bundle(channel.root, channel.root / 'recovered-retirement.zip', capture_request=result)
        with zipfile.ZipFile(archive) as z:
            recovered = json.loads(z.read('logs/runtime/memory-request-retirement.json'))
        assert not recovered.get('pending')
        assert recovered['retired_count'] == MAX_RETIRED + 1
        assert recovered['evicted_count'] == 1
        assert len(recovered['records']) == MAX_RETIRED
        assert not original_input.exists()
        for name, digest in before.items():
            path = folder / name
            if path.exists(): assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    finally:
        worker.close()
