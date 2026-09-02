from __future__ import annotations

from pathlib import Path

from chejin_worker_client import storage


def test_stale_update_request_cannot_clear_newer_intake_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")

    storage.set_update_new_work_gate(True, update_request_id="update-new")
    stale = storage.set_update_new_work_gate(
        False,
        update_request_id="update-old",
    )

    assert stale["update_no_new_work"] is True
    assert stale["update_request_id"] == "update-new"
    current = storage.load_runtime_control()
    assert current["update_no_new_work"] is True
    assert current["update_request_id"] == "update-new"

    cleared = storage.set_update_new_work_gate(
        False,
        update_request_id="update-new",
    )
    assert cleared["update_no_new_work"] is False
    assert cleared["update_request_id"] is None
