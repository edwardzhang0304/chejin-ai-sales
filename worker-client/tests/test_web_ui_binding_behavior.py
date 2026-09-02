from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, contextmanager
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
    qt_core.QTimer = _Widget
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
        window.update_state = {}
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

    def test_real_bridge_normalize_payload_flows_through_status_callback_to_qt_move(self):
        """Production chain: Sidecar public entry -> Bridge -> UI -> Qt boundary."""

        with _headless_web_ui_module() as module:
            omniauto_root = Path(__file__).resolve().parents[1] / "omniauto-rpa"
            if str(omniauto_root) not in sys.path:
                sys.path.insert(0, str(omniauto_root))
            from apps.wechat_ai_customer_service.adapters import (  # noqa: PLC0415
                wechat_win32_ocr_sidecar as sidecar,
            )
            from apps.wechat_ai_customer_service.tests.run_wechat_startup_calibration_v0923_checks import (  # noqa: E501, PLC0415
                search_ocr,
                shell_image,
            )

            image = shell_image()
            normalized_geometry = {
                "left": 100,
                "top": 80,
                "right": 900,
                "bottom": 932,
                "width": 800,
                "height": 852,
            }
            physical_calls = {"normalize": 0, "capture": 0, "ocr": 0}

            def normalize_window_boundary(_hwnd, **_kwargs):
                physical_calls["normalize"] += 1
                return {
                    "ok": True,
                    "enabled": True,
                    "applied": True,
                    "after": dict(normalized_geometry),
                    "after_client": {
                        "width": image.width,
                        "height": image.height,
                    },
                    "after_dpi_scale": 1.0,
                    "reason": "normalized",
                }

            def capture_boundary(_rect):
                physical_calls["capture"] += 1
                return image.copy()

            def ocr_boundary(*_args, **kwargs):
                physical_calls["ocr"] += 1
                return search_ocr(), kwargs.get("engine")

            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                stack.enter_context(patch.object(sidecar, "_WIN32_IMPORT_ERROR", ""))
                stack.enter_context(patch.object(
                    sidecar,
                    "ensure_visible_wechat_window",
                    return_value={
                        "main_windows": [{"hwnd": 101, "pid": 202}],
                        "visible_main_windows": [{"hwnd": 101, "pid": 202}],
                        "visible_windows": [{"hwnd": 101, "pid": 202}],
                    },
                ))
                stack.enter_context(patch.object(
                    sidecar,
                    "select_primary_visible_main_window",
                    return_value={"hwnd": 101, "pid": 202},
                ))
                stack.enter_context(patch.object(sidecar, "activate_window", return_value=None))
                stack.enter_context(patch.object(
                    sidecar,
                    "normalize_wechat_window",
                    side_effect=normalize_window_boundary,
                ))
                stack.enter_context(patch.object(
                    sidecar,
                    "get_window_client_geometry",
                    return_value={
                        "width": image.width,
                        "height": image.height,
                        "screen_left": 100,
                        "screen_top": 80,
                    },
                ))
                stack.enter_context(patch.object(
                    sidecar,
                    "get_window_geometry",
                    return_value=dict(normalized_geometry),
                ))
                stack.enter_context(patch.object(sidecar, "window_dpi_scale", return_value=1.0))
                stack.enter_context(patch.object(sidecar, "try_image_grab", side_effect=capture_boundary))
                stack.enter_context(patch.object(sidecar, "save_screenshot_artifact", return_value="startup.png"))
                stack.enter_context(patch.object(
                    sidecar,
                    "win32gui",
                    types.SimpleNamespace(
                        IsWindow=lambda hwnd: int(hwnd) == 101,
                        GetForegroundWindow=lambda: 101,
                        GetAncestor=lambda hwnd, _flag: int(hwnd),
                    ),
                ))
                stack.enter_context(patch.object(
                    sidecar,
                    "win32process",
                    types.SimpleNamespace(
                        GetWindowThreadProcessId=lambda _hwnd: (1, 202),
                    ),
                ))
                stack.enter_context(patch.object(
                    sidecar.ctypes,
                    "windll",
                    types.SimpleNamespace(
                        user32=types.SimpleNamespace(
                            IsIconic=lambda _hwnd: 0,
                            IsWindowVisible=lambda _hwnd: True,
                            SetProcessDpiAwarenessContext=lambda _context: True,
                            GetThreadDpiAwarenessContext=lambda: -4,
                            GetAwarenessFromDpiAwarenessContext=lambda _context: 2,
                        ),
                    ),
                    create=True,
                ))
                stack.enter_context(patch.object(sidecar, "_DPI_AWARENESS_STATUS", {}))
                stack.enter_context(patch.object(
                    sidecar.win32_ocr_engine,
                    "run_ocr_with_cache",
                    side_effect=ocr_boundary,
                ))
                stack.enter_context(patch.object(
                    sidecar,
                    "STARTUP_CALIBRATION_PATH",
                    Path(directory) / "startup-layout.json",
                ))
                normalize_payload = sidecar.run_action(types.SimpleNamespace(
                    action="normalize-window",
                    artifact_dir=directory,
                    phone="",
                    wechat="",
                    verify_message="",
                    remark_name="",
                    remark_code="",
                    window_policy="normalize",
                ))

            self.assertTrue(normalize_payload["ok"])
            self.assertTrue(normalize_payload["dpi_awareness"]["per_monitor_aware"])
            self.assertEqual(normalize_payload["dpi_awareness"]["awareness"], 2)
            self.assertNotIn("geometry", normalize_payload)
            self.assertEqual(
                normalize_payload["window_normalization"]["after"],
                normalized_geometry,
            )
            self.assertEqual(physical_calls, {"normalize": 1, "capture": 1, "ocr": 1})

            window = self._window(module, None)
            bridge = module.RpaBridge()
            bridge.mode = "real"
            sidecar_calls: list[list[str]] = []

            def sidecar_process_boundary(args, **_kwargs):
                sidecar_calls.append(list(args))
                return json.loads(json.dumps(normalize_payload))

            bridge._call_omniauto = Mock(side_effect=sidecar_process_boundary)
            window.rpa_bridge = bridge
            window.move = Mock()
            window._publish = Mock()

            with patch.object(sys, "platform", "win32"):
                self.assertEqual(bridge.probe(), ("ready", "logged_in"))
            window.on_status("online")

        self.assertEqual(sidecar_calls, [["normalize-window"]])
        self.assertEqual(
            bridge.last_probe_payload["geometry_source"],
            "window_normalization.after",
        )
        window.move.assert_called_once_with(912, 80)
        self.assertTrue(window._startup_position_attempted)

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

    def test_faulted_state_wins_over_offline_and_sync_failure(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ), patch.object(
            module, "save_binding"
        ):
            binding = Binding(
                worker_id="worker-faulted",
                worker_token="token-faulted",
                client_instance_id="client-faulted",
                run_status="faulted",
            )
            window = self._window(module, binding)
            window.runner.run_status_sync_error = "backend unavailable"
            window.on_profile(
                WorkerProfile(
                    id=binding.worker_id,
                    worker_name="故障状态测试 Worker",
                    run_status="faulted",
                    rpa_component_status="ready",
                    wechat_status="logged_in",
                )
            )
            window.on_status("offline")

            payload = json.loads(window.bridge.initialState())

        self.assertEqual(payload["screen"], "client-faulted")
        self.assertEqual(
            payload["model"]["status"]["receiveState"],
            "客户端故障",
        )
        self.assertEqual(payload["model"]["status"]["automationState"], "可用")
        self.assertEqual(payload["model"]["status"]["wechatState"], "已连接")
        self.assertEqual(
            payload["model"]["task"]["statusText"],
            "客户端故障",
        )
        self.assertIn(
            "后端故障状态未同步",
            payload["model"]["task"]["metaText"],
        )
        self.assertIn("故障证据已保留", payload["model"]["task"]["metaText"])
        self.assertNotIn(
            "暂停接单",
            payload["model"]["task"]["statusText"],
        )

    def test_real_automation_unavailable_keeps_environment_issue_screen(self):
        with _headless_web_ui_module() as module, patch.object(
            module, "_log_rows", return_value=[]
        ), patch.object(module, "latest_incident", return_value=None), patch.object(
            module, "lock_summary", return_value={}
        ):
            binding = Binding(
                worker_id="worker-rpa-unavailable",
                worker_token="token-rpa-unavailable",
                client_instance_id="client-rpa-unavailable",
                run_status="paused",
            )
            window = self._window(module, binding)
            window.connection_status = "online"
            window.profile = WorkerProfile(
                id=binding.worker_id,
                worker_name="组件异常测试 Worker",
                run_status="paused",
                rpa_component_status="unavailable",
                wechat_status="logged_in",
            )

            payload = json.loads(window.bridge.initialState())

        self.assertEqual(payload["screen"], "automation-unavailable")
        self.assertEqual(payload["model"]["status"]["automationState"], "不可用")

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
