from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG
from .c2_contract import c2_contract_v3
from .update_filesystem import update_filesystem_path
from apps.wechat_ai_customer_service.adapters import send_request_file, send_setup_contract, send_launch_journal
from .emergency_stop import emergency_stop_requested
from .artifact_retention import record_artifact_outcome
from .failure_evidence import exception_details, mark_failure_recovered, record_capture_failure, record_failure
from .action_journal import (
    action_journal_path,
    action_journal_phase,
    initialize_action_journal,
    read_action_journal,
)
from .models import RpaResult, RpaStep, Task


CLIENT_ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ADD_FRIEND_ACTION = "add-friend-entry-click-plan-windows"
CancellationCheck = Callable[[], bool | str]


def startup_probe_geometry(payload: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    """Expose the authoritative post-normalization WeChat rectangle to the UI.

    ``status`` historically returned ``geometry`` at the top level.  The
    v0.9.35 startup path intentionally reuses the successful
    ``normalize-window`` observation, whose final rectangle is nested under
    ``window_normalization.after``.  Keep one stable Worker-facing contract
    without taking another screenshot or running OCR.
    """

    source = payload if isinstance(payload, dict) else {}
    candidates = (
        (source.get("geometry"), "status.geometry"),
        (
            (
                source.get("window_normalization", {}).get("after")
                if isinstance(source.get("window_normalization"), dict)
                else None
            ),
            "window_normalization.after",
        ),
    )
    required = ("left", "top", "right", "bottom", "width", "height")
    for candidate, origin in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            geometry = {key: int(candidate[key]) for key in required}
        except (KeyError, TypeError, ValueError):
            continue
        if geometry["width"] <= 0 or geometry["height"] <= 0:
            continue
        return geometry, origin
    return {}, ""


def default_sidecar_script() -> Path:
    configured = os.environ.get("CHEJIN_OMNIAUTO_SIDECAR")
    if configured:
        return Path(configured)
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    candidates = [
        CLIENT_ROOT / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py",
        CLIENT_ROOT.parent / "omniauto" / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py",
        CLIENT_ROOT.parent / "omniauto" / "omniauto" / "omniauto" / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py",
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


class RpaBridge:
    def __init__(self, sidecar_script: Path | None = None) -> None:
        self.sidecar_script = sidecar_script or default_sidecar_script()
        self.mode = CONFIG.rpa_mode
        self._active_artifact_dirs: set[Path] = set()
        self._active_artifact_dirs_lock = threading.Lock()
        self._active_process_count = 0
        self._active_process_count_lock = threading.Lock()
        self.last_probe_payload: dict[str, Any] = {}
        self._startup_window_normalization_state = "pending"
        self.last_startup_window_normalization: dict[str, Any] = {}

    def probe(self) -> tuple[str, str]:
        if self.mode == "mock":
            self.last_probe_payload = {}
            return "ready", "logged_in"
        if sys.platform != "win32":
            self.last_probe_payload = {}
            return "unavailable", "unknown"
        startup_normalization: dict[str, Any] = {}
        if self._startup_window_normalization_state == "pending":
            startup_normalization = dict(
                self._call_omniauto(["normalize-window"], timeout=60)
            )
            self.last_startup_window_normalization = startup_normalization
            if startup_normalization.get("ok"):
                self._startup_window_normalization_state = "completed"
            elif (
                str(startup_normalization.get("error_code") or "")
                != "WECHAT_WINDOW_NOT_FOUND"
            ):
                # A failed post-move verification may mean the window was
                # already changed. Do not move it again on every heartbeat.
                self._startup_window_normalization_state = "failed_locked"
        # The successful startup calibration frame already proves the main
        # shell and login state. Reuse that observation instead of capturing
        # the unchanged window again through status.
        payload = (
            dict(startup_normalization)
            if startup_normalization.get("ok")
            else self._call_omniauto(["status"], timeout=30)
        )
        if startup_normalization:
            payload = {
                **dict(payload),
                "startup_window_normalization": startup_normalization,
            }
        elif self._startup_window_normalization_state == "failed_locked":
            payload = {
                **dict(payload),
                "startup_window_normalization": dict(
                    self.last_startup_window_normalization
                ),
            }
        payload = {
            **dict(payload),
            "startup_window_normalization_state": (
                self._startup_window_normalization_state
            ),
        }
        geometry, geometry_source = startup_probe_geometry(payload)
        if geometry:
            payload = {
                **payload,
                "geometry": geometry,
                "geometry_source": geometry_source,
            }
        self.last_probe_payload = dict(payload)
        if startup_normalization and not startup_normalization.get("ok"):
            if str(startup_normalization.get("error_code") or "") == "WECHAT_WINDOW_NOT_FOUND":
                return "ready", "not_found"
            return "unavailable", "unknown"
        if self._startup_window_normalization_state == "failed_locked":
            return "unavailable", "unknown"
        if payload.get("ok"):
            return "ready", "logged_in"
        error_code = str(payload.get("error_code") or "")
        if error_code == "WECHAT_WINDOW_NOT_FOUND":
            return "ready", "not_found"
        return "unavailable", "unknown"

    def diagnose_wechat(self) -> dict:
        if self.mode == "mock":
            return {"ok": True, "mode": "mock", "message": "mock RPA 模式不探测真实微信。"}
        return self._call_omniauto(["status"], timeout=60)

    def prepare_startup_layout_for_new_transaction(self) -> dict[str, Any]:
        """Leave startup-map ownership checks to the real Sidecar entry.

        The active Sidecar action selects the current visible WeChat HWND and
        compares only that identity with the persisted map immediately before
        consuming coordinates.  Worker must not add a second geometry/DPI
        state machine in front of every C0-C4 transaction.
        """
        if self.mode == "mock":
            return {"ok": True, "mode": "mock", "state": "mock_calibration_current"}
        return {
            "ok": True,
            "state": "startup_layout_binding_deferred_to_business_entry",
            "calibration_status_checked": False,
            "geometry_gate_added": False,
            "automatic_window_move_attempted": False,
            "automatic_recalibration_attempted": False,
        }

    def verify_startup_layout_for_inflight_transaction(self) -> dict[str, Any]:
        """Do not add a geometry/DPI gate to a recovered in-flight flow."""
        if self.mode == "mock":
            return {"ok": True, "mode": "mock", "state": "mock_calibration_current"}
        return {
            "ok": True,
            "state": "startup_layout_binding_deferred_to_business_entry",
            "calibration_status_checked": False,
            "geometry_gate_added": False,
            "automatic_window_move_attempted": False,
            "automatic_recalibration_attempted": False,
        }

    def list_sessions(
        self,
        *,
        artifact_dir: Path | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": f"mock-session-{uuid.uuid4()}",
                "artifact_dir": str(artifact_dir) if artifact_dir else "",
                "sessions": [],
            }
        resolved_artifact_dir = artifact_dir or CONFIG.app_dir / "artifacts" / "wechat_c2" / "sessions" / time.strftime("%Y%m%d_%H%M%S")
        resolved_artifact_dir.mkdir(parents=True, exist_ok=True)
        sidecar_run_id = f"sessions-{uuid.uuid4().hex}"
        scan_id = f"scan-{uuid.uuid4()}"
        payload = self._call_omniauto(
            [
                "sessions",
                "--sidecar-run-id",
                sidecar_run_id,
                "--scan-id",
                scan_id,
                "--artifact-dir",
                str(resolved_artifact_dir),
            ],
            timeout=60,
            cancel_check=cancel_check,
        )
        payload.setdefault("sidecar_run_id", sidecar_run_id)
        payload.setdefault("scan_id", scan_id)
        payload.setdefault("artifact_dir", str(resolved_artifact_dir))
        if not payload.get("screenshot_path"):
            screenshots = sorted(
                resolved_artifact_dir.glob("*.png"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if screenshots:
                payload["screenshot_path"] = str(screenshots[0])
        return payload

    def get_messages(
        self,
        *,
        display_name: str,
        rpa_session_key: str,
        remark_code: str = "",
        target_mode: str = "",
        expected_confirmed_self_text: str = "",
        chat_fact_roi_ocr: bool = False,
        same_frame_full_ocr_evidence: dict[str, Any] | None = None,
        text_recheck_capture: bool = False,
        history_mode: str = "",
        anchor_ids: list[str] | None = None,
        anchor_content_keys: list[str] | None = None,
        reply_content_keys: list[str] | None = None,
        max_scroll_steps: int = 0,
        max_snapshots: int = 1,
        restore_to_latest: bool = True,
        max_duration_seconds: int = 12,
        input_safety_observation: bool = False,
        input_safety_request_id: str = "",
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if input_safety_observation and (
            target_mode != "current" or not remark_code or not input_safety_request_id
            or max_scroll_steps != 0 or max_snapshots != 1 or restore_to_latest
            or history_mode or anchor_ids or anchor_content_keys or reply_content_keys
            or expected_confirmed_self_text or chat_fact_roi_ocr
            or same_frame_full_ocr_evidence or text_recheck_capture
        ):
            raise ValueError("INPUT_SAFETY_OBSERVATION_ARGUMENT_CONFLICT")
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "messages_mock",
                "sidecar_run_id": f"mock-message-{uuid.uuid4()}",
                "target_mode": target_mode or "visible",
                "remark_code": remark_code,
                "messages": [],
            }
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        sidecar_run_id = f"message-{timestamp}-{uuid.uuid4().hex[:8]}"
        replay_evidence = (
            dict(same_frame_full_ocr_evidence)
            if isinstance(same_frame_full_ocr_evidence, dict)
            and CONFIG.c3_pre_send_roi_reuse_enabled
            else {}
        )
        replay_path = Path(str(replay_evidence.get("screenshot_path") or ""))
        artifact_dir = (
            replay_path.parent
            if replay_evidence
            else CONFIG.app_dir
            / "artifacts"
            / "wechat_c2"
            / "messages"
            / f"{timestamp}_{sidecar_run_id}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        normalized_target_mode = str(target_mode or "").strip()
        effective_max_duration_seconds = max(1, int(max_duration_seconds))
        if normalized_target_mode == "search_by_remark_code":
            # Windows OCR on full WeChat screenshots can take 9-13s per pass on
            # test machines.  Search-by-remark-code needs several passes before
            # messages are read, so the default visible-read budget is too short.
            effective_max_duration_seconds = max(effective_max_duration_seconds, 75)
        normalized_history_mode = str(history_mode or "").strip()
        args = [
            "messages",
            "--sidecar-run-id",
            sidecar_run_id,
            "--history-load-times",
            str(max(0, int(max_scroll_steps or 0))),
            "--max-scroll-steps",
            "0",
            "--max-duration-seconds",
            str(effective_max_duration_seconds),
            "--max-snapshots",
            str(max(1, int(max_snapshots or 1))),
            "--artifact-dir",
            str(artifact_dir),
        ]
        if normalized_history_mode:
            args[1:1] = ["--history-mode", normalized_history_mode]
        if input_safety_observation:
            args.extend(["--input-safety-observation", "--input-safety-request-id", input_safety_request_id])
        if text_recheck_capture:
            args.append("--text-recheck-capture")
        for values, flag in (
            (anchor_ids, "--anchor-id"),
            (anchor_content_keys, "--anchor-content-key"),
            (reply_content_keys, "--reply-content-key"),
        ):
            for value in values or []:
                clean_value = str(value or "").strip()
                if clean_value:
                    args.extend([flag, clean_value])
        args.append(
            "--restore-to-latest"
            if restore_to_latest
            else "--no-restore-to-latest"
        )
        if str(display_name or "").strip():
            args[1:1] = ["--target", display_name]
        if str(rpa_session_key or "").strip():
            args[1:1] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[1:1] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[1:1] = ["--target-mode", normalized_target_mode]
        if str(expected_confirmed_self_text or "").strip():
            args[1:1] = [
                "--expected-confirmed-self-text",
                str(expected_confirmed_self_text),
            ]
        if chat_fact_roi_ocr and CONFIG.c3_pre_send_roi_reuse_enabled:
            args.append("--chat-fact-roi-ocr")
        if replay_evidence:
            args.extend(
                [
                    "--same-frame-full-ocr-evidence",
                    json.dumps(replay_evidence, ensure_ascii=True, default=str),
                ]
            )
        sidecar_timeout = (
            max(30, min(240, effective_max_duration_seconds + 75))
            if normalized_target_mode == "search_by_remark_code"
            or normalized_history_mode
            else max(30, min(90, effective_max_duration_seconds + 30))
        )
        call_options: dict[str, Any] = {"timeout": sidecar_timeout}
        if cancel_check is not None:
            call_options["cancel_check"] = cancel_check
        payload = self._call_omniauto(args, **call_options)
        payload.setdefault("sidecar_run_id", sidecar_run_id)
        payload.setdefault("artifact_dir", str(artifact_dir))
        payload.setdefault("target_mode", normalized_target_mode or "visible")
        payload.setdefault("remark_code", remark_code)
        return payload

    def recheck_text_bubbles(
        self, *, stage: str, payload: dict[str, Any], observation_ids: list[str],
        remark_code: str, cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        """Sidecar validates pixels or OCRs a saved frame; Worker supplies only references."""
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "messages" / f"text-recheck-{uuid.uuid4().hex}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        request_path = artifact_dir / "request.json"
        # Never send checkpoint text/answers to the recognizer.
        request_path.write_text(json.dumps({"stage": stage, "payload": {
            key: payload.get(key) for key in (
                "ok", "frame_observation", "observations", "text_recheck_frame_path",
                "text_recheck_frame_sha256",
                "observation_validation_errors",
            )}, "observation_ids": observation_ids}, ensure_ascii=False), encoding="utf-8")
        result = self._call_omniauto([
            "messages", "--target", remark_code, "--remark-code", remark_code,
            "--target-mode", "current", "--text-recheck-request", str(request_path),
            "--artifact-dir", str(artifact_dir),
        ], timeout=50, cancel_check=cancel_check)
        result.setdefault("artifact_dir", str(artifact_dir))
        result.setdefault("sidecar_run_id", f"text-recheck-{artifact_dir.name}")
        return result

    def recheck_original_message(self, *, original: dict, authorization: dict,
                                 image_path: str, cancel_check: CancellationCheck | None = None) -> dict:
        """Re-OCR immutable saved pixels in OmniAuto, with no desktop action."""
        directory = CONFIG.app_dir / "artifacts" / "wechat_c2" / "messages" / f"original-recheck-{uuid.uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=True)
        request_path = directory / "request.json"
        request_path.write_text(json.dumps({"original": original, "authorization": authorization,
            "image_path": image_path}, ensure_ascii=False), encoding="utf-8")
        result = self._call_omniauto(["messages", "--historical-text-correction-request", str(request_path),
            "--artifact-dir", str(directory)], timeout=60, cancel_check=cancel_check)
        if result.get("ok") is not True:
            raise ValueError(result.get("error_code") or "HISTORICAL_TEXT_CORRECTION_OCR_FAILED")
        expected = directory / "original-ocr-proposal.json"
        if Path(result.get("proposal_path") or "").resolve() != expected.resolve():
            raise ValueError("HISTORICAL_TEXT_CORRECTION_OCR_OUTPUT_INVALID")
        return json.loads(expected.read_text(encoding="utf-8"))

    def locate_chat(
        self,
        *,
        display_name: str,
        rpa_session_key: str,
        remark_code: str = "",
        target_mode: str = "",
        visible_session_candidate: dict[str, Any] | None = None,
        capture_initial_messages: bool = True,
        expected_confirmed_self_text: str = "",
        chat_fact_roi_ocr: bool = False,
        max_duration_seconds: int = 75,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "chat_target_confirmed",
                "sidecar_run_id": f"mock-locate-{uuid.uuid4()}",
                "target_mode": target_mode or "visible",
                "remark_code": remark_code,
                "targeting": {"ok": True, "mode": "mock"},
            }
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        sidecar_run_id = f"locate-{timestamp}-{uuid.uuid4().hex[:8]}"
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "messages" / f"{timestamp}_{sidecar_run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        normalized_target_mode = str(target_mode or "").strip()
        args = [
            "open-chat",
            "--sidecar-run-id",
            sidecar_run_id,
            "--target",
            display_name,
            "--artifact-dir",
            str(artifact_dir),
        ]
        if str(rpa_session_key or "").strip():
            args[1:1] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[1:1] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[1:1] = ["--target-mode", normalized_target_mode]
        if str(expected_confirmed_self_text or "").strip():
            args[1:1] = [
                "--expected-confirmed-self-text",
                str(expected_confirmed_self_text),
            ]
        if capture_initial_messages:
            args.append("--capture-initial-messages")
        if chat_fact_roi_ocr and CONFIG.c3_pre_send_roi_reuse_enabled:
            args.append("--chat-fact-roi-ocr")
        if normalized_target_mode == "visible" and isinstance(visible_session_candidate, dict) and visible_session_candidate:
            args.extend(["--visible-session-candidate", json.dumps(visible_session_candidate, ensure_ascii=True, default=str)])
        sidecar_timeout = max(30, min(240, int(max_duration_seconds) + 75))
        call_options: dict[str, Any] = {"timeout": sidecar_timeout}
        if cancel_check is not None:
            call_options["cancel_check"] = cancel_check
        payload = self._call_omniauto(args, **call_options)
        payload.setdefault("sidecar_run_id", sidecar_run_id)
        payload.setdefault("artifact_dir", str(artifact_dir))
        payload.setdefault("target_mode", normalized_target_mode or "visible")
        payload.setdefault("remark_code", remark_code)
        return payload

    def _voice_action_call(
        self,
        *,
        stage: str,
        display_name: str,
        rpa_session_key: str,
        canonical_voice_action_id: str = "",
        reserved_worker_stable_id: str = "",
        pre_frame_id: str = "",
        selected_pre_observation_id: str = "",
        selected_action_token: str = "",
        selected_target_fingerprint: str = "",
        message_viewport_change_digest: str = "",
        remark_code: str = "",
        target_mode: str = "",
        expected_confirmed_self_text: str = "",
        max_duration_seconds: int = 90,
        excluded_voice_anchor_keys: list[str] | None = None,
        action_journal: Path | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in {"prepare", "execute"}:
            raise ValueError("C2_VOICE_ACTION_STAGE_INVALID")
        if normalized_stage == "execute" and any(
            not str(value or "").strip()
            for value in (
                canonical_voice_action_id,
                reserved_worker_stable_id,
                pre_frame_id,
                selected_pre_observation_id,
                selected_action_token,
                selected_target_fingerprint,
                message_viewport_change_digest,
            )
        ):
            raise ValueError("C2_VOICE_EXECUTE_CONTRACT_INCOMPLETE")
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": (
                    "voice_action_prepare_empty"
                    if normalized_stage == "prepare"
                    else "voice_transcribe_no_visible_voice"
                ),
                "voice_action_stage": normalized_stage,
                "sidecar_run_id": f"mock-voice-{uuid.uuid4()}",
                "target_mode": target_mode or "visible",
                "remark_code": remark_code,
                "transcribed_messages": [],
                "attempt_count": 0,
                "quality_flags": ["mock_no_visible_voice"],
            }
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        sidecar_run_id = f"voice-{timestamp}-{uuid.uuid4().hex[:8]}"
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "voice" / f"{timestamp}_{sidecar_run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        normalized_target_mode = str(target_mode or "").strip()
        args = [
            "voice-transcribe",
            "--sidecar-run-id",
            sidecar_run_id,
            "--voice-action-stage",
            normalized_stage,
            "--max-duration-seconds",
            str(max(1, int(max_duration_seconds))),
            "--artifact-dir",
            str(artifact_dir),
        ]
        if normalized_stage == "execute":
            args[1:1] = [
                "--canonical-voice-action-id",
                str(canonical_voice_action_id).strip(),
                "--reserved-worker-stable-id",
                str(reserved_worker_stable_id).strip(),
                "--pre-frame-id",
                str(pre_frame_id).strip(),
                "--selected-pre-observation-id",
                str(selected_pre_observation_id).strip(),
                "--selected-action-token",
                str(selected_action_token).strip(),
                "--selected-target-fingerprint",
                str(selected_target_fingerprint).strip(),
                "--message-viewport-change-digest",
                str(message_viewport_change_digest).strip(),
            ]
        if str(display_name or "").strip():
            args[1:1] = ["--target", display_name]
        if str(rpa_session_key or "").strip():
            args[1:1] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[1:1] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[1:1] = ["--target-mode", normalized_target_mode]
        if str(expected_confirmed_self_text or "").strip():
            args[1:1] = [
                "--expected-confirmed-self-text",
                str(expected_confirmed_self_text),
            ]
        clean_excluded_anchor_keys = sorted(
            {str(value).strip() for value in (excluded_voice_anchor_keys or []) if str(value).strip()}
        )
        if clean_excluded_anchor_keys:
            args[1:1] = ["--excluded-voice-anchor-keys", json.dumps(clean_excluded_anchor_keys, ensure_ascii=True)]
        if action_journal is not None:
            args[1:1] = ["--action-journal", str(action_journal)]
        payload = self._call_omniauto(
            args,
            timeout=max(900, min(1800, int(max_duration_seconds) * 4 + 120)),
            cancel_check=cancel_check,
        )
        payload.setdefault("sidecar_run_id", sidecar_run_id)
        payload.setdefault("artifact_dir", str(artifact_dir))
        payload.setdefault("target_mode", normalized_target_mode or "visible")
        payload.setdefault("remark_code", remark_code)
        return payload

    def prepare_voice_action(self, **kwargs: Any) -> dict[str, Any]:
        """Capture and select one physical voice without touching WeChat."""

        return self._voice_action_call(stage="prepare", **kwargs)

    def execute_voice_action(self, **kwargs: Any) -> dict[str, Any]:
        """Execute only the exact prepare token persisted by the Worker."""

        return self._voice_action_call(stage="execute", **kwargs)

    def send_reply(
        self,
        *,
        target: str,
        rpa_session_key: str,
        text: str,
        task_id: str,
        reply_action_id: str = "",
        current_only: bool = True,
        expected_context_guard: dict[str, Any] | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "send_mock",
                "action_phase": "confirmed",
                "physical_send_triggered": True,
                "sidecar_run_id": f"mock-send-{uuid.uuid4()}",
                "target": target,
                "send_result": {
                    "ok": True,
                    "confirmed": True,
                    "result": "sent",
                    "action_phase": "confirmed",
                    "physical_send_triggered": True,
                    "method": "mock",
                },
            }
        journal_path = update_filesystem_path(self.send_transaction_journal_path(reply_action_id))
        attempt = send_launch_journal.begin(journal_path, task_id=task_id, action_id=reply_action_id)
        try:
            raw = send_setup_contract.package_bytes(request_id=attempt["request_id"], task_id=task_id,
                reply_action_id=reply_action_id, target=target, text=text, expected_context_guard=expected_context_guard)
            reference = send_request_file.write_package(raw, request_id=attempt["request_id"], app_dir=CONFIG.app_dir,
                before_write=lambda ref: send_launch_journal.record_request(journal_path, attempt["launch_attempt_id"], ref))
        except (OSError, ValueError, TypeError) as exc:
            return self._send_preparation_failure(journal_path, attempt, exc)
        # Persist the actual path before any subsequent preparation can fail.
        send_launch_journal.update(journal_path, attempt["launch_attempt_id"], allowed={"preparing"},
                                   process_state="prepared", request=reference)
        try:
            artifact_dir = update_filesystem_path(CONFIG.app_dir / "artifacts" / "tasks" / task_id / "chat_reply" / time.strftime("%Y%m%d_%H%M%S"))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            args = ["send", "--target", target, "--session-key", rpa_session_key, "--text", text,
                    "--artifact-dir", str(artifact_dir), "--action-journal", str(journal_path),
                    "--expected-context-guard-file", reference["path"], "--expected-context-guard-sha256", reference["sha256"],
                    "--send-task-id", task_id, "--send-action-id", reply_action_id]
            if current_only:
                args.append("--current-only")
            send_request_file.validate_command(self._sidecar_command(args))
        except (OSError, ValueError) as exc:
            return self._send_preparation_failure(journal_path, attempt, exc)
        result = self._call_omniauto(args, timeout=180, cancel_check=cancel_check)
        # The process has exited. Do not modify its journal while it can act.
        proven = send_launch_journal.proof(journal_path, contract=c2_contract_v3())
        if proven:
            return {**result, **send_launch_journal.failure_result(journal_path, contract=c2_contract_v3())}
        journal, _ = send_launch_journal.read(journal_path)
        current = journal["send_launch_attempts"][-1]
        if current["process_state"] == "creating":
            outcome = result.get("send_result")
            outcome = outcome if isinstance(outcome, dict) else {}
            from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import read_failure_valid
            read_failure = result.get("pre_send_read_failure_fact")
            phase = result.get("action_phase", outcome.get("action_phase"))
            physical = result.get("physical_send_triggered", outcome.get("physical_send_triggered"))
            if physical is None and read_failure_valid(read_failure) and phase == "not_attempted":
                # The original OCR proof already states this. Preserve its
                # existing bounded recheck, without inventing another budget.
                physical = read_failure["physical_send_triggered"]
            send_launch_journal.update(journal_path, attempt["launch_attempt_id"], allowed={"creating"},
                process_state="finished", action_phase=phase, physical_send_triggered=physical,
                read_failure_fact=read_failure if read_failure_valid(read_failure) else None,
                error_code=result.get("error_code"))
        return {**result, "send_request_files": send_launch_journal.references(journal_path)}

    @staticmethod
    def _send_preparation_failure(journal_path, attempt, exc):
        send_launch_journal.fail(journal_path, attempt["launch_attempt_id"],
            error_code="RPA_SEND_REQUEST_PREPARE_FAILED", process_state="not_called",
            reason=type(exc).__name__ + ":" + str(exc))
        result = send_launch_journal.failure_result(journal_path, contract=c2_contract_v3())
        # Preparation can fail before the ordinary process diagnostics run.
        # Record references and proof only, never the full conversation guard.
        record_failure("rpa_action_failed", error_code=result["error_code"],
            message="发送资料准备失败，原动作尚未执行，故障证据已保留。",
            metadata={"origin": "send", "result": result,
                      "root_failures": getattr(exc, "failures", [])})
        return result

    def run_add_friend(
        self,
        task: Task,
        emit_step: Callable[[RpaStep], None],
        cancel_check: CancellationCheck | None = None,
    ) -> RpaResult:
        if self.mode == "mock":
            return self._run_mock(task, emit_step)
        journal_path = self.add_friend_transaction_journal_path(task.id)
        if journal_path.exists():
            journal = read_action_journal(journal_path)
            if not journal:
                return self._add_friend_unknown_result(
                    journal_path,
                    "ADD_FRIEND_ACTION_JOURNAL_INVALID",
                )
            phase = action_journal_phase(journal_path)
            if phase == "confirmed":
                return self._add_friend_result_from_journal(
                    journal_path,
                    journal,
                )
            if phase == "trigger_attempted":
                return self._add_friend_unknown_result(
                    journal_path,
                    "ADD_FRIEND_RESULT_UNKNOWN",
                )
        validation = self._validate_task_payload(task)
        artifact_dir = self._artifact_dir(task)
        if validation:
            return RpaResult(
                ok=False,
                error_code="TASK_PAYLOAD_INVALID",
                failure_step="payload_validation",
                message=validation,
                evidence_path=str(artifact_dir),
                evidence_metadata={"artifact_dir": str(artifact_dir), "validation_error": validation},
            )
        if not journal_path.exists():
            initialize_action_journal(
                journal_path,
                action_kind="add_friend",
                transaction_id=task.id,
                conversation_id=f"task:{task.id}",
                items=[
                    {
                        "journal_item_id": task.id,
                        "action_local_id": task.id,
                        "physical_anchor_keys": [],
                    }
                ],
            )
        self._emit_preflight_steps(task, emit_step)
        payload = self._call_omniauto(
            self._add_friend_args(
                task,
                artifact_dir,
                action_journal=journal_path,
            ),
            timeout=360,
            cancel_check=cancel_check,
        )
        self._emit_steps(payload, emit_step)
        evidence_path = self._evidence_path(payload, artifact_dir)
        evidence_metadata = self._evidence_metadata(payload, artifact_dir)
        phase = action_journal_phase(journal_path)
        if phase == "confirmed":
            return self._add_friend_result_from_journal(
                journal_path,
                read_action_journal(journal_path),
                evidence_path=evidence_path,
                evidence_metadata=evidence_metadata,
            )
        payload_error_code = str(payload.get("error_code") or "").strip()
        payload_task_status = str(payload.get("task_status") or "").strip()
        if payload_error_code or payload_task_status == "failed":
            return RpaResult(
                ok=False,
                error_code=payload_error_code or "OTHER",
                failure_step=str(
                    payload.get("failure_step")
                    or payload.get("current_step")
                    or "rpa_execution"
                ),
                message=str(payload.get("message") or "RPA 执行失败"),
                evidence_path=evidence_path,
                evidence_metadata=evidence_metadata,
            )
        if phase == "trigger_attempted":
            return self._add_friend_unknown_result(
                journal_path,
                "ADD_FRIEND_RESULT_UNKNOWN",
                evidence_path=evidence_path,
                evidence_metadata=evidence_metadata,
            )
        if (
            payload.get("ok")
            and str(payload.get("result_code") or "invite_sent")
            == "invite_sent"
            and phase != "confirmed"
        ):
            return self._add_friend_unknown_result(
                journal_path,
                "ADD_FRIEND_ACTION_JOURNAL_UNCONFIRMED",
                evidence_path=evidence_path,
                evidence_metadata=evidence_metadata,
            )
        if payload.get("ok"):
            return RpaResult(
                ok=True,
                result_code=str(payload.get("result_code") or "invite_sent"),
                message=str(payload.get("message") or "已发送添加通讯录邀请"),
                evidence_path=evidence_path,
                evidence_metadata=evidence_metadata,
            )
        return RpaResult(
            ok=False,
            error_code=str(payload.get("error_code") or "OTHER"),
            failure_step=str(payload.get("failure_step") or payload.get("current_step") or "rpa_execution"),
            message=str(payload.get("message") or "RPA 执行失败"),
            evidence_path=evidence_path,
            evidence_metadata=evidence_metadata,
        )

    @staticmethod
    def add_friend_transaction_journal_path(task_id: str) -> Path:
        return action_journal_path("add_friend", str(task_id or "").strip())

    @staticmethod
    def _add_friend_unknown_result(
        journal_path: Path,
        error_code: str,
        *,
        evidence_path: str | None = None,
        evidence_metadata: dict[str, Any] | None = None,
    ) -> RpaResult:
        metadata = dict(evidence_metadata or {})
        metadata.update(
            {
                "action_journal": str(journal_path),
                "action_phase": action_journal_phase(journal_path),
                "manual_confirmation_required": True,
            }
        )
        return RpaResult(
            ok=False,
            error_code=error_code,
            failure_step="invite_confirm_recovery",
            message="好友申请可能已经发送，已禁止自动重复点击，请人工确认。",
            evidence_path=evidence_path,
            evidence_metadata=metadata,
        )

    def _add_friend_result_from_journal(
        self,
        journal_path: Path,
        journal: dict[str, Any],
        *,
        evidence_path: str | None = None,
        evidence_metadata: dict[str, Any] | None = None,
    ) -> RpaResult:
        items = (
            journal.get("items")
            if isinstance(journal.get("items"), dict)
            else {}
        )
        item = (
            items.get(str(journal.get("transaction_id") or ""))
            if isinstance(items, dict)
            else None
        )
        if not isinstance(item, dict) and len(items) == 1:
            item = next(iter(items.values()))
        terminal = (
            item.get("terminal_payload")
            if isinstance(item, dict)
            and isinstance(item.get("terminal_payload"), dict)
            else {}
        )
        if not terminal:
            return self._add_friend_unknown_result(
                journal_path,
                "ADD_FRIEND_RESULT_UNKNOWN",
            )
        ok = bool(terminal.get("ok"))
        metadata = {
            **dict(evidence_metadata or {}),
            "action_journal": str(journal_path),
            "action_phase": "confirmed",
            "recovered_from_action_journal": True,
            "terminal_payload": dict(terminal),
        }
        if ok:
            return RpaResult(
                ok=True,
                result_code=str(
                    terminal.get("result_code") or "invite_sent"
                ),
                message="已从不可逆动作日志恢复好友申请结果。",
                evidence_path=evidence_path,
                evidence_metadata=metadata,
            )
        return RpaResult(
            ok=False,
            error_code=str(
                terminal.get("error_code")
                or "ADD_FRIEND_CONFIRM_FAILED"
            ),
            failure_step=str(
                terminal.get("current_step")
                or "invite_confirm_recovery"
            ),
            message="已从不可逆动作日志恢复好友申请失败结果。",
            evidence_path=evidence_path,
            evidence_metadata=metadata,
        )

    def _run_mock(self, task: Task, emit_step: Callable[[RpaStep], None]) -> RpaResult:
        steps = [
            ("checking_rpa", "检查自动化组件", "自动化组件可用"),
            ("wechat_window_found", "打开微信桌面客户端", "已检测到微信窗口"),
            ("phone_search_started", "搜索手机号", f"正在搜索 {task.search_phone or task.wechat or '未知联系方式'}"),
            ("phone_search_finished", "搜索客户完成", "已定位客户资料"),
            ("add_friend_button_clicked", "进入添加通讯录流程", "已进入添加通讯录流程"),
            ("remark_written", "写入备注短码", task.remark_code or task.remark_name or "已写入备注短码"),
            ("invite_text_filled", "填写申请说明", "已填写添加通讯录申请说明"),
            ("invite_sent", "发送添加通讯录邀请", "已点击发送添加通讯录邀请"),
        ]
        for current_step, title, remark in steps:
            time.sleep(CONFIG.rpa_mock_step_delay_seconds)
            emit_step(RpaStep(current_step=current_step, title=title, remark=remark))
        return RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请")

    def _validate_task_payload(self, task: Task) -> str:
        if not (task.search_phone or task.wechat):
            return "phone or wechat is required"
        if not str(task.verify_message or "").strip():
            return "verify_message is required"
        if not str(task.remark_name or "").strip():
            return "remark_name is required"
        if not str(task.remark_code or "").strip():
            return "remark_code is required"
        if str(task.remark_code).strip() not in str(task.remark_name).strip():
            return "remark_name must include remark_code"
        return ""

    def _artifact_dir(self, task: Task) -> Path:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = CONFIG.app_dir / "artifacts" / "tasks" / task.id / timestamp
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _add_friend_args(
        self,
        task: Task,
        artifact_dir: Path,
        *,
        action_journal: Path | None = None,
    ) -> list[str]:
        args = [OMNIAUTO_ADD_FRIEND_ACTION]
        if task.search_phone:
            args.extend(["--phone", task.search_phone])
        elif task.wechat:
            args.extend(["--wechat", task.wechat])
        args.extend(["--verify-message", str(task.verify_message or "")])
        args.extend(["--remark-name", str(task.remark_name or "")])
        args.extend(["--remark-code", str(task.remark_code or "")])
        args.extend(["--artifact-dir", str(artifact_dir)])
        if action_journal is not None:
            args.extend(["--action-journal", str(action_journal)])
        return args

    def _emit_preflight_steps(self, task: Task, emit_step: Callable[[RpaStep], None]) -> None:
        contact = task.search_phone or task.wechat or "客户联系方式"
        steps = [
            ("rpa_sidecar_starting", "启动 OmniAuto", "正在启动 OmniAuto 加好友主链路。"),
            ("wechat_preflight_starting", "检测微信窗口", f"正在检测微信窗口并准备搜索 {contact}。"),
        ]
        for current_step, title, remark in steps:
            emit_step(RpaStep(current_step=current_step, title=title, remark=remark))

    def _call_omniauto(
        self,
        args: list[str],
        timeout: int = 30,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        original_cancel_check = cancel_check

        def emergency_aware_cancel_check() -> bool | str:
            if emergency_stop_requested():
                return "WORKER_EMERGENCY_STOPPED"
            if original_cancel_check is None:
                return False
            return original_cancel_check()

        args = list(args)
        action = str(args[0] if args else "unknown")
        artifact_dir = self._artifact_dir_from_args(args)
        probe_directory: Path | None = None
        # Keep the exact status frame until the result is known. Successful
        # probes are discarded; failed probes retain their original pixels.
        if action == "status" and artifact_dir is None:
            try:
                probe_directory = (
                    CONFIG.app_dir / "artifacts" / "rpa_probes"
                    / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}"
                )
                probe_directory.mkdir(parents=True, exist_ok=False)
                artifact_dir = probe_directory
                args.extend(["--artifact-dir", str(artifact_dir)])
            except OSError as exc:
                record_capture_failure("rpa_probe_directory", exc)
                probe_directory = None
        resolved_artifact_dir = artifact_dir.resolve() if artifact_dir is not None else None
        if resolved_artifact_dir is not None:
            with self._active_artifact_dirs_lock:
                self._active_artifact_dirs.add(resolved_artifact_dir)
        try:
            started_at = time.monotonic()
            try:
                if emergency_stop_requested():
                    result = {
                        "ok": False, "state": "action_cancelled",
                        "error_code": "WORKER_EMERGENCY_STOPPED",
                        "message": "The worker emergency stop is active.",
                    }
                else:
                    result = self._call_omniauto_process(
                        args, timeout=timeout,
                        cancel_check=emergency_aware_cancel_check,
                    )
            except Exception as exc:
                record_failure(
                    "rpa_action_failed", error_code="RPA_ACTION_EXCEPTION",
                    message="微信执行组件抛出异常，保留原异常处理流程。",
                    metadata={"origin": action, "artifact_dir": str(artifact_dir or ""),
                              "timeout_seconds": timeout, **exception_details(exc)},
                )
                raise
            # Win32 sends return a nested envelope; older adapters use a
            # scalar. Inspect it without changing the business result.
            send_result = result.get("send_result")
            send_failed = False
            if isinstance(send_result, dict):
                send_failed = send_result.get("ok") is False
                send_result = send_result.get("result")
            failed = (
                result.get("ok") is not True
                or send_failed
                or send_result in ("failed", "unknown")
            )
            if failed:
                record_failure(
                    "rpa_action_failed",
                    error_code=str(result.get("error_code") or "RPA_ACTION_FAILED"),
                    message="微信执行组件未成功完成，本次原始结果和可用截图已进入取证。",
                    metadata={
                        "origin": action, "artifact_dir": str(artifact_dir or ""),
                        "timeout_seconds": timeout,
                        "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                        "result": result,
                    },
                )
            else:
                mark_failure_recovered("rpa_action_failed", action)
                if probe_directory is not None:
                    try:
                        shutil.rmtree(probe_directory)
                    except OSError as exc:
                        record_capture_failure("successful_probe_cleanup", exc)
            screenshot_evidence = result.get("screenshot_evidence")
            if isinstance(screenshot_evidence, dict) and screenshot_evidence.get("status") == "save_failed":
                record_failure(
                    "rpa_screenshot_save_failed", error_code="EVIDENCE_SCREENSHOT_SAVE_FAILED",
                    message="状态检测原结果保持不变，但截图保存失败。",
                    metadata={"origin": action, "capture_error": result["screenshot_evidence"]},
                )
            try:
                record_artifact_outcome(artifact_dir, result)
            except Exception as exc:
                try:
                    from .storage import append_log

                    append_log(
                        "WARN",
                        "artifact_retention_marker_failed",
                        "证据留存标记写入失败，OmniAuto 业务结果保持不变。",
                        error_code=type(exc).__name__,
                        metadata={
                            "artifact_dir": str(artifact_dir or ""),
                        },
                    )
                except Exception as capture_exc:
                    record_capture_failure("artifact_retention_marker_failed", capture_exc)
            return result
        finally:
            if resolved_artifact_dir is not None:
                with self._active_artifact_dirs_lock:
                    self._active_artifact_dirs.discard(resolved_artifact_dir)

    def active_artifact_dirs(self) -> set[Path]:
        with self._active_artifact_dirs_lock:
            return set(self._active_artifact_dirs)

    def sidecar_active(self) -> bool:
        with self._active_process_count_lock:
            return self._active_process_count > 0

    @staticmethod
    def _artifact_dir_from_args(args: list[str]) -> Path | None:
        try:
            index = args.index("--artifact-dir")
            value = str(args[index + 1]).strip()
        except (ValueError, IndexError):
            return None
        return Path(value) if value else None

    def _call_omniauto_process(
        self,
        args: list[str],
        timeout: int = 30,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        with self._active_process_count_lock:
            self._active_process_count += 1
        try:
            return self._call_omniauto_process_impl(
                args,
                timeout=timeout,
                cancel_check=cancel_check,
            )
        finally:
            with self._active_process_count_lock:
                self._active_process_count = max(0, self._active_process_count - 1)

    def _call_omniauto_process_impl(
        self,
        args: list[str],
        timeout: int = 30,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if not self.sidecar_script.exists():
            if args and args[0] == "send" and "--expected-context-guard-file" in args:
                journal_path = args[args.index("--action-journal") + 1]
                journal, _ = send_launch_journal.read(journal_path)
                return self._send_preparation_failure(journal_path,
                    journal["send_launch_attempts"][-1], FileNotFoundError("send Sidecar is unavailable"))
            return {"ok": False, "error_code": "RPA_COMPONENT_NOT_READY", "message": f"sidecar 不存在：{self.sidecar_script}"}
        command = self._sidecar_command(args)
        sidecar_env = os.environ.copy()
        # OmniAuto owns the map. Worker supplies only a durable location so
        # independent Sidecar processes use the same calibration_id.
        sidecar_env["CHEJIN_WECHAT_STARTUP_CALIBRATION_PATH"] = str(
            CONFIG.app_dir / "wechat_startup_layout_calibration_v0.9.35.json"
        )
        try:
            if cancel_check is None:
                completed = subprocess.run(
                    command,
                    cwd=str(self._omniauto_root()),
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                    env=sidecar_env,
                )
            else:
                launch = None
                if args and args[0] == "send" and "--expected-context-guard-file" in args:
                    journal_path = args[args.index("--action-journal") + 1]
                    journal, _ = send_launch_journal.read(journal_path)
                    launch = journal["send_launch_attempts"][-1]
                    send_request_file.validate_command(command)
                    send_launch_journal.update(journal_path, launch["launch_attempt_id"],
                        allowed={"prepared"}, process_state="creating")
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(self._omniauto_root()),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace",
                        env=sidecar_env,
                    )
                except OSError as exc:
                    if launch is None:
                        raise
                    send_launch_journal.fail(journal_path, launch["launch_attempt_id"],
                        error_code="RPA_SEND_PROCESS_NOT_STARTED", process_state="create_failed",
                        reason=type(exc).__name__ + ":" + str(exc))
                    return send_launch_journal.failure_result(journal_path, contract=c2_contract_v3())
                deadline = time.monotonic() + max(1, int(timeout))
                while True:
                    cancel_reason = cancel_check()
                    if cancel_reason:
                        error_code = (
                            str(cancel_reason)
                            if isinstance(cancel_reason, str)
                            else "WORKER_INTERRUPTED"
                        )
                        process.terminate()
                        try:
                            stdout, stderr = process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate()
                        return {
                            "ok": False,
                            "state": "action_cancelled",
                            "error_code": error_code,
                            "message": "The RPA action was cancelled by its owning flow.",
                            "stdout": self._tail(stdout),
                            "stderr": self._tail(stderr),
                        }
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        stdout, stderr = process.communicate()
                        raise subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                        completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error_code": "RPA_SIDECAR_TIMEOUT",
                "current_step": "rpa_sidecar_timeout",
                "message": f"OmniAuto sidecar execution timed out after {timeout}s",
                "stdout": self._tail(exc.stdout),
                "stderr": self._tail(exc.stderr),
            }
        output = (completed.stdout or completed.stderr or "").strip()
        try:
            data = self._loads_json_output(output)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error_code": "RPA_SIDECAR_PROTOCOL_INVALID",
                "current_step": "rpa_sidecar_protocol",
                "message": output or "sidecar 无有效 JSON 输出",
                "stdout": self._tail(completed.stdout),
                "stderr": self._tail(completed.stderr),
                "returncode": completed.returncode,
            }
        if completed.returncode and data.get("ok") is not False:
            data = {
                **data,
                "ok": False,
                "error_code": data.get("error_code") or "RPA_SIDECAR_CRASHED",
                "message": data.get("message") or f"OmniAuto sidecar exited with {completed.returncode}",
            }
        data.setdefault("stdout_tail", self._tail(completed.stdout))
        data.setdefault("stderr_tail", self._tail(completed.stderr))
        data.setdefault("returncode", completed.returncode)
        return data

    def _sidecar_command(self, args: list[str]) -> list[str]:
        if bool(getattr(sys, "frozen", False)):
            return [sys.executable, "--omniauto-sidecar", *args]
        return [sys.executable, str(self.sidecar_script), *args]

    def _omniauto_root(self) -> Path:
        parts = list(self.sidecar_script.parents)
        for parent in parts:
            if (parent / "apps" / "wechat_ai_customer_service").exists():
                return parent
        return self.sidecar_script.parent

    def _loads_json_output(self, output: str) -> dict[str, Any]:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            start = output.find("{")
            end = output.rfind("}")
            if start >= 0 and end > start:
                return json.loads(output[start : end + 1])
            raise

    def _emit_steps(self, payload: dict[str, Any], emit_step: Callable[[RpaStep], None]) -> None:
        events = payload.get("diagnostic_events") or payload.get("native_diagnostic_events") or []
        sidecar_run_id = str(payload.get("sidecar_run_id") or "").strip() or None
        if isinstance(events, list):
            for item in events:
                if not isinstance(item, dict):
                    continue
                step_id = str(item.get("step_id") or item.get("current_step") or "")
                if not step_id:
                    continue
                emit_step(
                    RpaStep(
                        current_step=step_id,
                        title=str(item.get("title") or step_id),
                        remark=str(item.get("status") or item.get("state_after") or ""),
                        evidence_path=self._event_artifact_path(item),
                        error_code=str(item.get("error_code") or "").strip() or None,
                        sidecar_run_id=str(item.get("sidecar_run_id") or "").strip() or sidecar_run_id,
                    )
                )
        steps = payload.get("steps")
        if not events and isinstance(steps, list):
            for step in steps:
                current_step = str(step.get("current_step") if isinstance(step, dict) else step)
                emit_step(
                    RpaStep(
                        current_step=current_step,
                        title=current_step,
                        remark="",
                        sidecar_run_id=sidecar_run_id,
                    )
                )

    def _event_artifact_path(self, item: dict[str, Any]) -> str | None:
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, dict):
            return None
        for key in ("annotated", "raw", "screenshot", "review_path", "plan_path"):
            value = artifacts.get(key)
            if value:
                return str(value)
        return None

    def _evidence_path(self, payload: dict[str, Any], artifact_dir: Path) -> str:
        for key in ("review_path", "plan_path", "evidence_path"):
            if payload.get(key):
                return str(payload[key])
        return str(artifact_dir)

    def _evidence_metadata(self, payload: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
        metadata = {
            "artifact_dir": str(artifact_dir),
            "review_path": payload.get("review_path"),
            "plan_path": payload.get("plan_path"),
            "error_code": payload.get("error_code"),
            "result_code": payload.get("result_code"),
            "current_step": payload.get("current_step"),
            "state": payload.get("state"),
            "reason": payload.get("reason"),
            "error": payload.get("error"),
            "sidecar_run_id": payload.get("sidecar_run_id"),
            "stdout_tail": payload.get("stdout_tail") or payload.get("stdout"),
            "stderr_tail": payload.get("stderr_tail") or payload.get("stderr"),
        }
        diagnostics = self._failure_diagnostics(payload)
        if diagnostics:
            metadata["rpa_failure_diagnostics"] = diagnostics
        return metadata

    @classmethod
    def _failure_diagnostics(
        cls,
        value: Any,
        *,
        path: str = "",
        depth: int = 0,
        output: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        collected = output if output is not None else []
        if depth > 7 or len(collected) >= 80:
            return collected
        diagnostic_keys = {
            "state",
            "reason",
            "error",
            "error_code",
            "failure_step",
            "current_step",
            "blocking_reason",
            "ocr_reason",
            "ocr_error",
            "ocr_count",
            "layout_snapshot_id",
            "layout_confidence",
            "layout_conflicts",
            "startup_calibration_evidence",
            "conflicts",
            "no_clicks_performed",
        }
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in diagnostic_keys and item not in (None, "", [], {}):
                    clean_item = str(item)[-1200:] if isinstance(item, str) else item
                    collected.append({"path": child_path, "value": clean_item})
                    if len(collected) >= 80:
                        break
                if isinstance(item, (dict, list)):
                    cls._failure_diagnostics(
                        item,
                        path=child_path,
                        depth=depth + 1,
                        output=collected,
                    )
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                if isinstance(item, (dict, list)):
                    cls._failure_diagnostics(
                        item,
                        path=f"{path}[{index}]",
                        depth=depth + 1,
                        output=collected,
                    )
        return collected

    def _tail(self, value: Any, limit: int = 4000) -> str:
        text = str(value or "")
        return text[-limit:]

    @staticmethod
    def send_transaction_journal_path(reply_action_id: str) -> Path:
        clean_id = str(reply_action_id or "").strip()
        if not clean_id:
            raise ValueError("REPLY_ACTION_ID_MISSING")
        return action_journal_path("send", clean_id)
