"""Durable isolation for a potentially still-running DLL call, no TTL clearing."""
from . import storage
from .models import utc_now_iso
CALL_KINDS=frozenset({'history_capture','send_text','IsLoginStatus','ConfigureMsgReciveFullURL','GetDBHandleAndPath','QueryDB','SendTextMsg','GetFriendAndChatRoomList','Voice2Text'})
FIELDS=('process_generation','action_id','kind','state','attempted_at','completed_at')


def ensure(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS hook_active_calls (process_generation TEXT NOT NULL, action_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, attempted_at TEXT NOT NULL, completed_at TEXT, PRIMARY KEY(process_generation,action_id))')
    if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_active_calls)'))!=FIELDS:
        raise RuntimeError('HOOK_ACTIVE_CALL_SCHEMA_INVALID')


def unresolved(generation):
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_active_calls'").fetchone() is None:return False
        ensure(conn)
        return conn.execute("SELECT 1 FROM hook_active_calls WHERE process_generation=? AND state IN ('calling','uncertain') LIMIT 1",(generation,)).fetchone() is not None


def reserve(generation,action_id,kind):
    if kind not in CALL_KINDS:raise ValueError('HOOK_ACTIVE_CALL_KIND_INVALID')
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE');ensure(conn)
        if conn.execute('SELECT 1 FROM hook_active_calls WHERE process_generation=? AND action_id=?',(generation,action_id)).fetchone():
            raise ValueError('HOOK_ACTIVE_CALL_PREVIOUS_ATTEMPT')
        conn.execute('INSERT INTO hook_active_calls VALUES (?,?,?,?,?,NULL)',(generation,action_id,kind,'calling',utc_now_iso()));conn.commit()


def finish(generation,action_id,completed):
    with storage.db_connection() as conn:
        ensure(conn)
        changed=conn.execute("UPDATE hook_active_calls SET state=?,completed_at=? WHERE process_generation=? AND action_id=? AND state='calling'",('completed' if completed else 'uncertain',utc_now_iso(),generation,action_id)).rowcount
        if changed!=1:raise RuntimeError('HOOK_ACTIVE_CALL_RESULT_NOT_SAVED')
        conn.commit()
