"""Compatibility entry points for the shared message rules."""

from app.contracts.shared_rules import shared_adapter


def canonical_reply_text(value: object) -> str:
    return shared_adapter("message_contract").canonical_reply_text(value)


def reply_text_hash(value: object) -> str:
    return shared_adapter("message_contract").reply_text_hash(value)


def canonical_message_identity_text(value: object) -> str:
    return shared_adapter("message_contract").canonical_message_identity_text(value)


def normalize_voice_duration(value: object) -> str:
    return shared_adapter("message_contract").normalize_voice_duration(value)
