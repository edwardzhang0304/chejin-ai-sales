from __future__ import annotations

import os
from typing import Any


def execute_voice_transcribe(
    *,
    sidecar_ops: Any,
    hwnd: int,
    probe: dict[str, Any],
    target: str,
    conversation_type: str = "",
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Run the legacy Win32 voice action through injected sidecar primitives."""

    before_screenshot, before_path = sidecar_ops.capture_wechat(
        hwnd,
        artifact_dir=artifact_dir,
        label="voice_transcribe_before",
    )
    before_items = sidecar_ops.run_ocr(before_screenshot)
    geometry = sidecar_ops.get_window_geometry(hwnd)
    image_size = getattr(
        before_screenshot,
        "size",
        (int(geometry.get("width") or 0), int(geometry.get("height") or 0)),
    )
    before_messages = sidecar_ops.parse_messages_from_ocr(
        before_items,
        image_size,
        target=target,
        conversation_type=conversation_type,
    )
    duration_target = sidecar_ops.find_latest_untranscribed_voice_duration_target(
        before_items,
        image_size,
        screenshot=before_screenshot,
    )
    context_menu_attempt: dict[str, Any] | None = None
    dismiss_attempt: dict[str, Any] | None = None
    if not duration_target:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "voice_transcribe_target_not_found",
            "window_probe": probe,
            "target": target,
            "screenshot_path": before_path,
            "ocr_items_count": len(before_items),
            "messages": before_messages,
            "error": "No visible WeChat voice-to-text affordance was found.",
        }
    context_menu_attempt = sidecar_ops.open_voice_transcribe_context_menu(
        hwnd,
        duration_target,
        image_size=image_size,
        artifact_dir=artifact_dir,
    )
    click_target = (
        context_menu_attempt.get("click_target")
        if isinstance(context_menu_attempt, dict)
        else None
    )
    if not isinstance(click_target, dict):
        menu_reason = (
            str(context_menu_attempt.get("reason") or "")
            if isinstance(context_menu_attempt, dict)
            else ""
        )
        right_click_meta = (
            context_menu_attempt.get("right_click")
            if isinstance(context_menu_attempt, dict)
            and isinstance(context_menu_attempt.get("right_click"), dict)
            else {}
        )
        if bool(right_click_meta.get("ok")) and not menu_reason.startswith("voice_window_lost"):
            dismiss_attempt = sidecar_ops.dismiss_voice_transcribe_context_menu(hwnd)
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "voice_transcribe_context_menu_target_not_found",
            "window_probe": probe,
            "target": target,
            "screenshot_path": (
                str(context_menu_attempt.get("menu_screenshot_path") or before_path)
                if isinstance(context_menu_attempt, dict)
                else before_path
            ),
            "ocr_items_count": len(before_items),
            "messages": before_messages,
            "duration_target": duration_target,
            "context_menu_attempt": context_menu_attempt or {},
            "dismiss_context_menu": dismiss_attempt or {},
            "error": "Right-click menu did not expose a visible voice-to-text action.",
        }

    click_x, click_y, jitter_meta = sidecar_ops.jitter_voice_transcribe_click_point(
        click_target,
        geometry,
    )
    click_bounds = [int(value) for value in click_target.get("click_bounds") or []]
    click_result = sidecar_ops.human_window_image_click_in_bounds(
        hwnd,
        click_x,
        click_y,
        bounds=click_bounds,
        action_name="voice_transcribe_context_menu_click",
    )
    wait_ms = sidecar_ops.bounded_int(
        os.getenv("WECHAT_WIN32_OCR_VOICE_TRANSCRIBE_WAIT_MS"),
        default=2600,
        minimum=500,
        maximum=15000,
    )
    sidecar_ops.humanized_action_sleep(max(200, wait_ms - 500), wait_ms + 900)

    post_action_probe = sidecar_ops.probe_wechat_windows()
    post_action_visible = post_action_probe.get("visible_main_windows") or []
    if not any(
        int(item.get("hwnd") or 0) == int(hwnd)
        for item in post_action_visible
        if isinstance(item, dict)
    ):
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "voice_transcribe_window_lost_after_context_menu_click",
            "window_probe": probe,
            "post_action_window_probe": post_action_probe,
            "target": target,
            "duration_target": duration_target,
            "context_menu_attempt": context_menu_attempt or {},
            "click_target": click_target,
            "click": click_result,
            "planned_click_point": [click_x, click_y],
            "click_jitter": jitter_meta,
            "wait_ms": wait_ms,
            "before_messages": before_messages,
            "new_messages": [],
            "transcribed_messages": [],
            "ocr_items_count": 0,
            "risk_stop_recommended": True,
            "risk_stop_reason": "voice_transcribe_window_lost_after_context_menu_click",
        }

    after_screenshot, after_path = sidecar_ops.capture_wechat(
        hwnd,
        artifact_dir=artifact_dir,
        label="voice_transcribe_after",
    )
    after_items = sidecar_ops.run_ocr(after_screenshot)
    after_size = getattr(after_screenshot, "size", image_size)
    after_messages = sidecar_ops.parse_messages_from_ocr(
        after_items,
        after_size,
        target=target,
        conversation_type=conversation_type,
    )
    before_keys = {
        sidecar_ops.sidecar_message_content_key(message) for message in before_messages
    }
    new_messages = [
        message
        for message in after_messages
        if sidecar_ops.sidecar_message_content_key(message) not in before_keys
    ]
    transcribed_messages = [
        message
        for message in new_messages
        if not sidecar_ops.voice_duration_text_like(
            str(message.get("content_clean") or message.get("content") or "")
        )
        and not sidecar_ops.voice_transcribe_button_text_like(
            str(message.get("content_clean") or message.get("content") or "")
        )
    ]
    state = (
        "voice_transcribe_context_menu_clicked"
        if click_result.get("ok")
        else "voice_transcribe_context_menu_click_failed"
    )
    if click_result.get("ok") and not transcribed_messages:
        state = "voice_transcribe_context_menu_no_new_text"
    if not transcribed_messages:
        dismiss_attempt = sidecar_ops.dismiss_voice_transcribe_context_menu(hwnd)
    return {
        "ok": bool(click_result.get("ok")),
        "online": True,
        "adapter": "win32_ocr",
        "state": state,
        "window_probe": probe,
        "target": target,
        "before_screenshot_path": before_path,
        "after_screenshot_path": after_path,
        "duration_target": duration_target,
        "click_target": click_target,
        "context_menu_attempt": context_menu_attempt or {},
        "dismiss_context_menu": dismiss_attempt or {},
        "post_action_window_probe": post_action_probe,
        "click": click_result,
        "planned_click_point": [click_x, click_y],
        "click_jitter": jitter_meta,
        "wait_ms": wait_ms,
        "before_messages": before_messages,
        "messages": after_messages,
        "new_messages": new_messages,
        "transcribed_messages": transcribed_messages,
        "ocr_items_count": len(after_items),
    }
