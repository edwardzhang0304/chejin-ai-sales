"""A normal gate cycle must pass; actual persisted data damage must still fail."""
import importlib.util
from pathlib import Path

import pytest

from chejin_worker_client import storage
from chejin_worker_client.models import Binding

spec = importlib.util.spec_from_file_location(
    "windows_upgrade_gate", Path(__file__).parents[1] / "scripts/run-windows-client-upgrade-test.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    storage.save_binding(Binding("synthetic-worker", "synthetic-token", "synthetic-instance", run_status="paused"))
    storage.save_accept_schedule(enabled=True, start="09:10", end="18:20")
    storage.request_runtime_pause()
    (tmp_path / "incidents").mkdir()
    (tmp_path / "incidents/existing-evidence.json").write_text('{}')
    with storage.connect() as db:
        db.execute("INSERT INTO c2_runtime_state VALUES ('upgrade_gate_history', '{}', 'before')")
        db.execute("INSERT INTO c2_message_ledger VALUES ('isolated-history', 'message', 'read', 'dedupe', 'text', 'completed', 'confirmed', '{}', 'before', 'before')")
    return tmp_path, gate.preserved_values(tmp_path)


def test_real_runtime_gate_cycle_keeps_business_and_pause(seeded):
    data, before = seeded
    storage.set_update_new_work_gate(True, update_request_id="synthetic-request")
    with pytest.raises(AssertionError, match="gate was not preserved"):
        gate.assert_preserved(before, gate.preserved_values(data))
    storage.set_update_new_work_gate(False, update_request_id="synthetic-request")
    after = gate.preserved_values(data)
    assert before != after  # A real state transition advances the operational timestamp.
    gate.assert_preserved(before, after)


@pytest.mark.parametrize("sql", [
    "UPDATE binding SET worker_token='changed'",
    "UPDATE binding SET run_status='running'",
    "UPDATE client_settings SET value='{}' WHERE key='accept_schedule'",
    "UPDATE client_settings SET updated_at='changed' WHERE key='accept_schedule'",
    "DELETE FROM client_settings WHERE key='accept_schedule'",
    "UPDATE c2_runtime_state SET value='changed'",
    "DELETE FROM c2_message_ledger",
    "UPDATE c2_message_ledger SET result_json='changed'",
])
def test_actual_business_row_damage_is_rejected(seeded, sql):
    data, before = seeded
    with storage.connect() as db:
        db.execute(sql)
    with pytest.raises(AssertionError, match="business data changed"):
        gate.assert_preserved(before, gate.preserved_values(data))


def test_lost_pause_intent_is_rejected(seeded):
    data, before = seeded
    storage.clear_runtime_pause()
    with pytest.raises(AssertionError, match="gate was not preserved"):
        gate.assert_preserved(before, gate.preserved_values(data))


def test_missing_runtime_row_is_rejected(seeded):
    data, before = seeded
    with storage.connect() as db:
        db.execute("DELETE FROM client_settings WHERE key='runtime_control_v1'")
    with pytest.raises(AssertionError, match="one persisted runtime"):
        gate.assert_preserved(before, gate.preserved_values(data))


def test_changed_evidence_is_rejected(seeded):
    data, before = seeded
    (data / "incidents/existing-evidence.json").write_text('{"changed":true}')
    with pytest.raises(AssertionError, match="business data changed: evidence"):
        gate.assert_preserved(before, gate.preserved_values(data))
