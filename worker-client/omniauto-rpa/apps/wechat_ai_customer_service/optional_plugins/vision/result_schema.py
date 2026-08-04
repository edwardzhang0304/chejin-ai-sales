from __future__ import annotations

import math
from typing import Any


def project_to_schema(value: Any, schema: dict[str, Any]) -> Any:
    """Drop undeclared fields using the contract schema as the only allowlist."""

    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        return {
            key: project_to_schema(value[key], child_schema)
            for key, child_schema in properties.items()
            if key in value and isinstance(child_schema, dict)
        }
    if schema_type == "array" and isinstance(value, list):
        child_schema = (
            schema.get("items")
            if isinstance(schema.get("items"), dict)
            else {}
        )
        return [
            project_to_schema(item, child_schema)
            for item in value
        ]
    return value


def validate_schema(value: Any, schema: dict[str, Any]) -> list[str]:
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
    for error in sorted(
        Draft7Validator(schema).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        errors.append(f"{path}: {error.message}")
    return errors


def image_result_schema(
    config: dict[str, Any] | None,
    schema_name: str,
) -> dict[str, Any]:
    contract = (
        config.get("image_contract")
        if isinstance(config, dict)
        and isinstance(config.get("image_contract"), dict)
        else {}
    )
    schemas = (
        contract.get("schemas")
        if isinstance(contract.get("schemas"), dict)
        else {}
    )
    schema = schemas.get(schema_name)
    return dict(schema) if isinstance(schema, dict) else {}


def image_understanding_completed(
    value: Any,
    schema: dict[str, Any] | None = None,
) -> bool:
    """Return the sole business completion verdict for a Vision result."""

    if not isinstance(value, dict):
        return False
    if value.get("applied") is not True:
        return False
    if not str(value.get("vision_summary") or "").strip():
        return False
    return not schema or not validate_schema(value, schema)
