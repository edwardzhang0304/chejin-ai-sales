"""Focused regression checks for the AI smart recorder V2 flow."""

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

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.candidate_badges import enrich_candidate  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.formal_review_state import acknowledge_item, enrich_knowledge_item, mark_item_new  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.learning_service import LearningService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.rag_admin_service import RagAdminService, annotate_experience, build_candidate_from_experience  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_learning_service import RawMessageLearningService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore, find_ocr_near_duplicate, merge_message  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.recorder_service import RecorderService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.upload_store import UploadStore  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_raw_inbox_root, tenant_review_candidates_root, tenant_root, tenant_runtime_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402


TEST_TENANT = "smart_recorder_envelope_test"
def main() -> int:
    candidate_ids: list[str] = []
    try:
        with tenant_context(TEST_TENANT):
            cleanup_runtime()
            results = [
                check_raw_message_store_and_learning(candidate_ids),
                check_group_speaker_prefix_is_stored_as_metadata(),
                check_message_envelope_quote_blocks_learning_and_uses_captured_at(),
                check_recorder_default_auto_learn_is_disabled(),
                check_ocr_near_duplicate_deduplication(),
                check_raw_wechat_product_master_is_blocked(),
                check_rag_product_master_promotion_is_blocked(),
                check_upload_learning_uses_rag_experience(candidate_ids),
                check_badges_and_review_state(),
                check_admin_api_surfaces(),
            ]
        payload = {"ok": all(item["ok"] for item in results), "results": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    finally:
        cleanup_candidates(candidate_ids)
        with tenant_context(TEST_TENANT):
            cleanup_runtime()


def check_raw_message_store_and_learning(candidate_ids: list[str]) -> dict[str, Any]:
    store = RawMessageStore()
    conversation = {
        "target_name": "智能记录测试群",
        "display_name": "智能记录测试群",
        "conversation_type": "group",
        "selected_by_user": True,
        "notify_enabled": False,
        "learning_enabled": True,
        "source": {"type": "test"},
    }
    messages = [
        {
            "id": "msg-001",
            "type": "text",
            "sender": "张三",
            "content": "公司名称：蓝鲸智能科技有限公司\n主营范围：智能门锁安装和售后服务、办公椅销售\n标准回复：我们主营智能门锁安装和售后服务，可按项目安排师傅。",
            "time": "2026-05-01 10:00:00",
        },
        {
            "id": "msg-002",
            "type": "text",
            "sender": "李四",
            "content": "开票信息和安装售后也按这个规则回复客户，办公椅售后同上。",
            "time": "2026-05-01 10:01:00",
        },
    ]
    first = store.upsert_messages(conversation, messages, source_module="smart_recorder_test", batch_reason="test_capture")
    assert_true(first["inserted_count"] == 2, "raw message insert count")
    duplicate = store.upsert_messages(conversation, messages, source_module="smart_recorder_test", batch_reason="test_capture")
    assert_true(duplicate["inserted_count"] == 0, "duplicate raw messages should not be inserted twice")
    assert_true(duplicate["duplicate_count"] == 2, "duplicate raw messages should be reported")
    listed = store.list_conversations(conversation_type="group", status="all")
    assert_true(any(item["target_name"] == "智能记录测试群" for item in listed), "group conversation should be listed")

    learning = RawMessageLearningService().process_batch(first["batch"]["batch_id"], use_llm=False)
    candidate_ids.extend(learning.get("candidate_ids", []) or [])
    assert_true(learning.get("ok") is True, "raw batch learning should be ok")
    assert_equal(learning.get("candidate_count", 0), 0, "raw batch should only create RAG experience, not review candidates")
    assert_true(str(learning.get("rag_experience_id") or "").startswith("rag_exp_"), "raw batch should first create a rag experience")
    processed = store.get_batch(first["batch"]["batch_id"])
    assert_true(processed and processed.get("status") == "processed", "raw batch should be marked processed")
    assert_true(processed.get("rag_experience_id") == learning.get("rag_experience_id"), "raw batch should keep rag experience trace")
    experiences = RagExperienceStore().list(status="all", limit=50)
    assert_true(any(item.get("experience_id") == learning.get("rag_experience_id") for item in experiences), "rag experience should be stored")
    assert_true(not learning.get("candidate_ids"), "raw batch should not return candidate ids before manual RAG promotion")
    promoted = RagAdminService().promote_experience(str(learning.get("rag_experience_id")), {"target_category": "policies"})
    if promoted.get("ok") is True:
        first_candidate_id = promoted["candidate"]["candidate_id"]
        candidate_ids.append(first_candidate_id)
        candidate_path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{first_candidate_id}.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        assert_equal(candidate.get("source", {}).get("type"), "rag_experience", "candidate should be linked from rag experience")
        assert_equal(candidate.get("review", {}).get("rag_experience_id"), learning.get("rag_experience_id"), "candidate should trace rag experience")
    else:
        assert_true(
            "discarded" in str(promoted.get("message") or ""),
            f"promotion failure should be explainable when auto review discarded the experience: {promoted}",
        )
    return {"name": "raw_message_store_and_learning", "ok": True, "candidate_ids": candidate_ids}


def check_group_speaker_prefix_is_stored_as_metadata() -> dict[str, Any]:
    store = RawMessageStore()
    conversation = {
        "target_name": "新数据测试",
        "display_name": "新数据测试",
        "conversation_type": "group",
        "selected_by_user": True,
        "learning_enabled": False,
        "source": {"type": "test"},
    }
    result = store.upsert_messages(
        conversation,
        [
            {
                "id": "speaker-prefix-001",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "source_adapter": "win32_ocr",
                "content": "许聪\n在不在",
            }
        ],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    messages = store.list_messages_advanced(conversation_id=result["conversation"]["conversation_id"], limit=20)
    assert_equal(result["inserted_count"], 1, "speaker-prefixed OCR message should insert once")
    assert_equal(len(messages), 1, "speaker-prefixed OCR message should be listed")
    stored = messages[0]
    assert_equal(stored.get("content"), "在不在", "stored content should be body-only")
    assert_equal(stored.get("speaker_name"), "许聪", "speaker name should be preserved")
    assert_equal(stored.get("group_member_name"), "许聪", "group member should use OCR speaker prefix")
    assert_equal(stored.get("sender_role"), "group_member", "speaker-prefixed group message should be group_member")
    assert_true("许聪" not in str(stored.get("content") or ""), "speaker must not pollute semantic content")
    assert_true("许聪\n在不在" in str(stored.get("original_content") or ""), "original content should remain auditable")
    return {"name": "group_speaker_prefix_is_stored_as_metadata", "ok": True}


def check_message_envelope_quote_blocks_learning_and_uses_captured_at() -> dict[str, Any]:
    store = RawMessageStore()
    conversation = {
        "target_name": "记录员引用测试群",
        "display_name": "记录员引用测试群",
        "conversation_type": "group",
        "selected_by_user": True,
        "learning_enabled": True,
        "source": {"type": "test"},
    }
    captured_at = "2026-06-04T15:09:10"
    result = store.upsert_messages(
        conversation,
        [
            {
                "id": "envelope-quote-001",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "source_adapter": "win32_ocr",
                "content": "许聪\n[引用 张老师：旧订单 试剂盒 9盒 1元]\n枪头 2盒 30元",
                "time": "昨天03:02",
                "captured_at": captured_at,
                "ocr_confidence": 0.98,
            }
        ],
        source_module="smart_recorder",
        learning_enabled=True,
        create_batch=True,
        batch_reason="recorder_capture",
    )
    messages = store.list_messages_advanced(conversation_id=result["conversation"]["conversation_id"], limit=20)
    assert_equal(result["inserted_count"], 1, "quoted OCR message should insert")
    assert_equal(result.get("batch"), None, "quality-risk quoted OCR message should not create learning batch")
    stored = messages[0]
    assert_equal(stored.get("content"), "枪头 2盒 30元", "quote and speaker should be stripped from current content")
    assert_true(stored.get("captured_at") != captured_at, "OCR captured_at should use actual program read time, not screen time")
    assert_equal(stored.get("screen_time_text"), "昨天03:02", "OCR screen time should remain auditable")
    assert_equal(stored.get("message_time"), stored.get("captured_at"), "OCR message_time should use actual read time")
    assert_true(stored.get("learning_enabled") is False, "quote contamination should block learning")
    assert_true(stored.get("quoted_fragments"), "quote fragments should remain auditable")
    assert_true("quote_contamination" in set(stored.get("quality_flags") or []), "quality flags should record quote contamination")
    raw_payload = stored.get("raw_payload") if isinstance(stored.get("raw_payload"), dict) else {}
    assert_true(isinstance(raw_payload.get("message_envelope"), dict), "raw payload should store message envelope")
    return {"name": "message_envelope_quote_blocks_learning_and_uses_captured_at", "ok": True}


def check_recorder_default_auto_learn_is_disabled() -> dict[str, Any]:
    settings = RecorderService().settings()
    assert_true(settings.get("auto_learn") is False, f"recorder auto_learn should default false: {settings}")
    return {"name": "recorder_default_auto_learn_is_disabled", "ok": True}


def check_ocr_near_duplicate_deduplication() -> dict[str, Any]:
    store = RawMessageStore()
    conversation = {
        "target_name": "OCR近重复测试",
        "display_name": "OCR近重复测试",
        "conversation_type": "file_transfer",
        "selected_by_user": True,
        "learning_enabled": False,
        "source": {"type": "test"},
    }
    clean = {
        "type": "text",
        "sender": "self",
        "sender_role": "self",
        "source_adapter": "win32_ocr",
        "content": "记录员实盘验收04REC30T190006R04客户问置\n换补贴，旧车是2018款SUV。",
    }
    noisy = {
        **clean,
        "content": "记录员实盘验收04REC30T190006R04客户问置\n换补贴，I旧车是2018款SUV。",
    }
    first = store.upsert_messages(
        conversation,
        [clean],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    duplicate = store.upsert_messages(
        conversation,
        [noisy],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    assert_equal(first["inserted_count"], 1, "clean OCR message should insert once")
    assert_equal(duplicate["inserted_count"], 0, "near-duplicate OCR message should not insert")
    assert_equal(duplicate["duplicate_count"], 1, "near-duplicate OCR message should be reported as duplicate")
    listed = store.list_messages_advanced(conversation_id=first["conversation"]["conversation_id"], query="REC30T190006R04", limit=20)
    assert_equal(len(listed), 1, "near-duplicate OCR message should keep one raw record")
    fragment = {
        "type": "text",
        "sender": "self",
        "sender_role": "self",
        "source_adapter": "win32_ocr",
        "observed_at": "2026-05-24T20:00:00",
        "content": "老师LABTDEDUP-L20",
    }
    full = {
        **fragment,
        "observed_at": "2026-05-24T20:00:07",
        "content": "LABTDEDUP-L20密理博UFC901096超滤管[15ml10KD]订2根共计101*8=808元顾欣-陈秋平老师LABTDEDUP-L20",
    }
    fragment_insert = store.upsert_messages(
        conversation,
        [fragment],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    full_duplicate = store.upsert_messages(
        conversation,
        [full],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    assert_equal(fragment_insert["inserted_count"], 1, "OCR fragment should insert before the full bubble is visible")
    assert_equal(full_duplicate["inserted_count"], 0, "full OCR bubble should merge with its earlier fragment")
    assert_equal(full_duplicate["duplicate_count"], 1, "full OCR bubble should be reported as duplicate of fragment")
    merged = store.list_messages_advanced(conversation_id=fragment_insert["conversation"]["conversation_id"], query="LABTDEDUP-L20", limit=20)
    assert_equal(len(merged), 1, "partial OCR fragment should not leave a second raw record")
    assert_true("UFC901096" in str(merged[0].get("content") or ""), "merged OCR record should keep the more complete content")
    short_order = {
        "type": "text",
        "sender": "self",
        "sender_role": "self",
        "source_adapter": "win32_ocr",
        "observed_at": "2026-05-24T20:01:00",
        "captured_at": "2026-05-24T20:01:00",
        "bubble_id": "short-order-bubble-a",
        "content": "白鲨 50ml离心管 订2包 60元 周梦老师",
    }
    short_order_repeat = {
        **short_order,
        "observed_at": "2026-05-24T20:01:38",
        "captured_at": "2026-05-24T20:01:38",
        "bubble_id": "short-order-bubble-b",
    }
    short_insert = store.upsert_messages(
        conversation,
        [short_order],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    short_duplicate = store.upsert_messages(
        conversation,
        [short_order_repeat],
        source_module="smart_recorder",
        learning_enabled=False,
        create_batch=False,
    )
    assert_equal(short_insert["inserted_count"], 1, "short OCR order should insert once")
    assert_equal(short_duplicate["inserted_count"], 0, "exact short OCR repeat with new bubble_id should merge")
    assert_equal(short_duplicate["duplicate_count"], 1, "short OCR repeat should be reported as duplicate")
    short_listed = store.list_messages_advanced(conversation_id=short_insert["conversation"]["conversation_id"], query="白鲨", limit=20)
    assert_equal(len(short_listed), 1, "short OCR exact repeat should keep one raw record")
    far_existing = {
        "conversation_id": "conv_far_source_time",
        "sender": "self",
        "content_type": "text",
        "source_modules": ["smart_recorder"],
        "source_adapter": "win32_ocr",
        "content": "白鲨 50ml离心管 订2包 60元 周梦老师",
        "observed_at": "2026-05-24T20:01:00",
        "message_time": "2026-05-24T20:01:00",
        "screen_time_text": "2026-05-24T16:23:42",
    }
    far_incoming = {
        **far_existing,
        "observed_at": "2026-05-24T21:15:00",
        "message_time": "2026-05-24T21:15:00",
        "captured_at": "2026-05-24T21:15:00",
    }
    assert_true(
        find_ocr_near_duplicate([far_existing], far_incoming) is far_existing,
        "same OCR source screen time should dedupe old visible messages even when read times are far apart",
    )
    repeat_later = {
        **far_existing,
        "message_id": "repeat-later-message",
        "bubble_id": "repeat-later-bubble",
        "observed_at": "2026-05-24T20:06:30",
        "message_time": "2026-05-24T20:06:30",
        "screen_time_text": "2026-05-24T20:06:30",
    }
    assert_true(
        find_ocr_near_duplicate([far_existing], repeat_later) is None,
        "same OCR text several minutes later should be kept as a possible real repeated order",
    )
    far_existing["captured_at"] = "2026-05-24T20:01:00"
    far_existing["bubble_id"] = "old-visible-bubble"
    merged_far = merge_message(far_existing, far_incoming, source_module="smart_recorder")
    assert_equal(
        merged_far.get("captured_at"),
        "2026-05-24T20:01:00",
        "far old visible duplicate should not refresh captured_at into a new export window",
    )
    assert_equal(merged_far.get("screen_time_text"), "2026-05-24T16:23:42", "far duplicate should not refresh screen time")
    assert_equal(merged_far.get("bubble_id"), "old-visible-bubble", "far duplicate should not refresh bubble id")
    fragment_existing = {
        "source_modules": ["smart_recorder"],
        "source_adapter": "win32_ocr",
        "content": "老师LABTDEDUP-L21",
        "content_body": "老师LABTDEDUP-L21",
        "message_fingerprint": "old",
        "captured_at": "2026-05-24T20:00:00",
        "message_time": "2026-05-24T20:00:00",
        "observed_at": "2026-05-24T20:00:00",
    }
    full_incoming = {
        **fragment_existing,
        "content": "LABTDEDUP-L21密理博UFC901096超滤管[15ml10KD]订2根共计101*8=808元顾欣-陈秋平老师LABTDEDUP-L21",
        "content_body": "LABTDEDUP-L21密理博UFC901096超滤管[15ml10KD]订2根共计101*8=808元顾欣-陈秋平老师LABTDEDUP-L21",
        "message_fingerprint": "new",
        "captured_at": "2026-05-24T20:00:07",
        "message_time": "2026-05-24T20:00:07",
        "observed_at": "2026-05-24T20:00:07",
    }
    merged_complete = merge_message(fragment_existing, full_incoming, source_module="smart_recorder")
    assert_equal(merged_complete.get("message_time"), "2026-05-24T20:00:07", "more-complete OCR merge should use program-read message time")
    assert_equal(merged_complete.get("observed_at"), "2026-05-24T20:00:07", "more-complete OCR merge should refresh observed_at")
    assert_true("UFC901096" in str(merged_complete.get("content") or ""), "more-complete OCR merge should keep full content")
    return {"name": "ocr_near_duplicate_deduplication", "ok": True}


def check_raw_wechat_product_master_is_blocked() -> dict[str, Any]:
    store = RawMessageStore()
    conversation = {
        "target_name": "source_authority_private_probe",
        "display_name": "source_authority_private_probe",
        "conversation_type": "private",
        "learning_enabled": True,
        "notify_enabled": False,
        "source": {"type": "test"},
    }
    messages = [
        {
            "id": "source-policy-product-001",
            "type": "text",
            "sender": "customer",
            "content": (
                "商品资料：\n"
                "商品名称：2020款丰田卡罗拉1.2T自动挡\n"
                "型号/SKU：SOURCE-POLICY-COROLLA\n"
                "类目：二手燃油车\n"
                "价格：69800\n"
                "单位：台\n"
                "库存：1\n"
            ),
            "time": "2026-05-04 12:00:00",
        }
    ]
    result = store.upsert_messages(conversation, messages, source_module="source_authority_test", batch_reason="test_capture")
    learning = RawMessageLearningService().process_batch(result["batch"]["batch_id"], use_llm=False)
    assert_true(learning.get("ok") is True, "raw product-like chat learning should finish")
    assert_equal(learning.get("candidate_count"), 0, "raw WeChat product master data should not enter pending candidates")
    assert_equal(learning.get("skipped_source_policy_count", 0), 0, "source policy is enforced later if a user manually promotes this RAG experience")
    batch = store.get_batch(result["batch"]["batch_id"])
    assert_true(batch and batch.get("candidate_count", 0) == 0, "batch should record strict RAG-only learning")
    assert_true(str(learning.get("rag_experience_id") or "").startswith("rag_exp_"), "blocked raw product should still keep RAG trace")
    return {"name": "raw_wechat_product_master_is_blocked", "ok": True, "rag_experience_id": learning.get("rag_experience_id")}


def check_rag_product_master_promotion_is_blocked() -> dict[str, Any]:
    product_payload = {
        "name": "2020款丰田卡罗拉1.2T自动挡",
        "sku": "SOURCE-POLICY-COROLLA",
        "category": "二手燃油车",
        "price": 69800,
        "unit": "台",
        "inventory": 1,
    }
    experience = {
        "experience_id": "source_policy_rag_product_probe",
        "status": "active",
        "source_type": "raw_wechat_private",
        "summary": json.dumps(product_payload, ensure_ascii=False),
        "reply_text": "[车金AI] 这台卡罗拉参考价6.98万，库存1台。",
        "usage": {"reply_count": 3},
        "rag_hit": {"source_type": "wechat_raw_message", "category": "private", "text": "[车金AI] 这台卡罗拉参考价6.98万，库存1台。"},
    }
    annotated = annotate_experience(experience, [])
    assert_equal(annotated.get("formal_relation"), "blocked_by_source_policy", "raw-chat product RAG should be blocked in review relation")
    try:
        build_candidate_from_experience(experience, preferred_category="products")
    except ValueError as exc:
        assert_true("不能升级为商品资料" in str(exc) or "不能新增或修改商品资料" in str(exc), "block message should explain product authority")
    else:
        raise AssertionError("raw-chat product RAG must not promote into product candidate")
    return {"name": "rag_product_master_promotion_is_blocked", "ok": True}


def check_upload_learning_uses_rag_experience(candidate_ids: list[str]) -> dict[str, Any]:
    upload = UploadStore().save_upload(
        "smart_recorder_upload_probe.txt",
        (
            "商品资料：智能记录员上传测试车源\n"
            "商品名称：2023款比亚迪宋PLUS DM-i 冠军版\n"
            "型号：UPLOAD-SONGPLUS-20260501\n"
            "商品类目：二手车/SUV\n"
            "价格：11.80万\n"
            "单位：台\n"
            "库存：1\n"
            "发货：南京门店可看车，试驾需提前预约\n"
            "售后：车况以检测报告为准\n"
        ).encode("utf-8"),
        "products",
    )
    assert_true(upload.get("ok") is True, f"upload should be saved: {upload}")
    job = LearningService().create_job([upload["item"]["upload_id"]], use_llm=False)
    candidate_ids.extend(job.get("job", {}).get("candidate_ids", []) or [])
    assert_true(job.get("ok") is True, "upload learning should be ok")
    assert_equal(job.get("job", {}).get("candidate_count", 0), 0, "upload learning should only create RAG experience")
    rag_ids = job.get("job", {}).get("rag_experience_ids", []) or []
    assert_true(bool(rag_ids) and str(rag_ids[0]).startswith("rag_exp_"), "upload learning should first create rag experience")
    assert_true(not job["job"]["candidate_ids"], "upload learning should not create pending candidates automatically")
    return {"name": "upload_learning_uses_rag_experience", "ok": True, "candidate_ids": []}


def check_badges_and_review_state() -> dict[str, Any]:
    candidate = {
        "candidate_id": "candidate_badge_test",
        "source": {"type": "rag_experience", "original_type": "raw_wechat_group"},
        "detected_tags": ["wechat_group_chat"],
        "proposal": {"formal_patch": {"target_category": "policies"}},
        "review": {"status": "pending", "completeness_status": "ready"},
        "intake": {"status": "ready", "warnings": []},
    }
    enriched = enrich_candidate(candidate)
    badge_keys = {item["key"] for item in enriched["display_badges"]}
    assert_true({"complete", "rag_generated", "wechat_group", "can_promote"}.issubset(badge_keys), "candidate badges should include V2 status markers")

    item = mark_item_new({"id": "formal_badge_test", "data": {}}, {"source_module": "candidate"})
    assert_true(item["review_state"]["is_new"] is True, "formal item should be marked new")
    formal = enrich_knowledge_item(item)
    assert_true(any(badge["key"] == "new_unread" for badge in formal["display_badges"]), "formal new badge should render")
    acknowledged = acknowledge_item(item)
    assert_true(acknowledged["review_state"]["is_new"] is False, "acknowledge should clear new marker")
    return {"name": "badges_and_review_state", "ok": True}


def check_admin_api_surfaces() -> dict[str, Any]:
    client = TestClient(create_app())
    headers = {"X-Tenant-ID": TEST_TENANT}
    index = client.get("/")
    assert_true("AI智能记录员" in index.text, "admin UI should expose recorder page")
    assert_true("记录员总开关" in index.text, "admin UI should expose recorder global switch")
    assert_true("导出所有记录（结构化）" in index.text, "admin UI should expose structured recorder export action")
    assert_true("导出微信聊天Excel（按会话）" in index.text, "admin UI should expose raw chat export action")

    summary = client.get("/api/raw-messages/summary", headers=headers)
    assert_equal(summary.status_code, 200, "raw message summary endpoint")
    recorder = client.get("/api/recorder/summary", headers=headers)
    assert_equal(recorder.status_code, 200, "recorder summary endpoint")
    raw_export = client.post("/api/exports/raw-chats", headers=headers, json={"mode": "session"})
    assert_equal(raw_export.status_code, 200, "raw chat export endpoint")
    raw_export_payload = raw_export.json()
    assert_true(raw_export_payload.get("ok") is True and Path(raw_export_payload.get("path", "")).exists(), "raw chat export file should be created")
    export = client.post("/api/exports/knowledge", headers=headers, json={"sort_by": "time"})
    assert_equal(export.status_code, 200, "knowledge export endpoint")
    export_payload = export.json()
    assert_true(export_payload.get("ok") is True and Path(export_payload.get("path", "")).exists(), "knowledge export file should be created")
    return {"name": "admin_api_surfaces", "ok": True}


def cleanup_runtime() -> None:
    root = tenant_runtime_root(TEST_TENANT)
    if root.exists():
        remove_tree_tolerating_locked_logs(root)
    rag_root = tenant_root(TEST_TENANT) / "rag_experience"
    if rag_root.exists():
        remove_tree_tolerating_locked_logs(rag_root)
    raw_inbox = tenant_raw_inbox_root(TEST_TENANT)
    if raw_inbox.exists():
        remove_tree_tolerating_locked_logs(raw_inbox)


def remove_tree_tolerating_locked_logs(path: Path) -> None:
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    if not path.exists():
        return
    for child in path.iterdir():
        try:
            if child.is_dir():
                remove_tree_tolerating_locked_logs(child)
                if child.exists():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            else:
                child.unlink()
        except PermissionError:
            if child.suffix.lower() != ".log":
                raise
    try:
        path.rmdir()
    except OSError:
        pass


def cleanup_candidates(candidate_ids: list[str]) -> None:
    for candidate_id in candidate_ids:
        for status in ("pending", "approved", "rejected"):
            path = tenant_review_candidates_root(TEST_TENANT) / status / f"{candidate_id}.json"
            if path.exists():
                path.unlink()


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
