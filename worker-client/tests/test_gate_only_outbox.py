"""Failure reports do not borrow or settle earlier facts in the same read Flow."""
import copy

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client import storage
from chejin_worker_client.wechat_c2 import build_flow_gate_ingest_payload


def setup_gate(harness, ledger_state="confirmed"):
    api = FakeApi(None)
    api.message_ingest_read_completion = {
        "result": "technical_failed", "error_code": "C2_UNREAD_RESULT_REPEATEDLY_INCONCLUSIVE",
    }
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(api, bridge)
    binding = Binding("worker-test", "test-token", "instance-test", run_status="faulted")
    runner.binding = binding
    storage.save_binding(binding)
    runner._backend_pending_read_recovery = {"terminal_settlement_protocol_version": 1}
    target = WechatReadTarget(
        conversation_id="conv-gate", display_name="CJTEST01", remark_code="CJTEST01",
        rpa_session_key="test-session", authorization_revision="revision-conv-gate",
    )
    storage.save_c2_ledger_terminal(
        conversation_id=target.conversation_id, source_message_key="old-fact",
        origin_read_run_id="same-flow", dedupe_key="old-fact", message_type="text",
        terminal_state="completed", ingest_state=ledger_state, result={"content": "原事实"},
    )
    payload = build_flow_gate_ingest_payload(
        target, read_run_id="same-flow", error_code="MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
    )
    return runner, api, bridge, binding, payload


def ledger_rows():
    with storage.db_connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM c2_message_ledger")]


@pytest.mark.parametrize("ledger_state", ["confirmed", "waiting"])
@pytest.mark.parametrize("old_paused", [False, True])
def test_gate_with_existing_ledger_delivers_without_rewriting_facts(harness, monkeypatch, ledger_state, old_paused):
    runner, api, bridge, binding, payload = setup_gate(harness, ledger_state)
    original = copy.deepcopy(payload)
    before = ledger_rows()
    outbox = storage.enqueue_c2_outbox(payload)
    if old_paused:
        storage.mark_c2_outbox_capability_paused(outbox, "C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
        # Advance only the scheduler clock; never mark a row settled by hand.
        due = storage.load_c2_outbox_entry(outbox)["next_attempt_at"]
        monkeypatch.setattr(storage, "utc_now_iso", lambda: due)
    assert runner._replay_c2_outbox(binding)
    assert storage.load_c2_outbox_entry(outbox)["status"] == "confirmed"
    assert storage.load_c2_outbox_entry(outbox)["payload"] == original
    assert ledger_rows() == before
    assert len(api.message_payloads) == 1
    assert not bridge.message_reads and not bridge.sent_replies
    assert binding.run_status == "faulted"


@pytest.mark.parametrize("mode", ["not_received", "response_lost"])
def test_gate_replays_same_durable_payload_after_transport_failure(harness, monkeypatch, mode):
    runner, api, bridge, binding, payload = setup_gate(harness)
    outbox = storage.enqueue_c2_outbox(payload)
    before = ledger_rows()
    original_post = api.post_wechat_messages_ingest
    def unavailable(*args, **kwargs):
        if mode == "response_lost":
            original_post(*args, **kwargs)
        raise ConnectionError("controlled transport failure")
    api.post_wechat_messages_ingest = unavailable
    assert not runner._replay_c2_outbox(binding)
    assert storage.load_c2_outbox_entry(outbox)["status"] != "confirmed"
    due = storage.load_c2_outbox_entry(outbox)["next_attempt_at"]
    monkeypatch.setattr(storage, "utc_now_iso", lambda: due)
    api.post_wechat_messages_ingest = original_post
    # A new runner uses the same SQLite, without recreating its rows.
    restarted, _ = harness.make_runner(api, bridge)
    restarted.binding = binding
    restarted._backend_pending_read_recovery = runner._backend_pending_read_recovery
    assert restarted._replay_c2_outbox(binding)
    assert storage.load_c2_outbox_entry(outbox)["status"] == "confirmed"
    assert all(item == payload for item in api.message_payloads)
    assert ledger_rows() == before
    assert not bridge.message_reads and not bridge.sent_replies


@pytest.mark.parametrize("defect", ["message", "slot", "observation", "missing_gate", "bad_gate", "failed_voice", "bad_alignment"])
def test_a_gate_label_cannot_bypass_fact_evidence(harness, defect):
    runner, api, _, binding, payload = setup_gate(harness)
    if defect == "message":
        payload["messages"] = [{"source_message_key": "unproven-fact"}]
    elif defect == "slot":
        payload["evidence"]["slot_ledger_states"] = [{"source_message_key": "unproven-fact"}]
    elif defect == "observation":
        payload["evidence"]["observations"] = [{"observation_id": "unproven-row"}]
    elif defect == "missing_gate":
        payload["evidence"].pop("flow_gate_errors")
    elif defect == "bad_gate":
        payload["evidence"]["flow_gate_errors"] = [""]
    elif defect == "failed_voice":
        payload["evidence"]["failed_voice_source_keys"] = ["old-fact"]
    else:
        payload["evidence"]["sequence_alignment_evidence"] = {}
    outbox = storage.enqueue_c2_outbox(payload)
    before = ledger_rows()
    assert runner._prepare_persisted_c2_outbox(outbox_id=outbox, payload=payload) is None
    assert storage.load_c2_outbox_entry(outbox)["status"] == "capability_paused"
    assert ledger_rows() == before and api.message_payloads == []


@pytest.mark.parametrize("confirmed,pending,expected", [
    ("faulted", None, None), ("paused", None, "faulted"),
    (None, None, "faulted"), ("faulted", "running", "faulted"),
    ("faulted", "faulted", "faulted"),
])
def test_repeat_failure_keeps_backend_stop_confirmation(harness, confirmed, pending, expected):
    runner, _, _, binding, _ = setup_gate(harness)
    runner._backend_confirmed_run_status = confirmed
    runner._pending_run_status_sync = pending
    for _ in range(3):
        runner._pause_for_permanent_outbox_contract_error(binding)
    assert runner._pending_run_status_sync == expected
    assert storage.load_binding().run_status == "faulted"
    assert storage.load_runtime_control()["pause_requested"] is True


def test_fault_message_names_local_blocker_after_backend_confirmed_stop(harness):
    runner, api, _, binding, payload = setup_gate(harness)
    api.task_lease_fencing_tokens = {}
    outbox = storage.enqueue_c2_outbox(payload)
    storage.mark_c2_outbox_capability_paused(outbox, "C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    runner._backend_confirmed_run_status = "faulted"
    runner._pending_run_status_sync = None
    runner._pause_for_permanent_outbox_contract_error(binding)
    state = runner._check_fault_recovery()
    assert state["ready"] is False
    assert state["reason"] == "暂不能恢复：旧记录尚未通过身份或结果校验，原数据已保留。"
