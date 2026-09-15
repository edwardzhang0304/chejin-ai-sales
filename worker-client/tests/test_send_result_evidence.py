from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest

from chejin_worker_client import config, incident_evidence, rpa_bridge, storage
from chejin_worker_client.transaction_outcomes import classify_action_result


@pytest.fixture
def home(tmp_path, monkeypatch):
    incident_evidence.stop_incident_worker(wait=True)
    settings = replace(config.CONFIG, app_dir=tmp_path)
    monkeypatch.setenv("CHEJIN_WORKER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG", settings)
    monkeypatch.setattr(rpa_bridge, "CONFIG", settings)
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    monkeypatch.setattr(storage, "_post_update_initialized_database", None)
    monkeypatch.setattr(incident_evidence, "INCIDENT_SETTLE_WINDOW_SECONDS", 0)
    yield tmp_path
    incident_evidence.stop_incident_worker(wait=True)


@pytest.mark.parametrize("mode", ["sent", "failed", "unknown"])
def test_public_send_preserves_process_result_and_outcome(home, monkeypatch, mode):
    driver = Path(__file__).parent / "fixtures" / "send_result_process.py"
    bridge = rpa_bridge.RpaBridge(driver)
    bridge.mode = "omniauto"
    monkeypatch.setenv("SEND_RESULT_TEST_MODE", mode)
    monkeypatch.setattr(bridge, "_sidecar_command", lambda args: [sys.executable, str(driver), *args])

    result = bridge.send_reply(
        target="CJTEST01", rpa_session_key="", text="发送结构回归测试",
        task_id="task-send-result", current_only=True,
    )

    artifact = next((home / "artifacts" / "tasks" / "task-send-result" / "chat_reply").iterdir())
    original = json.loads((artifact / "boundary-result.json").read_text())
    assert {key: result[key] for key in original} == original
    if mode != "sent":
        assert result["returncode"] == 1
        assert json.loads(result["stdout_tail"]) == original
    calls = [json.loads(line) for line in (artifact / "boundary-calls.jsonl").read_text().splitlines()]
    assert len(calls) == 1  # Evidence recording must never repeat a send.
    assert calls[0]["pid"] != os.getpid()
    assert bridge.active_artifact_dirs() == set()
    assert not bridge.sidecar_active()
    outcome = classify_action_result("send", result)
    assert outcome["result"] == mode
    assert outcome["action_phase"] == {
        "sent": "confirmed", "failed": "not_attempted", "unknown": "trigger_attempted",
    }[mode]
    failures = [row for row in storage.read_logs(limit=100) if row["event"] == "rpa_action_failed"]
    if mode == "sent":
        assert outcome["business_result_confirmed"] is True
        assert failures == []
    else:
        assert len(failures) == 1
        assert failures[0]["metadata"]["result"] == result
        package = incident_evidence.wait_for_incident(failures[0]["metadata"]["incident_id"], timeout=5)
        assert package


@pytest.mark.parametrize("nested,failed", [
    ({"ok": True, "confirmed": True, "result": "sent"}, False),
    ({"ok": False, "result": "failed"}, True),
    ({"ok": False, "result": "unknown"}, True),
    ({"ok": False}, True),
    ("sent", False), ("failed", True), ("unknown", True),
    (None, False), ({}, False), ([], False),
    ({"result": {}}, False), ({"result": []}, False),
])
def test_evidence_check_accepts_nested_and_legacy_results_without_mutation(
    home, monkeypatch, nested, failed,
):
    bridge = rpa_bridge.RpaBridge(Path(__file__))
    payload = {"ok": True, "send_result": nested}
    before = json.dumps(payload, sort_keys=True)
    monkeypatch.setattr(bridge, "_call_omniauto_process", lambda *_args, **_kwargs: payload)
    assert bridge._call_omniauto(["send"]) is payload
    assert json.dumps(payload, sort_keys=True) == before
    failures = [row for row in storage.read_logs(limit=100) if row["event"] == "rpa_action_failed"]
    assert len(failures) == int(failed)
    # Malformed inputs are passed on for the existing business validator;
    # diagnostics must neither bless them nor throw a new exception.


def test_task_runner_send_result_reaches_normal_ack_without_recovery(home, monkeypatch):
    # Reuse external API/WeChat observations from the existing runner fixture.
    # The modified send path is real, including Popen and the async evidence
    # boundary; do not replace classification, receipt storage or settlement.
    import test_task_runner as runner_fixture
    from chejin_worker_client.models import Binding, RpaResult

    case = runner_fixture.TaskRunnerTest()
    task = case.make_chat_reply_task(task_id="task-real-send-bridge")
    api = runner_fixture.FakeApi(task)
    case.authorize_chat_reply_target(api)
    api.message_ingest_result = "duplicated"
    driver = Path(__file__).parent / "fixtures" / "send_result_process.py"
    send_bridge = rpa_bridge.RpaBridge(driver)
    send_bridge.mode = "omniauto"
    monkeypatch.setenv("SEND_RESULT_TEST_MODE", "sent")
    monkeypatch.setattr(send_bridge, "_sidecar_command", lambda args: [sys.executable, str(driver), *args])
    bridge = runner_fixture.FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
    bridge.send_reply = send_bridge.send_reply
    bridge.send_transaction_journal_path = send_bridge.send_transaction_journal_path
    runner, seen = case.make_runner(api, bridge)
    runner.binding = Binding(
        worker_id="worker-1", worker_token="test-only-token",
        client_instance_id="client-1", run_status="running",
    )

    runner.tick_once()

    assert "sent_ack:sent:None" in api.events
    ack = storage.load_reply_send_ack_outbox("reply-action-1")
    assert ack["status"] == "confirmed"
    assert "恢复" not in ack["ack_payload"]["remark"]
    assert seen["errors"] == []
    assert runner.binding.run_status == "running"
    logs = storage.read_logs(limit=200)
    assert not any(row["event"] in {
        "c2_message_read_failed", "rpa_action_failed", "inflight_flow_finish_failed",
    } for row in logs)
    calls = list((home / "artifacts" / "tasks" / task.id).rglob("boundary-calls.jsonl"))
    assert len(calls) == 1
    assert len(calls[0].read_text().splitlines()) == 1
