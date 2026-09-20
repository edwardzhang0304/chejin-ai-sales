"""HC admission through real Worker/SQLite; screenshots and HTTP are controlled.

These short-model vectors exercise retry routing, not OCR accuracy or live sends.
The independent unchanged eight-case review is also rerun outside this file.
"""
from copy import deepcopy
import hashlib
import os
import subprocess
import sys

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import (
    FakeApi, FakeBridge, identity_checkpoint_for_facts,
    pre_send_fact, pre_send_fact_checkpoint_response,
)
from chejin_worker_client import storage
from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.pre_send_checkpoint import canonical_sha256
from chejin_worker_client.shared_rules import text_correspondence
from chejin_worker_client.text_recheck import differing_text_observation_ids


def setup_read(harness, *, scrolled=False, corrected_at="capture", phase="authorized_read"):
    visible = ["您好，我想了解一下车型", "600 Pro"]
    history = (["上周看过一台车", "那台车已经卖了"] if scrolled else []) + visible
    checkpoint = identity_checkpoint_for_facts("hc-recheck", [{"content": t} for t in history])
    checkpoint.update(conversation_id="hc-recheck", text_correspondence_context={"version": 1, "known_entities": []},
        historical_match_policy=c2_contract_v3()["text_correspondence_contract"]["historical_match_policy"])
    for entry, text in zip(checkpoint["recent_messages"], history):
        entry["effective_text"] = {"text": text, "version": 0, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    checkpoint["checkpoint_digest"] = text_correspondence.checkpoint_digest(checkpoint)
    target = WechatReadTarget(conversation_id="hc-recheck", display_name="CJTEST01", remark_code="CJTEST01",
        rpa_session_key="", authorization_revision="hc-recheck-auth", unread_generation=1,
        raw={"identity_checkpoint": checkpoint})

    class Bridge(FakeBridge):
        captures = 0
        stages = None

        def frame(self, texts):
            return self._contractual_message_payload({"ok": True, "messages": [
                {"id": f"frame-{self.captures}-{i}", "sender_role": "customer", "type": "text", "content": text}
                for i, text in enumerate(texts)], "frame_observation": {"frame_id": f"frame-{self.captures}"},
                "sidecar_run_id": f"frame-{self.captures}", "tail_complete": True})

        def get_messages(self, **kwargs):
            self.captures += 1
            right = corrected_at == "first" or (corrected_at == "capture" and self.captures > 1)
            texts = visible[:-1] + ["600 Pro" if right else "600 Pr0", "有没有现车？"]
            self.get_messages_payloads = [self.frame(texts)]
            return super().get_messages(**kwargs)

        def recheck_text_bubbles(self, **kwargs):
            self.stages.append(kwargs["stage"])
            assert kwargs["observation_ids"] == [f"frame-{self.captures}-1"]
            if kwargs["stage"] == "validate":
                return {"ok": True}
            assert kwargs["stage"] == "ocr" and self.captures == 2
            return self.frame(visible[:-1] + ["600 Pro" if corrected_at == "ocr" else "600 Pr0", "有没有现车？"])

    api, bridge = FakeApi(None), Bridge(RpaResult(ok=True, result_code="unused"))
    bridge.stages = []
    runner, _ = harness.make_runner(api, bridge)
    binding = Binding("worker-test", "test-token", "instance-test", run_status="running")
    runner.binding = binding
    storage.save_binding(binding)
    api.read_targets = [target]
    if phase == "pre_send_refresh":
        response = pre_send_fact_checkpoint_response(conversation_id=target.conversation_id,
            batch_id="hc-batch", reply_action_id="hc-action", facts=[pre_send_fact(
                e["stable_id"], sender_role="customer", message_type="text", content=t)
                for e, t in zip(checkpoint["recent_messages"], history)])
        frozen = response["pre_send_fact_checkpoint"]
        for fact, entry in zip(frozen["committed_tail"], checkpoint["recent_messages"]):
            fact.update(deepcopy(entry), commit_basis=entry["message_identity_commit_record"]["commit_basis"])
            storage.save_c2_ledger_terminal(conversation_id=target.conversation_id,
                source_message_key=entry["source_message_key"], origin_read_run_id="historical-read",
                dedupe_key=None, message_type="text", terminal_state="completed", ingest_state="confirmed")
        response["pre_send_fact_checkpoint_binding"]["checkpoint_digest"] = canonical_sha256(frozen)
        target.raw["pre_send_fact_checkpoint_context"] = {"schema_version": 1, "checkpoint": frozen,
            "binding": response["pre_send_fact_checkpoint_binding"]}
    return runner, binding, target, api, bridge


@pytest.mark.parametrize("phase", ["authorized_read", "pre_send_refresh", "reply_sequence_read"])
@pytest.mark.parametrize("scrolled", [False, True])
@pytest.mark.parametrize("corrected_at", ["first", "capture", "ocr", "never"])
def test_one_anchor_recheck_in_real_read_entrances(harness, phase, scrolled, corrected_at):
    runner, binding, target, api, bridge = setup_read(harness, phase=phase, scrolled=scrolled, corrected_at=corrected_at)
    previous_reads = {r["metadata"]["read_run_id"] for r in storage.read_logs(limit=200)
                      if r["event"] == "c2_text_recheck_completed"}
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=False,
        current_step="pre_send_refresh" if phase == "pre_send_refresh" else "message_read", operation_phase=phase,
        current_only=phase != "authorized_read")
    assert result["ok"] is (corrected_at != "never"), result
    assert bridge.captures == (1 if corrected_at == "first" else 2)
    expected_stages = [] if corrected_at == "first" else ["validate"]
    if corrected_at in {"ocr", "never"}:
        expected_stages.append("ocr")
    assert bridge.stages == expected_stages
    assert not bridge.sent_replies
    assert not storage.load_runtime_control()["inflight_flow_id"]
    assert runner.current_ui_lock is None
    if corrected_at != "never":
        assert binding.run_status == "running"
        assert result["new_customer_message_count"] == 1
        sent = [m for p in api.message_payloads for m in p["messages"]]
        assert [m["content"] for m in sent] == ["有没有现车？"]
    else:
        assert all(not p["messages"] for p in api.message_payloads)
    records = [r["metadata"] for r in storage.read_logs(limit=200) if r["event"] == "c2_text_recheck_completed"
               and r["metadata"]["read_run_id"] not in previous_reads]
    assert len(records) == (0 if corrected_at == "first" else 1)
    if records:
        evidence = records[0]["text_recheck_evidence"]
        assert evidence["consumed"] and not evidence.get("exception_type"), evidence
        assert bool(evidence.get("adopted")) is (corrected_at != "never")
        assert storage.claim_read_recheck(records[0]["read_run_id"], kind="other_entry") is False


@pytest.mark.parametrize("damage", ["no_anchor", "duplicate_anchor", "role", "media", "native_id"])
def test_hc_admission_does_not_fall_back_to_legacy_matching(harness, damage):
    runner, _, target, _, bridge = setup_read(harness)
    frame = bridge.get_messages(display_name=target.remark_code, rpa_session_key="")
    rows = frame["observations"]
    if damage == "no_anchor":
        rows[0]["content_clean"] = "完全不同的话"
    elif damage == "duplicate_anchor":
        repeated = deepcopy(rows[0])
        repeated.update(observation_id="repeated", bubble_rect=[100, 800, 450, 850])
        rows.append(repeated)
    elif damage == "role":
        rows[1]["sender_role"] = "self"
    elif damage == "media":
        rows[1].update(message_type="voice", row_kind="voice_transcript", voice_state="transcribed")
    else:
        rows[1]["native_source_message_id"] = "another-message"
    compared, errors = runner._align_initial_identity_frame(target=target, sidecar_payload=frame, read_run_id="blocked")
    assert errors
    report = compared["historical_match_diagnostics"]
    assert report["candidates"] == [] and report["accepted"] is False
    frozen = deepcopy(compared)
    assert differing_text_observation_ids(compared["_text_recheck_old_projection"], compared) == []
    assert compared == frozen


def recheck(runner, binding, target, frame, read_id):
    def compare(value):
        return runner._align_initial_identity_frame(target=target, sidecar_payload=value, read_run_id=read_id)
    payload, errors = compare(frame)
    assert errors and payload["historical_match_diagnostics"]["best_score"] == 8333
    assert len(payload["historical_match_diagnostics"]["candidates"]) == 1
    return runner._recheck_text_alignment_once(binding=binding, target=target, read_run_id=read_id,
        payload=payload, decision=errors, compare=compare, succeeded=lambda value: not value,
        cancel_check=lambda: False, enforce_read_targets=True)


def assert_spent_in_restarted_process():
    from test_task_runner import TaskRunnerTest
    # Construct a new runner without calling setUp (which clears test state).
    runner, binding, target, _, bridge = setup_read(TaskRunnerTest())
    state = deepcopy(storage.load_c2_state("read_recheck:durable-read"))
    assert state and state["consumed"]
    frame = bridge.get_messages(display_name=target.remark_code, rpa_session_key="")
    _, errors = recheck(runner, binding, target, frame, "durable-read")
    assert errors and bridge.captures == 1 and bridge.stages == []
    assert storage.load_c2_state("read_recheck:durable-read") == state
    assert storage.claim_read_recheck("durable-read", kind="after_restart") is False


@pytest.mark.parametrize("spent_by", ["complete_text_bubble", "pre_send_context", "media_context"])
def test_same_sqlite_restart_cannot_renew_recheck(harness, spent_by):
    runner, binding, target, _, bridge = setup_read(harness, corrected_at="never")
    frame = bridge.get_messages(display_name=target.remark_code, rpa_session_key="")
    if spent_by != "complete_text_bubble":
        assert storage.claim_read_recheck("durable-read", kind=spent_by)
    _, errors = recheck(runner, binding, target, frame, "durable-read")
    assert errors
    assert bridge.captures == (2 if spent_by == "complete_text_bubble" else 1)
    assert bridge.stages == (["validate", "ocr"] if spent_by == "complete_text_bubble" else [])
    state = deepcopy(storage.load_c2_state("read_recheck:durable-read"))
    child = subprocess.run([sys.executable, "-c", "from test_historical_confidence_recheck import "
        "assert_spent_in_restarted_process; assert_spent_in_restarted_process()"],
        env={**os.environ, "CHEJIN_WORKER_HOME": str(storage.APP_DIR)}, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert storage.load_c2_state("read_recheck:durable-read") == state


@pytest.mark.parametrize("damage", ["observation_validation_errors", "flow_gate_errors", "ui_frame_invalidated",
                                   "history_gap", "different_frame_ids"])
def test_failed_frame_cannot_reuse_hc_admission(harness, damage):
    runner, _, target, _, bridge = setup_read(harness)
    frame = bridge.get_messages(display_name=target.remark_code, rpa_session_key="")
    compared, errors = runner._align_initial_identity_frame(target=target, sidecar_payload=frame, read_run_id="guarded")
    old = compared["_text_recheck_old_projection"]
    assert errors and differing_text_observation_ids(old, compared) == ["frame-1-1"]
    if damage == "different_frame_ids":
        compared["observations"][1]["observation_id"] = "another-frame"
    else:
        compared[damage] = True
    assert differing_text_observation_ids(old, compared) == []


def test_two_competing_hc_boundaries_do_not_choose_an_ocr_region_arbitrarily():
    from apps.wechat_ai_customer_service.tests.test_historical_confidence_alignment import scenario, build, frame
    from chejin_worker_client.message_viewport_projection import normalized_business_message_sequence
    x, y, z = (letter * 5 + "a" * 95 for letter in "xyz")
    a, b = (letter * 9 + "q" * 91 for letter in "bc")
    checkpoint, rows = scenario([x, a, y, b], [y, a, z, b, "新的客户问题"])
    old_rows = frame([x, a, y, b])
    for sequence in (old_rows, rows):
        for index, row in enumerate(sequence):
            row["sender_role"] = "customer" if index % 2 == 0 else "self"
    old = normalized_business_message_sequence(old_rows, message_viewport_bounds=None)
    for index, entry in enumerate(checkpoint["recent_messages"]):
        entry.update(sender_role=old_rows[index]["sender_role"], business_projection=old[index])
    checkpoint["checkpoint_digest"] = text_correspondence.checkpoint_digest(checkpoint)
    report = {}
    assert build(checkpoint, rows, diagnostics=report) is None
    assert len(report["candidates"]) == 2 and report["margin"] == 400
    payload = {"ok": True, "observations": rows, "frame_observation": {"frame_id": "current"},
               "historical_match_diagnostics": report}
    assert differing_text_observation_ids(old, payload) == []
