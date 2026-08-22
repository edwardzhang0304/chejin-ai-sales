from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import sys

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout


TARGET_HWND = 101
OTHER_HWND = 909
TARGET_PID = 202


def assert_true(value: object, message: str) -> None:
    if not value:
        raise AssertionError(message)


def shell_image(width: int = 800, height: int = 812) -> Image.Image:
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    nav_x = int(width * 0.09)
    sidebar_x = int(width * 0.38)
    header_y = int(height * 0.085)
    input_y = int(height * 0.77)
    draw.rectangle((0, 0, nav_x, height), fill=(226, 226, 226))
    draw.rectangle((nav_x + 1, 0, sidebar_x, height), fill=(242, 242, 242))
    draw.rectangle((sidebar_x + 1, 0, width, height), fill=(250, 250, 250))
    draw.line((nav_x, 0, nav_x, height), fill=(190, 190, 190), width=2)
    draw.line((sidebar_x, 0, sidebar_x, height), fill=(185, 185, 185), width=2)
    draw.line((nav_x, header_y, width, header_y), fill=(188, 188, 188), width=2)
    draw.line((sidebar_x, input_y, width, input_y), fill=(180, 180, 180), width=2)
    return image


def build_real_calibration() -> dict[str, object]:
    image = shell_image()
    calibration = window_layout.build_startup_layout_calibration(
        hwnd=TARGET_HWND,
        process_id=TARGET_PID,
        image=image,
        ocr_items=[{
            "text": "Q搜索",
            "left": 92,
            "top": 24,
            "right": 155,
            "bottom": 44,
            "center_x": 123.5,
            "center_y": 34.0,
            "confidence": 0.96,
        }],
        window_rect={"left": 12, "top": 12, "right": 812, "bottom": 864},
        client_rect={"width": image.width, "height": image.height},
        client_screen_origin=[20, 50],
        dpi_scale=1.0,
        capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
    )
    assert_true(calibration.get("executable"), json.dumps(calibration, ensure_ascii=False))
    return calibration


class ForegroundBoundary:
    def __init__(self, *, activation_succeeds: bool) -> None:
        self.foreground = OTHER_HWND
        self.activation_succeeds = activation_succeeds
        self.events: list[str] = []

    def IsWindow(self, hwnd: int) -> bool:
        self.events.append(f"IsWindow:{hwnd}")
        return int(hwnd) == TARGET_HWND

    def IsWindowVisible(self, hwnd: int) -> bool:
        self.events.append(f"IsWindowVisible:{hwnd}")
        return int(hwnd) == TARGET_HWND

    def IsIconic(self, hwnd: int) -> int:
        self.events.append(f"IsIconic:{hwnd}")
        return 0

    def ShowWindow(self, hwnd: int, mode: int) -> None:
        self.events.append(f"ShowWindow:{hwnd}:{mode}")

    def SetForegroundWindow(self, hwnd: int) -> None:
        self.events.append(f"SetForegroundWindow:{hwnd}")
        if self.activation_succeeds:
            self.foreground = int(hwnd)

    def GetForegroundWindow(self) -> int:
        self.events.append(f"GetForegroundWindow:{self.foreground}")
        return self.foreground

    def GetAncestor(self, hwnd: int, _flag: int) -> int:
        self.events.append(f"GetAncestor:{hwnd}")
        return int(hwnd)

    def SetActiveWindow(self, hwnd: int) -> None:
        self.events.append(f"SetActiveWindow:{hwnd}")

    def MoveWindow(self, hwnd: int, *_args) -> None:
        self.events.append(f"MoveWindow:{hwnd}")


class PatchProductionBoundaries:
    def __init__(self, directory: str, *, activation_succeeds: bool) -> None:
        self.directory = directory
        self.boundary = ForegroundBoundary(activation_succeeds=activation_succeeds)
        self.originals: dict[str, object] = {}
        self.windll_present = hasattr(sidecar.ctypes, "windll")
        self.original_windll = getattr(sidecar.ctypes, "windll", None)
        self.dispatches: list[tuple[str, int]] = []
        self.forbidden_counts = {
            "move_window": 0,
            "normalize": 0,
            "recalibrate": 0,
            "screenshot": 0,
            "ocr": 0,
            "mouse": 0,
            "keyboard": 0,
            "clipboard": 0,
            "geometry_gate": 0,
        }

    def __enter__(self) -> "PatchProductionBoundaries":
        names = (
            "_WIN32_IMPORT_ERROR",
            "STARTUP_CALIBRATION_PATH",
            "probe_wechat_windows",
            "get_window_geometry",
            "get_window_client_geometry",
            "window_dpi_scale",
            "win32gui",
            "win32process",
            "win32api",
            "win32con",
            "humanized_action_sleep",
            "require_active_ui_action_budget",
            "locate_chat_target_for_c2",
            "send_payload",
            "add_friend_entry_click_plan_payload",
            "status_payload",
            "capabilities_payload",
            "sessions_payload",
            "normalize_wechat_window",
            "build_and_store_startup_calibration",
            "validate_startup_calibration_state",
            "capture_wechat",
            "run_ocr",
            "human_client_click",
            "clipboard_copy",
            "clipboard_read",
            "coordinate_rpa_action",
        )
        self.originals = {name: getattr(sidecar, name) for name in names}
        sidecar._WIN32_IMPORT_ERROR = ""
        sidecar.STARTUP_CALIBRATION_PATH = Path(self.directory) / "startup.json"
        window_layout.write_startup_layout_calibration(
            sidecar.STARTUP_CALIBRATION_PATH,
            build_real_calibration(),
        )
        probe = {
            "windows": [{
                "hwnd": TARGET_HWND,
                "pid": TARGET_PID,
                "title": "微信",
                "class_name": "WeChatMainWndForPC",
                "visible": True,
            }],
            "visible_windows": [{"hwnd": TARGET_HWND, "pid": TARGET_PID}],
            "main_windows": [{
                "hwnd": TARGET_HWND,
                "pid": TARGET_PID,
                "title": "微信",
                "class_name": "WeChatMainWndForPC",
                "visible": True,
            }],
            "visible_main_windows": [{
                "hwnd": TARGET_HWND,
                "pid": TARGET_PID,
                "title": "微信",
                "class_name": "WeChatMainWndForPC",
                "visible": True,
            }],
            "main_count": 1,
            "visible_main_count": 1,
        }
        sidecar.probe_wechat_windows = lambda: dict(probe)
        sidecar.get_window_geometry = lambda _hwnd: {
            "left": 12, "top": 12, "right": 812, "bottom": 864,
            "width": 800, "height": 852,
        }
        sidecar.get_window_client_geometry = lambda _hwnd: {
            "width": 800, "height": 812, "screen_left": 20, "screen_top": 50,
        }
        sidecar.window_dpi_scale = lambda _hwnd: 1.0
        sidecar.ctypes.windll = SimpleNamespace(user32=self.boundary)
        sidecar.win32gui = self.boundary
        sidecar.win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (
                11 if int(hwnd) == OTHER_HWND else 22,
                303 if int(hwnd) == OTHER_HWND else TARGET_PID,
            ),
            AttachThreadInput=lambda *_args: None,
        )
        sidecar.win32api = SimpleNamespace(GetCurrentThreadId=lambda: 33)
        sidecar.win32con = SimpleNamespace(
            VK_MENU=0x12,
            KEYEVENTF_KEYUP=0x0002,
            SWP_NOMOVE=0x0002,
            SWP_NOSIZE=0x0001,
            SWP_SHOWWINDOW=0x0040,
            HWND_TOPMOST=-1,
            HWND_NOTOPMOST=-2,
        )
        sidecar.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        sidecar.require_active_ui_action_budget = lambda action, metadata=None: (
            self.boundary.events.append(f"budget:{action}") or {"ok": True}
        )
        sidecar.coordinate_rpa_action = lambda action, **_kwargs: (
            self.forbidden_counts.__setitem__(
                "keyboard" if action == "key_press" else "mouse",
                self.forbidden_counts["keyboard" if action == "key_press" else "mouse"] + 1,
            )
            or {"ok": True}
        )

        def dispatched(name: str, payload: dict[str, object]) -> dict[str, object]:
            self.dispatches.append((name, self.boundary.foreground))
            return payload

        sidecar.locate_chat_target_for_c2 = lambda *_args, **_kwargs: dispatched(
            "C2.open-chat", {"ok": True, "state": "chat_target_confirmed"}
        )
        sidecar.send_payload = lambda *_args, **_kwargs: dispatched(
            "C3.send", {"ok": True, "state": "sent"}
        )
        sidecar.add_friend_entry_click_plan_payload = lambda *_args, **_kwargs: dispatched(
            "C1.add-friend", {"ok": True, "state": "invite_sent"}
        )
        sidecar.status_payload = lambda *_args, **_kwargs: dispatched(
            "passive.status", {"ok": True, "state": "status"}
        )
        sidecar.capabilities_payload = lambda *_args, **_kwargs: dispatched(
            "passive.capabilities", {"ok": True, "state": "capabilities"}
        )
        sidecar.sessions_payload = lambda *_args, **_kwargs: dispatched(
            "passive.sessions", {"ok": True, "state": "sessions", "sessions": []}
        )
        sidecar.normalize_wechat_window = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__("normalize", self.forbidden_counts["normalize"] + 1)
            or {"ok": False}
        )
        sidecar.build_and_store_startup_calibration = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__("recalibrate", self.forbidden_counts["recalibrate"] + 1)
            or {"ok": False}
        )
        sidecar.validate_startup_calibration_state = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__(
                "geometry_gate", self.forbidden_counts["geometry_gate"] + 1
            )
            or {"ok": True, "reason": "diagnostic_only"}
        )
        sidecar.capture_wechat = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__("screenshot", self.forbidden_counts["screenshot"] + 1)
            or (_ for _ in ()).throw(AssertionError("unexpected screenshot before dispatch"))
        )
        sidecar.run_ocr = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__("ocr", self.forbidden_counts["ocr"] + 1)
            or []
        )
        sidecar.human_client_click = lambda *_args, **_kwargs: self.forbidden_counts.__setitem__(
            "mouse", self.forbidden_counts["mouse"] + 1
        )
        sidecar.clipboard_copy = lambda *_args, **_kwargs: self.forbidden_counts.__setitem__(
            "clipboard", self.forbidden_counts["clipboard"] + 1
        )
        sidecar.clipboard_read = lambda *_args, **_kwargs: (
            self.forbidden_counts.__setitem__("clipboard", self.forbidden_counts["clipboard"] + 1)
            or ""
        )
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        for name, value in self.originals.items():
            setattr(sidecar, name, value)
        if self.windll_present:
            sidecar.ctypes.windll = self.original_windll
        else:
            delattr(sidecar.ctypes, "windll")


def action_args(action: str) -> SimpleNamespace:
    values = {
        "action": action,
        "artifact_dir": "",
        "phone": "17368746889",
        "wechat": "",
        "verify_message": "您好，我是车金张文涛",
        "remark_name": "CJTEST01",
        "remark_code": "CJTEST01",
        "calibration_only": False,
        "action_journal": "",
        "target": "CJTEST01",
        "session_key": "session-1",
        "target_mode": "visible",
        "visible_session_candidate": "",
        "exact": False,
        "sidecar_run_id": "run-1",
        "capture_initial_messages": False,
        "text": "测试回复",
        "current_only": True,
        "skip_send_rate_guard": True,
        "expected_send_context_guard": "",
    }
    return SimpleNamespace(**values)


def check_passive_actions_do_not_steal_foreground() -> int:
    with tempfile.TemporaryDirectory() as directory:
        with PatchProductionBoundaries(directory, activation_succeeds=True) as patch:
            for action in ("status", "capabilities", "calibration-status", "sessions"):
                patch.boundary.foreground = OTHER_HWND
                before_foreground_calls = sum(
                    1 for event in patch.boundary.events if event.startswith("SetForegroundWindow:")
                )
                result = sidecar.run_action(action_args(action))
                after_foreground_calls = sum(
                    1 for event in patch.boundary.events if event.startswith("SetForegroundWindow:")
                )
                assert_true(result.get("ok") is True, f"passive {action} failed: {result}")
                assert_true(patch.boundary.foreground == OTHER_HWND, f"passive {action} stole foreground")
                assert_true(
                    after_foreground_calls == before_foreground_calls,
                    f"passive {action} called SetForegroundWindow: {patch.boundary.events}",
                )
    return 4


def check_c1_c2_c3_reuse_v0920_activation_before_dispatch() -> int:
    actions = (
        ("add-friend-entry-click-plan-windows", "C1.add-friend"),
        ("open-chat", "C2.open-chat"),
        ("send", "C3.send"),
    )
    with tempfile.TemporaryDirectory() as directory:
        with PatchProductionBoundaries(directory, activation_succeeds=True) as patch:
            for action, expected_dispatch in actions:
                patch.boundary.foreground = OTHER_HWND
                event_start = len(patch.boundary.events)
                dispatch_start = len(patch.dispatches)
                result = sidecar.run_action(action_args(action))
                action_events = patch.boundary.events[event_start:]
                action_dispatches = patch.dispatches[dispatch_start:]
                assert_true(result.get("ok") is True, f"active {action} failed: {result}")
                assert_true(
                    action_dispatches == [(expected_dispatch, TARGET_HWND)],
                    f"{action} dispatched before target foreground: {action_dispatches}",
                )
                foreground_trace = [
                    event for event in action_events
                    if event.startswith("GetForegroundWindow:") or event.startswith("SetForegroundWindow:")
                ]
                assert_true(
                    foreground_trace[0] == f"GetForegroundWindow:{OTHER_HWND}",
                    f"{action} did not begin with another foreground HWND: {foreground_trace}",
                )
                assert_true(
                    f"SetForegroundWindow:{TARGET_HWND}" in foreground_trace,
                    f"{action} did not activate calibrated WeChat: {foreground_trace}",
                )
                set_index = foreground_trace.index(f"SetForegroundWindow:{TARGET_HWND}")
                assert_true(
                    any(
                        event == f"GetForegroundWindow:{TARGET_HWND}"
                        for event in foreground_trace[set_index + 1:]
                    ),
                    f"{action} did not execute the v0.9.20 activation sequence: {foreground_trace}",
                )
                print(
                    f"TRACE {expected_dispatch}: "
                    f"other_hwnd={OTHER_HWND} -> activate_hwnd={TARGET_HWND} "
                    f"-> confirmed_hwnd={patch.boundary.foreground} -> dispatch"
                )
            assert_true(
                all(value == 0 for value in patch.forbidden_counts.values()),
                f"activation performed a forbidden operation: {patch.forbidden_counts}",
            )
            assert_true(
                not any(event.startswith("MoveWindow:") for event in patch.boundary.events),
                f"activation moved the WeChat window: {patch.boundary.events}",
            )
            print(f"TRACE activation forbidden_counts={patch.forbidden_counts}")
    return 15


def check_no_new_global_activation_success_gate() -> int:
    actions = (
        "add-friend-entry-click-plan-windows",
        "open-chat",
        "send",
    )
    with tempfile.TemporaryDirectory() as directory:
        with PatchProductionBoundaries(directory, activation_succeeds=False) as patch:
            for action in actions:
                patch.boundary.foreground = OTHER_HWND
                dispatch_start = len(patch.dispatches)
                result = sidecar.run_action(action_args(action))
                assert_true(result.get("ok") is True, f"v0.9.20 dispatch was gated for {action}: {result}")
                assert_true(
                    len(patch.dispatches) == dispatch_start + 1,
                    f"{action} did not reach its unchanged business dispatch",
                )
                assert_true(
                    "foreground_activation" not in (result.get("window_probe") or {}),
                    f"{action} retained the removed global activation gate",
                )
            assert_true(
                all(value == 0 for value in patch.forbidden_counts.values()),
                f"failed activation performed a forbidden operation: {patch.forbidden_counts}",
            )
            assert_true(
                not any("MoveWindow" in event or "ShowWindow" in event for event in patch.boundary.events),
                f"failure path moved/restored the window: {patch.boundary.events}",
            )
            print(
                "TRACE no_global_activation_gate: business_dispatches=3, "
                f"forbidden_counts={patch.forbidden_counts}"
            )
    return 25


def check_business_map_binding_uses_visible_hwnd_only() -> int:
    with tempfile.TemporaryDirectory() as directory:
        with PatchProductionBoundaries(directory, activation_succeeds=True) as patch:
            calibration = window_layout.read_startup_layout_calibration(
                sidecar.STARTUP_CALIBRATION_PATH
            ) or {}
            calibration["process_id"] = TARGET_PID + 999
            window_layout.write_startup_layout_calibration(
                sidecar.STARTUP_CALIBRATION_PATH,
                calibration,
            )
            result = sidecar.run_action(action_args("open-chat"))
            assert_true(result.get("ok") is True, str(result))
            assert_true(
                patch.dispatches == [("C2.open-chat", TARGET_HWND)],
                str(patch.dispatches),
            )
            assert_true(
                patch.forbidden_counts["geometry_gate"] == 0,
                str(patch.forbidden_counts),
            )
            assert_true(
                int(calibration.get("hwnd") or 0) == TARGET_HWND,
                str(calibration),
            )
    return 4


def main() -> int:
    checks = 0
    checks += check_passive_actions_do_not_steal_foreground()
    checks += check_c1_c2_c3_reuse_v0920_activation_before_dispatch()
    checks += check_no_new_global_activation_success_gate()
    checks += check_business_map_binding_uses_visible_hwnd_only()
    print(f"v0.9.29 business entry activation checks passed: {checks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
