"""Pure bridge from CheJin MessageEvent snapshots to OmniAuto Brain input.

This module is the single protocol boundary for CheJin C3 history.  It never
reads RawMessageStore and never queries a database; it validates and projects
the immutable snapshot supplied by the CheJin backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

try:
    from apps.wechat_ai_customer_service.workflows.customer_service_conversation_strategy import (
        update_conversation_interaction_state_on_capture,
        update_conversation_interaction_state_on_reply_sent,
        update_conversation_strategy_state,
    )
except ImportError:  # pragma: no cover - direct workflow script compatibility
    from customer_service_conversation_strategy import (
        update_conversation_interaction_state_on_capture,
        update_conversation_interaction_state_on_reply_sent,
        update_conversation_strategy_state,
    )


HISTORY_AUTHORITY = "chejin_message_events_v1"
SNAPSHOT_SCHEMA_VERSION = 1
MAX_HISTORY_ROUNDS = 12
MAX_HISTORY_CHARS = 3500
MAX_LEDGER_MESSAGES = 20
BASE_MESSAGE_FIELDS = (
    "message_event_id",
    "source_message_key",
    "sender_role",
    "message_type",
    "content",
    "item_state",
    "error_code",
    "occurred_at",
)
IMAGE_MESSAGE_FIELDS = (
    "vision_summary",
    "image_ocr_text",
    "classification",
    "entities",
    "normalized_vehicle_query",
    "server_validated_product_id",
)


class ChejinBrainContextError(ValueError):
    """Raised before any Provider call when the frozen snapshot is invalid."""


def _clean_text(value: Any, limit: int = 4000) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def canonical_prior_message(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChejinBrainContextError("prior_message_not_object")
    allowed = set(BASE_MESSAGE_FIELDS)
    if str(value.get("message_type") or "").strip().lower() == "image":
        allowed.update(IMAGE_MESSAGE_FIELDS)
    if set(value) - allowed:
        raise ChejinBrainContextError("prior_message_field_not_allowed")
    item = {
        "message_event_id": _clean_text(value.get("message_event_id"), 128),
        "source_message_key": _clean_text(value.get("source_message_key"), 255),
        "sender_role": _clean_text(value.get("sender_role"), 32).lower(),
        "message_type": _clean_text(value.get("message_type"), 32).lower(),
        "content": _clean_text(value.get("content"), 4000),
        "item_state": _clean_text(value.get("item_state"), 32).lower(),
        "error_code": _clean_text(value.get("error_code"), 64),
        "occurred_at": _clean_text(value.get("occurred_at"), 64),
    }
    if not item["message_event_id"] or not item["sender_role"] or not item["message_type"]:
        raise ChejinBrainContextError("prior_message_identity_missing")
    if item["sender_role"] not in {"customer", "self", "system", "unknown"}:
        raise ChejinBrainContextError("prior_message_role_invalid")
    if item["message_type"] == "image":
        ocr = value.get("image_ocr_text")
        if ocr is None:
            ocr = []
        if not isinstance(ocr, list):
            raise ChejinBrainContextError("prior_image_ocr_invalid")
        classification = value.get("classification") or {}
        entities = value.get("entities") or {}
        if not isinstance(classification, dict) or not isinstance(entities, dict):
            raise ChejinBrainContextError("prior_image_structure_invalid")
        item.update(
            {
                "vision_summary": _clean_text(value.get("vision_summary"), 2000),
                "image_ocr_text": [_clean_text(part, 500) for part in ocr[:20] if _clean_text(part, 500)],
                "classification": classification,
                "entities": entities,
                "normalized_vehicle_query": _clean_text(value.get("normalized_vehicle_query"), 500),
                "server_validated_product_id": _clean_text(value.get("server_validated_product_id"), 128),
            }
        )
    return item


def prior_messages_sha256(messages: list[dict[str, Any]]) -> str:
    canonical = [canonical_prior_message(item) for item in messages]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_time(item: dict[str, Any]) -> datetime:
    value = _clean_text(item.get("occurred_at"), 64)
    if not value:
        raise ChejinBrainContextError("prior_message_time_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChejinBrainContextError("prior_message_time_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _semantic_text(item: dict[str, Any]) -> str:
    if item.get("error_code") or item.get("item_state") == "failed":
        return ""
    if item.get("sender_role") not in {"customer", "self", "system"}:
        return ""
    if item.get("message_type") != "image":
        return _clean_text(item.get("content"), 4000)
    parts = [
        _clean_text(item.get("content"), 2000),
        _clean_text(item.get("vision_summary"), 2000),
        " ".join(
            _clean_text(part, 500)
            for part in (item.get("image_ocr_text") or [])
            if _clean_text(part, 500)
        ),
    ]
    return "；".join(dict.fromkeys(part for part in parts if part))


def _render_lines(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    semantic: list[dict[str, Any]] = []
    for item in items:
        text = _semantic_text(item)
        if text:
            semantic.append({**item, "semantic_text": text})
    # Keep the already-proven OmniAuto history semantics: consecutive
    # messages from the same role are one round, retain the most recent 12
    # rounds, then drop whole oldest rounds until the character budget fits.
    # Never slice the middle of a persisted message merely to satisfy the
    # budget; that would turn a retry of the same frozen snapshot into a
    # different customer fact.
    rounds: list[list[dict[str, Any]]] = []
    for item in semantic:
        if not rounds or rounds[-1][-1]["sender_role"] != item["sender_role"]:
            rounds.append([])
        rounds[-1].append(item)
    rounds = rounds[-MAX_HISTORY_ROUNDS:]
    labels = {"customer": "客户", "self": "客服", "system": "系统"}
    while rounds:
        lines = [
            f"{labels[item['sender_role']]}：{item['semantic_text']}"
            for group in rounds
            for item in group
        ]
        text = "\n".join(lines)
        if len(text) <= MAX_HISTORY_CHARS:
            selected_ids = {
                item["message_event_id"] for group in rounds for item in group
            }
            return (
                [item for item in semantic if item["message_event_id"] in selected_ids],
                text,
            )
        rounds.pop(0)
    return [], ""


def _current_message(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ChejinBrainContextError("current_message_not_object")
    event_id = _clean_text(item.get("id") or item.get("message_event_id"), 128)
    if not event_id:
        raise ChejinBrainContextError("current_message_id_missing")
    result = dict(item)
    result["id"] = event_id
    result["sender_role"] = _clean_text(
        item.get("sender_role") or item.get("sender_role_hint"), 32
    ).lower()
    result["message_type"] = _clean_text(item.get("message_type"), 32).lower()
    result["content"] = _clean_text(item.get("content"), 4000)
    if not result["message_type"] or result["sender_role"] not in {
        "customer",
        "self",
        "system",
    }:
        raise ChejinBrainContextError("current_message_identity_invalid")
    return result


def build_chejin_brain_context(
    *,
    brain_context_snapshot: dict[str, Any],
    current_batch: list[dict[str, Any]],
    expected_conversation_id: str,
) -> dict[str, Any]:
    if not isinstance(brain_context_snapshot, dict):
        raise ChejinBrainContextError("snapshot_missing")
    if int(brain_context_snapshot.get("schema_version") or 0) != SNAPSHOT_SCHEMA_VERSION:
        raise ChejinBrainContextError("snapshot_schema_invalid")
    if brain_context_snapshot.get("history_authority") != HISTORY_AUTHORITY:
        raise ChejinBrainContextError("snapshot_authority_invalid")
    conversation_id = _clean_text(brain_context_snapshot.get("conversation_id"), 128)
    if not conversation_id or conversation_id != _clean_text(expected_conversation_id, 128):
        raise ChejinBrainContextError("snapshot_conversation_mismatch")
    if brain_context_snapshot.get("history_window_complete") is not True:
        raise ChejinBrainContextError("snapshot_window_incomplete")

    raw_prior = brain_context_snapshot.get("prior_messages")
    raw_current_ids = brain_context_snapshot.get("current_batch_message_ids")
    if not isinstance(raw_prior, list) or not isinstance(raw_current_ids, list):
        raise ChejinBrainContextError("snapshot_message_lists_invalid")
    prior = [canonical_prior_message(item) for item in raw_prior]
    prior_ids = [item["message_event_id"] for item in prior]
    if len(prior_ids) != len(set(prior_ids)):
        raise ChejinBrainContextError("snapshot_prior_duplicate_id")
    times = [_event_time(item) for item in prior]
    if times != sorted(times):
        raise ChejinBrainContextError("snapshot_prior_order_invalid")
    if prior_messages_sha256(prior) != _clean_text(
        brain_context_snapshot.get("prior_messages_sha256"), 64
    ):
        raise ChejinBrainContextError("snapshot_digest_mismatch")

    current = [_current_message(item) for item in current_batch]
    current_ids = [item["id"] for item in current]
    expected_ids = [_clean_text(item, 128) for item in raw_current_ids]
    if not all(expected_ids) or len(expected_ids) != len(set(expected_ids)):
        raise ChejinBrainContextError("snapshot_current_ids_invalid")
    if current_ids != expected_ids:
        raise ChejinBrainContextError("snapshot_current_batch_mismatch")
    if set(prior_ids) & set(current_ids):
        raise ChejinBrainContextError("snapshot_current_history_overlap")

    semantic_count = int(
        brain_context_snapshot.get("semantic_history_count_before_batch") or 0
    )
    event_count = int(
        brain_context_snapshot.get("history_event_count_before_batch") or 0
    )
    if (
        semantic_count < 0
        or event_count < 0
        or len(prior) != min(50, event_count)
        or semantic_count > event_count
    ):
        raise ChejinBrainContextError("snapshot_counts_invalid")
    semantic_in_window = sum(1 for item in prior if _semantic_text(item))
    if semantic_count != semantic_in_window:
        raise ChejinBrainContextError("snapshot_semantic_count_invalid")
    selected_history, history_text = _render_lines(prior)
    if semantic_count > 0 and not history_text:
        raise ChejinBrainContextError("snapshot_semantic_history_missing")

    target_state: dict[str, Any] = {
        "conversation_id": conversation_id,
        "conversation_context": {},
    }
    replay_items: list[dict[str, Any]] = [*prior]
    replay_items.extend(
        {
            "message_event_id": item["id"],
            "sender_role": item["sender_role"],
            "message_type": item["message_type"],
            "content": item["content"],
            "occurred_at": _clean_text(
                item.get("occurred_at") or item.get("observed_at") or item.get("ingested_at"),
                64,
            ),
        }
        for item in current
    )
    for item in replay_items:
        role = item.get("sender_role")
        text = _semantic_text(item)
        tick = _clean_text(item.get("occurred_at"), 64) or "1970-01-01T00:00:00+00:00"
        if role == "customer" and text:
            update_conversation_strategy_state(target_state, text, now=tick)
            update_conversation_interaction_state_on_capture(
                target_state,
                text,
                message_ids=[_clean_text(item.get("message_event_id"), 128)],
                now=tick,
            )
        elif role == "self" and text:
            update_conversation_interaction_state_on_reply_sent(
                target_state,
                text,
                now=tick,
            )

    ledger = [
        {
            key: item.get(key)
            for key in BASE_MESSAGE_FIELDS
            if key in item
        }
        for item in prior[-MAX_LEDGER_MESSAGES:]
    ]
    # CheJin history is already authoritative MessageEvent data.  The bridge
    # must not derive a second semantic "current need" cache from Chinese
    # keywords; Brain receives the immutable raw history and interprets it.
    conversation_context = {"ledger_recent_messages": ledger}
    current_lines = []
    labels = {"customer": "客户", "self": "客服", "system": "系统"}
    for item in current:
        text = _semantic_text(item)
        if text and item.get("sender_role") in labels:
            current_lines.append(f"{labels[item['sender_role']]}：{text}")

    return {
        "schema_version": 1,
        "history_authority": HISTORY_AUTHORITY,
        "conversation_id": conversation_id,
        "history": selected_history,
        "history_text": history_text,
        "current_batch_text": "\n".join(current_lines),
        "conversation_summary": "",
        "conversation_context": conversation_context,
        "conversation_strategy_state": dict(
            target_state.get("conversation_strategy_state") or {}
        ),
        "conversation_interaction_state": dict(
            target_state.get("conversation_interaction_state") or {}
        ),
        "ledger_recent_messages": ledger,
        "prior_messages_sha256": brain_context_snapshot.get(
            "prior_messages_sha256"
        ),
    }
