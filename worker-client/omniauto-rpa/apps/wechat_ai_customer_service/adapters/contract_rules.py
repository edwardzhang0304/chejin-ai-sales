"""Pure contract interpretation shared by backend and Worker; no I/O or state."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


IMAGE_FORBIDDEN_FIELD_PREFIXES = (
    "provider_response", "raw_provider_response", "retry_response", "initial_response",
)


def pre_send_reidentification_errors(payload: dict[str, Any]) -> frozenset[str]:
    section = payload.get("pre_send_message_viewport_contract")
    if not isinstance(section, dict):
        raise RuntimeError("Invalid C2 pre_send_message_viewport_contract")
    return contract_values(section, "pre_send_specific_errors") - {
        "C2_PRE_SEND_LAYOUT_INVALID", "C2_PRE_SEND_FACT_CHECKPOINT_INVALID",
    }


def contract_values(payload: dict[str, Any], key: str) -> frozenset[str]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"Invalid C2 contract list: {key}")
    return frozenset(str(item) for item in values)


def contract_value_map(payload: dict[str, Any], key: str) -> dict[str, frozenset[str]]:
    values = payload.get(key)
    if not isinstance(values, dict):
        raise RuntimeError(f"Invalid C2 contract map: {key}")
    result: dict[str, frozenset[str]] = {}
    for map_key, items in values.items():
        if not isinstance(items, list):
            raise RuntimeError(f"Invalid C2 contract map values: {key}.{map_key}")
        result[str(map_key)] = frozenset(str(item) for item in items)
    return result


def contract_revision(payload: dict[str, Any]) -> str:
    value = str(payload.get("contract_revision") or "").strip()
    if not value:
        raise RuntimeError("Invalid C2 contract revision")
    return value


def contract_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def contract_row_rules(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = payload.get("row_rules")
    if not isinstance(values, dict):
        raise RuntimeError("Invalid C2 contract row_rules")
    rules: dict[str, dict[str, Any]] = {}
    for row_kind, raw_rule in values.items():
        if not isinstance(raw_rule, dict):
            raise RuntimeError(f"Invalid C2 row rule: {row_kind}")
        rules[str(row_kind)] = dict(raw_rule)
    if set(rules) != set(contract_values(payload, "row_kinds")):
        raise RuntimeError("C2 row_rules and row_kinds are inconsistent")
    declared_ingestible = set(contract_values(payload, "ingestible_row_kinds"))
    derived_ingestible = {row_kind for row_kind, rule in rules.items() if bool(rule.get("ingestible"))}
    if declared_ingestible != derived_ingestible:
        raise RuntimeError("C2 ingestible_row_kinds and row_rules are inconsistent")
    return rules


def image_contract(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("image_contract")
    if not isinstance(value, dict):
        raise RuntimeError("Invalid C2 contract image_contract")
    return dict(value)


def validate_image_result_schema(
    payload: dict[str, Any],
    value: Any,
    schema_name: str,
) -> list[str]:
    schemas = image_contract(payload).get("schemas")
    schema = schemas.get(schema_name) if isinstance(schemas, dict) else None
    if not isinstance(schema, dict):
        raise RuntimeError(f"Invalid C2 image schema: {schema_name}")
    errors: list[str] = []

    def reject_non_finite(item: Any, path: str) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            errors.append(f"{path}: non-finite number")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                reject_non_finite(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                reject_non_finite(child, f"{path}[{index}]")

    reject_non_finite(value, "$")
    try:
        from jsonschema import Draft7Validator
    except ImportError as exc:
        raise RuntimeError("jsonschema dependency is required") from exc
    validator = Draft7Validator(schema)
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        errors.append(f"{path}: {error.message}")
    return errors


def recovery_action_for_error(
    payload: dict[str, Any], error_code: str, status_code: int,
    recovery_action: str | None = "",
) -> str:
    """Return the single contract-owned recovery action for an API error."""

    contract = payload.get("outbox_recovery_contract")
    if not isinstance(contract, dict):
        raise RuntimeError("Invalid C2 outbox_recovery_contract")
    explicit = str(recovery_action or "").strip()
    if explicit in {str(value) for value in (contract.get("actions") or [])}:
        return explicit
    code = str(error_code or "").strip()
    code_groups = (
        ("identity_quarantined_codes", "identity_quarantined"),
        ("refresh_and_rebuild_codes", "refresh_and_rebuild"),
        ("split_and_retry_codes", "split_and_retry"),
        ("target_terminated_codes", "target_terminated"),
        ("conversation_terminated_codes", "conversation_terminated"),
        ("capability_paused_codes", "capability_paused"),
    )
    for field, action in code_groups:
        if code in {
            str(value)
            for value in (contract.get(field) or [])
        }:
            return action
    if int(status_code) in {
        int(value) for value in (contract.get("retry_statuses") or [])
    } or int(status_code) >= 500:
        return "retry"
    return str(
        contract.get("unknown_api_error_action")
        or "capability_paused"
    )
