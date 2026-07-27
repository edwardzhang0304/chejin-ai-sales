from __future__ import annotations

import hashlib
import json
from typing import Any


def visual_image_side(message: dict[str, Any]) -> str:
    side = str(message.get("visual_side") or "").strip().lower()
    if side in {"customer", "self"}:
        return side
    sender = str(message.get("sender") or message.get("sender_role") or "").strip().lower()
    if sender in {"customer", "self"}:
        return sender
    return ""


def customer_visual_image_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in sources
        if isinstance(item, dict) and visual_image_side(item) == "customer"
    ]


def self_visual_image_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in sources
        if isinstance(item, dict) and visual_image_side(item) == "self"
    ]


def customer_image_proxy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages or []:
        if not isinstance(item, dict) or not item.get("is_customer_image_proxy"):
            continue
        if str(item.get("visual_turn_kind") or "").strip() != "customer_image":
            continue
        if visual_image_side(item) == "self":
            continue
        # Clipboard image turns are represented by a text-only pending/proxy
        # envelope.  A historical local path must never be required here.
        flags = {str(flag or "").strip() for flag in (item.get("quality_flags") or [])}
        if not (item.get("image_capture_pending") or "clipboard_current_transaction" in flags):
            continue
        result.append(item)
    return result


def visual_bounds_key(value: Any) -> str:
    try:
        bounds = [int(float(item)) for item in list(value or [])[:4]]
    except (TypeError, ValueError):
        return ""
    if len(bounds) != 4:
        return ""
    return ",".join(str(item) for item in bounds)


def visual_image_identity_keys(message: dict[str, Any]) -> set[str]:
    if not isinstance(message, dict):
        return set()
    keys: set[str] = set()

    def add(prefix: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            keys.add(f"{prefix}:{text}")

    add("visual_occurrence", message.get("visual_occurrence_id"))
    add("pending_signal", message.get("pending_signal_id"))
    add("visual_msg", message.get("message_id") or message.get("id") or message.get("identity"))
    add("visual_msg", message.get("canonical_visual_id") or message.get("canonical_input_id"))
    source_message_id = message.get("source_message_id")
    add("source_msg", source_message_id)
    # A resolved occurrence retains the original structural id as
    # source_message_id.  Treat that value as the same visual identity when a
    # later surface probe sees the original structural envelope again.
    add("visual_msg", source_message_id)
    side = visual_image_side(message) or str(message.get("visual_side") or "").strip().lower() or "unknown"
    return keys


def target_state_seen_visual_identity_keys(target_state: dict[str, Any]) -> set[str]:
    state = target_state if isinstance(target_state, dict) else {}
    seen: set[str] = set()
    for item in state.get("processed_message_ids") or []:
        value = str(item or "").strip()
        if value:
            seen.add(f"visual_msg:{value}")
            seen.add(f"source_msg:{value}")
    context = state.get("conversation_context") if isinstance(state.get("conversation_context"), dict) else {}
    recent_sources: list[Any] = []
    for key in ("ledger_recent_messages", "recent_messages"):
        value = context.get(key) if isinstance(context, dict) else None
        if isinstance(value, list):
            recent_sources.extend(value)
    for key in ("ledger_recent_messages", "recent_messages", "visual_recent_messages"):
        value = state.get(key)
        if isinstance(value, list):
            recent_sources.extend(value)
    for item in recent_sources:
        if isinstance(item, dict):
            seen.update(visual_image_identity_keys(item))
    return seen


def filter_fresh_customer_visual_sources(
    sources: list[dict[str, Any]],
    *,
    target_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen = target_state_seen_visual_identity_keys(target_state)
    fresh: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        keys = visual_image_identity_keys(source)
        pending_signal_key = str(source.get("pending_signal_id") or "").strip()
        pending_signal_seen = bool(
            pending_signal_key and f"pending_signal:{pending_signal_key}" in seen
        )
        pending_signal_is_source_identity = bool(
            pending_signal_key and not source.get("_pending_signal_id_attached_by_scheduler")
        )
        occurrence_value = str(source.get("visual_occurrence_id") or "").strip()
        occurrence_seen = bool(
            occurrence_value and f"visual_occurrence:{occurrence_value}" in seen
        )
        if pending_signal_is_source_identity and not pending_signal_seen:
            already_seen = False
        elif occurrence_seen:
            already_seen = True
        elif pending_signal_key:
            already_seen = pending_signal_seen
        else:
            already_seen = bool(keys and seen.intersection(keys))
        if already_seen:
            filtered.append(
                {
                    "message_id": str(source.get("message_id") or source.get("id") or ""),
                    "visual_occurrence_id": occurrence_value,
                    "pending_signal_id": pending_signal_key,
                    "reason": "visual_image_already_seen",
                }
            )
            continue
        fresh.append(source)
    return fresh, {
        "seen_key_count": len(seen),
        "input_count": len(sources),
        "fresh_count": len(fresh),
        "filtered_count": len(filtered),
        "filtered": filtered[:20],
    }


def image_message_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ref = {
            key: str(item.get(key) or "").strip()
            for key in (
                "id",
                "message_id",
                "source_message_id",
                "visual_occurrence_id",
            )
            if str(item.get(key) or "").strip()
        }
        identity = "|".join(sorted(ref.values()))
        if not ref or identity in seen:
            continue
        seen.add(identity)
        refs.append(ref)
    return refs[:20]


def planner_customer_image_enrichment(
    capture: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    event = result.get("event") if isinstance(result.get("event"), dict) else {}
    image_turn = (
        event.get("customer_image_turn")
        if isinstance(event.get("customer_image_turn"), dict)
        else {}
    )
    understanding = (
        image_turn.get("customer_image_understanding")
        if isinstance(image_turn.get("customer_image_understanding"), dict)
        else {}
    )
    if not understanding:
        return {}
    assets_payload = (
        capture.get("customer_image_assets")
        if isinstance(capture.get("customer_image_assets"), dict)
        else {}
    )
    refs = image_message_refs(
        [
            item
            for item in [
                *(assets_payload.get("messages") or []),
                *(assets_payload.get("assets") or []),
                *(understanding.get("source_messages") or []),
            ]
            if isinstance(item, dict)
        ]
    )
    if not refs:
        refs = image_message_refs(
            [
                item
                for item in (capture.get("messages") or [])
                if isinstance(item, dict) and visual_image_side(item) == "customer"
            ]
        )
    return {
        "modality": "image",
        "message_refs": refs,
        "image_understanding": understanding,
        "reason": str(
            image_turn.get("source_reason")
            or understanding.get("reason")
            or "customer_image_planner"
        ),
    }


def _stable_id(prefix: str, *parts: Any) -> str:
    """Match the frozen scheduler identifier format without importing it."""

    seed = json.dumps([str(item) for item in parts], ensure_ascii=False, sort_keys=True)
    return f"{prefix}_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def is_structural_visual_occurrence(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    message_type = str(message.get("type") or message.get("message_type") or "").strip().lower()
    if message_type not in {"image", "picture", "photo"}:
        return False
    if str(message.get("source_adapter") or "").strip() != "win32_ocr_structural_image_observer":
        return False
    return visual_image_side(message) in {"customer", "self"}


def resolve_pending_visual_occurrence(
    messages: list[dict[str, Any]],
    *,
    target_state: dict[str, Any],
    explicit_image_pending: bool,
    pending_signal_id: str,
) -> dict[str, Any]:
    """Bind the current media signal to one direction-confirmed occurrence."""

    processed_signal_ids = {
        str(item or "").strip()
        for item in (target_state.get("processed_visual_pending_signal_ids") or [])
        if str(item or "").strip()
    }
    clean_signal_id = str(pending_signal_id or "").strip()
    if clean_signal_id and clean_signal_id in processed_signal_ids:
        return {"state": "completed", "direction": "", "occurrence": {}}
    candidates = [dict(item) for item in messages if is_structural_visual_occurrence(item)]
    if not explicit_image_pending:
        pending_message_ids: set[str] = set()
        for item in messages:
            if not isinstance(item, dict) or is_structural_visual_occurrence(item):
                continue
            if str(item.get("pending_signal_id") or "").strip() != clean_signal_id:
                continue
            for key in (
                "message_id",
                "id",
                "legacy_message_id",
                "original_message_id",
                "canonical_input_id",
                "canonical_visual_id",
            ):
                value = str(item.get(key) or "").strip()
                if value:
                    pending_message_ids.add(value)
        if not pending_message_ids:
            return {"state": "sidebar_signal_only", "direction": "", "occurrence": {}}
        seen = target_state_seen_visual_identity_keys(target_state)
        candidates = [
            item
            for item in candidates
            if str(item.get("_vision_following_text_id") or "").strip()
            in pending_message_ids
            and not seen.intersection(visual_image_identity_keys(item))
        ]
        if not candidates:
            return {"state": "sidebar_signal_only", "direction": "", "occurrence": {}}
    if not candidates:
        return {"state": "no_candidate", "direction": "", "occurrence": {}}
    occurrence = dict(candidates[-1])
    direction = visual_image_side(occurrence)
    if direction not in {"customer", "self"}:
        return {"state": "ambiguous", "direction": "", "occurrence": {}}
    if clean_signal_id:
        structural_message_id = str(occurrence.get("message_id") or occurrence.get("id") or "").strip()
        event_message_id = "visual_pending:" + _stable_id(
            "visual-pending-occurrence",
            structural_message_id,
            clean_signal_id,
            direction,
        )
        if structural_message_id:
            occurrence["source_message_id"] = structural_message_id
        occurrence["id"] = event_message_id
        occurrence["message_id"] = event_message_id
        occurrence["pending_signal_id"] = clean_signal_id
    occurrence["sender"] = direction
    occurrence["sender_role"] = direction
    occurrence["visual_side"] = direction
    for key in list(occurrence):
        if str(key).startswith("_vision_"):
            occurrence.pop(key, None)
    return {
        "state": f"{direction}_confirmed",
        "direction": direction,
        "occurrence": occurrence,
    }


def confirmed_customer_image_placeholder(
    resolution: dict[str, Any],
    *,
    target_name: str,
    session_key: str,
    pending_signal_id: str,
) -> dict[str, Any]:
    """Project a direction-confirmed occurrence into the frozen pending envelope."""

    if str(resolution.get("state") or "") != "customer_confirmed":
        return {}
    occurrence = resolution.get("occurrence") if isinstance(resolution.get("occurrence"), dict) else {}
    source_message_id = str(occurrence.get("message_id") or occurrence.get("id") or "").strip()
    placeholder_id = _stable_id(
        "clipboard-image-pending",
        target_name,
        session_key,
        pending_signal_id,
        source_message_id,
    )
    return {
        "id": f"clipboard_image_pending:{placeholder_id}",
        "message_id": f"clipboard_image_pending:{placeholder_id}",
        "type": "text",
        "sender": "customer",
        "sender_role": "customer",
        "content": "客户发送了一张图片，图片内容暂未取得。",
        "source_message_id": source_message_id,
        "visual_side": "customer",
        "visual_turn_kind": "customer_image",
        "is_customer_image_proxy": True,
        "pending_signal_id": str(pending_signal_id or ""),
        "image_capture_pending": True,
        "quality_flags": ["synthetic_visual_turn", "clipboard_current_transaction_required"],
    }


def image_capture_unavailable_message(
    *,
    target_name: str,
    session_key: str,
    pending_signal_id: str,
    pending_signal: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    """Project a failed image acquisition into a non-visible evidence record."""

    signal = pending_signal if isinstance(pending_signal, dict) else {}
    occurred_at = str(
        signal.get("pending_since")
        or signal.get("last_detected_at")
        or signal.get("last_message_time")
        or ""
    )
    stable = _stable_id(
        "image-capture-unavailable",
        target_name,
        session_key,
        pending_signal_id,
        occurred_at,
        reason,
    )
    return {
        "id": f"image_capture_unavailable:{stable}",
        "message_id": f"image_capture_unavailable:{stable}",
        "type": "text",
        "sender": "unknown",
        "sender_role": "unknown",
        "content": "客户发送了一张图片，但图片内容暂未取得。",
        "time": occurred_at,
        "image_capture_unavailable": True,
        "image_capture_failure_reason": str(reason or "customer_image_save_failed"),
        "pending_signal_id": str(pending_signal_id or ""),
        "pending_signal_kind": str(signal.get("pending_signal_kind") or "image_capture"),
        "quality_flags": ["image_capture_unavailable"],
    }
