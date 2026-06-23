"""Standard workflow APIs: curation/import/eval/release lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..auth_context import current_auth_context
from ..services.workflow_service import WorkflowService


router = APIRouter(prefix="/api/workflow", tags=["workflow"])


def service(request: Request, tenant_id: str = "") -> WorkflowService:
    context = current_auth_context(request)
    resolved_tenant = str(tenant_id or context.tenant_id or "").strip()
    return WorkflowService(tenant_id=resolved_tenant)


@router.post("/curation/jobs")
def create_curation_job(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "")
    industry_id = str(payload.get("industry_id") or "")
    batch_id = str(payload.get("batch_id") or "")
    source_files = [str(item) for item in (payload.get("source_files") or []) if str(item).strip()]
    strict_mode = payload.get("strict_mode", True) is not False
    if not batch_id:
        raise HTTPException(status_code=400, detail="batch_id is required")
    if not source_files:
        raise HTTPException(status_code=400, detail="source_files is required")
    return service(request, tenant_id).create_curation_job(
        tenant_id=tenant_id,
        industry_id=industry_id,
        batch_id=batch_id,
        source_files=source_files,
        strict_mode=bool(strict_mode),
    )


@router.get("/curation/jobs/{job_id}")
def get_curation_job(job_id: str, request: Request, tenant_id: str = "") -> dict[str, Any]:
    item = service(request, tenant_id).get_curation_job(job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"curation job not found: {job_id}")
    return {"ok": True, "item": item}


@router.post("/template-import/dry-run")
def dry_run_template_import(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "")
    industry_id = str(payload.get("industry_id") or "")
    input_file = str(payload.get("input_file") or "")
    if not input_file:
        raise HTTPException(status_code=400, detail="input_file is required")
    result = service(request, tenant_id).dry_run_import(
        tenant_id=tenant_id,
        industry_id=industry_id,
        input_file=input_file,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.post("/template-import/apply")
def apply_template_import(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "")
    industry_id = str(payload.get("industry_id") or "")
    dry_run_job_id = str(payload.get("dry_run_job_id") or "")
    input_file = str(payload.get("input_file") or "")
    release_version = str(payload.get("release_version") or "")
    result = service(request, tenant_id).apply_import(
        tenant_id=tenant_id,
        industry_id=industry_id,
        dry_run_job_id=dry_run_job_id,
        input_file=input_file,
        release_version=release_version,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.get("/template-import/jobs/{job_id}")
def get_template_import_job(job_id: str, request: Request, tenant_id: str = "") -> dict[str, Any]:
    item = service(request, tenant_id).get_import_job(job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"import job not found: {job_id}")
    return {"ok": True, "item": item}


@router.post("/releases")
def create_release(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "")
    release_version = str(payload.get("release_version") or "")
    industry_id = str(payload.get("industry_id") or "")
    import_job_ids = [str(item) for item in (payload.get("import_job_ids") or []) if str(item).strip()]
    feature_flags = payload.get("feature_flags") if isinstance(payload.get("feature_flags"), dict) else {}
    metrics_gate = payload.get("metrics_gate") if isinstance(payload.get("metrics_gate"), dict) else {}
    result = service(request, tenant_id).create_release(
        tenant_id=tenant_id,
        release_version=release_version,
        import_job_ids=import_job_ids,
        feature_flags=feature_flags,
        industry_id=industry_id,
        metrics_gate=metrics_gate,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.get("/releases/{release_id}")
def get_release(release_id: str, request: Request, tenant_id: str = "") -> dict[str, Any]:
    item = service(request, tenant_id).get_release(release_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"release not found: {release_id}")
    return {"ok": True, "item": item}


@router.post("/releases/{release_id}/approve")
def approve_release(release_id: str, payload: dict[str, Any], request: Request, tenant_id: str = "") -> dict[str, Any]:
    approved_by = str(payload.get("approved_by") or "admin")
    approval_note = str(payload.get("approval_note") or "")
    result = service(request, tenant_id).approve_release(
        release_id=release_id,
        approved_by=approved_by,
        approval_note=approval_note,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.post("/releases/{release_id}/rollback")
def rollback_release(release_id: str, payload: dict[str, Any], request: Request, tenant_id: str = "") -> dict[str, Any]:
    rollback_to_version = str(payload.get("rollback_to_version") or "")
    reason = str(payload.get("reason") or "")
    result = service(request, tenant_id).rollback_release(
        release_id=release_id,
        rollback_to_version=rollback_to_version,
        reason=reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.post("/replay-eval/run")
def run_replay_eval(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "")
    release_version = str(payload.get("release_version") or "")
    suite_id = str(payload.get("suite_id") or "")
    suite_file = str(payload.get("suite_file") or "")
    metrics_gate = payload.get("metrics_gate") if isinstance(payload.get("metrics_gate"), dict) else {}
    if not release_version:
        raise HTTPException(status_code=400, detail="release_version is required")
    result = service(request, tenant_id).run_replay_eval(
        tenant_id=tenant_id,
        release_version=release_version,
        suite_id=suite_id,
        suite_file=suite_file,
        metrics_gate=metrics_gate,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result)
    return result


@router.get("/replay-eval/jobs/{eval_job_id}")
def get_replay_eval_job(eval_job_id: str, request: Request, tenant_id: str = "") -> dict[str, Any]:
    item = service(request, tenant_id).get_replay_eval_job(eval_job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"eval job not found: {eval_job_id}")
    return {"ok": True, "item": item}
