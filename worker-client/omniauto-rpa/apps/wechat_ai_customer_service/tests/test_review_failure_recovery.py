"""Synthetic provider responses through real Brain, review serialization and Guard."""
from copy import deepcopy
import json
import sys

import pytest

from test_catalog_query_entry import run_entry, fixtures, review, summary_plan, query
from test_catalog_query import row, cq, evidence

brain = fixtures.brain_module
reviewer = sys.modules[brain.review_brain_reply_semantics.__module__]


@pytest.mark.parametrize("count", [7, 9, 15])
def test_review_serialization_preserves_all_claims_and_values(monkeypatch, count):
    captured = []
    expected = []

    def make_plan(receipt):
        p = fixtures.base_plan()
        p.update(answer_mode="recommend_from_catalog", reply_segments=["合成车fit报价3.58万。"],
                 evidence_used={"product_ids": ["fit"]}, facts_claimed=[
                     {"fact_type": "price", "value": "3.58万", "source_level": "product_master", "source_id": "fit"}
                     for _ in range(count)])
        # The full value of the last claim must also survive the prompt boundary.
        p["facts_claimed"][-1] = {"fact_type": "config", "value": "合成配置说明" * 30,
                                     "source_level": "product_master", "source_id": "fit"}
        expected[:] = deepcopy(p["facts_claimed"])
        return p

    def transport(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        captured.append(payload)
        return {"ok": True, "status": 200, "response_text": json.dumps(review())}

    monkeypatch.setattr(reviewer, "resolve_llm_api_key", lambda **_: "synthetic")
    monkeypatch.setattr(reviewer, "call_llm_request_with_failover", transport)
    result, _ = run_entry(monkeypatch, rows=[row("fit", 3.58)], make_plan=make_plan,
                          semantic=None, raw_query=query())
    assert result.get("adoptable"), result
    assert captured
    assert captured[0]["review_request"]["brain_plan_summary"]["facts_claimed"] == expected


@pytest.mark.parametrize("after_repair", [False, True])
@pytest.mark.parametrize("kind,error,classification", [
    ("transport", "wall_timeout_25s", "review_timeout"),
    ("transport", "connection reset", "review_unavailable"),
    ("protocol", "semantic_reviewer_response_incomplete", "review_invalid_response"),
])
def test_review_failure_never_requests_content_repair(monkeypatch, after_repair, kind, error, classification):
    generated = []
    def make_plan(receipt):
        p = summary_plan(receipt)
        generated.append(deepcopy(p))
        return p
    failure = {"status": "error", "error": error, "error_kind": kind,
               "elapsed_ms": 25001, "provider": "synthetic", "http_status": 0}
    original = brain.review_brain_reply_semantics
    reviewed = []
    def invoke(**kwargs):
        if after_repair and not reviewed:
            kwargs["settings"] = {**kwargs["settings"], "semantic_reviewer_result": review(
                "query_summary", verdict="repair", semantic_errors=["合成内容修订要求"],
                repair_instruction="请按证据修订内容")}
        reviewed.append(True)
        return original(**kwargs)
    monkeypatch.setattr(brain, "review_brain_reply_semantics", invoke)
    result, calls = run_entry(monkeypatch, rows=[], make_plan=make_plan,
                             semantic=failure, raw_query=query())
    assert not result.get("adoptable"), result
    assert len(generated) == (2 if after_repair else 1), calls
    key = "repaired_quality_gate_v2" if after_repair else "quality_gate_v2"
    saved = result[key]
    assert saved["error_kind"] == kind and saved["reason"] == error
    assert saved["elapsed_ms"] == 25001 and saved["http_status"] == 0
    assert "authority_review_invalid_contract" not in saved["errors"]
    assert result["no_visible_reply"]["class"] == classification
    assert result["no_visible_reply"]["stage"] == "semantic_reviewer"


def test_explicit_failure_with_pass_verdict_cannot_become_clearance(monkeypatch):
    result, calls = run_entry(monkeypatch, rows=[], make_plan=summary_plan, raw_query=query(),
                             semantic=review("query_summary", status="error", error="timeout", error_kind="transport"))
    assert not result.get("adoptable")
    assert len(calls) == 2
    assert result["no_visible_reply"]["class"] == "review_timeout"


@pytest.mark.parametrize("change,code,fields", [
    ({"version": "1"}, "invalid_query_schema", ["version"]),
    ({"private-customer-key": "sensitive-text"}, "invalid_query_schema", ["<unexpected_fields>"]),
    ({"budget": {"state": "not_mentioned"}}, "invalid_budget_state_or_unit", ["budget.unit"]),
    ({"budget": {"state": "private-customer-text", "unit": "private-phone"}},
     "invalid_budget_state_or_unit", ["budget.state", "budget.unit"]),
    ({"budget": {"state": [], "unit": "万元"}}, "invalid_budget_state_or_unit", ["budget.state"]),
])
def test_invalid_query_captures_bounded_fields_before_inventory(monkeypatch, change, code, fields):
    result, calls = run_entry(monkeypatch, rows=[], make_plan=summary_plan,
                             semantic=review("query_summary"), raw_query=query(**change))
    assert len(calls) == 1
    assert not result.get("adoptable")
    assert result["no_visible_reply"]["class"] == "catalog_query_invalid"
    details = result["catalog_stages"]["validation_error"]
    assert details["code"] == code and details["fields"] == fields
    assert len(details["query_sha256"]) == 64
    assert [x["field"] for x in details["field_evidence"]] == fields
    assert "private-" not in json.dumps(details) and "sensitive-text" not in json.dumps(details)
    assert result["llm_status"]["http_status"] == 200


def test_latest_review_wins_over_earlier_validation_repair_review():
    failure = reviewer.unavailable_review({"error": "wall_timeout_25s", "error_kind": "transport"})
    result = brain.classify_no_visible_reply("brain_quality_verification_failed", payload={
        "repaired_quality_gate_v2": {"ok": True, "status": "ok"},
        "quality_gate_v2": failure, "last_semantic_review_stage": "quality_gate_v2"})
    assert result["class"] == "review_timeout"


def test_disabled_required_review_does_not_request_content_repair():
    result = reviewer.review_brain_reply_semantics(settings={"semantic_reviewer_enabled": False},
        brain_input={}, evidence_pack={}, plan=fixtures.base_plan(),
        deterministic_quality={"ok": True, "authority_review_required": True})
    assert not result["ok"] and result["unavailable"] and not result["soft_pass_low_risk"]
    repair = brain.maybe_repair_brain_plan(settings={}, brain_input={}, plan=fixtures.base_plan(),
                                         quality=reviewer.semantic_review_to_quality(result))
    assert repair["status"] == "skipped" and repair["error"] == "semantic_review_requires_recovery"


def test_year_is_unsupported_without_silently_discarding_condition():
    q = query(filters=[{"field": "year", "op": "gte", "value": 2022, "source_ids": ["m1"]}])
    with pytest.raises(cq.CatalogQueryError, match="unsupported_filter_must_be_explicit"):
        cq.normalize_query(q, cq.message_sources(evidence()))
    q["filters"] = []
    q["unsupported_requirements"] = ["年份近一点"]
    assert cq.normalize_query(q, cq.message_sources(evidence()))["unsupported_requirements"] == ["年份近一点"]
