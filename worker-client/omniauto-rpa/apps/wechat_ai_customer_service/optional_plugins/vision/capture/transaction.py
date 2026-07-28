"""Host-neutral current-image acquisition transaction.

This module owns image direction, bubble/menu selection and clipboard
freshness. Hosts provide generic frame/action/clipboard ports only.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import nullcontext
from typing import Any

from ..clipboard_payload import ephemeral_image_from_memory
from ..ports import VisionHostPorts
from .wechat import (
    attach_image_physical_anchors,
    detect_visual_image_bubbles,
    find_copy_menu_item,
    image_visual_fingerprint_distance,
)


CLIPBOARD_WAIT_TIMEOUT_SECONDS = 2.5
CLIPBOARD_POLL_INTERVAL_SECONDS = 0.08
IMAGE_FINGERPRINT_MAX_DISTANCE = 6


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


def _matching_bubble(
    screenshot: Any,
    bubbles: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    expected_anchor: Any,
    expected_role: str,
) -> dict[str, Any]:
    anchor = expected_anchor if isinstance(expected_anchor, dict) else {}
    expected_fingerprint = str(
        anchor.get("bubble_visual_fingerprint") or ""
    ).strip().lower()
    expected_role = str(expected_role or "").strip().lower()
    if not expected_fingerprint or expected_role not in {"customer", "self"}:
        return {}
    try:
        expected_occurrence = int(anchor.get("occurrence_index") or 0)
        expected_occurrence_count = int(anchor.get("occurrence_count") or 0)
    except (TypeError, ValueError):
        return {}
    if expected_occurrence_count <= 0:
        return {}
    expected_preceding = str(
        anchor.get("preceding_stable_message") or ""
    ).strip()
    expected_following = str(
        anchor.get("following_stable_message") or ""
    ).strip()
    current_candidates = attach_image_physical_anchors(
        screenshot,
        bubbles,
        messages,
    )
    fingerprint_matches: list[dict[str, Any]] = []
    for bubble in current_candidates:
        current_anchor = (
            bubble.get("image_physical_anchor")
            if isinstance(bubble.get("image_physical_anchor"), dict)
            else {}
        )
        current_role = str(
            current_anchor.get("sender_role") or bubble.get("side") or ""
        ).strip().lower()
        if current_role != expected_role:
            continue
        fingerprint_distance = image_visual_fingerprint_distance(
            expected_fingerprint,
            current_anchor.get("bubble_visual_fingerprint"),
        )
        if (
            fingerprint_distance is None
            or fingerprint_distance > IMAGE_FINGERPRINT_MAX_DISTANCE
        ):
            continue
        fingerprint_matches.append(
            {
                **bubble,
                "identity_match_evidence": {
                    "sender_role": current_role,
                    "fingerprint_distance": fingerprint_distance,
                    "preceding_stable_message": str(
                        current_anchor.get("preceding_stable_message") or ""
                    ),
                    "following_stable_message": str(
                        current_anchor.get("following_stable_message") or ""
                    ),
                    "occurrence_index": int(
                        current_anchor.get("occurrence_index") or 0
                    ),
                    "occurrence_count": int(
                        current_anchor.get("occurrence_count") or 0
                    ),
                },
            }
        )
    contextual_matches = []
    for bubble in fingerprint_matches:
        evidence = (
            bubble.get("identity_match_evidence")
            if isinstance(bubble.get("identity_match_evidence"), dict)
            else {}
        )
        if int(evidence.get("occurrence_index") or 0) != expected_occurrence:
            continue
        if (
            int(evidence.get("occurrence_count") or 0)
            != expected_occurrence_count
        ):
            continue
        current_preceding = str(
            evidence.get("preceding_stable_message") or ""
        ).strip()
        current_following = str(
            evidence.get("following_stable_message") or ""
        ).strip()
        expected_neighbors = [
            value for value in (expected_preceding, expected_following) if value
        ]
        matching_neighbor_count = sum(
            (
                bool(expected_preceding)
                and current_preceding == expected_preceding,
                bool(expected_following)
                and current_following == expected_following,
            )
        )
        if expected_neighbors and matching_neighbor_count == 0:
            continue
        contextual_matches.append(bubble)
    if len(contextual_matches) != 1:
        return {}
    return contextual_matches[0]


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
    expected_anchor = data.get("image_physical_anchor")
    if not isinstance(expected_anchor, dict) or not str(
        expected_anchor.get("bubble_visual_fingerprint") or ""
    ).strip():
        return fail("image_slot_identity_missing")
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
                return fail(
                    str((frame or {}).get("reason") or "vision_window_frame_unavailable")
                    if isinstance(frame, dict)
                    else "vision_window_frame_unavailable"
                )
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
            bubble = _matching_bubble(
                surface,
                bubbles,
                [item for item in (frame.get("messages") or []) if isinstance(item, dict)],
                expected_anchor=expected_anchor,
                expected_role=sender_role,
            )
            if not bubble:
                return fail("image_bubble_slot_not_reconfirmed")
            if _cancelled(data):
                return fail("vision_cancelled")
            # Message ownership is decided by C2's same-row avatar contract.
            # Vision geometry is used only to click the already-authorized slot.
            direction = sender_role
            anchor = bubble.get("anchor") if isinstance(bubble.get("anchor"), dict) else {}
            current_bounds = [
                int(value)
                for value in list(bubble.get("bounds") or [])[:4]
            ]
            if len(current_bounds) != 4 or _bounds(current_bounds) is None:
                return fail("image_bubble_current_bounds_missing")
            sequence_before = ports.clipboard.sequence_number()
            right_click_result = ports.ui_action.right_click(
                int(anchor.get("x") or 0),
                int(anchor.get("y") or 0),
                bounds=current_bounds,
            )
            menu_opened = True
            if _cancelled(data):
                return fail("vision_cancelled")
            candidate_origin = list(frame.get("screen_origin") or [0, 0])
            if len(candidate_origin) < 2:
                candidate_origin = [0, 0]
            anchor_screen_x = int(
                (right_click_result or {}).get("screen_x")
                if isinstance(right_click_result, dict)
                else 0
            )
            anchor_screen_y = int(
                (right_click_result or {}).get("screen_y")
                if isinstance(right_click_result, dict)
                else 0
            )
            if not anchor_screen_x:
                anchor_screen_x = int(candidate_origin[0]) + int(anchor.get("x") or 0)
            if not anchor_screen_y:
                anchor_screen_y = int(candidate_origin[1]) + int(anchor.get("y") or 0)
            menu_frame = ports.window_frame.capture_frame(
                {
                    **data,
                    "phase": "image_context_menu",
                    "menu_anchor_screen": [anchor_screen_x, anchor_screen_y],
                }
            )
            if not isinstance(menu_frame, dict) or menu_frame.get("ok") is not True:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail("image_context_menu_unavailable")
            menu_surface = menu_frame.get("image")
            menu_size = getattr(menu_surface, "size", None) or tuple(menu_frame.get("image_size") or image_size)
            screen_origin = list(menu_frame.get("screen_origin") or [0, 0])
            if len(screen_origin) < 2:
                screen_origin = [0, 0]
            origin_x, origin_y = int(screen_origin[0]), int(screen_origin[1])
            anchor_in_menu_frame = (
                anchor_screen_x - origin_x,
                anchor_screen_y - origin_y,
            )
            copy_item = find_copy_menu_item(
                [item for item in (menu_frame.get("ocr_items") or []) if isinstance(item, dict)],
                tuple(menu_size),
                anchor=anchor_in_menu_frame,
                require_menu_cluster=True,
            )
            if not copy_item:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail("image_context_menu_copy_item_missing")
            if _cancelled(data):
                return fail("vision_cancelled")
            screen_click = getattr(ports.ui_action, "click_screen", None)
            if not callable(screen_click):
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail("image_context_menu_screen_click_unavailable")
            journal_update = data.get("action_journal_update")
            if callable(journal_update):
                journal_update(
                    action_phase="trigger_attempted",
                    business_state=None,
                    business_result_confirmed=False,
                )
            action_phase = "trigger_attempted"
            local_bounds = [
                int(value)
                for value in list(copy_item.get("bounds") or [])[:4]
            ]
            if len(local_bounds) != 4:
                return fail("image_context_menu_copy_bounds_missing")
            screen_click(
                origin_x + int(copy_item.get("x") or 0),
                origin_y + int(copy_item.get("y") or 0),
                bounds=[
                    origin_x + local_bounds[0],
                    origin_y + local_bounds[1],
                    origin_x + local_bounds[2],
                    origin_y + local_bounds[3],
                ],
            )
            menu_opened = False
            if sequence_before is None:
                return fail("clipboard_sequence_missing_before_copy")
            payload = None
            sequence_after = None
            clipboard_reason = "clipboard_sequence_unchanged_after_copy"
            wait_timeout = max(
                0.2,
                min(
                    5.0,
                    float(
                        data.get("clipboard_wait_timeout_seconds")
                        or CLIPBOARD_WAIT_TIMEOUT_SECONDS
                    ),
                ),
            )
            poll_interval = max(
                0.02,
                min(
                    0.25,
                    float(
                        data.get("clipboard_poll_interval_seconds")
                        or CLIPBOARD_POLL_INTERVAL_SECONDS
                    ),
                ),
            )
            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                if _cancelled(data):
                    return fail("vision_cancelled")
                candidate_sequence = ports.clipboard.sequence_number()
                if (
                    candidate_sequence is not None
                    and int(candidate_sequence) != int(sequence_before)
                ):
                    candidate_payload = ephemeral_image_from_memory(
                        ports.clipboard.read_current_bitmap(),
                        mime_type=str(data.get("mime_type") or "image/png"),
                    )
                    if candidate_payload is None:
                        clipboard_reason = "clipboard_current_content_not_bitmap"
                    else:
                        verified_sequence = ports.clipboard.sequence_number()
                        if (
                            verified_sequence is not None
                            and int(verified_sequence) == int(candidate_sequence)
                        ):
                            payload = candidate_payload
                            sequence_after = int(candidate_sequence)
                            break
                        candidate_payload.release()
                        clipboard_reason = "clipboard_sequence_changed_during_read"
                time.sleep(poll_interval)
            if payload is None or sequence_after is None:
                return fail(clipboard_reason)
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
                    "slot_identity_confirmed": True,
                    "slot_identity_evidence": dict(
                        bubble.get("identity_match_evidence") or {}
                    ),
                    "current_bubble_rect": list(bubble.get("bounds") or []),
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
