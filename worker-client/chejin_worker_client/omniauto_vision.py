from __future__ import annotations

import hashlib
import sys
import os
import base64
import json
import re
import subprocess
import threading
import time
from urllib.parse import urlparse
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from .c2_contract import (
    image_contract,
    observation_role_is_trusted,
    validate_image_result_schema,
)
from .action_journal import (
    action_journal_phase,
    update_action_journal_item,
)
from .emergency_stop import emergency_stop_requested
from .omniauto_ocr_client import (
    CancellableOmniAutoOcr,
    OmniAutoOcrCancelledError,
)
from .subprocess_protocol import (
    UNICODE_PROTOCOL_SENTINEL,
    encode_subprocess_json,
    require_unicode_protocol,
    subprocess_utf8_environment,
)
from .vision_credentials import (
    OFFICIAL_VISION_BASE_URL,
    OFFICIAL_VISION_MODEL,
    OFFICIAL_VISION_PROVIDER,
    OFFICIAL_VISION_REQUEST_STYLE,
    VISION_API_KEY_ENV,
    install_resolved_vision_api_key,
    is_official_vision_runtime,
    resolve_vision_runtime_settings,
)


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

DEFAULT_VISION_PROVIDER = OFFICIAL_VISION_PROVIDER
DEFAULT_VISION_BASE_URL = OFFICIAL_VISION_BASE_URL
DEFAULT_VISION_MODEL = OFFICIAL_VISION_MODEL
DEFAULT_VISION_REQUEST_STYLE = OFFICIAL_VISION_REQUEST_STYLE
DEFAULT_VISION_TIMEOUT_SECONDS = 60.0
MAX_VISION_TIMEOUT_SECONDS = 300.0
VISION_WINDOW_STABLE_FAILURE_REASONS = frozenset(
    {
        "vision_window_context_capture_missing",
        "vision_window_context_missing",
        "vision_window_capture_failed",
        "vision_window_frame_finalize_failed",
        "vision_window_message_parse_failed",
        "vision_window_ocr_failed",
    }
)
VISION_API_KEY_ENV_NAMES = (
    VISION_API_KEY_ENV,
)


class VisionCancelledError(RuntimeError):
    pass


def _cancel_requested(callback: Callable[[], bool] | None) -> bool:
    if emergency_stop_requested():
        return True
    if not callable(callback):
        return False
    try:
        return bool(callback())
    except Exception:
        return True


def _safe_exception_reason(exc: Exception, fallback: str) -> str:
    """Keep a stable diagnostic code without logging paths or image content."""

    message = str(exc or "").strip()
    token = message.split(":", 1)[0].strip()
    if token and re.fullmatch(r"[A-Za-z0-9_]+", token):
        return token.lower()
    return str(fallback or "vision_runtime_failed")


def _normalize_window_frame_failure_reason(
    reason: Any,
    fallback: str,
) -> tuple[str, str]:
    detail = str(reason or fallback).strip() or fallback
    if detail in VISION_WINDOW_STABLE_FAILURE_REASONS:
        return detail, detail
    if "ocr" in detail.lower():
        return "vision_window_ocr_failed", detail
    return fallback, detail


def _window_context_hwnd(value: Any) -> int:
    try:
        return int((value or {}).get("hwnd") or 0) if isinstance(value, dict) else 0
    except (TypeError, ValueError):
        return 0


def _vision_timeout_seconds(value: Any = None) -> float:
    raw = (
        value
        if value not in (None, "")
        else os.environ.get("CUSTOMER_IMAGE_UNDERSTANDING_TIMEOUT_SECONDS")
    )
    try:
        parsed = float(raw) if raw not in (None, "") else DEFAULT_VISION_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        parsed = DEFAULT_VISION_TIMEOUT_SECONDS
    return max(3.0, min(MAX_VISION_TIMEOUT_SECONDS, parsed))


def _vision_process_timeout_seconds(single_attempt_timeout: Any) -> float:
    provider_contract = image_contract().get("provider_contract") or {}
    max_attempts = max(
        1,
        int(provider_contract.get("max_provider_attempts") or 1),
    )
    process_overhead = max(
        5.0,
        float(provider_contract.get("process_overhead_seconds") or 5.0),
    )
    return max(
        3.0,
        _vision_timeout_seconds(single_attempt_timeout) * max_attempts
        + process_overhead,
    )


class _CancellableVisionProvider:
    """Run the blocking model request in a killable in-memory child process."""

    def __init__(self, cancel_check: Callable[[], bool] | None) -> None:
        self.cancel_check = cancel_check

    @staticmethod
    def _command() -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--vision-provider-worker"]
        return [
            sys.executable,
            "-m",
            "chejin_worker_client.vision_provider_worker",
        ]

    def understand(self, request: dict[str, Any]) -> dict[str, Any]:
        if _cancel_requested(self.cancel_check):
            raise VisionCancelledError("vision_cancelled_before_provider")
        image = request.get("image")
        image_bytes = getattr(image, "image_bytes", None)
        if not isinstance(image_bytes, (bytes, bytearray, memoryview)) or not image_bytes:
            raise ValueError("VISION_PROVIDER_IMAGE_INVALID")
        config = request.get("config")
        if not isinstance(config, dict):
            raise ValueError("VISION_PROVIDER_CONFIG_INVALID")
        settings = config.get("customer_image_understanding")
        timeout_seconds = _vision_timeout_seconds(
            (settings or {}).get("timeout_seconds")
            if isinstance(settings, dict)
            else None
        )
        payload = encode_subprocess_json(
            {
                "config": config,
                "customer_text": str(request.get("customer_text") or ""),
                "message_id": str(request.get("message_id") or ""),
                "mime_type": str(getattr(image, "mime_type", "") or "image/png"),
                "width": int(getattr(image, "width", 0) or 0),
                "height": int(getattr(image, "height", 0) or 0),
                "image_base64": base64.b64encode(bytes(image_bytes)).decode("ascii"),
                "protocol_unicode_sentinel": UNICODE_PROTOCOL_SENTINEL,
            }
        )
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": subprocess_utf8_environment(),
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        process = subprocess.Popen(self._command(), **popen_kwargs)
        completed: dict[str, str] = {}

        def communicate() -> None:
            stdout, stderr = process.communicate(input=payload)
            completed["stdout"] = stdout
            completed["stderr"] = stderr

        thread = threading.Thread(
            target=communicate,
            name="chejin-vision-provider-pipe",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + _vision_process_timeout_seconds(
            timeout_seconds
        )
        while thread.is_alive():
            if _cancel_requested(self.cancel_check):
                process.kill()
                thread.join(timeout=5.0)
                raise VisionCancelledError("vision_cancelled_during_provider")
            if time.monotonic() >= deadline:
                process.kill()
                thread.join(timeout=5.0)
                raise TimeoutError("VISION_PROVIDER_PROCESS_TIMEOUT")
            thread.join(timeout=0.15)
        try:
            envelope = json.loads(completed.get("stdout") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("VISION_PROVIDER_RESULT_INVALID") from exc
        if process.returncode != 0 or envelope.get("ok") is not True:
            error = RuntimeError(
                str(envelope.get("error_code") or "VISION_PROVIDER_WORKER_FAILED")
            )
            error.diagnostic_traceback = str(envelope.get("traceback") or "")  # type: ignore[attr-defined]
            raise error
        require_unicode_protocol(envelope)
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("VISION_PROVIDER_RESULT_INVALID")
        if _cancel_requested(self.cancel_check):
            raise VisionCancelledError("vision_cancelled_after_provider")
        return result


def _frame_fingerprint(image: Any) -> str:
    """Return a non-reversible visual fingerprint without persisting pixels."""

    sample = None
    try:
        sample = image.copy()
        sample.thumbnail((64, 64))
        seed = f"{getattr(sample, 'mode', '')}|{getattr(sample, 'size', '')}|".encode("utf-8")
        return hashlib.sha256(seed + sample.tobytes()).hexdigest()
    except Exception:
        return ""
    finally:
        close = getattr(sample, "close", None)
        if callable(close):
            close()


class _VisionHostState:
    def __init__(
        self,
        trace_id: str,
        *,
        window_context: dict[str, Any] | None = None,
        ocr_runner: CancellableOmniAutoOcr | None = None,
        artifact_dir: str | None = None,
    ) -> None:
        from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar

        self.host = wechat_win32_ocr_sidecar
        self.window_context = (
            dict(window_context)
            if isinstance(window_context, dict)
            else {}
        )
        self.hwnd = int(self.window_context.get("hwnd") or 0)
        self.window_context_validated = False
        self.trace_id = str(trace_id or "")
        self.started_at = time.perf_counter()
        self.events: list[dict[str, Any]] = []
        self.ocr_runner = ocr_runner
        self.artifact_dir = str(artifact_dir or "") or None

    def run_ocr(self, image: Any) -> list[dict[str, Any]]:
        if self.ocr_runner is not None:
            return self.ocr_runner.recognize(image)
        return self.host.run_ocr(image)

    def record(self, stage: str, status: str, *, started_at: float | None = None, **metadata: Any) -> None:
        event = {
            "sequence": len(self.events) + 1,
            "stage": str(stage),
            "status": str(status),
            "offset_ms": int((time.perf_counter() - self.started_at) * 1000),
        }
        if started_at is not None:
            event["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
        event.update({key: value for key, value in metadata.items() if value is not None})
        self.events.append(event)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "trace_id": self.trace_id,
            "total_duration_ms": int((time.perf_counter() - self.started_at) * 1000),
            "events": [dict(item) for item in self.events],
            "image_persisted": False,
        }

    def ensure_window(self) -> int:
        if not self.hwnd:
            raise RuntimeError("VISION_WINDOW_CONTEXT_MISSING")
        if self.window_context_validated:
            return self.hwnd
        validator = getattr(self.host, "validate_c2_window_context", None)
        if not callable(validator):
            raise RuntimeError("VISION_WINDOW_CONTEXT_VALIDATOR_MISSING")
        validation = validator(self.window_context)
        if not isinstance(validation, dict) or validation.get("ok") is not True:
            reason = (
                str((validation or {}).get("reason") or "")
                if isinstance(validation, dict)
                else ""
            )
            raise RuntimeError(
                f"VISION_WINDOW_CONTEXT_INVALID:{reason or 'unknown'}"
            )
        self.window_context_validated = True
        self.record(
            "window_context",
            "completed",
            hwnd=self.hwnd,
            source=str(self.window_context.get("source") or ""),
            reason=str(validation.get("reason") or ""),
        )
        return self.hwnd


class _ConversationTarget:
    def __init__(self, state: _VisionHostState) -> None:
        self.state = state

    def confirm_target(self, context: dict[str, Any]) -> dict[str, Any]:
        started_at = time.perf_counter()
        hwnd = self.state.ensure_window()
        target = str(context.get("remark_code") or context.get("target_name") or "").strip()
        if not target:
            self.state.record("target_confirmation", "failed", started_at=started_at, reason="vision_target_missing")
            return {"ok": False, "reason": "vision_target_missing"}
        candidate_frame = (
            context.get("candidate_frame")
            if isinstance(context.get("candidate_frame"), dict)
            else {}
        )
        try:
            validation = self.state.host.validate_active_send_target(
                hwnd,
                target,
                exact=False,
                artifact_dir=None,
                screenshot=candidate_frame.get("image"),
                ocr_items=candidate_frame.get("ocr_items"),
                screenshot_path="",
            )
            confirmed = self.state.host.c2_target_activation_confirmed(validation)
        except Exception as exc:
            self.state.record(
                "target_confirmation",
                "failed",
                started_at=started_at,
                reason="target_confirmation_exception",
                error_type=type(exc).__name__,
            )
            raise
        self.state.record(
            "target_confirmation",
            "completed" if confirmed else "failed",
            started_at=started_at,
            reason=str(validation.get("reason") or validation.get("state") or ""),
            frame_reused=bool(candidate_frame),
        )
        return {
            "ok": bool(confirmed),
            "reason": str(validation.get("reason") or validation.get("state") or ""),
            "remark_code": target,
            "conversation_type": str(validation.get("conversation_type") or ""),
            "frame_reused": bool(candidate_frame),
        }


class _WindowFrame:
    def __init__(self, state: _VisionHostState) -> None:
        self.state = state

    def capture_frame(self, context: dict[str, Any]) -> dict[str, Any]:
        started_at = time.perf_counter()
        phase = str(context.get("phase") or "image_candidate")
        if phase == "image_context_menu":
            hwnd = self.state.ensure_window()
            observation = self.state.host.observe_wechat_context_menu(
                hwnd,
                anchor_screen=list(context.get("menu_anchor_screen") or []),
                artifact_dir=self.state.artifact_dir,
                label="vision_image_context_menu",
                ocr_runner=self.state.run_ocr,
            )
            if observation.get("ok") is not True:
                self.state.record(
                    "frame_capture",
                    "failed",
                    started_at=started_at,
                    phase=phase,
                    capture_step="context_menu_observation",
                    reason=str(observation.get("reason") or "context_menu_observation_failed"),
                    error_type=str(observation.get("error_type") or ""),
                    image_persisted=bool(observation.get("screenshot_path")),
                )
                return observation
            self.state.record(
                "frame_capture",
                "completed",
                started_at=started_at,
                phase=phase,
                capture_step="context_menu_observation",
                capture_mode=str(observation.get("capture_mode") or "visible_screen"),
                image_size=list(observation.get("image_size") or []),
                ocr_item_count=int(observation.get("ocr_item_count") or 0),
                local_ocr_item_count=int(observation.get("local_ocr_item_count") or 0),
                ocr_roi=list(observation.get("ocr_roi") or []),
                menu_bounds=list(observation.get("menu_bounds") or []),
                menu_window_evidence=dict(
                    observation.get("menu_window_evidence") or {}
                ),
                ocr_execution=str(observation.get("ocr_execution") or ""),
                menu_structure_evidence=list(observation.get("menu_structure_evidence") or []),
                local_ocr_evidence=list(observation.get("local_ocr_evidence") or []),
                screenshot_path=str(observation.get("screenshot_path") or ""),
                roi_screenshot_path=str(observation.get("roi_screenshot_path") or ""),
                image_persisted=bool(
                    observation.get("screenshot_path")
                    or observation.get("roi_screenshot_path")
                ),
            )
            return {
                "ok": True,
                "image": observation.get("image"),
                "image_size": tuple(observation.get("image_size") or (0, 0)),
                "ocr_items": list(
                    observation.get("local_ocr_items") or []
                ),
                "messages": [],
                "time_markers": [],
                "screen_origin": [0, 0],
                "menu_bounds": list(observation.get("menu_bounds") or []),
                "screenshot_path": str(observation.get("screenshot_path") or ""),
                "roi_screenshot_path": str(
                    observation.get("roi_screenshot_path") or ""
                ),
            }
        capture_context = getattr(
            self.state.host,
            "capture_c2_window_context",
            None,
        )
        if not callable(capture_context):
            reason = "vision_window_context_capture_missing"
            self.state.record(
                "frame_capture",
                "failed",
                started_at=started_at,
                phase=phase,
                capture_step="window_context",
                reason=reason,
                image_persisted=False,
            )
            return {
                "ok": False,
                "reason": reason,
            }
        capture_result = capture_context(
            self.state.window_context,
            phase=phase,
            label=(
                "vision_image_context_menu"
                if phase == "image_context_menu"
                else "vision_image_candidate"
            ),
        )
        if (
            not isinstance(capture_result, dict)
            or capture_result.get("ok") is not True
        ):
            raw_reason = str(
                (capture_result or {}).get("reason")
                or "vision_window_capture_failed"
            )
            reason, reason_detail = _normalize_window_frame_failure_reason(
                raw_reason,
                "vision_window_capture_failed",
            )
            capture_mode = str(
                (capture_result or {}).get("capture_mode") or ""
            )
            self.state.record(
                "frame_capture",
                "failed",
                started_at=started_at,
                phase=phase,
                capture_step=(
                    "window_context"
                    if reason.startswith("vision_window_context")
                    else "window_capture"
                ),
                capture_mode=capture_mode,
                reason=reason,
                reason_detail=reason_detail,
                error_type=str(
                    (capture_result or {}).get("error_type") or ""
                ),
                image_persisted=False,
            )
            return {
                "ok": False,
                "reason": reason,
                "reason_detail": reason_detail,
                "error_type": str(
                    (capture_result or {}).get("error_type") or ""
                ),
            }
        image = capture_result.get("image")
        hwnd = int(capture_result.get("hwnd") or 0)
        capture_mode = str(capture_result.get("capture_mode") or "")
        screen_origin = list(capture_result.get("screen_origin") or [0, 0])
        if len(screen_origin) < 2:
            screen_origin = [0, 0]
        screen_origin = [int(screen_origin[0]), int(screen_origin[1])]
        validation = (
            capture_result.get("validation")
            if isinstance(capture_result.get("validation"), dict)
            else {}
        )
        if not self.state.window_context_validated:
            self.state.window_context_validated = True
            self.state.record(
                "window_context",
                "completed",
                hwnd=hwnd,
                source=str(self.state.window_context.get("source") or ""),
                reason=str(validation.get("reason") or ""),
            )
        try:
            state_ocr = getattr(self.state, "run_ocr", None)
            ocr_items = (
                state_ocr(image)
                if callable(state_ocr)
                else self.state.host.run_ocr(image)
            )
        except Exception as exc:
            raw_reason = _safe_exception_reason(
                exc,
                "vision_window_ocr_failed",
            )
            reason, reason_detail = _normalize_window_frame_failure_reason(
                raw_reason,
                "vision_window_ocr_failed",
            )
            self.state.record(
                "frame_capture",
                "failed",
                started_at=started_at,
                phase=phase,
                capture_step="ocr",
                capture_mode=capture_mode,
                reason=reason,
                reason_detail=reason_detail,
                error_type=type(exc).__name__,
                image_persisted=False,
            )
            image.close()
            return {
                "ok": False,
                "reason": reason,
                "reason_detail": reason_detail,
                "error_type": type(exc).__name__,
            }
        messages: list[dict[str, Any]] = []
        time_markers: list[dict[str, Any]] = []
        try:
            messages = self.state.host.parse_messages_from_ocr(
                ocr_items,
                image.size,
                target=str(context.get("remark_code") or context.get("target_name") or ""),
                screenshot=image,
            )
            from apps.wechat_ai_customer_service.optional_plugins.vision.capture.surface import (
                observe_structural_image_messages,
            )
            from apps.wechat_ai_customer_service.optional_plugins.vision.capture.wechat import (
                extract_chat_time_markers,
            )
            image_messages = observe_structural_image_messages(
                image,
                ocr_items,
                messages,
                target=str(
                    context.get("remark_code")
                    or context.get("target_name")
                    or ""
                ),
                role_resolver=(
                    self.state.host.message_row_avatar_role_details
                ),
                max_images=max(
                    1,
                    int(
                        context.get("max_images")
                        or (
                            image_contract().get("source_limits")
                            or {}
                        ).get("max_visible_image_candidates")
                        or 64
                    ),
                ),
            )
            messages.extend(image_messages)

            def message_visual_top(item: dict[str, Any]) -> float:
                rect = item.get("bubble_rect")
                try:
                    return float(
                        rect.get("top")
                        if isinstance(rect, dict)
                        else rect[1]
                    )
                except (TypeError, ValueError, IndexError):
                    return 0.0

            messages.sort(
                key=lambda item: (
                    message_visual_top(item),
                    str(item.get("id") or ""),
                )
            )
            time_markers = extract_chat_time_markers(ocr_items, image.size)
        except Exception as exc:
            reason = "vision_window_message_parse_failed"
            reason_detail = _safe_exception_reason(exc, reason)
            self.state.record(
                "frame_capture",
                "failed",
                started_at=started_at,
                phase=phase,
                capture_step="message_parse",
                capture_mode=capture_mode,
                reason=reason,
                reason_detail=reason_detail,
                error_type=type(exc).__name__,
                image_persisted=False,
            )
            image.close()
            return {
                "ok": False,
                "reason": reason,
                "reason_detail": reason_detail,
                "error_type": type(exc).__name__,
            }
        try:
            self.state.record(
                "frame_capture",
                "completed",
                started_at=started_at,
                phase=phase,
                capture_step="completed",
                capture_mode=capture_mode,
                frame_fingerprint=_frame_fingerprint(image),
                image_size=[int(image.size[0]), int(image.size[1])],
                ocr_item_count=len(ocr_items),
                parsed_message_count=len(messages),
                image_persisted=False,
            )
            return {
                "ok": True,
                "image": image,
                "image_size": image.size,
                "ocr_items": ocr_items,
                "messages": messages,
                "time_markers": time_markers,
                "screen_origin": screen_origin,
            }
        except Exception as exc:
            reason = "vision_window_frame_finalize_failed"
            reason_detail = _safe_exception_reason(exc, reason)
            self.state.record(
                "frame_capture",
                "failed",
                started_at=started_at,
                phase=phase,
                capture_step="frame_finalize",
                capture_mode=capture_mode,
                reason=reason,
                reason_detail=reason_detail,
                error_type=type(exc).__name__,
                image_persisted=False,
            )
            image.close()
            return {
                "ok": False,
                "reason": reason,
                "reason_detail": reason_detail,
                "error_type": type(exc).__name__,
            }


class _UiAction:
    def __init__(self, state: _VisionHostState) -> None:
        self.state = state

    def right_click(
        self,
        x: int,
        y: int,
        *,
        bounds: list[int],
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        hwnd = self.state.ensure_window()
        current_bounds = [int(value) for value in list(bounds or [])[:4]]
        if (
            len(current_bounds) != 4
            or current_bounds[2] <= current_bounds[0]
            or current_bounds[3] <= current_bounds[1]
        ):
            raise RuntimeError("IMAGE_CURRENT_BUBBLE_BOUNDS_INVALID")
        result = self.state.host.human_window_image_right_click_in_bounds(
            hwnd,
            int(x),
            int(y),
            bounds=current_bounds,
            action_name="c2_vision_image_slot_context_right_click",
        )
        if not result.get("ok"):
            self.state.record(
                "context_right_click",
                "failed",
                started_at=started_at,
                point=[int(x), int(y)],
                bounds=current_bounds,
            )
            raise RuntimeError("IMAGE_CONTEXT_RIGHT_CLICK_FAILED")
        self.state.record(
            "context_right_click",
            "completed",
            started_at=started_at,
            point=[int(x), int(y)],
            bounds=current_bounds,
        )
        wait_for_menu = getattr(
            self.state.host,
            "wait_for_wechat_context_menu_stable",
            None,
        )
        if not callable(wait_for_menu):
            raise RuntimeError("WECHAT_CONTEXT_MENU_WAIT_UNAVAILABLE")
        menu_wait_ms = int(wait_for_menu())
        self.state.record(
            "context_menu_stable_wait",
            "completed",
            menu_wait_ms=menu_wait_ms,
        )
        return dict(result)

    def click_screen(self, x: int, y: int, *, bounds: list[int]) -> None:
        started_at = time.perf_counter()
        self.state.ensure_window()
        result = self.state.host.human_screen_click_in_bounds(
            int(x),
            int(y),
            bounds=[int(value) for value in bounds[:4]],
            action_name="c2_vision_image_copy_menu_click",
        )
        if not result.get("ok"):
            self.state.record(
                "copy_menu_click",
                "failed",
                started_at=started_at,
                point=[int(x), int(y)],
                bounds=bounds,
            )
            raise RuntimeError("IMAGE_COPY_MENU_CLICK_FAILED")
        self.state.record(
            "copy_menu_click",
            "completed",
            started_at=started_at,
            point=[int(x), int(y)],
            bounds=bounds,
        )
        self.state.host.humanized_action_sleep(100, 220)

    def dismiss_menu_safely(self) -> None:
        started_at = time.perf_counter()
        hwnd = self.state.ensure_window()
        result = self.state.host.dismiss_voice_transcribe_context_menu(
            hwnd,
            artifact_dir=None,
            label="c2_vision_image_menu_dismissed",
        )
        self.state.record(
            "context_menu_dismiss",
            "completed" if not isinstance(result, dict) or result.get("ok", True) else "failed",
            started_at=started_at,
        )


class _Clipboard:
    def __init__(self, state: _VisionHostState) -> None:
        self.state = state

    def sequence_number(self) -> int | None:
        started_at = time.perf_counter()
        from apps.wechat_ai_customer_service.optional_plugins.vision.clipboard_payload import (
            windows_clipboard_sequence_number,
        )

        value = windows_clipboard_sequence_number()
        self.state.record(
            "clipboard_sequence",
            "completed" if value is not None else "failed",
            started_at=started_at,
            sequence_number=value,
        )
        return value

    def read_current_bitmap(self) -> Any:
        started_at = time.perf_counter()
        from apps.wechat_ai_customer_service.optional_plugins.vision import clipboard_payload

        result = clipboard_payload._read_current_windows_native_clipboard_image(
            source_limits=(
                image_contract().get("source_limits")
                if isinstance(image_contract(), dict)
                else {}
            ),
        )
        self.state.record(
            "clipboard_bitmap_read",
            "completed" if isinstance(result, dict) and result.get("ok") else "failed",
            started_at=started_at,
            reason=str(result.get("reason") or "") if isinstance(result, dict) else "clipboard_result_invalid",
            image_bytes_persisted=False,
        )
        return result

    def clear_current(self, expected_sequence: int) -> dict[str, Any]:
        started_at = time.perf_counter()
        from apps.wechat_ai_customer_service.optional_plugins.vision.clipboard_payload import (
            clear_current_windows_clipboard_image,
        )

        result = clear_current_windows_clipboard_image(
            int(expected_sequence)
        )
        self.state.record(
            "clipboard_clear",
            "completed" if result.get("ok") else "failed",
            started_at=started_at,
            reason=str(result.get("reason") or ""),
            image_bytes_persisted=False,
        )
        return result


class _ExistingWorkerLease:
    """Vision runs while TaskRunner already owns and renews the single UI lock."""

    @staticmethod
    def lease(action: str, *, timeout_seconds: float) -> Any:
        del action, timeout_seconds
        return nullcontext({"acquired": True, "source": "chejin_worker_ui_lease"})


def _rect(value: Any) -> list[int]:
    if isinstance(value, dict):
        raw = [value.get("left"), value.get("top"), value.get("right"), value.get("bottom")]
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = list(value[:4])
    else:
        return []
    try:
        result = [int(round(float(item))) for item in raw]
    except (TypeError, ValueError):
        return []
    return result if result[2] > result[0] and result[3] > result[1] else []


def explicit_vision_config() -> tuple[dict[str, Any] | None, list[str]]:
    provider_contract = image_contract().get("provider_contract") or {}
    required_api_key_env = str(
        provider_contract.get("api_key_env")
        or "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
    ).strip()
    configured = install_resolved_vision_api_key()
    api_key_env = required_api_key_env if configured else ""
    if not api_key_env:
        return None, [required_api_key_env]
    runtime_settings = resolve_vision_runtime_settings()
    return {
        "image_contract": image_contract(),
        "customer_image_understanding": {
            "enabled": True,
            "provider": runtime_settings["provider"],
            "base_url": runtime_settings["base_url"],
            "model": runtime_settings["model"],
            "request_style": runtime_settings["request_style"],
            "api_key_env": api_key_env,
            "timeout_seconds": _vision_timeout_seconds(),
        }
    }, []


def _vision_configuration_errors(
    values: dict[str, Any],
) -> list[str]:
    contract = image_contract().get("provider_contract") or {}
    parsed = urlparse(str(values.get("base_url") or ""))
    host = str(parsed.hostname or "").lower()
    scheme = str(parsed.scheme or "").lower()
    development_hosts = {
        str(item).lower()
        for item in contract.get("development_http_hosts") or []
    }
    runtime_mode = str(
        os.environ.get("CHEJIN_RPA_MODE") or ""
    ).strip().lower()
    development_mode = runtime_mode in {
        str(item).strip().lower()
        for item in contract.get("development_modes") or []
    }
    errors: list[str] = []
    if scheme != "https" and not (
        development_mode
        and scheme == "http"
        and host in development_hosts
    ):
        errors.append("CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL_HTTPS_REQUIRED")
    allowed = contract.get("allowed_combinations") or []
    matched = any(
        isinstance(item, dict)
        and str(item.get("provider") or "")
        == str(values.get("provider") or "")
        and str(item.get("host") or "").lower() == host
        and str(item.get("request_style") or "")
        == str(values.get("request_style") or "")
        and str(values.get("model") or "")
        in {
            str(model)
            for model in item.get("models") or []
        }
        for item in allowed
    )
    if not matched:
        errors.append(
            "CUSTOMER_IMAGE_UNDERSTANDING_PROVIDER_COMBINATION_INVALID"
        )
    return errors


def vision_configuration_status() -> dict[str, Any]:
    """Validate Vision capability before any WeChat image UI action."""

    config, missing = explicit_vision_config()
    settings = (
        config.get("customer_image_understanding")
        if isinstance(config, dict) and isinstance(config.get("customer_image_understanding"), dict)
        else {}
    )
    runtime_settings = resolve_vision_runtime_settings()
    api_key_env = str(settings.get("api_key_env") or "").strip()
    required_values = {
        "provider": str(settings.get("provider") or runtime_settings["provider"]).strip(),
        "base_url": str(settings.get("base_url") or runtime_settings["base_url"]).strip(),
        "model": str(settings.get("model") or runtime_settings["model"]).strip(),
        "request_style": str(settings.get("request_style") or runtime_settings["request_style"]).strip(),
        "api_key_env": str(settings.get("api_key_env") or api_key_env).strip(),
        "timeout_seconds": _vision_timeout_seconds(settings.get("timeout_seconds")),
    }
    missing_fields = list(missing)
    missing_fields.extend(
        name.upper()
        for name, value in required_values.items()
        if not value
        and name != "api_key_env"
        and name.upper() not in missing_fields
    )
    missing_fields.extend(
        error
        for error in _vision_configuration_errors(required_values)
        if error not in missing_fields
    )
    return {
        "ready": bool(config) and not missing_fields,
        "missing_configuration": missing_fields,
        "provider": required_values["provider"],
        "base_url": required_values["base_url"],
        "model": required_values["model"],
        "request_style": required_values["request_style"],
        "timeout_seconds": required_values["timeout_seconds"],
        "config": config if config and not missing_fields else None,
    }


def process_image_slot(
    *,
    observation: dict[str, Any],
    remark_code: str,
    session_key: str,
    window_context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    trace_id: str = "",
    cancel_check: Callable[[], bool] | None = None,
    action_journal_path: str | Path | None = None,
    source_message_key: str = "",
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Run one authorized image slot through OmniAuto Vision in memory."""

    resolved_trace_id = str(trace_id or observation.get("observation_id") or "")
    normalized_source_key = str(source_message_key or "").strip()

    def journal_update(
        *,
        action_phase: str,
        business_state: str | None,
        business_result_confirmed: bool,
    ) -> None:
        if action_journal_path is None or not normalized_source_key:
            return
        update_action_journal_item(
            action_journal_path,
            source_message_key=normalized_source_key,
            action_phase=action_phase,
            business_state=business_state,
            business_result_confirmed=business_result_confirmed,
        )

    def finish_result(result: dict[str, Any]) -> dict[str, Any]:
        state = str(result.get("state") or "").strip()
        completed = state == "completed"
        result["business_state"] = (
            "completed" if completed else "failed"
        )
        result["business_result_confirmed"] = completed
        if action_journal_path is None or not normalized_source_key:
            return result
        phase = str(
            result.get("action_phase")
            or action_journal_phase(action_journal_path)
            or "not_attempted"
        ).strip()
        result["action_phase"] = phase
        if phase != "not_attempted":
            terminal_payload = {
                "state": state or "failed",
                "reason": str(result.get("reason") or ""),
                "customer_image_understanding": (
                    dict(result.get("customer_image_understanding") or {})
                    if isinstance(
                        result.get("customer_image_understanding"), dict
                    )
                    else None
                ),
                "visual_bridge_input": (
                    result.get("visual_bridge_input")
                    if isinstance(
                        result.get("visual_bridge_input"),
                        (dict, list, str),
                    )
                    else None
                ),
            }
            update_action_journal_item(
                action_journal_path,
                source_message_key=normalized_source_key,
                action_phase=phase,
                business_state=result["business_state"],
                business_result_confirmed=result[
                    "business_result_confirmed"
                ],
                error_code=None if completed else str(
                    result.get("reason") or "IMAGE_RESULT_UNCONFIRMED"
                ),
                terminal_payload=terminal_payload,
            )
        return result

    def early_result(state: str, reason: str, **extra: Any) -> dict[str, Any]:
        event = {
            "sequence": 1,
            "stage": "vision_preflight",
            "status": state,
            "offset_ms": 0,
            "reason": reason,
            "image_persisted": False,
        }
        return {
            "state": state,
            "reason": reason,
            "action_phase": "not_attempted",
            **extra,
            "diagnostics": {
                "schema_version": 1,
                "trace_id": resolved_trace_id,
                "total_duration_ms": 0,
                "events": [event],
                "image_persisted": False,
            },
        }

    role = str(observation.get("sender_role") or "").strip().lower()
    role_source = str(
        observation.get("sender_role_source") or ""
    ).strip().lower()
    bubble_rect = _rect(observation.get("bubble_rect"))
    source_message = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    if not observation_role_is_trusted(observation):
        return early_result(
            "unconfirmed",
            "MESSAGE_IDENTITY_UNCONFIRMED",
        )
    image_physical_anchor = observation.get("image_physical_anchor")
    if not isinstance(image_physical_anchor, dict):
        image_physical_anchor = source_message.get("image_physical_anchor")
    if not bubble_rect:
        return early_result("failed", "image_bubble_rect_missing")
    if _cancel_requested(cancel_check):
        return early_result("cancelled", "vision_cancelled_before_start")
    runtime_config = dict(config or {})
    if not runtime_config:
        configuration = vision_configuration_status()
        configured = configuration.get("config")
        if not isinstance(configured, dict):
            return early_result(
                "failed",
                "vision_configuration_incomplete",
                missing_configuration=list(configuration.get("missing_configuration") or []),
            )
        runtime_config = configured
    runtime_config.setdefault(
        "image_contract",
        image_contract(),
    )
    vision_settings = runtime_config.get(
        "customer_image_understanding"
    )
    if not isinstance(vision_settings, dict):
        vision_settings = {}
    if is_official_vision_runtime():
        if not install_resolved_vision_api_key():
            return early_result(
                "failed",
                "vision_configuration_incomplete",
                missing_configuration=[VISION_API_KEY_ENV],
            )
        vision_settings = {
            **vision_settings,
            **resolve_vision_runtime_settings(),
            "api_key_env": VISION_API_KEY_ENV,
        }
    vision_settings = {
        "enabled": True,
        "provider": str(
            vision_settings.get("provider")
            or DEFAULT_VISION_PROVIDER
        ).strip(),
        "base_url": str(
            vision_settings.get("base_url")
            or DEFAULT_VISION_BASE_URL
        ).strip(),
        "model": str(
            vision_settings.get("model")
            or DEFAULT_VISION_MODEL
        ).strip(),
        "request_style": str(
            vision_settings.get("request_style")
            or DEFAULT_VISION_REQUEST_STYLE
        ).strip(),
        "timeout_seconds": _vision_timeout_seconds(
            vision_settings.get("timeout_seconds")
        ),
        **{
            key: value
            for key, value in vision_settings.items()
            if key not in {
                "enabled",
                "provider",
                "base_url",
                "model",
                "request_style",
                "timeout_seconds",
            }
        },
    }
    runtime_config["customer_image_understanding"] = vision_settings
    config_errors = _vision_configuration_errors(vision_settings)
    if config_errors:
        return early_result(
            "failed",
            "vision_configuration_invalid",
            missing_configuration=config_errors,
        )
    if (
        not isinstance(window_context, dict)
        or _window_context_hwnd(window_context) <= 0
        or str(window_context.get("source") or "")
        != "sidecar_selected_main_window"
    ):
        return early_result(
            "failed",
            "vision_window_context_missing",
        )
    if not isinstance(image_physical_anchor, dict) or not str(
        image_physical_anchor.get("bubble_visual_fingerprint") or ""
    ).strip():
        return early_result(
            "failed",
            "C2_IMAGE_SLOT_RECONFIRM_FAILED",
        )

    from apps.wechat_ai_customer_service.optional_plugins.vision.plugin import BuiltinVisionPlugin
    from apps.wechat_ai_customer_service.optional_plugins.vision.ports import VisionHostPorts

    ocr_runner = (
        CancellableOmniAutoOcr(cancel_check)
        if sys.platform == "win32"
        else None
    )
    state = _VisionHostState(
        resolved_trace_id,
        window_context=window_context,
        ocr_runner=ocr_runner,
        artifact_dir=artifact_dir,
    )
    vision_settings = runtime_config.get("customer_image_understanding")
    if not isinstance(vision_settings, dict):
        vision_settings = {}
    runtime_config["_chejin_c2_strict_adapter"] = True
    state.record(
        "vision_preflight",
        "completed",
        provider=str(vision_settings.get("provider") or ""),
        base_url=str(vision_settings.get("base_url") or ""),
        model=str(vision_settings.get("model") or ""),
        request_style=str(vision_settings.get("request_style") or ""),
        role=role,
        role_source=role_source,
        bubble_rect=bubble_rect,
        image_physical_anchor=image_physical_anchor,
        image_persisted=False,
    )
    ports = VisionHostPorts(
        rpa_lease=_ExistingWorkerLease(),
        conversation_target=_ConversationTarget(state),
        window_frame=_WindowFrame(state),
        ui_action=_UiAction(state),
        clipboard=_Clipboard(state),
        vision_provider=_CancellableVisionProvider(cancel_check),
    )
    plugin = BuiltinVisionPlugin(ports=ports, config=runtime_config)
    plugin_started_at = time.perf_counter()
    try:
        result = plugin.run(
            {
                "remark_code": remark_code,
                "target_name": remark_code,
                "session_key": session_key,
                "conversation_type": "private",
                "sender_role": role,
                "side_filter": "all",
                "bubble_rect": bubble_rect,
                "image_physical_anchor": dict(image_physical_anchor),
                "message_id": str(observation.get("observation_id") or ""),
                "customer_text": "客户发送了一张图片" if role == "customer" else "销售发送了一张图片",
                "config": runtime_config,
                "cancel_check": cancel_check,
                "action_journal_update": journal_update,
            }
        )
    except (VisionCancelledError, OmniAutoOcrCancelledError):
        state.record(
            "vision_provider",
            "cancelled",
            started_at=plugin_started_at,
            reason="vision_cancelled",
            image_persisted=False,
        )
        return finish_result({
            "state": "cancelled",
            "reason": "vision_cancelled",
            "action_phase": (
                action_journal_phase(action_journal_path)
                if action_journal_path is not None
                else "not_attempted"
            ),
            "diagnostics": state.diagnostics(),
        })
    except Exception as exc:
        provider_traceback = str(
            getattr(exc, "diagnostic_traceback", "") or ""
        )
        state.record(
            "vision_provider",
            "failed",
            started_at=plugin_started_at,
            reason="vision_plugin_exception",
            error_type=type(exc).__name__,
            provider_traceback=provider_traceback,
            image_persisted=False,
        )
        return finish_result({
            "state": "failed",
            "reason": "vision_plugin_exception",
            "error_type": type(exc).__name__,
            "diagnostics": state.diagnostics(),
        })
    finally:
        if ocr_runner is not None:
            ocr_runner.close()
    if _cancel_requested(cancel_check):
        transaction = dict(result.get("clipboard_transaction") or {})
        state.record(
            "vision_provider",
            "cancelled",
            started_at=plugin_started_at,
            reason="vision_cancelled_after_provider",
            image_persisted=False,
        )
        return finish_result({
            "state": "cancelled",
            "reason": "vision_cancelled_after_provider",
            "action_phase": str(
                transaction.get("action_phase") or "not_attempted"
            ),
            "transaction": transaction,
            "diagnostics": state.diagnostics(),
        })
    if not isinstance(result, dict):
        state.record(
            "vision_provider",
            "failed",
            started_at=plugin_started_at,
            reason="vision_result_invalid",
            image_persisted=False,
        )
        return finish_result({
            "state": "failed",
            "reason": "vision_result_invalid",
            "diagnostics": state.diagnostics(),
        })
    understanding = result.get("customer_image_understanding")
    bridge = result.get("visual_bridge_input")
    if not isinstance(understanding, dict):
        reason = str(result.get("reason") or "vision_understanding_missing")
        transaction = dict(result.get("clipboard_transaction") or {})
        acquisition_state = str(result.get("acquisition_state") or "")
        state.record(
            "vision_provider",
            "failed",
            started_at=plugin_started_at,
            reason=reason,
            image_persisted=False,
        )
        return finish_result({
            "state": (
                "not_visible"
                if acquisition_state == "image_not_visible"
                else "failed"
            ),
            "reason": reason,
            "action_phase": str(
                transaction.get("action_phase") or "not_attempted"
            ),
            "transaction": transaction,
            "diagnostics": state.diagnostics(),
        })
    understanding_errors = validate_image_result_schema(
        understanding,
        "customer_image_understanding_v1",
    )
    bridge_errors = validate_image_result_schema(
        bridge,
        "visual_bridge_input_v1",
    )
    if understanding_errors or bridge_errors:
        return finish_result({
            "state": "failed",
            "reason": "C2_IMAGE_UNDERSTANDING_SCHEMA_INVALID",
            "action_phase": str(
                (result.get("clipboard_transaction") or {}).get(
                    "action_phase"
                )
                or "not_attempted"
            ),
            "transaction": dict(
                result.get("clipboard_transaction") or {}
            ),
            "diagnostics": state.diagnostics(),
        })
    vision_summary = str(understanding.get("vision_summary") or "").strip()
    completed = result.get("applied") is True
    state.record(
        "vision_provider",
        "completed" if completed else "failed",
        started_at=plugin_started_at,
        reason=str(result.get("reason") or understanding.get("reason") or ""),
        applied=bool(result.get("applied")),
        vision_summary_length=len(vision_summary),
        vision_summary_sha256=hashlib.sha256(vision_summary.encode("utf-8")).hexdigest() if vision_summary else "",
        image_persisted=False,
    )
    return finish_result({
        "state": "completed" if completed else "failed",
        "reason": str(result.get("reason") or understanding.get("reason") or ""),
        "customer_image_understanding": dict(understanding),
        "visual_bridge_input": bridge if isinstance(bridge, (dict, list, str)) else {},
        "transaction": dict(result.get("clipboard_transaction") or {}),
        "action_phase": str(
            (result.get("clipboard_transaction") or {}).get("action_phase")
            or "not_attempted"
        ),
        "diagnostics": state.diagnostics(),
    })
