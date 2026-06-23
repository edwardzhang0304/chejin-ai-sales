"""Draft editing APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..services.draft_store import DraftStore


router = APIRouter(prefix="/api/drafts", tags=["drafts"])
store = DraftStore()


@router.post("")
def create_draft(payload: dict[str, Any]) -> dict[str, Any]:
    return store.create_draft(
        target_file=str(payload.get("target_file") or ""),
        content=payload.get("content"),
        summary=str(payload.get("summary") or "manual draft"),
    )


@router.get("/{draft_id}")
def get_draft(draft_id: str) -> dict[str, Any]:
    return {"ok": True, "draft": store.get_draft(draft_id)}


@router.patch("/{draft_id}")
def update_draft(draft_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return store.update_draft(draft_id, content=payload.get("content"), summary=payload.get("summary"))


@router.get("/{draft_id}/diff")
def draft_diff(draft_id: str) -> dict[str, Any]:
    return {"ok": True, "diff": store.diff(draft_id)}


@router.post("/{draft_id}/validate")
def validate_draft(draft_id: str) -> dict[str, Any]:
    return store.validate_draft(draft_id)


@router.post("/{draft_id}/apply")
def apply_draft(draft_id: str) -> dict[str, Any]:
    return store.apply_draft(draft_id)


@router.delete("/{draft_id}")
def delete_draft(draft_id: str) -> dict[str, Any]:
    return store.delete_draft(draft_id)

