"""One server-owned effective view; no mutation of immutable MessageEvent rows."""
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.models.message_text_correction import MessageTextCorrection
from app.models.wechat import MessageEvent
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def effective_versions(db, messages) -> dict[str, dict]:
    result = {m.id: {"text": m.content, "version": 0, "correction_id": None,
                     "sha256": text_sha256(m.content or "")} for m in messages}
    if not result:
        return result
    rows = db.scalars(select(MessageTextCorrection).where(
        MessageTextCorrection.message_event_id.in_(result)).order_by(MessageTextCorrection.effective_version))
    for row in rows:
        result[row.message_event_id] = {"text": row.effective_text, "version": row.effective_version,
                                       "correction_id": row.id, "sha256": row.effective_text_sha256}
    return result


def effective_context_digest(db, conversation_id: str) -> str:
    rows = db.execute(select(MessageTextCorrection.message_event_id,
        MessageTextCorrection.effective_version, MessageTextCorrection.effective_text_sha256).where(
        MessageTextCorrection.conversation_id == conversation_id).order_by(MessageTextCorrection.effective_version))
    latest = {row[0]: [row[1], row[2]] for row in rows}
    # Empty is the exact released baseline, including actions without metadata.
    return (hashlib.sha256(json.dumps(latest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if latest else "")


def comparison_views(db, messages):
    """Detached comparison copies; never mark an immutable ORM fact dirty."""
    versions = effective_versions(db, messages)
    result = []
    for message in messages:
        effective = versions[message.id]
        if not effective["version"]:
            result.append(message)
            continue
        raw = json.loads(json.dumps(message.raw_payload or {}))
        observation = dict(raw.get("observation") or {})
        observation["content_clean"] = effective["text"]
        raw["observation"] = observation
        projection = dict(raw.get("business_projection") or {})
        if projection:
            projection["normalized_content_signature"] = text_sha256(
                shared_adapter("text_correspondence").business_comparison_text(effective["text"]))
            raw["business_projection"] = projection
        values = {column.key: getattr(message, column.key) for column in MessageEvent.__table__.columns}
        result.append(SimpleNamespace(**{**values, "content": effective["text"], "raw_payload": raw}))
    return result


def require_current_context(db, batch):
    if (batch.ai_request_snapshot or {}).get("effective_context_digest", "") != effective_context_digest(db, batch.conversation_id):
        raise AppError("HISTORICAL_TEXT_CONTEXT_STALE", "历史文字已经更正，旧回复不能发送", 409,
                       {"suggested_action": "wait_for_fresh_read"})
