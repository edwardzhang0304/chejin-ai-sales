from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.core.database import SessionLocal
from app.errors import AppError
from app.models.base import new_id, utcnow
from app.models.c3 import MessageBatch
from app.models.knowledge_management import (
    CurrentKnowledgeRelease,
    KnowledgePublishPreview,
    KnowledgeRelease,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
)
from app.schemas.knowledge_management import KnowledgePublishPreviewRequest
from app.services.audit_service import write_log
from app.services.chejin_knowledge_seed import SEED_ITEMS, TENANT_ID


PREVIEW_TTL_MINUTES = 15
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
TITLE_MAX_CHARS = 80
CONTENT_MAX_CHARS = 5000
SENSITIVE_PATTERNS = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "疑似包含个人手机号"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "疑似包含身份证号"),
    (re.compile(r"(?i)(api[_ -]?key|access[_ -]?token|secret|密码)\s*[:：=]"), "疑似包含密钥或密码"),
)
HARD_RULE_CONFLICTS = (
    ("保证最低价", "不得通过知识承诺最低价"),
    ("保证有现车", "不得通过知识承诺实时库存"),
    ("百分百有车", "不得通过知识承诺实时库存"),
    ("无需人工确认合同", "合同必须经过人工硬门禁"),
    ("可以代收定金", "支付和定金必须经过人工硬门禁"),
)
logger = logging.getLogger(__name__)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_text(value: str | None) -> str:
    return str(value or "").strip()


def _utc_datetime(value: datetime) -> datetime:
    """Normalize SQLite's naive DateTime result and PostgreSQL timestamptz."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def record_operation_failure(
    actor: ActorContext,
    *,
    operation: str,
    target_id: str | None,
    error: Exception,
) -> None:
    """Persist a failed mutation after the business transaction rolls back."""

    error_code = error.code if isinstance(error, AppError) else "INTERNAL_ERROR"
    status_code = error.status_code if isinstance(error, AppError) else 500
    try:
        with SessionLocal() as audit_db:
            write_log(
                audit_db,
                actor,
                event_type="knowledge_operation_failed",
                module="knowledge",
                target_type=("knowledge_release" if "rollback" in operation else "knowledge_item"),
                target_id=target_id,
                metadata={
                    "operation": operation,
                    "error_code": error_code,
                    "error_type": type(error).__name__,
                    "status_code": status_code,
                },
            )
            audit_db.commit()
    except Exception:
        logger.exception(
            "knowledge failure audit persistence failed operation=%s request_id=%s",
            operation,
            actor.request_id,
        )


def _snapshot_item(*, item_id: str, revision_id: str, title: str, content: str) -> dict[str, str]:
    return {
        "item_id": item_id,
        "revision_id": revision_id,
        "title": title,
        "content": content,
        "content_sha256": _digest({"title": title, "content": content}),
    }


def _sorted_snapshot(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(item) for item in items), key=lambda item: (str(item.get("title") or ""), str(item.get("item_id") or "")))


def _search_terms(value: str) -> list[str]:
    """Build deterministic lexical terms without relying on a mutable RAG store."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    terms: set[str] = set(re.findall(r"[a-z0-9]+", normalized))
    for block in re.findall(r"[\u3400-\u9fff]+", normalized):
        terms.update(character for character in block if character.strip())
        terms.update(block[index : index + 2] for index in range(max(0, len(block) - 1)))
        terms.update(block[index : index + 3] for index in range(max(0, len(block) - 2)))
    return sorted(term for term in terms if term)


def build_retrieval_index(snapshot: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the immutable per-release index before the current pointer moves."""

    documents: list[dict[str, Any]] = []
    for item in snapshot:
        documents.append(
            {
                "item_id": str(item.get("item_id") or ""),
                "revision_id": str(item.get("revision_id") or ""),
                "title_terms": _search_terms(str(item.get("title") or "")),
                "content_terms": _search_terms(str(item.get("content") or "")),
            }
        )
    documents.sort(key=lambda item: (item["item_id"], item["revision_id"]))
    return {"schema_version": 1, "documents": documents}


def _release_version(db: Session, now: datetime) -> str:
    day = _utc_datetime(now).astimezone(BUSINESS_TIMEZONE).strftime("%Y%m%d")
    prefix = f"KR-{day}-"
    existing = list(
        db.scalars(
            select(KnowledgeRelease.version).where(
                KnowledgeRelease.tenant_id == TENANT_ID,
                KnowledgeRelease.version.like(f"{prefix}%"),
            )
        )
    )
    sequence = max(
        (
            int(value.removeprefix(prefix))
            for value in existing
            if value.removeprefix(prefix).isdigit()
        ),
        default=0,
    ) + 1
    return f"{prefix}{sequence:02d}"


def ensure_initial_release(db: Session) -> KnowledgeRelease:
    """Create an empty immutable baseline without publishing repository seeds."""

    pointer = db.get(CurrentKnowledgeRelease, TENANT_ID)
    if pointer:
        release = db.get(KnowledgeRelease, pointer.release_id)
        if release:
            return release
        raise AppError("KNOWLEDGE_RELEASE_POINTER_INVALID", "当前知识版本指针无效", 500)

    now = utcnow()
    release_id = new_id()
    snapshot: list[dict[str, str]] = []
    retrieval_index = build_retrieval_index(snapshot)
    release = KnowledgeRelease(
        id=release_id,
        tenant_id=TENANT_ID,
        version=_release_version(db, now),
        status="published",
        action="bootstrap",
        operator_id=None,
        operator_name="system",
        change_summary="初始化空知识版本",
        change_set=[],
        snapshot=snapshot,
        snapshot_sha256=_digest(snapshot),
        retrieval_index=retrieval_index,
        retrieval_index_sha256=_digest(retrieval_index),
        published_at=now,
    )
    # Flush the parent release (and the pending knowledge items) before adding
    # rows that reference it. SQLite does not enforce foreign keys by default,
    # while PostgreSQL does; relying on SQLAlchemy's incidental flush order
    # here can otherwise insert the current pointer before its release exists.
    db.add(release)
    db.flush()
    db.add(CurrentKnowledgeRelease(tenant_id=TENANT_ID, release_id=release_id, updated_at=now))
    # A deployment can contain active batches created before the knowledge
    # release column existed. Freeze those batches to the bootstrap snapshot
    # before any operator can publish a newer release; otherwise an old batch
    # could silently adopt post-deployment knowledge on retry.
    db.flush()
    db.execute(
        update(MessageBatch)
        .where(MessageBatch.knowledge_release_id.is_(None))
        .values(knowledge_release_id=release_id)
    )
    db.flush()
    return release


def _current_release(db: Session, *, lock: bool = False) -> KnowledgeRelease:
    ensure_initial_release(db)
    query = select(CurrentKnowledgeRelease).where(CurrentKnowledgeRelease.tenant_id == TENANT_ID)
    if lock:
        query = query.with_for_update()
    pointer = db.scalar(query)
    if pointer is None:
        raise AppError("KNOWLEDGE_RELEASE_POINTER_MISSING", "当前知识版本不存在", 500)
    release = db.get(KnowledgeRelease, pointer.release_id)
    if release is None or release.status != "published":
        raise AppError("KNOWLEDGE_RELEASE_POINTER_INVALID", "当前知识版本不可用", 500)
    return release


def current_release_for_batch(db: Session) -> KnowledgeRelease:
    return _current_release(db)


def release_snapshot_for_batch(db: Session, release_id: str | None) -> dict[str, Any]:
    release = db.get(KnowledgeRelease, str(release_id or "")) if release_id else _current_release(db)
    if release is None or release.status != "published":
        raise AppError("KNOWLEDGE_RELEASE_NOT_AVAILABLE", "消息批次绑定的知识版本不可用", 409)
    return {
        "release_id": release.id,
        "version": release.version,
        "snapshot_sha256": release.snapshot_sha256,
        "retrieval_index": dict(release.retrieval_index or {}),
        "retrieval_index_sha256": release.retrieval_index_sha256,
        "items": [dict(item) for item in (release.snapshot or [])],
    }


def _item_dict(db: Session, item: ManagedKnowledgeItem) -> dict[str, Any]:
    draft = db.get(ManagedKnowledgeRevision, item.draft_revision_id) if item.draft_revision_id else None
    published = db.get(ManagedKnowledgeRevision, item.current_revision_id) if item.current_revision_id else None
    visible = draft or published
    return {
        "id": item.id,
        "title": visible.title if visible else item.title,
        "content": visible.content if visible else item.content,
        "status": item.status,
        "current_revision_id": item.current_revision_id,
        "draft_revision_id": item.draft_revision_id,
        "draft_operation": item.draft_operation,
        "published_title": published.title if published else None,
        "published_content": published.content if published else None,
        "last_editor_id": item.last_editor_id,
        "last_editor_name": item.last_editor_name,
        "published_at": item.published_at,
        "archived_at": item.archived_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _release_dict(release: KnowledgeRelease, *, current_release_id: str | None = None, include_snapshot: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": release.id,
        "version": release.version,
        "status": release.status,
        "action": release.action,
        "operator_name": release.operator_name,
        "change_summary": release.change_summary,
        "change_set": list(release.change_set or []),
        "snapshot_sha256": release.snapshot_sha256,
        "retrieval_index_sha256": release.retrieval_index_sha256,
        "published_at": release.published_at,
        "is_current": release.id == current_release_id,
    }
    if include_snapshot:
        result["snapshot"] = list(release.snapshot or [])
        result["retrieval_index"] = dict(release.retrieval_index or {})
    return result


def dashboard_summary(db: Session) -> dict[str, Any]:
    release = _current_release(db)
    now = _utc_datetime(utcnow())
    local_today = now.astimezone(BUSINESS_TIMEZONE).date()
    local_start = datetime.combine(local_today, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    releases_today = db.scalar(
        select(func.count()).select_from(KnowledgeRelease).where(
            KnowledgeRelease.tenant_id == TENANT_ID,
            KnowledgeRelease.status == "published",
            KnowledgeRelease.action != "bootstrap",
            KnowledgeRelease.published_at >= start_utc,
            KnowledgeRelease.published_at < end_utc,
        )
    ) or 0
    today_action_rows = db.execute(
        select(KnowledgeRelease.action, func.count())
        .where(
            KnowledgeRelease.tenant_id == TENANT_ID,
            KnowledgeRelease.status == "published",
            KnowledgeRelease.action != "bootstrap",
            KnowledgeRelease.published_at >= start_utc,
            KnowledgeRelease.published_at < end_utc,
        )
        .group_by(KnowledgeRelease.action)
    ).all()
    today_breakdown = {"create": 0, "update": 0, "archive": 0, "rollback": 0}
    for action, count in today_action_rows:
        if action in today_breakdown:
            today_breakdown[action] = int(count or 0)
    published = len(release.snapshot or [])
    drafts = db.scalar(
        select(func.count()).select_from(ManagedKnowledgeItem).where(
            ManagedKnowledgeItem.tenant_id == TENANT_ID,
            ManagedKnowledgeItem.status == "draft",
        )
    ) or 0
    archived = db.scalar(
        select(func.count()).select_from(ManagedKnowledgeItem).where(
            ManagedKnowledgeItem.tenant_id == TENANT_ID,
            ManagedKnowledgeItem.status == "archived",
        )
    ) or 0
    return {
        "current_release": _release_dict(release, current_release_id=release.id),
        "published_today": int(releases_today),
        "published_today_breakdown": today_breakdown,
        "published_count": int(published),
        "draft_count": int(drafts),
        "archived_count": int(archived),
    }


def list_items(db: Session, *, keyword: str | None, status: str | None, page: int, page_size: int) -> dict[str, Any]:
    _current_release(db)
    query = select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.tenant_id == TENANT_ID)
    if status in {"draft", "published", "archived"}:
        query = query.where(ManagedKnowledgeItem.status == status)
    if _normalized_text(keyword):
        token = f"%{_normalized_text(keyword)}%"
        revision_items = select(ManagedKnowledgeRevision.item_id).where(
            or_(
                ManagedKnowledgeRevision.title.ilike(token),
                ManagedKnowledgeRevision.content.ilike(token),
            )
        )
        query = query.where(
            or_(
                ManagedKnowledgeItem.title.ilike(token),
                ManagedKnowledgeItem.content.ilike(token),
                ManagedKnowledgeItem.id.in_(revision_items),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(
        db.scalars(
            query.order_by(ManagedKnowledgeItem.updated_at.desc(), ManagedKnowledgeItem.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {"items": [_item_dict(db, item) for item in rows], "page": page, "page_size": page_size, "total": total}


def get_item(db: Session, item_id: str) -> dict[str, Any]:
    _current_release(db)
    item = db.get(ManagedKnowledgeItem, item_id)
    if item is None or item.tenant_id != TENANT_ID:
        raise AppError("KNOWLEDGE_ITEM_NOT_FOUND", "知识不存在", 404)
    releases = list(
        db.scalars(
            select(KnowledgeRelease)
            .where(KnowledgeRelease.tenant_id == TENANT_ID)
            .order_by(KnowledgeRelease.published_at.desc())
        )
    )
    data = _item_dict(db, item)
    data["release_history"] = [
        _release_dict(release)
        for release in releases
        if any(str(change.get("item_id") or "") == item.id for change in (release.change_set or []))
    ][:20]
    return data


def _validate_item(db: Session, *, item_id: str | None, title: str, content: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not title:
        issues.append({"field": "title", "problem": "知识标题不能为空", "suggestion": "填写便于运营识别的标题"})
    elif len(title) > TITLE_MAX_CHARS:
        issues.append({"field": "title", "problem": f"知识标题不能超过 {TITLE_MAX_CHARS} 个字符", "suggestion": "缩短标题"})
    if not content:
        issues.append({"field": "content", "problem": "规则正文不能为空", "suggestion": "填写 Brain 可引用的正式知识"})
    elif len(content) > CONTENT_MAX_CHARS:
        issues.append({"field": "content", "problem": f"规则正文不能超过 {CONTENT_MAX_CHARS} 个字符", "suggestion": "拆分或精简正文"})
    for pattern, problem in SENSITIVE_PATTERNS:
        if pattern.search(f"{title}\n{content}"):
            issues.append({"field": "content", "problem": problem, "suggestion": "删除敏感信息后再发布"})
    for phrase, problem in HARD_RULE_CONFLICTS:
        if phrase in content:
            issues.append({"field": "content", "problem": problem, "suggestion": "删除与业务硬门禁冲突的承诺"})
    # The release snapshot, not the mutable operator-facing item status, is the
    # authority for what is online.  A published item with a pending draft is
    # represented as ``draft`` in the list but its old revision is still live.
    # Querying item.status here would miss that duplicate.
    current_snapshot = list(_current_release(db).snapshot or [])
    if any(
        str(item.get("item_id") or "") != str(item_id or "")
        and str(item.get("title") or "").lower() == title.lower()
        and str(item.get("content") or "") == content
        for item in current_snapshot
        if isinstance(item, dict)
    ):
        issues.append({"field": "title", "problem": "存在标题和正文完全相同的已发布知识", "suggestion": "编辑原知识或调整内容"})
    return issues


def _save_draft_revision(
    db: Session,
    *,
    item: ManagedKnowledgeItem | None,
    title: str,
    content: str,
    operation: str,
    actor: ActorContext,
) -> ManagedKnowledgeItem:
    now = utcnow()
    revision_id = new_id()
    if item is None:
        item = ManagedKnowledgeItem(
            id=new_id(),
            tenant_id=TENANT_ID,
            title=title,
            content="",
            status="draft",
            current_revision_id=None,
            draft_revision_id=revision_id,
            draft_operation="create",
            last_editor_id=str(actor.operator_id),
            last_editor_name=actor.operator_name,
            published_at=None,
            archived_at=None,
        )
        db.add(item)
        db.flush()
    else:
        item.title = title
        item.status = "draft"
        item.draft_revision_id = revision_id
        item.draft_operation = operation
        item.last_editor_id = str(actor.operator_id)
        item.last_editor_name = actor.operator_name
        item.archived_at = None
    db.add(
        ManagedKnowledgeRevision(
            id=revision_id,
            item_id=item.id,
            release_id=None,
            title=title,
            content=content,
            content_sha256=_digest({"title": title, "content": content}),
            status="draft",
            created_by_id=str(actor.operator_id),
            created_by_name=actor.operator_name,
            created_at=now,
        )
    )
    write_log(
        db,
        actor,
        event_type="knowledge_draft_saved",
        module="knowledge",
        target_type="knowledge_item",
        target_id=item.id,
        after_data={"draft_revision_id": revision_id, "operation": operation, "title": title},
    )
    db.flush()
    return item


def create_draft(db: Session, *, title: str, content: str, actor: ActorContext) -> dict[str, Any]:
    _current_release(db)
    item = _save_draft_revision(
        db,
        item=None,
        title=_normalized_text(title),
        content=_normalized_text(content),
        operation="create",
        actor=actor,
    )
    return _item_dict(db, item)


def update_draft(
    db: Session,
    *,
    item_id: str,
    title: str,
    content: str,
    expected_updated_at: datetime | None,
    actor: ActorContext,
) -> dict[str, Any]:
    _current_release(db)
    item = db.get(ManagedKnowledgeItem, item_id)
    if item is None or item.tenant_id != TENANT_ID:
        raise AppError("KNOWLEDGE_ITEM_NOT_FOUND", "知识不存在", 404)
    if expected_updated_at and _utc_datetime(item.updated_at) != _utc_datetime(expected_updated_at):
        raise AppError("KNOWLEDGE_ITEM_STALE", "知识已被其他人修改，请刷新后重试", 409)
    operation = "create" if item.current_revision_id is None else "update"
    item = _save_draft_revision(
        db,
        item=item,
        title=_normalized_text(title),
        content=_normalized_text(content),
        operation=operation,
        actor=actor,
    )
    return _item_dict(db, item)


def stage_archive(db: Session, *, item_id: str, actor: ActorContext) -> dict[str, Any]:
    _current_release(db)
    item = db.get(ManagedKnowledgeItem, item_id)
    if item is None or item.tenant_id != TENANT_ID or not item.current_revision_id:
        raise AppError("KNOWLEDGE_ITEM_NOT_FOUND", "已发布知识不存在", 404)
    if item.draft_revision_id:
        raise AppError(
            "KNOWLEDGE_DRAFT_CONFLICT",
            "该知识存在未发布草稿，请先处理草稿再归档",
            409,
        )
    published = db.get(ManagedKnowledgeRevision, item.current_revision_id)
    if published is None:
        raise AppError("KNOWLEDGE_REVISION_NOT_FOUND", "线上知识修订不存在", 500)
    item = _save_draft_revision(
        db,
        item=item,
        title=published.title,
        content=published.content,
        operation="archive",
        actor=actor,
    )
    return _item_dict(db, item)


def import_seed_drafts(db: Session, actor: ActorContext) -> dict[str, Any]:
    """Explicit operator action only; imported repository examples stay drafts."""

    _current_release(db)
    imported: list[dict[str, Any]] = []
    skipped: list[str] = []
    for definition in SEED_ITEMS:
        existing = db.get(ManagedKnowledgeItem, definition.item_id)
        if existing is not None:
            skipped.append(definition.item_id)
            continue
        item = ManagedKnowledgeItem(
            id=definition.item_id,
            tenant_id=TENANT_ID,
            title=definition.title,
            content="",
            status="draft",
            current_revision_id=None,
            last_editor_id=str(actor.operator_id),
            last_editor_name=actor.operator_name,
            published_at=None,
        )
        db.add(item)
        db.flush()
        item = _save_draft_revision(
            db,
            item=item,
            title=definition.title,
            content=definition.content,
            operation="create",
            actor=actor,
        )
        imported.append(_item_dict(db, item))
    return {"items": imported, "imported_count": len(imported), "skipped_item_ids": skipped}


def _changes_between(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_map = {str(item.get("item_id") or ""): item for item in before}
    after_map = {str(item.get("item_id") or ""): item for item in after}
    changes: list[dict[str, Any]] = []
    for item_id in sorted(before_map.keys() | after_map.keys()):
        old = before_map.get(item_id)
        new = after_map.get(item_id)
        if old is None and new is not None:
            changes.append({"type": "create", "item_id": item_id, "title": new.get("title"), "before": None, "after": new})
        elif new is None and old is not None:
            changes.append({"type": "archive", "item_id": item_id, "title": old.get("title"), "before": old, "after": None})
        elif _digest(old) != _digest(new):
            changes.append({"type": "update", "item_id": item_id, "title": new.get("title"), "before": old, "after": new})
    return changes


def create_preview(db: Session, payload: KnowledgePublishPreviewRequest, actor: ActorContext) -> dict[str, Any]:
    release = _current_release(db)
    operation = payload.operation
    item_id = _normalized_text(payload.item_id)
    current_item = db.get(ManagedKnowledgeItem, item_id) if item_id else None
    if current_item is None or current_item.tenant_id != TENANT_ID:
        raise AppError("KNOWLEDGE_ITEM_NOT_FOUND", "知识草稿不存在", 404)
    if current_item and payload.expected_updated_at:
        actual = _utc_datetime(current_item.updated_at)
        expected = _utc_datetime(payload.expected_updated_at)
        if actual != expected:
            raise AppError("KNOWLEDGE_ITEM_STALE", "知识已被其他人修改，请刷新后重试", 409, {"actual_updated_at": actual.isoformat()})

    if current_item.status != "draft" or not current_item.draft_revision_id:
        raise AppError("KNOWLEDGE_DRAFT_REQUIRED", "请先保存草稿再发布", 409)
    if current_item.draft_operation != operation:
        raise AppError("KNOWLEDGE_DRAFT_OPERATION_MISMATCH", "草稿操作与发布操作不一致", 409)
    draft = db.get(ManagedKnowledgeRevision, current_item.draft_revision_id)
    if draft is None or draft.status != "draft" or draft.release_id is not None:
        raise AppError("KNOWLEDGE_DRAFT_REVISION_INVALID", "知识草稿修订无效", 500)
    title = _normalized_text(draft.title)
    content = _normalized_text(draft.content)
    issues = [] if operation == "archive" else _validate_item(db, item_id=current_item.id, title=title, content=content)
    before = _sorted_snapshot(list(release.snapshot or []))
    after = [dict(item) for item in before if str(item.get("item_id") or "") != item_id]
    if operation != "archive":
        after.append(
            _snapshot_item(
                item_id=item_id,
                revision_id=draft.id,
                title=title,
                content=content,
            )
        )
    after = _sorted_snapshot(after)
    changes = _changes_between(before, after)
    if not changes and not issues:
        issues.append({"field": "content", "problem": "本次内容与当前线上版本完全一致", "suggestion": "修改内容后再发布"})
    now = utcnow()
    digest_payload = {
        "operator_id": str(actor.operator_id),
        "base_release_id": release.id,
        "operation": operation,
        "item_id": item_id,
        "draft_revision_id": draft.id,
        "after_snapshot": after,
    }
    preview = KnowledgePublishPreview(
        tenant_id=TENANT_ID,
        operation=operation,
        item_id=item_id,
        base_release_id=release.id,
        operator_id=str(actor.operator_id),
        operator_name=actor.operator_name,
        target_version=_release_version(db, now),
        payload={
            "title": title,
            "content": content,
            "draft_revision_id": draft.id,
            "expected_updated_at": payload.expected_updated_at.isoformat() if payload.expected_updated_at else None,
        },
        before_snapshot=before,
        after_snapshot=after,
        change_set=changes,
        validation_issues=issues,
        content_digest=_digest(digest_payload),
        consumed=False,
        expires_at=now + timedelta(minutes=PREVIEW_TTL_MINUTES),
        created_at=now,
    )
    db.add(preview)
    write_log(
        db,
        actor,
        event_type="knowledge_publish_previewed",
        module="knowledge",
        target_type="knowledge_item",
        target_id=item_id,
        before_data=changes[0].get("before") if changes else None,
        after_data=changes[0].get("after") if changes else None,
        metadata={
            "operation": operation,
            "target_version": preview.target_version,
            "validation_issue_count": len(issues),
            "title": title,
        },
    )
    db.flush()
    return preview_to_dict(preview, current_version=release.version)


def create_rollback_preview(db: Session, target_release_id: str, actor: ActorContext) -> dict[str, Any]:
    current = _current_release(db)
    target = db.get(KnowledgeRelease, target_release_id)
    if target is None or target.tenant_id != TENANT_ID or target.status != "published":
        raise AppError("KNOWLEDGE_RELEASE_NOT_ROLLBACKABLE", "目标知识版本不可回滚", 409)
    if target.id == current.id:
        raise AppError("KNOWLEDGE_RELEASE_ALREADY_CURRENT", "目标版本已是当前线上版本", 409)
    before = _sorted_snapshot(list(current.snapshot or []))
    after = _sorted_snapshot(list(target.snapshot or []))
    changes = _changes_between(before, after)
    now = utcnow()
    digest_payload = {
        "operator_id": str(actor.operator_id),
        "base_release_id": current.id,
        "operation": "rollback",
        "target_release_id": target.id,
        "after_snapshot": after,
    }
    preview = KnowledgePublishPreview(
        tenant_id=TENANT_ID,
        operation="rollback",
        target_release_id=target.id,
        base_release_id=current.id,
        operator_id=str(actor.operator_id),
        operator_name=actor.operator_name,
        target_version=_release_version(db, now),
        payload={"target_version": target.version},
        before_snapshot=before,
        after_snapshot=after,
        change_set=changes,
        validation_issues=[],
        content_digest=_digest(digest_payload),
        consumed=False,
        expires_at=now + timedelta(minutes=PREVIEW_TTL_MINUTES),
        created_at=now,
    )
    db.add(preview)
    write_log(
        db,
        actor,
        event_type="knowledge_rollback_previewed",
        module="knowledge",
        target_type="knowledge_release",
        target_id=target.id,
        before_data={"release_id": current.id, "version": current.version},
        after_data={"release_id": target.id, "version": target.version},
        metadata={"target_version": preview.target_version, "change_count": len(changes)},
    )
    db.flush()
    return preview_to_dict(preview, current_version=current.version)


def preview_to_dict(preview: KnowledgePublishPreview, *, current_version: str) -> dict[str, Any]:
    return {
        "preview_id": preview.id,
        "operation": preview.operation,
        "item_id": preview.item_id,
        "current_version": current_version,
        "target_version": preview.target_version,
        "target_release_id": preview.target_release_id,
        "can_publish": not bool(preview.validation_issues),
        "validation_issues": list(preview.validation_issues or []),
        "change_set": list(preview.change_set or []),
        "content_digest": preview.content_digest,
        "expires_at": preview.expires_at,
    }


def confirm_preview(db: Session, *, preview_id: str, content_digest: str, actor: ActorContext) -> dict[str, Any]:
    preview = db.scalar(select(KnowledgePublishPreview).where(KnowledgePublishPreview.id == preview_id).with_for_update())
    if preview is None:
        raise AppError("KNOWLEDGE_PREVIEW_NOT_FOUND", "发布预览不存在", 404)
    if preview.consumed:
        raise AppError("KNOWLEDGE_PREVIEW_ALREADY_USED", "发布预览已使用", 409)
    if preview.operator_id != str(actor.operator_id):
        raise AppError("KNOWLEDGE_PREVIEW_OPERATOR_MISMATCH", "发布预览只能由原操作人确认", 403)
    if _utc_datetime(preview.expires_at) < _utc_datetime(utcnow()):
        raise AppError("KNOWLEDGE_PREVIEW_EXPIRED", "发布预览已过期，请重新校验", 409)
    if preview.content_digest != content_digest:
        raise AppError("KNOWLEDGE_PREVIEW_DIGEST_MISMATCH", "发布内容已变化，请重新校验", 409)
    if preview.validation_issues:
        raise AppError("KNOWLEDGE_PREVIEW_VALIDATION_FAILED", "知识校验未通过", 409, {"issues": preview.validation_issues})

    current = _current_release(db, lock=True)
    if current.id != preview.base_release_id:
        raise AppError("KNOWLEDGE_RELEASE_STALE", "线上知识版本已变化，请刷新后重新发布", 409)

    draft_item = None
    draft_revision = None
    if preview.operation != "rollback":
        draft_item = db.get(ManagedKnowledgeItem, str(preview.item_id or ""))
        expected_draft_id = str((preview.payload or {}).get("draft_revision_id") or "")
        if (
            draft_item is None
            or draft_item.draft_revision_id != expected_draft_id
            or draft_item.draft_operation != preview.operation
        ):
            raise AppError("KNOWLEDGE_DRAFT_STALE", "知识草稿已变化，请重新预览", 409)
        draft_revision = db.get(ManagedKnowledgeRevision, expected_draft_id)
        if draft_revision is None or draft_revision.status != "draft" or draft_revision.release_id is not None:
            raise AppError("KNOWLEDGE_DRAFT_REVISION_INVALID", "知识草稿修订无效", 500)

    now = utcnow()
    release_id = new_id()
    after = _sorted_snapshot(list(preview.after_snapshot or []))
    before_map = {str(item.get("item_id") or ""): item for item in (preview.before_snapshot or [])}
    after_map = {str(item.get("item_id") or ""): item for item in after}
    retrieval_index = build_retrieval_index(after)
    indexed_ids = {str(document.get("item_id") or "") for document in retrieval_index["documents"]}
    if indexed_ids != set(after_map):
        raise AppError("KNOWLEDGE_INDEX_BUILD_FAILED", "知识检索索引构建不完整", 500)

    action_labels = {"create": "新增", "update": "修改", "archive": "归档", "rollback": "回滚"}
    release = KnowledgeRelease(
        id=release_id,
        tenant_id=TENANT_ID,
        version=preview.target_version,
        status="published",
        action=preview.operation,
        source_release_id=preview.target_release_id if preview.operation == "rollback" else current.id,
        operator_id=str(actor.operator_id),
        operator_name=actor.operator_name,
        change_summary=(
            f"回滚至 {preview.payload.get('target_version')} 的内容并生成新版本"
            if preview.operation == "rollback"
            else f"{action_labels[preview.operation]} {len(preview.change_set or [])} 条知识变更"
        ),
        change_set=list(preview.change_set or []),
        snapshot=after,
        snapshot_sha256=_digest(after),
        retrieval_index=retrieval_index,
        retrieval_index_sha256=_digest(retrieval_index),
        published_at=now,
    )
    db.add(release)
    # Keep PostgreSQL's FK order explicit: items and the immutable release are
    # durable in the transaction before revisions or the current pointer refer
    # to them. This remains one atomic commit; the intermediate flushes never
    # expose a partial publication.
    db.flush()
    if preview.operation == "rollback":
        all_items = list(db.scalars(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.tenant_id == TENANT_ID)))
        for item in all_items:
            snapshot_item = after_map.get(item.id)
            if snapshot_item is None:
                if item.draft_revision_id:
                    item.status = "draft"
                elif item.current_revision_id:
                    item.status = "archived"
                    item.archived_at = now
                continue
            item.current_revision_id = str(snapshot_item["revision_id"])
            item.title = str(snapshot_item["title"])
            item.content = str(snapshot_item["content"])
            item.published_at = now
            item.archived_at = None
            item.status = "draft" if item.draft_revision_id else "published"
    else:
        assert draft_item is not None and draft_revision is not None
        draft_revision.release_id = release.id
        draft_revision.status = "archived" if preview.operation == "archive" else "published"
        draft_item.current_revision_id = draft_revision.id
        draft_item.title = draft_revision.title
        draft_item.content = draft_revision.content
        draft_item.draft_revision_id = None
        draft_item.draft_operation = None
        draft_item.last_editor_id = str(actor.operator_id)
        draft_item.last_editor_name = actor.operator_name
        draft_item.published_at = now
        if preview.operation == "archive":
            draft_item.status = "archived"
            draft_item.archived_at = now
        else:
            draft_item.status = "published"
            draft_item.archived_at = None
    db.flush()
    pointer = db.get(CurrentKnowledgeRelease, TENANT_ID)
    if pointer is None:
        raise AppError("KNOWLEDGE_RELEASE_POINTER_MISSING", "当前知识版本不存在", 500)
    pointer.release_id = release.id
    pointer.updated_at = now
    preview.consumed = True

    event_type = {
        "create": "knowledge_published",
        "update": "knowledge_published",
        "archive": "knowledge_archived",
        "rollback": "knowledge_rolled_back",
    }[preview.operation]
    write_log(
        db,
        actor,
        event_type=event_type,
        module="knowledge",
        target_type="knowledge_release" if preview.operation == "rollback" else "knowledge_item",
        target_id=preview.target_release_id if preview.operation == "rollback" else preview.item_id,
        before_data={"release_id": current.id, "version": current.version, "snapshot_sha256": current.snapshot_sha256},
        after_data={"release_id": release.id, "version": release.version, "snapshot_sha256": release.snapshot_sha256},
        metadata={
            "operation": preview.operation,
            "change_set": preview.change_set,
            "title": (
                str((preview.change_set or [{}])[0].get("title") or "")
                if preview.change_set
                else ""
            ),
        },
    )
    db.flush()
    item = db.get(ManagedKnowledgeItem, preview.item_id) if preview.item_id else None
    return {
        "release": _release_dict(release, current_release_id=release.id, include_snapshot=True),
        "item": _item_dict(db, item) if item else None,
        "message": "新创建的 AI 对话批次将使用此版本",
    }


def list_releases(db: Session, *, page: int, page_size: int) -> dict[str, Any]:
    current = _current_release(db)
    query = select(KnowledgeRelease).where(KnowledgeRelease.tenant_id == TENANT_ID, KnowledgeRelease.status == "published")
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(KnowledgeRelease.published_at.desc()).offset((page - 1) * page_size).limit(page_size)))
    return {"items": [_release_dict(item, current_release_id=current.id) for item in rows], "page": page, "page_size": page_size, "total": total}


def get_release(db: Session, release_id: str) -> dict[str, Any]:
    current = _current_release(db)
    release = db.get(KnowledgeRelease, release_id)
    if release is None or release.tenant_id != TENANT_ID:
        raise AppError("KNOWLEDGE_RELEASE_NOT_FOUND", "知识发布版本不存在", 404)
    return _release_dict(release, current_release_id=current.id, include_snapshot=True)
