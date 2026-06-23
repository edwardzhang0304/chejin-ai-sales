"""Contract checks for Win32/OCR activate_window execution behavior."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import types


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class ActivationFixture:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.user32 = FakeUser32(self)
        self.win32gui = FakeWin32Gui(self)
        self.win32process = FakeWin32Process(self)
        self.win32api = FakeWin32Api(self)
        self.win32con = types.SimpleNamespace(
            SWP_NOMOVE=0x0002,
            SWP_NOSIZE=0x0001,
            SWP_SHOWWINDOW=0x0040,
            HWND_TOPMOST=-1,
            HWND_NOTOPMOST=-2,
            VK_MENU=0x12,
            KEYEVENTF_KEYUP=0x0002,
        )


class FakeUser32:
    def __init__(self, fixture: ActivationFixture) -> None:
        self.fixture = fixture

    def IsIconic(self, hwnd: int) -> int:
        self.fixture.events.append(f"user32.IsIconic:{hwnd}")
        return 0

    def IsWindowVisible(self, hwnd: int) -> bool:
        self.fixture.events.append(f"user32.IsWindowVisible:{hwnd}")
        return True

    def IsWindow(self, hwnd: int) -> bool:
        self.fixture.events.append(f"user32.IsWindow:{hwnd}")
        return True

    def ShowWindow(self, hwnd: int, mode: int) -> None:
        self.fixture.events.append(f"user32.ShowWindow:{hwnd}:{mode}")

    def BringWindowToTop(self, hwnd: int) -> None:
        self.fixture.events.append(f"user32.BringWindowToTop:{hwnd}")

    def SetForegroundWindow(self, hwnd: int) -> None:
        self.fixture.events.append(f"user32.SetForegroundWindow:{hwnd}")


class FakeWin32Gui:
    def __init__(self, fixture: ActivationFixture) -> None:
        self.fixture = fixture

    def GetForegroundWindow(self) -> int:
        self.fixture.events.append("win32gui.GetForegroundWindow")
        return 2002

    def SetForegroundWindow(self, hwnd: int) -> None:
        self.fixture.events.append(f"win32gui.SetForegroundWindow:{hwnd}")

    def SetActiveWindow(self, hwnd: int) -> None:
        self.fixture.events.append(f"win32gui.SetActiveWindow:{hwnd}")

    def SetFocus(self, hwnd: int) -> None:
        self.fixture.events.append(f"win32gui.SetFocus:{hwnd}")

    def SetWindowPos(self, hwnd: int, insert_after: int, *_args) -> None:
        self.fixture.events.append(f"win32gui.SetWindowPos:{hwnd}:{insert_after}")

    def GetWindowRect(self, hwnd: int) -> tuple[int, int, int, int]:
        self.fixture.events.append(f"win32gui.GetWindowRect:{hwnd}")
        return (10, 20, 990, 880)


class FakeWin32Process:
    def __init__(self, fixture: ActivationFixture) -> None:
        self.fixture = fixture

    def GetWindowThreadProcessId(self, hwnd: int) -> tuple[int, int]:
        self.fixture.events.append(f"win32process.GetWindowThreadProcessId:{hwnd}")
        if int(hwnd) == 2002:
            return (10, 9002)
        return (20, 9001)

    def AttachThreadInput(self, source_tid: int, target_tid: int, attach: bool) -> None:
        self.fixture.events.append(f"win32process.AttachThreadInput:{source_tid}:{target_tid}:{int(bool(attach))}")


class FakeWin32Api:
    def __init__(self, fixture: ActivationFixture) -> None:
        self.fixture = fixture

    def GetCurrentThreadId(self) -> int:
        self.fixture.events.append("win32api.GetCurrentThreadId")
        return 30

    def keybd_event(self, key: int, scan: int, flags: int, extra: int) -> None:
        self.fixture.events.append(f"win32api.keybd_event:{key}:{scan}:{flags}:{extra}")


class PatchActivation:
    def __init__(self, fixture: ActivationFixture, *, focus_results: list[dict[str, object]], aggressive: bool = False, attach: bool = False) -> None:
        self.fixture = fixture
        self.focus_results = list(focus_results)
        self.aggressive = aggressive
        self.attach = attach
        self.originals: dict[str, object] = {}
        self.previous_env: dict[str, str | None] = {}

    def __enter__(self) -> "PatchActivation":
        for name in (
            "WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS",
            "WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS",
            "WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT",
        ):
            self.previous_env[name] = os.environ.get(name)
        os.environ["WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS"] = "0"
        os.environ["WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS"] = "1" if self.aggressive else "0"
        os.environ["WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT"] = "1" if self.attach else "0"
        self.originals = {
            "windll": sidecar.ctypes.windll,
            "win32gui": sidecar.win32gui,
            "win32process": sidecar.win32process,
            "win32api": sidecar.win32api,
            "win32con": sidecar.win32con,
            "foreground_window_matches_target": sidecar.foreground_window_matches_target,
            "require_active_ui_action_budget": sidecar.require_active_ui_action_budget,
            "humanized_action_sleep": sidecar.humanized_action_sleep,
            "coordinate_rpa_action": sidecar.coordinate_rpa_action,
            "focus_click_fallback_enabled": sidecar.focus_click_fallback_enabled,
            "click": sidecar.click,
            "last_activate": dict(sidecar._LAST_ACTIVATE_MONOTONIC_BY_HWND),
        }
        sidecar.ctypes.windll = types.SimpleNamespace(user32=self.fixture.user32)
        sidecar.win32gui = self.fixture.win32gui
        sidecar.win32process = self.fixture.win32process
        sidecar.win32api = self.fixture.win32api
        sidecar.win32con = self.fixture.win32con

        def fake_foreground(_hwnd: int) -> dict[str, object]:
            self.fixture.events.append("sidecar.foreground_window_matches_target")
            if self.focus_results:
                return dict(self.focus_results.pop(0))
            return {"ok": False, "reason": "foreground_not_wechat_target"}

        sidecar.foreground_window_matches_target = fake_foreground
        sidecar.require_active_ui_action_budget = lambda action, metadata=None: self.fixture.events.append(f"sidecar.require_budget:{action}:{metadata}") or {"ok": True}
        sidecar.humanized_action_sleep = lambda min_ms, max_ms=None: self.fixture.events.append(f"sidecar.sleep:{min_ms}:{max_ms}") or 0.0
        sidecar.coordinate_rpa_action = lambda action, metadata=None, recent_events=None: self.fixture.events.append(f"sidecar.coordinate:{action}:{metadata}") or {"ok": True}
        sidecar.focus_click_fallback_enabled = lambda: True
        sidecar.click = lambda x, y: self.fixture.events.append(f"sidecar.click:{x}:{y}")
        sidecar._LAST_ACTIVATE_MONOTONIC_BY_HWND.clear()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        sidecar.ctypes.windll = self.originals["windll"]
        sidecar.win32gui = self.originals["win32gui"]
        sidecar.win32process = self.originals["win32process"]
        sidecar.win32api = self.originals["win32api"]
        sidecar.win32con = self.originals["win32con"]
        sidecar.foreground_window_matches_target = self.originals["foreground_window_matches_target"]
        sidecar.require_active_ui_action_budget = self.originals["require_active_ui_action_budget"]
        sidecar.humanized_action_sleep = self.originals["humanized_action_sleep"]
        sidecar.coordinate_rpa_action = self.originals["coordinate_rpa_action"]
        sidecar.focus_click_fallback_enabled = self.originals["focus_click_fallback_enabled"]
        sidecar.click = self.originals["click"]
        sidecar._LAST_ACTIVATE_MONOTONIC_BY_HWND.clear()
        sidecar._LAST_ACTIVATE_MONOTONIC_BY_HWND.update(self.originals["last_activate"])
        for name, value in self.previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_activate_window_returns_without_budget_when_foreground_ready() -> None:
    fixture = ActivationFixture()
    with PatchActivation(fixture, focus_results=[{"ok": True, "reason": "foreground_matches_target"}]):
        sidecar.activate_window(1001)
    assert_true("sidecar.require_budget:activate_window:{'hwnd': 1001}" not in fixture.events, f"budget should not run when already focused: {fixture.events}")
    assert_true("user32.SetForegroundWindow:1001" not in fixture.events, f"foreground should not be reset when already focused: {fixture.events}")


def test_activate_window_normal_focus_uses_single_foreground_path() -> None:
    fixture = ActivationFixture()
    with PatchActivation(fixture, focus_results=[{"ok": False, "reason": "foreground_not_wechat_target"}]):
        sidecar.activate_window(1001)
    for expected in (
        "sidecar.require_budget:activate_window:{'hwnd': 1001}",
        "user32.SetForegroundWindow:1001",
        "win32gui.SetForegroundWindow:1001",
        "win32gui.SetActiveWindow:1001",
        "sidecar.sleep:55:95",
    ):
        assert_true(expected in fixture.events, f"missing normal focus event {expected}: {fixture.events}")
    assert_true(not any("AttachThreadInput" in item for item in fixture.events), f"normal focus should not attach threads: {fixture.events}")
    assert_true(not any("keybd_event" in item for item in fixture.events), f"normal focus should not press ALT fallback: {fixture.events}")


def test_activate_window_aggressive_focus_attaches_detaches_and_uses_alt_fallback() -> None:
    fixture = ActivationFixture()
    focus_results = [
        {"ok": False, "reason": "foreground_not_wechat_target"},
        {"ok": False, "reason": "foreground_not_wechat_target"},
        {"ok": True, "reason": "foreground_matches_target"},
    ]
    with PatchActivation(fixture, focus_results=focus_results, aggressive=True, attach=True):
        sidecar.activate_window(1001)
    expected_events = [
        "user32.BringWindowToTop:1001",
        "win32process.AttachThreadInput:10:20:1",
        "win32process.AttachThreadInput:30:20:1",
        "win32gui.SetFocus:1001",
        "win32gui.SetWindowPos:1001:-1",
        "win32gui.SetWindowPos:1001:-2",
        "win32process.AttachThreadInput:10:20:0",
        "win32process.AttachThreadInput:30:20:0",
        "sidecar.coordinate:key_press:{'key': 18, 'context': 'focus_alt_down'}",
        "win32api.keybd_event:18:0:0:0",
        "sidecar.coordinate:key_press:{'key': 18, 'context': 'focus_alt_up'}",
        "win32api.keybd_event:18:0:2:0",
    ]
    for expected in expected_events:
        assert_true(expected in fixture.events, f"missing aggressive focus event {expected}: {fixture.events}")
    assert_true(not any(item.startswith("sidecar.click:") for item in fixture.events), f"ready after ALT should not click fallback: {fixture.events}")


def main() -> int:
    tests = [
        test_activate_window_returns_without_budget_when_foreground_ready,
        test_activate_window_normal_focus_uses_single_foreground_path,
        test_activate_window_aggressive_focus_attaches_detaches_and_uses_alt_fallback,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR window activation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
