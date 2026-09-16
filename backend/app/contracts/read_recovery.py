"""Frozen, settlement-only compatibility for pre-upgrade C2 reads.

This is not general old-client admission. New flows carry their registered
contract; only an already registered legacy flow can consume the old contract.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from app.contracts.c2 import c2_contract_v3, contract_sha256
from app.contracts.shared_rules import shared_adapter

LEGACY_REVISION = "0.9.75"
LEGACY_SHA256 = "bcb1af09321339b159cc02581f5938e402f16094465933645c71bd7dc0eadcf1"


def compatible_read_contract(revision, sha256) -> dict | None:
    return shared_adapter("contract_rules").read_recovery_contract(c2_contract_v3(), revision, sha256)


def _load_frozen_contract(revision: str) -> dict:
    roots = (Path('/app/contracts'), Path(__file__).resolve().parents[3] / 'contracts')
    relative = f'recovery/c2_contract_v3_{revision}.json'
    path = next((r / relative for r in roots if (r / relative).is_file()), None)
    if path is None:
        raise RuntimeError('RECOVERY_CONTRACT_MISSING')
    return json.loads(path.read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def legacy_read_contract() -> dict:
    value = _load_frozen_contract(LEGACY_REVISION)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(encoded).hexdigest() != LEGACY_SHA256:
        raise RuntimeError('RECOVERY_CONTRACT_CORRUPTED')
    # Reuse the original validator only through the reviewed settlement migration.
    if compatible_read_contract(value['contract_revision'], LEGACY_SHA256) != value:
        raise RuntimeError('RECOVERY_CONTRACT_SEMANTICS_CHANGED')
    return value


def read_recovery_capability() -> dict:
    rules = shared_adapter('contract_rules')
    frozen = {revision: _load_frozen_contract(revision) for revision, _ in rules.RELEASED_READ_CONTRACTS}
    return rules.recovery_contract_capability(c2_contract_v3(), frozen)
