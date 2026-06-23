"""Boundary-matrix checks for WeChat AI customer-service behavior.

These checks exercise combinations that are easy to miss in normal happy-path
regression: RAG evidence versus structured authority, stale product context,
ambiguous intent keywords, and mixed unsafe requests with customer data.
They do not call WeChat and do not call an LLM provider.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from customer_intent_assist import analyze_intent  # noqa: E402
from customer_service_loop import ReplyDecision  # noqa: E402
from product_knowledge import decide_product_knowledge_reply, load_product_knowledge  # noqa: E402
from rag_answer_layer import maybe_build_rag_reply  # noqa: E402
from listen_and_reply import (  # noqa: E402
    llm_reply_allowed_for_decision,
    load_config,
    load_rules,
    parse_targets,
    process_target,
    resolve_path,
)
from apps.wechat_ai_customer_service.adapters import knowledge_loader  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402


CONFIG_PATH = APP_ROOT / "configs" / "file_transfer_smoke.example.json"
PRODUCT_KNOWLEDGE_PATH = APP_ROOT / "data" / "compiled" / "structured_compat" / "product_knowledge.example.json"
TEST_ARTIFACTS = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts"
RAG_EXPERIENCE_PROBE_MESSAGE_ID = "runtime-rag-soft"


class FakeConnector:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.sent_texts: list[str] = []

    def get_messages(self, target: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "target": target, "exact": exact, "messages": self.messages, "kwargs": kwargs}

    def send_text_and_verify(
        self,
        target: str,
        text: str,
        exact: bool = True,
        *,
        skip_send_rate_guard: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.sent_texts.append(text)
        return {"ok": True, "verified": True, "target": target, "exact": exact, "text": text, "kwargs": kwargs}


def cleanup_rag_experience_probe() -> None:
    store = RagExperienceStore()
    if not store.path.exists():
        return
    records = json.loads(store.path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        return
    filtered = [
        item
        for item in records
        if RAG_EXPERIENCE_PROBE_MESSAGE_ID not in set(item.get("message_ids", []) or [])
    ]
    store.path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_structured_quote_skips_rag_when_evidence_is_sufficient() -> None:
    original_rag_service = knowledge_loader.RagService

    class ExplodingRagService:
        def evidence(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("RAG should be skipped when structured quote evidence is sufficient")

    try:
        knowledge_loader.RagService = ExplodingRagService
        pack = knowledge_loader.build_evidence_pack("商用冰箱 BX-200 多少钱？")
    finally:
        knowledge_loader.RagService = original_rag_service

    product_ids = [item.get("id") for item in (pack.get("evidence", {}) or {}).get("products", [])]
    assert_true("commercial_fridge_bx_200" in product_ids, "structured product evidence should be present")
    rag = pack.get("rag_evidence", {}) or {}
    assert_true(rag.get("skipped") is True, "RAG should be marked skipped for sufficient structured quote evidence")
    assert_equal(rag.get("reason"), "structured_evidence_sufficient", "RAG skip reason")


def check_rag_only_hit_cannot_authorize_unknown_business_reply() -> None:
    original_rag_service = knowledge_loader.RagService

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "hits": [
                    {
                        "chunk_id": "rag_unknown_color",
                        "source_id": "source_unknown",
                        "score": 0.99,
                        "text": "老板喜欢蓝色包装，但这不是正式销售政策。",
                        "category": "raw_chat",
                    }
                ],
                "confidence": 0.99,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        knowledge_loader.RagService = FakeRagService
        pack = knowledge_loader.build_evidence_pack("你们老板喜欢什么颜色的包装？")
    finally:
        knowledge_loader.RagService = original_rag_service

    rag = pack.get("rag_evidence", {}) or {}
    safety = pack.get("safety", {}) or {}
    assert_true(rag.get("hits"), "RAG should be able to surface weak source evidence")
    assert_true(rag.get("rag_can_authorize") is False, "RAG must remain evidence-only")
    assert_true(safety.get("must_handoff") is True, "RAG-only unknown business fact should still hand off")
    assert_true(safety.get("allowed_auto_reply") is False, "RAG-only unknown business fact should not auto reply")


def check_explicit_product_overrides_stale_context_product() -> None:
    knowledge = load_product_knowledge(PRODUCT_KNOWLEDGE_PATH)
    products = [item for item in knowledge.get("products", []) or [] if isinstance(item, dict)]
    assert_true(len(products) >= 2, "product_knowledge fixture should provide at least two products")

    explicit_product = None
    explicit_alias = ""
    for product in products:
        aliases = [str(product.get("name") or "").strip(), *[str(value).strip() for value in product.get("aliases", []) or []]]
        alias = next((value for value in aliases if value), "")
        if alias:
            explicit_product = product
            explicit_alias = alias
            break
    assert_true(bool(explicit_product and explicit_alias), "fixture should include a product with at least one alias")

    stale_product = next((item for item in products if item.get("id") != explicit_product.get("id")), None)
    assert_true(bool(stale_product), "fixture should include a second product for stale context checks")

    result = decide_product_knowledge_reply(
        f"{explicit_alias}多少钱？",
        knowledge,
        context={
            "last_product_id": str(stale_product.get("id") or ""),
            "last_product_name": str(stale_product.get("name") or ""),
        },
    )
    assert_true(result.get("matched") is True, "explicit product should match")
    assert_equal(result.get("product_id"), explicit_product.get("id"), "explicit product should override stale context")
    assert_true(result.get("context_used") is False, "explicit product should not be treated as context hit")


def check_warranty_duration_preempts_logistics_duration_keyword() -> None:
    result = analyze_intent("商用冰箱保修多久？坏了怎么办？")
    assert_equal(result.intent, "after_sales_policy", "warranty duration should classify as after-sales")
    assert_equal(result.recommended_action, "answer_after_sales_policy", "warranty action should be after-sales")


def check_scene_product_request_is_not_customer_data() -> None:
    config = load_test_config()
    rules = load_rules(resolve_path(config.get("rules_path")))
    target = parse_targets(config)[0]
    knowledge_path = resolve_path((config.get("product_knowledge", {}) or {}).get("path"))
    knowledge = load_product_knowledge(knowledge_path)
    products = [item for item in knowledge.get("products", []) or [] if isinstance(item, dict)]
    assert_true(bool(products), "product_knowledge fixture should include products")
    scene_product = products[0]
    scene_name = str(scene_product.get("name") or "")
    scene_aliases = [scene_name, *[str(value) for value in scene_product.get("aliases", []) or []]]
    scene_alias = next((value for value in scene_aliases if value), scene_name)
    assert_true(bool(scene_alias), "scene product should expose a usable alias")
    expected_term = (scene_name or scene_alias)[:4]
    assert_true(bool(expected_term), "scene product should expose an expected reply term")

    connector = FakeConnector(
        [
            {
                "id": "scene-product",
                "type": "text",
                "content": f"我想看{scene_alias}，主要家用通勤，有什么推荐思路？",
                "sender": "self",
            }
        ]
    )
    event = process_target(
        connector=connector,
        target=target,
        config=config,
        rules=rules,
        state={},
        send=True,
        write_data=True,
        allow_fallback_send=False,
        mark_dry_run=False,
    )
    assert_equal(event.get("action"), "sent", "scene product request should receive an auto reply")
    assert_equal(event.get("decision", {}).get("rule_name"), "product_knowledge", "scene product should use product knowledge")
    intent = str(event.get("intent_assist", {}).get("intent") or "")
    assert_true(intent in {"product_detail", "unknown"}, f"scene product should keep a non-data intent, got {intent!r}")
    assert_true(event.get("data_capture", {}).get("is_customer_data") is False, "scene request should not be customer data")
    assert_true(expected_term in str(event.get("decision", {}).get("reply_text") or ""), "reply should reference the matched product")


def check_small_talk_auto_replies_without_handoff() -> None:
    config = load_test_config()
    rules = load_rules(resolve_path(config.get("rules_path")))
    target = parse_targets(config)[0]
    connector = FakeConnector(
        [
            {
                "id": "small-talk",
                "type": "text",
                "content": "哈哈我先随便看看，你们客服回复还挺快的",
                "sender": "self",
            }
        ]
    )
    event = process_target(
        connector=connector,
        target=target,
        config=config,
        rules=rules,
        state={},
        send=True,
        write_data=True,
        allow_fallback_send=False,
        mark_dry_run=False,
    )
    assert_equal(event.get("action"), "sent", "light small talk should not force handoff")
    assert_equal(event.get("decision", {}).get("rule_name"), "small_talk", "small talk rule should answer")
    assert_equal(event.get("intent_assist", {}).get("intent"), "small_talk", "small talk should audit as small talk")
    assert_true("慢慢看" in str(event.get("decision", {}).get("reply_text") or ""), "small talk reply should be conversational")


def check_complete_customer_data_with_unsafe_discount_handoffs_without_writing() -> None:
    config = load_test_config()
    rules = load_rules(resolve_path(config.get("rules_path")))
    target = parse_targets(config)[0]
    workbook_path = resolve_path(config.get("data_capture", {}).get("workbook_path"))
    remove_file(workbook_path)

    connector = FakeConnector(
        [
            {
                "id": "mixed-discount",
                "type": "text",
                "content": "我买 7 台商用冰箱，直接按 20 台价格可以吗？",
                "sender": "self",
            },
            {
                "id": "mixed-data",
                "type": "text",
                "content": "客户资料\n姓名：边界测试\n电话：13800008888\n地址：上海市浦东新区测试路 18 号\n产品：商用冰箱\n数量：7台",
                "sender": "self",
            },
        ]
    )
    state: dict[str, Any] = {}
    event = process_target(
        connector=connector,
        target=target,
        config=config,
        rules=rules,
        state=state,
        send=True,
        write_data=True,
        allow_fallback_send=False,
        mark_dry_run=False,
    )
    data_capture = event.get("data_capture", {}) or {}
    assert_equal(event.get("action"), "handoff_sent", "unsafe discount plus data should send handoff")
    assert_true(data_capture.get("complete") is True, "customer data may be complete")
    assert_true(not data_capture.get("write_result", {}).get("ok"), "complete data must not be written during handoff")
    assert_true(not workbook_path.exists(), "workbook should not be created for handoff-blocked write")
    # This legacy non-Brain fixture only proves that unsafe data is not written.
    # In Brain First mode, customer-visible handoff wording must be authored by
    # customer_service_brain; local/guard acknowledgement text is not required
    # here and must not be treated as the acceptance baseline.
    if connector.sent_texts:
        assert_true(
            all("转人工" not in text and "人工客服" not in text for text in connector.sent_texts),
            "legacy handoff text, when present, must not expose internal transfer wording",
        )


def check_rag_hits_are_summarized_in_intent_context_only_as_sources() -> None:
    original_rag_service = knowledge_loader.RagService

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "hits": [
                    {
                        "chunk_id": "rag_scene_001",
                        "source_id": "scene_doc",
                        "score": 0.88,
                        "text": "便利店饮料冷藏场景通常会考虑商用冰箱或冷柜。",
                        "category": "product_explanations",
                    }
                ],
                "confidence": 0.88,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        knowledge_loader.RagService = FakeRagService
        pack = knowledge_loader.build_evidence_pack("我开便利店，想找个放饮料的冷柜，有哪些推荐？")
    finally:
        knowledge_loader.RagService = original_rag_service

    rag = pack.get("rag_evidence", {}) or {}
    product_ids = [item.get("id") for item in (pack.get("evidence", {}) or {}).get("products", [])]
    assert_true("commercial_fridge_bx_200" in product_ids, "structured product should still be found for scene query")
    assert_true(rag.get("hits"), "scene query should allow RAG source snippets")
    assert_true(rag.get("structured_priority") is True, "RAG context should preserve structured priority")
    assert_true(rag.get("rag_can_authorize") is False, "RAG context should not grant authority")


def check_soft_rag_reference_can_clear_no_business_handoff() -> None:
    original_rag_service = knowledge_loader.RagService

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "hits": [
                    {
                        "chunk_id": "rag_soft_spec",
                        "source_id": "source_soft_spec",
                        "score": 0.88,
                        "text": "型号命名资料里通常会把容量、供电规格和适用场景放在型号说明附近，适合先作为选型参考。",
                        "category": "product_explanations",
                        "source_type": "product_doc",
                        "risk_terms": [],
                    }
                ],
                "confidence": 0.88,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        knowledge_loader.RagService = FakeRagService
        pack = knowledge_loader.build_evidence_pack("型号命名一般怎么看，我想先了解一下？")
    finally:
        knowledge_loader.RagService = original_rag_service

    rag = pack.get("rag_evidence", {}) or {}
    safety = pack.get("safety", {}) or {}
    assert_true(rag.get("hits"), "soft spec question should retrieve RAG evidence")
    assert_true(safety.get("rag_soft_reference_allowed") is True, "safe RAG should be allowed as soft reference")
    assert_true(safety.get("must_handoff") is False, "safe soft RAG evidence should clear no-business handoff")


def check_soft_installation_reference_can_use_rag_without_handoff() -> None:
    original_rag_service = knowledge_loader.RagService

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "hits": [
                    {
                        "chunk_id": "soft_installation_reference",
                        "source_id": "soft_installation_source",
                        "score": 0.88,
                        "text": "安装前建议确认门厚、开孔尺寸、开门方向和供电方式。",
                        "category": "product_explanations",
                        "source_type": "product_doc",
                        "product_id": "fl-920",
                        "risk_terms": [],
                    }
                ],
                "confidence": 0.88,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        knowledge_loader.RagService = FakeRagService
        pack = knowledge_loader.build_evidence_pack("民宿客房智能锁安装前要不要确认供电？")
    finally:
        knowledge_loader.RagService = original_rag_service
    safety = pack.get("safety", {})
    assert_true(safety.get("must_handoff") is False, "soft installation reference should not force handoff")
    assert_true(
        safety.get("rag_soft_installation_reference_allowed") is True,
        "soft installation allowance should be explicit",
    )


def check_rag_answer_layer_applies_to_soft_scene_evidence() -> None:
    config = load_test_config()
    decision = ReplyDecision(
        reply_text="默认回复",
        rule_name=None,
        matched=False,
        need_handoff=True,
        reason="no_rule_matched",
    )
    intent_assist = {
        "intent": "product_detail",
        "recommended_action": "answer_from_evidence",
        "evidence": {
            "intent_tags": ["scene_product"],
            "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
            "rag_hits": [
                {
                    "chunk_id": "rag_soft_scene",
                    "source_id": "source_scene",
                    "score": 0.86,
                    "text": "员工茶水间临时饮品冷藏场景，重点是容量、静音和取放便利，不建议直接承诺特殊时效。",
                    "category": "product_explanations",
                    "source_type": "product_doc",
                    "risk_terms": [],
                }
            ],
        },
    }
    result = maybe_build_rag_reply(
        config=config,
        text="员工茶水间临时放饮品，有什么选择思路？",
        decision=decision,
        reply_text="默认回复",
        intent_assist=intent_assist,
        product_knowledge={"matched": False},
        data_capture={"is_customer_data": False},
    )
    assert_true(result.get("applied") is True, "safe soft RAG scene should be replyable")
    assert_equal(result.get("rule_name"), "rag_context_reply", "RAG reply rule name")
    assert_true("茶水间" in str(result.get("raw_reply_text") or ""), "reply should quote the safe RAG snippet")
    assert_true(result.get("needs_handoff") is False, "safe RAG scene should not hand off")


def check_process_target_applies_safe_rag_reply_before_handoff() -> None:
    config = load_test_config()
    config.setdefault("product_knowledge", {})["enabled"] = False
    rules = {"default_reply": "默认回复", "rules": []}
    target = parse_targets(config)[0]
    cleanup_rag_experience_probe()
    connector = FakeConnector(
        [
            {
                "id": RAG_EXPERIENCE_PROBE_MESSAGE_ID,
                "type": "text",
                "content": "型号命名一般怎么看，我想先了解一下？",
                "sender": "self",
            }
        ]
    )
    build_evidence_globals = process_target.__globals__["build_evidence_pack"].__globals__
    original_rag_service = build_evidence_globals["RagService"]

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "hits": [
                    {
                        "chunk_id": "runtime_rag_soft_spec",
                        "source_id": "runtime_source_soft_spec",
                        "score": 0.9,
                        "text": "型号命名资料里通常会把容量、供电规格和适用场景放在型号说明附近，适合先作为选型参考。",
                        "category": "product_explanations",
                        "source_type": "product_doc",
                        "risk_terms": [],
                    }
                ],
                "confidence": 0.9,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        build_evidence_globals["RagService"] = FakeRagService
        event = process_target(
            connector=connector,
            target=target,
            config=config,
            rules=rules,
            state={},
            send=True,
            write_data=True,
            allow_fallback_send=False,
            mark_dry_run=False,
        )
    finally:
        build_evidence_globals["RagService"] = original_rag_service
        cleanup_rag_experience_probe()

    assert_equal(event.get("action"), "sent", "safe RAG reply should be sent before fallback handoff")
    assert_equal(event.get("decision", {}).get("rule_name"), "rag_context_reply", "runtime should use RAG answer layer")
    assert_true((event.get("rag_reply", {}) or {}).get("applied") is True, "RAG answer layer should be applied")
    assert_true(event.get("rag_experience", {}).get("experience_id"), "sent RAG reply should record an experience")
    assert_true("型号命名" in str(event.get("decision", {}).get("reply_text") or ""), "reply should include RAG reference")


def check_rag_answer_layer_blocks_authority_or_risk_terms() -> None:
    config = load_test_config()
    decision = ReplyDecision(
        reply_text="默认回复",
        rule_name=None,
        matched=False,
        need_handoff=True,
        reason="no_rule_matched",
    )
    intent_assist = {
        "intent": "product_detail",
        "recommended_action": "answer_from_evidence",
        "evidence": {
            "intent_tags": ["scene_product"],
            "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
            "rag_hits": [
                {
                    "chunk_id": "rag_risky",
                    "source_id": "source_risky",
                    "score": 0.91,
                    "text": "老客户可以谈月结账期，必要时先发货。",
                    "category": "raw_chat",
                    "source_type": "chat_log",
                    "risk_terms": ["账期"],
                }
            ],
        },
    }
    result = maybe_build_rag_reply(
        config=config,
        text="这个客户能不能先发货月底结？",
        decision=decision,
        reply_text="默认回复",
        intent_assist=intent_assist,
        product_knowledge={"matched": False},
        data_capture={"is_customer_data": False},
    )
    assert_true(result.get("applied") is False, "RAG hit with risk terms must not become a reply")

    scoped = dict(config)
    scoped["rag_response"] = {
        **(config.get("rag_response", {}) or {}),
        "allowed_source_types": ["product_doc"],
    }
    scoped_result = maybe_build_rag_reply(
        config=scoped,
        text="型号命名一般怎么看？",
        decision=ReplyDecision(
            reply_text="默认回复",
            rule_name=None,
            matched=False,
            need_handoff=False,
            reason="no_rule_matched",
        ),
        reply_text="默认回复",
        intent_assist={
            "intent": "product_detail",
            "recommended_action": "answer_from_evidence",
            "evidence": {
                "intent_tags": ["scene_product"],
                "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
                "rag_hits": [
                    {
                        "chunk_id": "out_of_scope_risky_upload",
                        "source_id": "upload_source",
                        "score": 0.99,
                        "text": "Out-of-scope upload mentions an installation fee.",
                        "category": "products",
                        "source_type": "upload",
                        "risk_terms": ["manual_risk"],
                    },
                    {
                        "chunk_id": "safe_product_doc",
                        "source_id": "doc_source",
                        "score": 0.8,
                        "text": "Product docs put capacity, power requirements, and scenarios near the model description for reference.",
                        "category": "product_explanations",
                        "source_type": "product_doc",
                        "risk_terms": [],
                    },
                ],
            },
        },
        product_knowledge={"matched": False},
        data_capture={"is_customer_data": False},
    )
    assert_true(scoped_result.get("applied") is True, "out-of-scope risky upload should not block safe scoped RAG")
    assert_equal(scoped_result.get("hit", {}).get("source_id"), "doc_source", "safe scoped RAG source should be selected")


def check_rag_answer_layer_preserves_structured_product_reply_by_default() -> None:
    config = load_test_config()
    decision = ReplyDecision(
        reply_text="商用冰箱 BX-200 单价 999 元/台。",
        rule_name="product_knowledge",
        matched=True,
        need_handoff=False,
        reason="product_knowledge_matched",
    )
    intent_assist = {
        "intent": "quote_with_product_detail",
        "recommended_action": "quote_from_product_knowledge",
        "evidence": {
            "intent_tags": ["scene_product", "quote"],
            "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
            "rag_hits": [
                {
                    "chunk_id": "rag_scene",
                    "source_id": "source_scene",
                    "score": 0.82,
                    "text": "旧聊天里提到类似场景，但价格以商品主档为准。",
                    "category": "raw_chat",
                    "source_type": "chat_log",
                    "risk_terms": [],
                }
            ],
        },
    }
    result = maybe_build_rag_reply(
        config=config,
        text="便利店冷柜多少钱？",
        decision=decision,
        reply_text="[BoundaryTest] 商用冰箱 BX-200 单价 999 元/台。",
        intent_assist=intent_assist,
        product_knowledge={"matched": True, "product_id": "commercial_fridge_bx_200"},
        data_capture={"is_customer_data": False},
    )
    assert_true(result.get("applied") is False, "structured product reply should keep priority over RAG by default")


def check_llm_gate_allows_small_talk_candidate_without_business_evidence() -> None:
    config = load_test_config()
    settings = ((config.get("intent_assist", {}) or {}).get("llm_advisory", {}) or {})
    settings["apply_to_small_talk"] = True
    candidate = {
        "intent": "small_talk",
        "recommended_action": "reply_small_talk",
    }
    decision = ReplyDecision(
        reply_text="没问题，您先慢慢看。",
        rule_name="small_talk",
        matched=True,
        need_handoff=False,
        reason="keyword_rule_matched",
    )
    allowed = llm_reply_allowed_for_decision(
        settings,
        candidate,
        decision,
        product_knowledge={"matched": False},
        intent_assist={"evidence": {"intent_tags": ["small_talk"], "safety": {"must_handoff": False}}},
    )
    assert_true(allowed is True, "LLM small-talk humanization should be allowed without business evidence")


def load_test_config() -> dict[str, Any]:
    config = copy.deepcopy(load_config(CONFIG_PATH))
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("reply", {})["prefix"] = "[BoundaryTest] "
    config.setdefault("data_capture", {})["workbook_path"] = str(
        TEST_ARTIFACTS / "boundary_matrix_customer_leads.xlsx"
    )
    return config


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


CHECKS = [
    check_structured_quote_skips_rag_when_evidence_is_sufficient,
    check_rag_only_hit_cannot_authorize_unknown_business_reply,
    check_explicit_product_overrides_stale_context_product,
    check_warranty_duration_preempts_logistics_duration_keyword,
    check_scene_product_request_is_not_customer_data,
    check_small_talk_auto_replies_without_handoff,
    check_complete_customer_data_with_unsafe_discount_handoffs_without_writing,
    check_rag_hits_are_summarized_in_intent_context_only_as_sources,
    check_soft_rag_reference_can_clear_no_business_handoff,
    check_soft_installation_reference_can_use_rag_without_handoff,
    check_rag_answer_layer_applies_to_soft_scene_evidence,
    check_process_target_applies_safe_rag_reply_before_handoff,
    check_rag_answer_layer_blocks_authority_or_risk_terms,
    check_rag_answer_layer_preserves_structured_product_reply_by_default,
    check_llm_gate_allows_small_talk_candidate_without_business_evidence,
]


if __name__ == "__main__":
    raise SystemExit(main())
