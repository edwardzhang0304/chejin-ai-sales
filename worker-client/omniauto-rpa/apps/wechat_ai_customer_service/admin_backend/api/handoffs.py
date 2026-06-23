"""Human handoff case APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services.handoff_store import HandoffStore


router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])
store = HandoffStore()


@router.get("")
def list_cases(status: str = Query("open"), limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "items": store.list_cases(status=status, limit=limit)}


@router.get("/summary")
def summary() -> dict[str, Any]:
    return {"ok": True, "summary": store.summary()}


@router.get("/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    item = store.get_case(case_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"handoff case not found: {case_id}")
    return {"ok": True, "item": item}


@router.post("/{case_id}/acknowledge")
def acknowledge(case_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    item = store.update_status(case_id, "acknowledged", payload or {})
    if not item:
        raise HTTPException(status_code=404, detail=f"handoff case not found: {case_id}")
    return {"ok": True, "item": item}


@router.post("/{case_id}/resolve")
def resolve(case_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    item = store.update_status(case_id, "resolved", payload or {})
    if not item:
        raise HTTPException(status_code=404, detail=f"handoff case not found: {case_id}")
    return {"ok": True, "item": item}


@router.post("/{case_id}/ignore")
def ignore(case_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    item = store.update_status(case_id, "ignored", payload or {})
    if not item:
        raise HTTPException(status_code=404, detail=f"handoff case not found: {case_id}")
    return {"ok": True, "item": item}

