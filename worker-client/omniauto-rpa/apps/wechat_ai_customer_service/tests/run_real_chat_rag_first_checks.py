"""Focused checks for real-chat RAG-first governance."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.services.workflow_service import (  # noqa: E402
    WorkflowService,
    real_chat_formal_import_block_reason,
)
from apps.wechat_ai_customer_service.knowledge_paths import default_admin_knowledge_base_root, tenant_context, tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.real_chat_learning import (  # noqa: E402
    formal_chat_item_is_real_chat,
    formal_chat_item_to_experience,
    formal_chat_item_to_style_example,
)
from apps.wechat_ai_customer_service.workflows.rag_experience_store import (  # noqa: E402
    RagExperienceStore,
    experience_is_retrievable,
    with_quality,
)
from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_governance import attach_governance  # noqa: E402


TENANT_ID = "chejin"
TEST_ARTIFACTS = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "real_chat_rag_first"


def main() -> int:
    checks = [
        check_workflow_blocks_cleaned_real_chat_formal_import,
        check_non_real_chat_template_still_importable,
        check_real_chat_conversion_contract,
        check_chejin_formal_library_is_clean_after_migration,
        check_chejin_learning_layers_have_migrated_samples,
        check_low_value_rag_reply_auto_discards,
    ]
    results = []
    for check in checks:
        try:
            results.append({"name": check.__name__, "ok": bool(check())})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_workflow_blocks_cleaned_real_chat_formal_import() -> bool:
    TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = TEST_ARTIFACTS / "real_chat_formal_block.jsonl"
    row = {
        "schema_version": 1,
        "category_id": "chats",
        "id": "chejin_real_test_block",
        "source": {"type": "cleaned_real_chat_pack", "batch_token": "chejin_realchat_test"},
        "data": {
            "customer_message": "预算十万左右，有没有推荐？",
            "service_reply": "哥，我先按预算和用途帮你缩到两三台。",
            "intent_tags": ["车辆推荐"],
            "tone_tags": ["自然"],
            "additional_details": {"cleaning_kind": "safe"},
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }
    write_jsonl(path, [row])
    service = WorkflowService(tenant_id=TENANT_ID)
    dry = service.dry_run_import(tenant_id=TENANT_ID, industry_id="used_car", input_file=str(path))
    return (
        bool(dry.get("ok"))
        and int((dry.get("summary") or {}).get("blocked_items", 0) or 0) == 1
        and str((dry.get("blocked_items") or [{}])[0].get("reason") or "") == "real_chat_requires_rag_first"
        and bool(real_chat_formal_import_block_reason(row))
    )


def check_non_real_chat_template_still_importable() -> bool:
    TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = TEST_ARTIFACTS / "formal_template_allowed.jsonl"
    row = {
        "schema_version": 1,
        "category_id": "chats",
        "id": "rag_first_allowed_tpl_probe",
        "source": {"type": "manual_canonical_template", "batch_token": "rag_first_probe"},
        "data": {
            "customer_message": "rfgov123 预算十万左右，有没有推荐？",
            "service_reply": "可以，我先按预算、用途和是否置换帮您筛两三台合适车源。",
            "intent_tags": ["车辆推荐"],
            "tone_tags": ["稳健"],
            "additional_details": {},
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }
    write_jsonl(path, [row])
    service = WorkflowService(tenant_id=TENANT_ID)
    dry = service.dry_run_import(tenant_id=TENANT_ID, industry_id="used_car", input_file=str(path))
    return bool(dry.get("ok")) and int((dry.get("summary") or {}).get("blocked_items", 0) or 0) == 0


def check_real_chat_conversion_contract() -> bool:
    item = {
        "id": "chejin_real_contract_probe",
        "source": {"type": "cleaned_real_chat_pack", "batch_token": "chejin_realchat_contract"},
        "data": {
            "customer_message": "价格还能少点吗？",
            "service_reply": "哥，价格我理解，您主要是预算卡在哪个区间？我按预算帮您看更合适的。",
            "intent_tags": ["价格谈判"],
            "tone_tags": ["自然"],
            "additional_details": {"scenario": "价格谈判", "hit_count": 5},
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }
    exp = formal_chat_item_to_experience(item, tenant_id=TENANT_ID, migration_id="contract_probe", source_file="probe.json")
    style = formal_chat_item_to_style_example(item, tenant_id=TENANT_ID, migration_id="contract_probe", source_file="probe.json")
    governed_exp = attach_governance(with_quality(exp or {}))
    return (
        formal_chat_item_is_real_chat(item)
        and bool(exp)
        and bool(style)
        and str(exp.get("formal_knowledge_policy") or "") == "experience_only_not_formal_knowledge"
        and experience_is_retrievable(governed_exp) is False
        and str((governed_exp.get("governance") or {}).get("effective_state") or "") == "style_only"
        and "哥" in str(style.get("service_reply") or "")
    )


def check_chejin_formal_library_is_clean_after_migration() -> bool:
    items_dir = default_admin_knowledge_base_root(TENANT_ID) / "chats" / "items"
    offenders = []
    for path in sorted(items_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and formal_chat_item_is_real_chat(item, path=path):
            offenders.append(path.name)
    return not offenders


def check_chejin_learning_layers_have_migrated_samples() -> bool:
    tenant_dir = tenant_root(TENANT_ID)
    rag_path = tenant_dir / "rag_experience" / "experiences.json"
    style_path = tenant_dir / "style_memory" / "examples.jsonl"
    rag_items = json.loads(rag_path.read_text(encoding="utf-8")) if rag_path.exists() else []
    migrated_rag = [
        item
        for item in rag_items
        if str((item.get("migration") or {}).get("from_layer") or "") == "formal_knowledge.chats"
        and str(item.get("source_type") or "") == "cleaned_real_chat_pack"
    ]
    style_rows = []
    if style_path.exists():
        for line in style_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                style_rows.append(json.loads(line))
    migrated_style = [
        item
        for item in style_rows
        if str((item.get("migration") or {}).get("from_layer") or "") == "formal_knowledge.chats"
        and str(item.get("source_type") or "") == "cleaned_real_chat_pack"
    ]
    return len(migrated_rag) >= 900 and len(migrated_style) >= 900


def check_low_value_rag_reply_auto_discards() -> bool:
    from apps.wechat_ai_customer_service.admin_backend.services import llm_knowledge_audit  # noqa: PLC0415

    tenant_id = "rag_reply_auto_discard_probe"
    probe_root = tenant_root(tenant_id)
    if probe_root.exists():
        shutil.rmtree(probe_root)
    old_has_llm_config = llm_knowledge_audit.has_llm_config
    old_audit_single = llm_knowledge_audit.audit_single_rag_experience
    try:
        llm_knowledge_audit.has_llm_config = lambda: True  # type: ignore[assignment]
        llm_knowledge_audit.audit_single_rag_experience = lambda record: {  # type: ignore[assignment]
            "action": "delete",
            "reason": "内容主要是系统日志/自动回复片段，没有真实客服问答与有效业务信息。",
        }
        with tenant_context(tenant_id):
            record = RagExperienceStore().record_reply(
                target="文件传输助手",
                message_ids=["probe-message"],
                question="还有哪些车源可以看看？",
                reply_text="我再帮你核一下合适车源。",
                raw_reply_text="我再帮你核一下合适车源。",
                intent_assist={"intent": "product_inquiry", "evidence": {"safety": {"must_handoff": False}}},
                rag_reply={
                    "applied": True,
                    "hit": {
                        "chunk_id": "chunk_probe",
                        "source_id": "rag_exp_probe",
                        "score": 0.99,
                        "category": "rag_experience",
                        "source_type": "rag_experience",
                        "text": "AI经验池概括：测试低价值回灌片段。",
                    },
                },
            )
        review = record.get("experience_review") if isinstance(record.get("experience_review"), dict) else {}
        review_state = record.get("review_state") if isinstance(record.get("review_state"), dict) else {}
        discard = record.get("auto_audit_discard") if isinstance(record.get("auto_audit_discard"), dict) else {}
        return (
            str(record.get("status") or "") == "discarded"
            and str(review.get("status") or "") == "auto_triaged"
            and str(review.get("auto_triage_action") or "") == "discard"
            and review_state.get("is_new") is False
            and str(discard.get("mode") or "") == "auto_discard_low_value_reply"
            and not experience_is_retrievable(with_quality(record))
        )
    finally:
        llm_knowledge_audit.has_llm_config = old_has_llm_config  # type: ignore[assignment]
        llm_knowledge_audit.audit_single_rag_experience = old_audit_single  # type: ignore[assignment]
        if probe_root.exists():
            shutil.rmtree(probe_root)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
