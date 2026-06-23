"""Pure text normalization helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

import re
from typing import Any


LOGIN_WINDOW_MAX_WIDTH = 560
LOGIN_WINDOW_MAX_HEIGHT = 680


def normalize_ocr_text(text: Any) -> str:
    clean = str(text or "").replace("\u3000", " ").strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean


def normalize_session_name(text: str) -> str:
    clean = normalize_ocr_text(text)
    clean = re.sub(r"^[：:.\s]+", "", clean).strip()
    return clean


def strip_chat_unread_suffix(text: str) -> str:
    clean = normalize_ocr_text(text)
    if not clean:
        return ""
    return re.sub(r"\s*[\(（]\s*\d{1,4}\s*[\)）]\s*$", "", clean).strip()


def normalize_chat_title_for_match(text: str) -> str:
    clean = strip_chat_unread_suffix(normalize_session_name(text))
    if not clean:
        return ""
    compact = re.sub(r"[\s:：\-_·|]+", "", clean).strip().lower()
    if not compact:
        return ""
    prefixes = (
        "当前会话",
        "聊天对象",
        "与",
        "和",
        "跟",
        "chatwith",
        "conversationwith",
        "with",
    )
    suffixes = (
        "的聊天",
        "聊天窗口",
        "聊天",
        "会话",
        "对话",
        "chatwindow",
        "conversation",
        "chat",
    )
    changed = True
    while changed and compact:
        changed = False
        for token in prefixes:
            if compact.startswith(token) and len(compact) > len(token):
                compact = compact[len(token) :]
                changed = True
        for token in suffixes:
            if compact.endswith(token) and len(compact) > len(token):
                compact = compact[: -len(token)]
                changed = True
    return compact.strip()


def canonical_session_name(text: str) -> str:
    clean = normalize_session_name(text)
    if not clean:
        return ""
    collapsed = re.sub(r"[\s_\-:：()\[\]（）]+", "", clean).lower()
    if is_file_transfer_session_alias(clean, collapsed=collapsed):
        return "__file_transfer_assistant__"
    return collapsed


def is_file_transfer_session_alias(text: str, *, collapsed: str | None = None) -> bool:
    clean = normalize_session_name(text)
    if not clean:
        return False
    compact = re.sub(r"\s+", "", clean)
    if compact.startswith("文件传输助"):
        return True
    if compact.startswith("文件传输") and re.search(r"(\.{1,3}|…|今天|昨天|前天|\d{1,2}:\d{2})", compact):
        return True
    if compact in {"文件传输助手", "仅传输文件"}:
        return True
    english = collapsed
    if english is None:
        english = re.sub(r"[^a-z]", "", clean.lower())
    if not english:
        return False
    return english in {
        "filetransferassistant",
        "filetransfer",
        "transferassistant",
    }


def normalize_message_content(text: str) -> str:
    return str(text or "").strip()


def quick_login_like(ocr_items: list[dict[str, Any]], *, geometry: dict[str, Any]) -> bool:
    width = int(geometry.get("width") or 0)
    height = int(geometry.get("height") or 0)
    texts = [normalize_ocr_text(item.get("text")) for item in ocr_items if normalize_ocr_text(item.get("text"))]
    joined = "\n".join(texts)
    login_tokens = ("进入微信", "切换账号", "仅传输文件")
    has_login_tokens = sum(1 for token in login_tokens if token in joined) >= 2
    likely_login_size = width <= LOGIN_WINDOW_MAX_WIDTH and height <= LOGIN_WINDOW_MAX_HEIGHT
    return bool(has_login_tokens and likely_login_size)


def session_name_matches(name: str, target: str, *, exact: bool) -> bool:
    normalized_name = normalize_session_name(name)
    normalized_target = normalize_session_name(target)
    if not normalized_name or not normalized_target:
        return False
    stripped_name = strip_session_time_suffix(normalized_name)
    canonical_name = canonical_session_name(normalized_name)
    canonical_target = canonical_session_name(normalized_target)
    if canonical_name and canonical_target and canonical_name == canonical_target:
        return True
    stripped_canonical_name = canonical_session_name(stripped_name)
    if stripped_canonical_name and canonical_target and stripped_canonical_name == canonical_target:
        return True
    if exact:
        if normalized_name == normalized_target or stripped_name == normalized_target:
            return True
        wrapped_name = normalize_chat_title_for_match(normalized_name)
        stripped_wrapped_name = normalize_chat_title_for_match(stripped_name)
        wrapped_target = normalize_chat_title_for_match(normalized_target)
        return bool(
            wrapped_target
            and (
                (wrapped_name and wrapped_name == wrapped_target)
                or (stripped_wrapped_name and stripped_wrapped_name == wrapped_target)
            )
        )
    return normalized_target in normalized_name or normalized_name in normalized_target


def strip_session_time_suffix(name: str) -> str:
    normalized = normalize_session_name(name)
    if not normalized:
        return ""
    patterns = (
        r"(?:今天|昨天|前天)?\d{1,2}:\d{2}$",
        r"(?:今天|昨天|前天)$",
        r"(?:星期|周)[一二三四五六日天]$",
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}$",
        r"\d{1,2}[/-]\d{1,2}$",
    )
    stripped = normalized
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            updated = re.sub(pattern, "", stripped).strip()
            if updated != stripped:
                stripped = updated
                changed = True
    return stripped or normalized


def is_session_name_candidate(text: str) -> bool:
    if not text:
        return False
    if len(text) > 28:
        return False
    if text.startswith("["):
        return False
    if "搜索" in text or text in {"?", "？", "+", "..."}:
        return False
    if re.fullmatch(r"(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|星期.|(今天|昨天|前天)\s*\d{1,2}:\d{2})", text):
        return False
    if "..." in text or "…" in text:
        return False
    return True


def is_session_time_text(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|星期.|(今天|昨天|前天)\s*\d{1,2}:\d{2})",
            str(text or "").strip(),
        )
    )


def is_message_noise(text: str) -> bool:
    if re.fullmatch(r"(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|(今天|昨天|前天)\s*\d{1,2}:\d{2}|星期.\s*\d{1,2}:\d{2}|星期.)", text):
        return True
    if text in {"发送", "按住 Alt 说话"}:
        return True
    return False


def infer_conversation_type(name: str) -> str:
    if is_file_transfer_session_alias(name):
        return "file_transfer"
    if re.search(r"(群|群聊|测试|chatroom|room)", name, re.IGNORECASE):
        return "group"
    return "private"
