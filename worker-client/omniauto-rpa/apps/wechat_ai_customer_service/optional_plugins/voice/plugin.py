from __future__ import annotations

from typing import Any


class BuiltinVoicePlugin:
    name = "builtin_wechat_voice_transcription"
    capability = "voice"

    def available(self) -> bool:
        return True

    def should_run(self, context: dict[str, Any]) -> dict[str, Any]:
        from .trigger import voice_transcription_trigger

        return voice_transcription_trigger(
            context.get("payload"),
            pending_signal_kind=str(context.get("pending_signal_kind") or ""),
        )

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        from .transcription import maybe_auto_transcribe_voice_messages

        return maybe_auto_transcribe_voice_messages(
            connector=context.get("connector"),
            target=context.get("target"),
            config=context.get("config") if isinstance(context.get("config"), dict) else {},
            console_settings=(
                context.get("console_settings")
                if isinstance(context.get("console_settings"), dict)
                else {}
            ),
            conversation_type=str(context.get("conversation_type") or ""),
            pending_signal_kind=str(context.get("pending_signal_kind") or ""),
        )


def create_default_voice_plugin() -> BuiltinVoicePlugin:
    return BuiltinVoicePlugin()
