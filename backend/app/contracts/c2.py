from __future__ import annotations

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
