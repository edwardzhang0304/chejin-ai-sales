"""Strict, one-release bridge from the shipped 0.9.67/0.9.68 updaters to 0.9.69.

The legacy snapshot is NEVER discarded or rebased. It must match before any
initialization and again afterward. The v2 snapshot additionally protects init.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

import psutil

from .client_update import UpdateStateStore, hash_file
from .models import ClientRelease
from .release_package_contract import validate_release_contract, verify_release_signature, verify_staged_package
from .update_data_access import acquire_update_access, clear_update_writer, canonical
from .update_data_snapshot import (
    PROTECTED_TABLE_FIELDS, _read_transaction, _canonical_rows,
    protected_update_snapshot, capture_data_baseline,
)

LEGACY_UPDATER_SHA256 = {
    "0.9.67": "2120b60f83a08807e066c6cc23c1c4523ba6b347641f77840f416f950f0df35b",
    "0.9.68": "f28068d191d54ad8d3f9efec549a4676aeb6f56305e8157d0d3ce20949b7f91e",
}


def assert_legacy_snapshot(expected: dict, data_dir: Path) -> None:
    if not isinstance(expected, dict) or expected.get("snapshot_schema_version") != 1:
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    tables = expected.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(PROTECTED_TABLE_FIELDS):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    actual = {}
    with _read_transaction(data_dir) as conn:
        for table, fields in PROTECTED_TABLE_FIELDS.items():
            if not isinstance(tables[table], dict) or tables[table].get("fields") != list(fields):
                raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
            rows = _canonical_rows(conn, table, fields)
            actual[table] = {"fields": list(fields), "row_count": len(rows),
                             "sha256": hashlib.sha256(canonical(rows)).hexdigest()}
    if actual != tables:
        raise RuntimeError("UPDATE_PROTECTED_DATABASE_CHANGED")
    expected_files = expected.get("files")
    if not isinstance(expected_files, dict):
        raise RuntimeError("UPDATE_PROTECTED_FILE_SNAPSHOT_INVALID")
    actual_files = protected_update_snapshot(data_dir=data_dir)["files"]
    if any(actual_files.get(path) != value for path, value in expected_files.items()):
        raise RuntimeError("UPDATE_PROTECTED_FILE_CHANGED")


def _validate_legacy_parent(plan: dict, plan_path: Path) -> None:
    # Only the actual shipped updater is allowed to initiate this compatibility
    # entry, never an arbitrary caller supplying a schema-1 JSON file.
    updater = plan_path.parent / "CheJinUpdater.exe"
    if hash_file(updater) != LEGACY_UPDATER_SHA256.get(plan.get("current_version")):
        raise RuntimeError("UPDATE_LEGACY_UPDATER_UNTRUSTED")
    try:
        parents = psutil.Process().parents()
        if not any(Path(p.exe()).resolve() == updater.resolve() for p in parents[:3]):
            raise RuntimeError("UPDATE_LEGACY_PARENT_INVALID")
        old_pid = int(plan["old_pid"])
        if old_pid <= 0 or old_pid == os.getpid():
            raise RuntimeError("UPDATE_LEGACY_OLD_PROCESS_INVALID")
        if psutil.pid_exists(old_pid) and psutil.Process(old_pid).status() != psutil.STATUS_ZOMBIE:
            raise RuntimeError("UPDATE_WRITERS_NOT_STOPPED")
    except (psutil.Error, KeyError, ValueError) as exc:
        raise RuntimeError("UPDATE_LEGACY_PARENT_INVALID") from exc


@contextmanager
def legacy_handoff(plan: dict, plan_path: Path, token: str):
    if plan.get("schema_version") != 1 or plan.get("current_version") not in LEGACY_UPDATER_SHA256 or plan.get("target_version") != "0.9.69":
        raise RuntimeError("UPDATE_MANUAL_UPGRADE_REQUIRED")
    _validate_legacy_parent(plan, plan_path)
    store = UpdateStateStore()
    state = store.load()
    if (state.get("update_request_id") != plan.get("update_request_id")
            or Path(str(state.get("plan_path") or "")).resolve() != plan_path.resolve()
            or not state.get("install_started")):
        raise RuntimeError("UPDATE_LEGACY_REQUEST_INVALID")
    release = ClientRelease.from_api({**plan["release"], "update_available": True,
                                     "latest_version": plan["target_version"]})
    validate_release_contract(release, current_version=plan["current_version"], require_download_url=False)
    verify_release_signature(release)
    verify_staged_package(release, Path(plan["current_program_dir"]))
    # Original plan stays immutable for the old updater's result/rollback path.
    converted = {k: v for k, v in plan.items() if k != "protected_data_snapshot"}
    converted.update(schema_version=2, data_baseline_path=str(plan_path.parent / "protected-data-baseline.json"),
                     legacy_source_plan_schema=1)
    guard = None
    try:
        guard = acquire_update_access(converted, token)
        assert_legacy_snapshot(plan.get("protected_data_snapshot"), Path(plan["data_dir"]))
        capture_data_baseline(converted, plan_path, token)
        yield converted
        assert_legacy_snapshot(plan["protected_data_snapshot"], Path(plan["data_dir"]))
    except Exception:
        # Old updater will launch its old client on failure. Persist the fault
        # intent in its existing request state, outside protected business data,
        # so reconciliation cannot restore a pre-update running state.
        latest = store.load()
        if latest.get("update_request_id") == plan["update_request_id"]:
            store.save({**latest, "fault_after_request": True, "data_integrity_failed": True})
        clear_update_writer()
        raise
    finally:
        if guard is not None:
            guard.close()
