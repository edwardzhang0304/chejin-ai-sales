"""Read-only priorities for one diagnostic SQLite snapshot, never settlement rules.

Unknown states are important evidence. A terminal label only lowers collection
priority; it never authorizes deletion, replay, delivery, or recovery.
"""
import json
import sqlite3
import time

CHAIN = ('hook_raw_inbox', 'hook_observation_projection', 'hook_delivery_outbox')
STATE_COLUMNS = {
    'hook_voice_attempts': ('state', ('transcribed', 'failed')),
    'hook_active_calls': ('state', ('completed',)),
    'hook_read_retry': ('state', ('resolved','operator_recheck')),
}


def _object_sql(expression, kind=None):
    # json_each.value for a JSON string is unquoted; checking its SQL value
    # alone could both throw and mistake an encoded object for an object.
    if kind:
        return f"CASE WHEN {kind}='object' THEN {expression} ELSE '{{}}' END"
    return (f"CASE WHEN json_valid({expression}) THEN CASE WHEN json_type({expression})='object' "
            f"THEN {expression} ELSE '{{}}' END ELSE '{{}}' END")


def _array_sql(document, field):
    return (f"CASE WHEN json_type({document},'$.{field}')='array' "
            f"THEN json_extract({document},'$.{field}') ELSE '[]' END")


def json_shape_invalid(table, field, value):
    """Only evidence-shape diagnostics; never a business acceptance decision."""
    children = {('hook_observation_projection', 'projection_json'): 'observations',
                ('hook_delivery_outbox', 'payload_json'): 'messages'}.get((table, field))
    if children is None:
        return False
    if not isinstance(value, dict) or not isinstance(value.get(children), list):
        return True
    if children == 'messages' and any(not isinstance(value.get(k), str)
            for k in ('self_wxid', 'account_epoch', 'peer_wxid')):
        return True
    for item in value[children]:
        fields = ('native_msg_id',) if children == 'messages' else ('self_wxid', 'account_epoch', 'message_type', 'disposition')
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in fields):
            return True
    return False


class SnapshotPriority:
    def __init__(self, db, limit, record, voice_receipt_known=lambda _: False, *, deadline):
        self.db, self.limit, self.record = db, limit, record
        self.sequences = []
        self.voice_rows = []
        self.voice_terminal_checked = 0
        self.columns = {r[0]: {c['name'] for c in db.execute(f'PRAGMA table_info("{r[0]}")')}
                        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        if r[0] in (*CHAIN, *STATE_COLUMNS, 'local_logs', 'hook_voice_interruptions',
                                    'hook_transport_events','hook_process_recovery')}
        self.delivery_pending = "(status IS NULL OR status!='stored')"
        delivery_fields = ('status','event_uuid','payload_digest','payload_json','receipt_json')
        if set(delivery_fields) <= self.columns.get('hook_delivery_outbox', set()):
            from .hook_delivery_lifecycle import terminal_delivery
            reported = set()

            def delivery_terminal(*values):
                row = dict(zip(delivery_fields, values))
                if row['status'] != 'covered_by_new_authorization':
                    return int(row['status'] == 'stored')
                reason = None
                if time.monotonic() >= deadline:
                    reason = 'delivery_proof_time_limit'
                elif row['payload_json'] is None or row['receipt_json'] is None:
                    reason = 'delivery_proof_unavailable_or_size_limit'
                else:
                    try:
                        return int(terminal_delivery(row))
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        reason = 'delivery_coverage_proof_invalid'
                # Bounded diagnostic reporting; an unverified terminal label
                # stays important. Never change business data during export.
                if reason not in reported:
                    record('hook_delivery_outbox', 'read_failed', reason=reason,
                           event_uuid=row['event_uuid'])
                    reported.add(reason)
                return 0

            db.create_function('diagnostic_delivery_terminal', len(delivery_fields), delivery_terminal)
            # Match the exporter's existing per-cell bound, including damaged
            # or oversized legacy rows. Unverifiable proof remains prioritized.
            arguments = ','.join(f'CASE WHEN length({f})<=65536 THEN {f} END'
                                if f.endswith('_json') else f for f in delivery_fields)
            self.delivery_pending = ("(CASE WHEN status='stored' THEN 0 "
                "WHEN status='covered_by_new_authorization' THEN "
                f'NOT diagnostic_delivery_terminal({arguments}) ELSE 1 END)')
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
                # Stream within the shared snapshot deadline. A confirmed old
                # receipt consumes time, not one of the critical evidence slots.
                voices = db.execute(f"SELECT rowid AS diagnostic_id,{fields} FROM hook_voice_attempts ORDER BY CASE WHEN state IN ('transcribed','failed') THEN 1 ELSE 0 END,rowid")
                for voice in voices:
                    value = dict(voice); rowid = value.pop('diagnostic_id')
                    reference = {k: value[k] for k in ('self_wxid', 'account_epoch', 'peer_wxid', 'native_msg_id')}
                    if time.monotonic() >= deadline:
                        record('hook_voice_attempts', 'omitted', reason='voice_receipt_time_limit', voice_reference=reference)
                        break
                    if value['state'] not in {'transcribed', 'failed'} or not voice_receipt_known(value):
                        if len(self.voice_rows) >= limit:
                            record('hook_voice_attempts', 'omitted', reason='critical_voice_limit',
                                   omitted_at_least=1, voice_reference=reference,
                                   missing_relations=['parent_raw', 'audio_raw', 'projection', 'voice_receipt'])
                            break
                        self.voice_rows.append(rowid)
                    if value['state'] in {'transcribed', 'failed'}:
                        self.voice_terminal_checked = rowid
                voices.close()
            except sqlite3.Error as exc:
                record('hook_voice_attempts', 'read_failed', reason='voice_priority_failed', error_type=type(exc).__name__)
        raw = self.columns.get('hook_raw_inbox', set())
        projection = self.columns.get('hook_observation_projection', set())
        if 'receive_seq' in raw:
            condition = ('NOT EXISTS (SELECT 1 FROM hook_observation_projection p WHERE p.receive_seq=r.receive_seq)'
                         if 'receive_seq' in projection else '1')
            collect('hook_raw_inbox', f'SELECT r.receive_seq FROM hook_raw_inbox r WHERE {condition} ORDER BY r.receive_seq', 1)
        if {'receive_seq', 'status'} <= self.columns.get('hook_delivery_outbox', set()):
            collect('hook_delivery_outbox', f"SELECT DISTINCT receive_seq FROM hook_delivery_outbox WHERE {self.delivery_pending} ORDER BY receive_seq", 1)
        if {'receive_seq', 'projection_json'} <= projection:
            document = _object_sql('projection_json')
            collect('hook_observation_projection', f"SELECT receive_seq FROM hook_observation_projection WHERE coalesce(json_extract({document},'$.status'),'')!='projected' ORDER BY receive_seq", 1)
            # Normalize before every JSON read. One damaged row cannot prevent
            # finding a different callback interrupted before/partway through enqueue.
            ctes = f"projections AS (SELECT receive_seq,{_object_sql('projection_json')} AS document FROM hook_observation_projection)"
            observation = _object_sql('o.value', 'o.type')
            message = _object_sql('m.value', 'm.type')
            stored = '0'
            if {'receive_seq', 'status', 'payload_json'} <= self.columns.get('hook_delivery_outbox', set()):
                ctes += f", deliveries AS (SELECT receive_seq,status,{_object_sql('payload_json')} AS document FROM hook_delivery_outbox)"
                stored = f"""EXISTS (SELECT 1 FROM deliveries d,
                    json_each({_array_sql('d.document', 'messages')}) m
                    WHERE d.receive_seq=p.receive_seq AND d.status='stored'
                    AND json_type(d.document,'$.self_wxid')='text'
                    AND json_type(d.document,'$.account_epoch')='text'
                    AND json_type(d.document,'$.peer_wxid')='text'
                    AND json_type({message},'$.native_msg_id')='text'
                    AND json_extract(d.document,'$.self_wxid')=json_extract({observation},'$.self_wxid')
                    AND json_extract(d.document,'$.account_epoch')=json_extract({observation},'$.account_epoch')
                    AND json_extract(d.document,'$.peer_wxid')=json_extract({observation},'$.peer_wxid')
                    AND json_extract({message},'$.native_msg_id')=json_extract({observation},'$.native_msg_id'))"""
            voice_settled = '0'
            if {'self_wxid', 'account_epoch', 'native_msg_id', 'peer_wxid', 'state'} <= voice_columns:
                excluded = ','.join(map(str, self.voice_rows)) or '0'
                voice_settled = f"""EXISTS (SELECT 1 FROM hook_voice_attempts v
                    WHERE v.self_wxid=json_extract({observation},'$.self_wxid')
                    AND v.account_epoch=json_extract({observation},'$.account_epoch')
                    AND v.native_msg_id=json_extract({observation},'$.native_msg_id')
                    AND (v.peer_wxid=json_extract({observation},'$.peer_wxid') OR
                        (json_extract({observation},'$.peer_wxid') IS NULL AND json_extract({observation},'$.media_stage')='audio_observed'))
                    AND v.state IN ('transcribed','failed') AND v.rowid<={self.voice_terminal_checked}
                    AND v.rowid NOT IN ({excluded}))"""
            collect('hook_observation_projection', f"""WITH {ctes}
                SELECT p.receive_seq FROM projections p
                WHERE json_extract(p.document,'$.status')='projected'
                AND (coalesce(json_type(p.document,'$.observations'),'')!='array' OR
                    EXISTS (SELECT 1 FROM json_each({_array_sql('p.document', 'observations')}) o
                    WHERE coalesce(json_extract({observation},'$.reason_code'),'')!='HOOK_GROUP_EXCLUDED'
                    AND CASE WHEN json_extract({observation},'$.message_type')='voice' THEN NOT ({voice_settled})
                        ELSE coalesce(json_extract({observation},'$.disposition'),'')!='message_candidate' OR NOT ({stored}) END))
                ORDER BY p.receive_seq""", 1)
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
        if table == 'hook_delivery_outbox' and 'status' in cols:
            parts.append(self.delivery_pending)
        elif state and state[0] in cols:
            column, terminal = state
            labels = ','.join("'"+x+"'" for x in terminal)
            parts.append(f'({column} IS NULL OR {column} NOT IN ({labels}))')
        if table == 'hook_voice_interruptions' and 'result_json' in cols:
            # All interruption records represent unknown original calls; retain
            # oldest first. They are never inferred to be successful results.
            parts.append('1')
        if table == 'hook_transport_events' and 'evidence' in cols:
            evidence=_object_sql('evidence')
            parts.append(f"coalesce(json_extract({evidence},'$.delivery'),'unknown')!='responded'")
        if table == 'hook_process_recovery':
            parts.append('new_generation=(SELECT new_generation FROM hook_process_recovery ORDER BY rowid DESC LIMIT 1)')
        important = '('+' OR '.join(parts)+')' if parts else '0'
        if table == 'hook_transport_events' and {'process_generation','action_id','route','evidence','created_at'} <= cols:
            current_retry = '0'
            if {'process_generation','route','first_transport_event_id','state'} <= self.columns.get('hook_read_retry',set()):
                current_retry = """EXISTS (SELECT 1 FROM hook_read_retry r
                    WHERE r.state NOT IN ('resolved','operator_recheck')
                    AND r.process_generation=hook_transport_events.process_generation
                    AND r.route=hook_transport_events.route
                    AND hook_transport_events.rowid>=(SELECT e.rowid FROM hook_transport_events e
                        WHERE e.event_id=r.first_transport_event_id))"""
            active_call = '0'
            if {'process_generation','action_id','state'} <= self.columns.get('hook_active_calls',set()):
                active_call = """EXISTS (SELECT 1 FROM hook_active_calls c
                    WHERE c.state IN ('calling','uncertain')
                    AND c.process_generation=hook_transport_events.process_generation
                    AND c.action_id=hook_transport_events.action_id)"""
            important=f'(CASE WHEN {active_call} THEN 3 WHEN {current_retry} THEN 2 WHEN {important} THEN 1 ELSE 0 END)'
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
