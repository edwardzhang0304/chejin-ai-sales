"""Replay actual full-flow-generated context into real Brain/Guard/HTTP Provider.

The evidence path is explicit. It must come from the Worker integration test,
not a hand-built history, and contains synthetic conversation data only.
"""
from copy import deepcopy
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.reply_sequence_policy import sequence_policy


@contextmanager
def controlled_recovery_provider(monkeypatch, answer, requests, *, review_verdict="pass"):
    plan = {"can_answer": True, "answer_mode": "collect_customer_info",
        "evidence_used": {"common_sense_topics": ["购车注意事项"]}, "facts_claimed": [], "reply_segments": [answer],
        "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
        "recommended_action": "send_reply", "confidence": .95}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            result = ({"verdict": review_verdict, "confidence": .99,
                "customer_visible_risk": "low" if review_verdict == "pass" else "high",
                "semantic_errors": [] if review_verdict == "pass" else ["unapproved_commitment"],
                "hard_boundary_concerns": [], "reason": "Synthetic review"}
                if payload["model"] == "recovery-reviewer" else deepcopy(plan))
            body = json.dumps({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
                                            "finish_reason": "stop"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    config = {"customer_service_brain": {**sequence_policy(), "enabled": True, "mode": "brain_first",
        "provider": "openai_compatible", "model": "recovery-author", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "SYNTHETIC-LOCAL-ONLY", "min_confidence": .2, "require_evidence": True, "require_fact_claims": True,
        "quality_verifier_enabled": True, "semantic_reviewer_enabled": True, "semantic_reviewer_force": True,
        "semantic_reviewer_cache_enabled": False, "semantic_reviewer_model": "recovery-reviewer",
        "require_final_visible_polish": False, "fallback_to_legacy_on_error": False},
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False}}
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "SYNTHETIC-LOCAL-ONLY")
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    try:
        yield requests
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()


@pytest.fixture
def recovery_provider_factory(monkeypatch, tmp_path):
    logs = []
    with ExitStack() as stack:
        def start(answer):
            requests = []
            logs.append(requests)
            stack.enter_context(controlled_recovery_provider(monkeypatch, answer, requests))
            return requests
        try:
            yield start
        finally:
            (tmp_path / "real-provider-requests.json").write_text(json.dumps(logs, ensure_ascii=False, indent=2))


def test_confirmed_partial_reply_reaches_final_provider_request(monkeypatch, tmp_path):
    path = os.environ.get("CHEJIN_RECOVERY_FLOW_EVIDENCE")
    if not path:
        pytest.skip("requires explicitly supplied Worker recovery integration evidence")
    evidence = json.loads(Path(path).read_text())
    assert evidence["failure_after"] == 1 and evidence["fresh"]["ok"]
    request = evidence["brain_inputs"][-1]
    prefix = request["conversation_context"]["brain_context_snapshot"]["partial_reply_recovery"]["confirmed_prefix"]
    assert len(prefix) == 1
    requests = []
    answer = evidence["resumed"]
    try:
        with controlled_recovery_provider(monkeypatch, answer, requests):
            result = RealOmniAutoAIEngineAdapter().generate_reply_decision(**request)
            from dataclasses import asdict
            (tmp_path / "provider-result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
            assert result.decision == "send_reply" and result.reply_text == answer, result
            authored = [p for p in requests if p["model"] == "recovery-author"]
            assert authored
            prompt = json.dumps(authored[0], ensure_ascii=False)
            assert "partial_reply_recovery" in prompt and "仅回答尚未答完的内容" in prompt
            assert "".join(prefix[0]["text"].split()) in "".join(prompt.split())
    finally:
        (tmp_path / "provider-requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
