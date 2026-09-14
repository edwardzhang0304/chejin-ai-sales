import copy
import hashlib
import json

import pytest

from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.pending_read_recovery import accepts_handoff, package_recovery_capability
from chejin_worker_client.shared_rules import contract_rules


def pair(revision, *, different_rule=False):
    document = copy.deepcopy(c2_contract_v3())
    document['contract_revision'] = revision
    if different_rule:
        document['new_business_rule'] = True
    return {'revision': revision, 'sha256': hashlib.sha256(json.dumps(document, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()}


@pytest.mark.parametrize('revision', ['0.9.78', '0.9.81', '0.10.12'])
def test_handoff_inherits_identical_rules_without_new_manifest_version_entries(revision):
    capability = package_recovery_capability()
    original = copy.deepcopy(capability)
    identity = pair(revision)
    assert identity not in capability['contracts']
    assert accepts_handoff(capability, {'protocol_version': 1, 'contracts': [identity]})
    assert capability == original


@pytest.mark.parametrize('change', ['different_rule', 'hash', 'revision', 'target_rules', 'missing_capability'])
def test_handoff_never_infers_compatibility_from_version_number_alone(change):
    capability = package_recovery_capability()
    identity = pair('0.9.78', different_rule=change == 'different_rule')
    if change == 'hash': identity['sha256'] = '0' * 64
    if change == 'revision': identity['revision'] = '0.9.80'
    if change == 'target_rules': capability['compatible_rules_sha256'] = '0' * 64
    if change == 'missing_capability': capability.pop('compatible_rules_sha256')
    assert not accepts_handoff(capability, {'protocol_version': 1, 'contracts': [identity]})


def test_actual_rule_change_invalidates_old_hash_even_when_release_label_is_unchanged():
    original = c2_contract_v3()
    identity = pair(original['contract_revision'])
    changed = copy.deepcopy(original)
    changed['new_business_rule'] = True
    assert contract_rules.equivalent_contract(changed, identity['revision'], identity['sha256']) is None


def test_old_manifest_readers_keep_the_explicit_contract_list():
    capability = package_recovery_capability()
    capability.pop('compatible_rules_sha256')
    assert accepts_handoff(capability, {'protocol_version': 1, 'contracts': capability['contracts']})
