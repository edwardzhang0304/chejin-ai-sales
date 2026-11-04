"""Same-snapshot equivalence and hard boundaries; no real messages or data."""
import hashlib
import json
import sys
import time
import tracemalloc
from types import SimpleNamespace

import pytest
from chejin_worker_client import python_memory_profile as m


def snapshot(rows):
    return tracemalloc.Snapshot(rows, 5)


def aggregate(snap):
    attempt = {'processed_traces': 0}
    return m._aggregate(snap, attempt, time.monotonic()), attempt


def test_raw_and_public_complete_normalization_equivalent(monkeypatch):
    frames = (('/private/synthetic/new.py', 3), ('C:\\synthetic\\old.py', 2))
    snap = snapshot([(0, 12, frames, 8), (1, 34, frames, 5),
                     (0, 7, tuple(reversed(frames)), 2), (2, 0, (('zero.py', 0),))])
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    raw, a = aggregate(snap)
    monkeypatch.setattr(m, '_RAW_LAYOUTS', set())
    public, b = aggregate(snap)
    assert raw == public and a['processed_traces'] == b['processed_traces'] == 4
    assert a['enumeration'] == 'cpython_raw' and b['enumeration'] == 'public'
    key = hashlib.sha256(repr(tuple(reversed(frames))).encode()).hexdigest()
    assert raw[key] == {'bytes': 46, 'blocks': 2, 'frames': [
        {'module': 'old.py', 'line': 2}, {'module': 'new.py', 'line': 3}]}
    assert '/private/' not in json.dumps(raw) and 'C:' not in json.dumps(raw)


def test_same_actual_snapshot_complete_normalization(monkeypatch):
    tracemalloc.start(5)
    try:
        held = [bytearray(1234) for _ in range(30)]
        snap = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    raw, _ = aggregate(snap)
    monkeypatch.setattr(m, '_RAW_LAYOUTS', set())
    public, _ = aggregate(snap)
    assert raw == public and held


def test_actual_inbox_projection_snapshot_equivalence(tmp_path, monkeypatch):
    """Actual business allocations, not fabricated snapshot rows or messages."""
    from chejin_worker_client import storage
    from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
    from chejin_worker_client.hook_projection import NativeProjectionStore
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    scope = HookReceiveScope('wxid_synthetic_sales', 'epoch', 'process', 30001)
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=131072)
    projector = NativeProjectionStore(max_projection_bytes=131072, max_entries=10)
    with storage.db_connection():
        pass  # The running Worker has initialized storage before diagnostics.
    projected = []
    tracemalloc.start(5)
    try:
        for index in range(20):
            body = json.dumps({'ServerPort':'30001', 'selfwxid':scope.self_wxid,
                'sendorrecv':'2', 'msgnumber':'1', 'msglist':[{'msgtype':'1',
                'msgsvrid':str(8100000000000000000 + index), 'fromtype':'1',
                'fromid':'wxid_synthetic_customer', 'toid':scope.self_wxid,
                'msg':'合成的选车问题'}]}).encode()
            inbox.persist(body, scope)
            projected.append(projector.project_next(scope))
        snap = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    raw, raw_attempt = aggregate(snap)
    monkeypatch.setattr(m, '_RAW_LAYOUTS', set())
    public, public_attempt = aggregate(snap)
    assert raw == public and raw_attempt['processed_traces'] == public_attempt['processed_traces']
    assert len({p['observations'][0]['native_msg_id'] for p in projected}) == 20
    modules = {f['module'] for site in raw.values() for f in site['frames']}
    assert {'hook_inbox.py', 'hook_projection.py'} <= modules
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0] == 20
        assert db.execute('SELECT COUNT(*) FROM hook_observation_projection').fetchone()[0] == 20


@pytest.mark.parametrize('count,error', [(50000,None),(50001,'profile_trace_budget')])
def test_trace_boundary(monkeypatch, count, error):
    # Fake clock isolates deterministic volume limits from machine speed.
    monkeypatch.setattr(m.time, 'monotonic', lambda: 0.)
    snap = snapshot([(0, 3, (('same.py', 1),), 1)] * count)
    if error:
        with pytest.raises(ValueError, match=error): aggregate(snap)
    else:
        sites, a = aggregate(snap)
        assert a['processed_traces'] == count
        assert next(iter(sites.values()))['blocks'] == count


@pytest.mark.parametrize('count,error', [(5000,None),(5001,'profile_site_budget')])
def test_site_boundary(monkeypatch, count, error):
    monkeypatch.setattr(m.time, 'monotonic', lambda: 0.)
    snap = snapshot([(0, 3, (('unique.py', n),), 1) for n in range(count)])
    if error:
        with pytest.raises(ValueError, match=error): aggregate(snap)
    else:
        sites, a = aggregate(snap)
        assert len(sites) == a['grouped_sites'] == count


def test_empty_and_missing_private_api(monkeypatch):
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    assert aggregate(snapshot([]))[0] == {}
    public_traces = list(snapshot([(0, 5, (('one.py', 1),), 1)]).traces)
    sites, a = aggregate(SimpleNamespace(traces=public_traces))
    assert a['enumeration'] == 'public' and next(iter(sites.values()))['blocks'] == 1


@pytest.mark.parametrize('row', [(), (True,1,(('a',1),)), (0,-1,(('a',1),)),
    (0,1,[]), (0,1,()), (0,1,(('a',1),)*6), (0,1,(([],1),)),
    (0,1,(('a',True),)), (0,1,(('a',-1),)), (0,1,(('a',1),),0),
    (0,1,(('a',1),),True)])
def test_malformed_raw_never_falls_back_or_hashes_invalid_values(monkeypatch, row):
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    class Traces:
        _traces = [row]
        def __len__(self): return 1
        def __iter__(self): raise AssertionError('must not retry malformed data as public')
    with pytest.raises(ValueError, match='profile_trace_format'):
        aggregate(SimpleNamespace(traces=Traces()))


@pytest.mark.parametrize('raw', [{}, [], 'bad'])
def test_outer_container_and_count_validation(monkeypatch, raw):
    monkeypatch.setattr(m, '_RAW_LAYOUTS', {sys.version_info[:3]})
    class Traces:
        _traces = raw
        def __len__(self): return 1
    with pytest.raises(ValueError, match='profile_trace_format'):
        aggregate(SimpleNamespace(traces=Traces()))


def test_public_fallback_uses_original_deadline_no_resnapshot(monkeypatch):
    monkeypatch.setattr(m, '_RAW_LAYOUTS', set())
    monkeypatch.setattr(m.time, 'monotonic', lambda: 1.)
    a = {'processed_traces': 0}
    with pytest.raises(ValueError, match='profile_capture_time_budget'):
        m._aggregate(snapshot([(0,1,(('a',1),),1)]), a, 0.)
    assert a['enumeration'] == 'public' and a['processed_traces'] == 0


@pytest.mark.parametrize('elapsed', [.499, .501])
@pytest.mark.parametrize('phase', ['snapshot','write'])
def test_full_sample_deadline_covers_snapshot_and_write(tmp_path, monkeypatch, phase, elapsed):
    clock = [0.]
    monkeypatch.setattr(m.time, 'monotonic', lambda:clock[0])
    monkeypatch.setattr(m.tracemalloc, 'is_tracing', lambda:True)
    monkeypatch.setattr(m.tracemalloc, 'get_tracemalloc_memory', lambda:100)
    monkeypatch.setattr(m.tracemalloc, 'get_traced_memory', lambda:(10,20))
    calls=[]
    def take():
        calls.append('snapshot')
        if phase=='snapshot': clock[0] = elapsed
        return snapshot([])
    monkeypatch.setattr(m.tracemalloc,'take_snapshot',take)
    profile = m.Profile(tmp_path,30); profile.owned=True
    def write():
        calls.append('write'); clock[0]=elapsed
    monkeypatch.setattr(profile,'_write',write)
    assert m.MAX_CAPTURE_SECONDS == .5
    if elapsed > .5:
        with pytest.raises(ValueError,match='profile_capture_time_budget'):profile.sample()
    else:
        profile.sample()
    assert calls.count('snapshot') == 1
    assert profile.data['last_attempt']['elapsed_ms'] == elapsed * 1000
    assert ('write' in calls) == (phase=='write' or elapsed < .5)
    if phase=='write':assert profile.data['last_attempt']['report_write_ms'] == elapsed * 1000
