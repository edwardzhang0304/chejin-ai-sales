"""Read-only priorities for one diagnostic SQLite snapshot, never settlement rules.

Unknown states are important evidence. A terminal label only lowers collection
priority; it never authorizes deletion, replay, delivery, or recovery.
"""
import json
import sqlite3
import time

CHAIN = ('hook_raw_inbox', 'hook_observation_projection', 'hook_delivery_outbox')
STATE_COLUMNS = {
    'hook_delivery_outbox': ('status', ('stored',)),
    'hook_voice_attempts': ('state', ('transcribed', 'failed')),
    'hook_active_calls': ('state', ('completed',)),
}


class SnapshotPriority:
    def __init__(self, db, limit, record, voice_receipt_known=lambda _: False, *, deadline=float("inf")):
        self.db, self.limit, self.record = db, limit, record
        self.sequences = []
        self.voice_rows = []
        self.columns = {r[0]: {c['name'] for c in db.execute(f'PRAGMA table_info("{r[0]}")')}
                        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        if r[0] in (*CHAIN, *STATE_COLUMNS, 'local_logs', 'hook_voice_interruptions')}
        roots = {}

        def collect(label, sql, priority):
            try:
                rows = list(db.execute(sql + ' LIMIT ?', (limit + 1,)))
                if len(rows) > limit:
                    record(label, 'omitted', reason='critical_rows_limit', omitted_at_least=1,
                           policy='current_fault_then_unfinished_oldest_then_recent_history')
                for row in rows[:limit]:
                    seq = row[0]
                    if type(seq) is int and 0 < seq <= 9223372036854775807:
                        roots[seq] = min(roots.get(seq, priority), priority)
            except sqlite3.Error as exc:
                record(label, 'read_failed', reason='priority_selection_failed', error_type=type(exc).__name__)

        # Explicit identifiers in recent faults outrank unrelated unfinished work.
        if {'metadata', 'level'} <= self.columns.get('local_logs', set()):
            try:
                for row in db.execute("SELECT CASE WHEN length(metadata)<=65536 THEN metadata END FROM local_logs WHERE lower(level) IN ('error','critical','warning') ORDER BY rowid DESC LIMIT 50"):
                    try:
                        value = json.loads(row[0]) if isinstance(row[0], str) and len(row[0]) <= 65536 else None
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(value, dict):
                        seq = value.get('receive_seq')
                        if type(seq) is int and 0 < seq <= 9223372036854775807:
                            roots[seq] = 0
            except sqlite3.Error as exc:
                record('local_logs', 'read_failed', reason='priority_selection_failed', error_type=type(exc).__name__)
        voice_columns = self.columns.get('hook_voice_attempts', set())
        if {'self_wxid', 'account_epoch', 'native_msg_id', 'peer_wxid', 'state'} <= voice_columns:
            try:
                quoted = ['"'+c.replace('"', '""')+'"' for c in sorted(voice_columns)]
                fields = ','.join(f'CASE WHEN length({c})>65536 THEN NULL ELSE {c} END AS {c}' for c in quoted)
                voices = list(db.execute(f"SELECT rowid AS diagnostic_id,{fields} FROM hook_voice_attempts ORDER BY CASE WHEN state IN ('transcribed','failed') THEN 1 ELSE 0 END,rowid LIMIT ?", (limit+1,)))
                if len(voices) > limit:
                    record('hook_voice_attempts', 'omitted', reason='voice_receipt_discovery_limit', omitted_at_least=1)
                for voice in voices[:limit]:
                    if time.monotonic() >= deadline:
                        record('hook_voice_attempts', 'omitted', reason='voice_receipt_time_limit')
                        break
                    value = dict(voice); rowid = value.pop('diagnostic_id')
                    if value['state'] not in {'transcribed', 'failed'} or not voice_receipt_known(value):
                        self.voice_rows.append(rowid)
            except sqlite3.Error as exc:
                record('hook_voice_attempts', 'read_failed', reason='voice_priority_failed', error_type=type(exc).__name__)
        raw = self.columns.get('hook_raw_inbox', set())
        projection = self.columns.get('hook_observation_projection', set())
        if 'receive_seq' in raw:
            condition = ('NOT EXISTS (SELECT 1 FROM hook_observation_projection p WHERE p.receive_seq=r.receive_seq)'
                         if 'receive_seq' in projection else '1')
            collect('hook_raw_inbox', f'SELECT r.receive_seq FROM hook_raw_inbox r WHERE {condition} ORDER BY r.receive_seq', 1)
        if {'receive_seq', 'status'} <= self.columns.get('hook_delivery_outbox', set()):
            collect('hook_delivery_outbox', "SELECT DISTINCT receive_seq FROM hook_delivery_outbox WHERE status IS NULL OR status!='stored' ORDER BY receive_seq", 1)
        if {'receive_seq', 'projection_json'} <= projection:
            collect('hook_observation_projection', "SELECT receive_seq FROM hook_observation_projection WHERE CASE WHEN json_valid(projection_json) THEN coalesce(json_extract(projection_json,'$.status'),'')!='projected' ELSE 1 END ORDER BY receive_seq", 1)
            if self.voice_rows:
                # Link on the complete retained account/peer identity. IDs from
                # another account must never provide evidence for this parent.
                collect('hook_voice_attempts', f"""SELECT DISTINCT p.receive_seq FROM hook_observation_projection p,
                    json_each(CASE WHEN json_valid(p.projection_json) THEN p.projection_json ELSE '{{}}' END,'$.observations') o
                    JOIN hook_voice_attempts v ON v.self_wxid=json_extract(o.value,'$.self_wxid')
                    AND v.account_epoch=json_extract(o.value,'$.account_epoch')
                    AND v.native_msg_id=json_extract(o.value,'$.native_msg_id')
                    AND (v.peer_wxid=json_extract(o.value,'$.peer_wxid') OR
                        (json_extract(o.value,'$.peer_wxid') IS NULL AND json_extract(o.value,'$.media_stage')='audio_observed'))
                    WHERE v.rowid IN ({','.join(map(str,self.voice_rows))}) ORDER BY p.receive_seq""", 1)
        ordered = sorted(roots, key=lambda seq: (roots[seq], seq))
        self.sequences = ordered[:limit]
        if len(ordered) > limit:
            record('hook_receive_chain', 'omitted', reason='critical_chain_limit', omitted_at_least=len(ordered)-limit)
        self.captured = {}

    def query(self, table, expressions):
        cols = self.columns.get(table, set())
        parts = []
        if table in CHAIN and 'receive_seq' in cols and self.sequences:
            # Integers were type-checked above, never source-provided SQL.
            ids = ','.join(map(str, self.sequences))
            parts.append(f'receive_seq IN ({ids})')
        if table == 'hook_voice_attempts' and self.voice_rows:
            parts.append(f'rowid IN ({",".join(map(str,self.voice_rows))})')
        state = STATE_COLUMNS.get(table)
        if state and state[0] in cols:
            column, terminal = state
            labels = ','.join("'"+x+"'" for x in terminal)
            parts.append(f'({column} IS NULL OR {column} NOT IN ({labels}))')
        if table == 'hook_voice_interruptions' and 'result_json' in cols:
            # All interruption records represent unknown original calls; retain
            # oldest first. They are never inferred to be successful results.
            parts.append('1')
        important = '('+' OR '.join(parts)+')' if parts else '0'
        # Keep oldest unresolved evidence ahead of ordinary recent history.
        linked_order = ''
        if table in CHAIN and 'receive_seq' in cols and self.sequences:
            rank = 'CASE receive_seq ' + ' '.join(f'WHEN {seq} THEN {i}' for i, seq in enumerate(self.sequences)) + f' ELSE {self.limit} END'
            linked_order = rank + ', '
        ordering = f'diagnostic_priority DESC, {linked_order}CASE WHEN {important} THEN rowid END ASC, rowid DESC'
        return f'SELECT {expressions}, {important} AS diagnostic_priority FROM "{table}" ORDER BY {ordering} LIMIT ?'

    def captured_chain(self, table, rows):
        if table not in CHAIN:
            return
        self.captured[table] = {r.get('receive_seq') for r in rows}
        missing = [seq for seq in self.sequences if seq not in self.captured[table]]
        # Missing outbox can be normal (unprojected/quarantined/media input).
        # Explicitly describe absence rather than declaring a complete chain.
        if missing:
            existing = {r[0] for r in self.db.execute(f'SELECT DISTINCT receive_seq FROM "{table}" WHERE receive_seq IN ({",".join(map(str, missing))})')}
            for status, ids in (('omitted', [x for x in missing if x in existing]),
                                ('absent', [x for x in missing if x not in existing])):
                if ids:
                    self.record(table, status, reason='priority_chain_member_unavailable', receive_sequences=ids)
