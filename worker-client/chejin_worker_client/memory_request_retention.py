"""Bounded diagnostic-only retirement; raw invalid bytes stay local, not in ZIP."""
import base64
import hashlib
import time

MAX_RETIRED = 32
MAX_RETAINED_BYTES = 16 * 1024


def retire(channel, path, reason):
    from .memory_capture_channel import safe
    path = safe(path, channel.root)
    if not path.is_file():
        raise ValueError('channel_record_shape')
    size = path.stat().st_size
    with path.open('rb') as handle:
        raw = handle.read(MAX_RETAINED_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    # Only protocol-owned UUID filenames reach here, never a business path.
    key = hashlib.sha256(path.name.encode()).hexdigest()
    folder = safe(channel.folder / 'retired', channel.root)
    folder.mkdir(exist_ok=True)
    index_path = channel.root / 'artifacts/runtime/memory-request-retirement.json'
    try:
        index = channel.read(index_path)
    except FileNotFoundError:
        index = {'schema': 1, 'evidence_kind': 'diagnostic_request_retirement',
                 'records': [], 'retired_count': 0, 'evicted_count': 0,
                 'raw_content_exported': False, 'max_retained_records': MAX_RETIRED,
                 'max_bytes_per_record': MAX_RETAINED_BYTES}
    _finish_pending(channel, index_path, folder, index)
    if not path.exists():
        return  # This input belonged to the recovered transaction.
    records = index['records']
    existing = next((r for r in records if r['key'] == key), None)
    item = {'key': key, 'source_name': path.name, 'reason': reason,
            'source_bytes': size, 'retained_bytes': len(raw), 'sha256': digest,
            'hash_scope': 'complete_file' if len(raw) == size else 'retained_prefix_only',
            'omitted_bytes': max(0, size - len(raw)), 'retired_at': time.time(),
            'raw_export_omitted_reason': 'invalid_request_may_contain_sensitive_content'}
    # Commit intent before deleting anything. Pending records are explicitly not
    # advertised as retained. At most 32 raw records exist, even after a crash.
    index['pending'] = {'item': item, 'replaces_existing': existing is not None}
    if existing is None and len(records) >= MAX_RETIRED:
        oldest = records.pop(0)
        index['evicted_count'] += 1
        index['last_eviction'] = {'key': oldest['key'], 'reason': 'diagnostic_retention_capacity',
                                  'at': time.time()}
        index['pending']['delete_key'] = oldest['key']
    safe(index_path, channel.root).parent.mkdir(parents=True, exist_ok=True)
    channel.write(index_path, index)
    _finish_pending(channel, index_path, folder, index)


def _finish_pending(channel, index_path, folder, index):
    """Replay the single bounded retirement transaction under the channel lock.

    Source input remains until both evidence and its manifest commit. An eviction
    is declared before unlink, so interrupted deletion never leaves a false
    retained claim. Replays reuse keys and do not increment counters twice.
    """
    from .memory_capture_channel import safe
    pending = index.get('pending')
    if not pending:
        return
    item = pending['item']
    source = safe(channel.requests / item['source_name'], channel.root)
    if pending.get('delete_key'):
        safe(folder / (pending['delete_key'] + '.json'), channel.root).unlink(missing_ok=True)
    with source.open('rb') as handle:
        raw = handle.read(MAX_RETAINED_BYTES)
    if source.stat().st_size != item['source_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise ValueError('retirement_source_changed')
    channel.write(folder / (item['key'] + '.json'), {
        **item, 'raw_base64': base64.b64encode(raw).decode('ascii')})
    index['records'] = [r for r in index['records'] if r['key'] != item['key']] + [item]
    if not pending['replaces_existing']:
        index['retired_count'] += 1
    del index['pending']
    channel.write(index_path, index)
    safe(source, channel.root).unlink()
