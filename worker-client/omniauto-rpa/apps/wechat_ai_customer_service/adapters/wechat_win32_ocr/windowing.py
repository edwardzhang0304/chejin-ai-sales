"""Pure window metadata helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import re
from typing import Any


def normalize_wechat_title(title: str) -> str:
    text = str(title or "").strip()
    text = re.sub(r"^\(\d+\)\s*", "", text).strip()
    text = re.sub(r"^（\d+）\s*", "", text).strip()
    return text


def is_wechat_main_window(item: dict[str, Any]) -> bool:
    title = normalize_wechat_title(str(item.get("title") or ""))
    class_name = str(item.get("class_name") or "").lower()
    if not title:
        return False
    if "qwindowicon" not in class_name and "wechatmainwndforpc" not in class_name:
        return False
    lowered = title.lower()
    if any(token in lowered for token in ("login", "qr", "update")) or any(token in title for token in ("登录", "扫码", "更新")):
        return False
    return any(token in title for token in ("微信", "Weixin", "WeChat")) or any(token in lowered for token in ("weixin", "wechat"))


def wechat_window_title_score(item: dict[str, Any]) -> int:
    title = normalize_wechat_title(str(item.get("title") or ""))
    lowered = title.lower()
    if title == "微信" or title.startswith("微信"):
        return 40
    if "微信" in title:
        return 35
    if lowered.startswith("wechat"):
        return 25
    if lowered.startswith("weixin"):
        return 10
    return 0
