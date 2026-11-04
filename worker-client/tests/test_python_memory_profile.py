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
    assert not (tmp_path / 'artifacts/runtime/python-memory').exists()
    state = json.loads((tmp_path / 'artifacts/runtime/python-memory-status.json').read_text())
    assert state['status'] == 'disabled' and state['enabled'] is False
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


@pytest.mark.parametrize('enabled,crash', [(False, False), (True, False), (True, True)])
def test_real_main_profile_option_and_exit_do_not_replace_business_gates(tmp_path, monkeypatch, enabled, crash):
    import sys
    from types import SimpleNamespace
    from chejin_worker_client import main, config, release_transport, hook_installation
    from chejin_worker_client import update_data_access, runtime_supervision, performance_evidence
    monkeypatch.setattr(sys, 'argv', ['worker'] + (['--python-memory-profile-seconds', '30'] if enabled else []))
    monkeypatch.setenv('CHEJIN_WORKER_UI_MODE', 'pyside')
    monkeypatch.setattr(config, 'CONFIG', SimpleNamespace(app_dir=tmp_path, api_base_url='https://synthetic.invalid'))
    guards = []
    monkeypatch.setattr(main, 'acquire_single_instance', lambda: guards.append('instance') or SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(update_data_access, 'acquire_data_access', lambda root: guards.append('data'))
    monkeypatch.setattr(update_data_access, 'clear_update_writer', lambda: None)
    monkeypatch.setattr(release_transport, 'packaged_transport', lambda: 'hook')
    monkeypatch.setattr(hook_installation, 'launch_information', lambda: guards.append('deployment') or {'api_base_url': 'https://synthetic.invalid'})
    monkeypatch.setattr(main, 'bootstrap_qt_plugins', lambda: None)
    monkeypatch.setattr(runtime_supervision, 'install_runtime_supervision', lambda: None)
    monkeypatch.setattr(runtime_supervision, 'mark_runtime_clean_exit', lambda code: None)
    monkeypatch.setattr(performance_evidence, 'install', lambda root: None)
    def app():
        assert guards == ['instance', 'data', 'deployment']
        if enabled:
            deadline = time.monotonic() + 3
            while not tracemalloc.is_tracing() and time.monotonic() < deadline:time.sleep(.01)
            assert tracemalloc.is_tracing()
        else:assert not tracemalloc.is_tracing()
        if crash:raise RuntimeError('synthetic UI exit')
        return 0
    monkeypatch.setitem(sys.modules, 'chejin_worker_client.ui', SimpleNamespace(run_app=app))
    if crash:
        with pytest.raises(RuntimeError, match='synthetic UI exit'):main.main()
    else:assert main.main() == 0
    assert not tracemalloc.is_tracing()
    reports = list((tmp_path / 'artifacts/runtime/python-memory').glob('*.json'))
    assert len(reports) == int(enabled)
    if enabled:assert json.loads(reports[0].read_text())['end_reason'] == 'worker_exit'


def test_fallback_failure_is_exported_as_failed_not_just_collected(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    profile = module.Profile(tmp_path, 30)
    profile.gap('synthetic_start_failure')
    root = tmp_path / 'artifacts/runtime'
    (root / 'hook-memory-gap.json').write_text(json.dumps({'status': 'pre_call_evidence_failed',
        'action_id': 'synthetic-action', 'process_generation': 'synthetic-generation'}))
    path = export_bundle(tmp_path, tmp_path / 'gap.zip')
    with zipfile.ZipFile(path) as zipped:
        manifest = json.loads(zipped.read('export.json'))
        assert manifest['partial']
        failures = [r for r in manifest['sources'] if r['status'] == 'read_failed']
        assert any(r['reason'] == 'python_profile_not_completed' for r in failures)
        assert any(r['reason'] == 'hook_pre_call_evidence_failed' for r in failures)


@pytest.mark.parametrize('cause', ['quota', 'directory', 'thread', 'write'])
def test_actual_failure_after_old_success_has_current_identity_in_new_zip(tmp_path, monkeypatch, cause):
    from pathlib import Path
    from chejin_worker_client.diagnostic_bundle import export_bundle
    old = module.Profile(tmp_path, 1)
    old.begin(); old.finish()
    old_bytes = old.path.read_bytes()
    expected = None
    if cause == 'quota':
        monkeypatch.setattr(module, 'MAX_FOLDER_BYTES', 10)
        expected = 'profile_folder_budget'
    elif cause == 'directory':
        for n in range(129):(old.root / f'synthetic-{n}.txt').touch()
        expected = 'profile_directory_boundary'
    elif cause == 'thread':
        def failed(_):raise RuntimeError('private test detail')
        monkeypatch.setattr(module.threading.Thread, 'start', failed)
        expected = 'RuntimeError'
    else:
        original = Path.write_bytes
        def failed(path, data):
            if path.parent == old.root:raise OSError('private test detail')
            return original(path, data)
        monkeypatch.setattr(Path, 'write_bytes', failed)
        expected = 'OSError'
    current = module.start_profile(tmp_path, 30)
    if current is not None:
        current.thread.join(timeout=5)
        assert not current.thread.is_alive()
    assert not tracemalloc.is_tracing() and old.path.read_bytes() == old_bytes
    archive = export_bundle(tmp_path, tmp_path / 'failure.zip')
    with zipfile.ZipFile(archive) as zipped:
        state = json.loads(zipped.read('logs/runtime/python-memory-status.json'))
        gap = json.loads(zipped.read('logs/runtime/python-memory-gap.json'))
        manifest = json.loads(zipped.read('export.json'))
    assert state['capture_id'] == gap['capture_id'] != old.data['capture_id']
    assert state['pid'] == gap['pid'] and state['started_wall'] == gap['started_wall']
    assert state['status'] == 'capture_failed' and state['reason'] == gap['reason'] == expected
    failures = [e for e in manifest['sources'] if e['status'] == 'read_failed' and e.get('capture_identity', {}).get('capture_id') == gap['capture_id']]
    assert failures and all(e['capture_reason'] == expected for e in failures)
    assert b'private test detail' not in json.dumps(manifest).encode()


def test_empty_directory_and_old_report_do_not_replace_current_disabled_status(tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    old = module.Profile(tmp_path, 1)
    old.begin(); old.finish()
    assert module.start_profile(tmp_path, 0) is None
    archive = export_bundle(tmp_path, tmp_path / 'disabled.zip')
    with zipfile.ZipFile(archive) as zipped:
        state = json.loads(zipped.read('logs/runtime/python-memory-status.json'))
        manifest = json.loads(zipped.read('export.json'))
    assert state['status'] == 'disabled' and state['capture_id'] != old.data['capture_id']
    assert any(e['status'] == 'missing' and e.get('capture_identity', {}).get('capture_id') == state['capture_id'] for e in manifest['sources'])
    assert old.path.exists() and not tracemalloc.is_tracing()


def test_disabled_state_write_failure_cannot_silently_reuse_old_success(tmp_path, monkeypatch):
    from pathlib import Path
    from chejin_worker_client.diagnostic_bundle import export_bundle
    old = module.Profile(tmp_path, 1)
    old.begin(); old.finish()
    original = Path.write_text
    def deny_status(path, *a, **k):
        if path.name == 'python-memory-status.tmp':raise PermissionError('synthetic')
        return original(path, *a, **k)
    monkeypatch.setattr(Path, 'write_text', deny_status)
    assert module.start_profile(tmp_path, 0) is None
    archive = export_bundle(tmp_path, tmp_path / 'disabled-failed.zip')
    with zipfile.ZipFile(archive) as zipped:
        gap = json.loads(zipped.read('logs/runtime/python-memory-gap.json'))
        manifest = json.loads(zipped.read('export.json'))
    assert gap['capture_id'] != old.data['capture_id'] and gap['enabled'] is False
    assert gap['reason'] == 'disabled_status_write_failed'
    assert any(e['status'] == 'read_failed' and e.get('capture_identity', {}).get('capture_id') == gap['capture_id'] for e in manifest['sources'])
    assert not tracemalloc.is_tracing()
