"""Focused checks for knowledge contamination prevention.

These tests lock the intended safety contract:
- customer-service live observations are audit data, not automatic training data;
- self/file-transfer/test messages never create learning batches;
- raw WeChat batches create review-only RAG experiences, not direct RAG chunks;
- RAG retrieval ignores raw WeChat chunks and product-master facts.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


os.environ.setdefault("WECHAT_STORAGE_BACKEND", "file")
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.raw_message_learning_service import (  # noqa: E402
    RawMessageLearningService,
)
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.conversation_history import assemble_conversation_history  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import text_has_test_marker  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.candidate_store import CandidateStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.rag_admin_service import build_candidate_from_experience  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    tenant_context,
    tenant_knowledge_base_root,
    tenant_review_candidates_root,
    tenant_root,
    tenant_runtime_root,
)
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402


TEST_TENANT = "knowledge_contamination_guard_test"


def main() -> int:
    cleanup()
    results = []
    try:
        with tenant_context(TEST_TENANT):
            prepare_isolated_formal_knowledge_root()
            for check in CHECKS:
                try:
                    check()
                    results.append({"name": check.__name__, "ok": True})
                except Exception as exc:
                    results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
                    break
    finally:
        cleanup()
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_customer_service_file_transfer_never_batches() -> None:
    store = RawMessageStore()
    result = store.upsert_messages(
        {
            "target_name": "文件传输助手",
            "display_name": "文件传输助手",
            "conversation_type": "file_transfer",
            "learning_enabled": True,
            "allow_learning_from_customer_service": True,
        },
        [
            {
                "id": "ft-001",
                "type": "text",
                "sender": "self",
                "content": "我看秦PLUS，每天通勤40公里，三电能保证吗？(LIVEFLOW_20260519_GUARD)",
                "time": "2026-05-19 10:00:00",
            }
        ],
        source_module="customer_service",
        learning_enabled=True,
        batch_reason="customer_service_poll",
    )
    assert_equal(result.get("batch"), None, "file-transfer self-test should not create a learning batch")
    messages = store.list_messages(limit=10)
    assert_true(messages and messages[0].get("learning_enabled") is False, "captured message should be non-learnable")
    assert_equal(messages[0].get("excluded_reason"), "file_transfer_test_channel", "exclusion reason should be explicit")


def check_customer_service_private_defaults_to_record_only() -> None:
    store = RawMessageStore()
    result = store.upsert_messages(
        {
            "target_name": "真实客户A",
            "display_name": "真实客户A",
            "conversation_type": "private",
            "learning_enabled": True,
        },
        [
            {
                "id": "private-001",
                "type": "text",
                "sender": "客户",
                "content": "我想找一台七座车，预算十五万以内。",
                "time": "2026-05-19 10:01:00",
            }
        ],
        source_module="customer_service",
        learning_enabled=True,
        batch_reason="customer_service_poll",
    )
    assert_equal(result.get("batch"), None, "customer service listener should not auto-learn without explicit source opt-in")
    messages = store.list_messages(query="七座车", limit=5)
    assert_true(messages and messages[0].get("learning_enabled") is False, "private service message should be record-only by default")
    assert_equal(messages[0].get("excluded_reason"), "customer_service_live_learning_disabled", "private exclusion reason")


def check_smart_recorder_review_only_learning() -> None:
    store = RawMessageStore()
    result = store.upsert_messages(
        {
            "target_name": "门店真实记录群",
            "display_name": "门店真实记录群",
            "conversation_type": "group",
            "learning_enabled": True,
            "selected_by_user": True,
            "source": {"type": "smart_recorder"},
        },
        [
            {
                "id": "rec-001",
                "type": "text",
                "sender": "销售小王",
                "content": "客户问置换时，先收车型、上牌年份、公里数、城市和车况照片，再说估价需要复核。",
                "time": "2026-05-19 10:02:00",
            }
        ],
        source_module="smart_recorder",
        learning_enabled=True,
        batch_reason="recorder_capture",
    )
    assert_true(result.get("batch"), "trusted smart recorder message should create a batch")
    learned = RawMessageLearningService().process_batch(result["batch"]["batch_id"], use_llm=False)
    assert_true(str(learned.get("rag_experience_id") or "").startswith("rag_exp_"), "learning should create review-only RAG experience")
    status = RagService().status()
    assert_equal(status.get("source_count"), 0, "raw recorder learning should not create direct rag source")
    assert_equal(status.get("chunk_count"), 0, "raw recorder learning should not create direct rag chunk")
    experiences = RagExperienceStore().list(status="all", limit=20)
    assert_true(any(item.get("experience_id") == learned.get("rag_experience_id") for item in experiences), "RAG experience should be stored")


def check_visual_ocr_text_is_record_only_and_skipped_by_history() -> None:
    store = RawMessageStore()
    result = store.upsert_messages(
        {
            "target_name": "真实客户视觉OCR",
            "display_name": "真实客户视觉OCR",
            "conversation_type": "private",
            "learning_enabled": True,
            "selected_by_user": True,
            "source": {"type": "smart_recorder"},
        },
        [
            {
                "id": "visual-ocr-learning-1",
                "type": "text",
                "sender": "客户",
                "content": "visual direct banner XQZ917",
                "time": "2026-06-20 09:04:00",
                "source_type": "image_ocr",
                "ocr_items": [
                    {"text": "visual direct banner XQZ917", "source_type": "image_ocr"},
                ],
            },
            {
                "id": "visual-ocr-learning-nested-1",
                "type": "text",
                "sender": "客户",
                "content": "visual nested poster ZRM402",
                "time": "2026-06-20 09:07:30",
                "source": {"metadata": {"source_type": "poster_ocr"}},
            },
            {
                "id": "normal-chat-learning-1",
                "type": "text",
                "sender": "客户",
                "content": "我想了解一下置换流程。",
                "time": "2026-06-20 09:05:00",
                "source_type": "chat_text",
            },
        ],
        source_module="smart_recorder",
        learning_enabled=True,
        batch_reason="recorder_capture",
    )
    assert_true(result.get("batch"), "normal trusted recorder text should still create a review-only learning batch")
    stored = store.list_messages(query="visual", limit=5)
    assert_true(stored, "visual OCR text should remain auditable in raw messages")
    visual_records = {str(item.get("content") or ""): item for item in stored}
    direct_visual = visual_records.get("visual direct banner XQZ917")
    nested_visual = visual_records.get("visual nested poster ZRM402")
    assert_true(direct_visual is not None, "direct visual OCR record should be present")
    assert_true(nested_visual is not None, "nested visual OCR record should be present")
    assert_equal(direct_visual.get("learning_enabled"), False, "visual OCR text must be non-learnable")
    assert_equal(direct_visual.get("excluded_reason"), "visual_ocr_non_text", "visual OCR exclusion reason should be explicit")
    assert_equal(nested_visual.get("learning_enabled"), False, "nested visual OCR metadata must be non-learnable")
    assert_equal(nested_visual.get("excluded_reason"), "visual_ocr_non_text", "nested visual OCR exclusion reason should be explicit")

    history = assemble_conversation_history(
        target_name="真实客户视觉OCR",
        conversation_id=str((result.get("conversation") or {}).get("conversation_id") or ""),
        current_batch=[
            {
                "id": "visual-ocr-current-1",
                "type": "text",
                "sender": "客户",
                "content": "visual current poster YKT631",
                "quality_flags": ["visual_ocr_non_text"],
            },
            {
                "id": "normal-current-1",
                "type": "text",
                "sender": "客户",
                "content": "你好",
            },
        ],
    )
    history_text = str(history.get("history_text") or "")
    current_text = str(history.get("current_batch_text") or "")
    assert_true("visual " not in history_text + current_text, "visual OCR text should not be assembled into Brain history/current text")
    assert_true("我想了解一下置换流程" in history_text, "normal stored chat text should remain in conversation history")
    assert_true("你好" in current_text, "normal current chat text should remain in Brain current batch text")


def check_rag_retrieval_filters_raw_and_product_chunks() -> None:
    service = RagService()
    source_dir = tenant_runtime_root(TEST_TENANT) / "guard_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    direct_chat_dir = tenant_root(TEST_TENANT) / "raw_inbox" / "chats"
    direct_chat_dir.mkdir(parents=True, exist_ok=True)
    raw_path = source_dir / "raw_wechat.txt"
    product_path = source_dir / "product_master.txt"
    safe_path = source_dir / "safe_policy.txt"
    direct_chat_path = direct_chat_dir / "upload_wechat_usedcar_chat_templates_strict.jsonl"
    raw_path.write_text("客户：我看秦PLUS，每天通勤40公里，三电怎么保证？\n客服：[车金AI] 这个得转人工。\n", encoding="utf-8")
    product_path.write_text("商品资料：2022款比亚迪秦PLUS DM-i 55KM，价格8.68万，库存1台。\n", encoding="utf-8")
    safe_path.write_text("置换流程：先收车型、上牌年份、公里数、城市、车况照片，再提示最终估价需复核。\n", encoding="utf-8")
    direct_chat_path.write_text(
        "{\"customer_message\":\"库存表\",\"service_reply\":\"向您推荐{inventory_count}辆好车。\"}\n",
        encoding="utf-8",
    )
    service.ingest_file(raw_path, source_type="wechat_raw_message", category="private", rebuild_index=True)
    service.ingest_file(product_path, source_type="upload", category="products", rebuild_index=True)
    service.ingest_file(safe_path, source_type="policy_doc", category="policies", rebuild_index=True)
    service.ingest_file(direct_chat_path, source_type="upload", category="chats", rebuild_index=True)
    raw_hits = service.search("秦PLUS 每天通勤40公里 三电", limit=5).get("hits", [])
    assert_true(not any(item.get("source_type") == "wechat_raw_message" for item in raw_hits), "raw WeChat chunks should not be retrievable")
    assert_true(not any(item.get("category") == "products" for item in raw_hits), "product-master chunks should not be generic RAG hits")
    direct_chat_hits = service.search("库存表 推荐几辆车", limit=5).get("hits", [])
    assert_true(not any(item.get("category") == "chats" for item in direct_chat_hits), "direct raw_inbox chat templates should not bypass RAG experience review")
    policy_hits = service.search("置换 上牌年份 公里数 车况照片", limit=5).get("hits", [])
    assert_true(any(item.get("category") == "policies" for item in policy_hits), "safe policy RAG chunks should remain retrievable")


def check_candidate_apply_enforces_source_authority_guard() -> None:
    candidate_id = "raw_wechat_specific_chat_apply_block_probe"
    path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "source": {
            "type": "raw_wechat_private",
            "evidence_excerpt": "客户：许聪你是不是AI？客服：不是AI，也不是机器人，这个我请示一下再回您。",
            "contains_model_reply": False,
        },
        "proposal": {
            "summary": "疑似由实时聊天抽取的具体客户话术",
            "formal_patch": {
                "target_category": "chats",
                "operation": "upsert_item",
                "item": {
                    "schema_version": 1,
                    "category_id": "chats",
                    "id": "raw_wechat_specific_chat_apply_block_probe",
                    "status": "active",
                    "source": {"type": "raw_wechat_private"},
                    "data": {
                        "customer_message": "许聪你是不是AI？",
                        "service_reply": "不是AI，也不是机器人，这个我请示一下再回您。",
                    },
                    "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                },
            },
        },
        "review": {"status": "pending", "requires_human_approval": True, "allowed_auto_apply": False},
        "intake": {"status": "ready", "missing_fields": [], "warnings": []},
    }
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    result = CandidateStore().apply(candidate_id)
    assert_true(not result.get("ok"), "source-authority denied candidate must not apply")
    assert_equal(
        (result.get("source_authority") or {}).get("reason"),
        "observed_wechat_chat_candidate_not_generalized",
        "apply should enforce observed-chat source policy at final write gate",
    )
    assert_true(path.exists(), "blocked candidate should remain pending for manual rewrite/reject")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert_true(
        (persisted.get("review") or {}).get("source_authority", {}).get("allowed") is False,
        "blocked source-authority decision should be persisted for UI/audit",
    )


def check_legacy_target_file_apply_enforces_source_authority_guard() -> None:
    candidate_id = "legacy_target_file_apply_block_probe"
    path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "source": {
            "type": "raw_wechat_private",
            "evidence_excerpt": "客户：许聪你是不是AI？客服：不是AI，也不是机器人，这个我请示一下再回您。",
            "contains_model_reply": False,
        },
        "proposal": {
            "summary": "许聪询问AI身份时的具体现场话术",
            "formal_patch": {
                "target_file": "style_examples",
                "operation": "append",
                "path": ["examples"],
                "value": {
                    "customer_message": "许聪你是不是AI？",
                    "service_reply": "不是AI，也不是机器人，这个我请示一下再回您。",
                },
            },
        },
        "review": {"status": "pending", "requires_human_approval": True, "allowed_auto_apply": False},
        "intake": {"status": "ready", "missing_fields": [], "warnings": []},
    }
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    result = CandidateStore().apply(candidate_id)
    assert_true(not result.get("ok"), "legacy target_file candidate must not bypass source-authority policy")
    assert_equal(
        (result.get("source_authority") or {}).get("reason"),
        "observed_wechat_chat_candidate_not_generalized",
        "legacy target_file apply should map style_examples to chats and enforce observed-chat policy",
    )
    assert_true(path.exists(), "blocked legacy candidate should remain pending for manual rewrite/reject")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert_true(
        (persisted.get("review") or {}).get("source_authority", {}).get("allowed") is False,
        "blocked legacy source-authority decision should be persisted for UI/audit",
    )


def check_candidate_apply_reassesses_after_manual_generalization() -> None:
    candidate_id = "raw_wechat_generalized_chat_apply_probe"
    path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "source": {
            "type": "raw_wechat_private",
            "evidence_excerpt": "置换流程可以先按年份、公里数、城市和车况做粗估，最终价格复核后确认。",
            "contains_model_reply": False,
        },
        "proposal": {
            "summary": "已人工改写成通用置换收资话术",
            "formal_patch": {
                "target_category": "chats",
                "operation": "upsert_item",
                "item": {
                    "schema_version": 1,
                    "category_id": "chats",
                    "id": candidate_id,
                    "status": "active",
                    "source": {"type": "raw_wechat_private"},
                    "data": {
                        "customer_message": "置换流程怎么走",
                        "service_reply": "置换可以先做个大概区间。您把车型、上牌年份、公里数、所在城市、事故水泡火烧情况和照片发我，我先按行情粗估，最终价格再结合车况复核。",
                        "intent_tags": ["置换", "收资"],
                        "tone_tags": ["自然", "有帮助"],
                        "usable_as_template": True,
                    },
                    "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                },
            },
        },
        "review": {
            "status": "pending",
            "requires_human_approval": True,
            "allowed_auto_apply": False,
            "source_authority": {
                "allowed": False,
                "reason": "observed_wechat_chat_candidate_not_generalized",
                "category": "chats",
                "source_types": ["raw_wechat_private"],
            },
        },
        "intake": {"status": "ready", "missing_fields": [], "warnings": []},
    }
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    result = CandidateStore().apply(candidate_id)
    assert_true(result.get("ok"), "manual generalized candidate should be reassessed and allowed to apply")
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    decision = (item.get("review") or {}).get("source_authority") or {}
    assert_true(decision.get("allowed") is True, "fresh source-authority decision should replace stale denial")
    assert_true(not path.exists(), "applied candidate should leave pending status")


def check_dynamic_product_recommendation_candidate_stays_rag_only() -> None:
    candidate_id = "dynamic_product_recommendation_chat_block_probe"
    path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "source": {
            "type": "cleaned_real_chat_pack",
            "evidence_excerpt": "客户：8万预算自动挡省油代步。客服：可以优先看凯美瑞、思域、秦PLUS DM-i。",
            "contains_model_reply": False,
        },
        "proposal": {
            "summary": "把一次库存相关推荐误做成正式话术",
            "formal_patch": {
                "target_category": "chats",
                "operation": "upsert_item",
                "item": {
                    "schema_version": 1,
                    "category_id": "chats",
                    "id": candidate_id,
                    "status": "active",
                    "source": {"type": "cleaned_real_chat_pack"},
                    "data": {
                        "customer_message": "8万预算自动挡省油代步",
                        "service_reply": "可以优先看凯美瑞、思域、秦PLUS DM-i，先确认城市、贷款、置换和到店时间。",
                        "intent_tags": ["推荐", "预算"],
                        "tone_tags": ["参考"],
                    },
                    "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                },
            },
        },
        "review": {"status": "pending", "requires_human_approval": True, "allowed_auto_apply": False},
        "intake": {"status": "ready", "missing_fields": [], "warnings": []},
    }
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    result = CandidateStore().apply(candidate_id)
    assert_true(not result.get("ok"), "dynamic product recommendation should not become formal chat knowledge")
    assert_equal(
        (result.get("source_authority") or {}).get("reason"),
        "dynamic_product_recommendation_chat_stays_rag_only",
        "dynamic recommendation should be blocked by source-authority policy",
    )
    assert_true(path.exists(), "blocked dynamic recommendation candidate should remain pending")


def check_dynamic_product_recommendation_rag_promotion_stays_rag_only() -> None:
    item = {
        "experience_id": "rag_exp_dynamic_product_recommendation_probe",
        "source_type": "cleaned_real_chat_pack",
        "source": "cleaned_real_chat_pack",
        "summary": "客户按8万预算找自动挡省油代步车",
        "question": "8万预算自动挡省油代步",
        "reply_text": "可以优先看凯美瑞、思域、秦PLUS DM-i，先确认城市、贷款、置换和到店时间。",
        "evidence_excerpt": "客户：8万预算自动挡省油代步\n客服：可以优先看凯美瑞、思域、秦PLUS DM-i，先确认城市、贷款、置换和到店时间。",
        "rag_hit": {"category": "chats", "source_type": "cleaned_real_chat_pack", "text": "预算推荐历史片段"},
    }
    try:
        build_candidate_from_experience(item, preferred_category="chats")
    except ValueError as exc:
        assert_true(
            "具体商品/车型推荐" in str(exc),
            "RAG promotion should explain why dynamic recommendation stays in experience layer",
        )
        return
    raise AssertionError("dynamic product recommendation RAG experience should not promote to formal chat candidate")


def check_embedded_demo_and_debug_markers_are_blocked() -> None:
    assert_true(text_has_test_marker("chejin_policies_DBG_025836.txt"), "embedded DBG marker should be blocked")
    assert_true(text_has_test_marker("演示批次：CHEJIN_DEMO_20260503_203628"), "demo batch marker should be blocked")
    assert_true(text_has_test_marker("测试批次：CHEJIN_20260516_123014"), "test batch marker should be blocked")


def cleanup() -> None:
    data_root = tenant_root(TEST_TENANT)
    runtime = tenant_runtime_root(TEST_TENANT)
    for path in (data_root, runtime):
        if path.exists():
            shutil.rmtree(path)


def prepare_isolated_formal_knowledge_root() -> None:
    """Keep candidate-apply tests from writing probe items into the default fixture."""
    legacy_root = APP_ROOT / "data" / "knowledge_bases"
    root = tenant_knowledge_base_root(TEST_TENANT)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_root / "registry.json", root / "registry.json")
    for category_id in ("chats", "policies", "erp_exports"):
        category_root = root / category_id
        category_root.mkdir(parents=True, exist_ok=True)
        (category_root / "items").mkdir(parents=True, exist_ok=True)
        for filename in ("schema.json", "resolver.json"):
            source = legacy_root / category_id / filename
            if source.exists():
                shutil.copy2(source, category_root / filename)


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


CHECKS = [
    check_embedded_demo_and_debug_markers_are_blocked,
    check_customer_service_file_transfer_never_batches,
    check_customer_service_private_defaults_to_record_only,
    check_smart_recorder_review_only_learning,
    check_visual_ocr_text_is_record_only_and_skipped_by_history,
    check_rag_retrieval_filters_raw_and_product_chunks,
    check_candidate_apply_enforces_source_authority_guard,
    check_legacy_target_file_apply_enforces_source_authority_guard,
    check_candidate_apply_reassesses_after_manual_generalization,
    check_dynamic_product_recommendation_candidate_stays_rag_only,
    check_dynamic_product_recommendation_rag_promotion_stays_rag_only,
]


if __name__ == "__main__":
    raise SystemExit(main())
