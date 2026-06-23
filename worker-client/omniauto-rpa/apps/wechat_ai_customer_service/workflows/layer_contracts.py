"""Shared layer contracts for Brain First customer-service runtime.

The project separates authority data, Brain-owned visible replies, reviewer
feedback, and code-mechanism metadata. This module keeps the names and small
validation helpers centralized so docs, prompts, and tests do not drift.
"""

from __future__ import annotations

import re
from typing import Any


PRODUCT_MASTER = "product_master"
PRODUCT_SCOPED_FORMAL = "product_scoped_formal"
FORMAL_KNOWLEDGE = "formal_knowledge"
CURRENT_CONVERSATION_FACT = "current_conversation_fact"
SHARED_PUBLIC_STRATEGY = "shared_public_strategy"
LLM_COMMON_SENSE = "llm_common_sense"
STYLE_MEMORY = "style_memory"
AI_EXPERIENCE_POOL = "ai_experience_pool"
REVIEW_CANDIDATE = "review_candidate"
CUSTOMER_SERVICE_BRAIN = "customer_service_brain"
REVIEWER_GUARD = "reviewer_guard"
REVIEWER_QUALITY = "reviewer_quality"
REVIEWER_POLISH = "reviewer_polish"
CODE_MECHANISM = "code_mechanism"
LEGACY_ADVISORY = "legacy_advisory"
TEST_FIXTURE = "test_fixture"
UNKNOWN = "unknown"

CUSTOMER_VISIBLE_REPLY_AUTHOR_LAYER = CUSTOMER_SERVICE_BRAIN
CUSTOMER_VISIBLE_REPLY_SOURCE = "brain_plan.reply_segments"
NO_VISIBLE_REPLY_SOURCE = "none"

FACT_AUTHORIZING_LAYERS = {
    PRODUCT_MASTER,
    PRODUCT_SCOPED_FORMAL,
    FORMAL_KNOWLEDGE,
    CURRENT_CONVERSATION_FACT,
}

STYLE_INFLUENCE_LAYERS = {
    SHARED_PUBLIC_STRATEGY,
    LLM_COMMON_SENSE,
    STYLE_MEMORY,
    AI_EXPERIENCE_POOL,
}

STRATEGY_INFLUENCE_LAYERS = {
    PRODUCT_MASTER,
    PRODUCT_SCOPED_FORMAL,
    FORMAL_KNOWLEDGE,
    CURRENT_CONVERSATION_FACT,
    SHARED_PUBLIC_STRATEGY,
    LLM_COMMON_SENSE,
}

CODE_MECHANISM_FIELDS = {
    "session_key",
    "target_name",
    "conversation_type",
    "capture_id",
    "message_ids",
    "input_content_keys",
    "message_content_digest",
    "message_digest",
    "context_version",
    "last_visible_anchor",
    "reply_id",
    "capture_source",
    "ledger_state_version",
    "send_target_confirmed",
    "freshness_check",
    "send_target_confirmation",
    "unread_signal",
    "preview_signal",
    "ocr_observation",
    "rpa_action_guard",
    "operator_guard",
    "conversation_strategy_state",
    "redirect_fatigue_level",
    "suggested_engagement_mode",
    "social_offtopic_streak",
    "identity_probe_streak",
    "last_business_context_version",
}

CODE_MECHANISM_VISIBLE_TERMS = {
    "session_key",
    "capture_id",
    "message_digest",
    "context_version",
    "reply_id",
    "ledger_state_version",
    "conversation_strategy_state",
    "redirect_fatigue_level",
    "suggested_engagement_mode",
    "social_offtopic_streak",
    "identity_probe_streak",
    "OCR",
    "RPA",
    "ledger",
    "automatic customer-service internal state",
    "auto customer-service internal state",
    "internal state",
}


def layer_attribution(source_layer: str | None) -> dict[str, Any]:
    layer = str(source_layer or UNKNOWN).strip() or UNKNOWN
    can_authorize_fact = layer in FACT_AUTHORIZING_LAYERS
    return {
        "source_layer": layer,
        "can_authorize_fact": can_authorize_fact,
        "can_influence_style": layer in STYLE_INFLUENCE_LAYERS,
        "can_influence_strategy": layer in STRATEGY_INFLUENCE_LAYERS,
        "must_not_authorize": [] if can_authorize_fact else ["customer_visible_fact"],
    }


def visible_reply_source_is_brain_owned(source: Any) -> bool:
    return str(source or "").strip() == CUSTOMER_VISIBLE_REPLY_SOURCE


def visible_reply_source_is_allowed(source: Any) -> bool:
    value = str(source or "").strip()
    return value in {CUSTOMER_VISIBLE_REPLY_SOURCE, NO_VISIBLE_REPLY_SOURCE}


def has_code_mechanism_visible_leak(reply_text: str) -> bool:
    text = str(reply_text or "")
    if not text:
        return False
    if any(term in text for term in CODE_MECHANISM_VISIBLE_TERMS):
        return True
    return bool(re.search(r"\b(?:session|capture|reply|ledger|context|strategy|redirect|social|identity)_[a-z_]*\b", text, flags=re.I))
