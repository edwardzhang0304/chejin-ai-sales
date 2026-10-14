"""Append-only raw history pages and scoped cursor evidence, no coverage grant."""
from dataclasses import asdict
import hashlib
import uuid

from . import storage
from .hook_facts import history_observations
from .hook_history import HookHistoryCapture, HookHistoryCatalog, HookHistoryQuery
from .hook_inbox import HookReceiveScope
from .hook_outbox import HookDeliveryScope
from .shared_rules import hook_contract_rules as rules

HISTORY_PAGE_FIELDS = ("history_seq", "event_uuid", "context_json", "context_digest", "raw_body", "raw_digest",
    "projection_json", "projection_digest", "cursor_before", "cursor_after", "upper_local_id", "captured_at")
HISTORY_PROJECTION_VERSION = "hook-history-page-v1"
HISTORY_NAMESPACE = uuid.UUID("1526ec9c-ea9e-4e29-8a0c-ce270e46c41f")


class HookHistoryPageStore:
    def __init__(self, *, max_storage_bytes: int) -> None:
        if type(max_storage_bytes) is not int or max_storage_bytes <= 0:
            raise ValueError("HOOK_HISTORY_STORAGE_LIMIT_INVALID")
        self.max_storage_bytes = max_storage_bytes

    def _schema(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS hook_history_pages (
            history_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_uuid TEXT NOT NULL UNIQUE,
            context_json TEXT NOT NULL, context_digest TEXT NOT NULL, raw_body BLOB NOT NULL,
            raw_digest TEXT NOT NULL, projection_json TEXT NOT NULL, projection_digest TEXT NOT NULL,
            cursor_before TEXT NOT NULL, cursor_after TEXT NOT NULL, upper_local_id TEXT NOT NULL,
            captured_at TEXT NOT NULL)""")
        if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_history_pages)')) != HISTORY_PAGE_FIELDS:
            raise RuntimeError("HOOK_HISTORY_STORAGE_SCHEMA_UNSUPPORTED")

    def append(self, capture: HookHistoryCapture, *, captured_at: str) -> dict:
        """The caller advances only this query's cursor after a committed return.

        Exact repeated reads are idempotent; changed responses are new evidence.
        Rows remain private and separate from callback sequence/authorization.
        """
        if not isinstance(capture, HookHistoryCapture) or capture.query.kind != 'page' or not rules.aware(captured_at):
            raise ValueError("HOOK_HISTORY_STORAGE_CAPTURE_INVALID")
        capture.query.validate_scope(capture.catalog)
        response = capture.query.validate_response(capture.payload)
        rows = response.get('data', [])
        after = rows[-1]['localId'] if rows else capture.query.after_local_id
        context = {'catalog': asdict(capture.catalog), 'query': asdict(capture.query)}
        context_json, context_digest = rules.canonical(context), rules.digest(context)
        raw_digest = hashlib.sha256(capture.raw_body).hexdigest()
        event_uuid = str(uuid.uuid5(HISTORY_NAMESPACE, rules.canonical([context_digest, raw_digest])))
        with storage.db_connection() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                self._schema(conn)
                old = conn.execute('SELECT * FROM hook_history_pages WHERE event_uuid=?', (event_uuid,)).fetchone()
                if old:
                    self._verify(old)
                    if old['context_digest'] != context_digest or old['raw_digest'] != raw_digest:
                        raise RuntimeError("HOOK_HISTORY_STORAGE_ID_CONFLICT")
                    conn.commit()
                    return self._receipt(old)
                # Temporary empty projection is never visible outside this transaction.
                cursor = conn.execute("""INSERT INTO hook_history_pages
                    (event_uuid,context_json,context_digest,raw_body,raw_digest,projection_json,projection_digest,
                        cursor_before,cursor_after,upper_local_id,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_uuid, context_json, context_digest, capture.raw_body, raw_digest, '{}', '',
                        capture.query.after_local_id, after, capture.query.upper_local_id, captured_at))
                observations = history_observations(rows, capture.catalog.scope, event_uuid=event_uuid,
                    receive_seq=cursor.lastrowid, database=capture.query.database.name,
                    database_generation=capture.query.database.generation, max_entries=capture.query.page_size)
                projection = {'contract_version': HISTORY_PROJECTION_VERSION, 'source_event_uuid': event_uuid,
                    'history_seq': cursor.lastrowid, 'raw_evidence_digest': raw_digest,
                    'observations': [asdict(value) for value in observations]}
                encoded, digest = rules.canonical(projection), rules.digest(projection)
                conn.execute('UPDATE hook_history_pages SET projection_json=?,projection_digest=? WHERE history_seq=?',
                    (encoded, digest, cursor.lastrowid))
                used = conn.execute("""SELECT COALESCE(SUM(length(raw_body)+length(CAST(context_json AS BLOB))+
                    length(CAST(projection_json AS BLOB))),0) FROM hook_history_pages""").fetchone()[0]
                if used > self.max_storage_bytes:
                    raise RuntimeError("HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED")
                row = conn.execute('SELECT * FROM hook_history_pages WHERE history_seq=?', (cursor.lastrowid,)).fetchone()
                conn.commit()
                return self._receipt(row)
            except Exception:
                conn.rollback()
                raise

    def _verify(self, row):
        if (not isinstance(row['raw_body'], bytes)
                or hashlib.sha256(row['raw_body']).hexdigest() != row['raw_digest']
                or hashlib.sha256(row['context_json'].encode('utf-8')).hexdigest() != row['context_digest']
                or hashlib.sha256(row['projection_json'].encode('utf-8')).hexdigest() != row['projection_digest']):
            raise RuntimeError("HOOK_HISTORY_STORAGE_EVIDENCE_CHANGED")
        try:
            context = rules.loads(row['context_json'])
            scope = HookReceiveScope(**context['catalog']['scope'])
            catalog = HookHistoryCatalog.capture(scope, {'data': [
                {'dbpath': db['path'], 'dbhandle': db['handle']} for db in context['catalog']['databases']]})
            if rules.digest(asdict(catalog)) != rules.digest(context['catalog']):
                raise ValueError()
            values = dict(context['query'])
            if rules.digest(values['database']) != rules.digest(asdict(catalog.database(values['database']['name']))):
                raise ValueError()
            values['database'] = catalog.database(values['database']['name'])
            values['delivery_scope'] = HookDeliveryScope(**values['delivery_scope'])
            capture = HookHistoryCapture(catalog, HookHistoryQuery(**values), row['raw_body'])
            if capture.query.kind != 'page' or not rules.aware(row['captured_at']):
                raise ValueError()
            rows = capture.payload.get('data', [])
            after = rows[-1]['localId'] if rows else capture.query.after_local_id
            if (row['cursor_before'] != capture.query.after_local_id or row['cursor_after'] != after
                    or row['upper_local_id'] != capture.query.upper_local_id
                    or str(uuid.uuid5(HISTORY_NAMESPACE, rules.canonical([row['context_digest'], row['raw_digest']]))) != row['event_uuid']):
                raise ValueError()
            observations = history_observations(rows, scope, event_uuid=row['event_uuid'], receive_seq=row['history_seq'],
                database=capture.query.database.name, database_generation=capture.query.database.generation,
                max_entries=capture.query.page_size)
            expected = {'contract_version': HISTORY_PROJECTION_VERSION, 'source_event_uuid': row['event_uuid'],
                'history_seq': row['history_seq'], 'raw_evidence_digest': row['raw_digest'],
                'observations': [asdict(value) for value in observations]}
            if rules.digest(expected) != row['projection_digest']:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError("HOOK_HISTORY_STORAGE_EVIDENCE_CHANGED") from None

    def _receipt(self, row) -> dict:
        return {'history_seq': row['history_seq'], 'event_uuid': row['event_uuid'],
            'cursor_before': row['cursor_before'], 'cursor_after': row['cursor_after'],
            'upper_local_id': row['upper_local_id'], 'business_ready': False}

    def read(self, event_uuid: str) -> dict | None:
        if not rules.identifier(event_uuid):
            raise ValueError("HOOK_HISTORY_STORAGE_REFERENCE_INVALID")
        with storage.db_connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_history_pages'").fetchone() is None:
                return None
            if tuple(row['name'] for row in conn.execute('PRAGMA table_info(hook_history_pages)')) != HISTORY_PAGE_FIELDS:
                raise RuntimeError("HOOK_HISTORY_STORAGE_SCHEMA_UNSUPPORTED")
            row = conn.execute('SELECT * FROM hook_history_pages WHERE event_uuid=?', (event_uuid,)).fetchone()
            if row is None:
                return None
            self._verify(row)
            return dict(row)
