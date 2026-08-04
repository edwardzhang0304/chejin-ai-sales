from __future__ import annotations

import os
from typing import Any


def auto_voice_transcription_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("voice_transcription") if isinstance(config.get("voice_transcription"), dict) else {}
    env_value = os.getenv("WECHAT_AUTO_VOICE_TRANSCRIBE")
    enabled = raw.get("enabled", True)
    if env_value is not None and env_value.strip():
        enabled = env_value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        max_attempts = int(
            os.getenv("WECHAT_AUTO_VOICE_TRANSCRIBE_MAX_ATTEMPTS")
            or raw.get("max_attempts")
            or 4
        )
    except (TypeError, ValueError):
        max_attempts = 4
    artifact_dir = str(
        raw.get("artifact_dir")
        or os.getenv("WECHAT_AUTO_VOICE_TRANSCRIBE_ARTIFACT_DIR")
        or ""
    ).strip()
    return {
        "enabled": bool(enabled),
        "max_attempts": max(1, min(max_attempts, 8)),
        "artifact_dir": artifact_dir,
    }


def maybe_auto_transcribe_voice_messages(
    *,
    connector: Any,
    target: Any,
    config: dict[str, Any],
    console_settings: dict[str, Any],
    conversation_type: str = "",
    pending_signal_kind: str = "",
) -> dict[str, Any]:
    settings = auto_voice_transcription_settings(config)
    if not settings.get("enabled"):
        return {"attempted": False, "enabled": False, "reason": "voice_transcription_disabled"}
    if console_settings.get("enabled") is False:
        return {"attempted": False, "enabled": True, "reason": "customer_service_disabled"}
    signal_kind = str(pending_signal_kind or "").strip().lower()
    if signal_kind in {"image_capture", "media_capture"}:
        return {
            "attempted": False,
            "enabled": True,
            "ok": True,
            "state": "voice_transcription_skipped_non_voice_signal",
            "reason": "pending_signal_is_non_voice_media",
            "pending_signal_kind": signal_kind,
        }
    transcribe = getattr(connector, "transcribe_voice_messages", None)
    if not callable(transcribe):
        return {
            "attempted": False,
            "enabled": True,
            "reason": "connector_voice_transcription_not_supported",
        }
    try:
        result = transcribe(
            target.name,
            exact=target.exact,
            session_key=str(getattr(target, "session_key", "") or ""),
            conversation_type=str(conversation_type or ""),
            max_attempts=int(settings.get("max_attempts") or 4),
            artifact_dir=str(settings.get("artifact_dir") or "") or None,
        )
    except Exception as exc:
        return {
            "attempted": True,
            "enabled": True,
            "ok": False,
            "state": "voice_transcription_exception",
            "error": repr(exc),
        }
    if isinstance(result, dict):
        normalized = dict(result)
        normalized["attempted"] = True
        normalized["enabled"] = True
        return normalized
    return {
        "attempted": True,
        "enabled": True,
        "ok": False,
        "state": "voice_transcription_invalid_result",
    }
