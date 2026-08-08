from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG
from .emergency_stop import emergency_stop_requested
from .artifact_retention import record_artifact_outcome
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

    def probe(self) -> tuple[str, str]:
        if self.mode == "mock":
            return "ready", "logged_in"
        if sys.platform != "win32":
            return "unavailable", "unknown"
        payload = self._call_omniauto(["status"], timeout=30)
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
        payload = self._call_omniauto(
            ["sessions", "--artifact-dir", str(resolved_artifact_dir)],
            timeout=60,
            cancel_check=cancel_check,
        )
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
        max_duration_seconds: int = 12,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
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
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "messages" / f"{timestamp}_{sidecar_run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        normalized_target_mode = str(target_mode or "").strip()
        effective_max_duration_seconds = max(1, int(max_duration_seconds))
        if normalized_target_mode == "search_by_remark_code":
            # Windows OCR on full WeChat screenshots can take 9-13s per pass on
            # test machines.  Search-by-remark-code needs several passes before
            # messages are read, so the default visible-read budget is too short.
            effective_max_duration_seconds = max(effective_max_duration_seconds, 75)
        args = [
            "messages",
            "--sidecar-run-id",
            sidecar_run_id,
            "--history-load-times",
            "0",
            "--max-scroll-steps",
            "0",
            "--max-duration-seconds",
            str(effective_max_duration_seconds),
            "--max-snapshots",
            "1",
            "--artifact-dir",
            str(artifact_dir),
        ]
        if str(display_name or "").strip():
            args[3:3] = ["--target", display_name]
        if str(rpa_session_key or "").strip():
            args[3:3] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[3:3] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[3:3] = ["--target-mode", normalized_target_mode]
        sidecar_timeout = (
            max(30, min(240, effective_max_duration_seconds + 75))
            if normalized_target_mode == "search_by_remark_code"
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

    def locate_chat(
        self,
        *,
        display_name: str,
        rpa_session_key: str,
        remark_code: str = "",
        target_mode: str = "",
        visible_session_candidate: dict[str, Any] | None = None,
        capture_initial_messages: bool = True,
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
            args[3:3] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[3:3] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[3:3] = ["--target-mode", normalized_target_mode]
        if capture_initial_messages:
            args.append("--capture-initial-messages")
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

    def voice_transcribe(
        self,
        *,
        display_name: str,
        rpa_session_key: str,
        remark_code: str = "",
        target_mode: str = "",
        max_duration_seconds: int = 90,
        excluded_voice_anchor_keys: list[str] | None = None,
        action_journal: Path | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "voice_transcribe_no_visible_voice",
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
            "--max-duration-seconds",
            str(max(1, int(max_duration_seconds))),
            "--artifact-dir",
            str(artifact_dir),
        ]
        if str(display_name or "").strip():
            args[3:3] = ["--target", display_name]
        if str(rpa_session_key or "").strip():
            args[3:3] = ["--session-key", rpa_session_key]
        if str(remark_code or "").strip():
            args[3:3] = ["--remark-code", remark_code]
        if normalized_target_mode:
            args[3:3] = ["--target-mode", normalized_target_mode]
        clean_excluded_anchor_keys = sorted(
            {str(value).strip() for value in (excluded_voice_anchor_keys or []) if str(value).strip()}
        )
        if clean_excluded_anchor_keys:
            args[3:3] = ["--excluded-voice-anchor-keys", json.dumps(clean_excluded_anchor_keys, ensure_ascii=True)]
        if action_journal is not None:
            args[3:3] = ["--action-journal", str(action_journal)]
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
        artifact_dir = CONFIG.app_dir / "artifacts" / "tasks" / task_id / "chat_reply" / time.strftime("%Y%m%d_%H%M%S")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "send",
            "--target",
            target,
            "--session-key",
            rpa_session_key,
            "--text",
            text,
            "--artifact-dir",
            str(artifact_dir),
        ]
        if reply_action_id:
            journal_path = self.send_transaction_journal_path(reply_action_id)
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            args.extend(["--action-journal", str(journal_path)])
        if current_only:
            args.append("--current-only")
        if isinstance(expected_context_guard, dict):
            args.extend(
                [
                    "--expected-context-guard",
                    json.dumps(
                        expected_context_guard,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ]
            )
        return self._call_omniauto(
            args,
            timeout=180,
            cancel_check=cancel_check,
        )

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
                        "source_message_key": task.id,
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
        if emergency_stop_requested():
            return {
                "ok": False,
                "state": "action_cancelled",
                "error_code": "WORKER_EMERGENCY_STOPPED",
                "message": "The worker emergency stop is active.",
            }
        original_cancel_check = cancel_check

        def emergency_aware_cancel_check() -> bool | str:
            if emergency_stop_requested():
                return "WORKER_EMERGENCY_STOPPED"
            if original_cancel_check is None:
                return False
            return original_cancel_check()

        artifact_dir = self._artifact_dir_from_args(args)
        resolved_artifact_dir = artifact_dir.resolve() if artifact_dir is not None else None
        if resolved_artifact_dir is not None:
            with self._active_artifact_dirs_lock:
                self._active_artifact_dirs.add(resolved_artifact_dir)
        try:
            result = self._call_omniauto_process(
                args,
                timeout=timeout,
                cancel_check=emergency_aware_cancel_check,
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
                except Exception:
                    pass
            return result
        finally:
            if resolved_artifact_dir is not None:
                with self._active_artifact_dirs_lock:
                    self._active_artifact_dirs.discard(resolved_artifact_dir)

    def active_artifact_dirs(self) -> set[Path]:
        with self._active_artifact_dirs_lock:
            return set(self._active_artifact_dirs)

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
        if not self.sidecar_script.exists():
            return {"ok": False, "error_code": "RPA_COMPONENT_NOT_READY", "message": f"sidecar 不存在：{self.sidecar_script}"}
        command = self._sidecar_command(args)
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
                )
            else:
                process = subprocess.Popen(
                    command,
                    cwd=str(self._omniauto_root()),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )
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
        return {
            "artifact_dir": str(artifact_dir),
            "review_path": payload.get("review_path"),
            "plan_path": payload.get("plan_path"),
            "error_code": payload.get("error_code"),
            "result_code": payload.get("result_code"),
            "current_step": payload.get("current_step"),
            "sidecar_run_id": payload.get("sidecar_run_id"),
            "stdout_tail": payload.get("stdout_tail") or payload.get("stdout"),
            "stderr_tail": payload.get("stderr_tail") or payload.get("stderr"),
        }

    def _tail(self, value: Any, limit: int = 4000) -> str:
        text = str(value or "")
        return text[-limit:]

    @staticmethod
    def send_transaction_journal_path(reply_action_id: str) -> Path:
        clean_id = str(reply_action_id or "").strip()
        if not clean_id:
            raise ValueError("REPLY_ACTION_ID_MISSING")
        return action_journal_path("send", clean_id)
