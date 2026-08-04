from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any


CONTRACT_FILENAME = "c2_contract_v3.json"


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
    values = c2_contract_v3().get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"Invalid C2 contract list: {key}")
    return frozenset(str(item) for item in values)


def contract_value_map(key: str) -> dict[str, frozenset[str]]:
    values = c2_contract_v3().get(key)
    if not isinstance(values, dict):
        raise RuntimeError(f"Invalid C2 contract map: {key}")
    result: dict[str, frozenset[str]] = {}
    for map_key, items in values.items():
        if not isinstance(items, list):
            raise RuntimeError(f"Invalid C2 contract map values: {key}.{map_key}")
        result[str(map_key)] = frozenset(str(item) for item in items)
    return result


def contract_revision() -> str:
    value = str(c2_contract_v3().get("contract_revision") or "").strip()
    if not value:
        raise RuntimeError("Invalid C2 contract revision")
    return value


def contract_sha256() -> str:
    canonical = json.dumps(
        c2_contract_v3(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def contract_row_rules() -> dict[str, dict[str, Any]]:
    values = c2_contract_v3().get("row_rules")
    if not isinstance(values, dict):
        raise RuntimeError("Invalid C2 contract row_rules")
    rules: dict[str, dict[str, Any]] = {}
    for row_kind, raw_rule in values.items():
        if not isinstance(raw_rule, dict):
            raise RuntimeError(f"Invalid C2 row rule: {row_kind}")
        rules[str(row_kind)] = dict(raw_rule)
    if set(rules) != set(contract_values("row_kinds")):
        raise RuntimeError("C2 row_rules and row_kinds are inconsistent")
    declared_ingestible = set(contract_values("ingestible_row_kinds"))
    derived_ingestible = {row_kind for row_kind, rule in rules.items() if bool(rule.get("ingestible"))}
    if declared_ingestible != derived_ingestible:
        raise RuntimeError("C2 ingestible_row_kinds and row_rules are inconsistent")
    return rules


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
    value = c2_contract_v3().get("image_contract")
    if not isinstance(value, dict):
        raise RuntimeError("Invalid C2 contract image_contract")
    return dict(value)


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
    schemas = image_contract().get("schemas")
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


def sidecar_contract_error(payload: dict[str, Any], *, require_observations: bool = True) -> str:
    if int(payload.get("observation_schema_version") or 0) != int(
        c2_contract_v3().get("observation_schema_version") or 0
    ):
        return "C2_OBSERVATION_SCHEMA_VERSION_REQUIRED"
    if require_observations and not isinstance(payload.get("observations"), list):
        return "C2_OBSERVATIONS_REQUIRED"
    if require_observations and payload.get("observation_validation_errors"):
        return "OMNIAUTO_OBSERVATION_CONTRACT_INVALID"
    return ""
