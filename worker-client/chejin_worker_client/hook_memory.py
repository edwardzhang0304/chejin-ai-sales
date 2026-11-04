"""Bounded process observations, not a DLL allocation profiler.

No memory trimming, DLL requests, sleeps, object walks or retained sample list.
Private bytes mean Windows private commit; RSS is never substituted for them.
"""
import hashlib
import json
import os
import time

import psutil

DIAGNOSTIC_ROUTES = frozenset({'DiySceneStats', 'SwapMemory'})
METRICS = ('rss_bytes', 'private_bytes')
# Provider units/coverage are not documented. Preserve only recognizable numeric
# labels as unverified counters, never turn them into an allocation estimate.
COUNTER_NAMES = frozenset({'created', 'released', 'alive', 'outstanding', 'active',
    'allocated', 'freed', 'count', 'bytes', 'objects', 'total', 'current',
    'created_count', 'released_count', 'alive_count', 'allocated_bytes', 'freed_bytes'})


def _sample(pid, expected_created=None):
    result = {'pid': pid, 'identity_valid': False, 'unavailable': {}}
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        result['created'] = created
        if expected_created is not None and created != expected_created:
            result['unavailable']['process'] = 'pid_reused'
            return result
        memory = process.memory_info()
        # Re-open to detect replacement during the observation, without relying
        # on psutil's cached create_time on the first Process object.
        if psutil.Process(pid).create_time() != created:
            result['unavailable']['process'] = 'pid_reused'
            return result
        result['identity_valid'] = True
        for field, attribute in (('rss_bytes', 'rss'), ('private_bytes', 'private')):
            value = getattr(memory, attribute, None)
            if type(value) is int and value >= 0:
                result[field] = value
            else:
                result[field] = None
                result['unavailable'][field] = 'metric_unavailable'
    except Exception as exc:
        # Evidence must not replace a business result or trigger a retry. Never
        # serialize exception messages, which may contain paths or identities.
        result['unavailable']['process'] = type(exc).__name__
    return result


def sample_pair(process):
    result = {'monotonic': time.monotonic(), 'wall_time': time.time(),
              'caller': _sample(os.getpid())}
    if process is None:
        result['wechat'] = {'identity_valid': False,
                            'unavailable': {'process': 'verified_instance_missing'}}
    else:
        result['wechat'] = _sample(process.pid, process.started_at)
        result['process_generation'] = process.generation
        result['dll_sha256'] = process.dll_sha256
    return result


def comparison(before, after):
    deltas = {}
    for role in ('caller', 'wechat'):
        left, right = before[role], after[role]
        same = (left.get('identity_valid') is True and right.get('identity_valid') is True
                and left.get('pid') == right.get('pid')
                and left.get('created') == right.get('created'))
        deltas[role] = {field: right[field] - left[field]
            if same and type(left.get(field)) is int and type(right.get(field)) is int else None
            for field in METRICS}
    return {'schema': 1, 'attribution': 'process_net_change_not_dll_allocation',
            'before': before, 'after': after, 'delta_bytes': deltas,
            'peak_measured': False, 'delayed_release_measured': False}


def provider_summary(route, raw):
    """Keep a bounded, body-free response receipt even for unsupported versions."""
    result = {'route': route, 'response_bytes': len(raw),
              'response_sha256': hashlib.sha256(raw).hexdigest(),
              'status': 'semantics_unverified', 'allocation_bytes_known': False,
              'counters': [], 'omitted_fields': 0}
    if route == 'SwapMemory':
        result['effect'] = 'working_set_only_not_leak_repair'
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError, UnicodeError):
        result['status'] = 'invalid_json'
        return result
    if not isinstance(value, dict):
        result['status'] = 'unsupported_shape'
        return result
    for key in ('code', 'status'):
        if type(value.get(key)) is int:
            result['provider_' + key] = value[key]
    if type(value.get('code')) is int and value['code'] != 0:
        result['status'] = 'provider_rejected'

    def visit(obj, path=(), depth=0):
        if not isinstance(obj, dict) or depth > 2:
            result['omitted_fields'] += 1
            return
        for name, item in obj.items():
            if name in ('data', 'stats', 'counters') and isinstance(item, dict):
                visit(item, (*path, name), depth + 1)
            elif name in COUNTER_NAMES and type(item) is int and len(result['counters']) < 32:
                result['counters'].append({'field': '.'.join((*path, name)),
                    'value': item, 'units': 'unverified', 'coverage': 'unverified'})
            elif name not in ('code', 'status'):
                result['omitted_fields'] += 1
    visit(value)
    return result


def record_gap(route, error):
    """One bounded fallback for unavailable pre-call telemetry, no raw payload."""
    from . import storage
    try:
        root = storage.APP_DIR / 'artifacts' / 'runtime'
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'hook-memory-gap.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'status': 'pre_call_evidence_failed',
            'delivery': 'not_attempted', 'route': route, 'reason': type(error).__name__,
            'at': time.time(), 'pid': os.getpid(), 'retention': 'latest_failure_only'}))
        os.replace(temporary, path)
    except Exception:
        pass  # Active-call settlement still must persist, or stays fail-closed.
