"""VPS-LOCAL sync, backup, shared-patch, and update APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..auth_context import current_auth_context
from apps.wechat_ai_customer_service.auth import assert_allowed
from apps.wechat_ai_customer_service.sync import BackupService, VpsLocalSyncService


router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    return VpsLocalSyncService().status(tenant_id=context.tenant_id)


@router.post("/register-node")
def register_node(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="backups", action="backup", tenant_id=context.tenant_id)
    payload = payload or {}
    return VpsLocalSyncService().register_node(
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        tenant_id=context.tenant_id,
        display_name=str(payload.get("display_name") or ""),
    )


@router.post("/backup")
def backup(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    payload = payload or {}
    tenant_id = str(payload.get("tenant_id") or context.tenant_id)
    scope = str(payload.get("scope") or "tenant")
    assert_allowed(context, resource="backups", action="backup", tenant_id=tenant_id)
    if scope == "all":
        assert_allowed(context, resource="commands", action="execute", tenant_id=tenant_id)
    try:
        return BackupService().build_backup(
            scope=scope,
            tenant_id=tenant_id,
            include_derived=bool(payload.get("include_derived", False)),
            include_runtime=bool(payload.get("include_runtime", False)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/shared/formal-candidates")
def upload_formal_shared_candidates(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="shared_knowledge", action="sync", tenant_id=context.tenant_id)
    payload = payload or {}
    try:
        limit = int(payload.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    return VpsLocalSyncService().upload_formal_knowledge_candidates(
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        tenant_id=context.tenant_id,
        use_llm=payload.get("use_llm", True) is not False,
        limit=limit,
        only_unscanned=payload.get("only_unscanned", True) is not False,
    )


@router.post("/shared/cloud-snapshot")
def pull_shared_cloud_snapshot(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="shared_knowledge", action="sync", tenant_id=context.tenant_id)
    payload = payload or {}
    return VpsLocalSyncService().fetch_shared_knowledge_snapshot(
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        tenant_id=context.tenant_id,
        since_version=str(payload.get("since_version") or ""),
        force=bool(payload.get("force", False)),
    )


@router.post("/commands/poll")
def poll_commands(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="commands", action="execute", tenant_id=context.tenant_id)
    return VpsLocalSyncService().poll_commands(
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        tenant_id=context.tenant_id,
    )


@router.post("/recorder/module-bindings")
def pull_recorder_module_bindings(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    payload = payload or {}
    requested_user_id = str(payload.get("user_id") or "").strip()
    user_id = requested_user_id or str(context.user.user_id or "")
    return VpsLocalSyncService().fetch_recorder_module_bindings(
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        tenant_id=context.tenant_id,
        user_id=user_id,
    )


@router.get("/update/check")
def check_update(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="updates", action="sync", tenant_id=context.tenant_id)
    return VpsLocalSyncService().check_update(token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip())
