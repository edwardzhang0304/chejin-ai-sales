"""Portable pure business-viewport continuity rules.

This module is deliberately stored with the shared OmniAuto adapters so both
the independent Sidecar and the Worker can execute the exact same pure
algorithm.  Worker remains the only owner that *decides* and binds continuity;
Sidecar may only verify the Worker-bound contract before S0/S1/S2.  The module
has no Worker, UI, network, storage, or durable-identity dependency.
"""

from __future__ import annotations

from collections import Counter
import hashlib
from typing import Any

from .message_viewport_projection import (
    SEND_CONTEXT_BUSINESS_DIGEST_SCHEMA_VERSION,
    normalized_business_message_sequence,
    ordered_message_viewport_observations,
    stable_business_content_signature,
)


BUSINESS_VIEWPORT_CONTINUITY_RESULTS = frozenset(
    {
        "business_sequence_equal",
        "unique_tail_append",
        "unique_viewport_slide_with_tail_append",
        "continuity_context_expansion_required",
        "business_sequence_not_continuous",
    }
)


def _normalized_voice_duration(value: object) -> str:
    text = str(value or "").strip().lower()
    for suffix in ("seconds", "second", "secs", "sec", "秒", "s"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return (
        str(int(number))
        if number.is_integer()
        else format(number, ".3f").rstrip("0").rstrip(".")
    )


def _native_source_message_id(observation: dict[str, Any]) -> str:
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    value = str(
        observation.get("native_source_message_id")
        or source.get("native_source_message_id")
        or ""
    ).strip()
    return value


def boundary_tokens_for_observations(
    observations: list[dict[str, Any]] | None,
    *,
    committed_only: bool,
) -> dict[int, set[str]]:
    """Project strong-boundary evidence without creating message identity.

    The current OCR frame may emit the observable half of a token even though
    it has no durable Worker id.  The old/checkpoint side uses
    ``committed_only=True`` so only already-committed facts can authorize a
    cross-frame boundary.
    """

    ordered = ordered_message_viewport_observations(observations)
    sequence = normalized_business_message_sequence(
        ordered,
        message_viewport_bounds=None,
    )
    result: dict[int, set[str]] = {}
    for index, (observation, fact) in enumerate(zip(ordered, sequence)):
        tokens: set[str] = set()
        native_id = _native_source_message_id(observation)
        committed = bool(
            str(observation.get("_worker_stable_id") or "").strip()
            and (
                str(observation.get("_worker_identity_scope") or "")
                .strip()
                .lower()
                == "committed"
                or isinstance(
                    observation.get("_worker_committed_message"), dict
                )
            )
        )
        pending_confirmed_action_result = bool(
            observation.get(
                "_worker_media_result_pending_continuity"
            )
            is True
        )
        if native_id and (committed or not committed_only):
            tokens.add(
                "native:"
                + str(fact.get("message_type") or "")
                + ":"
                + native_id
            )
        role = str(fact.get("sender_role") or "")
        message_type = str(fact.get("message_type") or "")
        signature = str(
            fact.get("normalized_content_signature") or ""
        )
        if message_type in {"text", "system"} and (
            committed or not committed_only
        ):
            tokens.add(
                f"fact:{role}:{message_type}:{signature}:"
                + str(fact.get("media_state") or "")
            )
        if message_type == "voice" and (
            committed
            or pending_confirmed_action_result
            or not committed_only
        ):
            summary = (
                observation.get("_worker_voice_action_summary")
                if isinstance(
                    observation.get("_worker_voice_action_summary"), dict
                )
                else {}
            )
            confirmed_mapping = (
                summary.get("confirmed_action_mapping")
                if isinstance(
                    summary.get("confirmed_action_mapping"), dict
                )
                else {}
            )
            source = (
                observation.get("source_message")
                if isinstance(observation.get("source_message"), dict)
                else {}
            )
            duration = _normalized_voice_duration(
                observation.get("voice_duration")
                or observation.get("voice_duration_text")
                or source.get("voice_duration")
                or source.get("voice_duration_text")
                or ""
            )
            transcript_confirmed = bool(
                str(observation.get("row_kind") or "").strip().lower()
                == "voice_transcript"
                and str(fact.get("media_state") or "") == "transcribed"
            )
            if transcript_confirmed and (
                not committed_only
                or pending_confirmed_action_result
                or str(
                    summary.get("canonical_action_id")
                    or confirmed_mapping.get("canonical_action_id")
                    or ""
                ).strip()
            ):
                material = f"{role}:{signature}:{duration}"
                tokens.add(
                    "voice-result:"
                    + hashlib.sha256(material.encode("utf-8")).hexdigest()
                )
        if tokens:
            result[index] = tokens
    return result


def _fact_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the cross-frame business key; screen_order is frame-local."""

    return (
        str(item.get("sender_role") or "").strip().lower(),
        str(item.get("message_type") or "").strip().lower(),
        str(item.get("normalized_content_signature") or "")
        .strip()
        .lower(),
        str(item.get("media_state") or "").strip().lower(),
    )


def _normalized_sequence(
    sequence: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in sequence or []:
        if not isinstance(item, dict):
            continue
        role, message_type, content, media_state = _fact_key(item)
        normalized.append(
            {
                "screen_order": len(normalized),
                "sender_role": role,
                "message_type": message_type,
                "normalized_content_signature": content,
                "media_state": media_state,
            }
        )
    return normalized


def _normalized_boundary_tokens(
    value: dict[int, set[str] | list[str] | tuple[str, ...]] | None,
    *,
    length: int,
) -> dict[int, frozenset[str]]:
    result: dict[int, frozenset[str]] = {}
    for raw_index, raw_tokens in (value or {}).items():
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            continue
        if raw_index < 0 or raw_index >= length:
            continue
        tokens = frozenset(
            str(token or "").strip()
            for token in (raw_tokens or [])
            if str(token or "").strip()
        )
        if tokens:
            result[raw_index] = tokens
    return result


def _compare_business_viewport_continuity_direct(
    old_sequence: list[dict[str, Any]] | None,
    new_sequence: list[dict[str, Any]] | None,
    *,
    old_boundary_tokens: (
        dict[int, set[str] | list[str] | tuple[str, ...]] | None
    ) = None,
    new_boundary_tokens: (
        dict[int, set[str] | list[str] | tuple[str, ...]] | None
    ) = None,
    old_top_boundary_complete: bool = False,
    new_top_boundary_complete: bool = False,
    context_expansion_used: bool = False,
) -> dict[str, Any]:
    """Compare two business viewports without geometry or durable-ID guesses.

    A unique slide is an old *suffix* equal to a new *prefix* with a non-empty
    new tail.  Repeated facts remain ambiguous unless the overlap contains a
    strong boundary token shared by exactly one old row and one new row.  The
    caller may retry once after a bounded read-only context expansion; this
    pure function never performs UI work itself.
    """

    old = _normalized_sequence(old_sequence)
    new = _normalized_sequence(new_sequence)
    old_keys = [_fact_key(item) for item in old]
    new_keys = [_fact_key(item) for item in new]
    old_tokens = _normalized_boundary_tokens(
        old_boundary_tokens,
        length=len(old),
    )
    new_tokens = _normalized_boundary_tokens(
        new_boundary_tokens,
        length=len(new),
    )
    old_token_counts = Counter(
        token for tokens in old_tokens.values() for token in tokens
    )
    new_token_counts = Counter(
        token for tokens in new_tokens.values() for token in tokens
    )

    def overlap_has_unique_boundary(
        old_start: int,
        new_start: int,
        size: int,
    ) -> bool:
        for offset in range(size):
            left = old_tokens.get(old_start + offset, frozenset())
            right = new_tokens.get(new_start + offset, frozenset())
            for token in left.intersection(right):
                if old_token_counts[token] == new_token_counts[token] == 1:
                    return True
        return False

    base: dict[str, Any] = {
        "relation": "business_sequence_not_continuous",
        "old_count": len(old),
        "new_count": len(new),
        "overlap_candidates": [],
        "matched_pairs": [],
        "new_suffix_indexes": [],
        "context_expansion_used": bool(context_expansion_used),
    }

    if not old and not new:
        if old_top_boundary_complete and new_top_boundary_complete:
            return {
                **base,
                "relation": "business_sequence_equal",
                "reason": "confirmed_empty_business_viewport",
            }
        return {
            **base,
            "relation": (
                "business_sequence_not_continuous"
                if context_expansion_used
                else "continuity_context_expansion_required"
            ),
            "reason": "empty_viewport_boundary_unconfirmed",
        }

    if not old and new:
        if old_top_boundary_complete:
            return {
                **base,
                "relation": "unique_tail_append",
                "reason": "confirmed_empty_baseline_with_new_tail",
                "new_suffix_indexes": list(range(len(new))),
            }
        return {
            **base,
            "relation": (
                "business_sequence_not_continuous"
                if context_expansion_used
                else "continuity_context_expansion_required"
            ),
            "reason": "empty_old_boundary_unconfirmed",
        }

    if old_keys == new_keys:
        identity_chain_confirmed = overlap_has_unique_boundary(
            0, 0, len(old)
        )
        if (
            identity_chain_confirmed
            or (old_top_boundary_complete and new_top_boundary_complete)
        ):
            return {
                **base,
                "relation": "business_sequence_equal",
                "reason": (
                    "unique_strong_boundary_equal"
                    if identity_chain_confirmed
                    else "complete_top_boundary_equal"
                ),
                "matched_pairs": [
                    {
                        "old_index": index,
                        "new_index": index,
                    }
                    for index in range(len(old))
                ],
            }
        return {
            **base,
            "relation": (
                "business_sequence_not_continuous"
                if context_expansion_used
                else "continuity_context_expansion_required"
            ),
            "reason": "equal_facts_without_strong_boundary",
        }

    # Enumerate every suffix-prefix explanation before accepting a complete
    # prefix.  With repeated weak media, e.g. [image, image] ->
    # [image, image, image], both "append one" and "slide then append" fit the
    # same five fields.  Choosing the longest overlap would silently bind an
    # action result to the wrong occurrence.
    raw_candidates: list[dict[str, Any]] = []
    for overlap_size in range(1, min(len(old), len(new)) + 1):
        old_start = len(old) - overlap_size
        if old_keys[old_start:] != new_keys[:overlap_size]:
            continue
        if overlap_size >= len(new):
            # A slide without a non-empty new tail is not a new-message
            # continuation and must not be accepted by this relation.
            continue
        raw_candidates.append(
            {
                "old_start": old_start,
                "new_start": 0,
                "overlap_size": overlap_size,
                "new_suffix_start": overlap_size,
                "complete_old_prefix": (
                    old_start == 0 and overlap_size == len(old)
                ),
                "has_unique_strong_boundary": (
                    overlap_has_unique_boundary(
                        old_start,
                        0,
                        overlap_size,
                    )
                ),
            }
        )
    base["overlap_candidates"] = raw_candidates

    if len(raw_candidates) == 1 and raw_candidates[0][
        "complete_old_prefix"
    ]:
        candidate = raw_candidates[0]
        return {
            **base,
            "relation": "unique_tail_append",
            "reason": "old_sequence_is_only_complete_new_prefix",
            "matched_pairs": [
                {"old_index": index, "new_index": index}
                for index in range(len(old))
            ],
            "new_suffix_indexes": list(range(len(old), len(new))),
        }

    strongly_bounded_candidates = [
        candidate
        for candidate in raw_candidates
        if candidate["has_unique_strong_boundary"]
    ]
    if len(strongly_bounded_candidates) == 1:
        candidate = strongly_bounded_candidates[0]
        old_start = int(candidate["old_start"])
        overlap_size = int(candidate["overlap_size"])
        relation = (
            "unique_tail_append"
            if candidate["complete_old_prefix"]
            else "unique_viewport_slide_with_tail_append"
        )
        return {
            **base,
            "relation": relation,
            "reason": "one_strongly_bounded_suffix_prefix_explanation",
            "matched_pairs": [
                {
                    "old_index": old_start + offset,
                    "new_index": offset,
                }
                for offset in range(overlap_size)
            ],
            "new_suffix_indexes": list(
                range(overlap_size, len(new))
            ),
        }

    return {
        **base,
        "relation": (
            "business_sequence_not_continuous"
            if context_expansion_used
            else "continuity_context_expansion_required"
        ),
        "reason": (
            "multiple_strongly_bounded_overlaps"
            if len(strongly_bounded_candidates) > 1
            else "multiple_weak_overlap_explanations"
            if len(raw_candidates) > 1
            else "no_unique_strongly_bounded_overlap"
        ),
    }


def compare_business_viewport_continuity(
    old_sequence: list[dict[str, Any]] | None,
    new_sequence: list[dict[str, Any]] | None,
    *,
    old_boundary_tokens: (
        dict[int, set[str] | list[str] | tuple[str, ...]] | None
    ) = None,
    new_boundary_tokens: (
        dict[int, set[str] | list[str] | tuple[str, ...]] | None
    ) = None,
    old_top_boundary_complete: bool = False,
    new_top_boundary_complete: bool = False,
    context_expansion_used: bool = False,
    expanded_context_sequence: list[dict[str, Any]] | None = None,
    expanded_context_boundary_tokens: (
        dict[int, set[str] | list[str] | tuple[str, ...]] | None
    ) = None,
) -> dict[str, Any]:
    """The single public pure continuity comparator.

    Worker owns when and why this result is used.  When a caller consumes its
    one expansion opportunity, the expanded
    sequence is verified from the original strong boundary through the final
    bottom frame.  No caller-provided "success token" can bypass either
    comparison.
    """

    direct = _compare_business_viewport_continuity_direct(
        old_sequence,
        new_sequence,
        old_boundary_tokens=old_boundary_tokens,
        new_boundary_tokens=new_boundary_tokens,
        old_top_boundary_complete=old_top_boundary_complete,
        new_top_boundary_complete=new_top_boundary_complete,
        context_expansion_used=context_expansion_used,
    )
    if (
        direct.get("relation")
        != "business_sequence_not_continuous"
        or not context_expansion_used
        or expanded_context_sequence is None
    ):
        return direct

    expanded = _normalized_sequence(expanded_context_sequence)
    current = _normalized_sequence(new_sequence)
    old = _normalized_sequence(old_sequence)
    expanded_decision = _compare_business_viewport_continuity_direct(
        old,
        expanded,
        old_boundary_tokens=old_boundary_tokens,
        new_boundary_tokens=expanded_context_boundary_tokens,
        old_top_boundary_complete=old_top_boundary_complete,
        new_top_boundary_complete=False,
        context_expansion_used=True,
    )
    if expanded_decision.get("relation") not in {
        "business_sequence_equal",
        "unique_tail_append",
        "unique_viewport_slide_with_tail_append",
    }:
        return {
            **direct,
            "reason": "expanded_context_not_continuous",
            "expanded_context_decision": expanded_decision,
        }

    expanded_keys = [_fact_key(item) for item in expanded]
    current_keys = [_fact_key(item) for item in current]
    candidates: list[dict[str, int]] = []
    for expanded_start in range(len(expanded_keys) + 1):
        overlap_size = len(expanded_keys) - expanded_start
        if overlap_size <= 0 or len(current_keys) < overlap_size:
            continue
        if expanded_keys[expanded_start:] != current_keys[:overlap_size]:
            continue
        candidates.append(
            {
                "expanded_start": expanded_start,
                "overlap_size": overlap_size,
            }
        )
    if len(candidates) != 1:
        return {
            **direct,
            "reason": (
                "expanded_to_bottom_multiple_explanations"
                if len(candidates) > 1
                else "expanded_to_bottom_no_overlap"
            ),
            "expanded_context_decision": expanded_decision,
            "expanded_to_bottom_candidates": candidates,
        }

    candidate = candidates[0]
    expanded_start = candidate["expanded_start"]
    overlap_size = candidate["overlap_size"]
    old_to_expanded = {
        int(pair.get("old_index")): int(pair.get("new_index"))
        for pair in (expanded_decision.get("matched_pairs") or [])
        if isinstance(pair, dict)
        and isinstance(pair.get("old_index"), int)
        and isinstance(pair.get("new_index"), int)
    }
    matched_pairs = [
        {
            "old_index": old_index,
            "new_index": expanded_index - expanded_start,
        }
        for old_index, expanded_index in sorted(old_to_expanded.items())
        if expanded_start <= expanded_index < len(expanded)
    ]
    if not matched_pairs:
        return {
            **direct,
            "reason": "expanded_context_lost_all_old_business_rows",
            "expanded_context_decision": expanded_decision,
        }
    expanded_new_indexes = {
        int(index)
        for index in (expanded_decision.get("new_suffix_indexes") or [])
        if isinstance(index, int) and not isinstance(index, bool)
    }
    new_suffix_indexes = sorted(
        {
            expanded_index - expanded_start
            for expanded_index in expanded_new_indexes
            if expanded_start <= expanded_index < len(expanded)
        }
        | set(range(overlap_size, len(current)))
    )
    if not new_suffix_indexes:
        relation = (
            "business_sequence_equal"
            if len(matched_pairs) == len(old) == len(current)
            else "business_sequence_not_continuous"
        )
    elif (
        len(matched_pairs) == len(old)
        and all(
            pair["old_index"] == pair["new_index"]
            for pair in matched_pairs
        )
    ):
        relation = "unique_tail_append"
    else:
        relation = "unique_viewport_slide_with_tail_append"
    if relation == "business_sequence_not_continuous":
        return {
            **direct,
            "reason": "expanded_context_has_no_new_tail",
            "expanded_context_decision": expanded_decision,
        }
    return {
        **direct,
        "relation": relation,
        "reason": "bounded_context_expansion_proved_unique_continuity",
        "matched_pairs": matched_pairs,
        "new_suffix_indexes": new_suffix_indexes,
        "expanded_context_decision": expanded_decision,
        "expanded_to_bottom_candidates": candidates,
    }

__all__ = [
    "BUSINESS_VIEWPORT_CONTINUITY_RESULTS",
    "SEND_CONTEXT_BUSINESS_DIGEST_SCHEMA_VERSION",
    "boundary_tokens_for_observations",
    "compare_business_viewport_continuity",
    "normalized_business_message_sequence",
    "ordered_message_viewport_observations",
    "stable_business_content_signature",
]
