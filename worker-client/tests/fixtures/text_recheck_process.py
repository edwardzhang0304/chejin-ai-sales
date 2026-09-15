"""Real Sidecar CLI/OCR in a child process; only Windows hardware is replaced.

PRIVATE fixture inputs are supplied through TEXT_RECHECK_FIXTURE. Never imports
checkpoint/history/expected text. Any unexpected UI action aborts the replay.
"""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "omniauto-rpa"))
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s

fixture = json.loads(Path(os.environ["TEXT_RECHECK_FIXTURE"]).read_text())
calibration = fixture["calibration"]
hwnd = calibration["hwnd"]
geometry = fixture["geometry"]
client = fixture["client_geometry"]
source = fixture["image_path"]
calls = Path(os.environ["TEXT_RECHECK_CALLS"])


def record(action):
    with calls.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"action": action, "pid": os.getpid(), "argv": sys.argv[1:]}) + "\n")


def forbidden(*args, **kwargs):
    record("forbidden_ui_action")
    raise AssertionError("No WeChat interaction is permitted in this replay")


def capture(window, *, artifact_dir=None, label="messages", **kwargs):
    record("capture")
    image = Image.open(source).convert("RGB")
    path = Path(artifact_dir) / f"{label}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    s._register_layout_snapshot(window, image, capture_mode=s.win32_ocr_layout.CAPTURE_MODE_CLIENT_AREA,
        screenshot_path=str(path), capture_screen_origin=[client["screen_left"], client["screen_top"]])
    return image, str(path)


s._WIN32_IMPORT_ERROR = ""
s.win32process = SimpleNamespace(GetWindowThreadProcessId=lambda window: (1, calibration["process_id"]))
s.get_window_geometry = lambda window: geometry
s.get_window_client_geometry = lambda window: client
s.window_dpi_scale = lambda window: calibration["dpi_scale"]
s.screen_work_area = lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1080}
window = {"hwnd": hwnd, "visible": True, "title": "微信", "class_name": "WeChatMainWndForPC", **geometry}
s.ensure_visible_wechat_window = lambda **kwargs: {"visible_main_windows": [window], "visible_windows": [window]}
s.capture_wechat = capture
for name in ("activate_window", "scroll_chat_history", "scroll_chat_to_latest", "click_screen_point", "click_client_point", "send_payload"):
    setattr(s, name, forbidden)
if not any(flag in sys.argv for flag in ("--text-recheck-request", "--text-recheck-capture")):
    s.activate_window = lambda *args, **kwargs: record("normal_read_activation")
Path(s.STARTUP_CALIBRATION_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(s.STARTUP_CALIBRATION_PATH).write_text(json.dumps(calibration), encoding="utf-8")
record("start")
result = s.run_sidecar_cli(sys.argv[1:])
print(json.dumps(result, ensure_ascii=True, default=str))
