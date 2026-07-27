"""Vision-owned projection of one scheduler capture.

The host scheduler supplies its existing connector, target, state snapshots,
and frozen callbacks.  This module owns every image-specific decision:
triggering, current-surface observation, customer/self direction resolution,
clipboard-pending projection, and context-only self-image dispatch.

No scheduler state is retained here and no customer-visible reply is authored.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from .occurrence import (
    confirmed_customer_image_placeholder,
    is_structural_visual_occurrence,
    resolve_pending_visual_occurrence,
)
from .trigger import customer_image_capture_trigger, image_preview_text


def legacy_observe_current_surface(
    *,
    connector: Any,
    target: Any,
    side_filter: str = "all",
    max_images: int = 8,
) -> dict[str, Any]:
    """Preserve the historical test/host seam inside the Vision owner."""

    # The bundled worker is a binding for the generic PR host.  A custom/test
    # connector without that host capability remains valid and absence-safe;
    # it must not accidentally start a real desktop worker.  Keep this check
    # inside the default host binding so an injected/custom Vision observer at
    # the same frozen seam can still work.
    if not callable(getattr(connector, "call_compat_sidecar", None)):
        return {
            "ok": False,
            "state": "vision_current_surface_host_unavailable",
            "reason": "vision_current_surface_host_unavailable",
            "assets": [],
            "messages": [],
        }

    from .integrations.wechat_current import observe_current_surface

    return observe_current_surface(
        connector,
        str(getattr(target, "name", "") or ""),
        exact=bool(getattr(target, "exact", True)),
        session_key=str(getattr(target, "session_key", "") or ""),
        conversation_type=str(getattr(target, "conversation_type", "") or ""),
        side_filter=side_filter,
        max_images=max_images,
    )


def prepare_scheduler_capture(
    *,
    connector: Any,
    target: Any,
    config: dict[str, Any],
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
    target_state: dict[str, Any],
    pending_signal: dict[str, Any] | None,
    pending_signal_kind: str,
    pending_signal_id: str,
    history_meta: dict[str, Any],
    self_context_runner: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return the existing scheduler fields after a vision-owned capture pass."""

    signal = pending_signal if isinstance(pending_signal, dict) else {}
    current_messages = [dict(item) for item in messages if isinstance(item, dict)]
    current_history = dict(history_meta or {})
    pending_text = str(
        signal.get("pending_signal_text")
        or signal.get("preview_content")
        or ""
    ).strip()
    explicit_image_pending = bool(
        str(pending_signal_kind or "").strip().lower()
        in {"image_capture", "media_capture"}
        or image_preview_text(pending_text)
    )
    visual_capture_trigger = customer_image_capture_trigger(
        payload=payload,
        pending_signal=signal,
        pending_signal_kind=pending_signal_kind,
        target_state=target_state,
    )
    visual_image_assets = {
        "ok": True,
        "state": "clipboard_vision_deferred",
        "reason": str(visual_capture_trigger.get("reason") or "no_recent_image_evidence"),
        "assets": [],
        "messages": [],
        "trigger": copy.deepcopy(visual_capture_trigger),
    }
    customer_image_assets: dict[str, Any] = {}
    self_image_context: dict[str, Any] = {
        "enabled": False,
        "applied": False,
        "context_only": True,
        "reason": "vision_self_image_context_unavailable",
    }
    pending_signal_consumed = False
    image_signal_already_processed = (
        str(visual_capture_trigger.get("reason") or "").strip()
        == "pending_image_signal_already_processed"
    )
    # Every real scheduler capture already reaches this optional plugin seam.
    # Observe the current surface once even when a later text preview has
    # replaced the sidebar's image preview.  This probe is structural only;
    # clipboard acquisition and LLM work still require a fresh bound
    # occurrence below.
    # Preserve the established explicit-media path even for a legacy monitor
    # event that has not yet acquired a canonical signal id.  Text-preview
    # recovery is stricter and requires the current id for adjacency binding.
    should_observe_current_surface = bool(
        signal and (pending_signal_id or explicit_image_pending)
    )
    if (
        should_observe_current_surface
        and not image_signal_already_processed
        and not any(is_structural_visual_occurrence(item) for item in current_messages)
    ):
        try:
            surface_observation = legacy_observe_current_surface(
                connector=connector,
                target=target,
                side_filter="all",
                max_images=8,
            )
        except Exception as exc:  # noqa: BLE001 - optional vision fails closed.
            surface_observation = {
                "ok": False,
                "state": "vision_current_surface_observation_exception",
                "reason": "vision_current_surface_observation_exception",
                "messages": [],
                "error": repr(exc),
            }
        observed_messages = [
            item
            for item in (surface_observation.get("messages") or [])
            if isinstance(item, dict) and is_structural_visual_occurrence(item)
        ]
        if observed_messages:
            existing_ids = {
                str(item.get("message_id") or item.get("id") or "").strip()
                for item in current_messages
            }
            current_messages.extend(
                item
                for item in observed_messages
                if str(item.get("message_id") or item.get("id") or "").strip()
                not in existing_ids
            )

    visual_resolution = (
        {"state": "completed", "direction": "", "occurrence": {}}
        if image_signal_already_processed
        else resolve_pending_visual_occurrence(
            current_messages,
            target_state=target_state,
            explicit_image_pending=bool(explicit_image_pending),
            pending_signal_id=pending_signal_id,
        )
    )
    current_messages = [
        item
        for item in current_messages
        if not is_structural_visual_occurrence(item)
    ]
    resolved_occurrence = (
        visual_resolution.get("occurrence")
        if isinstance(visual_resolution.get("occurrence"), dict)
        else {}
    )
    if resolved_occurrence:
        current_messages.append(resolved_occurrence)

    resolution_state = str(visual_resolution.get("state") or "")
    if resolution_state == "self_confirmed":
        if callable(self_context_runner):
            try:
                self_image_context = self_context_runner(
                    connector=connector,
                    target=target,
                    config=config,
                    messages=[resolved_occurrence],
                    target_state=target_state,
                    combined="\n".join(
                        str(item.get("content") or "")
                        for item in current_messages
                        if not is_structural_visual_occurrence(item)
                        and str(item.get("content") or "")
                    ).strip(),
                )
            except Exception as exc:  # noqa: BLE001 - context-only vision cannot block other sessions.
                self_image_context = {
                    "enabled": True,
                    "applied": False,
                    "context_only": True,
                    "reason": "self_image_context_exception",
                    "error": repr(exc),
                }
        # An explicit self-image monitor event has no customer text to pass
        # onward.  A self image recovered next to the current customer text is
        # context enrichment only and must not consume that text turn.
        pending_signal_consumed = bool(explicit_image_pending)
        current_history["visual_image_disposition"] = "self_context_only"
    elif resolution_state == "customer_confirmed":
        placeholder = confirmed_customer_image_placeholder(
            visual_resolution,
            target_name=str(getattr(target, "name", "") or ""),
            session_key=str(getattr(target, "session_key", "") or ""),
            pending_signal_id=pending_signal_id,
        )
        if placeholder:
            current_messages.append(placeholder)
        customer_image_assets = {
            "ok": True,
            "state": "clipboard_vision_pending",
            "assets": [],
            "messages": [],
            "pending_signal_id": pending_signal_id,
        }
        current_history["visual_image_disposition"] = "clipboard_vision_deferred"
    elif explicit_image_pending and image_signal_already_processed:
        pending_signal_consumed = True
        current_history["visual_image_disposition"] = "already_seen_pending_signal_consumed"
        current_history["visual_image_capture_trigger"] = copy.deepcopy(
            visual_capture_trigger
        )
    elif explicit_image_pending:
        current_history["visual_image_disposition"] = resolution_state or "no_candidate"

    return {
        "messages": current_messages,
        "history_meta": current_history,
        "self_image_context": self_image_context,
        "customer_image_assets": customer_image_assets,
        "visual_image_assets": visual_image_assets,
        "visual_capture_trigger": visual_capture_trigger,
        "pending_signal_consumed": pending_signal_consumed,
    }
