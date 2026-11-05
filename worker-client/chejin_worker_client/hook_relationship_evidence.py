"""Target-only relationship check evidence for the existing diagnostic ZIP."""
from pathlib import Path
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
        # Reuse the independent bounded recorder already included in the ZIP.
        # Do not call the SQLite logger from an optional evidence failure path.
        from .failure_evidence import record_capture_failure
        last = attempts[-1] if attempts else {}
        record_capture_failure('hook_relationship_review',exc,
            error_code=last.get('reason_code','HOOK_RELATIONSHIP_EVIDENCE_WRITE_FAILED'),
            safe_context={'account':account,'binding_id':binding['binding_id'],
                'binding_revision':binding['binding_revision'],'attempts':attempts[-2:],
                'missing_target':str(folder.name)+'/latest.json','result':result})
