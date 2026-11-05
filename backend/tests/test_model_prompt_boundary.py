"""Prompt boundary checks only; no model quality claims or live calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import test_brain_fact_guidance_regression  # establishes the packaged runtime import paths
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from apps.wechat_ai_customer_service.workflows import customer_service_quality_reviewer as reviewer
from apps.wechat_ai_customer_service.workflows.catalog_query import prompt_protocol, model_protocol_view
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import model_authority_view, reply_authority_snapshot
from customer_service_brain_contract import OUTPUT_FIELDS_INSTRUCTION


@pytest.mark.parametrize("spent,expected_timeout", [(10, 60), (150, 25), (175, None)])
def test_packaged_review_timeout_reaches_transport_within_shared_budget(monkeypatch, spent, expected_timeout):
    from apps.wechat_ai_customer_service import llm_config
    from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
    from app.core.config import get_settings
    path = Path(__file__).resolve().parents[1] / "configs/chejin_c3_brain.json"
    monkeypatch.setattr(get_settings(), "c3_omniauto_config_path", str(path))
    config = RealOmniAutoAIEngineAdapter._load_config()
    settings = reviewer.effective_reviewer_settings(brain.effective_brain_settings(config))
    assert settings["timeout_seconds"] == settings["fallback_timeout_seconds"] == 60
    monkeypatch.setattr(reviewer, "resolve_llm_api_key", lambda **kw: "offline-token")
    clock = [100.0]
    monkeypatch.setattr(llm_config.time, "monotonic", lambda: clock[0])
    calls = []
    def transport(**kw):
        calls.append(kw)
        # No real model result is claimed: stop at the transport boundary.
        return {"ok": True, "status": 200, "response_text": "{}"}
    monkeypatch.setattr(llm_config, "call_llm_request_once_with_wall_timeout", transport)
    with llm_config.llm_total_time_budget(config["customer_service_brain"]["total_time_budget_seconds"]):
        clock[0] += spent
        result = reviewer.run_quality_reviewer_llm(settings=settings, request={})
    assert result["status"] == "error"  # empty approval cannot acquire a permit
    if expected_timeout is None:
        assert not calls
    else:
        assert len(calls) == 1
        assert calls[0]["timeout"] == calls[0]["wall_timeout"] == expected_timeout


def inputs():
    evidence = {
        "catalog_protocol": {"protocol": "vehicle_query_v1", "model_query_contract": "vehicle_need_v2",
                             "phase": "not_queried", "preparation": {"route": "model"},
                             "sources": [{"id": "source-1", "content": "合成需求：只看电车"}]},
        "current_batch": [{"id": "source-1", "sender": "customer", "content": "合成需求：只看电车"}],
        "knowledge": {"product_master": {"items": []}, "formal_knowledge": {}},
        "generation_identity": {"batch_id": "internal-batch", "generation_attempt": 2},
    }
    return {"evidence": evidence, "current_message": {}, "conversation": {}, "target": {}}


def test_projection_hides_wire_identity_without_changing_frozen_facts():
    data = inputs(); evidence = data["evidence"]
    frozen = deepcopy(reply_authority_snapshot(evidence)); before = deepcopy(evidence)
    view = model_authority_view(evidence)
    assert "generation_identity" not in view
    assert "model_query_contract" not in view["catalog_protocol"]
    assert "protocol" not in view["catalog_protocol"]
    assert view["sources"] == frozen["sources"]
    assert view["product_master"] == frozen["product_master"]
    assert evidence == before
    assert reply_authority_snapshot(evidence) == frozen
    assert prompt_protocol(evidence)["sources"] == frozen["catalog_protocol"]["sources"]


def test_nested_query_metadata_is_hidden_but_conditions_and_raw_evidence_survive():
    query = {"model_query_contract": "vehicle_need_v2", "filters": [{"field": "energy_type", "value": "electric"}],
             "raw_model_query": {"unsupported_requirements": ["省油"]}, "source_ids": ["source-1"],
             "sort_origin": "program_default", "budget_bounds": {"upper": 8}}
    protocol = {"model_query_contract": "vehicle_need_v2", "receipt": {"protocol": "vehicle_query_v1", "query": query}}
    before = deepcopy(protocol)
    view = model_protocol_view(protocol)
    expected = deepcopy(query); expected.pop("model_query_contract")
    assert view["receipt"]["query"] == expected
    assert "vehicle_need_v2" not in json.dumps(view)
    assert protocol == before


@pytest.mark.parametrize("stage", ["understanding", "completion", "repair", "review"])
def test_every_prompt_stage_has_one_field_constraint_and_no_wire_contract(stage):
    data = inputs(); evidence = data["evidence"]
    plan = {"reply_segments": ["您更偏好哪种能源？"], "recommended_action": "send_reply"}
    if stage == "completion":
        evidence["catalog_protocol"]["phase"] = "queried"
    if stage == "review":
        request = reviewer.build_quality_review_request(settings={}, brain_input=data, evidence_pack=evidence,
                                                       plan=plan, deterministic_quality={})
        system, user = reviewer.build_quality_reviewer_prompt(request)
        assert request["authority_evidence_summary"] == model_authority_view(evidence)
    else:
        pack = (brain.build_brain_repair_prompt_pack(settings={}, brain_input=data, plan=plan, quality={})
                if stage == "repair" else brain.build_brain_prompt_pack(settings={}, brain_input=data))
        system, user = pack["system"], brain.build_brain_user_content(pack)
    assert system.count(OUTPUT_FIELDS_INSTRUCTION) == 1
    assert "vehicle_need_v2" not in system + user
    assert "vehicle_query_v1" not in system + user
    assert "internal-batch" not in user
    assert "合成需求：只看电车" in user


def test_json_repair_also_uses_the_field_constraint(monkeypatch):
    captured = []
    monkeypatch.setattr(brain, "resolve_effective_llm_provider", lambda *a, **k: "deepseek")
    monkeypatch.setattr(brain, "resolve_llm_api_key", lambda **k: "synthetic")
    monkeypatch.setattr(brain, "resolve_llm_tier_model", lambda **k: "synthetic")
    monkeypatch.setattr(brain, "resolve_llm_base_url", lambda **k: "https://invalid.example")
    def capture(**request):
        captured.append(request)
        return {"ok": False, "status": 0, "error": "offline_boundary_only"}
    monkeypatch.setattr(brain, "call_recorded_brain_llm", capture)
    brain.maybe_repair_brain_json_structure(settings={}, raw_text='{"catalog_query":{"operation" "search"}}',
                                          stage="brain_llm", brain_input=inputs())
    assert len(captured) == 1
    messages = captured[0]["messages"]
    assert messages[0]["content"].count(OUTPUT_FIELDS_INSTRUCTION) == 1
    assert "model_query_contract" not in json.loads(messages[1]["content"])
    assert "vehicle_need_v2" not in json.dumps(messages)
