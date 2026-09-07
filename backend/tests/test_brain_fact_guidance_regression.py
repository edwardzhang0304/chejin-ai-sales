"""Incident d85a414e: approved, redacted candidate text + artificial test context.

The two second-attempt candidate texts and cited guidance below came from the
approved incident inspection. IDs, history and customer input are test fixtures;
this is not a replay of the entire production conversation or Windows sending.
"""
from copy import deepcopy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "worker-client" / "omniauto-rpa"
for path in (RUNTIME, RUNTIME / "apps/wechat_ai_customer_service", RUNTIME / "apps/wechat_ai_customer_service/workflows", RUNTIME / "apps/wechat_ai_customer_service/adapters"):
    sys.path.insert(0, str(path))

from customer_service_brain_contract import normalize_brain_plan, validate_brain_plan
from apps.wechat_ai_customer_service.workflows.customer_service_brain import brain_plan_allows_soft_evidence_override, validate_plan_against_evidence

CANDIDATES = [
    ["好嘞，欢迎您～", "您之前咨询过二手车，想看哪类车、有什么需求都可以直接发我，我帮您筛选合适车源。"],
    ["好嘞，欢迎您～", "您之前问过二手车，想先看哪类车或大概什么预算，都可以告诉我，我帮您找合适的车源。"],
]
GUIDANCE = [
    ("购车需求收集", "低风险购车咨询可收集预算范围、主要用途、车型或车身类型偏好、所在城市、贷款或全款、是否置换。每轮只询问一到两个最关键问题，不要一次连续追问全部信息；没有车辆证据时不得推荐具体车型。"),
    ("闲聊自然转入购车需求", "客户闲聊时先自然回应，再结合上下文询问一个购车相关问题；没有车辆证据时不得借机推荐具体车型。"),
]


def make_plan(*, reply=None, mode="soft_redirect_to_business", evidence=None):
    return normalize_brain_plan({
        "can_answer": True,
        "answer_mode": mode,
        "evidence_used": evidence if evidence is not None else {"formal_knowledge_ids": ["test-guidance-1", "test-guidance-2"]},
        "facts_claimed": [],
        "reply_segments": reply or CANDIDATES[0],
        "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
        "recommended_action": "send_reply",
        "confidence": .9,
    })


@pytest.mark.parametrize("reply", CANDIDATES)
@pytest.mark.parametrize("mode", ["soft_social_reply", "soft_redirect_to_business", "ask_clarifying_question", "collect_customer_info", "direct_answer"])
def test_guidance_references_do_not_invent_factual_claims(reply, mode):
    plan = make_plan(reply=reply, mode=mode)
    original = deepcopy(plan)
    assert validate_brain_plan(plan, require_fact_claims=True)["ok"]
    assert brain_plan_allows_soft_evidence_override(plan)
    assert plan == original  # keep both knowledge references and the exact reply


@pytest.mark.parametrize("evidence", [{}, {"formal_knowledge_ids": ["test-guidance-1"]}])
@pytest.mark.parametrize("reply,mode", [
    (["这台售价8.68万。"], "soft_social_reply"),
    (["目前有现车库存。"], "ask_clarifying_question"),
    (["贷款审批一定通过。"], "direct_answer"),
    (["合同保证包退。"], "collect_customer_info"),
    (["累计行驶3万公里。"], "compare_options"),
    (["建议选择这款车。"], "recommend_from_catalog"),
    (["这是已经核实的车辆资料。"], "quote_product_fact"),
])
def test_labels_and_guidance_do_not_bypass_missing_fact_checks(evidence, reply, mode):
    plan = make_plan(reply=reply, mode=mode, evidence=evidence)
    assert "missing_fact_claims" in validate_brain_plan(plan, require_fact_claims=True)["errors"]
    assert not brain_plan_allows_soft_evidence_override(plan)


@pytest.mark.parametrize("mode", ["recommend_from_catalog", "quote_product_fact"])
def test_common_sense_label_cannot_exempt_factual_mode(mode):
    plan = make_plan(mode=mode, evidence={"common_sense_topics": ["测试常识"]})
    assert "missing_fact_claims" in validate_brain_plan(plan, require_fact_claims=True)["errors"]
    assert not brain_plan_allows_soft_evidence_override(plan)


def test_existing_common_sense_finance_question_remains_nonfactual():
    plan = make_plan(mode="collect_customer_info", reply=["您考虑贷款还是全款？"], evidence={"common_sense_topics": ["购车需求收集"]})
    assert validate_brain_plan(plan, require_fact_claims=True)["ok"]


@pytest.mark.parametrize("risk", [
    {"risk_level": "high", "risk_tags": ["policy_violation"], "needs_handoff": False},
    {"risk_level": "low", "risk_tags": [], "needs_handoff": True},
])
def test_guidance_does_not_override_hard_risk(risk):
    plan = make_plan()
    plan["risk"] = risk
    assert not brain_plan_allows_soft_evidence_override(plan)


@pytest.mark.parametrize("citation", ["test-guidance-1", "policy:test-guidance-1", "formal_knowledge:test-guidance-1"])
def test_fact_free_guidance_citation_must_exist_in_current_evidence(citation):
    plan = make_plan(evidence={"formal_knowledge_ids": [citation]})
    assert not validate_plan_against_evidence(plan, {})["ok"]
    assert validate_plan_against_evidence(plan, {"evidence_ids": ["policy:test-guidance-1"]})["ok"]
    assert not validate_plan_against_evidence(plan, {"evidence_ids": ["policy:other-turn-guidance"]})["ok"]


@pytest.mark.parametrize("candidate,bad_source_first", [(0, False), (1, False), (0, True)])
def test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, candidate, bad_source_first, expect_reply=True):
    """Public C3 API -> SQLite -> real child Brain/Guard -> HTTP provider.

    Only the external LLM endpoint/configuration is replaced; the provider returns
    the incident candidate and an explicit artificial semantic-review verdict.
    No mocked evidence builder, validator, Brain result or C3 decision.
    """
    import test_c3_api as api
    from app.core.config import get_settings
    from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction
    from app.models.task import Task
    from app.services.ai_adapter import RealOmniAutoAIEngineAdapter

    api.setup_function()
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(RUNTIME))
    monkeypatch.setenv("C3_AI_ADAPTER_MODE", "real")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "FAKE-BRAIN-GUIDANCE-TEST")
    get_settings.cache_clear()
    requests = []
    plan = make_plan(reply=CANDIDATES[candidate])

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            review = payload.get("model") == "test-semantic-reviewer"
            result = {
                "verdict": "pass", "confidence": .95,
                "customer_visible_risk": "low", "semantic_errors": [],
                "hard_boundary_concerns": [], "reason": "Artificial review of a fact-free greeting/needs question",
            } if review else plan
            if not review and bad_source_first and sum(item["model"] == "test-brain" for item in requests) == 1:
                result = deepcopy(plan)
                result["evidence_used"]["formal_knowledge_ids"] = ["invented-guidance-source"]
            body = json.dumps({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}).encode()
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
            "enabled": True, "mode": "brain_first",
            "provider": "openai_compatible", "model": "test-brain",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "api_key": "FAKE-BRAIN-GUIDANCE-TEST",
            "min_confidence": .2, "require_evidence": True, "require_fact_claims": True,
            "quality_verifier_enabled": True, "semantic_reviewer_enabled": True,
            "semantic_reviewer_force": True, "semantic_reviewer_cache_enabled": False,
            "semantic_reviewer_model": "test-semantic-reviewer",
            "require_final_visible_polish": False, "fallback_to_legacy_on_error": False,
        },
        "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
        "raw_message_store": {"enabled": False},
        "final_visible_llm_polish": {"enabled": False},
    }
    # Endpoint substitution only: _load_config normally pins the live DeepSeek URL.
    monkeypatch.setattr(RealOmniAutoAIEngineAdapter, "_load_config", staticmethod(lambda: deepcopy(config)))
    try:
        for title, content in GUIDANCE:
            api._publish_managed_knowledge(title, content)
        response = api.client.get("/api/knowledge/items", headers=api.HEADERS)
        assert response.status_code == 200, response.text
        plan["evidence_used"]["formal_knowledge_ids"] = sorted(item["id"] for item in response.json()["data"]["items"])
        worker, binding = api._setup_bound_conversation()
        event_id = api._ingest(worker, binding["conversation_id"], "incident-guidance-fixture", "您好，之前咨询过二手车，想了解购车需求。")
        batch = api._collect(binding["conversation_id"], event_id)
        first = api._generate(batch["batch_id"])
        if not expect_reply:
            assert first["decision"] == "retry_later", first
            assert first["error_code"] == "AI_ENGINE_NO_VISIBLE_REPLY"
            with api.SessionLocal() as db:
                saved = db.get(MessageBatch, batch["batch_id"])
                result = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
                assert "missing_fact_claims" in result["plan_validation"]["errors"]
                assert not list(db.scalars(select(ReplyAction)))
                assert not list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))
            return
        assert first["decision"] == "send_reply", first
        second = api._generate(batch["batch_id"])
        assert second["reply_action_id"] == first["reply_action_id"]
        assert second["task_id"] == first["task_id"]
        with api.SessionLocal() as db:
            saved = db.get(MessageBatch, batch["batch_id"])
            result = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
            assert result["plan_validation"]["ok"]
            assert result["brain_plan"]["facts_claimed"] == []
            assert result["brain_plan"]["evidence_used"]["formal_knowledge_ids"] == plan["evidence_used"]["formal_knowledge_ids"]
            assert result["brain_plan"]["reply_segments"] == CANDIDATES[candidate]
            assert result["visible_reply_source"] == "brain_plan.reply_segments"
            assert result["adoptable"] is True
            assert len(list(db.scalars(select(ReplyAction)))) == 1
            assert len(list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))) == 1
            assert not list(db.scalars(select(HandoffEvent)))
        assert sum(item["model"] == "test-brain" for item in requests) == (2 if bad_source_first else 1)
        assert sum(item["model"] == "test-semantic-reviewer" for item in requests) >= 1
        prompt = json.dumps(requests[0], ensure_ascii=False)
        for title, _ in GUIDANCE:
            assert title in prompt
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        get_settings.cache_clear()


# Artificial review counterexamples, not text taken from the production batch.
PAYMENT_QUESTIONS = [
    "您考虑贷款还是全款？",
    "您是考虑全款还是分期？",
    "你打算分期还是全款呢？",
    "请问您更倾向全款还是贷款？",
    "您好，您准备全款还是贷款买车？",
    "您这边是打算全款购车还是分期购车？",
    "您考虑贷款还是全款",  # Grammar, not a question mark, establishes an inquiry.
]
PAYMENT_CLAIMS = [
    "贷款审批一定通过，您考虑贷款还是全款？",
    "您考虑贷款还是全款？审批包过。",
    "您考虑贷款还是全款，利率3%？",
    "您考虑贷款还是全款？这台售价8.68万。",
    "贷款包过，您要分期吗？",
    "您考虑贷款还是全款都能批？",
    "您考虑贷款还是全款？分期不需要审核。",
    "这台贷款月供2000元，您考虑贷款还是全款？",
    "您考虑贷款还是全款？现车库存充足。",
    "您考虑贷款还是全款？合同保证包退。",
    "您考虑零利率分期还是全款？",
    "您考虑贷款还是全款？我们保证没有利息。",
]


@pytest.mark.parametrize("question", PAYMENT_QUESTIONS)
@pytest.mark.parametrize("mode", ["collect_customer_info", "ask_clarifying_question", "direct_answer"])
def test_formal_guidance_payment_preference_is_not_a_fact(question, mode):
    plan = make_plan(reply=[question], mode=mode)
    original = deepcopy(plan)
    assert validate_brain_plan(plan, require_fact_claims=True)["ok"]
    assert brain_plan_allows_soft_evidence_override(plan)
    assert plan == original


@pytest.mark.parametrize("reply", PAYMENT_CLAIMS)
def test_payment_question_cannot_hide_an_authoritative_claim(reply):
    plan = make_plan(reply=[reply], mode="collect_customer_info")
    assert "missing_fact_claims" in validate_brain_plan(plan, require_fact_claims=True)["errors"]
    assert not brain_plan_allows_soft_evidence_override(plan)


@pytest.mark.parametrize("question", PAYMENT_QUESTIONS[:2])
def test_payment_preference_reaches_c3_reply_task(monkeypatch, question):
    monkeypatch.setattr(sys.modules[__name__], "CANDIDATES", [[question]])
    test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, 0, False)


@pytest.mark.parametrize("reply", [PAYMENT_CLAIMS[0], PAYMENT_CLAIMS[2], PAYMENT_CLAIMS[3]])
def test_payment_commitment_creates_no_c3_reply_task(monkeypatch, reply):
    monkeypatch.setattr(sys.modules[__name__], "CANDIDATES", [[reply]])
    test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, 0, False, expect_reply=False)


# Artificial combinations of existing reply segments; do not concatenate them
# before exercising the production protocol or substitute the schema result.
SEGMENTED_QUESTIONS = [
    ["好嘞，欢迎您～", "您考虑贷款还是全款？"],
    ["收到，我们先了解下您的需求。", "您是考虑全款还是分期？"],
    ["您考虑贷款还是全款？", "您的预算大概多少？"],
    ["好嘞，欢迎您～", "您的预算大概多少？", "您考虑贷款还是全款？"],
    ["您考虑贷款还是全款？", "收到，我们先了解下您的需求。"],
]
SEGMENTED_CLAIMS = [
    ["贷款审批一定通过。", "您考虑贷款还是全款？"],
    ["您考虑贷款还是全款？", "贷款审批一定通过。"],
    ["好嘞，欢迎您～", "您考虑贷款还是全款？", "这台售价8.68万。"],
    ["这台售价8.68万。", "好嘞，欢迎您～", "您考虑贷款还是全款？"],
    ["您考虑贷款还是全款？", "审批包过。"],
    ["利率3%。", "您考虑贷款还是全款？"],
    ["您考虑贷款还是全款？", "月供2000元。"],
    ["您考虑贷款还是全款？", "肯定能批。"],
    ["您考虑贷款还是全款？", "保证通过。"],
    ["您考虑贷款还是全款？", "无息。"],
    ["您考虑贷款还是全款？审批包过。", "好嘞，欢迎您～"],
    ["好嘞，欢迎您～", "贷款包过，您考虑贷款还是全款？"],
]


@pytest.mark.parametrize("segments", SEGMENTED_QUESTIONS)
def test_segmented_payment_questions_preserve_every_segment(segments):
    plan = make_plan(reply=segments, mode="collect_customer_info")
    original = deepcopy(plan)
    assert plan["reply_segments"] == segments
    assert validate_brain_plan(plan, require_fact_claims=True)["ok"]
    assert brain_plan_allows_soft_evidence_override(plan)
    assert plan == original


@pytest.mark.parametrize("segments", SEGMENTED_CLAIMS)
def test_segmented_payment_question_never_hides_other_claims(segments):
    plan = make_plan(reply=segments, mode="collect_customer_info")
    original = deepcopy(plan)
    assert "missing_fact_claims" in validate_brain_plan(plan, require_fact_claims=True)["errors"]
    assert not brain_plan_allows_soft_evidence_override(plan)
    assert plan == original


@pytest.mark.parametrize("segments", [SEGMENTED_QUESTIONS[0], SEGMENTED_QUESTIONS[1], SEGMENTED_QUESTIONS[3]])
def test_segmented_payment_question_reaches_c3_task(monkeypatch, segments):
    monkeypatch.setattr(sys.modules[__name__], "CANDIDATES", [segments])
    test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, 0, False)


@pytest.mark.parametrize("segments", [SEGMENTED_CLAIMS[0], SEGMENTED_CLAIMS[1], SEGMENTED_CLAIMS[2], SEGMENTED_CLAIMS[4], SEGMENTED_CLAIMS[5]])
def test_segmented_payment_claim_creates_no_c3_task(monkeypatch, segments):
    monkeypatch.setattr(sys.modules[__name__], "CANDIDATES", [segments])
    test_c3_api_database_real_brain_and_provider_preserve_guided_reply(monkeypatch, 0, False, expect_reply=False)
