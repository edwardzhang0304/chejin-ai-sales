"""Target-only relationship check evidence for the existing diagnostic ZIP."""
from pathlib import Path
from . import storage
from .incident_evidence import _atomic_write_json
from .shared_rules import hook_contract_rules as rules


def save_review(root, account, binding, attempts, result=None):
    # Fixed-size snapshots per binding. Normal matching polls do not overwrite
    # the last anomaly or its confirmation, and no complete address book is saved.
    value = {'schema_version': 'hook-relationship-review-v1', 'account': account,
             'binding_id': binding['binding_id'], 'binding_revision': binding['binding_revision'],
             'attempts': attempts[-2:], 'result': result,
             'authority': 'diagnostic_only', 'business_actions_performed': False}
    key = rules.digest([account['self_wxid'], account['account_epoch'],
                        binding['binding_id'], binding['binding_revision']])
    folder = Path(root) / 'relationship-reviews' / key
    try:
        _atomic_write_json(folder / 'latest.json', value)
        if result and result.get('review_required'):
            _atomic_write_json(folder / 'confirmed.json', value)
    except OSError as exc:
        storage.append_log('WARNING', 'hook_relationship_evidence_write_failed',
            '联系人复核证据写入失败，故障包中请核对此缺口。',
            metadata={'binding_id': binding['binding_id'], 'error_type': type(exc).__name__},
            include_exception_text=False)
