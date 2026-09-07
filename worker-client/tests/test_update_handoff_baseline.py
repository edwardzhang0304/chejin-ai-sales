"""Real SQLite/WAL and process ownership tests; all identities/data are synthetic."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from chejin_worker_client import storage
from chejin_worker_client.models import Binding
from chejin_worker_client import update_data_snapshot as snapshots
from chejin_worker_client.update_data_access import (
    acquire_update_access, authorize_update_writer, clear_update_writer, process_identity,
)
from chejin_worker_client.chejin_updater import wait_for_old_writers


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(storage, 'APP_DIR', root)
    monkeypatch.setattr(storage, 'DB_FILE', root / 'worker_client.sqlite3')
    monkeypatch.setattr(snapshots, 'CONFIG', SimpleNamespace(app_dir=root))
    storage.save_binding(Binding('synthetic-worker', 'sentinel-do-not-log-key', 'synthetic-instance', run_status='paused'))
    storage.set_update_new_work_gate(True, update_request_id='test-update')
    yield root
    clear_update_writer()


def plan_for(data):
    control = data.parent / 'control'
    control.mkdir(exist_ok=True)
    plan_path = control / 'update-plan.json'
    plan = {'schema_version': 2, 'update_request_id': 'test-update', 'current_version': '0.9.68',
            'target_version': '0.9.69', 'data_dir': str(data),
            'data_baseline_path': str(control / 'protected-data-baseline.json')}
    plan_path.write_text(json.dumps(plan))
    return plan, plan_path, 'test-only-one-time-authentication'


def test_noop_binding_and_runtime_control_preserve_timestamps_and_true_changes_write(data):
    with storage.db_connection() as c:
        before = dict(c.execute('SELECT * FROM binding').fetchone())
        control = dict(c.execute('SELECT * FROM client_settings').fetchone())
    storage.save_binding(storage.load_binding())
    storage.set_update_new_work_gate(True, update_request_id='test-update')
    with storage.db_connection() as c:
        assert dict(c.execute('SELECT * FROM binding').fetchone()) == before
        assert dict(c.execute('SELECT * FROM client_settings').fetchone()) == control
    binding = storage.load_binding(); binding.run_status = 'faulted'
    storage.save_binding(binding)
    storage.set_update_new_work_gate(False, update_request_id='test-update')
    with storage.db_connection() as c:
        after = dict(c.execute('SELECT * FROM binding').fetchone())
        assert after['run_status'] == 'faulted' and after['updated_at'] != before['updated_at']
        assert dict(c.execute('SELECT * FROM client_settings').fetchone())['updated_at'] != control['updated_at']


def test_one_read_transaction_pins_all_tables_and_contains_committed_wal(data, monkeypatch):
    with storage.db_connection() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        c.execute("INSERT INTO c2_runtime_state VALUES ('wal-before', '{}', 'before')"); c.commit()
        assert Path(str(storage.DB_FILE) + '-wal').exists()
        expected = snapshots.protected_update_snapshot(digest_key='key')
        original = snapshots._canonical_rows
        fired = False
        def concurrent_commit(conn, table, fields):
            nonlocal fired
            rows = original(conn, table, fields)
            if not fired:
                fired = True
                with sqlite3.connect(storage.DB_FILE) as writer:
                    writer.execute("INSERT INTO c2_runtime_state VALUES ('wal-during', '{}', 'after')")
            return rows
        monkeypatch.setattr(snapshots, '_canonical_rows', concurrent_commit)
        actual = snapshots.protected_update_snapshot(digest_key='key')
        assert actual == expected
        monkeypatch.setattr(snapshots, '_canonical_rows', original)
        assert snapshots.protected_update_snapshot(digest_key='key') != expected


@pytest.mark.parametrize('change', ['binding', 'ledger', 'delete'])
def test_post_baseline_real_changes_reject_with_safe_field_diagnostics(data, change):
    with storage.db_connection() as c:
        c.execute("INSERT INTO c2_message_ledger (conversation_id,source_message_key,origin_read_run_id,dedupe_key,message_type,terminal_state,ingest_state,result_json,first_seen_at,updated_at) VALUES ('test-c','test-m','test-r','test-d','text','read','confirmed','{}','before','before')")
        c.commit()
    plan, path, token = plan_for(data)
    with acquire_update_access(plan, token):
        baseline = snapshots.capture_data_baseline(plan, path, token)
        # Deliberately bypass application locks with an external SQL connection to model tampering.
        with sqlite3.connect(storage.DB_FILE) as c:
            if change == 'binding': c.execute("UPDATE binding SET worker_token='new-secret' WHERE id=1")
            elif change == 'ledger': c.execute("UPDATE c2_message_ledger SET result_json='private-message'")
            else: c.execute('DELETE FROM c2_message_ledger')
        with pytest.raises(snapshots.SnapshotMismatch) as err:
            snapshots.assert_protected_update_snapshot(baseline['snapshot'], data_dir=data, digest_key=token)
        details = json.dumps(err.value.differences)
        assert 'sentinel' not in details and 'new-secret' not in details and 'private-message' not in details
        assert err.value.differences[0]['table'] == ('binding' if change == 'binding' else 'c2_message_ledger')
        if change == 'delete': assert err.value.differences[0]['missing'] == 1
        else: assert err.value.differences[0]['fields'] == {('worker_token' if change == 'binding' else 'result_json'): 1}


@pytest.mark.parametrize('mismatch', ['request', 'version', 'directory', 'auth', 'missing', 'corrupt'])
def test_authenticated_baseline_rejects_mismatch_without_recapture(data, mismatch):
    plan, path, token = plan_for(data)
    with acquire_update_access(plan, token):
        snapshots.capture_data_baseline(plan, path, token)
        original = Path(plan['data_baseline_path']).read_bytes()
        if mismatch == 'request': plan['update_request_id'] = 'different'
        elif mismatch == 'version': plan['target_version'] = '9.9.9'
        elif mismatch == 'directory': plan['data_dir'] = str(data.parent/'other')
        elif mismatch == 'auth': token = 'wrong'
        elif mismatch == 'missing': Path(plan['data_baseline_path']).unlink()
        else: Path(plan['data_baseline_path']).write_bytes(b'{corrupt')
        with pytest.raises(RuntimeError, match='UPDATE_DATA_BASELINE_INVALID'):
            snapshots.load_data_baseline(plan, path, token)
        with pytest.raises((RuntimeError, FileExistsError)):
            snapshots.capture_data_baseline(plan, path, token)
        if mismatch not in ('missing','corrupt'):
            assert Path(plan['data_baseline_path']).read_bytes() == original


def test_other_process_cannot_write_while_updater_owns_data(data):
    plan, path, token = plan_for(data)
    env = {**os.environ, 'CHEJIN_WORKER_HOME': str(data), 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    program = "from chejin_worker_client.storage import connect; connect()"
    with acquire_update_access(plan, token):
        snapshots.capture_data_baseline(plan, path, token)
        result = subprocess.run([sys.executable,'-c',program], env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode != 0 and 'UPDATE_DATA_DIRECTORY_BUSY' in result.stderr
        # The authenticated new Worker is the only delegated writer, with no release/reacquire gap.
        authorize_update_writer(plan, token)
        storage.connect().close()
        duplicate = subprocess.run([sys.executable,'-c',program], env=env, text=True, capture_output=True, timeout=10)
        assert duplicate.returncode != 0
    clear_update_writer()
    assert subprocess.run([sys.executable,'-c',program], env=env, capture_output=True, timeout=10).returncode == 0


def test_existing_connection_prevents_baseline_ownership(data):
    plan, path, token = plan_for(data)
    with storage.db_connection():
        with pytest.raises(RuntimeError, match='UPDATE_DATA_DIRECTORY_BUSY'):
            acquire_update_access(plan, token)
    assert not Path(plan['data_baseline_path']).exists()


def test_normal_lifetime_guard_allows_multiple_connections_but_excludes_updater(data):
    from chejin_worker_client.update_data_access import acquire_data_access
    plan, path, token = plan_for(data)
    with acquire_data_access(data):
        with storage.db_connection(), storage.db_connection():
            with pytest.raises(RuntimeError, match='UPDATE_DATA_DIRECTORY_BUSY'):
                acquire_update_access(plan, token)


def test_dropping_an_empty_protected_table_still_rejects(data):
    expected = snapshots.protected_update_snapshot(digest_key='key')
    with sqlite3.connect(storage.DB_FILE) as c:
        c.execute('DROP TABLE c2_message_ledger')
    with pytest.raises(snapshots.SnapshotMismatch) as err:
        snapshots.assert_protected_update_snapshot(expected, digest_key='key')
    assert err.value.differences[0]['table_presence_changed'] is True


def test_stop_for_update_waits_for_real_thread_final_database_commit(data):
    import threading
    from chejin_worker_client.task_runner import TaskRunner
    runner = object.__new__(TaskRunner)
    runner.stop_event = threading.Event()
    runner._task_wake_event = threading.Event()
    runner.c2_manual_scan_requested = threading.Event()
    runner.c2_thread = runner.thread_monitor = None
    def writer():
        runner.stop_event.wait(2)
        storage.save_accept_schedule(enabled=True, start='12:00', end='20:00')
    runner.thread = threading.Thread(target=writer)
    runner.thread.start()
    runner.stop_for_update(2)
    assert not runner.thread.is_alive()
    assert storage.load_accept_schedule()['start'] == '12:00'


def test_stop_for_update_times_out_without_killing_a_live_writer(data):
    import threading
    from chejin_worker_client.task_runner import TaskRunner
    runner = object.__new__(TaskRunner)
    runner.stop_event = threading.Event()
    runner._task_wake_event = threading.Event()
    runner.c2_manual_scan_requested = threading.Event()
    runner.c2_thread = runner.thread_monitor = None
    release = threading.Event()
    runner.thread = threading.Thread(target=lambda: release.wait(3))
    runner.thread.start()
    try:
        with pytest.raises(RuntimeError, match='UPDATE_WRITERS_NOT_STOPPED'):
            runner.stop_for_update(.01)
        assert runner.thread.is_alive()
    finally:
        release.set()
        runner.thread.join(2)


def test_old_writer_must_exit_and_wrong_identity_cannot_be_accepted():
    process = subprocess.Popen([sys.executable, '-c', 'import time;print("ready",flush=True);time.sleep(15)'], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'ready'
    try:
        identity = process_identity(process.pid)
        plan = {'old_exit_timeout_seconds': .02, 'old_process_identity': identity, 'old_child_identities': []}
        assert not wait_for_old_writers(plan)
        plan['old_process_identity'] = {**identity, 'create_time': identity['create_time']-10}
        with pytest.raises(RuntimeError, match='UPDATE_PROCESS_IDENTITY_MISMATCH'):
            wait_for_old_writers(plan)
    finally:
        process.terminate(); process.wait(timeout=5)
    plan['old_process_identity'] = identity
    assert wait_for_old_writers(plan)
