"""C3 API + isolated SQLite + child Brain + local HTTP provider.

Only provider configuration/HTTP responses are controlled. The real validators,
retry flow, adapter and creation of reply tasks are exercised. No Windows send.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest
from sqlalchemy import select

import test_c3_api as api
from app.core.config import get_settings
from app.models.c3 import MessageBatch, ReplyAction
from app.models.task import Task
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_brain_fact_guidance_regression import GUIDANCE, RUNTIME

FULL_REPLY = "好的，丰田先排除。您还有其他品牌偏好吗？"


def test_formal_config_keeps_repair_budget():
    path = Path(__file__).resolve().parents[1] / "configs" / "chejin_c3_brain.json"
    brain = json.loads(path.read_text())["customer_service_brain"]
    assert brain["quality_repair_max_tokens"] == 16384
    assert brain["low_authority_fast_repair_max_tokens"] == 16384


def plan(reply=FULL_REPLY):
    return {"can_answer": True, "understanding": {"excluded_brand": "丰田"},
            "answer_mode": "ask_clarifying_question", "evidence_used": {}, "facts_claimed": [],
            "reply_segments": [reply], "recommended_action": "send_reply",
            "confidence": 0.9, "risk": {"risk_level": "low", "needs_handoff": False}}


@pytest.mark.parametrize("repair_first", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_c3_only_complete_generation_creates_reply_task(monkeypatch, repair_first, recover):
    api.setup_function()
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(RUNTIME))
    monkeypatch.setenv("C3_AI_ADAPTER_MODE", "real")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "isolated-completion-test-key")
    get_settings.cache_clear()
    calls = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reviewer = payload["model"] == "isolated-reviewer"
            if reviewer:
                content = json.dumps({"verdict": "pass", "errors": [], "risk_level": "low"})
                finish = "stop"
            else:
                calls.append(payload)
                index = len(calls) - 1
                if repair_first and index == 0:
                    # A real missing-source validation error requests Brain repair.
                    bad = plan()
                    bad["evidence_used"] = {"formal_knowledge_ids": ["nonexistent-test-source"]}
                    content, finish = json.dumps(bad, ensure_ascii=False), "stop"
                elif index == int(repair_first) or not recover:
                    raw = json.dumps(plan(), ensure_ascii=False)
                    content = raw[:raw.index("好的，丰田") + len("好的，丰田")]
                    finish = "length"
                else:
                    content, finish = json.dumps(plan(), ensure_ascii=False), "stop"
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content},
                                            "finish_reason": finish}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "customer_service_brain": {
            "enabled": True, "mode": "brain_first", "provider": "openai_compatible",
            "model": "isolated-brain", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "min_confidence": .2, "require_evidence": True, "require_fact_claims": True,
            "semantic_reviewer_enabled": True, "semantic_reviewer_force": True,
            "semantic_reviewer_cache_enabled": False, "semantic_reviewer_model": "isolated-reviewer",
            "fallback_to_legacy_on_error": False, "require_final_visible_polish": False,
        },
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False},
    }
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    try:
        for title, text in GUIDANCE:
            api._publish_managed_knowledge(title, text)
        worker, binding = api._setup_bound_conversation()
        event = api._ingest(worker, binding["conversation_id"], "completion-fixture", "不要丰田车")
        batch = api._collect(binding["conversation_id"], event)
        # Ingest already runs the real asynchronous generation entry. Calling
        # generate here would start a second attempt for a retry_wait batch.
        with api.SessionLocal() as db:
            actions = list(db.scalars(select(ReplyAction)))
            tasks = list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))
            saved = db.get(MessageBatch, batch["batch_id"])
            assert saved.generation_attempt_count == 1
            result = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
            if recover:
                assert saved.decision == "send_reply", result
                assert len(actions) == len(tasks) == 1
                assert actions[0].reply_text == FULL_REPLY
                assert actions[0].conversation_id == binding["conversation_id"]
                assert result["guard"]["allowed"] is True
                assert result["quality_verification"]["ok"] is True
                repeated = api._generate(batch["batch_id"])
                assert repeated["reply_action_id"] == actions[0].id
            else:
                assert saved.decision == "retry_later", result
                assert actions == [] and tasks == []
                assert not result.get("reply_text")
            diagnostics = [e["response_diagnostics"] for e in result["provider_progress"]
                           if "response_diagnostics" in e]
            assert any(d.get("finish_reason") == "length" for d in diagnostics)
        assert len(calls) == 2 + int(repair_first), {
            "calls": [{"model": c["model"], "max_tokens": c["max_tokens"]} for c in calls],
            "progress": [(e.get("stage"), e.get("event")) for e in result["provider_progress"]],
        }
        if repair_first:
            assert all(c["max_tokens"] == 16384 for c in calls[1:])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        get_settings.cache_clear()
