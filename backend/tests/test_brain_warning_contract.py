"""Synthetic Brain/Guard -> adapter -> C3 tests; no production data/WeChat."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from sqlalchemy import select

import test_c3_api as api
from app.core.config import get_settings
from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction
from app.models.task import Task
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_brain_fact_guidance_regression import GUIDANCE, RUNTIME

ANSWER = "哈哈，聊得挺开心。您平时开车主要是上下班，还是周末出游？"


def warning_result():
    return {"rule_name": "customer_service_brain_reply", "applied": True,
            "adoptable": True, "visible_reply_source": "brain_plan.reply_segments",
            "reply_text": ANSWER, "guard_verdict": "warn",
            "guard": {"allowed": True, "action": "send_reply", "guard_verdict": "warn"},
            "brain_plan": {"recommended_action": "send_reply", "reply_segments": [ANSWER],
                           "confidence": .95, "risk_flags": [], "evidence_refs": []}}


@pytest.mark.parametrize("change,accepted", [
    ({}, True),
    ({"guard": {"allowed": True, "action": "send_reply", "severity": "warn"}}, True),
    ({"guard": {}}, False),
    ({"guard": {"allowed": False, "action": "send_reply", "guard_verdict": "warn"}}, False),
    ({"guard": {"allowed": "true", "action": "send_reply", "guard_verdict": "warn"}}, False),
    ({"guard": {"allowed": True, "action": "repair", "guard_verdict": "warn"}}, False),
    ({"guard": {"allowed": True, "action": "handoff", "guard_verdict": "warn"}}, False),
    ({"guard": {"allowed": True, "action": "send_reply", "guard_verdict": "block"}}, False),
    ({"applied": False}, False),
    ({"adoptable": False}, False),
    ({"adoptable": "true"}, False),
    ({"visible_reply_source": "guard_handoff_ack"}, False),
    ({"reply_text": ""}, False),
])
def test_only_explicit_warning_approval_maps_to_pass(monkeypatch, change, accepted):
    adapter = RealOmniAutoAIEngineAdapter()
    result = warning_result()
    result.update(change)
    original = deepcopy(result)
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "SYNTHETIC"}})
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", lambda **_: result)
    decision = api._generate_adapter_decision(adapter,
        conversation_context={"conversation_id": "synthetic-warning", "remark_code": "CJWARN01"},
        message_batch={"id": "synthetic-batch", "messages": [{"content": "今天聊得开心"}]})
    assert (decision.decision == "send_reply" and decision.guard_result == "pass") is accepted
    assert decision.raw_payload["omniauto_brain_result"] == original


@pytest.mark.parametrize("retry_after_incomplete", [False, True])
def test_actual_brain_warning_creates_one_reply_without_handoff(monkeypatch, retry_after_incomplete):
    api.setup_function()
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(RUNTIME))
    monkeypatch.setenv("C3_AI_ADAPTER_MODE", "real")
    monkeypatch.setenv("C3_BATCH_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "SYNTHETIC-LOCAL-ONLY")
    get_settings.cache_clear()
    calls = []
    complete = [not retry_after_incomplete]
    plan = {"can_answer": True, "answer_mode": "soft_redirect_to_business",
            "evidence_used": {}, "facts_claimed": [], "reply_segments": [ANSWER],
            "risk": {"risk_level": "low", "risk_tags": ["off_topic"], "needs_handoff": False},
            "recommended_action": "send_reply", "confidence": .95}
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request["model"])
            response = ({"verdict": "pass", "confidence": 1., "semantic_errors": [],
                         "hard_boundary_concerns": [], "customer_visible_risk": "low",
                         "repair_instruction": "", "reason": "Synthetic review"}
                        if request["model"] == "warning-reviewer" else plan)
            content = json.dumps(response, ensure_ascii=False)
            finish = "stop"
            if request["model"] == "warning-author" and not complete[0]:
                content, finish = content[:20], "length"
            body = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    config = {"customer_service_brain": {
        "enabled": True, "mode": "brain_first", "provider": "openai_compatible",
        "model": "warning-author", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "min_confidence": .2, "require_evidence": True, "require_fact_claims": True,
        "semantic_reviewer_enabled": True, "semantic_reviewer_force": True,
        "semantic_reviewer_cache_enabled": False, "semantic_reviewer_model": "warning-reviewer",
        "fallback_to_legacy_on_error": False, "require_final_visible_polish": False},
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False}}
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    try:
        for title, text in GUIDANCE:
            api._publish_managed_knowledge(title, text)
        worker, binding = api._setup_bound_conversation()
        event = api._ingest(worker, binding["conversation_id"], "warning-fixture", "今天聊得很开心")
        batch = api._collect(binding["conversation_id"], event)
        if retry_after_incomplete:
            with api.SessionLocal() as db:
                first = db.get(MessageBatch, batch["batch_id"])
                assert first.decision == "retry_later"
                assert first.generation_attempt_count == 1
                assert not list(db.scalars(select(ReplyAction)))
            complete[0] = True
            with api.SessionLocal() as db:
                claim = api.c3_service.claim_message_batch_generation(db, batch_id=batch["batch_id"], stale_only=True)
                assert claim["run"] and claim["attempt"] == 2
                api.c3_service.generate_for_batch(db, batch_id=batch["batch_id"], expected_generation_attempt=2)
                db.commit()
        with api.SessionLocal() as db:
            saved = db.get(MessageBatch, batch["batch_id"])
            assert saved.decision == "send_reply", saved.ai_response_snapshot
            result = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
            assert result["guard_verdict"] == "warn", result
            assert result["guard"]["allowed"] is True
            assert result["guard"]["action"] == "send_reply"
            assert saved.generation_attempt_count == 1 + int(retry_after_incomplete)
            actions = list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id == saved.id)))
            assert len(actions) == 1 and actions[0].reply_text == ANSWER
            assert len(list(db.scalars(select(Task).where(Task.reply_action_id == actions[0].id)))) == 1
            assert not list(db.scalars(select(HandoffEvent).where(HandoffEvent.batch_id == saved.id)))
            repeated = api._generate(batch["batch_id"])
            assert repeated["reply_action_id"] == actions[0].id
        assert "warning-author" in calls and "warning-reviewer" in calls
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        get_settings.cache_clear()
