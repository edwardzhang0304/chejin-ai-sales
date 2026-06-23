"""AI knowledge generator APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services.knowledge_generator import KnowledgeGenerator


router = APIRouter(prefix="/api/generator", tags=["generator"])
service = KnowledgeGenerator()


@router.post("/sessions")
def create_session(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return service.create_session(
            str(payload.get("message") or ""),
            preferred_category_id=str(payload.get("preferred_category_id") or ""),
            use_llm=bool(payload.get("use_llm", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/messages")
def continue_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return service.continue_session(session_id, str(payload.get("message") or ""), use_llm=bool(payload.get("use_llm", True)))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}/draft")
def update_draft(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return service.update_draft(session_id, data)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/confirm")
def confirm_session(session_id: str) -> dict[str, Any]:
    try:
        result = service.confirm_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/sessions/{session_id}/rag-experience")
def confirm_session_to_rag_experience(session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        result = service.confirm_session_to_rag_experience(
            session_id,
            use_llm=(payload or {}).get("use_llm", True) is not False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result
