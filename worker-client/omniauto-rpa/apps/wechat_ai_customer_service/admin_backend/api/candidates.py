"""Review-candidate APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from ..auth_context import current_auth_context
from ..services.candidate_store import CandidateStore
from ..services.shared_public_sync import queue_shared_public_scan


router = APIRouter(prefix="/api/candidates", tags=["candidates"])


def candidate_store() -> CandidateStore:
    return CandidateStore()


@router.get("")
def list_candidates(
    status: str = Query("pending", pattern="^(pending|approved|rejected)$"),
    compact: bool = Query(False),
) -> dict[str, Any]:
    return {"ok": True, "items": candidate_store().list_candidates(status, compact=compact)}


@router.get("/{candidate_id}")
def get_candidate(candidate_id: str) -> dict[str, Any]:
    return {"ok": True, "item": candidate_store().get_candidate(candidate_id)}


@router.patch("/{candidate_id}")
def update_candidate(candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return candidate_store().update_candidate(candidate_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc


@router.post("/{candidate_id}/supplement")
def supplement_candidate(candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return candidate_store().supplement_candidate(candidate_id, data=(payload or {}).get("data") or {})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc


@router.post("/{candidate_id}/category")
def change_candidate_category(candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = candidate_store().change_candidate_category(
            candidate_id,
            target_category=str((payload or {}).get("target_category") or ""),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{candidate_id}/reject")
def reject_candidate(candidate_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return candidate_store().reject(candidate_id, reason=str((payload or {}).get("reason") or "rejected in admin"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc


@router.post("/{candidate_id}/approve")
def approve_candidate(candidate_id: str) -> dict[str, Any]:
    try:
        return candidate_store().approve(candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc


@router.post("/{candidate_id}/apply")
def apply_candidate(candidate_id: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        result = candidate_store().apply(candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"candidate not found: {candidate_id}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    saved_item = result.get("saved_item") if isinstance(result.get("saved_item"), dict) else {}
    nested_saved_item = saved_item.get("item") if isinstance(saved_item.get("item"), dict) else {}
    category_id = str(saved_item.get("category_id") or nested_saved_item.get("category_id") or "")
    if not category_id:
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        category_id = str(review.get("target_category") or "")
    context = current_auth_context(request)
    queue_shared_public_scan(
        background_tasks,
        tenant_id=context.tenant_id,
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        category_id=category_id,
    )
    return result
