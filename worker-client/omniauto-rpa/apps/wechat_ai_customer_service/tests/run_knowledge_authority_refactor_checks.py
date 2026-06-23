"""Regression checks for product-master/formal/common-sense authority layering."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evidence_authority import (  # noqa: E402
    FORMAL_KNOWLEDGE,
    AI_EXPERIENCE_POOL,
    LLM_COMMON_SENSE,
    PRODUCT_MASTER,
    authority_order_payload,
    classify_evidence,
)
from llm_common_sense_layer import build_common_sense_guidance  # noqa: E402
from llm_reply_guard import guard_synthesized_reply  # noqa: E402
from llm_reply_synthesis import slim_realtime_evidence_pack  # noqa: E402
from reply_evidence_builder import compact_knowledge_pack  # noqa: E402


def main() -> int:
    results = [
        check_authority_classification(),
        check_common_sense_layer_is_non_authoritative(),
        check_compact_evidence_exposes_authority_layers(),
        check_guard_blocks_unsupported_price_fact(),
        check_guard_allows_product_master_price_fact(),
        check_realtime_slim_pack_keeps_authority_layers(),
        check_router_has_no_concrete_vehicle_hardcodes(),
    ]
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_authority_classification() -> dict[str, Any]:
    order = authority_order_payload()
    ok = (
        classify_evidence({"category_id": "products", "_knowledge_layer": "product_master"}) == PRODUCT_MASTER
        and classify_evidence({"category_id": "faq", "_knowledge_layer": "tenant"}) == FORMAL_KNOWLEDGE
        and classify_evidence({"chunk_id": "rag_1", "source_type": "rag"}) == AI_EXPERIENCE_POOL
        and classify_evidence({"source_type": "llm_common_sense"}) == LLM_COMMON_SENSE
        and order[0]["level"] == PRODUCT_MASTER
        and order[0].get("can_authorize_product_master_facts") is True
        and order[1].get("can_authorize_product_facts") is False
        and order[1].get("can_authorize_product_scoped_rules") is True
    )
    return {"name": "authority_classification", "ok": ok, "order": order[:3]}


def check_common_sense_layer_is_non_authoritative() -> dict[str, Any]:
    guidance = build_common_sense_guidance(customer_message="我预算12万，家用接娃，轿车和SUV哪个更适合？")
    fragment = guidance.to_prompt_fragment()
    text = json.dumps(fragment, ensure_ascii=False)
    forbidden = ["12万现车", "表显12万", "检测结论为"]
    ok = (
        fragment.get("non_authoritative") is True
        and "product_master" in fragment.get("must_defer_to", [])
        and not any(term in text for term in forbidden)
    )
    return {"name": "common_sense_layer_is_non_authoritative", "ok": ok, "fragment": fragment}


def check_compact_evidence_exposes_authority_layers() -> dict[str, Any]:
    pack = {
        "intent_tags": ["product", "catalog"],
        "selected_items": [{"id": "faq:test", "summary": "formal"}],
        "evidence": {
            "products": [{"id": "p1", "category_id": "products", "data": {"name": "测试商品A", "price": 12.8}}],
            "faq": [{"id": "f1", "intent": "faq_test", "answer": "正式知识"}],
            "policies": {"visit": "到店前先沟通"},
            "product_scoped": [{"id": "ps1", "category_id": "product_faq", "answer": "商品专属知识"}],
            "style_examples": [{"id": "s1", "reply": "短句"}],
        },
            "rag_evidence": {"hits": [{"chunk_id": "r1", "text": "历史经验", "source_type": "rag_experience", "category": "rag_experience"}], "ok": True},
        "safety": {},
    }
    compact = compact_knowledge_pack(
        "我想看测试商品A",
        pack,
        max_rag_hits=2,
        max_rag_text_chars=100,
        max_catalog_candidates=2,
    )
    ok = (
        compact["product_master"]["can_authorize_product_facts"] is True
        and compact["formal_knowledge"]["can_authorize_product_facts"] is False
        and compact["ai_experience_pool"]["can_authorize_reply_content"] is False
        and compact["ai_experience_pool"]["excluded_hit_count"] == 1
        and not compact["rag_evidence"]["hits"]
        and compact["product_master"]["items"][0]["authority_level"] == PRODUCT_MASTER
    )
    return {"name": "compact_evidence_exposes_authority_layers", "ok": ok, "compact_keys": sorted(compact.keys())}


def check_guard_blocks_unsupported_price_fact() -> dict[str, Any]:
    guard = guard_synthesized_reply(
        candidate={
            "reply": "这台可以按12.8万给您看，车况也透明。",
            "confidence": 0.9,
            "recommended_action": "send_reply",
            "used_evidence": ["rag:r1"],
            "rag_used": True,
        },
        evidence_pack={"current_message": "我想买个通勤车", "knowledge": {"evidence": {}}, "intent_tags": []},
        settings={"require_evidence": False},
    )
    ok = guard.get("action") == "repair" and guard.get("reason") == "unsupported_price_without_product_master"
    return {"name": "guard_blocks_unsupported_price_fact", "ok": ok, "guard": guard}


def check_guard_allows_product_master_price_fact() -> dict[str, Any]:
    evidence_pack = {
        "current_message": "这台多少钱？",
        "knowledge": {
            "evidence": {
                "products": [{"id": "p1", "name": "测试商品A", "price": 12.8, "authority_level": PRODUCT_MASTER}],
            },
            "product_master": {"items": [{"id": "p1", "name": "测试商品A", "price": 12.8}]},
        },
        "intent_tags": [],
        "audit_summary": {"evidence_ids": ["product:p1"], "structured_evidence_count": 1},
    }
    guard = guard_synthesized_reply(
        candidate={
            "reply": "这台测试商品A是12.8万，具体以商品库和到店核实为准。",
            "confidence": 0.9,
            "recommended_action": "send_reply",
            "used_evidence": ["product:p1"],
            "structured_used": True,
        },
        evidence_pack=evidence_pack,
        settings={"require_evidence": True},
    )
    ok = guard.get("action") == "send_reply"
    return {"name": "guard_allows_product_master_price_fact", "ok": ok, "guard": guard}


def check_realtime_slim_pack_keeps_authority_layers() -> dict[str, Any]:
    evidence_pack = {
        "schema_version": 1,
        "target": "文件传输助手",
        "current_message": "怎么选",
        "authority_order": authority_order_payload(),
        "common_sense": build_common_sense_guidance(customer_message="怎么选").to_prompt_fragment(),
        "conversation": {"history": [], "history_text": "", "current_batch_text": "", "conversation_summary": ""},
        "knowledge": {
            "authority_order": authority_order_payload(),
            "intent_tags": ["catalog"],
            "evidence": {"products": [], "catalog_candidates": [], "faq": [], "product_scoped": [], "style_examples": []},
            "product_master": {"items": []},
            "formal_knowledge": {},
            "rag_experience": {"style_only": True},
            "rag_evidence": {"hits": []},
            "safety": {},
        },
        "safety": {},
        "intent_tags": ["catalog"],
        "audit_summary": {},
    }
    slim = slim_realtime_evidence_pack(evidence_pack, settings={"max_catalog_candidates": 3, "max_rag_hits": 2})
    ok = bool(slim.get("authority_order")) and bool(slim.get("common_sense")) and "product_master" in slim.get("knowledge", {})
    return {"name": "realtime_slim_pack_keeps_authority_layers", "ok": ok, "slim_keys": sorted(slim.keys())}


def check_router_has_no_concrete_vehicle_hardcodes() -> dict[str, Any]:
    source = (WORKFLOWS_ROOT / "realtime_reply_router.py").read_text(encoding="utf-8")
    forbidden = ["凯美瑞", "雅阁", "奇骏", "昂克赛拉", "高尔夫", "哈弗", "途观", "宝马", "奔驰", "奥迪", "丰田", "本田", "比亚迪", "大众"]
    leaked = [term for term in forbidden if term in source]
    return {"name": "router_has_no_concrete_vehicle_hardcodes", "ok": not leaked, "leaked": leaked}


if __name__ == "__main__":
    raise SystemExit(main())
