"""Host-neutral current-image acquisition transaction.

This module owns image direction, bubble/menu selection and clipboard
freshness. Hosts provide generic frame/action/clipboard ports only.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any

from ..clipboard_payload import ephemeral_image_from_memory
from ..ports import VisionHostPorts
from .wechat import detect_visual_image_bubbles, find_copy_menu_item


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "state": "vision_port_transaction_failed",
        "reason": str(reason or "vision_port_transaction_failed"),
        "assets": [],
        "messages": [],
        **extra,
    }


def _bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        raw = (value.get("left"), value.get("top"), value.get("right"), value.get("bottom"))
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = value[:4]
    else:
        return None
    try:
        left, top, right, bottom = (float(item) for item in raw)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _matching_bubble(bubbles: list[dict[str, Any]], requested: Any) -> dict[str, Any]:
    target = _bounds(requested)
    if target is None:
        return {}
    target_left, target_top, target_right, target_bottom = target
    target_cx = (target_left + target_right) / 2.0
    target_cy = (target_top + target_bottom) / 2.0
    target_height = max(1.0, target_bottom - target_top)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for bubble in bubbles:
        current = _bounds(bubble.get("bounds"))
        if current is None:
            continue
        left, top, right, bottom = current
        intersection = max(0.0, min(target_right, right) - max(target_left, left)) * max(
            0.0, min(target_bottom, bottom) - max(target_top, top)
        )
        union = (target_right - target_left) * (target_bottom - target_top) + (right - left) * (bottom - top) - intersection
        iou = intersection / union if union > 0 else 0.0
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        distance = abs(cx - target_cx) + abs(cy - target_cy) * 1.5
        score = iou * 1000.0 - distance
        if iou >= 0.18 or (abs(cy - target_cy) <= max(24.0, target_height * 0.65) and abs(cx - target_cx) <= 120.0):
            ranked.append((score, dict(bubble)))
    if not ranked:
        return {}
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _dismiss_menu_safely(port: Any) -> None:
    dismiss = getattr(port, "dismiss_menu_safely", None)
    if callable(dismiss):
        try:
            dismiss()
        except Exception:
            pass


def _cancelled(data: dict[str, Any]) -> bool:
    callback = data.get("cancel_check")
    if not callable(callback):
        return False
    try:
        return bool(callback())
    except Exception:
        return True


def acquire_current_image_via_ports(
    ports: VisionHostPorts,
    request: dict[str, Any] | None,
) -> dict[str, Any]:
    """Acquire exactly one current bitmap while holding the host RPA lease."""

    data = dict(request or {})
    action_phase = "not_attempted"

    def fail(reason: str, **extra: Any) -> dict[str, Any]:
        return _failure(
            reason,
            action_phase=action_phase,
            transaction={"action_phase": action_phase},
            **extra,
        )

    required = (
        ports.conversation_target,
        ports.window_frame,
        ports.ui_action,
        ports.clipboard,
    )
    if any(item is None for item in required):
        return fail("vision_host_ports_incomplete")
    sender_role = str(data.get("sender_role") or "").strip().lower()
    if sender_role not in {"customer", "self"}:
        return fail("image_sender_role_untrusted")
    if _bounds(data.get("bubble_rect")) is None:
        return fail("image_bubble_rect_missing")
    side_filter = "all"
    if str(data.get("side_filter") or "all").strip().lower() not in {"customer", "self", "all"}:
        return fail("image_clipboard_side_filter_invalid")
    lease = (
        ports.rpa_lease.lease("vision_current_image", timeout_seconds=float(data.get("lock_timeout_seconds") or 45.0))
        if ports.rpa_lease is not None
        else nullcontext({"acquired": True, "source": "vision_port_noop_lease"})
    )
    surface = None
    menu_surface = None
    menu_opened = False
    try:
        with lease:
            if _cancelled(data):
                return fail("vision_cancelled")
            frame = ports.window_frame.capture_frame({**data, "phase": "image_candidate"})
            if not isinstance(frame, dict) or frame.get("ok") is not True:
                return fail("vision_window_frame_unavailable")
            surface = frame.get("image")
            image_size = getattr(surface, "size", None) or tuple(frame.get("image_size") or ())
            if surface is None or len(image_size) != 2:
                return fail("vision_window_frame_invalid")
            target_proof = ports.conversation_target.confirm_target(
                {**data, "candidate_frame": frame}
            )
            if not isinstance(target_proof, dict) or target_proof.get("ok") is not True:
                return fail("vision_target_confirmation_failed")
            bubbles = detect_visual_image_bubbles(
                surface,
                messages=[item for item in (frame.get("messages") or []) if isinstance(item, dict)],
                max_images=max(1, int(data.get("max_images") or 8)),
                side_filter=side_filter,
                time_markers=[item for item in (frame.get("time_markers") or []) if isinstance(item, dict)],
            )
            if not bubbles:
                return fail("image_bubble_not_found")
            bubble = _matching_bubble(bubbles, data.get("bubble_rect"))
            if not bubble:
                return fail("image_bubble_slot_not_reconfirmed")
            if _cancelled(data):
                return fail("vision_cancelled")
            # Message ownership is decided by C2's same-row avatar contract.
            # Vision geometry is used only to click the already-authorized slot.
            direction = sender_role
            anchor = bubble.get("anchor") if isinstance(bubble.get("anchor"), dict) else {}
            sequence_before = ports.clipboard.sequence_number()
            ports.ui_action.right_click(int(anchor.get("x") or 0), int(anchor.get("y") or 0))
            menu_opened = True
            if _cancelled(data):
                return fail("vision_cancelled")
            menu_frame = ports.window_frame.capture_frame({**data, "phase": "image_context_menu"})
            if not isinstance(menu_frame, dict) or menu_frame.get("ok") is not True:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail("image_context_menu_unavailable")
            menu_surface = menu_frame.get("image")
            menu_size = getattr(menu_surface, "size", None) or tuple(menu_frame.get("image_size") or image_size)
            copy_item = find_copy_menu_item(
                [item for item in (menu_frame.get("ocr_items") or []) if isinstance(item, dict)],
                tuple(menu_size),
            )
            if not copy_item:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail("image_context_menu_copy_item_missing")
            if _cancelled(data):
                return fail("vision_cancelled")
            journal_update = data.get("action_journal_update")
            if callable(journal_update):
                journal_update(
                    action_phase="trigger_attempted",
                    business_state=None,
                    business_result_confirmed=False,
                )
            action_phase = "trigger_attempted"
            ports.ui_action.click(int(copy_item.get("x") or 0), int(copy_item.get("y") or 0))
            menu_opened = False
            sequence_after = ports.clipboard.sequence_number()
            if sequence_before is None or sequence_after is None or int(sequence_after) == int(sequence_before):
                return fail("clipboard_sequence_unchanged_after_copy")
            payload = ephemeral_image_from_memory(
                ports.clipboard.read_current_bitmap(),
                mime_type=str(data.get("mime_type") or "image/png"),
            )
            if payload is None:
                return fail("clipboard_current_content_not_bitmap")
            if _cancelled(data):
                payload.release()
                return fail("vision_cancelled")
            image_sha256 = hashlib.sha256(bytes(payload.image_bytes)).hexdigest()
            if callable(journal_update):
                journal_update(
                    action_phase="confirmed",
                    business_state="clipboard_confirmed",
                    business_result_confirmed=False,
                )
            return {
                "ok": True,
                "state": "image_clipboard_copied",
                "action_phase": "confirmed",
                "direction": direction,
                "occurrence": {
                    "sender": direction,
                    "sender_role": direction,
                    "visual_side": direction,
                    "pending_signal_id": str(data.get("pending_signal_id") or ""),
                    "message_id": str(data.get("message_id") or data.get("pending_signal_id") or "memory-current-image"),
                },
                "target_proof": dict(target_proof),
                "transaction": {
                    "status": "clipboard_read",
                    "action_phase": "confirmed",
                    "right_click_ok": True,
                    "menu_copy_confirmed": True,
                    "clipboard_sequence_changed": True,
                    "clipboard_content_read": True,
                    "clipboard_image_valid": True,
                    "visual_side": direction,
                    "image_sha256": image_sha256,
                    "image_width": int(payload.width),
                    "image_height": int(payload.height),
                },
                "_ephemeral_clipboard_image": payload,
            }
    except TimeoutError as exc:
        return fail("image_clipboard_transaction_lock_timeout", error_type=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - host failures are normalized
        return fail("vision_port_transaction_exception", error_type=type(exc).__name__)
    finally:
        if menu_opened:
            _dismiss_menu_safely(ports.ui_action)
        for transient_image in (surface, menu_surface):
            close = getattr(transient_image, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
