"""OCR text helpers for add_friend RPA."""

from __future__ import annotations

import re
from typing import Any


def normalize_ocr_text_value(text: Any) -> str:
    clean = str(text or "").replace("\u3000", " ").strip()
    return re.sub(r"\s+", " ", clean)


def compact_ocr_text(text: Any) -> str:
    return re.sub(r"\s+", "", normalize_ocr_text_value(text)).lower()


def ocr_item_text(item: dict[str, Any]) -> str:
    return compact_ocr_text(item.get("text"))


def ocr_surface_text(ocr_items: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        text = ocr_item_text(item)
        if text:
            texts.append(text)
    return "\n".join(texts)


def ocr_text_has_any(text: str, tokens: tuple[str, ...]) -> bool:
    compact_text = compact_ocr_text(text)
    return any(compact_ocr_text(token) in compact_text for token in tokens if compact_ocr_text(token))


def matched_ocr_tokens(text: str, tokens: tuple[str, ...]) -> list[str]:
    compact_text = compact_ocr_text(text)
    return [token for token in tokens if compact_ocr_text(token) in compact_text]
