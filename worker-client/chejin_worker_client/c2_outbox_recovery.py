from __future__ import annotations

import copy
import json
from typing import Any

from .c2_contract import c2_contract_v3


PARTITION_GATE_CODE = "C2_INGEST_PARTITION_INCOMPLETE"


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
    *,
    include_complete_observations: bool = False,
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
    if not include_complete_observations:
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
        if encoded_payload_size(prepared) > target_bytes:
            raise ValueError("C2_INGEST_SINGLE_ITEM_TOO_LARGE")
        return [copy.deepcopy(prepared)]

    # The final request is the commit point for one authoritative frame.  It
    # must carry every observation, including already-confirmed history and
    # identity-unknown rows.  Reserve the smallest possible message chunk for
    # that request instead of silently shrinking the frame evidence again.
    if encoded_payload_size(
        _partition_payload(
            prepared,
            chunks[-1],
            include_complete_observations=True,
        )
    ) > target_bytes and len(chunks[-1]) > 1:
        preceding = chunks[-1][:-1]
        final_message = chunks[-1][-1:]
        chunks[-1] = preceding
        chunks.append(final_message)
    if encoded_payload_size(
        _partition_payload(
            prepared,
            chunks[-1],
            include_complete_observations=True,
        )
    ) > target_bytes:
        raise ValueError("C2_INGEST_SINGLE_ITEM_TOO_LARGE")

    source_keys = [
        str(item.get("source_message_key") or "").strip()
        for item in messages
        if str(item.get("source_message_key") or "").strip()
    ]
    read_run_id = str(prepared.get("read_run_id") or "").strip()
    result: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        part = _partition_payload(
            prepared,
            chunk,
            include_complete_observations=index == len(chunks),
        )
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
