"""Vision-owned supplemental operation dispatcher.

Operation names are internal to the mounted Vision plugin.  Core modules reach
them only through the neutral optional-capability protocol; all positive image
semantics remain here.
"""

from __future__ import annotations

from typing import Any


def invoke_vision_operation(operation: str, context: dict[str, Any]) -> Any:
    data = context if isinstance(context, dict) else {}
    clean = str(operation or "").strip()

    if clean == "prepare_scheduler_capture":
        from .scheduler_capture import prepare_scheduler_capture

        return prepare_scheduler_capture(**data)

    if clean == "build_brain_safe_image_proxy_messages":
        from .projection.message import build_brain_safe_image_proxy_messages

        return build_brain_safe_image_proxy_messages(
            data.get("sources") if isinstance(data.get("sources"), list) else [],
            target_name=str(data.get("target_name") or ""),
            session_key=str(data.get("session_key") or ""),
            content=str(data.get("content") or ""),
        )

    if clean in {
        "augment_text_with_visual_query",
        "compact_customer_image_brain_bridge",
        "resolve_visual_brain_turn_text",
    }:
        from .projection import brain

        if clean == "augment_text_with_visual_query":
            return brain.augment_text_with_visual_query(
                str(data.get("combined") or ""),
                data.get("visual_bridge_input")
                if isinstance(data.get("visual_bridge_input"), dict)
                else None,
            )
        if clean == "compact_customer_image_brain_bridge":
            return brain.compact_customer_image_brain_bridge(
                data.get("value") if isinstance(data.get("value"), dict) else None
            )
        return brain.resolve_visual_brain_turn_text(
            str(data.get("combined") or ""),
            data.get("visual_bridge_input")
            if isinstance(data.get("visual_bridge_input"), dict)
            else None,
        )

    from . import occurrence

    if clean == "visual_image_side":
        return occurrence.visual_image_side(
            data.get("message") if isinstance(data.get("message"), dict) else {}
        )
    if clean == "customer_visual_image_sources":
        return occurrence.customer_visual_image_sources(
            data.get("sources") if isinstance(data.get("sources"), list) else []
        )
    if clean == "self_visual_image_sources":
        return occurrence.self_visual_image_sources(
            data.get("sources") if isinstance(data.get("sources"), list) else []
        )
    if clean == "customer_image_proxy_messages":
        return occurrence.customer_image_proxy_messages(
            data.get("messages") if isinstance(data.get("messages"), list) else []
        )
    if clean == "visual_bounds_key":
        return occurrence.visual_bounds_key(data.get("value"))
    if clean == "visual_image_identity_keys":
        return occurrence.visual_image_identity_keys(
            data.get("message") if isinstance(data.get("message"), dict) else {}
        )
    if clean == "target_state_seen_visual_identity_keys":
        return occurrence.target_state_seen_visual_identity_keys(
            data.get("target_state")
            if isinstance(data.get("target_state"), dict)
            else {}
        )
    if clean == "filter_fresh_customer_visual_sources":
        return occurrence.filter_fresh_customer_visual_sources(
            data.get("sources") if isinstance(data.get("sources"), list) else [],
            target_state=(
                data.get("target_state")
                if isinstance(data.get("target_state"), dict)
                else {}
            ),
        )
    if clean == "image_message_refs":
        return occurrence.image_message_refs(
            data.get("items") if isinstance(data.get("items"), list) else []
        )
    if clean == "planner_customer_image_enrichment":
        return occurrence.planner_customer_image_enrichment(
            data.get("capture") if isinstance(data.get("capture"), dict) else {},
            data.get("result") if isinstance(data.get("result"), dict) else {},
        )
    if clean == "is_structural_visual_occurrence":
        return occurrence.is_structural_visual_occurrence(
            data.get("message") if isinstance(data.get("message"), dict) else {}
        )
    if clean == "resolve_pending_visual_occurrence":
        return occurrence.resolve_pending_visual_occurrence(
            data.get("messages") if isinstance(data.get("messages"), list) else [],
            target_state=(
                data.get("target_state")
                if isinstance(data.get("target_state"), dict)
                else {}
            ),
            explicit_image_pending=bool(data.get("explicit_image_pending")),
            pending_signal_id=str(data.get("pending_signal_id") or ""),
        )
    if clean == "confirmed_customer_image_placeholder":
        return occurrence.confirmed_customer_image_placeholder(
            data.get("resolution") if isinstance(data.get("resolution"), dict) else {},
            target_name=str(data.get("target_name") or ""),
            session_key=str(data.get("session_key") or ""),
            pending_signal_id=str(data.get("pending_signal_id") or ""),
        )
    if clean == "image_capture_unavailable_message":
        return occurrence.image_capture_unavailable_message(
            target_name=str(data.get("target_name") or ""),
            session_key=str(data.get("session_key") or ""),
            pending_signal_id=str(data.get("pending_signal_id") or ""),
            pending_signal=(
                data.get("pending_signal")
                if isinstance(data.get("pending_signal"), dict)
                else None
            ),
            reason=str(data.get("reason") or ""),
        )
    return NotImplemented
