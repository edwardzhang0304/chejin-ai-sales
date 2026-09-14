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


@lru_cache(maxsize=1)
def legacy_read_contract() -> dict:
    roots = (Path('/app/contracts'), Path(__file__).resolve().parents[3] / 'contracts')
    path = next((r / 'recovery/c2_contract_v3_0.9.75.json' for r in roots
                 if (r / 'recovery/c2_contract_v3_0.9.75.json').is_file()), None)
    if path is None:
        raise RuntimeError('RECOVERY_CONTRACT_MISSING')
    value = json.loads(path.read_text(encoding='utf-8'))
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(encoded).hexdigest() != LEGACY_SHA256:
        raise RuntimeError('RECOVERY_CONTRACT_CORRUPTED')
    # Reuse the structural validator only while every business rule is equal.
    # A later semantic change requires a separately reviewed migration/validator.
    if compatible_read_contract(value['contract_revision'], LEGACY_SHA256) != value:
        raise RuntimeError('RECOVERY_CONTRACT_SEMANTICS_CHANGED')
    return value


def read_recovery_capability() -> dict:
    legacy = legacy_read_contract()
    current = c2_contract_v3()
    return {'protocol_version': 1,
        'compatible_rules_sha256': shared_adapter("contract_rules").contract_rules_sha256(current),
        'contracts': [
        {'revision': legacy['contract_revision'], 'sha256': LEGACY_SHA256},
        {'revision': '0.9.78', 'sha256': 'b4151ab61fb5d90688e1e0ac187cc767acaee1617cb420be3028acc52ccf7eab'},
        {'revision': '0.9.80', 'sha256': '43f8c07e3660d790c39f3b348dcce9fb1e2c0bed243b41cff6669a658995e380'},
        {'revision': current['contract_revision'], 'sha256': contract_sha256()},
    ]}
