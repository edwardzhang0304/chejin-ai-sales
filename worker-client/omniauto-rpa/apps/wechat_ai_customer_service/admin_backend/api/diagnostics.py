"""Diagnostics APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..services.diagnostics_service import DiagnosticsService
from ..services.knowledge_base_store import KnowledgeBaseStore
from ..services.knowledge_compiler import KnowledgeCompiler
from ..services.knowledge_registry import KnowledgeRegistry
from ..services.knowledge_schema_manager import KnowledgeSchemaManager
from ..services.rag_admin_service import RagAdminService


router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])
service = DiagnosticsService()


def knowledge_components() -> tuple[KnowledgeRegistry, KnowledgeSchemaManager, KnowledgeBaseStore]:
    registry = KnowledgeRegistry()
    schema_manager = KnowledgeSchemaManager(registry)
    base_store = KnowledgeBaseStore(registry, schema_manager)
    return registry, schema_manager, base_store


@router.post("/run")
def run_diagnostics(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return service.run(
        mode=str(payload.get("mode") or "quick"),
        include_llm_probe=bool(payload.get("include_llm_probe", False)),
        include_wechat_live=bool(payload.get("include_wechat_live", False)),
        include_ignored=bool(payload.get("include_ignored", False)),
        include_llm_audit=bool(payload.get("include_llm_audit", True)),
        auto_dedup_rag=bool(payload.get("auto_dedup_rag", False)),
    )


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    return {"ok": True, "items": service.list_runs()}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return {"ok": True, "item": service.get_run(run_id)}


@router.post("/runs/{run_id}/apply-suggestion")
def apply_suggestion(run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return service.apply_suggestion(run_id, payload or {})


@router.get("/ignores")
def list_ignores() -> dict[str, Any]:
    return {"ok": True, "items": service.list_ignored()}


@router.post("/ignore")
def ignore_issue(payload: dict[str, Any]) -> dict[str, Any]:
    return service.ignore_issue(
        fingerprint=str(payload.get("fingerprint") or ""),
        reason=str(payload.get("reason") or ""),
    )


@router.post("/clear-notices")
def clear_acknowledged_notices(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return service.clear_acknowledged_notices(code=str(payload.get("code") or "knowledge_token_budget_large"))


@router.post("/merge-knowledge")
def merge_knowledge(payload: dict[str, Any]) -> dict[str, Any]:
    primary_target = str(payload.get("primary_target") or "").strip()
    secondary_targets = [str(t).strip() for t in payload.get("secondary_targets", []) if str(t).strip()]
    merged_data = payload.get("merged_data") if isinstance(payload.get("merged_data"), dict) else {}

    if not primary_target or not secondary_targets:
        return {"ok": False, "message": "primary_target and secondary_targets are required"}

    _, _, base_store = knowledge_components()

    # Parse primary target
    primary_parts = primary_target.split("/", 1)
    if len(primary_parts) != 2:
        return {"ok": False, "message": f"invalid primary_target: {primary_target}"}
    primary_category, primary_item_id = primary_parts

    primary_item = base_store.get_item(primary_category, primary_item_id)
    if not primary_item:
        return {"ok": False, "message": f"primary item not found: {primary_target}"}

    # Merge merged_data into primary item data
    if merged_data:
        primary_data = dict(primary_item.get("data") or {})
        primary_data.update(merged_data)
        primary_item["data"] = primary_data
        primary_item.setdefault("metadata", {})["updated_at"] = _now()

    save_result = base_store.save_item(primary_category, primary_item)
    if not save_result.get("ok"):
        return {"ok": False, "message": "failed to save merged primary item", "detail": save_result}

    archived = []
    for target in secondary_targets:
        parts = target.split("/", 1)
        if len(parts) != 2:
            continue
        category_id, item_id = parts
        try:
            result = base_store.archive_item(category_id, item_id)
            if result.get("ok"):
                archived.append(target)
        except Exception:
            pass

    KnowledgeCompiler().compile_to_disk()

    return {
        "ok": True,
        "item": save_result.get("item"),
        "archived": archived,
        "message": f"已合并 {primary_target}，已归档 {len(archived)} 条。",
    }


@router.post("/delete-target")
def delete_target(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or "").strip()
    if not target:
        return {"ok": False, "message": "target is required"}

    # AI experience pool target
    if target.startswith("rag_exp_"):
        try:
            result = RagAdminService().discard_experience(target, reason=str(payload.get("reason") or "deleted from diagnostics"))
            return {"ok": True, "message": f"AI经验池 {target} 已废弃。", "item": result.get("item")}
        except Exception as exc:
            return {"ok": False, "message": f"failed to discard AI experience pool item: {exc}"}

    # Knowledge item target: category_id/item_id
    parts = target.split("/", 1)
    if len(parts) != 2:
        return {"ok": False, "message": f"invalid target: {target}"}
    category_id, item_id = parts

    _, _, base_store = knowledge_components()
    try:
        result = base_store.archive_item(category_id, item_id)
        if result.get("ok"):
            KnowledgeCompiler().compile_to_disk()
            return {"ok": True, "message": f"知识条目 {target} 已归档。"}
        return {"ok": False, "message": result.get("message") or "archive failed"}
    except Exception as exc:
        return {"ok": False, "message": f"archive failed: {exc}"}


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
