"""Durable, non-sleeping retry budget for one proven-unsent read episode.

Only the gateway's official read operations may produce an episode. Tick and
object/Worker recreation never reset it. UI explicitly closes an exhausted or
cancelled episode; old evidence is retained, not overwritten.
"""
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict
from . import storage
from .models import utc_now_iso

DELAYS = (1.0, 5.0)
READ_STEPS = frozenset({'hook_login_check','hook_history_catalog','hook_history_read',
                        'hook_history_capture','hook_formal_friend_read'})
WAIT_CODES = frozenset({'HOOK_READ_RETRY_WAIT','HOOK_READ_RETRY_EXHAUSTED','HOOK_READ_RETRY_CANCELLED'})
TABLE_FIELDS = {
    'hook_read_retry': ('event_id','process_generation','request_hash','route','failures',
                        'next_at','state','intent','created_at','updated_at','cancel_reason','scope_key'),
    'hook_transport_events': ('event_id','action_id','process_generation','route','evidence','created_at'),
    'hook_process_recovery': ('old_generation','new_generation','evidence_sha256','death_proof','verified_at'),
}


def validate_schema(conn, table):
    if tuple(r['name'] for r in conn.execute(f'PRAGMA table_info({table})')) != TABLE_FIELDS[table]:
        raise RuntimeError('HOOK_RECOVERY_SCHEMA_INVALID')


class ReadRetryWait(ValueError):
    outcome_unknown = False
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS hook_read_retry (
        event_id TEXT PRIMARY KEY, process_generation TEXT NOT NULL,
        request_hash TEXT NOT NULL, route TEXT NOT NULL, failures INTEGER NOT NULL,
        next_at REAL NOT NULL, state TEXT NOT NULL, intent TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, cancel_reason TEXT, scope_key TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hook_transport_events (
        event_id TEXT PRIMARY KEY, action_id TEXT, process_generation TEXT,
        route TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL)''')
    for table in ('hook_read_retry','hook_transport_events'):
        validate_schema(conn,table)


def pending(conn=None):
    if conn is None:
        with storage.db_connection() as db:
            return pending(db)
    ensure(conn)
    rows=conn.execute("SELECT * FROM hook_read_retry WHERE state NOT IN ('resolved','operator_recheck')").fetchall()
    if len(rows)>1:raise RuntimeError('HOOK_RETRY_JOURNAL_CONFLICT')
    if not rows:return None
    row=dict(rows[0])
    if (row['state'] not in {'waiting','exhausted','cancelled'}
            or type(row['failures']) is not int or not 1<=row['failures']<=3
            or row['state']=='waiting' and row['failures']>2
            or type(row['next_at']) not in {int,float} or not math.isfinite(row['next_at'])):
        raise RuntimeError('HOOK_RETRY_JOURNAL_INVALID')
    return row


def fingerprint(route, payload):
    return hashlib.sha256(json.dumps([route,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def history_subject(delivery,since):
    # Stable authorized capture, independent of a changing local page cursor.
    return {'delivery':asdict(delivery),'since_create_time':since}


def cancel_retired_history(generation,subjects):
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE');row=pending(conn)
        if (row and row['scope_key'] and row['process_generation']==generation
                and row['scope_key'] not in {fingerprint('QueryDB',s) for s in subjects}):
            conn.execute("UPDATE hook_read_retry SET state='cancelled',cancel_reason='history_authorization_changed',updated_at=? WHERE event_id=?",(utc_now_iso(),row['event_id']))
            conn.commit()


def gate(generation, intent, *, now=None, allowed=True):
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE');row=pending(conn)
        if not row:return
        if not allowed or row['process_generation'] != generation or row['intent'] != intent:
            reason=('lifecycle_blocked' if not allowed else 'process_changed'
                    if row['process_generation'] != generation else 'operator_or_worker_changed')
            conn.execute("UPDATE hook_read_retry SET state='cancelled',cancel_reason=?,updated_at=? WHERE event_id=?",(reason,utc_now_iso(),row['event_id']));conn.commit()
            raise ReadRetryWait('HOOK_READ_RETRY_CANCELLED')
        if row['state']!='waiting':
            raise ReadRetryWait('HOOK_READ_RETRY_EXHAUSTED' if row['state']=='exhausted' else 'HOOK_READ_RETRY_CANCELLED')
        if (time.time() if now is None else now)<row['next_at']:
            raise ReadRetryWait('HOOK_READ_RETRY_WAIT')


def record_transport(generation, action_id, route, evidence):
    with storage.db_connection() as conn:
        ensure(conn)
        conn.execute('INSERT INTO hook_transport_events VALUES (?,?,?,?,?,?)',
            (str(uuid.uuid4()),action_id,generation,route,json.dumps(evidence,sort_keys=True),utc_now_iso()))
        conn.commit()


def failed(generation, intent, route, payload, *, now=None, scope_key=None):
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE');row=pending(conn);at=utc_now_iso()
        failures=1 if row is None else row['failures']+1
        state='waiting' if failures<=len(DELAYS) else 'exhausted'
        next_at=(time.time() if now is None else now)+(DELAYS[failures-1] if state=='waiting' else 0)
        if row:
            conn.execute('UPDATE hook_read_retry SET failures=?,next_at=?,state=?,updated_at=? WHERE event_id=?',
                         (failures,next_at,state,at,row['event_id']))
        else:
            conn.execute('INSERT INTO hook_read_retry VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)',
                (str(uuid.uuid4()),generation,fingerprint(route,payload),route,failures,next_at,state,intent,at,at,scope_key))
        conn.commit()
    raise ReadRetryWait('HOOK_READ_RETRY_WAIT' if state=='waiting' else 'HOOK_READ_RETRY_EXHAUSTED')


def succeeded(generation,route,payload):
    with storage.db_connection() as conn:
        ensure(conn)
        conn.execute("UPDATE hook_read_retry SET state='resolved',updated_at=? WHERE process_generation=? AND request_hash=? AND state='waiting'",
                     (utc_now_iso(),generation,fingerprint(route,payload)));conn.commit()


def request_new_round():
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE');row=pending(conn)
        if not row or row['state'] not in {'exhausted','cancelled'}:return False
        conn.execute("UPDATE hook_read_retry SET state='operator_recheck',updated_at=? WHERE event_id=?",(utc_now_iso(),row['event_id']));conn.commit()
        return True


def retire_completed_evidence(conn,cutoff):
    """Use existing critical-retention housekeeping; never retire open waits or
    evidence of a call that might still execute. No cleanup is a recovery step.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_read_retry'").fetchone():
        return
    ensure(conn)
    if pending(conn):return
    conn.execute("DELETE FROM hook_read_retry WHERE state IN ('resolved','operator_recheck') AND updated_at<?",(cutoff,))
    conn.execute('''DELETE FROM hook_transport_events WHERE created_at<?
        AND NOT EXISTS (SELECT 1 FROM hook_active_calls c WHERE
            c.process_generation=hook_transport_events.process_generation
            AND c.action_id=hook_transport_events.action_id AND c.state!='completed')''',(cutoff,))
