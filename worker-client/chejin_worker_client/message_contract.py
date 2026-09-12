"""Compatibility entry points for the shared message rules."""

from .shared_rules import message_contract as _rules


def canonical_reply_text(value: object) -> str:
    return _rules.canonical_reply_text(value)


def reply_text_hash(value: object) -> str:
    return _rules.reply_text_hash(value)


def canonical_message_identity_text(value: object) -> str:
    return _rules.canonical_message_identity_text(value)


def normalize_voice_duration(value: object) -> str:
    return _rules.normalize_voice_duration(value)
