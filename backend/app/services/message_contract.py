from __future__ import annotations

import hashlib


def canonical_reply_text(value: object) -> str:
    """Return the single reply-text representation shared by backend contracts."""

    return " ".join(str(value or "").split())


def reply_text_hash(value: object) -> str:
    return hashlib.sha256(canonical_reply_text(value).encode("utf-8")).hexdigest()
