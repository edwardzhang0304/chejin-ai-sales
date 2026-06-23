"""Background task handlers for the customer-service work queue."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root

APP_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
for path in (WORKFLOWS_ROOT, APP_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def handle_experience_interpretation(payload: dict[str, Any]) -> dict[str, Any]:
    """Process a raw message batch through LLM interpretation."""
    batch_id = str(payload.get("batch_id") or "").strip()
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    use_llm = payload.get("use_llm", True) is not False
    if not batch_id:
        return {"ok": False, "error": "missing batch_id"}
    try:
        from admin_backend.services.raw_message_learning_service import RawMessageLearningService

        result = RawMessageLearningService(tenant_id=tenant_id).process_batch(batch_id, use_llm=use_llm)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def handle_rag_quality_audit(payload: dict[str, Any]) -> dict[str, Any]:
    """Run AI review on a AI experience pool item."""
    experience_id = str(payload.get("experience_id") or "").strip()
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    use_llm = payload.get("use_llm", True) is not False
    if not experience_id:
        return {"ok": False, "error": "missing experience_id"}
    try:
        from rag_experience_store import RagExperienceStore
        from admin_backend.services.rag_experience_auto_review import auto_review_rag_experience

        store = RagExperienceStore(tenant_id=tenant_id)
        experience = store.get(experience_id)
        if not experience:
            return {"ok": False, "error": f"experience not found: {experience_id}"}
        reviewed = auto_review_rag_experience(experience, store=store, force=False, use_llm=use_llm)
        return {"ok": True, "experience_id": experience_id, "reviewed": reviewed}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def handle_knowledge_compile(payload: dict[str, Any]) -> dict[str, Any]:
    """Trigger KnowledgeCompiler to rebuild compiled knowledge files."""
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    try:
        from admin_backend.services.knowledge_compiler import KnowledgeCompiler
        from admin_backend.services.knowledge_runtime import KnowledgeRuntime

        runtime = KnowledgeRuntime(tenant_id=tenant_id)
        result = KnowledgeCompiler(runtime=runtime).compile_to_disk()
        return {"ok": True, "compiled": result}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def handle_customer_profile_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Analyze customer conversation and update profile tags via LLM."""
    target_name = str(payload.get("target_name") or "").strip()
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    if not target_name:
        return {"ok": False, "error": "missing target_name"}
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.customer_profile_analyzer import CustomerProfileAnalyzer

        analyzer = CustomerProfileAnalyzer(tenant_id=tenant_id)
        return analyzer.analyze(target_name)
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def handle_conversation_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize a long conversation (placeholder — not yet implemented)."""
    return {
        "ok": True,
        "status": "skipped",
        "reason": "conversation_summary_not_yet_implemented",
    }


def handle_customer_data_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Sync customer data to external systems (placeholder for now)."""
    return {
        "ok": True,
        "status": "skipped",
        "reason": "customer_data_sync_not_yet_implemented",
    }


def handle_raw_message_archive(payload: dict[str, Any]) -> dict[str, Any]:
    """Archive or expire old raw message batches."""
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    days = int(payload.get("days", 30))
    try:
        from admin_backend.services.raw_message_store import RawMessageStore

        store = RawMessageStore(tenant_id=tenant_id)
        batches = store.list_batches(limit=500)
        cutoff = _days_ago_iso(days)
        archived_count = 0
        for batch in batches:
            created = str(batch.get("created_at") or "")
            batch_id = str(batch.get("batch_id") or "")
            if created < cutoff and batch_id and str(batch.get("status") or "") not in {"archived", "skipped"}:
                store.update_batch(batch_id, {"status": "archived"})
                archived_count += 1
        return {"ok": True, "archived_count": archived_count, "checked_count": len(batches)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def handle_diagnostics_deep_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Run LLM-powered knowledge audit (placeholder — not yet implemented)."""
    return {
        "ok": True,
        "status": "skipped",
        "reason": "diagnostics_deep_check_not_yet_implemented",
    }


def handle_recorder_export_run_execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one recorder export run asynchronously."""
    run_id = str(payload.get("run_id") or "").strip()
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    if not run_id:
        return {"ok": False, "error": "missing run_id"}
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.recorder_export_run_service import RecorderExportRunService

        result = RecorderExportRunService(tenant_id=tenant_id).process_run(run_id)
        return {"ok": bool(result.get("ok")), **result}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "run_id": run_id}


def handle_rag_rebuild_index(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild tenant RAG index in background."""
    tenant_id = active_tenant_id(payload.get("tenant_id"))
    try:
        from apps.wechat_ai_customer_service.workflows.rag_layer import RagService

        rebuild = RagService(tenant_id=tenant_id).rebuild_index()
        return {
            "ok": bool(rebuild.get("ok", False)),
            "tenant_id": tenant_id,
            "rebuild": rebuild,
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "tenant_id": tenant_id}


JOB_HANDLERS: dict[str, Any] = {
    "experience_interpretation": handle_experience_interpretation,
    "rag_quality_audit": handle_rag_quality_audit,
    "rag_rebuild_index": handle_rag_rebuild_index,
    "knowledge_compile": handle_knowledge_compile,
    "conversation_summary": handle_conversation_summary,
    "customer_data_sync": handle_customer_data_sync,
    "raw_message_archive": handle_raw_message_archive,
    "diagnostics_deep_check": handle_diagnostics_deep_check,
    "customer_profile_analysis": handle_customer_profile_analysis,
    "recorder_export_run_execute": handle_recorder_export_run_execute,
}


def _days_ago_iso(days: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
