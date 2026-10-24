import json
from pathlib import Path

import pytest

from chejin_worker_client.update_progress import UpdateProgress, record_window_shown
from chejin_worker_client import chejin_updater as updater
from test_chejin_updater import _prepare_plan, HEALTHY_WORKER


def test_phase_durations_and_no_credentials(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('chejin_worker_client.update_progress.time.monotonic', lambda: clock[0])
    progress = UpdateProgress(tmp_path / 'plan.json', 'updater')
    with progress.stage('data_snapshot'):
        clock[0] = 12.5
    records = [json.loads(line) for line in (tmp_path / 'updater-progress.jsonl').read_text().splitlines()]
    assert [r['event'] for r in records] == ['started', 'completed']
    assert records[-1]['elapsed_ms'] == 2500
    assert set(records[-1]) == {'phase', 'event', 'elapsed_ms', 'timestamp_epoch', 'pid'}


def test_failed_phase_preserves_exception(tmp_path):
    progress = UpdateProgress(tmp_path / 'plan.json', 'worker')
    with pytest.raises(RuntimeError, match='sentinel'):
        with progress.stage('startup_data'):
            raise RuntimeError('sentinel')
    assert json.loads((tmp_path / 'worker-progress.json').read_text())['event'] == 'failed'


def test_unwritable_progress_does_not_change_business_result(tmp_path):
    path = tmp_path / 'not-directory'
    path.write_text('occupied')
    progress = UpdateProgress(path / 'plan.json', 'worker')
    with progress.stage('startup_files'):
        pass
    progress.close()


def test_worker_phase_visible_and_bad_record_ignored(tmp_path):
    parent = UpdateProgress(tmp_path / 'plan.json', 'updater')
    parent.begin('new_start')
    child = UpdateProgress(tmp_path / 'plan.json', 'worker')
    child.begin('startup_data')
    assert '核对原数据' in parent.text()
    (tmp_path / 'worker-progress.json').write_text('{bad')
    assert '正在启动新客户端' in parent.text()


def test_window_shown_records_ui_boundary_only(tmp_path):
    child = UpdateProgress(tmp_path / 'plan.json', 'worker')
    child.begin('startup_ui')
    record_window_shown({'mode': 'updated', 'plan': {'healthy_marker_path': str(tmp_path / 'healthy.json')}})
    lines = [json.loads(line) for line in (tmp_path / 'worker-progress.jsonl').read_text().splitlines()]
    assert [r['phase'] for r in lines] == ['startup_ui', 'startup_ui', 'window_shown']
    assert not (tmp_path / 'healthy.json').exists()


def test_revalidation_skips_only_archive_read(tmp_path, monkeypatch):
    plan_path, token, *_ = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER)
    original_hash = updater.hash_file
    reads = []
    monkeypatch.setattr(updater, 'hash_file', lambda path: (reads.append(path), original_hash(path))[1])
    first = updater.validate_update_plan(plan_path, token)
    second = updater.validate_update_plan(plan_path, token, verify_archive=False)
    assert first == second
    assert reads == [Path(first['archive_path'])]


@pytest.mark.parametrize('tamper', ['file', 'manifest', 'signature', 'binding_boundary', 'token'])
def test_optimized_check_still_rejects_tampering(tmp_path, monkeypatch, tamper):
    plan_path, token, *_ = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER)
    plan = updater.validate_update_plan(plan_path, token)
    staged = Path(plan['staged_program_dir'])
    raw = json.loads(plan_path.read_text())
    if tamper == 'file':
        (staged / 'CheJinWorkerClient.exe').write_bytes(b'changed')
    elif tamper == 'manifest':
        (staged / 'update-package-manifest.json').write_text('{}')
    elif tamper == 'signature':
        raw['release']['manifest_signature'] = 'bad'
    elif tamper == 'binding_boundary':
        raw['safe_boundary']['pending_sent_ack'] = 1
    else:
        token = 'incorrect'
    plan_path.write_text(json.dumps(raw))
    with pytest.raises(Exception):
        updater.validate_update_plan(plan_path, token, verify_archive=False)


def test_progress_is_exportable():
    from chejin_worker_client.diagnostic_bundle import UPDATE_DIAGNOSTICS
    for role in ('worker', 'updater'):
        assert f'{role}-progress.json' in UPDATE_DIAGNOSTICS
        assert f'{role}-progress.jsonl' in UPDATE_DIAGNOSTICS


def test_real_upgrade_hashes_zip_once(tmp_path, monkeypatch):
    plan_path, token, *_ = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER)
    original_hash = updater.hash_file
    reads = []
    monkeypatch.setattr(updater, 'hash_file', lambda path: (reads.append(path), original_hash(path))[1])
    assert updater.run_update(plan_path, token) == 0
    assert len(reads) == 1
    records = [json.loads(line) for line in (plan_path.parent / 'updater-progress.jsonl').read_text().splitlines()]
    completed = {r['phase'] for r in records if r['event'] == 'completed'}
    assert {'old_exit', 'data_lock', 'data_snapshot', 'package_check', 'replace', 'new_start'} <= completed


def test_tampering_after_ready_never_replaces_program(tmp_path, monkeypatch):
    plan_path, token, *_ = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER)
    raw = json.loads(plan_path.read_text())
    def exited(*args, **kwargs):
        (Path(raw['staged_program_dir']) / 'CheJinWorkerClient.exe').write_bytes(b'tampered')
        return True
    monkeypatch.setattr(updater, 'wait_for_old_writers', exited)
    assert updater.run_update(plan_path, token) == 1
    assert not Path(raw['previous_program_dir']).exists()
    assert json.loads((plan_path.parent / 'update-result.json').read_text())['state'] != 'succeeded'


@pytest.mark.parametrize('protocol,expected', [(None, 1), (1, 0)])
def test_old_updater_gets_worker_progress_fallback(tmp_path, monkeypatch, protocol, expected):
    from chejin_worker_client import update_progress as module
    shown = []
    monkeypatch.setattr(UpdateProgress, 'show', lambda self: shown.append(True))
    progress = module.start_worker_progress(tmp_path / 'plan.json', {'restart_progress_protocol': protocol})
    assert len(shown) == expected
    progress.begin('startup_ui')
    module.record_window_shown({'mode': 'updated', 'plan': {'healthy_marker_path': str(tmp_path / 'healthy.json')}})
    assert progress.closed.is_set()
    assert json.loads((tmp_path / 'worker-progress.json').read_text())['phase'] == 'window_shown'


def test_closing_progress_does_not_cancel_installer(tmp_path):
    progress = UpdateProgress(tmp_path / 'plan.json', 'updater')
    progress.begin('replace')
    progress.close()
    assert progress.closed.is_set()
    assert not (tmp_path / 'worker-shutdown-failed.json').exists()
    assert not (tmp_path / 'update-result.json').exists()


def test_window_api_uses_pointer_sized_handles_and_does_not_activate(tmp_path, monkeypatch):
    import ctypes
    from chejin_worker_client import update_progress as module
    calls = []
    class Function:
        def __init__(self, name): self.name = name
        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == 'CreateWindowExW': return 2**40
            return 0
    class User32:
        def __init__(self): self.functions = {}
        def __getattr__(self, name): return self.functions.setdefault(name, Function(name))
    user32 = User32()
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: user32, raising=False)
    progress = UpdateProgress(tmp_path / 'plan.json', 'updater')
    progress.closed.set()
    progress._window()
    creation = [args for name, args in calls if name == 'CreateWindowExW']
    assert len(creation) == 2
    assert creation[0][0] == 0x08000000
    assert creation[1][8] == 2**40
    assert user32.CreateWindowExW.restype == ctypes.wintypes.HWND
