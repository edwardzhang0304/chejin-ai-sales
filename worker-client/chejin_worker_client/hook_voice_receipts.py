"""Original voice result delivery evidence shared by recovery and installation."""
from pathlib import Path
import time
from itertools import zip_longest
from .shared_rules import hook_contract_rules as rules


def receipt_path(root, attempt):
    keys = [attempt[k] for k in ('self_wxid', 'account_epoch', 'native_msg_id')]
    if not all(isinstance(v, str) and v not in {'', '.', '..'} and
               all(c.isalnum() or c in '-_' for c in v) for v in keys):
        raise ValueError('HOOK_VOICE_RECEIPT_IDENTITY_INVALID')
    return Path(root) / 'voice-receipts' / keys[0] / keys[1] / (keys[2] + '.json')


def _read_receipt(root, attempt):
    path = receipt_path(root, attempt)
    if path.is_symlink():
        raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    if not path.exists():
        return None
    if path.stat().st_size > 1024*1024:
        raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    value = rules.loads(path.read_bytes())
    if value.get('attempt_digest') != rules.digest(attempt) or not isinstance(value.get('receipt'), dict):
        raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    return value


def acknowledged(root, attempt):
    return _read_receipt(root, attempt) is not None


# Same SQLite database as the original attempts. This is a recoverable delivery
# projection, not a second source of voice identity or permission.
RECONCILE_LIMIT = 32
RECONCILE_SECONDS = 0.05
_INDEX = 'hook_voice_result_delivery'
_COLUMNS = ('root', 'self_wxid', 'account_epoch', 'native_msg_id',
            'status', 'attempt_digest', 'receipt_json', 'checked_seq')
_JOIN = 'd.self_wxid=a.self_wxid AND d.account_epoch=a.account_epoch AND d.native_msg_id=a.native_msg_id'
_FINAL = "a.state IN ('transcribed','failed')"
_TRIGGERS = {
    'hook_voice_delivery_updated': """CREATE TRIGGER hook_voice_delivery_updated
        AFTER UPDATE ON hook_voice_attempts BEGIN
        DELETE FROM hook_voice_result_delivery WHERE self_wxid=OLD.self_wxid
        AND account_epoch=OLD.account_epoch AND native_msg_id=OLD.native_msg_id;
        END""",
    'hook_voice_delivery_deleted': """CREATE TRIGGER hook_voice_delivery_deleted
        AFTER DELETE ON hook_voice_attempts BEGIN
        DELETE FROM hook_voice_result_delivery WHERE self_wxid=OLD.self_wxid
        AND account_epoch=OLD.account_epoch AND native_msg_id=OLD.native_msg_id;
        END""",
}


def _root_key(root):
    return str(Path(root).resolve())


def _has_attempts(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone() is not None


def _index_ready(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_INDEX,)).fetchone() is None:
        return False
    if tuple(r['name'] for r in conn.execute('PRAGMA table_info('+_INDEX+')')) != _COLUMNS:
        raise ValueError('HOOK_VOICE_DELIVERY_INDEX_INVALID')
    for name, sql in _TRIGGERS.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or ' '.join(row['sql'].split()) != ' '.join(sql.split()):
            raise ValueError('HOOK_VOICE_DELIVERY_INDEX_INVALID')
    return True


def _ensure_index(conn):
    if _index_ready(conn):
        return
    conn.execute("""CREATE TABLE hook_voice_result_delivery (
        root TEXT NOT NULL, self_wxid TEXT NOT NULL, account_epoch TEXT NOT NULL,
        native_msg_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('unchecked','pending','confirmed')),
        attempt_digest TEXT, receipt_json TEXT, checked_seq INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(root,self_wxid,account_epoch,native_msg_id),
        CHECK(status<>'confirmed' OR (attempt_digest IS NOT NULL AND receipt_json IS NOT NULL)))""")
    conn.execute('CREATE INDEX hook_voice_delivery_queue ON hook_voice_result_delivery(root,status,checked_seq)')
    conn.execute('CREATE INDEX hook_voice_delivery_sequence ON hook_voice_result_delivery(root,checked_seq)')
    for sql in _TRIGGERS.values():
        conn.execute(sql)


def pending_count(conn, root):
    """Read-only installation projection: unchecked legacy rows also block."""
    if not _has_attempts(conn):
        return 0
    if not _index_ready(conn):
        return conn.execute("SELECT COUNT(*) FROM hook_voice_attempts AS a WHERE "+_FINAL).fetchone()[0]
    key = _root_key(root)
    return conn.execute('SELECT (SELECT COUNT(*) FROM hook_voice_attempts AS a WHERE '+_FINAL+
        ' AND NOT EXISTS (SELECT 1 FROM '+_INDEX+' AS d WHERE d.root=? AND '+_JOIN+')) + '
        '(SELECT COUNT(*) FROM '+_INDEX+" WHERE root=? AND status<>'confirmed')", (key,key)).fetchone()[0]


def reconcile_pending(conn, root):
    """Check bounded legacy/pending work once per dispatcher cycle.

    Confirmed proof is persisted with the original attempt digest. Changes to an
    attempt invalidate that projection transactionally. An ACK-file write before
    an interrupted index update is picked up next time without another HTTP call.
    No in-memory cache survives a cycle and no original record is removed.
    The caller commits the projection before performing network operations.
    """
    if not _has_attempts(conn):
        return []
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    _ensure_index(conn)
    key = _root_key(root)
    conn.execute('INSERT INTO '+_INDEX+' (root,self_wxid,account_epoch,native_msg_id,status) '
        "SELECT ?,a.self_wxid,a.account_epoch,a.native_msg_id,'unchecked' FROM hook_voice_attempts AS a WHERE "+_FINAL+
        ' AND NOT EXISTS (SELECT 1 FROM '+_INDEX+' AS d WHERE d.root=? AND '+_JOIN+') LIMIT ?',
        (key,key,RECONCILE_LIMIT))
    # Give both old-data initialization and already pending uploads a share of
    # each round. checked_seq rotates pending items instead of starving later IDs.
    seq = conn.execute('SELECT COALESCE(MAX(checked_seq),0) FROM '+_INDEX+' WHERE root=?',(key,)).fetchone()[0]
    batches = []
    statuses = ('pending','unchecked') if seq % 2 else ('unchecked','pending')
    for status in statuses:
        batches.append(conn.execute('SELECT a.* FROM '+_INDEX+' AS d JOIN hook_voice_attempts AS a ON '+_JOIN+
            ' WHERE d.root=? AND d.status=? ORDER BY d.checked_seq,d.native_msg_id LIMIT ?',
            (key,status,RECONCILE_LIMIT//2)).fetchall())
    rows = [row for pair in zip_longest(*batches) for row in pair if row is not None]
    deadline = time.monotonic()+RECONCILE_SECONDS
    pending = []
    for i,row in enumerate(rows):
        if i and time.monotonic() >= deadline:
            break
        attempt = dict(row)
        proof = _read_receipt(root,attempt)
        seq += 1
        conn.execute('UPDATE '+_INDEX+' SET status=?,attempt_digest=?,receipt_json=?,checked_seq=? '
            'WHERE root=? AND self_wxid=? AND account_epoch=? AND native_msg_id=?',
            ('confirmed' if proof is not None else 'pending',rules.digest(attempt),
             rules.canonical(proof) if proof is not None else None,seq,key,
             attempt['self_wxid'],attempt['account_epoch'],attempt['native_msg_id']))
        if proof is None:
            pending.append(attempt)
    return pending


def defer_pending(conn, root, attempt):
    """Move the actual upload attempt behind its peers before network I/O.

    Reconciliation only observes files. It cannot stand in for a delivery turn:
    one failed upload must not keep winning just because all files were checked.
    This changes scheduling only, never evidence or completion state.
    """
    if not _index_ready(conn):
        return  # A newly recovered result remains unindexed and blocks install.
    key = _root_key(root)
    conn.execute('UPDATE '+_INDEX+' SET checked_seq=(SELECT COALESCE(MAX(checked_seq),0)+1 '
        'FROM '+_INDEX+' WHERE root=?) WHERE root=? AND self_wxid=? AND account_epoch=? '
        "AND native_msg_id=? AND status IN ('unchecked','pending')",
        (key,key,attempt['self_wxid'],attempt['account_epoch'],attempt['native_msg_id']))


def publish(client, credentials, account, binding, attempt, root):
    from .hook_formal_c1 import _save
    # Retain the original authorization before the HTTP call. If its response
    # is lost, the server may stop advertising this voice job in its inventory.
    path = receipt_path(root, attempt)
    intent = path.with_suffix('.intent.json')
    value = {'attempt_digest': rules.digest(attempt), 'binding': binding}
    if intent.exists():
        prior = rules.loads(intent.read_bytes())
        if prior['attempt_digest'] != value['attempt_digest'] or any(
            prior['binding'][k] != binding[k] for k in ('binding_id', 'binding_revision', 'peer_wxid')):
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
    else:
        _save(intent, value)
    if acknowledged(root, attempt):
        return
    receipt = client.voice_result(credentials, account, binding, attempt)
    _save(path, {'attempt_digest': rules.digest(attempt), 'receipt': receipt})


def retained_job_map(attempts, root, scope):
    jobs = {}
    for attempt in attempts:
        if (attempt['self_wxid'], attempt['account_epoch']) != (scope.self_wxid, scope.account_epoch):
            continue
        intent = receipt_path(root, attempt).with_suffix('.intent.json')
        if intent.is_symlink():
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
        if not intent.exists():
            continue  # Old records require the original server-advertised job.
        if intent.stat().st_size > 1024*1024:
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
        saved = rules.loads(intent.read_bytes())
        if saved['attempt_digest'] != rules.digest(attempt):
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
        binding = saved['binding']
        if binding['peer_wxid'] != attempt['peer_wxid']:
            raise ValueError('HOOK_VOICE_RECEIPT_CONFLICT')
        key = (binding['binding_id'],binding['binding_revision'],binding['peer_wxid'])
        jobs.setdefault(key,[]).append({'native_msg_id': attempt['native_msg_id']})
    return jobs
