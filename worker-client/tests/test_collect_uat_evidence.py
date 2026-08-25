from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "packaging" / "collect_uat_evidence.py"
FROM_ISO = "2026-08-15T12:10:00+00:00"
TO_ISO = "2026-08-15T12:40:00+00:00"
EVENT_ISO = "2026-08-15T12:20:00+00:00"


def _touch_in_window(path: Path) -> None:
    timestamp = datetime.fromisoformat(EVENT_ISO).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_time_window_evidence_export_is_read_only_redacted_and_complete(tmp_path: Path) -> None:
    app_dir = tmp_path / "CheJinWorker"
    package_dir = tmp_path / "CheJinWorkerDebug"
    app_dir.mkdir()
    (package_dir / "app" / "contracts").mkdir(parents=True)
    (package_dir / "fast-uat-manifest.json").write_text(
        json.dumps(
            {
                "version": "0.9.34",
                "git_commit": "commit-123",
                "git_branch": "codex/gray-release-0.9.x",
                "git_dirty": False,
                "omniauto_source": {"source_commit": "omniauto-123"},
            }
        ),
        encoding="utf-8",
    )
    contract = {
        "contract_version": 3,
        "contract_revision": "0.9.34",
        "observation_schema_version": 3,
    }
    (package_dir / "app" / "contracts" / "c2_contract_v3.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )

    database = app_dir / "worker_client.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE binding (id INTEGER, worker_id TEXT, worker_token TEXT,
              client_instance_id TEXT, run_status TEXT, bound_at TEXT, updated_at TEXT);
            CREATE TABLE local_logs (id TEXT, created_at TEXT, level TEXT, event TEXT,
              task_id TEXT, error_code TEXT, message TEXT, metadata TEXT);
            CREATE TABLE c2_message_ledger (conversation_id TEXT, source_message_key TEXT,
              origin_read_run_id TEXT, dedupe_key TEXT, message_type TEXT,
              terminal_state TEXT, ingest_state TEXT, result_json TEXT,
              first_seen_at TEXT, updated_at TEXT);
            CREATE TABLE c2_action_journal (flow_id TEXT, conversation_id TEXT,
              source_message_key TEXT, origin_read_run_id TEXT, outcome_json TEXT,
              created_at TEXT, updated_at TEXT);
            CREATE TABLE c2_ingest_outbox (outbox_id TEXT, conversation_id TEXT,
              authorization_revision TEXT, read_run_id TEXT, payload_json TEXT,
              status TEXT, attempt_count INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE reply_send_ack_outbox (reply_action_id TEXT, task_id TEXT,
              send_token TEXT, status TEXT, action_phase TEXT, reply_text_hash TEXT,
              ack_payload_json TEXT, attempt_count INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE c2_runtime_state (key TEXT, value TEXT, updated_at TEXT);
            """
        )
        connection.execute(
            "INSERT INTO binding VALUES (1,?,?,?,?,?,?)",
            ("worker-1", "super-secret-token", "client-1", "paused", EVENT_ISO, EVENT_ISO),
        )
        connection.execute(
            "INSERT INTO local_logs VALUES (?,?,?,?,?,?,?,?)",
            (
                "log-1", EVENT_ISO, "INFO", "c2_message_read_timing", "task-1", None,
                "消息读取完成",
                json.dumps(
                    {
                        "conversation_id": "conversation-1",
                        "batch_id": "batch-1",
                        "reply_action_id": "reply-1",
                        "trace_id": "trace-1",
                        "sidecar_run_id": "sidecar-1",
                        "timing_ms": 321,
                        "ocr_call_count": 2,
                        "content": "客户原文 13800138000",
                        "worker_token": "leaked-token",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO c2_message_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "conversation-1", "source-1", "read-1", "dedupe-1", "voice",
                "completed", "confirmed", json.dumps({"transcript": "客户语音"}),
                EVENT_ISO, EVENT_ISO,
            ),
        )
        connection.execute(
            "INSERT INTO c2_action_journal VALUES (?,?,?,?,?,?,?)",
            ("flow-1", "conversation-1", "source-1", "read-1", "{}", EVENT_ISO, EVENT_ISO),
        )
        connection.execute(
            "INSERT INTO c2_ingest_outbox VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "outbox-1", "conversation-1", "revision-secret", "read-1",
                json.dumps({"content": "不应外泄"}), "confirmed", 1, EVENT_ISO, EVENT_ISO,
            ),
        )
        connection.execute(
            "INSERT INTO reply_send_ack_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "reply-1", "task-1", "send-secret", "confirmed", "confirmed", "hash",
                json.dumps({"reply_text": "AI 回复"}), 1, EVENT_ISO, EVENT_ISO,
            ),
        )
        connection.execute(
            "INSERT INTO c2_runtime_state VALUES (?,?,?)",
            ("last_read", json.dumps({"conversation_id": "conversation-1"}), EVENT_ISO),
        )
        connection.commit()

    artifact_dir = app_dir / "artifacts" / "sidecar-1"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "result.json"
    artifact.write_text(
        json.dumps(
            {
                "sidecar_run_id": "sidecar-1",
                "step_events": [{"step": "ocr", "timing_ms": 120}],
                "ocr_items": [{"text": "客户原文"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    screenshot = artifact_dir / "chat.png"
    screenshot.write_bytes(b"raw screenshot")
    _touch_in_window(artifact)
    _touch_in_window(screenshot)

    incident_dir = app_dir / "incidents"
    incident_dir.mkdir()
    incident = incident_dir / "INC-UNKNOWN.zip"
    with zipfile.ZipFile(incident, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"error_code": "SEND_RESULT_UNKNOWN", "task_id": "task-1"}),
        )
        archive.writestr("evidence/chat.png", b"raw screenshot")
    _touch_in_window(incident)

    output = tmp_path / "evidence.zip"
    result = subprocess.run(
        [
            sys.executable, str(COLLECTOR), "--app-dir", str(app_dir),
            "--package-dir", str(package_dir), "--from-iso", FROM_ISO,
            "--to-iso", TO_ISO, "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        combined = b"\n".join(archive.read(name) for name in names).decode(
            "utf-8", errors="replace"
        )
        manifest = json.loads(archive.read("manifest.json"))
        related_ids = json.loads(archive.read("ids/related_ids.json"))

    assert not any(name.endswith((".sqlite3", ".png", ".env")) for name in names)
    assert "super-secret-token" not in combined
    assert "leaked-token" not in combined
    assert "send-secret" not in combined
    assert "13800138000" not in combined
    assert "客户原文" not in combined
    assert manifest["build"]["worker_version"] == "0.9.34"
    assert manifest["worker"]["worker_id"] == "worker-1"
    assert manifest["worker"]["client_instance_id"] == "client-1"
    assert related_ids["conversation_id"] == ["conversation-1"]
    assert related_ids["task_id"] == ["task-1"]
    assert "state/ledger.json" in names
    assert "state/c2_outbox.json" in names
    assert "state/sent_ack.json" in names
    assert any(name.startswith("incidents/INC-UNKNOWN") for name in names)
