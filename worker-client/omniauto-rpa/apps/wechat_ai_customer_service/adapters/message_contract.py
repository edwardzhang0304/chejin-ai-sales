from __future__ import annotations

import hashlib
import re
import unicodedata


def canonical_reply_text(value: object) -> str:
    """Frozen reply-text contract, including NBSP handling; distinct from OCR identity."""

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
    """Normalize OCR layout whitespace without weakening message semantics.

    WeChat may wrap one CJK bubble into multiple OCR lines.  Such a visual
    newline is not a character sent by the user and must not change the
    cross-round identity hash.  Horizontal whitespace and whitespace between
    ASCII words remain one real space, so this is narrower than deleting all
    whitespace.
    """

    # Preserve the old canonical hash for every value without a visual line
    # break. Existing backend checkpoints therefore remain valid across the
    # upgrade; only OCR-inserted wrapping receives new treatment.
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


def normalize_voice_duration(value: object) -> str:
    text = str(value or "").strip().lower()
    for suffix in ("seconds", "second", "secs", "sec", "秒", "s"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return (
        str(int(number))
        if number.is_integer()
        else format(number, ".3f").rstrip("0").rstrip(".")
    )
