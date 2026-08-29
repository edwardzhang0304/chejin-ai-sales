from __future__ import annotations

"""Worker-owned comparison for the immutable pre-send fact checkpoint.

This module deliberately does not allocate, inherit, or validate durable
message identities.  It compares the facts frozen for one Brain reply with a
fresh ordered Sidecar observation frame and exposes only an unchanged/suffix/
not-continuous decision to TaskRunner.
"""

import hashlib
import json
from typing import Any

from .message_contract import canonical_message_identity_text
from .message_viewport_projection import (
    boundary_tokens_for_observations,
    compare_business_viewport_continuity,
    normalized_business_message_sequence,
    ordered_message_viewport_observations,
)


CHECKPOINT_REVISION = 5
CHECKPOINT_RESULTS = {
    "checkpoint_equal",
    "checkpoint_unique_prefix_with_suffix",
    "checkpoint_unique_viewport_slide_with_suffix",
    "checkpoint_continuity_context_expansion_required",
    "checkpoint_not_continuous",
}
IGNORED_NON_INGESTIBLE_ROW_KINDS = {"call_event", "system_banner"}
TERMINAL_ACTION_COMMIT_BASES = {
    "confirmed_voice_action",
    "confirmed_image_action",
}


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _normalize_voice_duration_value(value: object) -> str:
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
    return str(int(number)) if number.is_integer() else format(number, ".3f").rstrip("0").rstrip(".")


def stable_fact_signature(
    *,
    sender_role: object,
    message_type: object,
    item_state: object,
    content: object = "",
    voice_duration: object = "",
    image_content_sha256: object = "",
    error_code: object = "",
) -> str:
    normalized_type = str(message_type or "").strip().lower()
    normalized_state = str(item_state or "").strip().lower()
    material: dict[str, str] = {
        "sender_role": str(sender_role or "").strip().lower(),
        "message_type": normalized_type,
        "item_state": normalized_state,
    }
    if normalized_type != "image":
        material["normalized_content_hash"] = hashlib.sha256(
            canonical_message_identity_text(content).encode("utf-8")
        ).hexdigest()
        if normalized_type == "voice":
            material["voice_duration"] = _normalize_voice_duration_value(
                voice_duration
            )
    else:
        # The formal image receipt remains strict. Its one-frame bubble/ROI
        # digest is not a cross-frame business fact.
        _ = image_content_sha256
    if normalized_state == "failed":
        material["error_code"] = str(error_code or "").strip()
    return canonical_sha256(material)


def exact_image_content_sha256(value: object) -> str:
    """Return the exact/quantized ROI digest, never the perceptual dHash."""

    text = str(value or "").strip().lower()
    if text.startswith("imagev2:"):
        parts = text.split(":", 2)
        digest = parts[2] if len(parts) == 3 else ""
    elif text.startswith("sha256:"):
        digest = text.split(":", 1)[1]
    else:
        digest = ""
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return ""
    return digest


def _normalized_voice_duration(observation: dict[str, Any]) -> str:
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    value = (
        observation.get("voice_duration")
        or source.get("voice_duration")
        or observation.get("voice_duration_text")
        or source.get("voice_duration_text")
        or ""
    )
    return _normalize_voice_duration_value(value)


def reply_fact_evidence_for_observation(
    observation: dict[str, Any],
    *,
    item_state: str,
) -> dict[str, str]:
    """Build facts observable again without assigning a durable identity."""

    message_type = _message_type(observation)
    role = str(observation.get("sender_role") or "").strip().lower()
    state = str(item_state or "").strip().lower()
    evidence: dict[str, str] = {
        "sender_role": role,
        "message_type": message_type,
        "item_state": state,
    }
    if message_type == "voice" and state == "completed":
        content = observation.get("content_clean")
        if content is None:
            content = observation.get("content")
        normalized = canonical_message_identity_text(content)
        evidence["normalized_transcript_sha256"] = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if normalized
            else ""
        )
        evidence["voice_duration"] = _normalized_voice_duration(observation)
    elif message_type == "image" and state == "completed":
        evidence["exact_image_content_sha256"] = exact_image_content_sha256(
            _image_fingerprint(observation)
        )
    return evidence


def checkpoint_binding_error(
    checkpoint: object,
    binding: object,
    *,
    conversation_id: str,
    batch_id: str,
    reply_action_id: str,
) -> str:
    if not isinstance(checkpoint, dict) or not isinstance(binding, dict):
        return "checkpoint_or_binding_missing"
    try:
        revision = int(checkpoint.get("checkpoint_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if revision != CHECKPOINT_REVISION:
        return "checkpoint_revision_invalid"
    if checkpoint.get("tail_complete") is not True:
        return "checkpoint_tail_incomplete"
    baseline_kind = str(
        checkpoint.get("baseline_kind") or ""
    ).strip()
    frame_source = str(
        checkpoint.get("authoritative_frame_source") or ""
    ).strip()
    tail = checkpoint.get("committed_tail")
    if not isinstance(tail, list):
        return "checkpoint_tail_missing"
    if baseline_kind == "friend_welcome_empty":
        if tail or frame_source != "control_empty":
            return "checkpoint_empty_baseline_invalid"
    elif baseline_kind == "message_tail":
        if not tail or frame_source not in {"initial_read", "final_read"}:
            return "checkpoint_message_tail_invalid"
    else:
        return "checkpoint_baseline_kind_invalid"
    expected = {
        "conversation_id": str(conversation_id or "").strip(),
        "batch_id": str(batch_id or "").strip(),
        "reply_action_id": str(reply_action_id or "").strip(),
    }
    for key, value in expected.items():
        if not value or str(binding.get(key) or "").strip() != value:
            return f"binding_{key}_mismatch"
    if str(checkpoint.get("conversation_id") or "").strip() != expected[
        "conversation_id"
    ]:
        return "checkpoint_conversation_id_mismatch"
    if str(checkpoint.get("batch_id") or "").strip() != expected["batch_id"]:
        return "checkpoint_batch_id_mismatch"
    digest = str(binding.get("checkpoint_digest") or "").strip().lower()
    if not _is_sha256(digest) or digest != canonical_sha256(checkpoint):
        return "checkpoint_digest_mismatch"
    seen_stable_ids: set[str] = set()
    for index, item in enumerate(tail):
        if not isinstance(item, dict):
            return "checkpoint_item_invalid"
        stable_id = str(item.get("worker_stable_id") or "").strip()
        role = str(item.get("sender_role") or "").strip().lower()
        message_type = str(item.get("message_type") or "").strip().lower()
        item_state = str(item.get("item_state") or "").strip().lower()
        signature = str(item.get("stable_fact_signature") or "").strip().lower()
        reply_fact_evidence = item.get("reply_fact_evidence")
        business_projection = item.get("business_projection")
        strong_boundary_tokens = item.get("strong_boundary_tokens")
        commit_record = item.get("message_identity_commit_record")
        runtime_evidence = item.get("message_identity_runtime_evidence")
        commit_basis = str(item.get("commit_basis") or "").strip()
        action_receipt_digest = str(
            item.get("action_receipt_digest") or ""
        ).strip().lower()
        source_message_key = str(
            item.get("source_message_key") or ""
        ).strip()
        business_projection_valid = bool(
            isinstance(business_projection, dict)
            and set(business_projection)
            == {
                "screen_order",
                "sender_role",
                "message_type",
                "normalized_content_signature",
                "media_state",
            }
            and business_projection.get("screen_order") == index
            and str(business_projection.get("sender_role") or "")
            .strip()
            .lower()
            == role
            and str(business_projection.get("message_type") or "")
            .strip()
            .lower()
            == message_type
            and _is_sha256(
                business_projection.get("normalized_content_signature")
            )
            and isinstance(strong_boundary_tokens, list)
            and all(
                isinstance(token, str) and bool(token.strip())
                for token in strong_boundary_tokens
            )
            and len(strong_boundary_tokens)
            == len(set(strong_boundary_tokens))
        )
        reply_fact_matches_item = bool(
            isinstance(reply_fact_evidence, dict)
            and str(reply_fact_evidence.get("sender_role") or "")
            .strip()
            .lower()
            == role
            and str(reply_fact_evidence.get("message_type") or "")
            .strip()
            .lower()
            == message_type
            and str(reply_fact_evidence.get("item_state") or "")
            .strip()
            .lower()
            == item_state
        )
        commit_record_valid = bool(
            isinstance(commit_record, dict)
            and commit_record.get("object_type") == "committed_message"
            and str(commit_record.get("worker_stable_id") or "").strip()
            == stable_id
            and str(commit_record.get("sender_role") or "").strip().lower()
            == role
            and str(commit_record.get("message_type") or "").strip().lower()
            == message_type
            and str(commit_record.get("commit_basis") or "").strip()
            == commit_basis
            and str(commit_record.get("observation_id") or "").strip()
            and isinstance(commit_record.get("proof"), dict)
        )
        formal_media_action_valid = bool(
            message_type not in {"voice", "image"}
            or (
                commit_basis == f"confirmed_{message_type}_action"
                and _is_sha256(action_receipt_digest)
            )
        )
        if (
            not stable_id
            or stable_id in seen_stable_ids
            or role not in {"customer", "self", "system"}
            or message_type not in {"text", "voice", "image", "system"}
            or item_state not in {"completed", "failed"}
            or not _is_sha256(signature)
            or not isinstance(reply_fact_evidence, dict)
            or not business_projection_valid
            or not reply_fact_matches_item
            or not source_message_key
            or not commit_record_valid
            or not formal_media_action_valid
            or not isinstance(runtime_evidence, dict)
        ):
            return "checkpoint_item_invalid"
        seen_stable_ids.add(stable_id)
    return ""


def _message_type(observation: dict[str, Any]) -> str:
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    if row_kind in IGNORED_NON_INGESTIBLE_ROW_KINDS:
        return ""
    explicit = str(observation.get("message_type") or "").strip().lower()
    if explicit in {"text", "voice", "image", "system"}:
        return explicit
    return {
        "text_bubble": "text",
        "voice_bubble": "voice",
        "voice_transcript": "voice",
        "image_bubble": "image",
        "system_message": "system",
        "system_row": "system",
    }.get(row_kind, "")


def _image_fingerprint(observation: dict[str, Any]) -> str:
    summary = (
        observation.get("_worker_image_action_summary")
        if isinstance(observation.get("_worker_image_action_summary"), dict)
        else {}
    )
    digest = str(summary.get("image_sha256") or "").strip().lower()
    return f"sha256:{digest}" if _is_sha256(digest) else ""


def observation_fact_sequence(
    observations: list[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sequence: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw_index, raw in enumerate(observations or []):
        if not isinstance(raw, dict):
            continue
        row_kind = str(raw.get("row_kind") or "").strip().lower()
        message_type = _message_type(raw)
        if not message_type:
            if row_kind and row_kind not in IGNORED_NON_INGESTIBLE_ROW_KINDS:
                errors.append(
                    {
                        "reason": "unknown_business_row",
                        "raw_index": raw_index,
                        "row_kind": row_kind,
                    }
                )
            continue
        role = str(raw.get("sender_role") or "").strip().lower()
        if role not in {"customer", "self", "system"}:
            errors.append(
                {
                    "reason": "sender_role_unconfirmed",
                    "raw_index": raw_index,
                    "row_kind": row_kind,
                }
            )
            continue
        content = raw.get("content_clean")
        if content is None:
            content = raw.get("content")
        image_fingerprint = _image_fingerprint(raw)
        if message_type == "voice":
            item_state = (
                "completed"
                if row_kind == "voice_transcript"
                and str(raw.get("voice_state") or "").strip().lower()
                == "transcribed"
                and canonical_message_identity_text(content)
                else "failed"
                if str(raw.get("item_state") or "").strip().lower()
                == "failed"
                else "discovered"
            )
        elif message_type == "image":
            item_state = (
                "failed"
                if str(raw.get("item_state") or "").strip().lower()
                == "failed"
                else "completed"
                if image_fingerprint
                else "discovered"
            )
        else:
            item_state = (
                "failed"
                if str(raw.get("item_state") or "").strip().lower()
                == "failed"
                else "completed"
            )
        sequence.append(
            {
                "observation_id": str(raw.get("observation_id") or "").strip(),
                "screen_order": len(sequence) + 1,
                "sender_role": role,
                "message_type": message_type,
                "item_state": item_state,
                "stable_fact_signature": stable_fact_signature(
                    sender_role=role,
                    message_type=message_type,
                    item_state=item_state,
                    content=content,
                    voice_duration=_normalized_voice_duration(raw),
                    image_content_sha256=image_fingerprint,
                    error_code=raw.get("error_code") or "",
                ),
                "reply_fact_evidence": reply_fact_evidence_for_observation(
                    raw,
                    item_state=item_state,
                ),
            }
        )
    return sequence, errors


def _compare_checkpoint_business_continuity_v5(
    checkpoint: dict[str, Any],
    observations: list[Any] | None,
    *,
    before_frame_id: str,
    after_frame_id: str,
    current_tail_complete: bool,
    current_empty_viewport_confirmed: bool,
    context_expansion_used: bool,
    expanded_context_observations: list[Any] | None,
) -> dict[str, Any]:
    frozen = [
        dict(item)
        for item in (checkpoint.get("committed_tail") or [])
        if isinstance(item, dict)
    ]
    ordered_current = ordered_message_viewport_observations(
        [item for item in (observations or []) if isinstance(item, dict)]
    )
    current_projection = normalized_business_message_sequence(
        ordered_current,
        message_viewport_bounds=None,
    )
    old_projection = [
        dict(item.get("business_projection"))
        for item in frozen
        if isinstance(item.get("business_projection"), dict)
    ]
    _current_facts, errors = observation_fact_sequence(ordered_current)
    base = {
        "comparison_result": "checkpoint_not_continuous",
        "source_message_type": (
            str(frozen[0].get("message_type") or "").strip().lower()
            if len(frozen) == 1
            else ""
        ),
        "before_frame_id": str(before_frame_id or "").strip(),
        "after_frame_id": str(after_frame_id or "").strip(),
        "checkpoint_count": len(frozen),
        "current_count": len(current_projection),
        "current_prefix_count": 0,
        "matched_pairs": [],
        "new_suffix_observation_ids": [],
        "old_tail_fully_consumed": False,
        "observation_errors": errors,
        "physical_identity_confirmed": False,
        "terminal_fact_equivalence_count": 0,
        "context_expansion_used": bool(context_expansion_used),
    }
    if errors:
        return {
            **base,
            "reason": str(errors[0].get("reason") or "observation_invalid"),
        }
    if not current_tail_complete:
        return {**base, "reason": "checkpoint_current_tail_incomplete"}
    if len(old_projection) != len(frozen):
        return {**base, "reason": "checkpoint_business_projection_missing"}

    old_tokens = {
        index: set(item.get("strong_boundary_tokens") or [])
        for index, item in enumerate(frozen)
        if isinstance(item.get("strong_boundary_tokens"), list)
        and item.get("strong_boundary_tokens")
    }
    current_tokens = boundary_tokens_for_observations(
        ordered_current,
        committed_only=False,
    )
    baseline_kind = str(checkpoint.get("baseline_kind") or "").strip()
    decision = compare_business_viewport_continuity(
        old_projection,
        current_projection,
        old_boundary_tokens=old_tokens,
        new_boundary_tokens=current_tokens,
        old_top_boundary_complete=(
            baseline_kind == "friend_welcome_empty"
            and not old_projection
        ),
        new_top_boundary_complete=bool(current_empty_viewport_confirmed),
        context_expansion_used=context_expansion_used,
        expanded_context_sequence=(
            normalized_business_message_sequence(
                ordered_message_viewport_observations(
                    [
                        item
                        for item in expanded_context_observations
                        if isinstance(item, dict)
                    ]
                ),
                message_viewport_bounds=None,
            )
            if isinstance(expanded_context_observations, list)
            else None
        ),
        expanded_context_boundary_tokens=(
            boundary_tokens_for_observations(
                [
                    item
                    for item in expanded_context_observations
                    if isinstance(item, dict)
                ],
                committed_only=False,
            )
            if isinstance(expanded_context_observations, list)
            else None
        ),
    )
    relation = str(decision.get("relation") or "")
    result_map = {
        "business_sequence_equal": "checkpoint_equal",
        "unique_tail_append": "checkpoint_unique_prefix_with_suffix",
        "unique_viewport_slide_with_tail_append": (
            "checkpoint_unique_viewport_slide_with_suffix"
        ),
        "continuity_context_expansion_required": (
            "checkpoint_continuity_context_expansion_required"
        ),
        "business_sequence_not_continuous": "checkpoint_not_continuous",
    }
    mapped_result = result_map.get(relation, "checkpoint_not_continuous")
    matched_pairs: list[dict[str, Any]] = []
    for pair in decision.get("matched_pairs") or []:
        if not isinstance(pair, dict):
            continue
        raw_old_index = pair.get("old_index")
        raw_new_index = pair.get("new_index")
        if (
            isinstance(raw_old_index, bool)
            or not isinstance(raw_old_index, int)
            or isinstance(raw_new_index, bool)
            or not isinstance(raw_new_index, int)
        ):
            continue
        old_index = raw_old_index
        new_index = raw_new_index
        if not (
            0 <= old_index < len(frozen)
            and 0 <= new_index < len(ordered_current)
        ):
            continue
        matched_pairs.append(
            {
                "pre_sequence_index": old_index,
                "post_sequence_index": new_index,
                "worker_stable_id": "",
                "post_observation_id": str(
                    ordered_current[new_index].get("observation_id") or ""
                ).strip(),
                "match_basis": "worker_business_viewport_continuity",
                "physical_identity_confirmed": False,
            }
        )
    suffix_indexes = [
        int(index)
        for index in (decision.get("new_suffix_indexes") or [])
        if isinstance(index, int)
        and not isinstance(index, bool)
        and 0 <= index < len(ordered_current)
    ]
    suffix_ids = [
        str(ordered_current[index].get("observation_id") or "").strip()
        for index in suffix_indexes
        if str(ordered_current[index].get("observation_id") or "").strip()
    ]
    prefix_count = min(suffix_indexes) if suffix_indexes else len(
        ordered_current
    )
    return {
        **base,
        "comparison_result": mapped_result,
        "reason": str(decision.get("reason") or ""),
        "matched_pairs": matched_pairs,
        "new_suffix_observation_ids": suffix_ids,
        "old_tail_fully_consumed": relation
        in {
            "business_sequence_equal",
            "unique_tail_append",
            "unique_viewport_slide_with_tail_append",
        },
        "current_prefix_count": prefix_count,
        "continuity_relation": relation,
        "overlap_candidates": list(
            decision.get("overlap_candidates") or []
        ),
    }


def compare_checkpoint_to_observations(
    checkpoint: dict[str, Any],
    observations: list[Any] | None,
    *,
    before_frame_id: str,
    after_frame_id: str,
    current_tail_complete: bool,
    current_empty_viewport_confirmed: bool = False,
    context_expansion_used: bool = False,
    expanded_context_observations: list[Any] | None = None,
) -> dict[str, Any]:
    return _compare_checkpoint_business_continuity_v5(
        checkpoint,
        observations,
        before_frame_id=before_frame_id,
        after_frame_id=after_frame_id,
        current_tail_complete=current_tail_complete,
        current_empty_viewport_confirmed=(
            current_empty_viewport_confirmed
        ),
        context_expansion_used=context_expansion_used,
        expanded_context_observations=expanded_context_observations,
    )
