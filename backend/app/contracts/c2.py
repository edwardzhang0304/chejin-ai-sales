from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.contracts.shared_rules import shared_adapter


CONTRACT_FILENAME = "c2_contract_v3.json"


@lru_cache(maxsize=1)
def c2_contract_v3() -> dict[str, Any]:
    candidates = [
        Path("/app/contracts") / CONTRACT_FILENAME,
        Path(__file__).resolve().parents[3] / "contracts" / CONTRACT_FILENAME,
    ]
    for path in candidates:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("contract_version") or 0) != 3:
                raise RuntimeError(f"Invalid C2 contract version in {path}")
            return payload
    raise RuntimeError(f"Missing {CONTRACT_FILENAME}")


def contract_values(key: str) -> frozenset[str]:
    return shared_adapter("contract_rules").contract_values(c2_contract_v3(), key)


def contract_value_map(key: str) -> dict[str, frozenset[str]]:
    return shared_adapter("contract_rules").contract_value_map(c2_contract_v3(), key)


def contract_revision() -> str:
    return shared_adapter("contract_rules").contract_revision(c2_contract_v3())


def contract_sha256() -> str:
    return shared_adapter("contract_rules").contract_sha256(c2_contract_v3())


def contract_row_rules() -> dict[str, dict[str, Any]]:
    return shared_adapter("contract_rules").contract_row_rules(c2_contract_v3())


def image_contract() -> dict[str, Any]:
    return shared_adapter("contract_rules").image_contract(c2_contract_v3())


def validate_image_result_schema(
    value: Any,
    schema_name: str,
) -> list[str]:
    return shared_adapter("contract_rules").validate_image_result_schema(c2_contract_v3(), value, schema_name)


def recovery_action_for_error(error_code: str, status_code: int) -> str:
    return shared_adapter("contract_rules").recovery_action_for_error(
        c2_contract_v3(), error_code, status_code
    )
