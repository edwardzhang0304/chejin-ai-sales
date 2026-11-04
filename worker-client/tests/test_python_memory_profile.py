import gc
import json
import time
import tracemalloc
import zipfile

import pytest
from chejin_worker_client import python_memory_profile as module


def allocate():
    return bytearray(b'synthetic-private-content-' * 4000)


def test_retained_growth_and_release_exported_without_object_contents(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    profile = module.Profile(tmp_path, 30)
    held = []
    try:
        assert profile.begin()
        for _ in range(3):held.append(allocate())
        profile.sample()
        grown = profile.data['samples'][-1]
        relevant = [s for s in grown['sites'] if any(f['module'] == __file__.split('/')[-1] for f in s['frames'])]
        assert sum(s['baseline_delta_bytes'] for s in relevant) > 200_000
        held.clear(); gc.collect(); profile.sample()
        released = profile.data['samples'][-1]
        assert any(s['previous_delta_bytes'] < -200_000 for s in released['sites'])
    finally:
        held.clear(); profile.finish()
    assert not tracemalloc.is_tracing() and profile.previous is None
    assert all(s['capture_ms'] >= 0 for s in profile.data['samples'])
    assert profile.path.stat().st_size <= module.MAX_REPORT_BYTES
    # A new exporter reads persisted evidence after the tracer has exited.
    archive = export_bundle(tmp_path, tmp_path / 'evidence.zip')
    with zipfile.ZipFile(archive) as zipped:
        member = next(n for n in zipped.namelist() if n.startswith('logs/runtime/python-memory/') and n.endswith('.json'))
        raw = zipped.read(member)
        assert b'synthetic-private-content' not in raw
        assert json.loads(raw)['status'] == 'finished'
        capabilities = json.loads(zipped.read('memory/coverage.json'))
        assert any(a['mode'] == 'not_instrumented' for a in capabilities['areas'])
    assert not tracemalloc.is_tracing()


def test_existing_tracer_is_not_stopped(tmp_path):
    tracemalloc.start(1)
    try:
        profile = module.Profile(tmp_path, 1)
        assert profile.begin() is False
        profile.finish()
        assert tracemalloc.is_tracing()
        assert json.loads(profile.path.read_text())['reason'] == 'another_tracer_active'
    finally:tracemalloc.stop()


def test_folder_budget_refuses_capture_and_records_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_FOLDER_BYTES', 10)
    profile = module.Profile(tmp_path, 1)
    profile._run()
    assert not profile.path.exists() and not tracemalloc.is_tracing()
    assert json.loads((profile.root.parent / 'python-memory-gap.json').read_text())['reason'] == 'profile_folder_budget'


def test_metadata_budget_stops_owned_tracer(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_METADATA_BYTES', 0)
    profile = module.Profile(tmp_path, 1)
    profile._run()
    report = json.loads(profile.path.read_text())
    assert report['status'] == 'capture_failed' and report['end_reason'] == 'profile_metadata_budget'
    assert not tracemalloc.is_tracing()


def test_opt_in_duration_and_worker_exit(tmp_path):
    assert module.start_profile(tmp_path, 0) is None
    assert not (tmp_path / 'artifacts').exists()
    profile = module.start_profile(tmp_path, 1)
    profile.thread.join(timeout=5)
    assert not profile.thread.is_alive() and not tracemalloc.is_tracing()
    report = json.loads(profile.path.read_text())
    assert report['end_reason'] == 'duration_complete'
    assert 1 <= len(report['samples']) <= 2
    profile = module.start_profile(tmp_path, 30)
    time.sleep(.05)
    profile.close(); profile.thread.join(timeout=5)
    assert json.loads(profile.path.read_text())['end_reason'] == 'worker_exit'
    assert not tracemalloc.is_tracing()


def test_start_failure_does_not_break_worker_and_is_visible(tmp_path, monkeypatch):
    def failed(_):raise RuntimeError('sensitive message')
    monkeypatch.setattr(module.threading.Thread, 'start', failed)
    assert module.start_profile(tmp_path, 30) is None
    raw = (tmp_path / 'artifacts/runtime/python-memory-gap.json').read_text()
    assert 'RuntimeError' in raw and 'sensitive message' not in raw


def test_capture_time_budget_records_failure_and_frees_tracer(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_CAPTURE_SECONDS', -1)
    profile = module.Profile(tmp_path, 1)
    profile._run()
    assert not tracemalloc.is_tracing()
    assert json.loads(profile.path.read_text())['capture_failed'] == 'profile_capture_time_budget'


def test_report_volume_omissions_are_explicit(tmp_path, monkeypatch):
    profile = module.Profile(tmp_path, 1)
    profile.root.mkdir(parents=True)
    monkeypatch.setattr(module, 'MAX_REPORT_BYTES', 3000)
    profile.data['samples'] = [{'synthetic': 'x' * 1200} for _ in range(5)]
    profile._write()
    report = json.loads(profile.path.read_text())
    assert report['omitted_samples'] > 0 and profile.path.stat().st_size <= 3000


def test_unfinished_capture_survives_new_exporter(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    profile = module.Profile(tmp_path, 1)
    profile.root.mkdir(parents=True)
    profile._write()
    path = export_bundle(tmp_path, tmp_path / 'unfinished.zip')
    with zipfile.ZipFile(path) as zipped:
        raw = zipped.read('logs/runtime/python-memory/' + profile.path.name)
        assert json.loads(raw)['status'] == 'starting'
    assert not tracemalloc.is_tracing()


def test_profile_failures_and_missing_capture_are_explicit_in_manifest(tmp_path):
    from chejin_worker_client.diagnostic_bundle import Collector
    collector = Collector(tmp_path, tmp_path / 'unused.zip')
    collector.evidence_issues({'evidence_kind': 'python_allocations', 'status': 'capture_failed',
                              'samples': [{'omitted_sites': 7}]}, 'profile.json')
    assert {e['status'] for e in collector.entries} == {'read_failed', 'omitted'}
    collector.evidence_issues({'evidence_kind': 'python_allocations', 'status': 'running',
                              'samples': None}, 'invalid-profile.json')
    assert collector.entries[-1]['status'] == 'incomplete'
    path = collector.run()
    with zipfile.ZipFile(path) as zipped:
        manifest = zipped.read('export.json')
        assert b'not_enabled_or_no_capture' in manifest
