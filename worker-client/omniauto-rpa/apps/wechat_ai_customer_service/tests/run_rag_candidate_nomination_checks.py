"""Focused checks for automatic RAG experience candidate nomination."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_candidate_nominator import (  # noqa: E402
    RagExperienceCandidateNominator,
)
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_review_candidates_root, tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402


TEST_TENANT = "rag_candidate_nomination_probe"


def main() -> int:
    checks = [
        check_candidate_suggested_creates_pending_candidate_only,
        check_product_fact_is_not_nominated,
    ]
    results = []
    for check in checks:
        reset_tenant()
        try:
            results.append({"name": check.__name__, "ok": bool(check())})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
        finally:
            reset_tenant()
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_candidate_suggested_creates_pending_candidate_only() -> bool:
    with tenant_context(TEST_TENANT):
        store = RagExperienceStore()
        record = store.record_reply(
            target="candidate_nomination_probe",
            message_ids=["nominate-001"],
            question="客户问置换流程怎么走，需要准备什么资料？",
            reply_text="置换可以先做大概区间，您发车型、上牌年份、公里数、所在城市、事故水泡火烧情况和照片，我先按行情粗估。",
            raw_reply_text="置换可以先做大概区间，您发车型、上牌年份、公里数、所在城市、事故水泡火烧情况和照片，我先按行情粗估。",
            intent_assist={"intent": "policy_detail", "recommended_action": "answer"},
            rag_reply={
                "applied": True,
                "hit": {
                    "chunk_id": "nominate-chunk",
                    "source_id": "nominate-source",
                    "score": 0.9,
                    "category": "chats",
                    "source_type": "rag_soft_reference",
                    "text": "置换资料收集流程：车型、年份、公里数、城市、车况和照片。",
                },
            },
        )
        store.update_metadata(
            record["experience_id"],
            {
                "ai_interpretation": {
                    "recommended_action": "promote_to_pending",
                    "promotion_allowed": True,
                    "action_reason": "置换资料收集流程稳定，可整理成待确认知识。",
                },
                "experience_review": {"status": "pending"},
            },
            rebuild_index=False,
        )
        dry_run = RagExperienceCandidateNominator().nominate({"dry_run": True, "limit": 5})
        if dry_run.get("suggested_count") != 1 or dry_run.get("created_count") != 0:
            return False
        result = RagExperienceCandidateNominator().nominate({"limit": 5})
        if result.get("created_count") != 1:
            return False
        candidate_id = result.get("created", [{}])[0].get("candidate_id")
        candidate_path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
        if not candidate_path.exists():
            return False
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        review = candidate.get("review") or {}
        if review.get("requires_human_approval") is not True or review.get("allowed_auto_apply") is not False:
            return False
        if review.get("rag_experience_id") != record["experience_id"]:
            return False
        if (candidate.get("proposal") or {}).get("formal_patch") and candidate.get("can_promote") is False:
            return False
        if (tenant_review_candidates_root(TEST_TENANT) / "approved" / f"{candidate_id}.json").exists():
            return False
        updated = next(item for item in store.list(status="all", limit=20) if item.get("experience_id") == record["experience_id"])
        nomination = updated.get("candidate_nomination") or {}
        governance = updated.get("governance") or {}
        if nomination.get("created_candidate_id") != candidate_id:
            return False
        if governance.get("effective_state") != "candidate_created":
            return False
        duplicate = RagExperienceCandidateNominator().nominate({"limit": 5})
        return duplicate.get("created_count") == 0


def check_product_fact_is_not_nominated() -> bool:
    with tenant_context(TEST_TENANT):
        store = RagExperienceStore()
        record = store.record_reply(
            target="candidate_nomination_probe",
            message_ids=["nominate-product-001"],
            question="这台宝马多少钱？",
            reply_text="2021年宝马325Li，5.8万公里，报价17万左右。",
            raw_reply_text="2021年宝马325Li，5.8万公里，报价17万左右。",
            intent_assist={"intent": "product_detail", "recommended_action": "answer"},
            rag_reply={
                "applied": True,
                "hit": {
                    "chunk_id": "nominate-product-chunk",
                    "source_id": "nominate-product-source",
                    "score": 0.93,
                    "category": "products",
                    "source_type": "raw_upload",
                    "product_id": "bmw-325li",
                    "text": "商品资料：2021年宝马325Li，报价17万左右。",
                },
            },
        )
        store.update_metadata(
            record["experience_id"],
            {
                "ai_interpretation": {
                    "recommended_action": "promote_to_pending",
                    "promotion_allowed": True,
                    "action_reason": "错误建议：商品资料不应被提名。",
                },
                "category": "products",
                "experience_review": {"status": "pending"},
            },
            rebuild_index=False,
        )
        result = RagExperienceCandidateNominator().nominate({"limit": 5})
        root = tenant_review_candidates_root(TEST_TENANT) / "pending"
        return result.get("created_count") == 0 and not list(root.glob("*.json")) if root.exists() else result.get("created_count") == 0


def reset_tenant() -> None:
    root = tenant_root(TEST_TENANT)
    resolved = root.resolve()
    expected_parent = (APP_ROOT / "data" / "tenants").resolve()
    if expected_parent not in resolved.parents:
        raise RuntimeError(f"refusing to remove unexpected tenant root: {root}")
    if root.exists():
        shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
