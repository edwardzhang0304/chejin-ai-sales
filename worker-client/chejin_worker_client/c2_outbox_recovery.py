from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .c2_contract import c2_contract_v3


PARTITION_GATE_CODE = "C2_INGEST_PARTITION_INCOMPLETE"


def _stable_digest(payload: Any, *, length: int) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def _replace_identity_value(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_identity_value(child, replacements)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_identity_value(child, replacements)
            for child in value
        ]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def rebuild_identity_collision(
    payload: dict[str, Any],
    *,
    source_message_key: str,
    dedupe_key: str,
    next_sequence_floor: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Rekey one persisted Worker-sequence fact without repeating UI work."""

    rebuilt = copy.deepcopy(payload)
    messages = [
        item
        for item in (rebuilt.get("messages") or [])
        if isinstance(item, dict)
    ]
    matching = [
        item
        for item in messages
        if (
            source_message_key
            and str(item.get("source_message_key") or "")
            == source_message_key
        )
        or (
            dedupe_key
            and str(item.get("dedupe_key") or "") == dedupe_key
        )
    ]
    if len(matching) != 1:
        raise ValueError("MESSAGE_IDENTITY_COLLISION_ITEM_AMBIGUOUS")
    item = matching[0]
    raw_payload = item.get("raw_payload")
    raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
    basis = raw_payload.get("dedupe_basis")
    basis = basis if isinstance(basis, dict) else {}
    old_stable_id = str(basis.get("worker_stable_id") or "").strip()
    if (
        str(basis.get("source") or "") != "worker_cross_round_sequence"
        or not re.fullmatch(r"worker-message-\d+", old_stable_id)
    ):
        raise ValueError("MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE")
    same_stable_id_items = []
    for message in messages:
        candidate_raw = message.get("raw_payload")
        candidate_raw = candidate_raw if isinstance(candidate_raw, dict) else {}
        candidate_basis = candidate_raw.get("dedupe_basis")
        candidate_basis = (
            candidate_basis if isinstance(candidate_basis, dict) else {}
        )
        if (
            str(candidate_basis.get("worker_stable_id") or "").strip()
            == old_stable_id
        ):
            same_stable_id_items.append(message)
    if same_stable_id_items != [item]:
        raise ValueError("MESSAGE_IDENTITY_COLLISION_ITEM_AMBIGUOUS")
    used_sequences = []
    for message in messages:
        candidate_raw = message.get("raw_payload")
        candidate_raw = candidate_raw if isinstance(candidate_raw, dict) else {}
        candidate_basis = candidate_raw.get("dedupe_basis")
        candidate_basis = (
            candidate_basis if isinstance(candidate_basis, dict) else {}
        )
        match = re.fullmatch(
            r"worker-message-(\d+)",
            str(candidate_basis.get("worker_stable_id") or "").strip(),
        )
        if match:
            used_sequences.append(int(match.group(1)))
    sequence = max(
        1,
        int(next_sequence_floor),
        max(used_sequences, default=0) + 1,
    )
    new_stable_id = f"worker-message-{sequence}"
    conversation_id = str(rebuilt.get("conversation_id") or "").strip()
    if not conversation_id:
        raise ValueError("MESSAGE_CONVERSATION_ID_MISSING")
    new_dedupe_key = (
        f"{conversation_id}:"
        + _stable_digest(
            {
                "conversation_id": conversation_id,
                "worker_stable_id": new_stable_id,
            },
            length=32,
        )
    )[:255]
    identity_kind = (
        "worker_sequence"
        if str(item.get("message_type") or "") == "image"
        else "worker_dedupe_key"
    )
    identity = (
        new_stable_id
        if identity_kind == "worker_sequence"
        else new_dedupe_key
    )
    new_source_message_key = (
        "source:"
        + _stable_digest(
            {
                "conversation_id": conversation_id,
                "identity_kind": identity_kind,
                "identity": identity,
            },
            length=40,
        )
    )[:255]
    old_source_message_key = str(item.get("source_message_key") or "")
    old_dedupe_key = str(item.get("dedupe_key") or "")
    rebuilt = _replace_identity_value(
        rebuilt,
        {
            old_source_message_key: new_source_message_key,
            old_dedupe_key: new_dedupe_key,
            old_stable_id: new_stable_id,
        },
    )
    return rebuilt, {
        "old_source_message_key": old_source_message_key,
        "new_source_message_key": new_source_message_key,
        "old_dedupe_key": old_dedupe_key,
        "new_dedupe_key": new_dedupe_key,
        "old_stable_id": old_stable_id,
        "new_stable_id": new_stable_id,
    }


def encoded_payload_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _message_limits() -> dict[str, Any]:
    limits = c2_contract_v3().get("message_limits")
    if not isinstance(limits, dict):
        raise RuntimeError("Invalid C2 message_limits contract")
    return limits


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _allowlisted_dict(
    value: Any,
    *,
    allowed_fields: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): copy.deepcopy(child)
        for key, child in value.items()
        if str(key) in allowed_fields
    }


def _compact_source_message(
    value: Any,
    *,
    limits: dict[str, Any],
) -> dict[str, Any]:
    return _allowlisted_dict(
        value,
        allowed_fields={
            str(field)
            for field in (
                limits.get("source_message_transport_fields") or []
            )
        },
    )


def _compact_observation(
    value: Any,
    *,
    limits: dict[str, Any],
) -> dict[str, Any]:
    compacted = _allowlisted_dict(
        value,
        allowed_fields={
            str(field)
            for field in (
                limits.get("observation_transport_fields") or []
            )
        },
    )
    if "source_message" in compacted:
        compacted["source_message"] = _compact_source_message(
            compacted["source_message"],
            limits=limits,
        )
    return compacted


def _compact_raw_payload(
    value: Any,
    *,
    limits: dict[str, Any],
    compacted_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compacted = _allowlisted_dict(
        value,
        allowed_fields={
            str(field)
            for field in (
                limits.get("raw_payload_transport_fields") or []
            )
        },
    )
    observation = (
        compacted_observation
        if isinstance(compacted_observation, dict)
        else _compact_observation(
            compacted.get("observation"),
            limits=limits,
        )
    )
    compacted["observation"] = copy.deepcopy(observation)
    return compacted


def _compact_transport_payload(
    payload: dict[str, Any],
    *,
    compact_all_observations: bool,
) -> dict[str, Any]:
    limits = _message_limits()
    raw_limit = int(limits.get("raw_payload_max_bytes") or 0)
    if raw_limit <= 0:
        raise RuntimeError("Invalid C2 raw_payload limit")
    if not compact_all_observations:
        has_oversized_raw_payload = any(
            isinstance(message, dict)
            and _json_size(message.get("raw_payload") or {}) > raw_limit
            for message in (payload.get("messages") or [])
        )
        if not has_oversized_raw_payload:
            return copy.deepcopy(payload)
    result = copy.deepcopy(payload)
    evidence = (
        result.get("evidence")
        if isinstance(result.get("evidence"), dict)
        else {}
    )
    compacted_observations: dict[str, dict[str, Any]] = {}
    evidence_items: list[dict[str, Any]] = []
    for item in evidence.get("observations") or []:
        if not isinstance(item, dict):
            continue
        should_compact = compact_all_observations
        compacted = (
            _compact_observation(item, limits=limits)
            if should_compact
            else copy.deepcopy(item)
        )
        observation_id = str(compacted.get("observation_id") or "").strip()
        if observation_id:
            compacted_observations[observation_id] = compacted
        evidence_items.append(compacted)
    evidence["observations"] = evidence_items
    result["evidence"] = evidence

    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        raw_payload = (
            message.get("raw_payload")
            if isinstance(message.get("raw_payload"), dict)
            else {}
        )
        observation_id = _message_observation_id(message)
        should_compact = (
            compact_all_observations
            or _json_size(raw_payload) > raw_limit
        )
        if should_compact:
            compacted_observation = _compact_observation(
                compacted_observations.get(observation_id)
                or raw_payload.get("observation"),
                limits=limits,
            )
            if observation_id:
                compacted_observations[
                    observation_id
                ] = compacted_observation
            raw_payload = _compact_raw_payload(
                raw_payload,
                limits=limits,
                compacted_observation=compacted_observation,
            )
            message["raw_payload"] = raw_payload
        if _json_size(raw_payload) > raw_limit:
            raise ValueError("C2_MESSAGE_RAW_PAYLOAD_TOO_LARGE")

    evidence["observations"] = [
        copy.deepcopy(
            compacted_observations.get(
                str(item.get("observation_id") or "").strip(),
                item,
            )
        )
        for item in evidence_items
    ]
    return result


def _message_observation_id(message: dict[str, Any]) -> str:
    raw_payload = (
        message.get("raw_payload")
        if isinstance(message.get("raw_payload"), dict)
        else {}
    )
    observation = (
        raw_payload.get("observation")
        if isinstance(raw_payload.get("observation"), dict)
        else {}
    )
    return str(observation.get("observation_id") or "").strip()


def _partition_payload(
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["messages"] = copy.deepcopy(messages)
    evidence = (
        result.get("evidence")
        if isinstance(result.get("evidence"), dict)
        else {}
    )
    observation_ids = {
        _message_observation_id(message)
        for message in messages
        if _message_observation_id(message)
    }
    evidence["observations"] = [
        item
        for item in (evidence.get("observations") or [])
        if isinstance(item, dict)
        and str(item.get("observation_id") or "").strip()
        in observation_ids
    ]
    result["evidence"] = evidence
    return result


def split_ingest_payload(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compact and split one persisted atomic batch without partial Brain execution."""

    limits = _message_limits()
    target_bytes = int(limits.get("split_target_bytes") or 0)
    if target_bytes <= 0:
        raise RuntimeError("Invalid C2 split target")
    prepared = _compact_transport_payload(
        payload,
        compact_all_observations=False,
    )
    if encoded_payload_size(prepared) > target_bytes:
        prepared = _compact_transport_payload(
            prepared,
            compact_all_observations=True,
        )
    messages = [
        dict(item)
        for item in (prepared.get("messages") or [])
        if isinstance(item, dict)
    ]
    if not messages or encoded_payload_size(prepared) <= target_bytes:
        return [copy.deepcopy(prepared)]

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        candidate = [*current, message]
        candidate_payload = _partition_payload(prepared, candidate)
        if current and encoded_payload_size(candidate_payload) > target_bytes:
            chunks.append(current)
            current = [message]
        else:
            current = candidate
        if encoded_payload_size(_partition_payload(prepared, current)) > target_bytes:
            raise ValueError("C2_INGEST_SINGLE_ITEM_TOO_LARGE")
    if current:
        chunks.append(current)
    if len(chunks) <= 1:
        return [copy.deepcopy(prepared)]

    source_keys = [
        str(item.get("source_message_key") or "").strip()
        for item in messages
        if str(item.get("source_message_key") or "").strip()
    ]
    read_run_id = str(prepared.get("read_run_id") or "").strip()
    result: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        part = _partition_payload(prepared, chunk)
        evidence = part["evidence"]
        evidence["ingest_partition"] = {
            "group_id": read_run_id,
            "index": index,
            "count": len(chunks),
            "expected_source_message_keys": source_keys,
        }
        if index < len(chunks):
            evidence["flow_gate_errors"] = [PARTITION_GATE_CODE]
            evidence["flow_gate_details"] = [
                {
                    "error_code": PARTITION_GATE_CODE,
                    "position_source": "position_unavailable",
                }
            ]
        result.append(part)
    return result


def rebuild_invalid_media_as_failed(
    payload: dict[str, Any],
    *,
    error_code: str,
    source_message_key: str,
) -> dict[str, Any]:
    """Turn a rejected media item into the contract's explicit failed fact."""

    rebuilt = copy.deepcopy(payload)
    evidence = (
        rebuilt.get("evidence")
        if isinstance(rebuilt.get("evidence"), dict)
        else {}
    )
    observations = {
        str(item.get("observation_id") or "").strip(): item
        for item in (evidence.get("observations") or [])
        if isinstance(item, dict)
        and str(item.get("observation_id") or "").strip()
    }
    changed: list[dict[str, Any]] = []
    rejected_source_key = str(source_message_key or "").strip()
    if not rejected_source_key:
        raise ValueError("C2_OUTBOX_MEDIA_REBUILD_SOURCE_MISSING")
    for message in rebuilt.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if (
            str(message.get("source_message_key") or "").strip()
            != rejected_source_key
        ):
            continue
        message_type = str(message.get("message_type") or "").strip().lower()
        item_state = str(message.get("item_state") or "").strip().lower()
        should_rebuild = (
            error_code.startswith("VOICE_") and message_type == "voice"
        ) or (
            error_code.startswith("IMAGE_") and message_type == "image"
        )
        if not should_rebuild:
            continue
        if (
            error_code == "VOICE_FAILURE_REASON_MISSING"
            and item_state != "failed"
        ):
            continue
        if (
            error_code == "IMAGE_FAILURE_REASON_MISSING"
            and item_state != "failed"
        ):
            continue
        raw_payload = (
            message.get("raw_payload")
            if isinstance(message.get("raw_payload"), dict)
            else {}
        )
        observation = (
            raw_payload.get("observation")
            if isinstance(raw_payload.get("observation"), dict)
            else {}
        )
        observation_id = str(
            observation.get("observation_id") or ""
        ).strip()
        reason = str(error_code)
        message["item_state"] = "failed"
        message["flow_state"] = "failed"
        message["content"] = None
        reason_detail = (
            "Worker preserved the original media observation after the "
            f"backend rejected it with {reason}."
        )
        raw_payload["error_code"] = reason
        raw_payload["reason_detail"] = reason_detail
        observation["error_code"] = reason
        observation["reason_detail"] = reason_detail
        observation["item_state"] = "failed"
        observation["content_clean"] = None
        if message_type == "image":
            raw_payload.pop("customer_image_understanding", None)
            raw_payload.pop("visual_bridge_input", None)
            observation.pop("customer_image_understanding", None)
            observation.pop("visual_bridge_input", None)
        raw_payload["observation"] = observation
        message["raw_payload"] = raw_payload
        if observation_id in observations:
            evidence_observation = observations[observation_id]
            if evidence_observation is not observation:
                replacement = copy.deepcopy(observation)
                evidence_observation.clear()
                evidence_observation.update(replacement)
        changed.append(message)
    if not changed:
        raise ValueError("C2_OUTBOX_MEDIA_REBUILD_TARGET_MISSING")

    changed_source_keys = {
        str(item.get("source_message_key") or "").strip()
        for item in changed
        if str(item.get("source_message_key") or "").strip()
    }
    for slot in evidence.get("slot_ledger_states") or []:
        if (
            isinstance(slot, dict)
            and str(slot.get("source_message_key") or "").strip()
            in changed_source_keys
        ):
            slot["item_state"] = "failed"
            slot["delivery_state"] = "outbox_waiting"

    gate_code = (
        "C2_VOICE_TRANSCRIBE_FAILED"
        if str(error_code).startswith("VOICE_")
        else "C2_IMAGE_UNDERSTANDING_FAILED"
    )
    flow_gate_errors = [
        str(value)
        for value in (evidence.get("flow_gate_errors") or [])
        if str(value)
    ]
    if gate_code not in flow_gate_errors:
        flow_gate_errors.append(gate_code)
    evidence["flow_gate_errors"] = flow_gate_errors
    orders = [
        int(item["message_position"]["screen_order"])
        for item in changed
        if isinstance(item.get("message_position"), dict)
        and item["message_position"].get("order_source") == "visual_top"
        and item["message_position"].get("screen_order")
    ]
    detail = {
        "error_code": gate_code,
        "position_source": "position_unavailable",
    }
    if orders:
        detail.update(
            {
                "min_screen_order": min(orders),
                "max_screen_order": max(orders),
                "position_source": (
                    "failed_voice_visual_top"
                    if gate_code == "C2_VOICE_TRANSCRIBE_FAILED"
                    else "failed_image_visual_top"
                ),
            }
        )
    details = [
        item
        for item in (evidence.get("flow_gate_details") or [])
        if isinstance(item, dict)
        and str(item.get("error_code") or "") != gate_code
    ]
    details.append(detail)
    evidence["flow_gate_details"] = details
    rebuilt["evidence"] = evidence
    return rebuilt
