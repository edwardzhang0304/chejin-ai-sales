from __future__ import annotations

"""Worker-owned identity alignment across UI actions.

Screen coordinates and frame-local media anchors are deliberately absent from
this module.  They may locate an action inside one Sidecar call, but they can
never establish a durable message identity.
"""

import hashlib
from typing import Any

from .message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
)

from .message_contract import canonical_message_identity_text


IDENTITY_STATES = {
    "committed",
    "selected_action",
    "frame_local_unselected",
}


def normalized_content_hash(value: Any) -> str:
    normalized = canonical_message_identity_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _token(value: Any) -> str:
    return str(value or "").strip()


def _message_type(item: dict[str, Any]) -> str:
    explicit = _token(item.get("message_type")).lower()
    if explicit in {"text", "voice", "image", "system"}:
        return explicit
    row_kind = _token(item.get("row_kind")).lower()
    return {
        "text_bubble": "text",
        "voice_bubble": "voice",
        "voice_transcript": "voice",
        "image_bubble": "image",
        "system_message": "system",
    }.get(row_kind, "")


def _source_value(item: dict[str, Any], key: str) -> str:
    direct = _token(item.get(key))
    if direct:
        return direct
    source = item.get("source_message")
    if isinstance(source, dict):
        nested = _token(source.get(key))
        if nested:
            return nested
    return ""


def is_business_observation(item: Any) -> bool:
    return isinstance(item, dict) and _message_type(item) in {
        "text",
        "voice",
        "image",
        "system",
    }


def observation_sequence_item(
    observation: dict[str, Any],
    *,
    sequence_index: int,
) -> dict[str, Any]:
    message_type = _message_type(observation)
    content = observation.get("content_clean")
    if content is None:
        content = observation.get("content")
    action_summary = next(
        (
            observation.get(key)
            for key in (
                "_worker_voice_action_summary",
                "_worker_image_action_summary",
            )
            if isinstance(observation.get(key), dict)
        ),
        {},
    )
    prior_mapping = (
        action_summary.get("confirmed_action_mapping")
        if isinstance(action_summary.get("confirmed_action_mapping"), dict)
        else {}
    )
    return {
        "post_observation_id": _token(observation.get("observation_id")),
        "post_sequence_index": int(sequence_index),
        "sender_role": _token(observation.get("sender_role")).lower(),
        "message_type": message_type,
        "normalized_content_hash": normalized_content_hash(content),
        "native_source_message_id": _source_value(
            observation, "native_source_message_id"
        ),
        # Frame visual ids are diagnostic/click-local evidence only.  Keep
        # them in the sequence report so an incident can be reconstructed,
        # but never use them to establish or reject a cross-round identity.
        "frame_visual_id": _source_value(
            observation, "frame_visual_id"
        ),
        "voice_state": _token(observation.get("voice_state")).lower(),
        "image_visual_fingerprint": _token(
            (
                observation.get("image_physical_anchor") or {}
            ).get("bubble_visual_fingerprint")
            if isinstance(observation.get("image_physical_anchor"), dict)
            else ""
        ).lower(),
        "prior_confirmed_action_id": _token(
            prior_mapping.get("canonical_action_id")
        ),
        "prior_confirmed_action_post_observation_id": _token(
            prior_mapping.get("post_observation_id")
        ),
        "prior_confirmed_action_binding": (
            prior_mapping.get("binding_confirmed") is True
        ),
        "prior_confirmed_action_image_fingerprint": _token(
            action_summary.get("image_visual_fingerprint")
        ).lower(),
    }


def build_post_action_observation_sequence(
    observations: list[Any],
    *,
    confirmed_action_mapping: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the newest business frame after folding action derivatives.

    The mapping is Sidecar-confirmed action evidence.  It may identify the
    transcript row produced by a voice action; that row is represented as the
    selected physical voice rather than appended as another text message.
    """

    mapping = (
        dict(confirmed_action_mapping)
        if isinstance(confirmed_action_mapping, dict)
        else {}
    )
    derivative_ids = {
        _token(value)
        for value in (mapping.get("derived_observation_ids") or [])
        if _token(value)
    }
    selected_post_id = _token(mapping.get("post_observation_id"))
    sequence: list[dict[str, Any]] = []
    for observation in observations:
        if not is_business_observation(observation):
            continue
        observation_id = _token(observation.get("observation_id"))
        if observation_id in derivative_ids and observation_id != selected_post_id:
            continue
        item = observation_sequence_item(
            observation,
            sequence_index=len(sequence),
        )
        if observation_id == selected_post_id:
            item["confirmed_action_id"] = _token(
                mapping.get("canonical_action_id")
            )
        sequence.append(item)
    return sequence


def build_pre_action_identity_sequence(
    observations: list[Any],
    *,
    committed_ids: dict[str, str] | None = None,
    provisional_ids: dict[str, str] | None = None,
    selected_observation_id: str = "",
    canonical_action_id: str = "",
    reserved_worker_stable_id: str = "",
) -> list[dict[str, Any]]:
    committed = {
        _token(key): _token(value)
        for key, value in (committed_ids or {}).items()
        if _token(key) and _token(value)
    }
    provisional = {
        _token(key): _token(value)
        for key, value in (provisional_ids or {}).items()
        if _token(key) and _token(value)
    }
    selected_id = _token(selected_observation_id)
    action_id = _token(canonical_action_id)
    reserved_id = _token(reserved_worker_stable_id)
    if selected_id and (not action_id or not reserved_id):
        raise ValueError("C2_SELECTED_ACTION_IDENTITY_MISSING")

    sequence: list[dict[str, Any]] = []
    for observation in observations:
        if not is_business_observation(observation):
            continue
        observation_id = _token(observation.get("observation_id"))
        base = observation_sequence_item(
            observation,
            sequence_index=len(sequence),
        )
        item = {
            "pre_observation_id": observation_id,
            "pre_sequence_index": base.pop("post_sequence_index"),
            **{
                key: value
                for key, value in base.items()
                if key != "post_observation_id"
            },
        }
        if observation_id == selected_id:
            item.update(
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                }
            )
        elif observation_id in committed:
            item.update(
                {
                    "identity_state": "committed",
                    "worker_stable_id": committed[observation_id],
                }
            )
        else:
            item["identity_state"] = "frame_local_unselected"
            if observation_id in provisional:
                item["provisional_worker_stable_id"] = provisional[
                    observation_id
                ]
        sequence.append(item)
    return sequence


def _strong_anchor_basis(
    pre: dict[str, Any],
    post: dict[str, Any],
    confirmed_action_mapping: dict[str, Any],
) -> str:
    pre_native = _token(pre.get("native_source_message_id"))
    post_native = _token(post.get("native_source_message_id"))
    if pre_native and post_native and pre_native == post_native:
        return "native_source_message_id"
    if pre.get("identity_state") == "selected_action":
        action_id = _token(pre.get("canonical_action_id"))
        if (
            action_id
            and action_id == _token(
                confirmed_action_mapping.get("canonical_action_id")
            )
            and _token(post.get("post_observation_id"))
            == _token(confirmed_action_mapping.get("post_observation_id"))
            and confirmed_action_mapping.get("binding_confirmed") is True
        ):
            return "confirmed_action"
    prior_action_id = _token(pre.get("prior_confirmed_action_id"))
    prior_post_id = _token(
        pre.get("prior_confirmed_action_post_observation_id")
    )
    if (
        prior_action_id
        and prior_post_id
        and pre.get("prior_confirmed_action_binding") is True
        and prior_post_id == _token(pre.get("pre_observation_id"))
        and prior_post_id == _token(post.get("post_observation_id"))
    ):
        if _token(pre.get("message_type")) == "image":
            expected_fingerprint = _token(
                pre.get("prior_confirmed_action_image_fingerprint")
            ).lower()
            if (
                not expected_fingerprint
                or expected_fingerprint
                != _token(post.get("image_visual_fingerprint")).lower()
            ):
                return ""
        return "prior_confirmed_action"
    return ""


def _compatible(
    pre: dict[str, Any],
    post: dict[str, Any],
    confirmed_action_mapping: dict[str, Any],
) -> tuple[bool, str]:
    if _token(pre.get("sender_role")) != _token(post.get("sender_role")):
        return False, ""
    if _token(pre.get("message_type")) != _token(post.get("message_type")):
        return False, ""

    strong_basis = _strong_anchor_basis(
        pre, post, confirmed_action_mapping
    )
    pre_native = _token(pre.get("native_source_message_id"))
    post_native = _token(post.get("native_source_message_id"))
    if pre_native and post_native and pre_native != post_native:
        return False, ""
    if pre.get("identity_state") == "selected_action" and not strong_basis:
        return False, ""
    message_type = _token(pre.get("message_type"))
    if message_type in {"text", "system"} and _token(
        pre.get("normalized_content_hash")
    ) != _token(post.get("normalized_content_hash")):
        return False, ""
    return True, strong_basis or "ordered_compatible"


def _candidate(
    pre: list[dict[str, Any]],
    post: list[dict[str, Any]],
    *,
    pre_start: int,
    post_start: int,
    confirmed_action_mapping: dict[str, Any],
) -> dict[str, Any] | None:
    pre_suffix = pre[pre_start:]
    if not pre_suffix or post_start + len(pre_suffix) > len(post):
        return None
    bases: list[str] = []
    for offset, pre_item in enumerate(pre_suffix):
        compatible, basis = _compatible(
            pre_item,
            post[post_start + offset],
            confirmed_action_mapping,
        )
        if not compatible:
            return None
        bases.append(basis)
    strong_count = sum(basis != "ordered_compatible" for basis in bases)
    committed_count = sum(
        item.get("identity_state") == "committed" for item in pre_suffix
    )
    weak_text_context_count = sum(
        item.get("identity_state") == "committed"
        and _token(item.get("message_type")) in {"text", "system"}
        for item in pre_suffix
    )
    # Weak voice/image rows need their own proof.  A text row on only one side
    # proves that text row, not the neighbouring media: when the old media is
    # clipped or missed, a new same-role media row can otherwise inherit its
    # worker id.  A weak committed media row is reusable only when two
    # already-matched historical boundaries enclose it in this complete,
    # contiguous frame.  Native ids and confirmed actions remain self-proving.
    boundary_offsets = {
        offset
        for offset, (item, basis) in enumerate(zip(pre_suffix, bases, strict=True))
        if basis != "ordered_compatible"
        or (
            item.get("identity_state") == "committed"
            and _token(item.get("message_type")) in {"text", "system"}
        )
    }
    weak_media_offsets = [
        offset
        for offset, (item, basis) in enumerate(zip(pre_suffix, bases, strict=True))
        if basis == "ordered_compatible"
        and item.get("identity_state") == "committed"
        and _token(item.get("message_type")) in {"voice", "image"}
    ]
    weak_media_proven = all(
        pre_suffix[offset].get("identity_state") == "committed"
        and any(boundary < offset for boundary in boundary_offsets)
        and any(boundary > offset for boundary in boundary_offsets)
        for offset in weak_media_offsets
    )
    if weak_media_proven:
        for offset in weak_media_offsets:
            bases[offset] = "two_sided_historical_context"

    general_context_proven = strong_count > 0 or (
        committed_count > 1 and weak_text_context_count > 0
    )
    proof_sufficient = general_context_proven and weak_media_proven
    return {
        "pre_start": pre_start,
        "post_start": post_start,
        "length": len(pre_suffix),
        "bases": bases,
        "proof_sufficient": proof_sufficient,
        "weak_media_proven": weak_media_proven,
        "single_weak_text_candidate": bool(
            len(pre_suffix) == 1
            and pre_suffix[0].get("identity_state") == "committed"
            and _token(pre_suffix[0].get("message_type"))
            in {"text", "system"}
            and strong_count == 0
        ),
        "ai_reply_boundary_candidate": bool(
            len(pre_suffix) == 1
            and pre_suffix[0].get("ai_reply_boundary") is True
            and pre_suffix[0].get("identity_state") == "committed"
            and _token(pre_suffix[0].get("message_type")) == "text"
        ),
        "contains_ai_reply_boundary": any(
            item.get("ai_reply_boundary") is True
            and item.get("identity_state") == "committed"
            and _token(item.get("message_type")) == "text"
            for item in pre_suffix
        ),
    }


def _base_evidence(
    *,
    pre_sequence_source: str,
    pre_frame_id: str,
    post_frame_id: str,
) -> dict[str, Any]:
    source = _token(pre_sequence_source)
    before = _token(pre_frame_id)
    after = _token(post_frame_id)
    if source not in {"action_frame", "checkpoint", "empty_checkpoint"}:
        raise ValueError("C2_PRE_SEQUENCE_SOURCE_INVALID")
    if not before or not after or before == after:
        raise ValueError("C2_SEQUENCE_FRAME_ID_INVALID")
    return {
        "pre_sequence_source": source,
        "pre_frame_id": before,
        "post_frame_id": after,
        "alignment_status": "unresolved",
        "candidate_alignment_count": 0,
        "matched_pairs": [],
        "old_tail_fully_consumed": False,
        "new_suffix_observation_ids": [],
    }


def align_committed_message_sequence(
    pre_sequence: list[dict[str, Any]],
    post_sequence: list[dict[str, Any]],
    confirmed_action_mapping: dict[str, Any] | None = None,
    *,
    pre_sequence_source: str,
    pre_frame_id: str,
    post_frame_id: str,
) -> dict[str, Any]:
    """Find the only safe monotonic suffix alignment.

    A pre suffix maps to one contiguous post segment.  This permits viewport
    clipping at the top while ensuring no unexplained business message appears
    between matched history.  Only a uniquely proven match may expose a new
    post tail.
    """

    evidence = _base_evidence(
        pre_sequence_source=pre_sequence_source,
        pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id,
    )
    if pre_sequence_source == "empty_checkpoint":
        if pre_sequence:
            raise ValueError("C2_EMPTY_CHECKPOINT_PRE_SEQUENCE_NOT_EMPTY")
        evidence.update(
            {
                "alignment_status": "not_required",
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    _token(item.get("post_observation_id"))
                    for item in post_sequence
                    if _token(item.get("post_observation_id"))
                ],
            }
        )
        return evidence

    mapping = (
        dict(confirmed_action_mapping)
        if isinstance(confirmed_action_mapping, dict)
        else {}
    )
    raw_candidates: list[dict[str, Any]] = []
    for pre_start in range(len(pre_sequence)):
        for post_start in range(len(post_sequence)):
            candidate = _candidate(
                pre_sequence,
                post_sequence,
                pre_start=pre_start,
                post_start=post_start,
                confirmed_action_mapping=mapping,
            )
            if candidate is not None:
                action_post_id = _token(mapping.get("post_observation_id"))
                action_appended = bool(
                    mapping.get("action_appended") is True
                    and mapping.get("binding_confirmed") is True
                    and _token(mapping.get("canonical_action_id"))
                    and _token(mapping.get("reserved_worker_stable_id"))
                    and action_post_id
                    and post_start + int(candidate["length"])
                    < len(post_sequence)
                    and _token(
                        post_sequence[
                            post_start + int(candidate["length"])
                        ].get("post_observation_id")
                    )
                    == action_post_id
                )
                if mapping.get("action_appended") is True:
                    candidate["proof_sufficient"] = bool(
                        candidate["proof_sufficient"] and action_appended
                    )
                if candidate["proof_sufficient"] and action_appended:
                    candidate["appended_action_proof"] = True
                raw_candidates.append(candidate)
    # One historical text/system row is sufficient only for the ordinary
    # "old row + new tail" case: there must be exactly one monotonic
    # explanation, it must begin at the visible top, and it must expose a
    # non-empty tail. A lone repeated "好的" therefore remains ambiguous.
    single_boundary_candidates = [
        item
        for item in raw_candidates
        if item.get("ai_reply_boundary_candidate") is True
    ]
    if len(single_boundary_candidates) == 1:
        # A confirmed sent_ack identifies which equal-text self bubble is the
        # current reply boundary. Rows above that bubble are already-history
        # and need not reacquire weak media identities merely to prove the
        # latest unreplied tail. Ordinary candidates cannot outvote that
        # explicit action identity.
        single_boundary_candidates[0]["proof_sufficient"] = True
        boundary_candidates = [
            item
            for item in raw_candidates
            if item.get("contains_ai_reply_boundary") is True
            and item.get("proof_sufficient") is True
        ]
        for item in raw_candidates:
            if item not in boundary_candidates:
                item["proof_sufficient"] = False
    if len(raw_candidates) == 1:
        weak = raw_candidates[0]
        if (
            weak.get("single_weak_text_candidate") is True
            and int(weak["post_start"]) == 0
            and int(weak["post_start"]) + int(weak["length"])
            < len(post_sequence)
        ):
            weak["proof_sufficient"] = True
        if (
            weak.get("ai_reply_boundary_candidate") is True
            and int(weak["post_start"]) == 0
            and int(weak["length"]) == len(post_sequence)
        ):
            # A backend sent_ack plus its committed Worker stable identity is
            # a stronger boundary than an ordinary repeated text row.  It may
            # prove the exact no-new-message frame without inventing a tail.
            weak["proof_sufficient"] = True
    proven = [item for item in raw_candidates if item["proof_sufficient"]]
    if proven:
        # A shorter suffix contained in a valid longer alignment is not a
        # second physical explanation.  Compare only candidates that consume
        # the greatest available historical context.
        maximal_length = max(int(item["length"]) for item in proven)
        proven = [
            item for item in proven if int(item["length"]) == maximal_length
        ]
    if len(proven) != 1:
        if raw_candidates:
            evidence["alignment_status"] = "ambiguous"
            evidence["candidate_alignment_count"] = max(
                2, len(proven) or len(raw_candidates)
            )
        return evidence

    candidate = proven[0]
    pre_start = int(candidate["pre_start"])
    post_start = int(candidate["post_start"])
    length = int(candidate["length"])
    pairs: list[dict[str, Any]] = []
    for offset in range(length):
        pre_item = pre_sequence[pre_start + offset]
        post_item = post_sequence[post_start + offset]
        state = _token(pre_item.get("identity_state"))
        if state not in IDENTITY_STATES:
            raise ValueError("C2_PRE_SEQUENCE_IDENTITY_STATE_INVALID")
        inherited_id = ""
        if state == "committed":
            inherited_id = _token(pre_item.get("worker_stable_id"))
            if not inherited_id:
                raise ValueError("C2_COMMITTED_SEQUENCE_ID_MISSING")
        elif state == "selected_action":
            inherited_id = _token(
                pre_item.get("reserved_worker_stable_id")
            )
            if not inherited_id:
                raise ValueError("C2_RESERVED_SEQUENCE_ID_MISSING")
        pair = {
            "identity_state": state,
            "worker_stable_id": inherited_id or None,
            "pre_observation_id": _token(
                pre_item.get("pre_observation_id")
            ),
            "post_observation_id": _token(
                post_item.get("post_observation_id")
            ),
            "pre_index": int(pre_item.get("pre_sequence_index") or 0),
            "post_index": int(
                post_item.get("post_sequence_index") or 0
            ),
            "match_basis": candidate["bases"][offset],
        }
        if state == "frame_local_unselected":
            provisional_id = _token(
                pre_item.get("provisional_worker_stable_id")
            )
            if provisional_id:
                pair["provisional_worker_stable_id"] = provisional_id
        pairs.append(pair)
    suffix_start = post_start + length
    evidence.update(
        {
            "alignment_status": "unique",
            "candidate_alignment_count": 1,
            "matched_pairs": pairs,
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": [
                _token(item.get("post_observation_id"))
                for item in post_sequence[suffix_start:]
                if _token(item.get("post_observation_id"))
            ],
        }
    )
    selected_action_pairs = [
        pair
        for pair in pairs
        if pair.get("identity_state") == "selected_action"
    ]
    if (
        len(selected_action_pairs) == 1
        and mapping.get("binding_confirmed") is True
        and _token(mapping.get("canonical_action_id"))
        and _token(mapping.get("reserved_worker_stable_id"))
        and _token(mapping.get("pre_observation_id"))
        == _token(selected_action_pairs[0].get("pre_observation_id"))
        and _token(mapping.get("post_observation_id"))
        == _token(selected_action_pairs[0].get("post_observation_id"))
    ):
        evidence["confirmed_action_mapping"] = {
            "canonical_action_id": _token(
                mapping.get("canonical_action_id")
            ),
            "reserved_worker_stable_id": _token(
                mapping.get("reserved_worker_stable_id")
            ),
            **(
                {
                    "selected_action_token": _token(
                        mapping.get("selected_action_token")
                    )
                }
                if _token(mapping.get("selected_action_token"))
                else {}
            ),
            "pre_observation_id": _token(
                mapping.get("pre_observation_id")
            ),
            "post_observation_id": _token(
                mapping.get("post_observation_id")
            ),
            "binding_confirmed": True,
            "action_appended": bool(
                candidate.get("appended_action_proof")
            ),
        }
    return evidence


def inherited_worker_ids(
    evidence: dict[str, Any],
) -> dict[str, str]:
    if evidence.get("alignment_status") not in {"unique", "not_required"}:
        return {}
    return {
        _token(pair.get("post_observation_id")): _token(
            pair.get("worker_stable_id")
        )
        for pair in (evidence.get("matched_pairs") or [])
        if isinstance(pair, dict)
        and _token(pair.get("post_observation_id"))
        and _token(pair.get("worker_stable_id"))
    }


def apply_inherited_worker_ids(
    observations: list[Any],
    evidence: dict[str, Any],
) -> list[Any]:
    identities = inherited_worker_ids(evidence)
    pairs_by_observation = {
        _token(pair.get("post_observation_id")): pair
        for pair in (evidence.get("matched_pairs") or [])
        if isinstance(pair, dict)
        and _token(pair.get("post_observation_id"))
    }
    provisional_identities = {
        _token(pair.get("post_observation_id")): _token(
            pair.get("provisional_worker_stable_id")
        )
        for pair in (evidence.get("matched_pairs") or [])
        if isinstance(pair, dict)
        and pair.get("identity_state") == "frame_local_unselected"
        and _token(pair.get("post_observation_id"))
        and _token(pair.get("provisional_worker_stable_id"))
    }
    result: list[Any] = []
    for raw in observations:
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        observation = dict(raw)
        observation_id = _token(observation.get("observation_id"))
        observation.pop("_worker_stable_id", None)
        observation.pop("_worker_identity_scope", None)
        observation.pop("_worker_committed_message", None)
        if observation_id in identities:
            stable_id = identities[observation_id]
            observation["_worker_stable_id"] = stable_id
            observation["_worker_identity_scope"] = "committed"
            pair = pairs_by_observation.get(observation_id) or {}
            basis = (
                MessageCommitBasis.CONFIRMED_SENT_ACK
                if pair.get("identity_state") == "selected_action"
                and str(observation.get("message_type") or "").strip().lower()
                == "text"
                and str(observation.get("sender_role") or "").strip().lower()
                == "self"
                else MessageCommitBasis.CONFIRMED_VOICE_ACTION
                if pair.get("identity_state") == "selected_action"
                and str(observation.get("message_type") or "").strip().lower()
                == "voice"
                else MessageCommitBasis.CONFIRMED_IMAGE_ACTION
                if pair.get("identity_state") == "selected_action"
                and str(observation.get("message_type") or "").strip().lower()
                == "image"
                else MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT
            )
            proof = {
                "alignment_status": str(
                    evidence.get("alignment_status") or ""
                ),
                "worker_stable_id": stable_id,
                "pre_observation_id": _token(
                    pair.get("pre_observation_id")
                ),
                "post_observation_id": observation_id,
                "match_basis": _token(pair.get("match_basis")),
            }
            mapping = evidence.get("confirmed_action_mapping")
            if (
                pair.get("identity_state") == "selected_action"
                and isinstance(mapping, dict)
            ):
                proof.update(dict(mapping))
            observation["_worker_committed_message"] = (
                committed_identity_record(
                    worker_stable_id=stable_id,
                    commit_basis=basis,
                    observation_id=observation_id,
                    sender_role=str(observation.get("sender_role") or ""),
                    message_type=str(observation.get("message_type") or ""),
                    proof=proof,
                )
            )
        elif observation_id in provisional_identities:
            observation["_worker_stable_id"] = provisional_identities[
                observation_id
            ]
            observation["_worker_identity_scope"] = (
                "current_read_provisional"
            )
        result.append(observation)
    return result
