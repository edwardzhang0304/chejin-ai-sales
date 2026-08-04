from __future__ import annotations

from typing import Any


def voice_transcription_trigger(
    payload: dict[str, Any] | None,
    *,
    pending_signal_kind: str = "",
) -> dict[str, Any]:
    """Return the legacy metadata-only decision for voice RPA."""

    signal_kind = str(pending_signal_kind or "").strip().lower()
    if signal_kind in {"image_capture", "media_capture"}:
        return {
            "should_run": False,
            "reason": "pending_signal_is_non_voice_media",
            "pending_signal_kind": signal_kind,
        }
    if signal_kind == "voice_capture":
        return {
            "should_run": True,
            "reason": "pending_signal_is_voice",
            "pending_signal_kind": signal_kind,
        }

    source = payload if isinstance(payload, dict) else {}
    if bool(source.get("voice_transcription_candidate")):
        return {
            "should_run": True,
            "reason": "sidecar_visual_voice_candidate",
            "pending_signal_kind": signal_kind,
            "candidate": source.get("voice_transcription_candidate_evidence") or {},
        }
    for message in source.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or message.get("message_type") or "").strip().lower()
        content = str(message.get("content") or "").strip().lower()
        if message_type in {"voice", "audio"} or content.startswith(
            ("[语音]", "语音", "[voice]", "[audio]")
        ):
            return {
                "should_run": True,
                "reason": "captured_voice_message_evidence",
                "pending_signal_kind": signal_kind,
            }
    return {
        "should_run": False,
        "reason": "no_voice_evidence_in_capture",
        "pending_signal_kind": signal_kind,
    }
