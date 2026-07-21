from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


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
