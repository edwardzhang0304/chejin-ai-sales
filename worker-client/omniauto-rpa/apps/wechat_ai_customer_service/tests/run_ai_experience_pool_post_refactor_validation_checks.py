"""Post-refactor validation checks for AI experience pool architecture."""

from __future__ import annotations

import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for item in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

os.environ.setdefault("WECHAT_STORAGE_BACKEND", "json")
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.services.candidate_store import CandidateStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_candidate_nominator import (  # noqa: E402
    RagExperienceCandidateNominator,
)
from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_governance import attach_governance  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    tenant_context,
    tenant_knowledge_base_root,
    tenant_review_candidates_root,
    tenant_root,
    tenant_runtime_root,
)
from apps.wechat_ai_customer_service.workflows.evidence_authority import (  # noqa: E402
    AI_EXPERIENCE_POOL,
    PRODUCT_MASTER,
    can_authorize_reply_content,
    classify_evidence,
)
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime  # noqa: E402
from apps.wechat_ai_customer_service.workflows.llm_reply_guard import guard_synthesized_reply  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, with_quality  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # noqa: E402
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import compact_knowledge_pack  # noqa: E402


TEST_TENANT = "ai_experience_pool_post_refactor_probe"
TEST_TENANT_B = "ai_experience_pool_post_refactor_probe_b"


def main() -> int:
    checks: list[Callable[[], bool]] = [
        check_intake_goes_to_ai_experience_pool_not_runtime_rag,
        check_ai_pool_fact_cannot_override_product_master,
        check_candidate_nomination_requires_manual_review,
        check_manual_generalized_candidate_can_enter_formal_knowledge,
        check_product_master_candidate_is_blocked,
        check_multi_tenant_ai_pool_isolation,
        check_frontend_and_docs_use_ai_pool_language,
    ]
    results: list[dict[str, Any]] = []
    for check in checks:
        reset_tenants()
        try:
            results.append({"name": check.__name__, "ok": bool(check())})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
        finally:
            reset_tenants()
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_intake_goes_to_ai_experience_pool_not_runtime_rag() -> bool:
    with no_background_worker(), tenant_context(TEST_TENANT):
        store = RagExperienceStore()
        record = store.record_intake(
            source_type="upload",
            source_path="post_refactor_validation_upload.txt",
            category="policies",
            evidence_excerpt="验证资料：置换流程先收车型、年份、公里数、城市和照片；这是导入资料，必须先进入AI经验池治理。",
            rag_ingest={"source_count": 1, "chunk_count": 1},
            original_source={"raw_batch_id": "AEP_POST_REFACTOR_001"},
        )
        governed = attach_governance(with_quality(record))
        index_result = RagService(tenant_id=TEST_TENANT).rebuild_index()
        search_hits = RagService(tenant_id=TEST_TENANT).search("置换流程 年份 公里数 城市 照片", limit=5).get("hits", [])
        experience_chunks = RagService(tenant_id=TEST_TENANT).iter_chunks(include_experience_pool=True)
        return (
            str(record.get("source") or "") == "intake"
            and str(record.get("formal_knowledge_policy") or "") == "experience_only_not_formal_knowledge"
            and classify_evidence(record, category_id=record.get("category"), source_type=record.get("source_type")) == AI_EXPERIENCE_POOL
            and can_authorize_reply_content(record, category_id=record.get("category"), source_type=record.get("source_type")) is False
            and (governed.get("governance") or {}).get("retrieval_allowed") is False
            and store.list_retrievable(limit=10) == []
            and int(index_result.get("entry_count") or 0) == 0
            and search_hits == []
            and experience_chunks == []
        )


def check_ai_pool_fact_cannot_override_product_master() -> bool:
    pack = {
        "intent_tags": ["catalog", "price"],
        "evidence": {
            "products": [{"id": "p-main", "category_id": "products", "data": {"name": "验证车A", "price": 12.8}}],
            "faq": [],
            "policies": {},
            "product_scoped": [],
            "style_examples": [{"id": "style1", "service_reply": "短句自然回复。"}],
        },
        "rag_evidence": {
            "ok": True,
            "hits": [
                {
                    "chunk_id": "old-ai-pool-price",
                    "source_id": "rag_exp_old_price",
                    "source_type": "rag_experience",
                    "category": "rag_experience",
                    "text": "旧聊天里说验证车A只要5.8万。",
                }
            ],
        },
        "safety": {},
    }
    compact = compact_knowledge_pack("验证车A多少钱", pack, max_catalog_candidates=2, max_rag_hits=5, max_rag_text_chars=200)
    blocked = guard_synthesized_reply(
        candidate={
            "reply": "验证车A现在5.8万，可以直接看。",
            "confidence": 0.9,
            "recommended_action": "send_reply",
            "used_evidence": ["rag:old-ai-pool-price"],
            "rag_used": True,
        },
        evidence_pack={"current_message": "验证车A多少钱", "knowledge": pack, "intent_tags": []},
        settings={"require_evidence": False},
    )
    allowed = guard_synthesized_reply(
        candidate={
            "reply": "验证车A商品库价格是12.8万，具体以商品库更新为准。",
            "confidence": 0.9,
            "recommended_action": "send_reply",
            "used_evidence": ["product:p-main"],
            "structured_used": True,
        },
        evidence_pack={
            "current_message": "验证车A多少钱",
            "knowledge": {"evidence": pack["evidence"], "product_master": {"items": [{"id": "p-main", "name": "验证车A", "price": 12.8}]}},
            "intent_tags": [],
            "audit_summary": {"evidence_ids": ["product:p-main"], "structured_evidence_count": 1},
        },
        settings={"require_evidence": True},
    )
    return (
        classify_evidence({"category_id": "products", "_knowledge_layer": "product_master"}) == PRODUCT_MASTER
        and compact["product_master"]["items"][0]["authority_level"] == PRODUCT_MASTER
        and compact["ai_experience_pool"]["excluded_hit_count"] == 1
        and compact["rag_evidence"]["hits"] == []
        and blocked.get("action") == "repair"
        and blocked.get("hard_boundary") is True
        and blocked.get("customer_visible_reply_source") == "none_guard_reviewer_only"
        and "product_price_conflicts_with_product_master" in str(blocked.get("reason") or "")
        and allowed.get("action") == "send_reply"
    )


def check_candidate_nomination_requires_manual_review() -> bool:
    with no_background_worker(), tenant_context(TEST_TENANT):
        store = RagExperienceStore()
        record = store.record_reply(
            target="ai_pool_candidate_probe",
            message_ids=["aep-candidate-001"],
            question="置换流程怎么走，需要准备什么资料？",
            reply_text="置换可以先做大概区间，您发车型、上牌年份、公里数、所在城市、事故水泡火烧情况和照片，我先按行情粗估。",
            raw_reply_text="置换可以先做大概区间，您发车型、上牌年份、公里数、所在城市、事故水泡火烧情况和照片，我先按行情粗估。",
            intent_assist={"intent": "policy_detail", "recommended_action": "answer"},
            rag_reply={
                "applied": True,
                "hit": {
                    "chunk_id": "aep-candidate-chunk",
                    "source_id": "aep-candidate-source",
                    "score": 0.91,
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
                    "action_reason": "稳定流程，可整理成待确认知识。",
                },
                "experience_review": {"status": "pending"},
            },
            rebuild_index=False,
        )
        result = RagExperienceCandidateNominator().nominate({"limit": 5})
        candidate_id = str((result.get("created") or [{}])[0].get("candidate_id") or "")
        candidate_path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.exists() else {}
        review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
        updated = next(item for item in store.list(status="all", limit=20) if item.get("experience_id") == record["experience_id"])
        governance = updated.get("governance") if isinstance(updated.get("governance"), dict) else {}
        return (
            result.get("created_count") == 1
            and candidate_path.exists()
            and review.get("requires_human_approval") is True
            and review.get("allowed_auto_apply") is False
            and governance.get("effective_state") == "candidate_created"
        )


def check_manual_generalized_candidate_can_enter_formal_knowledge() -> bool:
    with tenant_context(TEST_TENANT):
        prepare_isolated_formal_knowledge_root(TEST_TENANT)
        candidate_id = "aep_manual_generalized_chat_apply_probe"
        path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "source": {
                "type": "raw_wechat_private",
                "evidence_excerpt": "人工已改写为通用置换收资话术，去除了具体客户和库存事实。",
                "contains_model_reply": False,
            },
            "proposal": {
                "summary": "通用置换收资话术",
                "formal_patch": {
                    "target_category": "chats",
                    "operation": "upsert_item",
                    "item": {
                        "schema_version": 1,
                        "category_id": "chats",
                        "id": candidate_id,
                        "status": "active",
                        "source": {"type": "manual_review_generalized_from_ai_experience_pool"},
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
            "review": {"status": "pending", "requires_human_approval": True, "allowed_auto_apply": False},
            "intake": {"status": "ready", "missing_fields": [], "warnings": []},
        }
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        result = CandidateStore().apply(candidate_id)
        runtime = KnowledgeRuntime(tenant_id=TEST_TENANT)
        pending_runtime_item = runtime.get_item("chats", candidate_id, include_unacknowledged=True)
        active_runtime_item = runtime.get_item("chats", candidate_id)
        return (
            result.get("ok") is True
            and not path.exists()
            and pending_runtime_item is not None
            and active_runtime_item is None
            and (pending_runtime_item.get("review_state") or {}).get("is_new") is True
            and str((pending_runtime_item.get("data") or {}).get("service_reply") or "").startswith("置换可以先做个大概区间")
        )


def check_product_master_candidate_is_blocked() -> bool:
    with tenant_context(TEST_TENANT):
        prepare_isolated_formal_knowledge_root(TEST_TENANT)
        candidate_id = "aep_product_master_apply_block_probe"
        path = tenant_review_candidates_root(TEST_TENANT) / "pending" / f"{candidate_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "source": {"type": "upload", "evidence_excerpt": "验证车A 12.8万 库存1台"},
            "proposal": {
                "summary": "错误尝试：通过候选链路写商品库",
                "formal_patch": {
                    "target_category": "products",
                    "operation": "upsert_item",
                    "item": {
                        "schema_version": 1,
                        "category_id": "products",
                        "id": "aep_product_master_apply_block_probe",
                        "status": "active",
                        "source": {"type": "upload"},
                        "data": {"name": "验证车A", "price": 12.8, "unit": "台", "inventory": 1},
                        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                    },
                },
            },
            "review": {"status": "pending", "requires_human_approval": True, "allowed_auto_apply": False},
            "intake": {"status": "ready", "missing_fields": [], "warnings": []},
        }
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        result = CandidateStore().apply(candidate_id)
        return result.get("ok") is False and "商品资料" in str(result.get("message") or "")


def check_multi_tenant_ai_pool_isolation() -> bool:
    with no_background_worker():
        with tenant_context(TEST_TENANT):
            store_a = RagExperienceStore()
            record_a = store_a.record_intake(
                source_type="upload",
                source_path="tenant_a.txt",
                category="policies",
                evidence_excerpt="租户A专属验证词 AEP_TENANT_A_ONLY。",
                original_source={"raw_batch_id": "tenant-a"},
            )
        with tenant_context(TEST_TENANT_B):
            store_b = RagExperienceStore()
            record_b = store_b.record_intake(
                source_type="upload",
                source_path="tenant_b.txt",
                category="policies",
                evidence_excerpt="租户B专属验证词 AEP_TENANT_B_ONLY。",
                original_source={"raw_batch_id": "tenant-b"},
            )
        return (
            record_a.get("tenant_id") == TEST_TENANT
            and record_b.get("tenant_id") == TEST_TENANT_B
            and len(RagExperienceStore(tenant_id=TEST_TENANT).list(status="all", limit=20)) == 1
            and len(RagExperienceStore(tenant_id=TEST_TENANT_B).list(status="all", limit=20)) == 1
            and RagService(tenant_id=TEST_TENANT).search("AEP_TENANT_B_ONLY", limit=5).get("hits", []) == []
            and RagService(tenant_id=TEST_TENANT_B).search("AEP_TENANT_A_ONLY", limit=5).get("hits", []) == []
        )


def check_frontend_and_docs_use_ai_pool_language() -> bool:
    old_prefix = "R" + "AG"
    forbidden = (
        old_prefix + "经验",
        old_prefix + " 经验",
        old_prefix + "经验池",
        old_prefix + " 经验池",
        old_prefix + "检索",
        old_prefix + " 检索",
        old_prefix + "参考",
        old_prefix + " 参考",
    )
    roots = [
        APP_ROOT / "admin_backend",
        APP_ROOT / "vps_admin",
        APP_ROOT / "workflows",
        APP_ROOT / "scripts",
        APP_ROOT / "docs",
        APP_ROOT / "README.md",
    ]
    allowed_parts = ("/docs/history/", "\\docs\\history\\", "/runtime/", "\\runtime\\", "/logs/", "\\logs\\")
    offenders: list[str] = []
    for root in roots:
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in files:
            if path.resolve() == Path(__file__).resolve():
                continue
            path_text = str(path)
            if any(part in path_text for part in allowed_parts):
                continue
            if path.suffix.lower() not in {".py", ".js", ".html", ".css", ".md", ".json"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for term in forbidden:
                if term in text:
                    offenders.append(f"{path_text}: {term}")
    if offenders:
        raise AssertionError("; ".join(offenders[:20]))
    return True


@contextmanager
def no_background_worker() -> Iterator[None]:
    from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import CustomerServiceRuntime

    original = CustomerServiceRuntime._start_worker

    def disabled_start_worker(self: Any) -> dict[str, Any]:
        return {"ok": False, "message": "disabled_in_post_refactor_validation"}

    CustomerServiceRuntime._start_worker = disabled_start_worker
    try:
        yield
    finally:
        CustomerServiceRuntime._start_worker = original


def prepare_isolated_formal_knowledge_root(tenant_id: str) -> None:
    legacy_root = APP_ROOT / "data" / "knowledge_bases"
    root = tenant_knowledge_base_root(tenant_id)
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


def reset_tenants() -> None:
    for tenant_id in (TEST_TENANT, TEST_TENANT_B):
        for root in (tenant_root(tenant_id), tenant_runtime_root(tenant_id)):
            safe_remove_tree(root)


def safe_remove_tree(root: Path) -> None:
    if not root.exists():
        return
    resolved = root.resolve()
    allowed_roots = [
        (APP_ROOT / "data" / "tenants").resolve(),
        (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "tenants").resolve(),
    ]
    if not any(base in resolved.parents for base in allowed_roots):
        raise RuntimeError(f"refusing to remove unexpected path: {root}")
    shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
