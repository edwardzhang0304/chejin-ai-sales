"""Render and capture diagnostics for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any

from PIL import ImageStat

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import (
    MIN_CAPTURE_WINDOW_HEIGHT,
    MIN_CAPTURE_WINDOW_WIDTH,
)


BLANK_RENDER_BRIGHT_MIN = 238.0
BLANK_RENDER_DARK_MAX = 18.0
BLANK_RENDER_STDDEV_MAX = 8.0
BLANK_RENDER_DENSE_RATIO_MIN = 0.93
BLANK_RENDER_BORDERED_BRIGHT_MIN = 245.0
BLANK_RENDER_BORDERED_DENSE_RATIO_MIN = 0.965
FOREIGN_CAPTURE_TOKENS = (
    "apps/wechat_ai_customer_servic",
    "new project",
    "展开显示",
    "文件已更改",
    "serverchan",
    "要求后续变更",
)
WINDOW_HEALTH_CHAT_TOKENS = ("搜索", "文件传输助手", "发送", "聊天", "通讯录")


def window_content_health_score_from_signals(
    ocr_items: list[dict[str, Any]],
    *,
    blank_render_detected: bool,
    quick_login_detected: bool,
    auxiliary_shell_detected: bool,
    blocking_reason: str,
    text_normalizer: Any,
) -> int:
    if blank_render_detected:
        return -100
    if quick_login_detected:
        return -20
    if auxiliary_shell_detected:
        return -50
    if blocking_reason:
        return -10
    texts: list[str] = []
    for item in ocr_items or []:
        text = str(text_normalizer(item.get("text")) or "")
        if text:
            texts.append(text)
    token_score = 15 if any(token in text for text in texts for token in WINDOW_HEALTH_CHAT_TOKENS) else 0
    return min(80, 20 + min(len(texts), 30) + token_score)


def detect_blank_render(
    screenshot: Any,
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    ocr_count = len(ocr_items or [])
    if ocr_count > 0:
        return {
            "detected": False,
            "reason": "",
            "ocr_count": ocr_count,
            "metrics": {},
        }
    try:
        gray = screenshot.convert("L")
        stat = ImageStat.Stat(gray)
        mean = float((stat.mean or [0.0])[0])
        stddev = float((stat.stddev or [0.0])[0])
        histogram = gray.histogram()
        total = max(1, int(sum(histogram)))
        bright_ratio = float(sum(histogram[245:])) / total
        dark_ratio = float(sum(histogram[:10])) / total
    except Exception as exc:
        return {
            "detected": False,
            "reason": "render_metric_probe_failed",
            "error": repr(exc),
            "ocr_count": ocr_count,
            "metrics": {},
        }
    bright_blank = (
        mean >= BLANK_RENDER_BRIGHT_MIN
        and stddev <= BLANK_RENDER_STDDEV_MAX
        and bright_ratio >= BLANK_RENDER_DENSE_RATIO_MIN
    )
    dark_blank = (
        mean <= BLANK_RENDER_DARK_MAX
        and stddev <= BLANK_RENDER_STDDEV_MAX
        and dark_ratio >= BLANK_RENDER_DENSE_RATIO_MIN
    )
    bordered_bright_blank = (
        mean >= BLANK_RENDER_BORDERED_BRIGHT_MIN
        and bright_ratio >= BLANK_RENDER_BORDERED_DENSE_RATIO_MIN
        and int(geometry.get("width") or screenshot.size[0]) >= MIN_CAPTURE_WINDOW_WIDTH
        and int(geometry.get("height") or screenshot.size[1]) >= MIN_CAPTURE_WINDOW_HEIGHT
    )
    detected = bool(bright_blank or dark_blank or bordered_bright_blank)
    if bright_blank:
        reason = "blank_white_like"
    elif dark_blank:
        reason = "blank_dark_like"
    elif bordered_bright_blank:
        reason = "blank_bordered_white_like"
    else:
        reason = ""
    return {
        "detected": detected,
        "reason": reason,
        "ocr_count": ocr_count,
        "metrics": {
            "mean": round(mean, 3),
            "stddev": round(stddev, 3),
            "bright_ratio": round(bright_ratio, 4),
            "dark_ratio": round(dark_ratio, 4),
            "width": int(geometry.get("width") or screenshot.size[0]),
            "height": int(geometry.get("height") or screenshot.size[1]),
        },
        "thresholds": {
            "bright_min": BLANK_RENDER_BRIGHT_MIN,
            "dark_max": BLANK_RENDER_DARK_MAX,
            "stddev_max": BLANK_RENDER_STDDEV_MAX,
            "dense_ratio_min": BLANK_RENDER_DENSE_RATIO_MIN,
            "bordered_bright_min": BLANK_RENDER_BORDERED_BRIGHT_MIN,
            "bordered_dense_ratio_min": BLANK_RENDER_BORDERED_DENSE_RATIO_MIN,
        },
    }


def image_information_score(image: Any) -> float:
    try:
        gray = image.convert("L")
        stat = ImageStat.Stat(gray)
        std = float(stat.stddev[0]) if stat.stddev else 0.0
        extrema = stat.extrema[0] if stat.extrema else (0, 0)
        contrast = float(extrema[1] - extrema[0])
        return std + contrast * 0.02
    except Exception:
        return 0.0


def likely_foreign_overlay_capture(ocr_items: list[dict[str, Any]]) -> bool:
    if not ocr_items:
        return False
    joined = "\n".join(str(item.get("text") or "").lower() for item in ocr_items)
    hits = sum(1 for token in FOREIGN_CAPTURE_TOKENS if token in joined)
    return hits >= 2
