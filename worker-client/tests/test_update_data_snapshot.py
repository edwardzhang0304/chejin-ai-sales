from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage
from chejin_worker_client.models import Binding
import chejin_worker_client.update_data_snapshot as snapshot_module


def _use_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    monkeypatch.setattr(snapshot_module, "CONFIG", SimpleNamespace(app_dir=tmp_path))


def test_update_snapshot_preserves_database_and_existing_evidence_but_allows_new_incident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_data_dir(tmp_path, monkeypatch)
    storage.connect().close()
    evidence = tmp_path / "incidents" / "before.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("before", encoding="utf-8")
    screenshot = tmp_path / "artifacts" / "wechat_c2" / "before.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"before-image")
    expected = snapshot_module.protected_update_snapshot()

    (tmp_path / "incidents" / "after.json").write_text("after", encoding="utf-8")
    snapshot_module.assert_protected_update_snapshot(expected)

    evidence.write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="UPDATE_PROTECTED_FILE_CHANGED"):
        snapshot_module.assert_protected_update_snapshot(expected)

    evidence.write_text("before", encoding="utf-8")
    screenshot.write_bytes(b"tampered-image")
    with pytest.raises(RuntimeError, match="UPDATE_PROTECTED_FILE_CHANGED"):
        snapshot_module.assert_protected_update_snapshot(expected)


def test_update_snapshot_detects_binding_or_business_table_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_data_dir(tmp_path, monkeypatch)
    storage.connect().close()
    expected = snapshot_module.protected_update_snapshot()
    storage.save_binding(
        Binding(
            worker_id="worker-a",
            worker_token="secret-token",
            client_instance_id="instance-a",
            run_status="paused",
        )
    )

    with pytest.raises(RuntimeError, match="UPDATE_PROTECTED_DATABASE_CHANGED"):
        snapshot_module.assert_protected_update_snapshot(expected)


def test_update_snapshot_ignores_new_compatible_column_but_not_frozen_field_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_data_dir(tmp_path, monkeypatch)
    storage.connect().close()
    storage.save_binding(
        Binding(
            worker_id="worker-a",
            worker_token="secret-token",
            client_instance_id="instance-a",
            run_status="paused",
        )
    )
    expected = snapshot_module.protected_update_snapshot()

    with storage.db_connection() as conn:
        conn.execute(
            "ALTER TABLE binding "
            "ADD COLUMN future_compatible_note TEXT NOT NULL DEFAULT ''"
        )
    snapshot_module.assert_protected_update_snapshot(expected)

    with storage.db_connection() as conn:
        conn.execute("UPDATE binding SET run_status = 'faulted' WHERE id = 1")
        conn.commit()
    with pytest.raises(RuntimeError, match="UPDATE_PROTECTED_DATABASE_CHANGED"):
        snapshot_module.assert_protected_update_snapshot(expected)
