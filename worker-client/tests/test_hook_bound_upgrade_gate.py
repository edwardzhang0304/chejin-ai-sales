"""Acceptance-tool tests; no changes to either shipped Worker or updater."""
import copy
import sqlite3
import importlib.util
from pathlib import Path

import pytest
from chejin_worker_client import storage
from chejin_worker_client.models import Binding

spec = importlib.util.spec_from_file_location('bound_upgrade_driver',
    Path(__file__).parents[1] / 'scripts/run-windows-client-upgrade-test.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
hook = gate.hook_upgrade_fixture


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    storage.save_binding(Binding('00000000-0000-4000-8000-000000000001', 'synthetic-token', 'synthetic-instance', run_status='paused'))
    storage.request_runtime_pause()
    hook.seed(tmp_path, 'https://127.0.0.1:4443/api', '00000000-0000-4000-8000-000000000001', 'synthetic-instance', '2026-10-16T04:00:00+00:00')
    (tmp_path / 'incidents').mkdir()
    (tmp_path / 'incidents/existing-evidence.json').write_text('{}')
    return tmp_path


def test_bound_fixture_uses_real_hook_schemas_and_completed_history(fixture):
    snap = hook.snapshot(fixture)
    assert set(snap['tables']) == set(hook.TABLES)
    assert len(snap['tables']['hook_raw_inbox']) == 3
    assert snap['tables']['hook_delivery_outbox'][0][10] == 'stored'
    assert snap['tables']['hook_voice_attempts'][0][7] == 'transcribed'
    assert len(snap['journal_files']) == 3
    gate.assert_preserved(gate.preserved_values(fixture), gate.preserved_values(fixture))


@pytest.mark.parametrize('change', [
    "DELETE FROM hook_raw_inbox", "UPDATE hook_voice_attempts SET text='changed'",
    "UPDATE hook_delivery_outbox SET receipt_json='{}'", "DELETE FROM hook_observation_projection",
])
def test_hook_data_loss_or_change_is_rejected(fixture, change):
    before = gate.preserved_values(fixture)
    # Inject offline corruption without normal application FK protection.
    with sqlite3.connect(fixture / 'worker_client.sqlite3') as db:
        db.execute(change)
    with pytest.raises(AssertionError):
        gate.assert_preserved(before, gate.preserved_values(fixture))


def test_send_journal_change_is_rejected(fixture):
    before = gate.preserved_values(fixture)
    next((fixture / 'transactions/hook').rglob('outcome.json')).write_text('{}')
    with pytest.raises(AssertionError, match='business data changed: hook'):
        gate.assert_preserved(before, gate.preserved_values(fixture))


def test_expected_local_environment_fault_preserves_all_other_state(fixture):
    before = gate.preserved_values(fixture)
    with storage.connect() as db:
        db.execute("UPDATE binding SET run_status='faulted'")
    after = gate.preserved_values(fixture)
    hook.assert_environment_fault(before, after)
    damaged = copy.deepcopy(after)
    damaged['binding'] = [('other-worker', *after['binding'][0][1:])]
    with pytest.raises(AssertionError, match='Unexpected data change'):
        hook.assert_environment_fault(before, damaged)
    with pytest.raises(AssertionError, match='business data changed: binding'):
        gate.assert_preserved(before, after)


@pytest.mark.parametrize('bad', ['c2_listener', 'dead_loop', 'startup_failure'])
def test_hook_health_cannot_be_replaced_by_ocr_or_dead_threads(bad):
    health = {'binding_state': 'bound', 'required_threads': list(hook.THREADS), 'startup_failures': [],
              'threads': {name: {'entered_loop': True, 'alive': True} for name in hook.THREADS}}
    hook.assert_health({'runtime_health': health})
    if bad == 'c2_listener':
        health['required_threads'] = ['task_runner', 'thread_monitor', 'c2_listener']
    elif bad == 'dead_loop':
        health['threads']['hook_intake']['alive'] = False
    else:
        health['startup_failures'] = ['hook_business']
    with pytest.raises(AssertionError):
        hook.assert_health({'runtime_health': health})


@pytest.mark.parametrize('substitution', ['--manual-install', '--updater-repair=synthetic.ps1'])
def test_bound_gate_rejects_repair_or_manual_substitution(substitution):
    import subprocess
    import sys
    script = Path(__file__).parents[1] / 'scripts/run-windows-client-upgrade-test.py'
    result = subprocess.run([sys.executable, str(script), '--hook-bound', substitution], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'requires original updater and real button' in result.stderr


def test_environment_fault_waits_for_this_process_and_rejects_other_errors(fixture):
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp()
    assert hook.environment_fault(fixture, not_before=cutoff) is None
    with storage.connect() as db:
        db.execute("UPDATE binding SET run_status='faulted'")
    old = storage.append_log('ERROR', 'hook_connection_fault', 'synthetic old result',
                             error_code='HOOK_PROCESS_MISSING_OR_AMBIGUOUS')
    # A log that exists but belongs to the old lifetime cannot pass the new one.
    assert hook.environment_fault(fixture, not_before=cutoff + 3600) is None
    assert hook.environment_fault(fixture, not_before=cutoff, excluded_ids=[old['id']]) is None
    fresh = storage.append_log('ERROR', 'hook_connection_fault', 'synthetic fresh result',
                               error_code='HOOK_PROCESS_MISSING_OR_AMBIGUOUS')
    result = hook.environment_fault(fixture, not_before=cutoff, excluded_ids=[old['id']])
    assert result['log_ids'] == [fresh['id']] and result['run_status'] == 'faulted'
    storage.append_log('ERROR', 'hook_connection_fault', 'synthetic unexpected result',
                       error_code='HOOK_COMPONENT_HASH_OR_LOAD_MISMATCH')
    with pytest.raises(AssertionError, match='Unexpected current-process Hook fault'):
        hook.environment_fault(fixture, not_before=cutoff, excluded_ids=[old['id']])
