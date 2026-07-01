"""Replay WeChat screenshot OCR through sender-role classification.

This check intentionally does not print recognized message text. Some replay
screenshots may contain private customer data; assertions use geometry and role
metadata only.

Set these environment variables to run against local screenshots:

- WECHAT_WIN32_OCR_SENDER_ROLE_LIGHT_SCREENSHOT
- WECHAT_WIN32_OCR_SENDER_ROLE_DARK_SCREENSHOT

Set WECHAT_WIN32_OCR_SENDER_ROLE_REPLAY_REQUIRE_INPUTS=1 to fail instead of
skipping when screenshots are not supplied.
Set WECHAT_WIN32_OCR_SENDER_ROLE_REPLAY_REQUIRE_OCR=1 to fail instead of
skipping when RapidOCR is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (  # noqa: E402
    parse_messages_from_ocr,
    run_ocr,
    session_split_x,
)


LIGHT_SCREENSHOT_ENV = "WECHAT_WIN32_OCR_SENDER_ROLE_LIGHT_SCREENSHOT"
DARK_SCREENSHOT_ENV = "WECHAT_WIN32_OCR_SENDER_ROLE_DARK_SCREENSHOT"


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def replay_screenshot(name: str, path: Path, *, target: str, require_legacy_left_downgrade: bool = False) -> dict[str, Any]:
    image = Image.open(path).convert("RGB")
    width, _height = image.size
    try:
        ocr_items = run_ocr(image)
    except RuntimeError as exc:
        if "rapidocr_onnxruntime_unavailable" in str(exc) and not env_flag("WECHAT_WIN32_OCR_SENDER_ROLE_REPLAY_REQUIRE_OCR"):
            return {"name": name, "status": "skipped", "reason": "rapidocr_unavailable"}
        raise
    messages = parse_messages_from_ocr(ocr_items, image.size, target=target)
    assert_true(ocr_items, f"{name}: OCR should produce items")
    assert_true(messages, f"{name}: OCR replay should produce messages")

    split_x = session_split_x(width)
    legacy_left_hint_min = max(float(split_x + 75), float(width) * 0.43)
    right_self_lane_min = max(float(split_x + 260), float(width) * 0.72)

    self_count = 0
    non_self_count = 0
    downgraded_legacy_left = 0
    right_self_count = 0
    left_non_self_count = 0
    for message in messages:
        rect = message.get("bubble_rect") if isinstance(message.get("bubble_rect"), dict) else {}
        left = float(rect.get("left") or 0)
        right = float(rect.get("right") or 0)
        sender = str(message.get("sender") or "")
        if sender == "self":
            self_count += 1
            assert_true(
                left >= legacy_left_hint_min and right >= right_self_lane_min,
                f"{name}: self message must have right-side start and lane geometry, rect={rect}",
            )
            right_self_count += 1
        else:
            non_self_count += 1
            if right < right_self_lane_min:
                left_non_self_count += 1
            if left >= legacy_left_hint_min and right < right_self_lane_min:
                downgraded_legacy_left += 1

    assert_true(self_count >= 1, f"{name}: expected at least one right-side self message")
    assert_true(non_self_count >= 1, f"{name}: expected at least one left/peer non-self message")
    assert_true(right_self_count >= 1, f"{name}: expected right-side self geometry")
    assert_true(left_non_self_count >= 1, f"{name}: expected left/peer non-self geometry")
    if require_legacy_left_downgrade:
        assert_true(
            downgraded_legacy_left >= 1,
            f"{name}: expected a legacy-left-hint peer message to remain non-self",
        )
    return {
        "name": name,
        "status": "passed",
        "image_size": list(image.size),
        "ocr_items": len(ocr_items),
        "messages": len(messages),
        "self_count": self_count,
        "non_self_count": non_self_count,
        "downgraded_legacy_left": downgraded_legacy_left,
    }


def main() -> int:
    light_path = Path(os.getenv(LIGHT_SCREENSHOT_ENV) or "")
    dark_path = Path(os.getenv(DARK_SCREENSHOT_ENV) or "")
    require_inputs = env_flag("WECHAT_WIN32_OCR_SENDER_ROLE_REPLAY_REQUIRE_INPUTS")
    if not light_path.is_file() or not dark_path.is_file():
        missing = [
            name
            for name, path in ((LIGHT_SCREENSHOT_ENV, light_path), (DARK_SCREENSHOT_ENV, dark_path))
            if not path.is_file()
        ]
        if require_inputs:
            raise FileNotFoundError(f"missing screenshot replay inputs: {', '.join(missing)}")
        print({"status": "skipped", "reason": "missing_screenshot_inputs", "missing": missing})
        return 0

    results = [
        replay_screenshot("light", light_path, target="sender-role-light-replay", require_legacy_left_downgrade=True),
        replay_screenshot("dark", dark_path, target="sender-role-dark-replay"),
    ]
    print({"status": "passed", "results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
