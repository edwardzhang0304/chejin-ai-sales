"""Frozen released contracts, not current contracts with an old version label."""
import copy
import json
from pathlib import Path

import pytest

from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.pending_read_recovery import accepts_handoff, package_recovery_capability
from chejin_worker_client.shared_rules import contract_rules as rules

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ('0.9.75', '0.9.78', '0.9.80', '0.9.85')


def frozen_contracts():
    return {v: json.loads((ROOT / f'contracts/recovery/c2_contract_v3_{v}.json').read_text()) for v in VERSIONS}


@pytest.mark.parametrize('revision', VERSIONS)
def test_actual_released_read_migrates_without_claiming_full_equivalence(revision):
    historical = frozen_contracts()[revision]
    sha = rules.contract_sha256(historical)
    current = c2_contract_v3()
    before = copy.deepcopy(current)
    assert rules.equivalent_contract(current, revision, sha) is None
    assert rules.read_recovery_contract(current, revision, sha) == historical
    pair = {'revision': revision, 'sha256': sha}
    capability = package_recovery_capability()
    assert pair in capability['contracts']
    assert accepts_handoff(capability, {'protocol_version': 1, 'contracts': [pair]})
    assert current == before


@pytest.mark.parametrize('change', ['identity', 'sequence_limit', 'sequence_unknown',
    'sequence_missing', 'storage', 'terminal', 'unknown'])
def test_future_rule_changes_cannot_use_the_reviewed_migration(change):
    current = copy.deepcopy(c2_contract_v3())
    if change == 'identity': current['message_identity_contract']['duplicate_identity_invariants'].remove('sender_role')
    elif change == 'sequence_limit': current['c3_reply_sequence_contract']['max_segments'] = 4
    elif change == 'sequence_unknown': current['c3_reply_sequence_contract']['unreviewed_rule'] = True
    elif change == 'sequence_missing': del current['c3_reply_sequence_contract']
    elif change == 'storage': current['pre_send_fact_checkpoint_contract']['storage'] = 'unreviewed_storage'
    elif change == 'terminal': current['terminal_read_settlement_contract']['unreviewed_rule'] = True
    else: current['unreviewed_rule'] = True
    for revision, historical in frozen_contracts().items():
        assert rules.read_recovery_contract(current, revision, rules.contract_sha256(historical)) is None
    with pytest.raises(RuntimeError, match='RECOVERY_CONTRACT_SEMANTICS_CHANGED'):
        rules.recovery_contract_capability(current, frozen_contracts())


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'development_085'])
def test_published_085_resource_must_be_the_actual_release(damage):
    frozen = frozen_contracts()
    if damage == 'missing': del frozen['0.9.85']
    elif damage == 'corrupt': frozen['0.9.85']['unreviewed_rule'] = True
    else: frozen['0.9.85'] = {**c2_contract_v3(), 'contract_revision': '0.9.85'}
    expected = 'MISSING' if damage == 'missing' else 'CORRUPTED'
    with pytest.raises(RuntimeError, match='RECOVERY_CONTRACT_' + expected):
        rules.recovery_contract_capability(c2_contract_v3(), frozen)


@pytest.mark.parametrize('ablation', ['migration', '085_declaration'])
def test_removing_migration_or_085_declaration_breaks_the_positive(monkeypatch, ablation):
    if ablation == 'migration': monkeypatch.setattr(rules, '_sequence_read_predecessor', lambda current: None)
    else: monkeypatch.setattr(rules, 'RELEASED_READ_CONTRACTS', tuple(p for p in rules.RELEASED_READ_CONTRACTS if p[0] != '0.9.85'))
    with pytest.raises((RuntimeError, AssertionError)):
        test_actual_released_read_migrates_without_claiming_full_equivalence('0.9.85')
