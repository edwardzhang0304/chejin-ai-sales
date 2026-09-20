"""Real Worker ingest/continuation/SQLite/timeline; API and desktop are doubles."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge, identity_checkpoint
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.runtime_process_timeline import RuntimeProcessTimeline
from chejin_worker_client.storage import load_runtime_control, read_logs


def handoff_status(reason="AI_ENGINE_RETRY_EXHAUSTED"):
    # Current get_message_batch_for_worker response fields, not a send permit.
    return {
        "batch_id": "batch-handoff", "conversation_id": "conv-handoff",
        "batch_status": "handoff_created", "processing": False, "terminal": True,
        "decision": "handoff", "error_code": reason, "reply_action": None, "task": None,
        "authorization": {"allowed": False},
        "handoff_event": {
            "id": "handoff-1", "batch_id": "batch-handoff",
            "conversation_id": "conv-handoff", "handoff_reason_code": reason,
            "status": "created", "closed_at": None,
        },
    }


@pytest.mark.parametrize("revoked", [True, False])
@pytest.mark.parametrize("reason", ["AI_ENGINE_RETRY_EXHAUSTED", "CUSTOMER_REQUESTED_HUMAN"])
def test_read_flow_projects_confirmed_handoff_to_ui(harness, revoked, reason):
    class Api(FakeApi):
        ingested = False
        batch_reads = 0

        def post_wechat_messages_ingest(self, *args, **kwargs):
            result = super().post_wechat_messages_ingest(*args, **kwargs)
            self.ingested = True
            # Model the network interval so the real one-second auth throttle
            # checks again after ingest; no Worker business method is replaced.
            time.sleep(1.05)
            return result

        def get_wechat_read_authorization(self, *args, **kwargs):
            if self.ingested and revoked:
                return {"allowed": False, "conversation_id": "conv-handoff"}
            return super().get_wechat_read_authorization(*args, **kwargs)

        def get_wechat_message_batch(self, *args, **kwargs):
            self.batch_reads += 1
            return handoff_status(reason)

    api = Api(None)
    target = WechatReadTarget(
        conversation_id="conv-handoff", rpa_session_key="wx:handoff",
        display_name="CJTEST01", remark_code="CJTEST01",
        read_reason="waiting_user_reply", authorization_revision="revision-handoff",
        raw={"identity_checkpoint": identity_checkpoint()},
    )
    api.read_targets = [target]
    api.message_batch_result = {
        "batch_id": "batch-handoff", "batch_status": "generating",
        "continuation": {"batch_id": "batch-handoff", "token": "continuation-batch-handoff",
                         "authorization_revision": target.authorization_revision,
                         "read_reason": target.read_reason},
    }
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(api, bridge)
    binding = Binding("worker-1", "test-token", "client-1", run_status="running")
    runner.binding = binding
    timeline = RuntimeProcessTimeline()
    runner.on_runtime_process = timeline.apply
    previous_log_ids = {entry["id"] for entry in read_logs(limit=100)}

    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True)

    assert result["ok"] and result["conversation_terminal_state"] == "handoff", result
    assert len(api.message_payloads) == 1 and api.batch_reads == 1
    assert not bridge.sent_replies
    assert not load_runtime_control()["inflight_flow_id"]
    assert runner.binding.run_status == "running"
    assert not any("technical_failed" in e for e in api.inflight_flow_events)
    logs = [entry for entry in read_logs(limit=100) if entry["id"] not in previous_log_ids]
    assert not any(e["event"] == "c2_conversation_flow_failed" for e in logs)
    assert any(e["event"] == "c3_batch_handoff_confirmed" for e in logs) == revoked
    terminal = timeline.customer_model()[-1]
    assert timeline.customer_terminal_state == "handoff"
    if reason == "AI_ENGINE_RETRY_EXHAUSTED":
        assert terminal["title"] == "AI 服务暂时不可用，已转人工"
        assert terminal["description"] == "多次尝试仍未能生成回复，本轮未发送新回复。"
        assert terminal["finalText"] == "请销售手动回复客户。"
    else:
        assert terminal["title"] == "已转人工"
        assert "AI 服务" not in str(terminal)
    output = os.environ.get("CHEJIN_HANDOFF_UI_EVIDENCE")
    if output and revoked and reason == "AI_ENGINE_RETRY_EXHAUSTED":
        Path(output).write_text(json.dumps({"steps": timeline.customer_model(), "result": result},
                                         ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("defect", [
    "network", "wrong_batch", "wrong_conversation", "wrong_handoff_batch",
    "wrong_handoff_conversation", "missing_handoff", "closed_handoff",
    "processing", "not_terminal", "send_reply", "reply_then_handoff",
])
def test_cancelled_wait_never_invents_handoff_or_send(harness, defect):
    status = handoff_status()
    if defect == "wrong_batch": status["batch_id"] = "other-batch"
    elif defect == "wrong_conversation": status["conversation_id"] = "other-conversation"
    elif defect == "wrong_handoff_batch": status["handoff_event"]["batch_id"] = "other-batch"
    elif defect == "wrong_handoff_conversation": status["handoff_event"]["conversation_id"] = "other-conversation"
    elif defect == "missing_handoff": status["handoff_event"] = None
    elif defect == "closed_handoff": status["handoff_event"]["closed_at"] = "2026-09-20T08:46:10Z"
    elif defect == "processing": status["processing"] = True
    elif defect == "not_terminal": status["terminal"] = False
    elif defect in {"send_reply", "reply_then_handoff"}: status["decision"] = defect
    api = FakeApi(None)
    calls = []
    def get_batch(*args):
        calls.append(args)
        if defect == "network": raise ConnectionError("sensitive-response-body-test")
        return copy.deepcopy(status)
    api.get_wechat_message_batch = get_batch
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(api, bridge)
    result = runner._wait_and_send_current_c3_batch(
        binding=Binding("worker-1", "test-token", "client-1", run_status="running"),
        target=WechatReadTarget(conversation_id="conv-handoff", display_name="CJTEST01", rpa_session_key="wx:handoff"),
        batch_id="batch-handoff", cancel_check=lambda: "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
    )
    assert result["ok"] is False and result["error_code"] == "WORKER_INTERRUPTED"
    assert len(calls) == 1 and not bridge.sent_replies
    if defect == "network":
        logs = [e for e in read_logs(limit=100) if e["event"] == "c3_batch_terminal_lookup_failed"]
        assert logs and "ConnectionError" in str(logs)
        assert "sensitive-response-body-test" not in str(logs)


@pytest.mark.parametrize("cancel_reason", [True, "UI_LOCK_LEASE_LOST", "TASK_LEASE_EXPIRED"])
def test_local_stop_or_lease_loss_is_not_reclassified_as_handoff(harness, cancel_reason):
    api = FakeApi(None)
    def unexpected_read(*args):
        pytest.fail("local cancellation must not query or continue the AI batch")
    api.get_wechat_message_batch = unexpected_read
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(api, bridge)
    result = runner._wait_and_send_current_c3_batch(
        binding=Binding("worker-1", "test-token", "client-1", run_status="running"),
        target=WechatReadTarget(conversation_id="conv-handoff", display_name="CJTEST01", rpa_session_key="wx:handoff"),
        batch_id="batch-handoff", cancel_check=lambda: cancel_reason,
    )
    assert not result["ok"] and not bridge.sent_replies
