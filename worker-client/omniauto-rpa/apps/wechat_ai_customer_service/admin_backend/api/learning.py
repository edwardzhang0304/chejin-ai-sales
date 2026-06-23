"""AI learning job APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..services.learning_service import LearningService


router = APIRouter(prefix="/api/learning", tags=["learning"])


def learning_service() -> LearningService:
    return LearningService()


@router.post("/jobs")
def create_job(payload: dict[str, Any]) -> dict[str, Any]:
    return learning_service().create_job(
        upload_ids=[str(item) for item in payload.get("upload_ids", []) or []],
        use_llm=payload.get("use_llm", True) is not False,
    )


@router.get("/jobs")
def list_jobs() -> dict[str, Any]:
    return {"ok": True, "items": learning_service().list_jobs()}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return {"ok": True, "item": learning_service().get_job(job_id)}
