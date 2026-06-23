"""Recorder export-run and module-binding APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..auth_context import current_auth_context
from ..services.recorder_export_run_service import RecorderExportRunService
from ..services.recorder_module_registry import RecorderModuleRegistryService
from ..services.recorder_runtime import RecorderRuntime


router = APIRouter(prefix="/api/recorder", tags=["recorder"])


@router.get("/modules")
def list_modules(include_inactive: bool = Query(default=True)) -> dict[str, Any]:
    return {"ok": True, "items": RecorderModuleRegistryService().list_modules(include_inactive=include_inactive)}


@router.post("/modules")
def upsert_module(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        item = RecorderModuleRegistryService().upsert_module(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.get("/module-bindings")
def list_module_bindings(
    scope_type: str = Query(default=""),
    scope_id: str = Query(default=""),
    tenant_id: str = Query(default=""),
    user_id: str = Query(default=""),
) -> dict[str, Any]:
    items = RecorderModuleRegistryService().list_bindings(
        scope_type=scope_type,
        scope_id=scope_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    return {"ok": True, "items": items}


@router.post("/module-bindings")
def upsert_module_binding(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        item = RecorderModuleRegistryService().upsert_binding(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.delete("/module-bindings/{binding_id}")
def delete_module_binding(binding_id: str) -> dict[str, Any]:
    deleted = RecorderModuleRegistryService().delete_binding(binding_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"module binding not found: {binding_id}")
    return {"ok": True, "deleted": True}


@router.post("/exports/runs")
def create_export_run(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    runtime = RecorderRuntime(tenant_id=context.tenant_id)
    payload = payload or {}
    ensure_worker = payload.get("ensure_worker", False) is True
    worker: dict[str, Any] = {}
    try:
        service = RecorderExportRunService(tenant_id=context.tenant_id)
        item = service.create_run(
            payload,
            requested_by_user_id=str(context.user.user_id or ""),
            requested_by_username=str(context.user.username or ""),
            requested_module_key=str(payload.get("module_key") or ""),
        )
        if ensure_worker:
            worker = runtime.ensure_export_worker()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_status = worker.get("item") if isinstance(worker.get("item"), dict) else runtime.status()
    return {"ok": True, "item": item, "runtime": runtime_status, "worker": worker}


@router.get("/exports/runs")
def list_export_runs(
    request: Request,
    status: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
    ensure_worker: bool = Query(default=False),
) -> dict[str, Any]:
    context = current_auth_context(request)
    service = RecorderExportRunService(tenant_id=context.tenant_id)
    items = service.list_runs(status=status, limit=limit)
    runtime = RecorderRuntime(tenant_id=context.tenant_id)
    worker: dict[str, Any] = {}
    if ensure_worker:
        for item in items:
            if str(item.get("status") or "") in {"queued", "running"}:
                run_id = str(item.get("run_id") or "")
                if run_id:
                    try:
                        service.ensure_run_job(run_id)
                    except (ValueError, FileNotFoundError):
                        pass
    if ensure_worker and any(str(item.get("status") or "") in {"queued", "running"} for item in items):
        worker = runtime.ensure_export_worker()
    runtime_status = worker.get("item") if isinstance(worker.get("item"), dict) else runtime.status()
    return {"ok": True, "items": items, "runtime": runtime_status, "worker": worker}


@router.get("/exports/runs/{run_id}")
def get_export_run(run_id: str, request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    item = RecorderExportRunService(tenant_id=context.tenant_id).get_run(run_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"export run not found: {run_id}")
    return {"ok": True, "item": item}


@router.delete("/exports/runs/{run_id}")
def delete_export_run(run_id: str, request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    service = RecorderExportRunService(tenant_id=context.tenant_id)
    try:
        result = service.delete_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=f"export run not found: {run_id}")
    return {"ok": True, **result}


@router.get("/exports/runs/{run_id}/download")
def download_export_run(run_id: str, request: Request) -> FileResponse:
    context = current_auth_context(request)
    service = RecorderExportRunService(tenant_id=context.tenant_id)
    item = service.get_run(run_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"export run not found: {run_id}")
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
    path = Path(str(artifacts.get("xlsx_path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"export file not found for run: {run_id}")
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/exports/runs/{run_id}/report")
def download_export_run_report(run_id: str, request: Request) -> FileResponse:
    context = current_auth_context(request)
    service = RecorderExportRunService(tenant_id=context.tenant_id)
    item = service.get_run(run_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"export run not found: {run_id}")
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
    path = Path(str(artifacts.get("report_path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"export report not found for run: {run_id}")
    return FileResponse(path, filename=path.name, media_type="application/json")
