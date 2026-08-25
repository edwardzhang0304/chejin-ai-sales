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


CHECKPOINT_REVISION = 3
CHECKPOINT_RESULTS = {
    "checkpoint_equal",
    "checkpoint_unique_prefix_with_suffix",
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
    image_visual_fingerprint: object = "",
    error_code: object = "",
) -> str:
    normalized_type = str(message_type or "").strip().lower()
    normalized_state = str(item_state or "").strip().lower()
    material: dict[str, str] = {
        "sender_role": str(sender_role or "").strip().lower(),
        "message_type": normalized_type,
        "item_state": normalized_state,
    }
    if normalized_type == "image":
        material["image_visual_fingerprint"] = str(
            image_visual_fingerprint or ""
        ).strip().lower()
    else:
        material["normalized_content_hash"] = hashlib.sha256(
            canonical_message_identity_text(content).encode("utf-8")
        ).hexdigest()
        if normalized_type == "voice":
            material["voice_duration"] = _normalize_voice_duration_value(
                voice_duration
            )
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
    for item in tail:
        if not isinstance(item, dict):
            return "checkpoint_item_invalid"
        stable_id = str(item.get("worker_stable_id") or "").strip()
        role = str(item.get("sender_role") or "").strip().lower()
        message_type = str(item.get("message_type") or "").strip().lower()
        item_state = str(item.get("item_state") or "").strip().lower()
        signature = str(item.get("stable_fact_signature") or "").strip().lower()
        continuity_basis = str(
            item.get("continuity_basis") or ""
        ).strip()
        continuity_signature = str(
            item.get("continuity_signature") or ""
        ).strip().lower()
        commit_basis = str(item.get("commit_basis") or "").strip()
        action_receipt_digest = str(
            item.get("action_receipt_digest") or ""
        ).strip().lower()
        reply_fact_evidence = item.get("reply_fact_evidence")
        physical_identity_confirmed = item.get(
            "physical_identity_confirmed"
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
        terminal_reply_fact_valid = bool(
            reply_fact_matches_item
            and (
                (
                    message_type == "voice"
                    and _is_sha256(
                        reply_fact_evidence.get(
                            "normalized_transcript_sha256"
                        )
                    )
                )
                or (
                    message_type == "image"
                    and _is_sha256(
                        reply_fact_evidence.get(
                            "exact_image_content_sha256"
                        )
                    )
                )
            )
        )
        if (
            not stable_id
            or stable_id in seen_stable_ids
            or role not in {"customer", "self", "system"}
            or message_type not in {"text", "voice", "image", "system"}
            or item_state not in {"completed", "failed"}
            or not _is_sha256(signature)
            or continuity_basis not in {
                "ordered_fact",
                "native_source_message_id",
                "two_sided_static_context",
                "terminal_committed_fact_equivalence",
                "unproven_media_continuity",
            }
            or (
                continuity_basis == "unproven_media_continuity"
                and continuity_signature
            )
            or (
                continuity_basis != "unproven_media_continuity"
                and not _is_sha256(continuity_signature)
            )
            or not isinstance(reply_fact_evidence, dict)
            or not isinstance(physical_identity_confirmed, bool)
            or (
                message_type in {"voice", "image"}
                and continuity_basis
                not in {
                    "native_source_message_id",
                    "two_sided_static_context",
                    "terminal_committed_fact_equivalence",
                    "unproven_media_continuity",
                }
            )
            or (
                message_type in {"text", "system"}
                and continuity_basis != "ordered_fact"
            )
            or (
                continuity_basis == "terminal_committed_fact_equivalence"
                and (
                    commit_basis not in TERMINAL_ACTION_COMMIT_BASES
                    or commit_basis
                    != f"confirmed_{message_type}_action"
                    or not _is_sha256(action_receipt_digest)
                    or physical_identity_confirmed is not False
                    or not terminal_reply_fact_valid
                )
            )
            or (
                continuity_basis in {
                    "native_source_message_id",
                    "two_sided_static_context",
                }
                and physical_identity_confirmed is not True
            )
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
    anchor = (
        observation.get("image_physical_anchor")
        if isinstance(observation.get("image_physical_anchor"), dict)
        else {}
    )
    return str(anchor.get("bubble_visual_fingerprint") or "").strip().lower()


def _native_source_message_id(observation: dict[str, Any]) -> str:
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    explicit = str(
        observation.get("native_source_message_id")
        or source.get("native_source_message_id")
        or ""
    ).strip()
    if explicit:
        return explicit
    adapter = str(
        observation.get("source_adapter")
        or source.get("source_adapter")
        or ""
    ).strip().lower()
    raw_id = str(
        source.get("id")
        or source.get("message_id")
        or observation.get("source_message_id")
        or ""
    ).strip()
    if (
        raw_id
        and adapter not in {
            "win32_ocr",
            "wechat_win32_ocr",
            "rpa_ocr",
            "ocr_rpa",
        }
        and not raw_id.lower().startswith(
            (
                "win32_ocr:",
                "wechat_win32_ocr:",
                "ocr:",
                "screen_ocr:",
                "uia_ocr:",
            )
        )
    ):
        return raw_id
    return ""


def _media_continuity_anchor(
    observation: dict[str, Any],
    *,
    message_type: str,
) -> dict[str, Any]:
    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    if message_type == "voice":
        return {
            "voice_anchor": str(
                observation.get("parent_voice_anchor_key")
                or observation.get("voice_anchor_key")
                or source.get("parent_voice_anchor_key")
                or source.get("voice_anchor_stable_key")
                or source.get("voice_anchor_key")
                or ""
            ).strip(),
            "voice_duration": str(
                observation.get("voice_duration")
                or source.get("voice_duration")
                or observation.get("voice_duration_text")
                or source.get("voice_duration_text")
                or ""
            ).strip(),
        }
    if message_type == "image":
        anchor = (
            observation.get("image_physical_anchor")
            if isinstance(observation.get("image_physical_anchor"), dict)
            else {}
        )
        return {
            "bubble_visual_fingerprint": str(
                anchor.get("bubble_visual_fingerprint") or ""
            ).strip().lower(),
            "preceding_stable_message": str(
                anchor.get("preceding_stable_message") or ""
            ).strip(),
            "following_stable_message": str(
                anchor.get("following_stable_message") or ""
            ).strip(),
            "occurrence_index": anchor.get("occurrence_index"),
            "occurrence_count": anchor.get("occurrence_count"),
        }
    return {}


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
                    image_visual_fingerprint=image_fingerprint,
                    error_code=raw.get("error_code") or "",
                ),
                "reply_fact_evidence": reply_fact_evidence_for_observation(
                    raw,
                    item_state=item_state,
                ),
                "_native_source_message_id": (
                    _native_source_message_id(raw)
                ),
                "_media_continuity_anchor": (
                    _media_continuity_anchor(
                        raw,
                        message_type=message_type,
                    )
                ),
            }
        )
    for index, item in enumerate(sequence):
        native_id = str(
            item.pop("_native_source_message_id", "") or ""
        ).strip()
        media_anchor = item.pop("_media_continuity_anchor", {})
        signatures = {
            "ordered_fact": str(
                item.get("stable_fact_signature") or ""
            )
        }
        if native_id:
            signatures["native_source_message_id"] = canonical_sha256(
                {
                    "message_type": item.get("message_type"),
                    "native_source_message_id": native_id,
                }
            )
        if item.get("message_type") in {"voice", "image"}:
            before = (
                str(
                    sequence[index - 1].get("stable_fact_signature") or ""
                )
                if index > 0
                else ""
            )
            after = (
                str(
                    sequence[index + 1].get("stable_fact_signature") or ""
                )
                if index + 1 < len(sequence)
                else ""
            )
            anchor_present = bool(
                isinstance(media_anchor, dict)
                and any(
                    value not in (None, "", [], {})
                    for value in media_anchor.values()
                )
            )
            if before and after and anchor_present:
                signatures["two_sided_static_context"] = canonical_sha256(
                    {
                        "message_type": item.get("message_type"),
                        "media_anchor": media_anchor,
                        "before_fact_signature": before,
                        "after_fact_signature": after,
                    }
                )
        item["continuity_signatures"] = signatures
    return sequence, errors


def compare_checkpoint_to_observations(
    checkpoint: dict[str, Any],
    observations: list[Any] | None,
    *,
    before_frame_id: str,
    after_frame_id: str,
    current_tail_complete: bool,
) -> dict[str, Any]:
    current, errors = observation_fact_sequence(observations)
    frozen = [
        dict(item)
        for item in (checkpoint.get("committed_tail") or [])
        if isinstance(item, dict)
    ]
    base = {
        "comparison_result": "checkpoint_not_continuous",
        "before_frame_id": str(before_frame_id or "").strip(),
        "after_frame_id": str(after_frame_id or "").strip(),
        "checkpoint_count": len(frozen),
        "current_count": len(current),
        "matched_pairs": [],
        "new_suffix_observation_ids": [],
        "old_tail_fully_consumed": False,
        "observation_errors": errors,
        "physical_identity_confirmed": True,
        "terminal_fact_equivalence_count": 0,
    }
    if errors:
        base["reason"] = str(errors[0].get("reason") or "observation_invalid")
        return base
    if not current_tail_complete:
        base["reason"] = "checkpoint_current_tail_incomplete"
        return base
    baseline_kind = str(
        checkpoint.get("baseline_kind") or ""
    ).strip()
    if baseline_kind == "friend_welcome_empty":
        if frozen:
            base["reason"] = "empty_baseline_contains_committed_facts"
            return base
        suffix_ids = [
            str(item.get("observation_id") or "").strip()
            for item in current
            if str(item.get("observation_id") or "").strip()
        ]
        base.update(
            {
                "comparison_result": (
                    "checkpoint_equal"
                    if not current
                    else "checkpoint_unique_prefix_with_suffix"
                ),
                "reason": (
                    "friend_welcome_empty_baseline_still_empty"
                    if not current
                    else "friend_welcome_cancelled_by_new_message_suffix"
                ),
                "new_suffix_observation_ids": suffix_ids,
                "old_tail_fully_consumed": True,
            }
        )
        return base
    if len(current) < len(frozen):
        base["reason"] = "checkpoint_rows_missing_or_viewport_truncated"
        return base
    matched_pairs: list[dict[str, Any]] = []
    for index, expected in enumerate(frozen):
        actual = current[index]
        if (
            str(expected.get("sender_role") or "").strip().lower()
            != actual["sender_role"]
            or str(expected.get("message_type") or "").strip().lower()
            != actual["message_type"]
            or str(expected.get("item_state") or "").strip().lower()
            != actual["item_state"]
            or str(expected.get("stable_fact_signature") or "").strip().lower()
            != actual["stable_fact_signature"]
        ):
            base.update(
                {
                    "reason": "checkpoint_prefix_fact_mismatch",
                    "mismatch_screen_order": index + 1,
                }
            )
            return base
        message_type = str(
            expected.get("message_type") or ""
        ).strip().lower()
        continuity_basis = str(
            expected.get("continuity_basis") or ""
        ).strip()
        expected_continuity = str(
            expected.get("continuity_signature") or ""
        ).strip().lower()
        actual_continuity = str(
            (actual.get("continuity_signatures") or {}).get(
                continuity_basis
            )
            or ""
        ).strip().lower()
        match_basis = "pre_send_fact_checkpoint"
        physical_identity_confirmed = True
        if message_type in {"voice", "image"}:
            if continuity_basis == "terminal_committed_fact_equivalence":
                expected_reply_fact = expected.get("reply_fact_evidence")
                actual_reply_fact = actual.get("reply_fact_evidence")
                terminal_equivalent = bool(
                    index == len(frozen) - 1
                    and current_tail_complete
                    and str(expected.get("commit_basis") or "").strip()
                    in TERMINAL_ACTION_COMMIT_BASES
                    and len(
                        str(expected.get("action_receipt_digest") or "")
                        .strip()
                        .lower()
                    )
                    == 64
                    and isinstance(expected_reply_fact, dict)
                    and expected_reply_fact
                    and actual_reply_fact == expected_reply_fact
                    and expected_continuity
                    == canonical_sha256(expected_reply_fact)
                )
                if not terminal_equivalent:
                    base.update(
                        {
                            "reason": (
                                "checkpoint_terminal_fact_equivalence_incomplete"
                            ),
                            "mismatch_screen_order": index + 1,
                            "continuity_basis": continuity_basis,
                        }
                    )
                    return base
                match_basis = "terminal_committed_fact_equivalence"
                physical_identity_confirmed = False
                base["physical_identity_confirmed"] = False
                base["terminal_fact_equivalence_count"] = int(
                    base["terminal_fact_equivalence_count"]
                ) + 1
            elif (
                continuity_basis
                not in {
                    "native_source_message_id",
                    "two_sided_static_context",
                }
                or continuity_basis == "unproven_media_continuity"
                or not expected_continuity
                or actual_continuity != expected_continuity
            ):
                base.update(
                    {
                        "reason": (
                            "checkpoint_media_continuity_unproven"
                            if continuity_basis
                            == "unproven_media_continuity"
                            else "checkpoint_media_continuity_mismatch"
                        ),
                        "mismatch_screen_order": index + 1,
                        "continuity_basis": continuity_basis,
                    }
                )
                return base
        matched_pairs.append(
            {
                "pre_sequence_index": index,
                "post_sequence_index": index,
                # Fact equivalence deliberately does not attach the frozen
                # durable identity to the fresh Win32 observation.  The old
                # stable id remains only inside the immutable checkpoint.
                "worker_stable_id": (
                    ""
                    if not physical_identity_confirmed
                    else str(
                        expected.get("worker_stable_id") or ""
                    ).strip()
                ),
                "post_observation_id": actual["observation_id"],
                "match_basis": match_basis,
                "physical_identity_confirmed": (
                    physical_identity_confirmed
                ),
            }
        )
    new_suffix_ids = [
        str(item.get("observation_id") or "").strip()
        for item in current[len(frozen) :]
        if str(item.get("observation_id") or "").strip()
    ]
    base.update(
        {
            "comparison_result": (
                "checkpoint_equal"
                if len(current) == len(frozen)
                else "checkpoint_unique_prefix_with_suffix"
            ),
            "reason": (
                "ordered_facts_equal"
                if len(current) == len(frozen)
                else "ordered_checkpoint_is_unique_prefix"
            ),
            "matched_pairs": matched_pairs,
            "new_suffix_observation_ids": new_suffix_ids,
            "old_tail_fully_consumed": True,
        }
    )
    return base
