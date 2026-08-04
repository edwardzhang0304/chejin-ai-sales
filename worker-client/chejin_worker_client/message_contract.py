from __future__ import annotations

import hashlib


def canonical_reply_text(value: object) -> str:
    """Mirror the frozen backend reply-text contract, including NBSP handling."""

    return " ".join(str(value or "").split())


def reply_text_hash(value: object) -> str:
    return hashlib.sha256(canonical_reply_text(value).encode("utf-8")).hexdigest()
