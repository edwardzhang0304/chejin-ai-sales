"""Real C3/DB/Brain/Guard with a controlled HTTP LLM and simulated send acknowledgement.

Candidates and history are synthetic. No assertion claims native Windows sending
or a real model's semantic judgement; those require separate acceptance evidence.
"""
from copy import deepcopy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest
from sqlalchemy import select

import test_c3_api as api
from app.core.config import get_settings
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "worker-client/omniauto-rpa"
WARNING = "social_context_review:over_eager_business_redirect_after_social_fatigue"
BUSINESS_REPLY = "手动挡可以的，您预算多少？"
PUSHY_REPLY = "先不聊别的了，咱们还是回到预算和车源，您想看轿车还是SUV？"
RESPECTFUL_REPLY = "可以，先放松聊两句也没事。您想聊什么我听着。"

def seed_history(binding_payload, history):
    """Confirmed old facts only; never seed the action or its terminal outcome."""
    base = api.utcnow() - timedelta(minutes=5)
    with api.SessionLocal() as db:
        binding = db.get(WechatSessionBinding, binding_payload["id"])
        for i, content in enumerate(history):
            db.add(MessageEvent(
                id=f"intent-history-{i}", conversation_id=binding.conversation_id,
                binding_id=binding.id, lead_id=binding.lead_id, sales_id=binding.sales_id,
                worker_id=binding.worker_id, rpa_session_key=binding.rpa_session_key,
                read_run_id=f"intent-history-{i}", contract_version=3,
                source_message_key=f"intent-history-{i}", dedupe_key=f"intent-history-{i}",
                sender_role="customer", message_type="text", content=content,
                item_state="confirmed", raw_payload={"item_state": "confirmed"}, evidence={},
                occurred_at=base + timedelta(seconds=i), observed_at=base + timedelta(seconds=i),
                observation_order=i + 1,
            ))
        db.commit()

def simulated_send_ack(worker, binding, decision, expected_reply):
    """Exercise the public send contract. The ack is explicitly a desktop substitute."""
    task_id, action_id = decision["task_id"], decision["reply_action_id"]
    claimed = api.client.post(f"/api/tasks/{task_id}/claim", json={
        "worker_id": worker["id"], "current_step": "chat_reply_claimed",
        "claim_source": "c2_conversation_flow", "conversation_id": binding["conversation_id"],
    }, headers=api._worker_headers(worker))
    assert claimed.status_code == 200, claimed.text
    headers = api._task_lease_headers(worker, claimed)
    send = api.client.post(f"/api/reply-actions/{action_id}/claim-send",
                          json={"task_id": task_id, "worker_id": worker["id"]}, headers=headers)
    assert send.status_code == 200, send.text
    payload = send.json()["data"]
    assert payload["reply_text"] == expected_reply
    ack_body = {
        "send_token": payload["send_token"], "task_id": task_id, "worker_id": worker["id"],
        "client_instance_id": "client-c3", "send_result": "sent", "action_phase": "confirmed",
        "reply_text_hash": payload["reply_text_hash"], "sidecar_run_id": "simulated-intent-send",
    }
    for _ in range(2):  # Retry the receipt: it must not create a second send or reply count.
        ack = api.client.post(f"/api/reply-actions/{action_id}/sent-ack", json=ack_body, headers=headers)
        assert ack.status_code == 200, ack.text
    with api.SessionLocal() as db:
        assert db.get(ReplyAction, action_id).status == "sent"
        assert db.get(Task, task_id).status == "completed"
        assert len(list(db.scalars(select(SentAck)))) == 1
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_user_reply"
        assert conversation.reply_count == 1
        assert not list(db.scalars(select(HandoffEvent)))

@pytest.mark.parametrize("scenario", ["greeting_then_need", "stale_fatigue", "explicit_refusal", "unsupported_price"])
def test_intent_reaches_send_contract_without_false_handoff(monkeypatch, tmp_path, scenario):
    api.setup_function()
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(RUNTIME))
    monkeypatch.setenv("C3_AI_ADAPTER_MODE", "real")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "FAKE-INTENT-REGRESSION")
    get_settings.cache_clear()
    requests = []
    refusal = scenario == "explicit_refusal"
    unsafe = scenario == "unsupported_price"
    question = "我就随便聊两句，你别老聊车" if refusal else "我想买个手动挡"
    expected_reply = RESPECTFUL_REPLY if refusal else BUSINESS_REPLY

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            review = payload["model"] == "intent-reviewer"
            brain_calls = sum(r["model"] == "intent-brain" for r in requests)
            if review:
                repair = refusal and brain_calls == 1
                result = {
                    "verdict": "repair" if repair else "pass", "confidence": .95,
                    "customer_visible_risk": "low", "hard_boundary_concerns": [],
                    "semantic_errors": ["ignores_current_refusal"] if repair else [],
                    "repair_instruction": "尊重客户本轮拒绝推销，停止追问预算。" if repair else "",
                    "reason": "Controlled reviewer response; not real LLM acceptance",
                }
            else:
                reply = ("这台售价5万。" if unsafe else
                         PUSHY_REPLY if refusal and brain_calls == 1 else expected_reply)
                result = {
                    "can_answer": True,
                    "answer_mode": "quote_product_fact" if unsafe else "soft_social_reply" if refusal else "ask_clarifying_question",
                    "evidence_used": {"common_sense_topics": ["购车需求收集或日常交流"]},
                    "facts_claimed": [], "reply_segments": [reply], "confidence": .95,
                    "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
                    "recommended_action": "send_reply",
                }
            body = json.dumps({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "customer_service_brain": {
            "enabled": True, "mode": "brain_first", "provider": "openai_compatible",
            "model": "intent-brain", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "api_key": "FAKE-INTENT-REGRESSION", "min_confidence": .2,
            "require_evidence": True, "require_fact_claims": True,
            "quality_verifier_enabled": True, "semantic_reviewer_enabled": True,
            "semantic_reviewer_cache_enabled": False, "semantic_reviewer_model": "intent-reviewer",
            "require_final_visible_polish": False, "fallback_to_legacy_on_error": False,
        },
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False},
    }
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    try:
        worker, binding = api._setup_bound_conversation()
        history = ["你好"] if scenario == "greeting_then_need" else ["你今天吃啥", "你是不是AI", "别老聊车，我就随便问问"]
        seed_history(binding, history)
        event = api._ingest(worker, binding["conversation_id"], "intent-current", question)
        batch_id = api._collect(binding["conversation_id"], event)["batch_id"]
        decision = api._generate(batch_id)
        with api.SessionLocal() as db:
            result = db.get(MessageBatch, batch_id).ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
            (tmp_path / "brain-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            if unsafe:
                assert decision["decision"] == "retry_later", decision
                assert "missing_fact_claims" in result["plan_validation"]["errors"]
                assert not list(db.scalars(select(ReplyAction)))
                return
            assert decision["decision"] == "send_reply", decision
            assert result["adoptable"] is True
            assert result["reply_text"] == expected_reply
            assert not list(db.scalars(select(HandoffEvent)))
            assert len(list(db.scalars(select(ReplyAction)))) == 1
            if scenario == "stale_fatigue":
                assert WARNING in result["quality_verification"]["warnings"]
                assert result["quality_gate_v2"]["invoked"] is True
            if refusal:
                assert result["quality_repair"]["ok"] is True
                assert result["repaired_quality_gate_v2"]["ok"] is True
        brains = [p for p in requests if p["model"] == "intent-brain"]
        assert len(brains) == (2 if refusal else 1), requests
        if refusal:
            assert "ignores_current_refusal" in json.dumps(brains[1], ensure_ascii=False)
        first_prompt = json.dumps(brains[0], ensure_ascii=False)
        assert question in first_prompt
        assert all(text in first_prompt for text in history)
        simulated_send_ack(worker, binding, decision, expected_reply)
    finally:
        (tmp_path / "provider-requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        get_settings.cache_clear()

