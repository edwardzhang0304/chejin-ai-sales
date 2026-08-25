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
from .slot_identity import (
    match_image_slot as _bubble_match_evidence,
    valid_bounds as _bounds,
)


CLIPBOARD_WAIT_TIMEOUT_SECONDS = 15.0
CLIPBOARD_POLL_INTERVAL_SECONDS = 0.08
_TEXT_MENU_LABELS = frozenset({"放大阅读", "翻译", "搜一搜"})
_IMAGE_MENU_LABELS = frozenset({"编辑", "用窗口打开", "另存为", "打开方式"})
_VOICE_MENU_LABELS = frozenset({"语音转文字", "收起文字"})
_PUBLIC_MENU_LABELS = frozenset({"复制", "转发", "收藏", "多选", "提醒", "引用", "删除"})
_KNOWN_MENU_LABELS = (
    _TEXT_MENU_LABELS
    | _IMAGE_MENU_LABELS
    | _VOICE_MENU_LABELS
    | _PUBLIC_MENU_LABELS
)


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "state": "vision_port_transaction_failed",
        "reason": str(reason or "vision_port_transaction_failed"),
        "assets": [],
        "messages": [],
        **extra,
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


def _item_bounds(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    bounds = item.get("bounds")
    try:
        if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
            result = tuple(float(value) for value in bounds[:4])
        else:
            result = (
                float(item.get("left")),
                float(item.get("top")),
                float(item.get("right")),
                float(item.get("bottom")),
            )
    except (TypeError, ValueError, IndexError):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def _menu_panel_bounds(value: Any) -> tuple[float, float, float, float] | None:
    try:
        bounds = tuple(float(item) for item in list(value)[:4])
    except (TypeError, ValueError):
        return None
    if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return bounds


def _inside_menu(
    item: dict[str, Any],
    menu_panel_bounds: tuple[float, float, float, float],
) -> bool:
    bounds = _item_bounds(item)
    if bounds is None:
        return False
    return (
        menu_panel_bounds[0] <= bounds[0]
        and menu_panel_bounds[1] <= bounds[1]
        and bounds[2] <= menu_panel_bounds[2]
        and bounds[3] <= menu_panel_bounds[3]
    )


def _exact_menu_label(text: Any) -> str:
    label = str(text or "").strip()
    for base in ("转发", "另存为"):
        if label in {f"{base}...", f"{base}…"}:
            return base
    return label


def _classify_context_menu(
    ocr_items: list[dict[str, Any]],
    copy_item: dict[str, Any] | None,
    *,
    menu_panel_bounds: Any,
) -> dict[str, Any]:
    """Classify exact labels contained by one confirmed popup window."""

    confirmed_bounds = _menu_panel_bounds(menu_panel_bounds)
    if confirmed_bounds is None:
        return {"kind": "unknown", "labels": [], "copy_item": None}
    candidates: list[tuple[dict[str, Any], str]] = []
    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        label = _exact_menu_label(item.get("text"))
        if label in _KNOWN_MENU_LABELS and _inside_menu(item, confirmed_bounds):
            candidates.append((dict(item), label))
    if (
        not isinstance(copy_item, dict)
        or _exact_menu_label(copy_item.get("text")) != "复制"
        or not _inside_menu(copy_item, confirmed_bounds)
    ):
        copy_item = None
    labels = {row[1] for row in candidates}
    is_text = "放大阅读" in labels or {"翻译", "搜一搜"} <= labels
    is_image = "复制" in labels and bool(labels & _IMAGE_MENU_LABELS)
    is_voice = bool(labels & _VOICE_MENU_LABELS)
    kinds = [
        kind
        for kind, matched in (
            ("text", is_text),
            ("image", is_image),
            ("voice", is_voice),
        )
        if matched
    ]
    kind = (
        kinds[0]
        if len(kinds) == 1
        else ("conflict" if kinds else "unknown")
    )
    return {
        "kind": kind,
        "labels": sorted(labels),
        "copy_item": copy_item if kind == "image" else None,
    }


def _safe_copy_click_geometry(
    copy_item: Any,
    *,
    menu_panel_bounds: Any,
) -> dict[str, Any] | None:
    """Return click geometry only when one Copy item is fully inside the popup."""

    confirmed_bounds = _menu_panel_bounds(menu_panel_bounds)
    if (
        not isinstance(copy_item, dict)
        or confirmed_bounds is None
        or not _inside_menu(copy_item, confirmed_bounds)
    ):
        return None
    item_bounds = _item_bounds(copy_item)
    if item_bounds is None:
        return None
    try:
        raw_x = copy_item.get("x", copy_item.get("center_x"))
        raw_y = copy_item.get("y", copy_item.get("center_y"))
        click_x = int(
            raw_x
            if raw_x is not None
            else (item_bounds[0] + item_bounds[2]) / 2
        )
        click_y = int(
            raw_y
            if raw_y is not None
            else (item_bounds[1] + item_bounds[3]) / 2
        )
    except (TypeError, ValueError):
        return None
    if not (
        item_bounds[0] <= click_x <= item_bounds[2]
        and item_bounds[1] <= click_y <= item_bounds[3]
        and confirmed_bounds[0] <= click_x <= confirmed_bounds[2]
        and confirmed_bounds[1] <= click_y <= confirmed_bounds[3]
    ):
        return None
    return {
        "x": click_x,
        "y": click_y,
        "bounds": [int(value) for value in item_bounds],
    }


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
    # v0.9.35 permits one physical image action only. A copied bitmap whose
    # result cannot be confirmed is journaled for recovery; it is never
    # acquired again in the same or a nested transaction.
    retry_attempt = 0
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
                    else "vision_window_frame_unavailable",
                    reason_detail=(
                        str((frame or {}).get("reason_detail") or "")
                        if isinstance(frame, dict)
                        else ""
                    ),
                )
            surface = frame.get("image")
            image_size = getattr(surface, "size", None) or tuple(frame.get("image_size") or ())
            if surface is None or len(image_size) != 2:
                return fail("vision_window_frame_invalid")
            expected_viewport_digest = str(
                data.get("expected_message_viewport_change_digest") or ""
            ).strip()
            current_viewport_digest = str(
                frame.get("message_viewport_change_digest") or ""
            ).strip()
            viewport_guard_required = bool(
                data.get("message_viewport_guard_required")
                or expected_viewport_digest
            )
            if viewport_guard_required and (
                not expected_viewport_digest
                or not current_viewport_digest
            ):
                return fail(
                    "C2_PRE_SEND_LAYOUT_INVALID",
                    state="worker_environment_failed",
                    transaction={
                        "expected_message_viewport_change_digest": (
                            expected_viewport_digest
                        ),
                        "current_message_viewport_change_digest": (
                            current_viewport_digest
                        ),
                        "message_viewport_change_evidence": dict(
                            frame.get("message_viewport_change_evidence") or {}
                        ),
                    },
                )
            if (
                viewport_guard_required
                and current_viewport_digest != expected_viewport_digest
            ):
                return fail(
                    "C2_IMAGE_REIDENTIFICATION_REQUIRED",
                    state="image_reidentification_required",
                    transaction={
                        "expected_message_viewport_change_digest": (
                            expected_viewport_digest
                        ),
                        "current_message_viewport_change_digest": (
                            current_viewport_digest
                        ),
                        "message_viewport_change_evidence": dict(
                            frame.get("message_viewport_change_evidence") or {}
                        ),
                        "candidate_count": len(
                            frame.get("messages") or []
                        ),
                    },
                )
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
            expected_candidate_group_count = int(
                data.get("expected_image_candidate_group_count") or 0
            )
            if "expected_image_candidate_group_count" in data and (
                expected_candidate_group_count <= 0
                or len(current_candidates)
                != expected_candidate_group_count
            ):
                return fail(
                    "C2_IMAGE_REIDENTIFICATION_REQUIRED",
                    state="image_reidentification_required",
                    transaction={
                        "expected_candidate_group_count": (
                            expected_candidate_group_count
                        ),
                        "current_candidate_group_count": len(
                            current_candidates
                        ),
                    },
                )
            if not current_candidates:
                return fail(
                    "C2_PRE_SEND_IMAGE_TARGET_NOT_FOUND",
                    state="image_not_visible",
                )
            match_evidence = _bubble_match_evidence(
                current_candidates,
                expected_anchor=expected_anchor,
                expected_role=sender_role,
                expected_bounds=data.get("bubble_rect"),
            )
            if match_evidence.get("state") == "not_visible":
                return fail(
                    "C2_PRE_SEND_IMAGE_TARGET_NOT_FOUND",
                    state="image_not_visible",
                    transaction={
                        "slot_identity_evidence": match_evidence,
                    },
                )
            bubble = dict(match_evidence.get("bubble") or {})
            if not bubble:
                match_state = str(
                    match_evidence.get("state") or ""
                ).strip()
                return fail(
                    (
                        "C2_PRE_SEND_MESSAGE_ROLE_UNCONFIRMED"
                        if match_state == "role_mismatch"
                        else "C2_PRE_SEND_IMAGE_TARGET_AMBIGUOUS"
                    ),
                    state="image_identity_failed",
                    transaction={
                        "slot_identity_evidence": match_evidence,
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
                return fail(
                    "C2_IMAGE_MENU_OPERATION_FAILED",
                    transaction={
                        "status": "menu_panel_unconfirmed",
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                    },
                )
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
            menu_ocr_items = [
                item
                for item in (menu_frame.get("ocr_items") or [])
                if isinstance(item, dict)
            ]
            confirmed_menu_bounds = _menu_panel_bounds(
                menu_frame.get("menu_panel_bounds")
            )
            bounded_menu_items = (
                [
                    item
                    for item in menu_ocr_items
                    if _inside_menu(item, confirmed_menu_bounds)
                ]
                if confirmed_menu_bounds is not None
                else []
            )
            copy_item = find_copy_menu_item(
                bounded_menu_items,
                tuple(menu_size),
                anchor=anchor_in_menu_frame,
            )
            classification = _classify_context_menu(
                menu_ocr_items,
                copy_item,
                menu_panel_bounds=confirmed_menu_bounds,
            )
            menu_kind = str(classification.get("kind") or "unknown")
            if menu_kind in {"text", "voice"}:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail(
                    "C2_IMAGE_SOURCE_INVALID",
                    transaction={
                        "status": f"{menu_kind}_context_menu_rejected",
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                        "menu_labels": classification.get("labels") or [],
                        "menu_panel_bounds": list(confirmed_menu_bounds or []),
                    },
                )
            if menu_kind != "image":
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail(
                    "C2_IMAGE_MENU_OPERATION_FAILED",
                    transaction={
                        "status": (
                            "menu_panel_unconfirmed"
                            if confirmed_menu_bounds is None
                            else (
                                "menu_evidence_conflict"
                                if menu_kind == "conflict"
                                else "menu_evidence_incomplete"
                            )
                        ),
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                        "menu_labels": classification.get("labels") or [],
                        "menu_panel_bounds": list(confirmed_menu_bounds or []),
                    },
                )
            copy_item = classification.get("copy_item")
            if not isinstance(copy_item, dict):
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail(
                    "C2_IMAGE_MENU_OPERATION_FAILED",
                    transaction={
                        "status": "menu_copy_item_unsafe",
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                    },
                )
            if _cancelled(data):
                return fail("vision_cancelled")
            screen_click = getattr(ports.ui_action, "click_screen", None)
            if not callable(screen_click):
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail(
                    "C2_IMAGE_MENU_OPERATION_FAILED",
                    transaction={
                        "status": "menu_copy_item_unsafe",
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                    },
                )
            journal_update = data.get("action_journal_update")
            copy_geometry = _safe_copy_click_geometry(
                copy_item,
                menu_panel_bounds=confirmed_menu_bounds,
            )
            if copy_geometry is None:
                _dismiss_menu_safely(ports.ui_action)
                menu_opened = False
                return fail(
                    "C2_IMAGE_MENU_OPERATION_FAILED",
                    transaction={
                        "status": "menu_copy_item_unsafe",
                        "right_click_ok": True,
                        "menu_copy_confirmed": False,
                        "clipboard_content_read": False,
                    },
                )
            local_bounds = list(copy_geometry["bounds"])
            if callable(journal_update):
                journal_update(
                    action_phase="trigger_attempted",
                    business_state=None,
                    business_result_confirmed=False,
                )
            action_phase = "trigger_attempted"
            screen_click(
                int(copy_geometry["x"]),
                int(copy_geometry["y"]),
                bounds=local_bounds,
            )
            # A successful input injection does not prove that WeChat
            # accepted the Copy command or closed its popup.  Keep the menu
            # marked open until clipboard evidence confirms the action, so
            # every no-progress/error exit performs the safe dismissal in
            # ``finally``.
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
                config.get("image_contract")
                if isinstance(
                    config.get("image_contract"),
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
                    candidate_payload = ephemeral_image_from_memory(
                        ports.clipboard.read_current_bitmap(),
                        mime_type=str(data.get("mime_type") or "image/png"),
                        source_limits=source_limits,
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
                            acquired_payload = candidate_payload
                            sequence_after = int(candidate_sequence)
                            break
                        candidate_payload.release()
                        clipboard_reason = "clipboard_sequence_changed_during_read"
                time.sleep(poll_interval)
            if payload is None or sequence_after is None:
                if clipboard_reason == "clipboard_current_content_not_bitmap":
                    return fail(
                        clipboard_reason,
                        transaction={
                            "status": "clipboard_current_content_not_bitmap",
                            "right_click_ok": True,
                            "menu_copy_confirmed": True,
                            "clipboard_sequence_changed": True,
                            "clipboard_content_read": True,
                            "clipboard_image_valid": False,
                        },
                    )
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
                return fail(
                    "clipboard_image_fingerprint_mismatch",
                    state="image_action_result_unconfirmed",
                    transaction={
                        "status": "clipboard_rejected",
                        "right_click_ok": True,
                        "menu_copy_confirmed": True,
                        "clipboard_sequence_changed": True,
                        "clipboard_content_read": True,
                        "clipboard_image_valid": True,
                        "clipboard_image_matches_target": False,
                        "physical_action_may_have_occurred": True,
                        "automatic_retry_allowed": False,
                    },
                )
            # Clearing is permitted only after the copied bitmap is proven to
            # match the target slot. A stable but mismatched generation may
            # belong to a concurrent user clipboard action.
            owned_clipboard_sequence = int(sequence_after)
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
            menu_opened = False
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
