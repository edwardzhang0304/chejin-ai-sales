"""Real Brain/Guard subprocess and HTTP provider; only the external model is synthetic."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from sqlalchemy import select
import test_c3_api as fixtures
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction, MessageBatch
from app.models.worker import Worker
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.reply_sequence_policy import sequence_policy
from test_reply_sequence_http import status


PARTS = [
    "您好，购车需求可以慢慢聊，您之前咨询过二手车，平时主要是自己上下班使用，还是更多考虑一家人出行，也可以说说您目前的想法。",
    "您大概准备多少购车预算呢，付款方式更倾向全款还是贷款，暂时没有决定也没关系，可以先告诉我您已经确定的需求，再继续了解。",
    "另外您对轿车或者其他车身形式有没有特别偏好，平时通常几个人用车，如果还没有明确方向，也可以先说说自己最在意的使用需求。",
]


@pytest.mark.parametrize("oversized_first", [False, True])
def test_brain_provider_to_group_without_replacing_brain_or_guard(http_api, monkeypatch, async_generation, tmp_path, oversized_first):
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "SYNTHETIC-LOCAL-ONLY")
    requests = []
    plan = {"can_answer": True, "answer_mode": "collect_customer_info", "evidence_used": {"common_sense_topics": ["购车注意事项"]},
            "facts_claimed": [], "reply_segments": PARTS,
            "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
            "recommended_action": "send_reply", "confidence": .95}
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            review = payload["model"] == "sequence-reviewer"
            result = {"verdict": "pass", "confidence": .99, "customer_visible_risk": "low", "semantic_errors": [],
                      "hard_boundary_concerns": [], "reason": "Synthetic review of nonfactual advice"} if review else deepcopy(plan)
            if oversized_first and not review and sum(r["model"] != "sequence-reviewer" for r in requests) == 1:
                result["reply_segments"] = ["啊" * 109]
            body = json.dumps({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}, "finish_reason": "stop"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    config = {"customer_service_brain": {**sequence_policy(), "enabled": True, "mode": "brain_first",
        "provider": "openai_compatible", "model": "sequence-author", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "SYNTHETIC-LOCAL-ONLY", "min_confidence": .2, "require_evidence": True, "require_fact_claims": True,
        "quality_verifier_enabled": True, "semantic_reviewer_enabled": True, "semantic_reviewer_force": True,
        "semantic_reviewer_cache_enabled": False, "semantic_reviewer_model": "sequence-reviewer",
        "require_final_visible_polish": False, "fallback_to_legacy_on_error": False},
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False}}
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", RealOmniAutoAIEngineAdapter)
    monkeypatch.setattr(fixtures, "client", http_api)
    try:
        worker, binding = fixtures._setup_bound_conversation()
        with SessionLocal() as db:
            db.get(Worker, worker["id"]).local_lock_summary = {"capabilities": {"reply_sequence_version": 1}}
            db.commit()
        fixtures._ingest(worker, binding["conversation_id"], "sequence-provider-question", "买二手车需要注意哪些事情？")
        import time
        actions = []
        for _ in range(200):
            with SessionLocal() as db:
                actions = list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
            if actions: break
            time.sleep(.05)
        with SessionLocal() as db:
            snapshots = [{"status": b.status, "error": b.error_code, "snapshot": b.ai_response_snapshot} for b in db.scalars(select(MessageBatch))]
            (tmp_path / "brain-results.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2, default=str))
        assert [a.reply_text for a in actions] == PARTS, snapshots
        assert all(len(a.reply_text) <= 108 for a in actions)
        assert len([r for r in requests if r["model"] != "sequence-reviewer"]) >= (2 if oversized_first else 1)
        assert requests and "108" in json.dumps(requests[0], ensure_ascii=False)
        assert status(http_api, worker, actions[0].batch_id)["reply_sequence"]["segment_count"] == 3
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()
        (tmp_path / "provider-requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
