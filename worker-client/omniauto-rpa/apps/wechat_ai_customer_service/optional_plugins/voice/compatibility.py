from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.optional_plugins.registry import resolve_optional_capability


def legacy_auto_voice_transcription_settings(config: dict[str, Any]) -> dict[str, Any]:
    from .transcription import auto_voice_transcription_settings

    return auto_voice_transcription_settings(config)


def legacy_voice_transcription_trigger(
    payload: dict[str, Any] | None,
    *,
    pending_signal_kind: str = "",
) -> dict[str, Any]:
    plugin = resolve_optional_capability("voice")
    if plugin is None:
        return {
            "should_run": False,
            "reason": "voice_capability_unavailable",
            "pending_signal_kind": str(pending_signal_kind or "").strip().lower(),
        }
    return plugin.should_run(
        {
            "payload": payload,
            "pending_signal_kind": pending_signal_kind,
        }
    )


def legacy_maybe_auto_transcribe_voice_messages(
    *,
    connector: Any,
    target: Any,
    config: dict[str, Any],
    console_settings: dict[str, Any],
    conversation_type: str = "",
    pending_signal_kind: str = "",
) -> dict[str, Any]:
    plugin = resolve_optional_capability("voice")
    if plugin is None:
        return {
            "attempted": False,
            "enabled": False,
            "reason": "voice_capability_unavailable",
        }
    return plugin.run(
        {
            "connector": connector,
            "target": target,
            "config": config,
            "console_settings": console_settings,
            "conversation_type": conversation_type,
            "pending_signal_kind": pending_signal_kind,
        }
    )


def legacy_attach_voice_transcription_audit(
    payload: dict[str, Any],
    voice_transcription: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(payload, dict) and voice_transcription and voice_transcription.get("attempted"):
        payload["voice_transcription"] = voice_transcription
    return payload
