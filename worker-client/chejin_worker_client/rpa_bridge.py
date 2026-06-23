from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG
from .models import RpaResult, RpaStep, Task


CLIENT_ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ADD_FRIEND_ACTION = "add-friend-entry-click-plan-windows"


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

    def list_sessions(self) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": f"mock-session-{uuid.uuid4()}",
                "sessions": [],
            }
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "sessions" / time.strftime("%Y%m%d_%H%M%S")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._call_omniauto(["sessions", "--artifact-dir", str(artifact_dir)], timeout=120)

    def get_messages(self, *, display_name: str, rpa_session_key: str) -> dict[str, Any]:
        if self.mode == "mock":
            return {
                "ok": True,
                "online": True,
                "adapter": "mock",
                "state": "messages_mock",
                "sidecar_run_id": f"mock-message-{uuid.uuid4()}",
                "messages": [],
            }
        artifact_dir = CONFIG.app_dir / "artifacts" / "wechat_c2" / "messages" / time.strftime("%Y%m%d_%H%M%S")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        args = ["messages", "--target", display_name, "--session-key", rpa_session_key, "--history-load-times", "0", "--artifact-dir", str(artifact_dir)]
        return self._call_omniauto(args, timeout=120)

    def run_add_friend(self, task: Task, emit_step: Callable[[RpaStep], None]) -> RpaResult:
        if self.mode == "mock":
            return self._run_mock(task, emit_step)
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
        self._emit_preflight_steps(task, emit_step)
        payload = self._call_omniauto(self._add_friend_args(task, artifact_dir), timeout=360)
        self._emit_steps(payload, emit_step)
        evidence_path = self._evidence_path(payload, artifact_dir)
        evidence_metadata = self._evidence_metadata(payload, artifact_dir)
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

    def _add_friend_args(self, task: Task, artifact_dir: Path) -> list[str]:
        args = [OMNIAUTO_ADD_FRIEND_ACTION]
        if task.search_phone:
            args.extend(["--phone", task.search_phone])
        elif task.wechat:
            args.extend(["--wechat", task.wechat])
        args.extend(["--verify-message", str(task.verify_message or "")])
        args.extend(["--remark-name", str(task.remark_name or "")])
        args.extend(["--remark-code", str(task.remark_code or "")])
        args.extend(["--artifact-dir", str(artifact_dir)])
        return args

    def _emit_preflight_steps(self, task: Task, emit_step: Callable[[RpaStep], None]) -> None:
        contact = task.search_phone or task.wechat or "客户联系方式"
        steps = [
            ("rpa_sidecar_starting", "启动 OmniAuto", "正在启动 OmniAuto 加好友主链路。"),
            ("wechat_preflight_starting", "检测微信窗口", f"正在检测微信窗口并准备搜索 {contact}。"),
            ("operator_guard_starting", "启动键鼠守护", "正在启动悬浮球键鼠守护，执行期间请勿操作鼠标键盘。"),
        ]
        for current_step, title, remark in steps:
            emit_step(RpaStep(current_step=current_step, title=title, remark=remark))

    def _call_omniauto(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        if not self.sidecar_script.exists():
            return {"ok": False, "error_code": "RPA_COMPONENT_NOT_READY", "message": f"sidecar 不存在：{self.sidecar_script}"}
        try:
            completed = subprocess.run(
                [sys.executable, str(self.sidecar_script), *args],
                cwd=str(self._omniauto_root()),
                text=True,
                capture_output=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
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
                    )
                )
        steps = payload.get("steps")
        if not events and isinstance(steps, list):
            for step in steps:
                current_step = str(step.get("current_step") if isinstance(step, dict) else step)
                emit_step(RpaStep(current_step=current_step, title=current_step, remark=""))

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
            "stdout_tail": payload.get("stdout_tail") or payload.get("stdout"),
            "stderr_tail": payload.get("stderr_tail") or payload.get("stderr"),
        }

    def _tail(self, value: Any, limit: int = 4000) -> str:
        text = str(value or "")
        return text[-limit:]
