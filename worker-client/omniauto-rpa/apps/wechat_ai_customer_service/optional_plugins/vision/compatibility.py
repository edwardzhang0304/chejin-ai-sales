from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.optional_plugins.registry import resolve_optional_capability


def legacy_image_preview_text(value: Any) -> bool:
    from .trigger import image_preview_text

    return image_preview_text(value)


def legacy_customer_image_capture_trigger(
    *,
    payload: dict[str, Any] | None,
    pending_signal: dict[str, Any] | None = None,
    pending_signal_kind: str = "",
    target_state: dict[str, Any] | None = None,
    recent_message_limit: int = 6,
) -> dict[str, Any]:
    plugin = resolve_optional_capability("vision")
    if plugin is None:
        return {
            "should_run": False,
            "reason": "vision_capability_unavailable",
            "pending_signal_kind": str(
                pending_signal_kind
                or (pending_signal or {}).get("pending_signal_kind")
                or ""
            ).strip().lower(),
            "pending_signal_id": str((pending_signal or {}).get("pending_signal_id") or ""),
            "evidence_count": 0,
        }
    return plugin.should_run(
        {
            "payload": payload,
            "pending_signal": pending_signal,
            "pending_signal_kind": pending_signal_kind,
            "target_state": target_state,
            "recent_message_limit": recent_message_limit,
        }
    )


def legacy_maybe_route_customer_image_turn(
    *,
    connector: Any,
    target: Any,
    config: dict[str, Any],
    payload: dict[str, Any],
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    combined: str,
) -> dict[str, Any]:
    plugin = resolve_optional_capability("vision")
    if plugin is None:
        return {
            "enabled": False,
            "applied": False,
            "adoptable": False,
            "reason": "vision_capability_unavailable",
        }
    return plugin.run(
        {
            "connector": connector,
            "target": target,
            "config": config,
            "payload": payload,
            "target_state": target_state,
            "batch": batch,
            "combined": combined,
        }
    )


def legacy_observe_current_surface(
    *,
    connector: Any,
    target: Any,
    side_filter: str = "all",
    max_images: int = 8,
) -> dict[str, Any]:
    """Absence-safe bridge to the vision-owned WeChat surface observer."""

    plugin = resolve_optional_capability("vision")
    observer = getattr(plugin, "observe_current_surface", None) if plugin is not None else None
    if not callable(observer):
        return {
            "ok": False,
            "state": "vision_current_surface_observer_unavailable",
            "reason": "vision_current_surface_observer_unavailable",
            "assets": [],
            "messages": [],
        }
    return observer(
        {
            "connector": connector,
            "target": target,
            "side_filter": side_filter,
            "max_images": max_images,
        }
    )


def legacy_prepare_scheduler_capture(**kwargs: Any) -> dict[str, Any]:
    """Compatibility bridge to the vision-owned scheduler projection."""

    from .scheduler_capture import prepare_scheduler_capture

    return prepare_scheduler_capture(**kwargs)


def legacy_maybe_capture_self_image_context(
    *,
    connector: Any,
    target: Any,
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    target_state: dict[str, Any],
    combined: str = "",
) -> dict[str, Any]:
    """Compatibility seam for optional, context-only self-image vision."""

    plugin = resolve_optional_capability("vision")
    capture_self_context = getattr(plugin, "capture_self_context", None) if plugin is not None else None
    if not callable(capture_self_context):
        return {
            "enabled": False,
            "applied": False,
            "context_only": True,
            "reason": "vision_self_image_context_unavailable",
        }
    return capture_self_context(
        {
            "connector": connector,
            "target": target,
            "config": config,
            "messages": messages,
            "target_state": target_state,
            "combined": combined,
        }
    )


def legacy_build_brain_safe_image_proxy_messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    from .projection.message import build_brain_safe_image_proxy_messages

    return build_brain_safe_image_proxy_messages(*args, **kwargs)


def legacy_augment_text_with_visual_query(
    combined: str,
    visual_bridge_input: dict[str, Any] | None,
) -> str:
    try:
        from .projection.brain import augment_text_with_visual_query
    except ImportError:
        return str(combined or "")
    return augment_text_with_visual_query(combined, visual_bridge_input)


def legacy_compact_customer_image_brain_bridge(
    value: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        from .projection.brain import compact_customer_image_brain_bridge
    except ImportError:
        return {}
    return compact_customer_image_brain_bridge(value)


def legacy_resolve_visual_brain_turn_text(
    combined: str,
    visual_bridge_input: dict[str, Any] | None,
) -> str:
    try:
        from .projection.brain import resolve_visual_brain_turn_text
    except ImportError:
        return str(combined or "")
    return resolve_visual_brain_turn_text(combined, visual_bridge_input)
