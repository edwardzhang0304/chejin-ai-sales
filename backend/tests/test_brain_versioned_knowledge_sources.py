"""Artificial fixtures exercise production knowledge projection and citation checks.

The HTTP integration reuses real C3/SQLite/Brain subprocesses. Only the external
model response is controlled; it echoes source_id from the actual received prompt.
"""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

import test_brain_fact_guidance_regression as regression
from app.services.knowledge_management_service import build_retrieval_index
from apps.wechat_ai_customer_service.workflows.customer_service_brain import (
    collect_formal_ids, validate_plan_against_evidence,
)
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import apply_chejin_knowledge_release


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _project(revision="revision-1"):
    items = [{"item_id": "item-1", "revision_id": revision, "title": "购车需求收集", "content": "可以询问购车预算和付款偏好，不承诺审批结果。"}]
    index = build_retrieval_index(items)
    target = {"chejin_knowledge_required": True, "chejin_knowledge_release": {
        "release_id": "release-" + revision, "version": "KR-TEST-" + revision,
        "items": items, "snapshot_sha256": _digest(items), "retrieval_index": index,
        "retrieval_index_sha256": _digest(index),
    }}
    pack = {}
    apply_chejin_knowledge_release(pack, target, query_text="购车需求")
    assert len(pack["knowledge"]["formal_knowledge"]["faq"]) == 1
    return pack


@pytest.mark.parametrize("with_policy_fact", [False, True])
def test_exact_projected_source_is_accepted_without_dropping_revision(with_policy_fact):
    pack = _project()
    source = pack["knowledge"]["formal_knowledge"]["faq"][0]["source_id"]
    assert source == "knowledge:item-1@revision-1"
    assert source in collect_formal_ids(pack)
    plan = regression.make_plan(evidence={"formal_knowledge_ids": [source]})
    if with_policy_fact:
        plan["facts_claimed"] = [{"fact_type": "policy", "value": "不承诺审批结果", "source_level": "formal_knowledge", "source_id": source}]
    original = deepcopy(plan)
    assert validate_plan_against_evidence(plan, pack)["ok"]
    assert plan == original


@pytest.mark.parametrize("source", [
    "knowledge:item-1@revision-0", "knowledge:other-item@revision-1", "knowledge:item-1",
])
@pytest.mark.parametrize("with_policy_fact", [False, True])
def test_invalid_versioned_sources_are_rejected(source, with_policy_fact):
    pack = _project()
    plan = regression.make_plan(evidence={"formal_knowledge_ids": [source]})
    if with_policy_fact:
        plan["facts_claimed"] = [{"fact_type": "policy", "value": "测试声明", "source_level": "formal_knowledge", "source_id": source}]
    result = validate_plan_against_evidence(plan, pack)
    assert f"formal_knowledge_source_not_in_evidence:{source}" in result["errors"]
    if with_policy_fact:
        assert f"policy_fact_source_not_in_evidence:{source}" in result["errors"]


def test_each_batch_only_accepts_its_own_immutable_revision():
    old_pack, new_pack = _project("revision-1"), _project("revision-2")
    old_plan = regression.make_plan(evidence={"formal_knowledge_ids": ["knowledge:item-1@revision-1"]})
    new_plan = regression.make_plan(evidence={"formal_knowledge_ids": ["knowledge:item-1@revision-2"]})
    assert validate_plan_against_evidence(old_plan, old_pack)["ok"]
    assert validate_plan_against_evidence(new_plan, new_pack)["ok"]
    assert not validate_plan_against_evidence(old_plan, new_pack)["ok"]
    assert not validate_plan_against_evidence(new_plan, old_pack)["ok"]


def test_same_source_in_rag_does_not_become_formal_authority():
    source = "knowledge:item-1@revision-1"
    plan = regression.make_plan(evidence={"formal_knowledge_ids": [source]})
    pack = {"rag": {"hits": [{"source_id": source}]}, "audit_summary": {"evidence_ids": [source]}}
    assert not validate_plan_against_evidence(plan, pack)["ok"]


@pytest.mark.parametrize("bucket", ["faq", "product_scoped", "policies"])
def test_formal_source_id_is_recognized_in_each_supported_bucket(bucket):
    item = {"id": "item-1", "source_id": "knowledge:item-1@revision-1"}
    payload = {"policy-1": item} if bucket == "policies" else [item]
    pack = {"knowledge": {"formal_knowledge": {bucket: payload}}}
    plan = regression.make_plan(evidence={"formal_knowledge_ids": [item["source_id"]]})
    assert validate_plan_against_evidence(plan, pack)["ok"]


@pytest.mark.parametrize("citation_format", ["source_id", "id", "wrong_revision", "unknown_item", "mixed_revision", "unversioned"])
def test_provider_echoed_citation_reaches_real_c3_only_if_valid(monkeypatch, citation_format):
    regression.test_c3_api_database_real_brain_and_provider_preserve_guided_reply(
        monkeypatch, 0, False, expect_reply=citation_format in {"source_id", "id"},
        citation_format=citation_format, expected_validation_error="formal_knowledge_source_not_in_evidence",
    )
