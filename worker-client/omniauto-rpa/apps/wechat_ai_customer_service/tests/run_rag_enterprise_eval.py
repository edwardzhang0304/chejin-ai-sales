"""Enterprise-style evaluation checks for the local RAG layer.

The suite stays local and file-based. It verifies the new hybrid retrieval,
experience layer, operations analytics, and answer safety boundaries without
calling WeChat or an LLM provider.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "rag_enterprise_eval"
REPORT_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "rag_enterprise_eval_report.json"
TENANT_ID = "rag_enterprise_eval"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from customer_service_loop import ReplyDecision  # noqa: E402
from rag_answer_layer import maybe_build_rag_reply  # noqa: E402
from rag_experience_store import RagExperienceStore  # noqa: E402
from rag_layer import RagService  # noqa: E402
from rag_operations import RagOperationsAnalyzer  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config  # noqa: E402


def main() -> int:
    cleanup()
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item["ok"]]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cleanup()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def make_service() -> RagService:
    return RagService(
        tenant_id=TENANT_ID,
        sources_root=TEST_ROOT / "rag_sources",
        chunks_root=TEST_ROOT / "rag_chunks",
        index_root=TEST_ROOT / "rag_index",
        cache_root=TEST_ROOT / "rag_cache",
    )


def seed_sources(service: RagService) -> dict[str, Path]:
    source_root = TEST_ROOT / "sources"
    source_root.mkdir(parents=True, exist_ok=True)
    lock = source_root / "fl920_lock.txt"
    lock.write_text(
        "智能指纹门锁 FL-920 适合酒店公寓、民宿客房和办公室门禁场景。安装前建议确认门厚、开孔尺寸、开门方向和供电方式。",
        encoding="utf-8",
    )
    bed = source_root / "folding_bed.txt"
    bed.write_text(
        "折叠午休床 FB-100 适合办公室午休、酒店临时加床和员工休息区。带记忆海绵床垫，可折叠收纳。",
        encoding="utf-8",
    )
    risk = source_root / "risky_payment.txt"
    risk.write_text(
        "内部聊天记录：老客户如果要求月结账期、先发货或最低价，需要转人工确认，不允许自动承诺。",
        encoding="utf-8",
    )
    service.ingest_file(lock, source_type="product_doc", category="product_explanations", product_id="fl-920")
    service.ingest_file(bed, source_type="product_doc", category="product_explanations", product_id="fb-100")
    service.ingest_file(risk, source_type="chat_log", category="raw_chat", product_id="")
    return {"lock": lock, "bed": bed, "risk": risk}


def check_hybrid_semantic_recall() -> None:
    service = make_service()
    seed_sources(service)
    result = service.search("民宿客房的智能锁安装要不要提前确认电源", product_id="fl-920", limit=3)
    assert_true(result.get("hits"), "semantic synonym query should retrieve a hit")
    top = result["hits"][0]
    assert_equal(top.get("product_id"), "fl-920", "top hit should be FL-920")
    assert_equal(top.get("retrieval_mode"), "hybrid_lexical_semantic", "hit should use hybrid retrieval mode")
    assert_true(float(top.get("scoring", {}).get("semantic", 0)) > 0, "hit should include semantic score")
    assert_true("vector" in (top.get("scoring", {}) or {}), "hit should include vector score")
    expanded = set(result.get("query_profile", {}).get("expanded_terms", []) or [])
    assert_true("供电方式" in expanded or "酒店公寓" in expanded, "query profile should include semantic expansion")


def check_product_filter_and_rerank() -> None:
    service = make_service()
    seed_sources(service)
    result = service.search("酒店临时加床午休能不能折叠收纳", product_id="fb-100", limit=3)
    assert_true(result.get("hits"), "folding bed query should retrieve a hit")
    assert_equal(result["hits"][0].get("product_id"), "fb-100", "product filter should prioritize folding bed")
    blocked = service.search("酒店临时加床午休能不能折叠收纳", product_id="fl-920", limit=3)
    assert_true(not any(hit.get("product_id") == "fb-100" for hit in blocked.get("hits", [])), "product filter should block unrelated product")


def check_risk_hit_retrieves_but_answer_layer_blocks() -> None:
    service = make_service()
    seed_sources(service)
    result = service.search("老客户能不能月结先发货", limit=3)
    assert_true(result.get("hits"), "risk query should still be auditable in retrieval")
    hit = result["hits"][0]
    assert_true(hit.get("risk_terms"), "risk hit should expose risk terms")
    decision = ReplyDecision(reply_text="默认回复", rule_name=None, matched=False, need_handoff=True, reason="no_rule_matched")
    payload = maybe_build_rag_reply(
        config={"rag_response": {"enabled": True, "apply_to_unmatched": True, "min_hit_score": 0.12}},
        text="老客户能不能月结先发货",
        decision=decision,
        reply_text="默认回复",
        intent_assist={
            "intent": "product_detail",
            "recommended_action": "answer_from_evidence",
            "evidence": {"intent_tags": ["scene_product"], "safety": {"must_handoff": False}, "rag_hits": [hit]},
        },
        product_knowledge={},
        data_capture={},
    )
    assert_true(payload.get("applied") is False, "risk hit must not produce a RAG reply")
    assert_equal(payload.get("reason"), "rag_hit_or_query_has_risk_terms", "risk block reason should be explicit")


def check_experience_layer_active_and_discarded_retrieval() -> None:
    service = make_service()
    seed_sources(service)
    store = RagExperienceStore(tenant_id=TENANT_ID)
    record = store.record_reply(
        target="eval",
        message_ids=["eval-exp-001"],
        question="客户问办公室午休床收纳会不会占地方",
        reply_text="资料里提到 FB-100 可以折叠收纳，适合办公室午休和临时休息区。",
        raw_reply_text="资料里提到 FB-100 可以折叠收纳，适合办公室午休和临时休息区。",
        intent_assist={"intent": "product_detail", "recommended_action": "answer_from_evidence"},
        rag_reply={
            "applied": True,
            "hit": {
                "chunk_id": "eval-bed-chunk",
                "source_id": "eval-bed-source",
                "score": 0.88,
                "category": "product_explanations",
                "source_type": "product_doc",
                "product_id": "fb-100",
                "text": "FB-100 可以折叠收纳，适合办公室午休。",
                "risk_terms": [],
            },
        },
    )
    record = store.update_metadata(
        record["experience_id"],
        {
            "experience_review": {
                "status": "kept",
                "kept_reason": "enterprise eval keeps this experience for retrieval",
            },
            "reviewed_by_user": True,
        },
        rebuild_index=True,
    )
    active = service.search("办公室午休床占不占地方", product_id="fb-100", limit=10)
    assert_true(
        any(hit.get("source_id") == record["experience_id"] for hit in active.get("hits", [])),
        "manually kept experience should be searchable",
    )
    low_record = store.record_reply(
        target="eval",
        message_ids=["eval-exp-low"],
        question="客户问午休床闲聊里的颜色偏好",
        reply_text="这个只能作为弱参考，不能当正式规则。",
        raw_reply_text="这个只能作为弱参考，不能当正式规则。",
        intent_assist={"intent": "product_detail", "recommended_action": "answer_from_evidence"},
        rag_reply={
            "applied": True,
            "hit": {
                "chunk_id": "eval-low-chunk",
                "source_id": "eval-low-source",
                "score": 0.04,
                "category": "product_explanations",
                "source_type": "product_doc",
                "product_id": "fb-100",
                "text": "闲聊里提到的颜色偏好，和正式资料关联很弱。",
                "risk_terms": [],
            },
        },
    )
    assert_true(low_record.get("quality", {}).get("retrieval_allowed") is False, "low-confidence experience should be blocked by quality gate")
    low_search = service.search("午休床颜色偏好", product_id="fb-100", limit=10)
    assert_true(not any(hit.get("source_id") == low_record["experience_id"] for hit in low_search.get("hits", [])), "low-confidence experience should not be searchable")
    store.discard(record["experience_id"], reason="eval discard")
    discarded = service.search("办公室午休床占不占地方", product_id="fb-100", limit=10)
    assert_true(not any(hit.get("source_id") == record["experience_id"] for hit in discarded.get("hits", [])), "discarded experience should not be searchable")


def check_operations_analytics_report() -> None:
    service = make_service()
    seed_sources(service)
    store = RagExperienceStore(tenant_id=TENANT_ID)
    store.record_reply(
        target="eval",
        message_ids=["eval-exp-ops"],
        question="客户问智能锁型号怎么看",
        reply_text="可以先看型号命名和适用场景。",
        raw_reply_text="可以先看型号命名和适用场景。",
        intent_assist={"intent": "product_detail", "recommended_action": "answer_from_evidence"},
        rag_reply={
            "applied": True,
            "hit": {
                "chunk_id": "eval-lock-chunk",
                "source_id": "eval-lock-source",
                "score": 0.9,
                "category": "product_explanations",
                "source_type": "product_doc",
                "product_id": "fl-920",
                "text": "FL-920 型号说明包含适用场景。",
                "risk_terms": [],
            },
        },
    )
    audit_root = TEST_ROOT / "runtime" / "logs"
    audit_root.mkdir(parents=True, exist_ok=True)
    (audit_root / "eval_audit.jsonl").write_text(
        json.dumps(
            {
                "action": "sent",
                "rag_reply": {"applied": True, "reason": "safe_rag_context_reply"},
                "rag_experience": {"experience_id": "eval-exp"},
                "intent_assist": {
                    "intent": "product_detail",
                    "evidence": {"rag_hits": [{"chunk_id": "eval-lock-chunk"}]},
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    report = RagOperationsAnalyzer(rag_service=service, experience_store=store, runtime_root=TEST_ROOT / "runtime").report()
    assert_true(report.get("ok") is True, "operations report should be ok")
    assert_true(report.get("rag_status", {}).get("chunk_count", 0) >= 3, "operations should include chunk count")
    assert_true(report.get("audit", {}).get("counters", {}).get("rag_reply_applied", 0) >= 1, "operations should count applied rag replies")


def cleanup() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    tenant_path = tenant_root(TENANT_ID)
    if tenant_path.exists():
        shutil.rmtree(tenant_path)
    config = load_storage_config()
    if config.use_postgres and config.postgres_configured:
        store = get_postgres_store(tenant_id=TENANT_ID, config=config)
        if store.available():
            for table in ("rag_index_entries", "rag_chunks", "rag_sources", "rag_experiences", "audit_events"):
                store.execute(f"DELETE FROM {store.schema}.{table} WHERE tenant_id = %s", [TENANT_ID])


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


CHECKS = [
    check_hybrid_semantic_recall,
    check_product_filter_and_rerank,
    check_risk_hit_retrieves_but_answer_layer_blocks,
    check_experience_layer_active_and_discarded_retrieval,
    check_operations_analytics_report,
]


if __name__ == "__main__":
    raise SystemExit(main())
