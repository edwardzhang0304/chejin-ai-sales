"""Raw WeChat message APIs shared by customer-service and recorder modules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services.raw_message_learning_service import RawMessageLearningService
from ..services.raw_message_store import RawMessageStore


router = APIRouter(prefix="/api/raw-messages", tags=["raw-messages"])


@router.get("/summary")
def summary() -> dict[str, Any]:
    return {"ok": True, "item": RawMessageStore().summary()}


@router.get("/conversations")
def conversations(
    conversation_type: str = Query("", pattern="^(|private|group|file_transfer|system|unknown)$"),
    status: str = Query("all", pattern="^(all|active|paused|ignored)$"),
    limit: int = 200,
) -> dict[str, Any]:
    return {
        "ok": True,
        "items": RawMessageStore().list_conversations(conversation_type=conversation_type, status=status, limit=limit),
    }


@router.post("/conversations")
def upsert_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "item": RawMessageStore().upsert_conversation(payload)}


@router.get("/messages")
def messages(
    conversation_id: str = "",
    query: str = "",
    limit: int = 100,
    offset: int = 0,
    start_time: str = "",
    end_time: str = "",
    sender: str = "",
    content_type: str = "",
    conversation_type: str = Query("", pattern="^(|private|group|file_transfer|system|unknown)$"),
    keywords: str = "",
) -> dict[str, Any]:
    keyword_items = [item.strip() for item in str(keywords or "").split(",") if item.strip()]
    items = RawMessageStore().list_messages_advanced(
        conversation_id=conversation_id,
        query=query,
        limit=limit,
        offset=offset,
        start_time=start_time,
        end_time=end_time,
        sender=sender,
        content_type=content_type,
        conversation_type=conversation_type,
        keywords=keyword_items,
    )
    return {"ok": True, "items": items, "count": len(items)}


@router.post("/messages")
def upsert_messages(payload: dict[str, Any]) -> dict[str, Any]:
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    messages_payload = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not conversation:
        raise HTTPException(status_code=400, detail="conversation is required")
    result = RawMessageStore().upsert_messages(
        conversation,
        [item for item in messages_payload if isinstance(item, dict)],
        source_module=str(payload.get("source_module") or "api"),
        learning_enabled=payload.get("learning_enabled", True) is not False,
        create_batch=payload.get("create_batch", True) is not False,
        batch_reason=str(payload.get("batch_reason") or "api_import"),
    )
    if payload.get("auto_learn") and result.get("batch"):
        result["learning"] = RawMessageLearningService().process_batch(
            str(result["batch"].get("batch_id") or ""),
            use_llm=payload.get("use_llm", True) is not False,
        )
    return result


@router.get("/batches")
def batches(conversation_id: str = "", limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "items": RawMessageStore().list_batches(conversation_id=conversation_id, limit=limit)}


@router.post("/batches/{batch_id}/learn")
def learn_batch(batch_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return RawMessageLearningService().process_batch(batch_id, use_llm=(payload or {}).get("use_llm", True) is not False)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"raw message batch not found: {batch_id}") from exc


@router.post("/learning/process-pending")
def learn_pending(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return RawMessageLearningService().process_pending(
        limit=int(payload.get("limit", 10) or 10),
        use_llm=payload.get("use_llm", True) is not False,
    )
