"""Resolve one capture's producer outcome without promoting stale reports."""
from .python_memory_profile import MAX_REPORT_BYTES


def resolve(channel, capture_id, target):
    root = channel.root / 'artifacts/runtime'
    inputs = [('report', root / 'python-memory' / (capture_id + '.json')),
              ('current_status', root / 'python-memory-status.json'),
              ('gap', root / 'python-memory-gap.json')]
    matched, issues = [], []
    for role, path in inputs:
        try:
            value = channel.read(path, limit=MAX_REPORT_BYTES)
        except FileNotFoundError:
            continue
        except Exception as exc:
            from .memory_capture_channel import failure_reason
            issues.append({'source': role, 'reason': failure_reason(exc)})
            continue
        if (value.get('capture_id') == capture_id
                and all(value.get(key) == target.get(key)
                        for key in ('pid', 'created', 'runtime_session'))):
            matched.append((role, value))
    if not matched:
        raise ValueError('capture_identity_mismatch')
    # A capture is single-use. A matching failure remains authoritative even if
    # its incomplete report could not be replaced. Other sessions never vote.
    failures = [(role, value) for role, value in matched
                if value.get('status') in ('capture_failed', 'not_started')
                or value.get('capture_failed')]
    terminal = failures or [(r, v) for r, v in matched if v.get('status') == 'finished']
    candidates = terminal or matched
    role, value = candidates[0]
    reason = (value.get('first_failure') or value.get('capture_failed')
              or value.get('reason') or value.get('end_reason'))
    # A failed report can contain the original cause; later gap writes may
    # record a second I/O error. Keep both sources, not just the last exception.
    if failures:
        for _, item in failures:
            if item.get('first_failure') or item.get('capture_failed'):
                reason = item.get('first_failure') or item['capture_failed']
                break
    complete = not failures and value.get('status') == 'finished' and reason == 'duration_complete'
    if issues:
        complete = False
        reason = reason if failures else issues[0]['reason']
    return {'status': 'finished' if complete else 'failed',
            'reason': reason or 'capture_interrupted_before_terminal_record',
            'source_capture_status': value.get('status'),
            'started_monotonic': value['started_monotonic'],
            'outcome_source': role,
            'outcome_sources': [{'source': r, 'status': v.get('status'),
                                 'reason': v.get('first_failure') or v.get('capture_failed')
                                 or v.get('reason') or v.get('end_reason')} for r, v in matched],
            'outcome_read_issues': issues}
