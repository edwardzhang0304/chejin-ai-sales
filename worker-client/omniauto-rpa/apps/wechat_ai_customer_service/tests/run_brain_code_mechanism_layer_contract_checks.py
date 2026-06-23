"""Contract checks for Brain layer and code-mechanism layer boundaries.

These checks keep the new layer vocabulary executable. They focus on global
authority/ownership rules instead of scenario-specific reply wording.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
for path in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evidence_authority import (  # noqa: E402
    AI_EXPERIENCE_POOL,
    CURRENT_CONVERSATION_FACT,
    FORMAL_KNOWLEDGE,
    LLM_COMMON_SENSE,
    PRODUCT_MASTER,
    SHARED_PUBLIC_STRATEGY,
    STYLE_MEMORY,
    annotate_authority,
    authority_order_payload,
    can_authorize_reply_content,
    classify_evidence,
)
from layer_contracts import (  # noqa: E402
    CODE_MECHANISM,
    CUSTOMER_SERVICE_BRAIN,
    CUSTOMER_VISIBLE_REPLY_SOURCE,
    NO_VISIBLE_REPLY_SOURCE,
    PRODUCT_SCOPED_FORMAL,
    REVIEWER_GUARD,
    REVIEWER_POLISH,
    REVIEWER_QUALITY,
    has_code_mechanism_visible_leak,
    layer_attribution,
    visible_reply_source_is_allowed,
    visible_reply_source_is_brain_owned,
)


@dataclass
class CaseResult:
    name: str
    ok: bool
    details: dict[str, Any]


def main() -> int:
    results = [
        check_fact_authority_boundaries(),
        check_style_and_strategy_layers_are_non_authoritative(),
        check_visible_reply_source_contract(),
        check_code_mechanism_metadata_cannot_leak_to_visible_reply(),
        check_evidence_authority_annotations(),
        check_authority_payload_declares_shared_public_strategy(),
    ]
    failures = [result for result in results if not result.ok]
    payload = {
        "ok": not failures,
        "failures": [failure.__dict__ for failure in failures],
        "results": [result.__dict__ for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_fact_authority_boundaries() -> CaseResult:
    authorizing = [PRODUCT_MASTER, PRODUCT_SCOPED_FORMAL, FORMAL_KNOWLEDGE, CURRENT_CONVERSATION_FACT]
    non_authorizing = [
        SHARED_PUBLIC_STRATEGY,
        LLM_COMMON_SENSE,
        STYLE_MEMORY,
        AI_EXPERIENCE_POOL,
        CUSTOMER_SERVICE_BRAIN,
        REVIEWER_GUARD,
        REVIEWER_QUALITY,
        REVIEWER_POLISH,
        CODE_MECHANISM,
    ]
    authorizing_ok = all(layer_attribution(layer)["can_authorize_fact"] is True for layer in authorizing)
    non_authorizing_ok = all(
        layer_attribution(layer)["can_authorize_fact"] is False
        and "customer_visible_fact" in layer_attribution(layer)["must_not_authorize"]
        for layer in non_authorizing
    )
    return CaseResult(
        "fact_authority_boundaries",
        authorizing_ok and non_authorizing_ok,
        {"authorizing": authorizing, "non_authorizing": non_authorizing},
    )


def check_style_and_strategy_layers_are_non_authoritative() -> CaseResult:
    shared = layer_attribution(SHARED_PUBLIC_STRATEGY)
    common = layer_attribution(LLM_COMMON_SENSE)
    style = layer_attribution(STYLE_MEMORY)
    pool = layer_attribution(AI_EXPERIENCE_POOL)
    code = layer_attribution(CODE_MECHANISM)
    ok = (
        shared["can_influence_strategy"] is True
        and shared["can_influence_style"] is True
        and common["can_influence_strategy"] is True
        and style["can_influence_style"] is True
        and pool["can_influence_style"] is True
        and code["can_influence_strategy"] is False
        and code["can_influence_style"] is False
        and all(item["can_authorize_fact"] is False for item in [shared, common, style, pool, code])
    )
    return CaseResult(
        "style_and_strategy_layers_are_non_authoritative",
        ok,
        {"shared": shared, "common": common, "style": style, "pool": pool, "code": code},
    )


def check_visible_reply_source_contract() -> CaseResult:
    allowed = [
        CUSTOMER_VISIBLE_REPLY_SOURCE,
        NO_VISIBLE_REPLY_SOURCE,
    ]
    forbidden = [
        "guard_handoff_ack",
        "quality_gate.safe_fallback",
        "semantic_reviewer.reply",
        "final_polish.reply",
        "legacy_advisory.reply",
        "code_mechanism.status_text",
    ]
    ok = (
        visible_reply_source_is_brain_owned(CUSTOMER_VISIBLE_REPLY_SOURCE) is True
        and visible_reply_source_is_brain_owned(NO_VISIBLE_REPLY_SOURCE) is False
        and all(visible_reply_source_is_allowed(source) for source in allowed)
        and not any(visible_reply_source_is_allowed(source) for source in forbidden)
    )
    return CaseResult("visible_reply_source_contract", ok, {"allowed": allowed, "forbidden": forbidden})


def check_code_mechanism_metadata_cannot_leak_to_visible_reply() -> CaseResult:
    leaks = [
        "session_key=target:许聪",
        "capture_id cap_001 已确认",
        "message_digest 不一致，稍等",
        "context_version=3",
        "reply_id 已重排",
        "OCR 识别失败，我稍后回复",
        "RPA 正在切会话",
        "ledger_state_version 过期",
        "conversation_strategy_state=social_companion",
        "redirect_fatigue_level=suppress",
        "suggested_engagement_mode=soft_bridge",
    ]
    safe = [
        "在的，您说。",
        "这台我先帮您按现有资料看一下，车况细节以检测报告为准。",
        "可以，我先按您刚才说的预算和用途筛两台更贴近的。",
    ]
    ok = all(has_code_mechanism_visible_leak(text) for text in leaks) and not any(
        has_code_mechanism_visible_leak(text) for text in safe
    )
    return CaseResult("code_mechanism_metadata_cannot_leak_to_visible_reply", ok, {"leaks": leaks, "safe": safe})


def check_evidence_authority_annotations() -> CaseResult:
    cases = [
        ({"category_id": "products", "_knowledge_layer": "product_master"}, PRODUCT_MASTER, True),
        ({"category_id": "policies", "_knowledge_layer": "tenant"}, FORMAL_KNOWLEDGE, True),
        ({"source_type": "current_conversation_fact"}, CURRENT_CONVERSATION_FACT, True),
        ({"_knowledge_layer": "shared_public_strategy"}, SHARED_PUBLIC_STRATEGY, False),
        ({"source_type": "global_guidelines"}, SHARED_PUBLIC_STRATEGY, False),
        ({"source_type": "llm_common_sense"}, LLM_COMMON_SENSE, False),
        ({"category_id": "chats", "source_type": "real_chat_style"}, AI_EXPERIENCE_POOL, False),
        ({"chunk_id": "rag_1", "source_type": "rag"}, AI_EXPERIENCE_POOL, False),
        ({"source_type": "style_memory"}, STYLE_MEMORY, False),
    ]
    observed: list[dict[str, Any]] = []
    ok = True
    for item, expected_level, expected_authorized in cases:
        level = classify_evidence(item, category_id=item.get("category_id"), source_type=item.get("source_type"))
        annotated = annotate_authority(item, category_id=item.get("category_id"), source_type=item.get("source_type"))
        authorized = can_authorize_reply_content(item, category_id=item.get("category_id"), source_type=item.get("source_type"))
        observed.append(
            {
                "item": item,
                "level": level,
                "expected_level": expected_level,
                "authorized": authorized,
                "expected_authorized": expected_authorized,
                "can_authorize_fact": annotated.get("can_authorize_fact"),
                "can_influence_strategy": annotated.get("can_influence_strategy"),
                "can_influence_style": annotated.get("can_influence_style"),
            }
        )
        ok = ok and level == expected_level and authorized is expected_authorized
    return CaseResult("evidence_authority_annotations", ok, {"observed": observed})


def check_authority_payload_declares_shared_public_strategy() -> CaseResult:
    payload = authority_order_payload()
    by_level = {str(item.get("level")): item for item in payload}
    shared = by_level.get(SHARED_PUBLIC_STRATEGY) or {}
    ok = (
        PRODUCT_MASTER in by_level
        and FORMAL_KNOWLEDGE in by_level
        and CURRENT_CONVERSATION_FACT in by_level
        and SHARED_PUBLIC_STRATEGY in by_level
        and shared.get("can_authorize_product_facts") is False
        and "不授权商品或政策事实" in str(shared.get("description") or "")
    )
    return CaseResult("authority_payload_declares_shared_public_strategy", ok, {"shared": shared})


if __name__ == "__main__":
    raise SystemExit(main())
