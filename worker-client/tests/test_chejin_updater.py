from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from chejin_worker_client.chejin_updater import (
    run_missing_result_recovery,
    run_update,
    validate_update_plan,
)
import chejin_worker_client.chejin_updater as updater_module
from chejin_worker_client.client_update import ClientUpdateError
from chejin_worker_client.client_update import canonical_release_manifest
from chejin_worker_client.models import ClientRelease


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows uses the packaged two-executable workflow test")


HEALTHY_WORKER = """#!/usr/bin/env python3
import argparse, hashlib, json, pathlib, time
p=argparse.ArgumentParser(); p.add_argument('--post-update-plan'); p.add_argument('--post-rollback-plan'); p.add_argument('--post-update-token', required=True); a=p.parse_args()
plan_path=pathlib.Path(a.post_update_plan or a.post_rollback_plan); plan=json.loads(plan_path.read_text(encoding='utf-8'))
if a.post_update_plan:
 marker=pathlib.Path(plan['healthy_marker_path']); marker.parent.mkdir(parents=True, exist_ok=True); marker.write_text(json.dumps({'healthy': True, 'version': plan['target_version'], 'update_request_id': plan['update_request_id'], 'one_time_token_sha256': hashlib.sha256(a.post_update_token.encode()).hexdigest(), 'runtime_health': {'ready': True, 'binding_state': 'bound', 'ui_event_loop_alive': True, 'required_threads': ['task_runner', 'c2_listener', 'thread_monitor'], 'threads': {name: {'entered_loop': True, 'alive': True} for name in ('task_runner', 'c2_listener', 'thread_monitor')}, 'startup_failures': [], 'stable_sample_count': 3, 'stable_for_ms': 1250}}), encoding='utf-8')
time.sleep(2)
"""

FAILED_WORKER = """#!/usr/bin/env python3
raise SystemExit(7)
"""


def _write_worker(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, new_worker: str) -> tuple[Path, str, Path, Path]:
    control = tmp_path / "control"
    current = tmp_path / "install" / "CheJinWorkerClient"
    staged = tmp_path / "staging" / "CheJinWorkerClient"
    previous = tmp_path / "install" / "CheJinWorkerClient.previous"
    failed = tmp_path / "install" / "CheJinWorkerClient.failed"
    data = tmp_path / "data"
    archive = control / "client.zip"
    marker = control / "healthy.json"
    updater_ready = control / "updater-ready.json"
    for directory in (control, current, staged, data):
        directory.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"signed-archive")
    _write_worker(current / "worker.py", HEALTHY_WORKER)
    _write_worker(staged / "worker.py", new_worker)
    (staged / "CheJinWorkerClient.exe").write_bytes(b"client-exe")
    (staged / "CheJinUpdater.exe").write_bytes(b"updater-exe")

    manifest = {
        "schema_version": 1,
        "version": "0.9.60",
        "platform": "windows-x64",
        "git_commit": "b" * 40,
        "rollback_safe": True,
        "files": {
            "CheJinUpdater.exe": hashlib.sha256(b"updater-exe").hexdigest(),
            "CheJinWorkerClient.exe": hashlib.sha256(b"client-exe").hexdigest(),
            "worker.py": hashlib.sha256((staged / "worker.py").read_bytes()).hexdigest(),
        },
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    (staged / "update-package-manifest.json").write_bytes(manifest_bytes)

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_file = control / "keys.json"
    key_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": [
                    {
                        "key_id": "test-key",
                        "algorithm": "ed25519",
                        "public_key_base64": base64.b64encode(public_bytes).decode(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHEJIN_RELEASE_SIGNING_KEYS_PATH", str(key_file))
    release = ClientRelease(
        update_available=True,
        latest_version="0.9.60",
        channel="gray",
        platform="windows-x64",
        artifact_url="https://download.example.test/client.zip",
        artifact_size_bytes=archive.stat().st_size,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        manifest_signature=None,
        signature_key_id="test-key",
        git_commit="b" * 40,
        package_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        published_at="2026-09-01T00:00:00+00:00",
        release_notes="test",
        minimum_updater_version="0.9.59",
        rollback_safe=True,
    )
    signature = private_key.sign(canonical_release_manifest(release))
    release = ClientRelease(**{**release.__dict__, "manifest_signature": base64.b64encode(signature).decode()})
    token = "single-use-token"
    plan = {
        "schema_version": 1,
        "update_request_id": "update-process-test",
        "one_time_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "current_version": "0.9.59",
        "target_version": "0.9.60",
        "current_program_dir": str(current),
        "staged_program_dir": str(staged),
        "previous_program_dir": str(previous),
        "failed_program_dir": str(failed),
        "data_dir": str(data),
        "archive_path": str(archive),
        "healthy_marker_path": str(marker),
        "updater_ready_path": str(updater_ready),
        "worker_executable_relative": "worker.py",
        "old_pid": 0,
        "old_exit_timeout_seconds": 1,
        "health_timeout_seconds": 3,
        "result_timeout_seconds": 10,
        "safe_boundary": {
            "safe": True,
            "new_work_blocked": True,
            "backend_stopped_confirmed_or_unbound": True,
            "confirmed_run_status": "paused",
            "current_task": None,
            "inflight_flow_id": None,
            "task_lease_active": False,
            "ui_lock_active": False,
            "sidecar_active": False,
            "waiting_ledger": 0,
            "pending_c2_outbox": 0,
            "pending_sqlite_action_journal": 0,
            "pending_file_action_journal": 0,
            "pending_sent_ack": 0,
            "action_journal_state_unavailable": 0,
        },
        "release": release.__dict__,
    }
    plan_path = control / "update-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, token, current, previous


def test_real_new_process_writes_health_marker_and_directory_switch_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path, token, current, previous = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER)
    assert run_update(plan_path, token) == 0
    result = json.loads((plan_path.parent / "update-result.json").read_text())
    assert result["result_code"] == "UPDATE_SUCCEEDED"
    assert current.is_dir()
    assert previous.is_dir()
    assert (current / "worker.py").read_text() == HEALTHY_WORKER


def test_failed_new_process_is_moved_to_evidence_and_old_process_is_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path, token, current, previous = _prepare_plan(tmp_path, monkeypatch, new_worker=FAILED_WORKER)
    assert run_update(plan_path, token) == 1
    result = json.loads((plan_path.parent / "update-result.json").read_text())
    assert result["result_code"] == "UPDATE_ROLLED_BACK"
    assert current.is_dir()
    assert not previous.exists()
    assert (current / "worker.py").read_text() == HEALTHY_WORKER
    assert (tmp_path / "install" / "CheJinWorkerClient.failed" / "worker.py").read_text() == FAILED_WORKER


def test_missing_result_recovery_restores_previous_program_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, current, previous = _prepare_plan(
        tmp_path,
        monkeypatch,
        new_worker=FAILED_WORKER,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    staged = Path(plan["staged_program_dir"])
    os.replace(current, previous)
    os.replace(staged, current)

    assert run_missing_result_recovery(plan_path, token, 0) == 0
    result = json.loads((plan_path.parent / "update-result.json").read_text())
    assert result["state"] == "rolled_back"
    assert result["failure_code"] == "UPDATE_RESULT_MISSING"
    assert (current / "worker.py").read_text() == HEALTHY_WORKER
    assert not previous.exists()
    assert (tmp_path / "install" / "CheJinWorkerClient.failed" / "worker.py").read_text() == FAILED_WORKER

    assert run_missing_result_recovery(plan_path, token, 0) == 0


def test_plan_rejects_nested_program_or_data_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, current, _previous = _prepare_plan(
        tmp_path,
        monkeypatch,
        new_worker=HEALTHY_WORKER,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    nested_data = current / "business-data"
    nested_data.mkdir()
    plan["data_dir"] = str(nested_data)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ClientUpdateError) as raised:
        validate_update_plan(plan_path, token)
    assert raised.value.code == "UPDATE_INSTALL_FAILED"
    assert "不得相互包含" in str(raised.value)


def test_plan_rejects_archive_inside_replaceable_program_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, current, _previous = _prepare_plan(
        tmp_path,
        monkeypatch,
        new_worker=HEALTHY_WORKER,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    original_archive = Path(plan["archive_path"])
    nested_archive = current / "downloads" / "client.zip"
    nested_archive.parent.mkdir(parents=True)
    nested_archive.write_bytes(original_archive.read_bytes())
    original_archive.unlink()
    plan["archive_path"] = str(nested_archive)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ClientUpdateError) as raised:
        validate_update_plan(plan_path, token)
    assert raised.value.code == "UPDATE_INSTALL_FAILED"
    assert "更新包不得位于可替换程序目录内" in str(raised.value)


def test_plan_rejects_unsettled_business_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, _current, _previous = _prepare_plan(
        tmp_path,
        monkeypatch,
        new_worker=HEALTHY_WORKER,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["safe_boundary"]["pending_sent_ack"] = 1
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ClientUpdateError) as raised:
        validate_update_plan(plan_path, token)
    assert raised.value.code == "UPDATE_INSTALL_FAILED"
    assert "未结算业务记录" in str(raised.value)


def test_staged_directory_switch_failure_restarts_unchanged_old_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, current, previous = _prepare_plan(
        tmp_path,
        monkeypatch,
        new_worker=HEALTHY_WORKER,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    staged = Path(plan["staged_program_dir"])
    real_replace = updater_module.os.replace

    def fail_only_staged_switch(source, destination):
        if Path(source) == staged and Path(destination) == current:
            raise PermissionError("simulated directory switch refusal")
        return real_replace(source, destination)

    monkeypatch.setattr(updater_module.os, "replace", fail_only_staged_switch)

    assert run_update(plan_path, token) == 1
    result = json.loads((plan_path.parent / "update-result.json").read_text())
    assert result["result_code"] == "UPDATE_ROLLED_BACK"
    assert current.is_dir()
    assert not previous.exists()
    assert (current / "worker.py").read_text() == HEALTHY_WORKER


def test_updater_startup_diagnostic_records_phases_without_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic_path = tmp_path / "updater-startup.jsonl"
    plan_path = tmp_path / "update-plan.json"
    plan_path.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
    monkeypatch.setenv("CHEJIN_UPDATER_DIAGNOSTIC_PATH", str(diagnostic_path))

    assert updater_module.main(
        ["--plan", str(plan_path), "--token", "must-not-enter-diagnostics"]
    ) == 1

    records = [json.loads(line) for line in diagnostic_path.read_text().splitlines()]
    assert [item["phase"] for item in records] == [
        "main_entered",
        "plan_validation_started",
        "update_failed",
    ]
    assert records[-1]["error_code"] == "UPDATE_INSTALL_FAILED"
    assert "must-not-enter-diagnostics" not in diagnostic_path.read_text()
