from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QBitmap, QColor, QPainter
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QMainWindow

from .api import WorkerApiClient
from .config import CONFIG
from .incident_evidence import incident_directory, latest_incident
from .models import Binding, RpaResult, RpaStep, Task, WorkerProfile, task_type_title
from .qt_application import GuardedQApplication
from .rpa_bridge import RpaBridge
from .storage import (
    append_log,
    clear_binding,
    is_accept_schedule_active,
    load_accept_schedule,
    load_binding,
    new_client_instance_id,
    read_logs,
    save_accept_schedule,
    save_binding,
)
from .task_runner import TaskRunner
from .ui_lock import lock_summary

WINDOW_WIDTH = 316
WINDOW_HEIGHT = 628
CLIENT_VERSION = "V16.117 · Worker C2/C3 客户端"
TITLEBAR_HEIGHT = 28
WINDOW_CONTROL_WIDTH = 90
WINDOW_RADIUS = 10


def _format_time(value: str | None) -> str:
    if not value:
        return "暂无"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return value[-8:] if len(value) >= 8 else value


def _task_model(task: Task | None) -> dict[str, str]:
    if not task:
        return {
            "id": "-",
            "title": "暂无任务",
            "statusText": "暂无任务",
            "customerName": "暂无客户",
            "phone": "-",
            "sellerName": "-",
            "noteCode": "-",
            "metaText": "当前没有正在执行的 Worker 任务。",
        }
    note_code = task.remark_code or "-"
    if task.task_type == "chat_reply":
        meta_text = f"{task.customer_name or '当前客户'} · AI 回复发送"
    else:
        meta_text = f"{task.customer_name or '未知客户'} · {task.phone or task.wechat or '-'} · {task.sales_name or '-'} · 备注短码：{note_code}"
    return {
        "id": task.id or "-",
        "title": task_type_title(task.task_type),
        "statusText": "接单中",
        "customerName": task.customer_name or "未知客户",
        "phone": task.phone or task.wechat or "-",
        "sellerName": task.sales_name or "-",
        "noteCode": note_code,
        "metaText": meta_text,
    }


def _log_rows() -> list[dict[str, str]]:
    def metadata_value(value: Any, key: str) -> str:
        if isinstance(value, dict):
            if value.get(key):
                return str(value[key])
            for child in value.values():
                found = metadata_value(child, key)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = metadata_value(child, key)
                if found:
                    return found
        return ""

    rows: list[dict[str, str]] = []
    for row in read_logs(limit=200):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        rows.append(
            {
                "time": _format_time(str(row.get("created_at") or "")),
                "level": str(row.get("level") or "-"),
                "task": str(row.get("task_id") or "-"),
                "content": str(row.get("message") or row.get("event") or ""),
                "event": str(row.get("event") or "-"),
                "errorCode": str(row.get("error_code") or "-"),
                "incidentId": metadata_value(metadata, "incident_id") or "-",
                "sidecarRunId": metadata_value(metadata, "sidecar_run_id") or "-",
                "evidencePath": metadata_value(metadata, "evidence_path") or "-",
            }
        )
    return rows


class WorkerWebBridge(QObject):
    stateChanged = Signal(str)

    def __init__(self, window: "WorkerWebWindow") -> None:
        super().__init__()
        self.window = window

    @Slot(result=str)
    def initialState(self) -> str:
        return self.window.state_json()

    @Slot(str)
    def changeScreen(self, screen: str) -> None:
        self.window.change_screen(screen)

    @Slot()
    def goBack(self) -> None:
        self.window.go_back()

    @Slot(str, str)
    def bindWorker(self, worker_id: str, worker_token: str) -> None:
        self.window.bind_worker(worker_id, worker_token)

    @Slot()
    def startAccepting(self) -> None:
        self.window.set_accepting(True)

    @Slot()
    def pauseAccepting(self) -> None:
        self.window.set_accepting(False)

    @Slot(bool, str, str)
    def updateAcceptSchedule(self, enabled: bool, start: str, end: str) -> None:
        self.window.update_accept_schedule(enabled, start, end)

    @Slot()
    def triggerWechatScan(self) -> None:
        self.window.trigger_wechat_scan()

    @Slot(result=str)
    def exportLatestIncident(self) -> str:
        return self.window.export_latest_incident()

    @Slot(result=str)
    def openIncidentDirectory(self) -> str:
        return self.window.open_incident_directory()

    @Slot(int, int)
    def startWindowDrag(self, screen_x: int, screen_y: int) -> None:
        self.window.start_window_drag(screen_x, screen_y)

    @Slot(int, int)
    def moveWindowDrag(self, screen_x: int, screen_y: int) -> None:
        self.window.move_window_drag(screen_x, screen_y)

    @Slot()
    def endWindowDrag(self) -> None:
        self.window.end_window_drag()

    @Slot()
    def minimizeWindow(self) -> None:
        self.window.showMinimized()

    @Slot()
    def closeWindow(self) -> None:
        self.window.close()

    def emit_state(self) -> None:
        self.stateChanged.emit(self.window.state_json())


class WorkerWebWindow(QMainWindow):
    profile_signal = Signal(object)
    status_signal = Signal(str)
    task_signal = Signal(object)
    step_signal = Signal(object)
    result_signal = Signal(object)
    error_signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("车金 Worker 客户端")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.api = WorkerApiClient()
        self.rpa_bridge = RpaBridge()
        self.binding = load_binding()
        self.profile: WorkerProfile | None = None
        self.accept_schedule = load_accept_schedule()
        self.current_task: Task | None = None
        self.last_task: Task | None = None
        self.last_result: RpaResult | None = None
        self.connection_status = "connecting"
        self.active_page = "workbench"
        self.bind_error = ""
        self.step_history: list[dict[str, Any]] = []
        self._drag_origin: QPoint | None = None

        self.runner = TaskRunner(
            self.api,
            self.rpa_bridge,
            on_profile=lambda value: self.profile_signal.emit(value),
            on_status=lambda value: self.status_signal.emit(value),
            on_task=lambda value: self.task_signal.emit(value),
            on_step=lambda value: self.step_signal.emit(value),
            on_result=lambda value: self.result_signal.emit(value),
            on_error=lambda value: self.error_signal.emit(value),
            can_pull_tasks=self.is_accept_schedule_active,
        )

        self.bridge = WorkerWebBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("chejinBridge", self.bridge)

        self.view = QWebEngineView(self)
        self.view.page().setWebChannel(self.channel)
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.installEventFilter(self)
        self.setCentralWidget(self.view)

        self._wire_signals()
        self._load_web_assets()
        if self.binding:
            self.runner.start(self.binding)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._apply_rounded_mask()

    def _apply_rounded_mask(self) -> None:
        mask = QBitmap(self.size())
        mask.fill(Qt.GlobalColor.color0)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.GlobalColor.color1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), WINDOW_RADIUS, WINDOW_RADIUS)
        painter.end()
        self.setMask(mask)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.view:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if pos.y() <= TITLEBAR_HEIGHT:
                if pos.x() >= WINDOW_WIDTH - WINDOW_CONTROL_WIDTH:
                    return self._handle_window_control_click(pos.x())
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
        if event.type() == QEvent.Type.MouseMove and self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            return True
        if event.type() in {QEvent.Type.MouseButtonRelease, QEvent.Type.Leave}:
            self._drag_origin = None
        return super().eventFilter(watched, event)

    def _handle_window_control_click(self, x: int) -> bool:
        control_index = max(0, min(2, (x - (WINDOW_WIDTH - WINDOW_CONTROL_WIDTH)) // 30))
        if control_index == 0:
            self.showMinimized()
        elif control_index == 2:
            self.close()
        return True

    def start_window_drag(self, screen_x: int, screen_y: int) -> None:
        self._drag_origin = QPoint(screen_x, screen_y) - self.frameGeometry().topLeft()

    def move_window_drag(self, screen_x: int, screen_y: int) -> None:
        if self._drag_origin is None:
            return
        self.move(QPoint(screen_x, screen_y) - self._drag_origin)

    def end_window_drag(self) -> None:
        self._drag_origin = None

    def _wire_signals(self) -> None:
        self.profile_signal.connect(self.on_profile)
        self.status_signal.connect(self.on_status)
        self.task_signal.connect(self.on_task)
        self.step_signal.connect(self.on_step)
        self.result_signal.connect(self.on_result)
        self.error_signal.connect(self.on_error)

    def _load_web_assets(self) -> None:
        index_file = Path(__file__).resolve().parent / "web_assets" / "index.html"
        self.view.load(QUrl.fromLocalFile(str(index_file)))

    def state_json(self) -> str:
        return json.dumps(self._state(), ensure_ascii=False)

    def _state(self) -> dict[str, Any]:
        return {
            "screen": self._screen(),
            "model": self._model(),
            "bindError": self.bind_error,
        }

    def _screen(self) -> str:
        if not self.binding:
            return "bind"
        if self.active_page in {"settings", "schedule-settings", "logs"}:
            return self.active_page
        if self.connection_status == "offline":
            return "offline"
        schedule_active = self.is_accept_schedule_active()
        if self.current_task and (self.binding.run_status == "paused" or not schedule_active):
            return "paused-running"
        if self.current_task:
            return "running"
        if self.last_result and self.last_result.ok:
            return "completed"
        if self.last_result and not self.last_result.ok:
            return "failed"
        if self.binding.run_status == "running" and schedule_active:
            return "accepting-wait"
        if self.binding.run_status == "running":
            return "schedule-paused"
        return "paused-empty"

    def _model(self) -> dict[str, Any]:
        run_status = self.binding.run_status if self.binding else "paused"
        display_task = self.current_task or self.last_task
        profile = self.profile
        offline = self.connection_status == "offline"
        schedule_active = self.is_accept_schedule_active()
        receive_state = "接单中" if run_status == "running" and schedule_active else "暂停接单"
        return {
            "workerId": self.binding.worker_id if self.binding else "",
            "workerToken": self.binding.worker_token if self.binding else "",
            "version": CLIENT_VERSION,
            "schedule": dict(self.accept_schedule),
            "status": {
                "sellerName": self._seller_name(profile),
                "receiveState": "离线" if offline else receive_state,
                "connectionState": "连接异常" if offline else "连接正常",
                "lastHeartbeat": _format_time(profile.last_heartbeat_at if profile else None),
                "automationState": "可用" if profile and profile.rpa_component_status == "ready" else "不可用",
                "wechatState": "已连接" if profile and profile.wechat_status == "logged_in" else "未连接",
                "currentStep": self.runner.current_step or (profile.current_step if profile else None) or "",
            },
            "listener": self._listener_model(),
            "localLock": lock_summary(),
            "task": self._task_model_for_screen(display_task, offline, run_status),
            "runningSteps": self._running_steps(),
            "completedSteps": self._completed_steps(),
            "failedSteps": self._failed_steps(),
            "logs": _log_rows(),
            "latestIncident": latest_incident() or {},
        }

    def _listener_model(self) -> dict[str, Any]:
        stats = dict(getattr(self.runner, "c2_stats", {}) or {})
        return {
            "enabled": bool(CONFIG.c2_enabled),
            "lastScanAt": stats.get("last_scan_at"),
            "lastScanSessions": stats.get("last_scan_sessions") or 0,
            "lastBoundCount": stats.get("last_bound_count") or 0,
            "lastMessageReadAt": stats.get("last_message_read_at"),
            "lastIngestedCount": stats.get("last_ingested_count") or 0,
            "lastVisibleHitCount": stats.get("last_visible_hit_count") or 0,
            "lastStateTargetCount": stats.get("last_state_target_count") or 0,
            "lastError": stats.get("last_error"),
        }

    def _task_model_for_screen(self, task: Task | None, offline: bool, run_status: str) -> dict[str, str]:
        model = _task_model(task)
        if self.runner.run_status_sync_error and run_status == "paused":
            model["statusText"] = "暂停接单 · 同步失败"
            model["metaText"] = (
                "本地微信操作已停止，暂停状态尚未同步到后端，客户端正在自动重试。"
            )
            return model
        if task:
            if offline:
                model["statusText"] = "离线"
            elif run_status == "paused":
                model["statusText"] = "暂停接单"
            return model
        if offline:
            model["statusText"] = "离线"
            model["metaText"] = "客户端当前无法连接后端，暂不领取新任务。"
        elif run_status == "running" and self.is_accept_schedule_active():
            model["statusText"] = "等待任务"
            model["metaText"] = "接单中，等待服务端分配任务。"
        elif run_status == "running":
            model["statusText"] = "非接单时段"
            model["metaText"] = "当前不在自动接单时段内，客户端保持连接但不领取新任务。"
        else:
            model["statusText"] = "暂停接单"
            model["metaText"] = "暂停接单后不会领取新的任务。"
        return model

    def is_accept_schedule_active(self) -> bool:
        return is_accept_schedule_active(self.accept_schedule)

    def _seller_name(self, profile: WorkerProfile | None) -> str:
        if profile and profile.bound_sales_name:
            return profile.bound_sales_name
        if profile and profile.worker_name:
            return profile.worker_name
        if self.binding and self.binding.worker_id:
            return "已绑定 Worker"
        return "未绑定"

    def _running_steps(self) -> list[dict[str, Any]]:
        steps = list(self.step_history)
        if self.current_task:
            title = self.current_task.current_step or "正在执行任务"
            steps.append(
                {
                    "state": "current",
                    "title": title,
                    "description": (
                        f"Worker 正在执行"
                        f"{task_type_title(self.current_task.task_type)}任务。"
                    ),
                }
            )
        return steps or [{"state": "current", "title": "等待任务", "description": "接单中，等待服务端分配任务。"}]

    def _completed_steps(self) -> list[dict[str, Any]]:
        steps = list(self.step_history)
        steps.append(
            {
                "state": "done",
                "title": "回传执行结果",
                "description": self.last_result.message if self.last_result else "任务已完成。",
                "finalText": "任务执行完成",
            }
        )
        return steps

    def _failed_steps(self) -> list[dict[str, Any]]:
        steps = list(self.step_history)
        message = self.last_result.message if self.last_result else "任务执行失败。"
        code = self.last_result.error_code if self.last_result else "OTHER"
        steps.append({"state": "error", "title": "任务执行失败", "description": f"{code} · {message}", "finalText": "任务执行失败"})
        return steps

    def _publish(self) -> None:
        self.bridge.emit_state()

    def change_screen(self, screen: str) -> None:
        if screen in {"settings", "schedule-settings", "logs"}:
            self.active_page = screen
        else:
            self.active_page = "workbench"
        self._publish()

    def go_back(self) -> None:
        self.active_page = "settings" if self.active_page in {"schedule-settings", "logs"} else "workbench"
        self._publish()

    def bind_worker(self, worker_id: str, worker_token: str) -> None:
        worker_id = worker_id.strip()
        worker_token = worker_token.strip()
        if not worker_id or not worker_token:
            self.bind_error = "Worker ID 和 Worker Token 必填。"
            self._publish()
            return
        client_instance_id = self.binding.client_instance_id if self.binding else new_client_instance_id()
        try:
            profile = self.api.bind(worker_id, worker_token, client_instance_id)
            self.binding = Binding(worker_id=worker_id, worker_token=worker_token, client_instance_id=client_instance_id, run_status="paused")
            save_binding(self.binding)
            append_log("INFO", "worker_bound", "绑定 Worker 成功。")
            self.bind_error = ""
            self.active_page = "workbench"
            self.on_profile(profile)
            self.runner.start(self.binding)
        except Exception as exc:
            self.bind_error = str(exc)
            append_log("ERROR", "worker_bind_failed", str(exc))
            self._publish()

    def set_accepting(self, accepting: bool) -> None:
        if not self.binding:
            return
        next_status = "running" if accepting else "paused"
        self.runner.set_run_status(next_status)
        self._publish()

    def update_accept_schedule(self, enabled: bool, start: str, end: str) -> None:
        self.accept_schedule = save_accept_schedule(enabled=enabled, start=start, end=end)
        append_log(
            "INFO",
            "accept_schedule_changed",
            f"自动接单时段{'开启' if self.accept_schedule['enabled'] else '关闭'}：{self.accept_schedule['start']} 至 {self.accept_schedule['end']}。",
        )
        self._publish()

    def trigger_wechat_scan(self) -> None:
        self.runner.request_immediate_scan()
        append_log("INFO", "c2_manual_scan_requested", "已触发手动立即扫描。")
        self._publish()

    def export_latest_incident(self) -> str:
        latest = latest_incident()
        if not latest:
            self.on_error("当前没有可导出的故障证据包。")
            return ""
        source = Path(latest["evidence_path"])
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "导出最近一次故障证据",
            str(Path.home() / "Downloads" / source.name),
            "ZIP 文件 (*.zip)",
        )
        if not destination:
            return ""
        try:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            append_log(
                "ERROR",
                "incident_export_failed",
                str(exc),
                error_code="INCIDENT_EXPORT_FAILED",
            )
            self.on_error("故障证据导出失败。")
            return ""
        append_log(
            "INFO",
            "incident_exported",
            "最近一次故障证据已导出。",
            metadata={"incident_id": latest.get("incident_id"), "export_path": str(target)},
        )
        self._publish()
        return str(target)

    def open_incident_directory(self) -> str:
        directory = incident_directory()
        try:
            if sys.platform == "win32":
                os.startfile(str(directory))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(directory)])
            else:
                subprocess.Popen(["xdg-open", str(directory)])
        except OSError as exc:
            append_log(
                "ERROR",
                "incident_directory_open_failed",
                str(exc),
                error_code="INCIDENT_DIRECTORY_OPEN_FAILED",
            )
            self.on_error("无法打开故障证据目录。")
            return ""
        return str(directory)

    @Slot(object)
    def on_profile(self, profile: WorkerProfile) -> None:
        self.profile = profile
        if self.binding:
            self.binding.run_status = profile.run_status
            save_binding(self.binding)
        self._publish()

    @Slot(str)
    def on_status(self, status: str) -> None:
        self.connection_status = status
        if status == "invalid":
            clear_binding()
            self.binding = None
            self.runner.stop()
            self.bind_error = "绑定已失效，请重新绑定。"
            self.active_page = "workbench"
        self._publish()

    @Slot(object)
    def on_task(self, task: Task | None) -> None:
        self.current_task = task
        if task:
            if not self.last_task or self.last_task.id != task.id or self.last_result:
                self.step_history.clear()
            self.last_task = task
            self.last_result = None
            if not self.step_history:
                self.step_history.append(
                    {
                        "state": "done",
                        "title": "任务已领取",
                        "description": (
                            f"Worker 已领取"
                            f"{task_type_title(task.task_type)}任务。"
                        ),
                    }
                )
        self._publish()

    @Slot(object)
    def on_step(self, step: RpaStep) -> None:
        self.step_history.append({"state": "done", "title": step.title, "description": step.remark or step.current_step})
        self._publish()

    @Slot(object)
    def on_result(self, result: RpaResult | None) -> None:
        if not result:
            self.last_result = None
            self._publish()
            return
        self.last_result = result
        self._publish()

    @Slot(str)
    def on_error(self, message: str) -> None:
        if message:
            append_log("WARN", "client_notice", message)
        self._publish()


def run_app() -> int:
    app = GuardedQApplication(sys.argv)
    app.setApplicationName("车金 Worker 客户端")
    window = WorkerWebWindow()
    window.show()
    return app.exec()
