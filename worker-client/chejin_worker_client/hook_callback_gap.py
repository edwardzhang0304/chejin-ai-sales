"""Durable customer-only read limits. These records never grant a send permit.

Keep counters on disk, not a growing in-memory map. Process/Worker restart and
new callbacks do not renew a missing message's budget. Original captures and
receipts remain in the existing evidence store.
"""
import json
from . import storage
from .models import utc_now_iso
from .shared_rules import hook_contract_rules as rules

MAX_ATTEMPTS = 3


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS hook_callback_gap_reads (
        scope_key TEXT NOT NULL, native_msg_id TEXT NOT NULL,
        attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 3), updated_at TEXT NOT NULL,
        PRIMARY KEY(scope_key,native_msg_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hook_callback_gap_holds (
        binding_key TEXT PRIMARY KEY, checkpoint TEXT NOT NULL,
        result_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')


class CallbackGapBudget:
    def __init__(self, worker_id, scope, binding):
        self.scope_key = rules.digest([worker_id,scope.self_wxid,scope.account_epoch,binding['peer_wxid']])
        self.binding_key = rules.digest([self.scope_key,binding['binding_id'],binding['binding_revision'],
                                         binding['history_since_create_time']])
        self.generation = scope.process_generation

    def reserve(self, native_id):
        # Reserve before the DLL call: a crash must not replenish attempts.
        with storage.db_connection() as conn:
            ensure(conn); conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT attempts FROM hook_callback_gap_reads WHERE scope_key=? AND native_msg_id=?',
                             (self.scope_key,native_id)).fetchone()
            attempts=row[0] if row else 0
            if attempts >= MAX_ATTEMPTS:
                return False
            conn.execute('INSERT OR REPLACE INTO hook_callback_gap_reads VALUES(?,?,?,?)',
                         (self.scope_key,native_id,attempts+1,utc_now_iso()))
            conn.commit()
        return True

    def resolved(self, native_id):
        # A unique, validated pre-invitation proof is not an unresolved gap.
        with storage.db_connection() as conn:
            ensure(conn)
            conn.execute('UPDATE hook_callback_gap_reads SET attempts=0,updated_at=? WHERE scope_key=? AND native_msg_id=?',
                         (utc_now_iso(),self.scope_key,native_id)); conn.commit()

    def remaining(self, native_id):
        with storage.db_connection() as conn:
            ensure(conn)
            row=conn.execute('SELECT attempts FROM hook_callback_gap_reads WHERE scope_key=? AND native_msg_id=?',
                             (self.scope_key,native_id)).fetchone()
            return row is None or row[0] < MAX_ATTEMPTS

    def checkpoint(self, barrier):
        return rules.digest([self.generation,barrier])

    def held(self, barrier):
        if barrier[1]:
            return None
        with storage.db_connection() as conn:
            ensure(conn)
            row=conn.execute('SELECT result_json FROM hook_callback_gap_holds WHERE binding_key=? AND checkpoint=?',
                             (self.binding_key,self.checkpoint(barrier))).fetchone()
        if row is None:
            return None
        result=json.loads(row[0])
        if (result.get('history_ready') is not False or result.get('content_isolated') is not True
                or result.get('callback_gap_isolated') is not True):
            raise ValueError('HOOK_CALLBACK_GAP_HOLD_INVALID')
        return result

    def hold(self, barrier, result):
        if (barrier[1] or result.get('history_ready') is not False
                or result.get('content_isolated') is not True
                or result.get('callback_gap_isolated') is not True):
            return
        compact={key:result[key] for key in ('history_ready','content_isolated','callback_gap_isolated','reason_code',
                                           'conversation_id','history_receipt_id') if key in result}
        with storage.db_connection() as conn:
            ensure(conn)
            conn.execute('INSERT OR REPLACE INTO hook_callback_gap_holds VALUES(?,?,?,?)',
                         (self.binding_key,self.checkpoint(barrier),rules.canonical(compact),utc_now_iso()))
            conn.commit()
