from __future__ import annotations

import json
import os
from typing import Any


UNICODE_PROTOCOL_SENTINEL = "复制|语音转文字|图片摘要"


def encode_subprocess_json(payload: dict[str, Any]) -> str:
    """Serialize pipe traffic as ASCII so Windows code pages cannot corrupt it."""

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def subprocess_utf8_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def require_unicode_protocol(envelope: dict[str, Any]) -> None:
    if envelope.get("protocol_unicode_sentinel") != UNICODE_PROTOCOL_SENTINEL:
        raise RuntimeError("SUBPROCESS_UNICODE_PROTOCOL_MISMATCH")
