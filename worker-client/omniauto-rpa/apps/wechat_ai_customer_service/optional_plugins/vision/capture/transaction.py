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
    find_copy_menu_item,
    image_visual_fingerprint_distance,
)
from .visual_fingerprint import (
    clipboard_payload_fingerprint,
    crop_fingerprint,
    fingerprints_match,
)


CLIPBOARD_WAIT_TIMEOUT_SECONDS = 15.0
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


def _bubble_match_evidence(
    current_candidates: list[dict[str, Any]],
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
        return {"state": "identity_invalid", "bubble": {}}
    try:
        expected_occurrence = int(anchor.get("occurrence_index") or 0)
        expected_occurrence_count = int(anchor.get("occurrence_count") or 0)
    except (TypeError, ValueError):
        return {"state": "identity_invalid", "bubble": {}}
    if expected_occurrence_count <= 0:
        return {"state": "identity_invalid", "bubble": {}}
    expected_preceding = str(
        anchor.get("preceding_stable_message") or ""
    ).strip()
    expected_following = str(
        anchor.get("following_stable_message") or ""
    ).strip()
    fingerprint_matches: list[dict[str, Any]] = []
    role_conflicts: list[dict[str, Any]] = []
    for bubble in current_candidates:
        current_anchor = (
            bubble.get("image_physical_anchor")
            if isinstance(bubble.get("image_physical_anchor"), dict)
            else {}
        )
        current_role = str(
            current_anchor.get("sender_role") or ""
        ).strip().lower()
        current_visual_side = str(
            current_anchor.get("visual_side") or bubble.get("side") or ""
        ).strip().lower()
        fingerprint_distance = image_visual_fingerprint_distance(
            expected_fingerprint,
            current_anchor.get("bubble_visual_fingerprint"),
        )
        if (
            fingerprint_distance is None
            or fingerprint_distance > IMAGE_FINGERPRINT_MAX_DISTANCE
        ):
            continue
        if current_role != expected_role:
            role_conflicts.append(
                {
                    "expected_c2_sender_role": expected_role,
                    "refreshed_c2_sender_role": (
                        current_role
                        if current_role in {"customer", "self"}
                        else "unknown"
                    ),
                    "visual_side": current_visual_side,
                    "fingerprint_distance": fingerprint_distance,
                }
            )
            continue
        fingerprint_matches.append(
            {
                **bubble,
                "identity_match_evidence": {
                    "expected_c2_sender_role": expected_role,
                    "refreshed_c2_sender_role": current_role,
                    "visual_side": current_visual_side,
                    "visual_side_consistent": (
                        current_visual_side == current_role
                    ),
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
    if len(contextual_matches) == 1:
        return {
            "state": "matched",
            "bubble": contextual_matches[0],
            "fingerprint_match_count": len(fingerprint_matches),
            "contextual_match_count": 1,
        }
    if not fingerprint_matches:
        if role_conflicts:
            return {
                "state": "role_mismatch",
                "bubble": {},
                "fingerprint_match_count": len(role_conflicts),
                "contextual_match_count": 0,
                "role_conflicts": role_conflicts,
            }
        return {
            "state": "not_visible",
            "bubble": {},
            "fingerprint_match_count": 0,
            "contextual_match_count": 0,
        }
    return {
        "state": "ambiguous",
        "bubble": {},
        "fingerprint_match_count": len(fingerprint_matches),
        "contextual_match_count": len(contextual_matches),
    }


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
    return _acquire_current_image_via_ports(
        ports,
        data,
        lease_already_held=False,
    )


def _acquire_current_image_via_ports(
    ports: VisionHostPorts,
    data: dict[str, Any],
    *,
    lease_already_held: bool,
) -> dict[str, Any]:
    action_phase = str(
        data.get("_prior_action_phase") or "not_attempted"
    )
    retry_attempt = max(
        0,
        min(1, int(data.get("_clipboard_fingerprint_retry_attempt") or 0)),
    )
    terminal_result: dict[str, Any] | None = None

    def fail(reason: str, **extra: Any) -> dict[str, Any]:
        nonlocal terminal_result
        transaction = dict(extra.pop("transaction", {}) or {})
        transaction.setdefault("action_phase", action_phase)
        transaction.setdefault(
            "clipboard_fingerprint_retry_count",
            retry_attempt,
        )
        terminal_result = _failure(
            reason,
            action_phase=action_phase,
            transaction=transaction,
            **extra,
        )
        return terminal_result

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
        if ports.rpa_lease is not None and not lease_already_held
        else nullcontext({"acquired": True, "source": "vision_port_noop_lease"})
    )
    surface = None
    menu_surface = None
    menu_opened = False
    owned_clipboard_sequence: int | None = None
    acquired_payload = None
    payload_transferred = False

    def clear_owned_clipboard() -> dict[str, Any]:
        nonlocal owned_clipboard_sequence
        if owned_clipboard_sequence is None:
            return {
                "ok": True,
                "cleared": False,
                "reason": "clipboard_not_owned",
            }
        sequence = int(owned_clipboard_sequence)
        clear_current = getattr(ports.clipboard, "clear_current", None)
        if not callable(clear_current):
            return {
                "ok": False,
                "reason": "clipboard_clear_port_missing",
            }
        try:
            result = clear_current(sequence)
        except Exception as exc:  # noqa: BLE001 - cleanup must be normalized
            return {
                "ok": False,
                "reason": "clipboard_clear_exception",
                "error_type": type(exc).__name__,
            }
        normalized = (
            dict(result)
            if isinstance(result, dict)
            else {"ok": False, "reason": "clipboard_clear_invalid_result"}
        )
        reason = str(normalized.get("reason") or "")
        if normalized.get("ok") is True:
            owned_clipboard_sequence = None
            normalized.setdefault("cleared", True)
            return normalized
        if reason in {
            "clipboard_sequence_not_current_for_clear",
            "clipboard_sequence_changed_before_clear",
        }:
            owned_clipboard_sequence = None
            return {
                "ok": True,
                "cleared": False,
                "reason": "clipboard_replaced_by_external",
            }
        return normalized

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
            current_candidates = [
                dict(item)
                for item in (frame.get("messages") or [])
                if isinstance(item, dict)
                and str(
                    item.get("type")
                    or item.get("message_type")
                    or ""
                ).strip().lower()
                == "image"
            ]
            if not current_candidates:
                return fail(
                    "image_bubble_not_visible_after_refresh",
                    state="image_not_visible",
                )
            match_evidence = _bubble_match_evidence(
                current_candidates,
                expected_anchor=expected_anchor,
                expected_role=sender_role,
            )
            if match_evidence.get("state") == "not_visible":
                return fail(
                    "image_bubble_not_visible_after_refresh",
                    state="image_not_visible",
                    transaction={
                        "identity_match_evidence": match_evidence,
                    },
                )
            bubble = dict(match_evidence.get("bubble") or {})
            if not bubble:
                return fail(
                    "C2_IMAGE_SLOT_RECONFIRM_FAILED",
                    state="image_identity_failed",
                    transaction={
                        "identity_match_evidence": match_evidence,
                    },
                )
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
            try:
                expected_clipboard_fingerprint = crop_fingerprint(
                    surface,
                    current_bounds,
                )
            except Exception:
                expected_clipboard_fingerprint = {}
            if not expected_clipboard_fingerprint:
                return fail("image_bubble_clipboard_fingerprint_missing")
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
            config = (
                data.get("config")
                if isinstance(data.get("config"), dict)
                else {}
            )
            image_contract = (
                config.get("_chejin_image_contract")
                if isinstance(
                    config.get("_chejin_image_contract"),
                    dict,
                )
                else {}
            )
            source_limits = (
                image_contract.get("source_limits")
                if isinstance(
                    image_contract.get("source_limits"),
                    dict,
                )
                else {}
            )
            wait_timeout = max(
                0.2,
                min(
                    60.0,
                    float(
                        data.get("clipboard_wait_timeout_seconds")
                        or source_limits.get(
                            "clipboard_no_progress_timeout_seconds"
                        )
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
                    prove_ownership = getattr(
                        ports.clipboard,
                        "claim_copy_ownership",
                        None,
                    )
                    ownership = (
                        prove_ownership(int(candidate_sequence))
                        if callable(prove_ownership)
                        else {"owned": True, "reason": "test_port_bitmap_proof"}
                    )
                    candidate_payload = ephemeral_image_from_memory(
                        ports.clipboard.read_current_bitmap(),
                        mime_type=str(data.get("mime_type") or "image/png"),
                        source_limits=source_limits,
                    )
                    if candidate_payload is None:
                        clipboard_reason = "clipboard_current_content_not_bitmap"
                        if (
                            isinstance(ownership, dict)
                            and ownership.get("owned") is True
                        ):
                            owned_clipboard_sequence = int(
                                candidate_sequence
                            )
                    else:
                        if (
                            not isinstance(ownership, dict)
                            or ownership.get("owned") is not True
                        ):
                            candidate_payload.release()
                            clipboard_reason = str(
                                (ownership or {}).get("reason")
                                or "clipboard_copy_ownership_unconfirmed"
                            )
                            time.sleep(poll_interval)
                            continue
                        verified_sequence = ports.clipboard.sequence_number()
                        if (
                            verified_sequence is not None
                            and int(verified_sequence) == int(candidate_sequence)
                        ):
                            payload = candidate_payload
                            acquired_payload = candidate_payload
                            sequence_after = int(candidate_sequence)
                            # A stable sequence plus a successfully decoded
                            # bitmap proves this copy generation belongs to
                            # the current transaction. Sequence alone does not.
                            owned_clipboard_sequence = int(
                                candidate_sequence
                            )
                            break
                        candidate_payload.release()
                        clipboard_reason = "clipboard_sequence_changed_during_read"
                time.sleep(poll_interval)
            if payload is None or sequence_after is None:
                return fail(clipboard_reason)
            if _cancelled(data):
                payload.release()
                acquired_payload = None
                return fail("vision_cancelled")
            actual_clipboard_fingerprint = clipboard_payload_fingerprint(
                payload
            )
            clipboard_matches_target = fingerprints_match(
                expected_clipboard_fingerprint,
                actual_clipboard_fingerprint,
            )
            if not clipboard_matches_target:
                payload.release()
                acquired_payload = None
                clear_result = clear_owned_clipboard()
                if clear_result.get("ok") is not True:
                    return fail(
                        "C2_IMAGE_CLIPBOARD_CLEAR_FAILED",
                        transaction={
                            "status": "clipboard_clear_failed",
                            "clipboard_image_matches_target": False,
                            "clipboard_cleared": False,
                            "clipboard_clear_reason": str(
                                clear_result.get("reason") or ""
                            ),
                        },
                    )
                if (
                    str(clear_result.get("reason") or "")
                    == "clipboard_replaced_by_external"
                ):
                    return fail(
                        "clipboard_replaced_by_external",
                        transaction={
                            "status": "clipboard_replaced_by_external",
                            "clipboard_image_matches_target": False,
                            "clipboard_cleared": False,
                            "clipboard_clear_reason": (
                                "clipboard_replaced_by_external"
                            ),
                        },
                    )
                if retry_attempt < 1:
                    retry_data = {
                        **data,
                        "_clipboard_fingerprint_retry_attempt": 1,
                        "_prior_action_phase": "trigger_attempted",
                    }
                    retry_result = _acquire_current_image_via_ports(
                        ports,
                        retry_data,
                        lease_already_held=True,
                    )
                    retry_transaction = (
                        dict(retry_result.get("transaction") or {})
                        if isinstance(retry_result, dict)
                        else {}
                    )
                    retry_transaction[
                        "clipboard_fingerprint_retry_count"
                    ] = 1
                    retry_transaction[
                        "clipboard_fingerprint_first_attempt_mismatch"
                    ] = True
                    if isinstance(retry_result, dict):
                        retry_result["transaction"] = retry_transaction
                    return retry_result
                return fail(
                    "clipboard_image_fingerprint_mismatch",
                    transaction={
                        "status": "clipboard_rejected",
                        "right_click_ok": True,
                        "menu_copy_confirmed": True,
                        "clipboard_sequence_changed": True,
                        "clipboard_content_read": True,
                        "clipboard_image_valid": True,
                        "clipboard_image_matches_target": False,
                    },
                )
            clear_result = clear_owned_clipboard()
            if clear_result.get("ok") is not True:
                payload.release()
                acquired_payload = None
                action_phase = "confirmed"
                if callable(journal_update):
                    journal_update(
                        action_phase="confirmed",
                        business_state="failed",
                        business_result_confirmed=False,
                    )
                return fail(
                    "C2_IMAGE_CLIPBOARD_CLEAR_FAILED",
                    transaction={
                        "status": "clipboard_clear_failed",
                        "right_click_ok": True,
                        "menu_copy_confirmed": True,
                        "clipboard_sequence_changed": True,
                        "clipboard_content_read": True,
                        "clipboard_image_valid": True,
                        "clipboard_image_matches_target": True,
                        "clipboard_cleared": False,
                        "clipboard_clear_reason": str(
                            clear_result.get("reason") or ""
                        ),
                    },
                )
            image_sha256 = hashlib.sha256(bytes(payload.image_bytes)).hexdigest()
            visual_side = str(
                (bubble.get("identity_match_evidence") or {}).get(
                    "visual_side"
                )
                or bubble.get("side")
                or "unknown"
            ).strip().lower()
            if callable(journal_update):
                journal_update(
                    action_phase="confirmed",
                    business_state="clipboard_confirmed",
                    business_result_confirmed=False,
                )
            payload_transferred = True
            return {
                "ok": True,
                "state": "image_clipboard_copied",
                "action_phase": "confirmed",
                "direction": direction,
                "occurrence": {
                    "sender": direction,
                    "sender_role": direction,
                    "visual_side": visual_side,
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
                    "clipboard_image_matches_target": True,
                    "clipboard_cleared": bool(
                        clear_result.get(
                            "cleared",
                            clear_result.get("ok"),
                        )
                    ),
                    "clipboard_clear_reason": str(
                        clear_result.get("reason") or ""
                    ),
                    "clipboard_fingerprint_retry_count": retry_attempt,
                    "visual_side": visual_side,
                    "visual_side_consistent": (
                        visual_side in {"customer", "self"}
                        and visual_side == direction
                    ),
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
        cleanup_result = clear_owned_clipboard()
        if (
            cleanup_result.get("ok") is not True
            and isinstance(terminal_result, dict)
        ):
            original_reason = str(terminal_result.get("reason") or "")
            transaction = dict(
                terminal_result.get("transaction") or {}
            )
            transaction.update(
                {
                    "status": "clipboard_clear_failed",
                    "clipboard_cleared": False,
                    "clipboard_clear_reason": str(
                        cleanup_result.get("reason") or ""
                    ),
                    "original_failure_reason": original_reason,
                }
            )
            terminal_result.update(
                {
                    "reason": "C2_IMAGE_CLIPBOARD_CLEAR_FAILED",
                    "transaction": transaction,
                }
            )
        elif isinstance(terminal_result, dict):
            transaction = dict(
                terminal_result.get("transaction") or {}
            )
            if (
                str(cleanup_result.get("reason") or "")
                == "clipboard_replaced_by_external"
            ):
                transaction.update(
                    {
                        "clipboard_cleared": False,
                        "clipboard_clear_reason": (
                            "clipboard_replaced_by_external"
                        ),
                    }
                )
                terminal_result["transaction"] = transaction
        if acquired_payload is not None and not payload_transferred:
            try:
                acquired_payload.release()
            except Exception:
                pass
        if menu_opened:
            _dismiss_menu_safely(ports.ui_action)
        for transient_image in (surface, menu_surface):
            close = getattr(transient_image, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
