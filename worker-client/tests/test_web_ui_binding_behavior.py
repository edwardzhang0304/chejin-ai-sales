from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from chejin_worker_client.models import Binding, WorkerProfile


class _Signal:
    def __init__(self, *_args, **_kwargs) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self._callbacks):
            callback(*args)


class _QtObject:
    def __getattr__(self, _name):
        return _QtObject()

    def __or__(self, _other):
        return self


class _Widget:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


def _slot(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


@contextmanager
def _headless_web_ui_module():
    module_names = (
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWidgets",
        "chejin_worker_client.qt_application",
        "chejin_worker_client.web_ui",
    )
    previous = {name: sys.modules.get(name) for name in module_names}

    qt_core = types.ModuleType("PySide6.QtCore")
    qt_core.QEvent = _QtObject()
    qt_core.QPoint = _Widget
    qt_core.QObject = _Widget
    qt_core.Qt = _QtObject()
    qt_core.QUrl = _Widget
    qt_core.Signal = _Signal
    qt_core.Slot = _slot

    qt_gui = types.ModuleType("PySide6.QtGui")
    qt_gui.QBitmap = _Widget
    qt_gui.QColor = _Widget
    qt_gui.QGuiApplication = _Widget
    qt_gui.QPainter = _Widget

    qt_web_channel = types.ModuleType("PySide6.QtWebChannel")
    qt_web_channel.QWebChannel = _Widget

    qt_web_engine = types.ModuleType("PySide6.QtWebEngineWidgets")
    qt_web_engine.QWebEngineView = _Widget

    qt_widgets = types.ModuleType("PySide6.QtWidgets")
    qt_widgets.QApplication = _Widget
    qt_widgets.QFileDialog = _Widget
    qt_widgets.QMainWindow = _Widget

    pyside = types.ModuleType("PySide6")
    qt_application = types.ModuleType("chejin_worker_client.qt_application")
    qt_application.GuardedQApplication = _Widget

    sys.modules.update(
        {
            "PySide6": pyside,
            "PySide6.QtCore": qt_core,
            "PySide6.QtGui": qt_gui,
            "PySide6.QtWebChannel": qt_web_channel,
            "PySide6.QtWebEngineWidgets": qt_web_engine,
            "PySide6.QtWidgets": qt_widgets,
            "chejin_worker_client.qt_application": qt_application,
        }
    )
    sys.modules.pop("chejin_worker_client.web_ui", None)
    try:
        yield importlib.import_module("chejin_worker_client.web_ui")
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _Runner:
    def __init__(self) -> None:
        self.current_step = None
        self.c2_stats = {}
        self.run_status_sync_error = False
        self.started_with = None

    def start(self, binding) -> None:
        self.started_with = binding

    def stop(self) -> None:
        pass


class WebUiBindingBehaviorTest(unittest.TestCase):
    @staticmethod
    def _window(module, binding):
        available = types.SimpleNamespace(
            x=lambda: 0,
            y=lambda: 0,
            width=lambda: 1920,
            height=lambda: 1080,
        )
        screen = types.SimpleNamespace(
            availableGeometry=lambda: available,
            devicePixelRatio=lambda: 1.0,
        )
        module.QGuiApplication = types.SimpleNamespace(
            screenAt=lambda _point: screen,
            primaryScreen=lambda: screen,
        )
        window = module.WorkerWebWindow.__new__(module.WorkerWebWindow)
        window.binding = binding
        window.profile = None
        window.accept_schedule = {"enabled": False, "start": "09:00", "end": "21:00"}
        window.current_task = None
        window.last_task = None
        window.last_result = None
        window.connection_status = "connecting"
        window.active_page = "workbench"
        window.bind_error = ""
        window.notice = ""
        window.state_revision = 0
        window.step_history = []
        window.runtime_process_timeline = module.RuntimeProcessTimeline()
        window.runner = _Runner()
        window.rpa_bridge = types.SimpleNamespace(last_probe_payload={})
        window._startup_position_attempted = False
        window.bridge = module.WorkerWebBridge(window)
        return window

    def test_startup_wechat_probe_positions_window_once_and_keeps_dragging_free(self):
        with _headless_web_ui_module() as module:
            window = self._window(module, None)
            window.rpa_bridge.last_probe_payload = {
                "ok": True,
                "geometry": {
                    "left": 100,
                    "top": 80,
                    "right": 900,
                    "bottom": 700,
                    "width": 800,
                    "height": 620,
                },
            }
            window.move = Mock()

            window._position_next_to_wechat_once()
            window._position_next_to_wechat_once()

        window.move.assert_called_once_with(912, 80)

    def test_startup_position_converts_native_pixels_at_125_percent_scaling(self):
        with _headless_web_ui_module() as module:
            window = self._window(module, None)
            available = types.SimpleNamespace(
                x=lambda: 0,
                y=lambda: 0,
                width=lambda: 1536,
                height=lambda: 864,
            )
            screen = types.SimpleNamespace(
                availableGeometry=lambda: available,
                devicePixelRatio=lambda: 1.25,
            )
            module.QGuiApplication = types.SimpleNamespace(
                screenAt=lambda _point: screen,
                primaryScreen=lambda: screen,
            )
            window.rpa_bridge.last_probe_payload = {
                "ok": True,
                "geometry": {
                    "left": 20,
                    "top": 90,
                    "right": 983,
                    "bottom": 940,
                    "width": 963,
                    "height": 850,
                },
            }
            window.move = Mock()

            window._position_next_to_wechat_once()

        window.move.assert_called_once_with(798, 72)

    def test_missing_wechat_geometry_keeps_default_window_position(self):
        with _headless_web_ui_module() as module:
            window = self._window(module, None)
            window.move = Mock()

            window._position_next_to_wechat_once()

        window.move.assert_not_called()
        self.assertFalse(window._startup_position_attempted)

    def test_backend_profile_before_first_probe_does_not_consume_position_attempt(self):
        with _headless_web_ui_module() as module:
            window = self._window(module, None)
            window.move = Mock()

            window.on_profile(WorkerProfile(
                id="worker-1",
                worker_name="测试 Worker",
                run_status="paused",
            ))
            self.assertFalse(window._startup_position_attempted)
            window.rpa_bridge.last_probe_payload = {
                "ok": True,
                "geometry": {
                    "left": 100,
                    "top": 80,
                    "right": 900,
                    "bottom": 700,
                    "width": 800,
                    "height": 620,
                },
            }
            window.on_status("connected")

        window.move.assert_called_once_with(912, 80)
        self.assertTrue(window._startup_position_attempted)

    def test_existing_binding_initial_state_restores_workbench(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ):
            binding = Binding(
                worker_id="worker-existing",
                worker_token="token-existing",
                client_instance_id="client-existing",
                run_status="paused",
            )
            window = self._window(module, binding)
            payload = json.loads(window.bridge.initialState())

        self.assertEqual(payload["screen"], "paused-empty")
        self.assertEqual(payload["model"]["workerId"], "worker-existing")
        self.assertNotEqual(payload["screen"], "bind")

    def test_runtime_control_read_failure_does_not_project_client_offline(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ), patch.object(
            module, "load_runtime_control", side_effect=PermissionError("locked")
        ):
            binding = Binding(
                worker_id="worker-existing",
                worker_token="token-existing",
                client_instance_id="client-existing",
                run_status="paused",
            )
            window = self._window(module, binding)
            payload = json.loads(window.bridge.initialState())

        self.assertEqual(payload["screen"], "paused-empty")
        self.assertEqual(
            payload["model"]["status"]["connectionState"], "连接正常"
        )

    def test_binding_success_emits_workbench_state_and_starts_runner(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ), patch.object(module, "save_binding") as save_binding, patch.object(
            module, "append_log"
        ), patch.object(module, "new_client_instance_id", return_value="client-new"):
            window = self._window(module, None)
            profile = WorkerProfile(
                id="worker-new",
                worker_name="测试 Worker",
                run_status="paused",
                rpa_component_status="ready",
                wechat_status="logged_in",
            )
            window.api = types.SimpleNamespace(bind=lambda *_args: profile)
            emitted = []
            window.bridge.stateChanged.connect(lambda value: emitted.append(json.loads(value)))

            window.bind_worker("worker-new", "token-new")

        self.assertIsNotNone(window.binding)
        self.assertEqual(window.binding.worker_id, "worker-new")
        self.assertEqual(window.runner.started_with, window.binding)
        self.assertEqual(emitted[-1]["screen"], "paused-empty")
        self.assertEqual(emitted[-1]["model"]["workerId"], "worker-new")
        self.assertEqual(emitted[-1]["notice"], "绑定成功，已进入 Worker 工作台。")
        save_binding.assert_called()

    def test_paused_idle_worker_does_not_keep_stale_scan_card(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "load_runtime_control", return_value={"inflight_flow_id": ""}
        ):
            binding = Binding(
                worker_id="worker-existing",
                worker_token="token-existing",
                client_instance_id="client-existing",
                run_status="paused",
            )
            window = self._window(module, binding)
            window.connection_status = "online"
            window.runtime_process_timeline.apply({"event": "scan_started"})
            window.runtime_process_timeline.apply({"event": "scan_cancelled"})

            screen = window._screen()

        self.assertEqual(screen, "paused-empty")

    def test_lock_projection_permission_error_keeps_worker_running(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ) as lock_summary_mock, patch.object(module, "append_log") as append_log:
            binding = Binding(
                worker_id="worker-existing",
                worker_token="token-existing",
                client_instance_id="client-existing",
                run_status="running",
            )
            window = self._window(module, binding)
            cached = window.state_json()
            window.runner.stop = Mock()
            emitted: list[str] = []
            window.bridge.stateChanged.connect(emitted.append)
            lock_summary_mock.side_effect = PermissionError(
                "ui_lock.json is temporarily unreadable"
            )

            window._publish()

        self.assertEqual(emitted, [cached])
        window.runner.stop.assert_not_called()
        append_log.assert_called_once()
        self.assertEqual(
            append_log.call_args.kwargs["error_code"],
            "UI_STATE_PROJECTION_FAILED",
        )


if __name__ == "__main__":
    unittest.main()
