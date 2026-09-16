"""The server owns reply sizing; Brain supplies the complete semantic units."""

from app.contracts.shared_rules import shared_adapter
from app.core.config import get_settings


def sequence_policy() -> dict:
    settings = get_settings()
    return {"reply_sequence_version": 1,
            "reply_sequence_max_chars": settings.c3_reply_segment_max_chars,
            "reply_sequence_max_segments": settings.c3_reply_max_segments}


def sequence_instruction() -> str:
    policy = sequence_policy()
    return shared_adapter("reply_sequence").reply_sequence_instruction(
        max_chars=policy["reply_sequence_max_chars"],
        max_segments=policy["reply_sequence_max_segments"],
    )


def segments_for_decision(text: str, payload: dict) -> list[str]:
    raw = payload.get("raw_payload") or {}
    result = raw.get("omniauto_brain_result") or {}
    plan = result.get("brain_plan") or {}
    policy = sequence_policy()
    return shared_adapter("reply_sequence").pack_reply_sequence(
        text, plan.get("reply_segments"),
        max_chars=policy["reply_sequence_max_chars"],
        max_segments=policy["reply_sequence_max_segments"],
    )
