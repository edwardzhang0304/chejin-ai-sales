from __future__ import annotations

import hashlib
import re
import unicodedata


def canonical_reply_text(value: object) -> str:
    """Return the single reply-text representation shared by backend contracts."""

    return " ".join(str(value or "").split())


def reply_text_hash(value: object) -> str:
    return hashlib.sha256(canonical_reply_text(value).encode("utf-8")).hexdigest()


_WHITESPACE_RUN = re.compile(r"\s+")


def _is_east_asian_text_or_punctuation(value: str) -> bool:
    if not value:
        return False
    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
        or unicodedata.category(value).startswith("P")
    )


def canonical_message_identity_text(value: object) -> str:
    """Mirror the Worker OCR-layout identity normalization exactly."""

    text = str(value or "").strip()

    def replace_whitespace(match: re.Match[str]) -> str:
        run = match.group(0)
        if "\n" not in run and "\r" not in run:
            return " "
        previous = text[match.start() - 1] if match.start() else ""
        following = text[match.end()] if match.end() < len(text) else ""
        if _is_east_asian_text_or_punctuation(
            previous
        ) or _is_east_asian_text_or_punctuation(following):
            return ""
        return " "

    return _WHITESPACE_RUN.sub(replace_whitespace, text)
