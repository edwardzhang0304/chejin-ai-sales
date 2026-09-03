from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Literal

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.schemas.knowledge_management import (
    KnowledgeDraftRequest,
    KnowledgePublishConfirmRequest,
    KnowledgePublishPreviewRequest,
    KnowledgeRollbackPreviewRequest,
)
from app.services import knowledge_management_service as service


router = APIRouter(tags=["knowledge-management"], dependencies=[Depends(require_admin_auth)])


def _commit(
    db: Session,
    callback,
    *,
    actor: ActorContext | None = None,
    operation: str | None = None,
    target_id: str | None = None,
):
    try:
        result = callback()
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        if actor is not None and operation:
            service.record_operation_failure(
                actor,
                operation=operation,
                target_id=target_id,
                error=exc,
            )
        raise


@router.get("/knowledge/summary")
def summary(
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(_commit(db, lambda: service.dashboard_summary(db)))


@router.get("/knowledge/items")
def items(
    keyword: str | None = None,
    status: Literal["all", "draft", "published", "archived"] | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.list_items(
                db,
                keyword=keyword,
                status=status,
                page=page,
                page_size=page_size,
            ),
        )
    )


@router.get("/knowledge/items/{item_id}")
def item(
    item_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(_commit(db, lambda: service.get_item(db, item_id)))


@router.post("/knowledge/items")
def create_draft(
    payload: KnowledgeDraftRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.create_draft(db, title=payload.title, content=payload.content, actor=actor),
            actor=actor,
            operation="create_draft",
        )
    )


@router.put("/knowledge/items/{item_id}/draft")
def update_draft(
    item_id: str,
    payload: KnowledgeDraftRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.update_draft(
                db,
                item_id=item_id,
                title=payload.title,
                content=payload.content,
                expected_updated_at=payload.expected_updated_at,
                actor=actor,
            ),
            actor=actor,
            operation="update_draft",
            target_id=item_id,
        )
    )


@router.post("/knowledge/items/{item_id}/archive")
def archive_draft(
    item_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.stage_archive(db, item_id=item_id, actor=actor),
            actor=actor,
            operation="archive_draft",
            target_id=item_id,
        )
    )


@router.post("/knowledge/seeds/import-drafts")
def import_seed_drafts(
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.import_seed_drafts(db, actor),
            actor=actor,
            operation="import_seed_drafts",
        )
    )


@router.post("/knowledge/releases/preview")
def preview_release(
    payload: KnowledgePublishPreviewRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.create_preview(db, payload, actor),
            actor=actor,
            operation=f"preview_{payload.operation}",
            target_id=payload.item_id,
        )
    )


@router.post("/knowledge/releases")
def publish_release(
    payload: KnowledgePublishConfirmRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.confirm_preview(
                db,
                preview_id=payload.preview_id,
                content_digest=payload.content_digest,
                actor=actor,
            ),
            actor=actor,
            operation="confirm_publish",
            target_id=payload.preview_id,
        )
    )


@router.get("/knowledge/releases")
def releases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(_commit(db, lambda: service.list_releases(db, page=page, page_size=page_size)))


@router.get("/knowledge/releases/{release_id}")
def release(
    release_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(_commit(db, lambda: service.get_release(db, release_id)))


@router.post("/knowledge/releases/rollback/preview")
def preview_rollback(
    payload: KnowledgeRollbackPreviewRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: service.create_rollback_preview(db, payload.target_release_id, actor),
            actor=actor,
            operation="preview_rollback",
            target_id=payload.target_release_id,
        )
    )
