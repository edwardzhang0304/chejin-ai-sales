"""Knowledge export APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..auth_context import current_auth_context
from ..services.knowledge_export_service import KnowledgeExportService
from ..services.raw_chat_export_service import RawChatExportService


router = APIRouter(prefix="/api/exports", tags=["exports"])


@router.post("/knowledge")
def build_knowledge_export(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return KnowledgeExportService().build_export(sort_by=str(payload.get("sort_by") or "type"))


@router.get("/knowledge/download")
def download_knowledge_export(sort_by: str = Query("type", pattern="^(type|time)$")) -> FileResponse:
    result = KnowledgeExportService().build_export(sort_by=sort_by)
    path = Path(str(result.get("path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="export file not found")
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/raw-chats")
def build_raw_chat_export(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    mode = str(payload.get("mode") or "session")
    context = current_auth_context(request)
    return RawChatExportService(tenant_id=context.tenant_id).build_export(mode=mode)


@router.get("/raw-chats/download")
def download_raw_chat_export(
    request: Request,
    mode: str = Query("session", pattern="^(session|time)$"),
) -> FileResponse:
    context = current_auth_context(request)
    result = RawChatExportService(tenant_id=context.tenant_id).build_export(mode=mode)
    path = Path(str(result.get("path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="raw chat export file not found")
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
