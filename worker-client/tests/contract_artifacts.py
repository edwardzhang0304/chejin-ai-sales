from __future__ import annotations

from pathlib import Path


CLIENT_ROOT = Path(__file__).resolve().parents[1]


def resolve_contract_artifact(*parts: str) -> Path:
    candidates = (
        CLIENT_ROOT.parent / "contracts" / Path(*parts),
        CLIENT_ROOT / "contracts" / Path(*parts),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("/".join(parts))
