from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RuntimeIdentityObject(str, Enum):
    FRAME_OBSERVATION = "frame_observation"
    PENDING_MEDIA_ACTION = "pending_media_action"
    COMMITTED_MESSAGE = "committed_message"
    QUARANTINE_RECORD = "quarantine_record"


class MessageCommitBasis(str, Enum):
    HISTORICAL_CHECKPOINT_ALIGNMENT = "historical_checkpoint_alignment"
    NEW_SUFFIX = "new_suffix"
    CONFIRMED_VOICE_ACTION = "confirmed_voice_action"
    CONFIRMED_IMAGE_ACTION = "confirmed_image_action"
    CONFIRMED_SENT_ACK = "confirmed_sent_ack"
    NATIVE_SOURCE_MESSAGE_ID = "native_source_message_id"


class MediaActionTerminal(str, Enum):
    CANCELLED_BEFORE_TRIGGER = "cancelled_before_trigger"
    COMMITTED_COMPLETED = "committed_completed"
    COMMITTED_FAILED = "committed_failed"
    IDENTITY_UNRESOLVED = "identity_unresolved"


@dataclass(frozen=True, slots=True)
class CommittedMessage:
    conversation_id: str
    observation_id: str
    worker_stable_id: str
    sender_role: str
    message_type: str
    commit_basis: MessageCommitBasis
    source_message_key: str
    proof: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IdentityCommitRejection:
    error_code: str
    reason: str
    observation_id: str = ""


IdentityCommitResult = CommittedMessage | IdentityCommitRejection


_WORKER_STABLE_ID_RE = re.compile(r"worker-message-[1-9][0-9]*")
_ROW_KIND_TO_MESSAGE_TYPE = {
    "text_bubble": "text",
    "system_row": "system",
    "system_message": "system",
    "voice_bubble": "voice",
    "voice_transcript": "voice",
    "image_bubble": "image",
}


def committed_identity_record(
    *,
    worker_stable_id: str,
    commit_basis: MessageCommitBasis | str,
    observation_id: str,
    sender_role: str,
    message_type: str,
    proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the sole serializable input accepted by the commit gate."""

    basis = (
        commit_basis
        if isinstance(commit_basis, MessageCommitBasis)
        else MessageCommitBasis(str(commit_basis))
    )
    return {
        "object_type": RuntimeIdentityObject.COMMITTED_MESSAGE.value,
        "worker_stable_id": str(worker_stable_id or "").strip(),
        "commit_basis": basis.value,
        "observation_id": str(observation_id or "").strip(),
        "sender_role": str(sender_role or "").strip().lower(),
        "message_type": str(message_type or "").strip().lower(),
        "proof": dict(proof),
    }


def commit_message_identity(
    *,
    conversation_id: str,
    observation: Mapping[str, Any],
) -> IdentityCommitResult:
    """Convert one runtime observation into the only durable message type.

    Consumers must use the returned ``CommittedMessage``.  Missing, blank,
    unknown or contradictory state is rejected; no stable-id-only fallback
    exists here.
    """

    clean_conversation_id = str(conversation_id or "").strip()
    observation_id = str(observation.get("observation_id") or "").strip()
    if not clean_conversation_id:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "conversation_id_missing", observation_id)

    raw_record = observation.get("_worker_committed_message")
    if not isinstance(raw_record, Mapping):
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "committed_message_record_missing", observation_id)
    if str(raw_record.get("object_type") or "").strip() != RuntimeIdentityObject.COMMITTED_MESSAGE.value:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "runtime_object_not_committed_message", observation_id)

    record_observation_id = str(raw_record.get("observation_id") or "").strip()
    stable_id = str(raw_record.get("worker_stable_id") or "").strip()
    sender_role = str(raw_record.get("sender_role") or "").strip().lower()
    message_type = str(raw_record.get("message_type") or "").strip().lower()
    row_kind = str(observation.get("row_kind") or "").strip().lower()
    observed_role = str(observation.get("sender_role") or "").strip().lower()
    observed_type = str(observation.get("message_type") or "").strip().lower()
    proof = raw_record.get("proof")
    if not all((observation_id, record_observation_id, stable_id, sender_role, message_type)):
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "committed_message_field_missing", observation_id)
    if observation_id != record_observation_id:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "observation_id_mismatch", observation_id)
    if not _WORKER_STABLE_ID_RE.fullmatch(stable_id):
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "worker_stable_id_invalid", observation_id)
    if str(observation.get("_worker_stable_id") or "").strip() != stable_id:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "stable_id_projection_mismatch", observation_id)
    if str(observation.get("_worker_identity_scope") or "").strip() != "committed":
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "identity_scope_not_committed", observation_id)
    if sender_role != observed_role or message_type != observed_type:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "role_or_type_mismatch", observation_id)
    if _ROW_KIND_TO_MESSAGE_TYPE.get(row_kind) != message_type:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "row_kind_message_type_mismatch", observation_id)
    if not isinstance(proof, Mapping):
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "commit_proof_missing", observation_id)
    try:
        basis = MessageCommitBasis(str(raw_record.get("commit_basis") or ""))
    except ValueError:
        return _reject("MESSAGE_IDENTITY_CONTRACT_INVALID", "commit_basis_invalid", observation_id)

    reason = _validate_basis(
        basis=basis,
        observation=observation,
        proof=proof,
        stable_id=stable_id,
        observation_id=observation_id,
        sender_role=sender_role,
        message_type=message_type,
    )
    if reason:
        error_code = {
            "image": "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            "voice": "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        }.get(message_type, "MESSAGE_IDENTITY_CONTRACT_INVALID")
        return _reject(error_code, reason, observation_id)

    return CommittedMessage(
        conversation_id=clean_conversation_id,
        observation_id=observation_id,
        worker_stable_id=stable_id,
        sender_role=sender_role,
        message_type=message_type,
        commit_basis=basis,
        source_message_key=_source_message_key(clean_conversation_id, stable_id),
        proof=dict(proof),
    )


def require_committed_message(
    *,
    conversation_id: str,
    observation: Mapping[str, Any],
) -> CommittedMessage:
    result = commit_message_identity(
        conversation_id=conversation_id,
        observation=observation,
    )
    if isinstance(result, IdentityCommitRejection):
        raise ValueError(f"{result.error_code}:{result.reason}")
    return result


def _validate_basis(
    *,
    basis: MessageCommitBasis,
    observation: Mapping[str, Any],
    proof: Mapping[str, Any],
    stable_id: str,
    observation_id: str,
    sender_role: str,
    message_type: str,
) -> str:
    if basis is MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT:
        if proof.get("alignment_status") != "unique":
            return "historical_alignment_not_unique"
        if not str(proof.get("pre_observation_id") or "").strip():
            return "historical_pre_observation_missing"
        if str(proof.get("post_observation_id") or "").strip() != observation_id:
            return "historical_observation_mismatch"
        if str(proof.get("worker_stable_id") or "").strip() != stable_id:
            return "historical_stable_id_mismatch"
        match_basis = str(proof.get("match_basis") or "").strip()
        if not match_basis:
            return "historical_match_basis_missing"
        if message_type in {"voice", "image"} and match_basis not in {
            "native_source_message_id",
            "confirmed_action",
            "prior_confirmed_action",
            "two_sided_historical_context",
        }:
            return "historical_media_proof_insufficient"
        return ""

    if basis is MessageCommitBasis.NEW_SUFFIX:
        if message_type not in {"text", "system"}:
            return "new_suffix_media_forbidden"
        if proof.get("alignment_status") not in {"unique", "not_required"}:
            return "new_suffix_alignment_invalid"
        if proof.get("old_tail_fully_consumed") is not True:
            return "new_suffix_old_tail_not_consumed"
        if str(proof.get("new_suffix_observation_id") or "").strip() != observation_id:
            return "new_suffix_observation_mismatch"
        return ""

    if basis is MessageCommitBasis.CONFIRMED_VOICE_ACTION:
        if message_type != "voice":
            return "voice_action_type_mismatch"
        summary = observation.get("_worker_voice_action_summary")
        mapping = summary.get("confirmed_action_mapping") if isinstance(summary, Mapping) else None
        return _validate_action_mapping(
            mapping,
            proof,
            stable_id,
            observation_id,
            require_fingerprint=False,
        )

    if basis is MessageCommitBasis.CONFIRMED_IMAGE_ACTION:
        if message_type != "image":
            return "image_action_type_mismatch"
        summary = observation.get("_worker_image_action_summary")
        mapping = summary.get("confirmed_action_mapping") if isinstance(summary, Mapping) else None
        reason = _validate_action_mapping(
            mapping,
            proof,
            stable_id,
            observation_id,
            require_fingerprint=True,
        )
        if reason:
            return reason
        anchor = observation.get("image_physical_anchor")
        fingerprint = str(anchor.get("bubble_visual_fingerprint") or "").strip() if isinstance(anchor, Mapping) else ""
        if not fingerprint or str(summary.get("image_visual_fingerprint") or "").strip() != fingerprint:
            return "image_fingerprint_mismatch"
        if str(proof.get("image_visual_fingerprint") or "").strip() != fingerprint:
            return "image_commit_proof_fingerprint_mismatch"
        return ""

    if basis is MessageCommitBasis.CONFIRMED_SENT_ACK:
        if message_type != "text" or sender_role != "self":
            return "sent_ack_role_or_type_mismatch"
        receipt = observation.get("_worker_ai_reply_receipt")
        if not isinstance(receipt, Mapping):
            return "sent_ack_receipt_missing"
        if str(receipt.get("worker_stable_id") or "").strip() != stable_id:
            return "sent_ack_stable_id_mismatch"
        if not all(
            str(receipt.get(key) or "").strip()
            for key in ("reply_action_id", "reply_text_hash", "confirmed_at")
        ):
            return "sent_ack_receipt_incomplete"
        if str(proof.get("reply_action_id") or "").strip() != str(receipt.get("reply_action_id") or "").strip():
            return "sent_ack_action_mismatch"
        return ""

    if basis is MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID:
        native_id = str(observation.get("native_source_message_id") or "").strip()
        if not native_id or str(proof.get("native_source_message_id") or "").strip() != native_id:
            return "native_source_message_id_invalid"
        if str(proof.get("sender_role") or "").strip().lower() != sender_role:
            return "native_sender_role_mismatch"
        if str(proof.get("message_type") or "").strip().lower() != message_type:
            return "native_message_type_mismatch"
        return ""

    return "commit_basis_unsupported"


def _validate_action_mapping(
    mapping: Any,
    proof: Mapping[str, Any],
    stable_id: str,
    observation_id: str,
    *,
    require_fingerprint: bool,
) -> str:
    if not isinstance(mapping, Mapping):
        return "confirmed_action_mapping_missing"
    if not str(mapping.get("canonical_action_id") or "").strip():
        return "confirmed_action_id_missing"
    if str(mapping.get("reserved_worker_stable_id") or "").strip() != stable_id:
        return "confirmed_action_reserved_id_mismatch"
    if str(mapping.get("post_observation_id") or "").strip() != observation_id:
        return "confirmed_action_post_observation_mismatch"
    if not str(mapping.get("pre_observation_id") or "").strip():
        return "confirmed_action_pre_observation_missing"
    if mapping.get("binding_confirmed") is not True:
        return "confirmed_action_binding_not_confirmed"
    for field in (
        "canonical_action_id",
        "reserved_worker_stable_id",
        "pre_observation_id",
        "post_observation_id",
        "binding_confirmed",
    ):
        if proof.get(field) != mapping.get(field):
            return "confirmed_action_commit_proof_mismatch"
    return ""


def _source_message_key(conversation_id: str, stable_id: str) -> str:
    # Keep the pre-0.9.17 worker-sequence digest byte-for-byte stable.  The
    # lifecycle refactor changes who may request a durable key, never the key
    # of an already committed historical message.
    raw = json.dumps(
        {
            "conversation_id": conversation_id,
            "identity_kind": "worker_sequence",
            "identity": stable_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return ("source:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40])[:255]


def _reject(error_code: str, reason: str, observation_id: str) -> IdentityCommitRejection:
    return IdentityCommitRejection(
        error_code=error_code,
        reason=reason,
        observation_id=observation_id,
    )
