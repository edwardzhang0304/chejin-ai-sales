from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .shared_rules import contract_rules


CONTRACT_FILENAME = "c2_contract_v3.json"
IMAGE_FORBIDDEN_FIELD_PREFIXES = contract_rules.IMAGE_FORBIDDEN_FIELD_PREFIXES


def pre_send_reidentification_errors() -> frozenset[str]:
    return contract_rules.pre_send_reidentification_errors(c2_contract_v3())


def _contract_candidates() -> list[Path]:
    client_root = Path(__file__).resolve().parents[1]
    return [
        client_root / "contracts" / CONTRACT_FILENAME,
        client_root.parent / "contracts" / CONTRACT_FILENAME,
    ]


@lru_cache(maxsize=1)
def c2_contract_v3() -> dict[str, Any]:
    for path in _contract_candidates():
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("contract_version") or 0) != 3:
                raise RuntimeError(f"Invalid C2 contract version in {path}")
            return payload
    raise RuntimeError(f"Missing {CONTRACT_FILENAME}")


def contract_values(key: str) -> frozenset[str]:
    return contract_rules.contract_values(c2_contract_v3(), key)


def contract_value_map(key: str) -> dict[str, frozenset[str]]:
    return contract_rules.contract_value_map(c2_contract_v3(), key)


def contract_revision() -> str:
    return contract_rules.contract_revision(c2_contract_v3())


def contract_sha256() -> str:
    return contract_rules.contract_sha256(c2_contract_v3())


def target_location_recovery_contract() -> dict[str, Any]:
    value = c2_contract_v3().get("target_location_recovery_contract")
    if not isinstance(value, dict):
        raise RuntimeError("Invalid C2 target_location_recovery_contract")
    required_values = value.get("required_evidence_values")
    if not isinstance(required_values, dict):
        raise RuntimeError(
            "Invalid C2 target_location_recovery_contract evidence"
        )
    return dict(value)


def contract_row_rules() -> dict[str, dict[str, Any]]:
    return contract_rules.contract_row_rules(c2_contract_v3())


def validate_slot_ledger_states(
    states: Any,
    *,
    read_run_id: str,
) -> list[dict[str, Any]]:
    schema = c2_contract_v3().get("slot_ledger_state_schema")
    if not isinstance(schema, dict) or not isinstance(states, list):
        raise ValueError("C2_SLOT_LEDGER_STATE_SCHEMA_INVALID")
    required = {str(value) for value in schema.get("required_fields") or []}
    fact_scopes = {str(value) for value in schema.get("fact_scopes") or []}
    delivery_states = {
        str(value) for value in schema.get("delivery_states") or []
    }
    item_states = {str(value) for value in schema.get("item_states") or []}
    order_sources = {str(value) for value in schema.get("order_sources") or []}
    clean_read_run_id = str(read_run_id or "").strip()
    if not clean_read_run_id:
        raise ValueError("C2_READ_RUN_ID_MISSING")
    normalized: list[dict[str, Any]] = []
    source_keys: set[str] = set()
    screen_orders: set[int] = set()
    for raw in states:
        if not isinstance(raw, dict) or any(
            key not in raw or raw.get(key) in {None, ""}
            for key in required
        ):
            raise ValueError("C2_SLOT_LEDGER_REQUIRED_FIELD_MISSING")
        item = dict(raw)
        source_key = str(item.get("source_message_key") or "").strip()
        origin = str(item.get("origin_read_run_id") or "").strip()
        fact_scope = str(item.get("fact_scope") or "").strip()
        delivery_state = str(item.get("delivery_state") or "").strip()
        item_state = str(item.get("item_state") or "").strip()
        order_source = str(item.get("order_source") or "").strip()
        try:
            screen_order = int(item.get("screen_order") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("C2_SLOT_LEDGER_SCREEN_ORDER_INVALID") from exc
        if (
            not source_key
            or screen_order <= 0
            or fact_scope not in fact_scopes
            or delivery_state not in delivery_states
            or item_state not in item_states
            or order_source not in order_sources
        ):
            raise ValueError("C2_SLOT_LEDGER_STATE_INVALID")
        if fact_scope == "current_read_run" and origin != clean_read_run_id:
            raise ValueError("C2_SLOT_LEDGER_CURRENT_ORIGIN_MISMATCH")
        if fact_scope == "historical" and origin == clean_read_run_id:
            raise ValueError("C2_SLOT_LEDGER_HISTORICAL_ORIGIN_MISMATCH")
        if source_key in source_keys or screen_order in screen_orders:
            raise ValueError("C2_SLOT_LEDGER_IDENTITY_DUPLICATED")
        source_keys.add(source_key)
        screen_orders.add(screen_order)
        normalized.append(item)
    return normalized


def validate_sequence_alignment_evidence(
    value: Any,
    *,
    post_observation_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Validate the complete Worker-owned alignment envelope.

    The backend validates this same envelope with Pydantic.  Keeping this
    deep Worker-side gate prevents an Outbox from being frozen with a shape
    that can only fail after the HTTP request.  This is intentionally more
    than a top-level presence check: every pair, frame, index and suffix id is
    validated before the payload is allowed to reach durable transport.
    """

    contract = c2_contract_v3().get("sequence_alignment_contract")
    if not isinstance(contract, dict) or not isinstance(value, dict):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    required = {
        str(field)
        for field in (contract.get("required_evidence_fields") or [])
        if str(field).strip()
    }
    if not required or not required.issubset(value):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")

    source = str(value.get("pre_sequence_source") or "").strip()
    status = str(value.get("alignment_status") or "").strip()
    sources = {
        str(item)
        for item in (contract.get("pre_sequence_sources") or [])
    }
    statuses = {
        str(item)
        for item in (contract.get("alignment_statuses") or [])
    }
    identity_states = {
        str(item)
        for item in (contract.get("identity_states") or [])
    }
    pre_frame_id = str(value.get("pre_frame_id") or "").strip()
    post_frame_id = str(value.get("post_frame_id") or "").strip()
    candidate_count = value.get("candidate_alignment_count")
    matched_pairs = value.get("matched_pairs")
    suffix_ids = value.get("new_suffix_observation_ids")
    old_tail_fully_consumed = value.get("old_tail_fully_consumed")
    if (
        source not in sources
        or status not in statuses
        or not pre_frame_id
        or not post_frame_id
        or len(pre_frame_id) > 255
        or len(post_frame_id) > 255
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < 0
        or not isinstance(matched_pairs, list)
        or len(matched_pairs) > 500
        or not isinstance(suffix_ids, list)
        or len(suffix_ids) > 500
        or not isinstance(old_tail_fully_consumed, bool)
    ):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")

    normalized_suffix_ids = [str(item or "").strip() for item in suffix_ids]
    if (
        any(not item for item in normalized_suffix_ids)
        or len(normalized_suffix_ids) != len(set(normalized_suffix_ids))
    ):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")

    required_pair_fields = {
        str(field)
        for field in (contract.get("required_pair_fields") or [])
        if str(field).strip()
    }
    if not required_pair_fields:
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    seen_pre_ids: set[str] = set()
    seen_post_ids: set[str] = set()
    seen_pre_indexes: set[int] = set()
    seen_post_indexes: set[int] = set()
    normalized_pairs: list[dict[str, Any]] = []
    previous_pre_index = -1
    previous_post_index = -1
    for raw_pair in matched_pairs:
        if (
            not isinstance(raw_pair, dict)
            or not required_pair_fields.issubset(raw_pair)
        ):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_PAIR_INVALID")
        pair = dict(raw_pair)
        identity_state = str(pair.get("identity_state") or "").strip()
        pre_observation_id = str(
            pair.get("pre_observation_id") or ""
        ).strip()
        post_observation_id = str(
            pair.get("post_observation_id") or ""
        ).strip()
        match_basis = str(pair.get("match_basis") or "").strip()
        pre_index = pair.get("pre_index")
        post_index = pair.get("post_index")
        stable_id = str(pair.get("worker_stable_id") or "").strip()
        if (
            identity_state not in identity_states
            or not pre_observation_id
            or not post_observation_id
            or not match_basis
            or len(pre_observation_id) > 255
            or len(post_observation_id) > 255
            or len(match_basis) > 64
            or isinstance(pre_index, bool)
            or not isinstance(pre_index, int)
            or pre_index < 0
            or isinstance(post_index, bool)
            or not isinstance(post_index, int)
            or post_index < 0
        ):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_PAIR_INVALID")
        if identity_state in {"committed", "selected_action"} and not (
            len(stable_id) <= 128
            and re.fullmatch(r"worker-message-[1-9]\d*", stable_id)
        ):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_PAIR_INVALID")
        if identity_state == "frame_local_unselected" and stable_id:
            raise ValueError("C2_SEQUENCE_ALIGNMENT_PAIR_INVALID")
        if (
            pre_observation_id in seen_pre_ids
            or post_observation_id in seen_post_ids
            or pre_index in seen_pre_indexes
            or post_index in seen_post_indexes
            or pre_index <= previous_pre_index
            or post_index <= previous_post_index
        ):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_PAIR_INVALID")
        seen_pre_ids.add(pre_observation_id)
        seen_post_ids.add(post_observation_id)
        seen_pre_indexes.add(pre_index)
        seen_post_indexes.add(post_index)
        previous_pre_index = pre_index
        previous_post_index = post_index
        normalized_pairs.append(pair)

    if status == "not_required" and (
        candidate_count != 0 or normalized_pairs
    ):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    if status == "unique" and candidate_count != 1:
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    if status in {"ambiguous", "unresolved"} and (
        old_tail_fully_consumed or normalized_suffix_ids
    ):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    if normalized_suffix_ids and not old_tail_fully_consumed:
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")
    if set(normalized_suffix_ids).intersection(seen_post_ids):
        raise ValueError("C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID")

    if post_observation_ids is not None:
        normalized_post_ids = [
            str(item or "").strip() for item in post_observation_ids
        ]
        if (
            any(not item for item in normalized_post_ids)
            or len(normalized_post_ids) != len(set(normalized_post_ids))
        ):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_POST_FRAME_INVALID")
        for pair in normalized_pairs:
            post_index = int(pair["post_index"])
            if (
                post_index >= len(normalized_post_ids)
                or normalized_post_ids[post_index]
                != pair["post_observation_id"]
            ):
                raise ValueError("C2_SEQUENCE_ALIGNMENT_POST_FRAME_INVALID")
        # Every claimed alignment target must belong to this authoritative
        # frame.  The reverse is intentionally not required: an invalid or
        # unselected frame-local media row may be present so the normal
        # identity/action gate can reject it without pretending it was part
        # of the successful alignment.
        claimed_post_ids = {
            str(pair["post_observation_id"])
            for pair in normalized_pairs
        }.union(normalized_suffix_ids)
        if not claimed_post_ids.issubset(set(normalized_post_ids)):
            raise ValueError("C2_SEQUENCE_ALIGNMENT_POST_FRAME_INVALID")

    return {
        **value,
        "pre_sequence_source": source,
        "pre_frame_id": pre_frame_id,
        "post_frame_id": post_frame_id,
        "alignment_status": status,
        "candidate_alignment_count": candidate_count,
        "matched_pairs": normalized_pairs,
        "old_tail_fully_consumed": old_tail_fully_consumed,
        "new_suffix_observation_ids": normalized_suffix_ids,
    }


def observation_role_is_trusted(observation: dict[str, Any]) -> bool:
    """Validate one observation's final role against the shared C2 contract."""

    row_kind = str(observation.get("row_kind") or "").strip().lower()
    rule = contract_row_rules().get(row_kind)
    if not isinstance(rule, dict):
        return False
    role = str(observation.get("sender_role") or "").strip().lower()
    role_source = str(
        observation.get("sender_role_source") or ""
    ).strip().lower()
    return role in {
        str(value).strip().lower()
        for value in (rule.get("allowed_sender_roles") or [])
    } and role_source in {
        str(value).strip().lower()
        for value in (rule.get("allowed_sender_role_sources") or [])
    }


def temporary_capability_gate_codes() -> frozenset[str]:
    return contract_values("temporary_capability_gate_codes")


def image_contract() -> dict[str, Any]:
    return contract_rules.image_contract(c2_contract_v3())


def formal_image_failure_code(reason: Any) -> str:
    clean = str(reason or "").strip()
    contract = image_contract()
    declared = {
        str(value)
        for value in (contract.get("error_codes") or [])
    }
    reason_map = contract.get("failure_reason_to_error_code")
    if not isinstance(reason_map, dict):
        raise RuntimeError("Invalid C2 image failure reason map")
    code = str(
        reason_map.get(clean)
        or contract.get("default_failure_error_code")
        or ""
    ).strip()
    if code not in declared:
        raise RuntimeError(f"Undeclared C2 image error code: {code}")
    return code


def validate_image_result_schema(
    value: Any,
    schema_name: str,
) -> list[str]:
    return contract_rules.validate_image_result_schema(c2_contract_v3(), value, schema_name)


def sidecar_contract_error(payload: dict[str, Any], *, require_observations: bool = True) -> str:
    if int(payload.get("observation_schema_version") or 0) != int(
        c2_contract_v3().get("observation_schema_version") or 0
    ):
        return "C2_OBSERVATION_SCHEMA_VERSION_REQUIRED"
    if require_observations and not isinstance(payload.get("observations"), list):
        return "C2_OBSERVATIONS_REQUIRED"
    if require_observations and payload.get("observation_validation_errors"):
        return "OMNIAUTO_OBSERVATION_CONTRACT_INVALID"
    frame_binding = c2_contract_v3().get(
        "frame_action_binding_contract"
    )
    forbidden_identity_fields = (
        frame_binding.get("sidecar_must_not_return")
        if isinstance(frame_binding, dict)
        else None
    )
    if not isinstance(forbidden_identity_fields, list):
        raise RuntimeError(
            "Invalid C2 frame_action_binding_contract.sidecar_must_not_return"
        )
    forbidden = {
        str(field).strip()
        for field in forbidden_identity_fields
        if str(field).strip()
    }

    def contains_forbidden_identity(value: Any) -> bool:
        if isinstance(value, dict):
            if forbidden.intersection(str(key) for key in value):
                return True
            return any(
                contains_forbidden_identity(child)
                for child in value.values()
            )
        if isinstance(value, list):
            return any(
                contains_forbidden_identity(child) for child in value
            )
        return False

    if contains_forbidden_identity(payload):
        return "C2_SIDECAR_IDENTITY_CONTRACT_INVALID"

    observations = payload.get("observations")
    if isinstance(observations, list):
        seen_observation_ids: set[str] = set()
        stable_voice_anchor_owners: dict[str, str] = {}
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_id = str(
                observation.get("observation_id") or ""
            ).strip()
            if observation_id:
                if observation_id in seen_observation_ids:
                    return "C2_SIDECAR_IDENTITY_CONTRACT_INVALID"
                seen_observation_ids.add(observation_id)
            source = (
                observation.get("source_message")
                if isinstance(observation.get("source_message"), dict)
                else {}
            )
            # Sidecar is the sole same-frame voice merger.  Worker does not
            # compare structural aliases or geometry and never tries to pick
            # a winner.  It only rejects an explicit stable anchor that the
            # Sidecar assigned to two different observations, which means the
            # Sidecar contract has not converged to one observation per row.
            stable_voice_anchors = {
                str(value).strip()
                for value in (
                    observation.get("voice_anchor_stable_key"),
                    source.get("voice_anchor_stable_key"),
                )
                if str(value or "").strip()
            }
            for anchor in stable_voice_anchors:
                owner = stable_voice_anchor_owners.get(anchor)
                if owner is not None and owner != observation_id:
                    return "C2_SIDECAR_IDENTITY_CONTRACT_INVALID"
                stable_voice_anchor_owners[anchor] = observation_id
    return ""
