"""Durable isolation for a potentially still-running DLL call, no TTL clearing."""
from . import storage
from .models import utc_now_iso
from .hook_process import HookProcessError
CALL_KINDS=frozenset({'history_capture','send_text','IsLoginStatus','ConfigureMsgReciveFullURL','GetDBHandleAndPath','QueryDB','SendTextMsg','GetFriendAndChatRoomList','Voice2Text'})
FIELDS=('process_generation','action_id','kind','state','attempted_at','completed_at')


class PreviousProcessUnconfirmed(ValueError):
    code = 'HOOK_PREVIOUS_PROCESS_EXIT_UNCONFIRMED'

    def __init__(self):
        super().__init__(self.code)


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


def verify_previous_generations(current, generation_exited):
    """Keep old calls immutable; record OS proof allowing only new-process reads.

    This is not a business settlement. Account, callback, historical sends and
    operator recovery still pass their original gates after this check.
    """
    import json
    import hashlib
    with storage.db_connection() as conn:
        ensure(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS hook_process_recovery (
            old_generation TEXT NOT NULL, new_generation TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL, death_proof TEXT NOT NULL,
            verified_at TEXT NOT NULL, PRIMARY KEY(old_generation,new_generation))''')
        from .hook_read_retry import validate_schema
        validate_schema(conn,'hook_process_recovery')
        generations={r[0] for r in conn.execute("SELECT DISTINCT process_generation FROM hook_active_calls WHERE state IN ('calling','uncertain') AND process_generation<>?",(current,))}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone():
            generations.update(r[0] for r in conn.execute("SELECT DISTINCT process_generation FROM hook_voice_attempts WHERE state IN ('transcribing','uncertain') AND process_generation<>?",(current,)))
        for old in sorted(generations):
            proof=conn.execute('SELECT * FROM hook_process_recovery WHERE old_generation=? AND new_generation=?',(old,current)).fetchone()
            if proof:
                if proof['death_proof']!='os_process_generation_absent':
                    raise PreviousProcessUnconfirmed()
                continue  # Death proof remains valid after later business settlement.
            try:
                exited = generation_exited(old)
            except (HookProcessError, OSError) as exc:
                raise PreviousProcessUnconfirmed() from exc
            if exited is not True:
                raise PreviousProcessUnconfirmed()
            rows={'calls':[dict(r) for r in conn.execute('SELECT * FROM hook_active_calls WHERE process_generation=? ORDER BY action_id',(old,))]}
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_voice_attempts'").fetchone():
                rows['voices']=[dict(r) for r in conn.execute('SELECT * FROM hook_voice_attempts WHERE process_generation=? ORDER BY rowid',(old,))]
            digest=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
            conn.execute('INSERT INTO hook_process_recovery VALUES (?,?,?,?,?)',
                (old,current,digest,'os_process_generation_absent',utc_now_iso()))
        conn.commit()
