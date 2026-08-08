from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

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
        window.runner = _Runner()
        window.bridge = module.WorkerWebBridge(window)
        return window

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


if __name__ == "__main__":
    unittest.main()
