"""Version snapshot APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services.version_store import VersionStore


router = APIRouter(prefix="/api/versions", tags=["versions"])
store = VersionStore()


@router.get("")
def list_versions() -> dict[str, Any]:
    return {"ok": True, "items": store.list_versions()}


@router.get("/{version_id}")
def get_version(version_id: str) -> dict[str, Any]:
    return {"ok": True, "item": store.get_version(version_id)}


@router.get("/{version_id}/download")
def download_version(version_id: str) -> FileResponse:
    try:
        package_path = store.download_path(version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"version not found: {version_id}") from exc
    return FileResponse(package_path, filename=package_path.name, media_type="application/zip")


@router.post("")
def create_version(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    snapshot = store.create_snapshot(
        reason=str(payload.get("reason") or "manual backup"),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {"source": "admin_console"},
    )
    return {"ok": True, "item": snapshot}


@router.post("/{version_id}/rollback")
def rollback(version_id: str) -> dict[str, Any]:
    return store.rollback(version_id)
