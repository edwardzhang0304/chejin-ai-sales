"""Durable delivery of authorized native observations, never physical sends."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from . import storage
from .hook_projection import PROJECTION_VERSION
from .shared_rules import hook_contract_rules as rules

OUTBOX_FIELDS = ("event_uuid", "receive_seq", "worker_id", "client_instance_id", "self_wxid", "account_epoch",
    "binding_id", "binding_revision", "payload_digest", "payload_json", "status", "attempt_count", "lease_id",
    "lease_expires_at", "next_attempt_at", "last_error", "receipt_json", "created_at", "updated_at")


@dataclass(frozen=True)
class HookDeliveryScope:
    worker_id: str
    client_instance_id: str
    self_wxid: str
    account_epoch: str
    binding_id: str
    binding_revision: int
    peer_wxid: str

    def __post_init__(self):
        if (any(not rules.identifier(v) for v in (self.worker_id, self.account_epoch, self.binding_id))
                or not rules.bounded(self.client_instance_id, 128) or not rules.wxid(self.self_wxid)
                or not rules.wxid(self.peer_wxid) or self.self_wxid == self.peer_wxid
                or type(self.binding_revision) is not int or self.binding_revision <= 0):
            raise ValueError("HOOK_DELIVERY_SCOPE_INVALID")


@dataclass(frozen=True)
class HookDeliveryClaim:
    event_uuid: str
    lease_id: str
    payload_digest: str
    payload_json: str
    attempt_count: int

    @property
    def envelope(self):
        payload = rules.validate_envelope(rules.loads(self.payload_json))
        if payload['event_uuid'] != self.event_uuid or rules.digest(payload) != self.payload_digest:
            raise RuntimeError("HOOK_OUTBOX_PAYLOAD_CHANGED")
        return payload


def _instant(value):
    if not rules.aware(value):
        raise ValueError("HOOK_OUTBOX_TIME_INVALID")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class HookDeliveryOutbox:
    def __init__(self, *, max_storage_bytes: int):
        if type(max_storage_bytes) is not int or max_storage_bytes <= 0:
            raise ValueError("HOOK_OUTBOX_LIMIT_INVALID")
        self.max_storage_bytes = max_storage_bytes

    def _schema(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS hook_delivery_outbox (
            event_uuid TEXT PRIMARY KEY, receive_seq INTEGER NOT NULL REFERENCES hook_raw_inbox(receive_seq),
            worker_id TEXT NOT NULL, client_instance_id TEXT NOT NULL, self_wxid TEXT NOT NULL, account_epoch TEXT NOT NULL,
            binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL, payload_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, lease_id TEXT, lease_expires_at TEXT,
            next_attempt_at TEXT NOT NULL, last_error TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        if tuple(r['name'] for r in conn.execute('PRAGMA table_info(hook_delivery_outbox)')) != OUTBOX_FIELDS:
            raise RuntimeError("HOOK_OUTBOX_SCHEMA_UNSUPPORTED")

    def enqueue_projection(self, receive_seq: int, scope: HookDeliveryScope, *, now: str, allowed_native_ids=None):
        return self.enqueue_projections([receive_seq], scope, now=now,
                                        allowed_native_ids=allowed_native_ids)[0]

    def enqueue_projections(self, receive_seqs, scope: HookDeliveryScope, *, now: str, allowed_native_ids=None):
        """Validate the same immutable evidence with bounded shared transactions.

        No network calls or delivery acknowledgements occur under this lock.
        Already committed chunks are safe to revisit after interruption; the
        original delivery id and payload digest remain the idempotency boundary.
        """
        instant = _instant(now).isoformat()
        sequences = list(receive_seqs)
        if any(type(seq) is not int or seq <= 0 for seq in sequences):
            raise ValueError("HOOK_OUTBOX_SEQUENCE_INVALID")
        result = []
        for offset in range(0, len(sequences), 128):
            with storage.db_connection() as conn:
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    self._schema(conn)
                    for seq in sequences[offset:offset+128]:
                        result.append(self._enqueue_projection(conn, seq, scope, instant, allowed_native_ids))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        return result

    def _enqueue_projection(self, conn, receive_seq, scope, instant, allowed_native_ids):
        row = conn.execute("""SELECT p.*, i.event_uuid AS raw_uuid, i.raw_digest, i.self_wxid, i.account_epoch,
            i.process_generation, i.received_at FROM hook_observation_projection p
            JOIN hook_raw_inbox i ON i.receive_seq=p.receive_seq WHERE p.receive_seq=?""", (receive_seq,)).fetchone()
        if row is None:
            raise RuntimeError("HOOK_OUTBOX_PROJECTION_MISSING")
        if row['self_wxid'] != scope.self_wxid or row['account_epoch'] != scope.account_epoch:
            raise ValueError("HOOK_OUTBOX_ACCOUNT_MISMATCH")
        encoded = row['projection_json']
        if hashlib.sha256(encoded.encode('utf-8')).hexdigest() != row['projection_digest'] or row['contract_version'] != PROJECTION_VERSION:
            raise RuntimeError("HOOK_OUTBOX_PROJECTION_CHANGED")
        projection = rules.loads(encoded)
        if (projection['event_uuid'] != row['raw_uuid'] or projection['receive_seq'] != receive_seq
                or projection['raw_evidence_digest'] != row['raw_digest'] or projection['observed_at'] != row['received_at']):
            raise RuntimeError("HOOK_OUTBOX_PROJECTION_CHANGED")
        messages = []
        for observation in projection['observations']:
            if observation['disposition'] != 'message_candidate' or observation['peer_wxid'] != scope.peer_wxid:
                continue
            if allowed_native_ids is not None and observation['native_msg_id'] not in allowed_native_ids:
                continue
            if (observation['self_wxid'] != scope.self_wxid or observation['account_epoch'] != scope.account_epoch
                    or observation['process_generation'] != row['process_generation']
                    or observation['event_uuid'] != row['raw_uuid'] or observation['receive_seq'] != receive_seq):
                raise RuntimeError("HOOK_OUTBOX_PROJECTION_CHANGED")
            message = {key: observation[key] for key in rules.MESSAGE_FIELDS if key != 'source_time_raw'}
            message['source_time_raw'] = observation['provider_time']
            message['ordering_fields'] = dict(observation['ordering_fields'])
            messages.append(message)
        if projection['status'] != 'projected' or not messages:
            return None
        envelope = {"transport": "hook", "schema_version": rules.SCHEMA_VERSION,
            **{key: getattr(scope, key) for key in scope.__dataclass_fields__},
            "process_generation": row['process_generation'], "source_event_uuid": row['raw_uuid'],
            "receive_seq": str(receive_seq), "raw_evidence_digest": row['raw_digest'],
            "observed_at": row['received_at'], "messages": messages,
            "event_uuid": rules.delivery_id(row['raw_uuid'], scope.worker_id, scope.client_instance_id,
                scope.account_epoch, scope.binding_id, scope.binding_revision)}
        rules.validate_envelope(envelope)
        payload_json, payload_digest = rules.canonical(envelope), rules.digest(envelope)
        old = conn.execute('SELECT payload_digest FROM hook_delivery_outbox WHERE event_uuid=?', (envelope['event_uuid'],)).fetchone()
        if old is not None:
            if old['payload_digest'] != payload_digest:
                raise RuntimeError("HOOK_OUTBOX_ID_CONFLICT")
            return envelope['event_uuid']
        used = conn.execute('SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))+COALESCE(length(CAST(receipt_json AS BLOB)),0)),0) FROM hook_delivery_outbox').fetchone()[0]
        if used + len(payload_json.encode('utf-8')) > self.max_storage_bytes:
            raise RuntimeError("HOOK_OUTBOX_CAPACITY_EXCEEDED")
        conn.execute("""INSERT INTO hook_delivery_outbox (event_uuid,receive_seq,worker_id,client_instance_id,
            self_wxid,account_epoch,binding_id,binding_revision,payload_digest,payload_json,status,next_attempt_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""", (envelope['event_uuid'], receive_seq,
            scope.worker_id, scope.client_instance_id, scope.self_wxid, scope.account_epoch, scope.binding_id,
            scope.binding_revision, payload_digest, payload_json, instant, instant, instant))
        return envelope['event_uuid']

    def claim_next(self, scope: HookDeliveryScope, *, now: str, lease_seconds: int):
        instant = _instant(now)
        if type(lease_seconds) is not int or not 0 < lease_seconds <= 300:
            raise ValueError("HOOK_OUTBOX_LEASE_INVALID")
        with storage.db_connection() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_delivery_outbox'").fetchone() is None:
                    conn.rollback()
                    return None
                self._schema(conn)
                row = conn.execute("""SELECT * FROM hook_delivery_outbox WHERE worker_id=? AND client_instance_id=?
                    AND self_wxid=? AND account_epoch=? AND binding_id=? AND binding_revision=?
                    AND ((status IN ('pending','retry_wait') AND next_attempt_at<=?)
                        OR (status='attempting' AND lease_expires_at<=?)) ORDER BY receive_seq,event_uuid LIMIT 1""",
                    (scope.worker_id, scope.client_instance_id, scope.self_wxid, scope.account_epoch,
                     scope.binding_id, scope.binding_revision, instant.isoformat(), instant.isoformat())).fetchone()
                if row is None:
                    conn.commit()
                    return None
                lease = str(uuid.uuid4())
                claim = HookDeliveryClaim(row['event_uuid'], lease, row['payload_digest'], row['payload_json'], row['attempt_count']+1)
                envelope = claim.envelope
                if any(envelope[key] != getattr(scope, key) for key in scope.__dataclass_fields__):
                    raise RuntimeError("HOOK_OUTBOX_BINDING_CHANGED")
                conn.execute("UPDATE hook_delivery_outbox SET status='attempting',lease_id=?,lease_expires_at=?,attempt_count=?,updated_at=? WHERE event_uuid=?",
                    (lease, (instant + timedelta(seconds=lease_seconds)).isoformat(), claim.attempt_count, instant.isoformat(), claim.event_uuid))
                conn.commit()
                return claim
            except Exception:
                conn.rollback()
                raise

    def mark_stored(self, claim: HookDeliveryClaim, receipt: dict, *, now: str):
        rules.validate_receipt(claim.envelope, receipt)
        encoded = rules.canonical(receipt)
        instant = _instant(now).isoformat()
        with storage.db_connection() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                self._schema(conn)
                row = conn.execute('SELECT * FROM hook_delivery_outbox WHERE event_uuid=?', (claim.event_uuid,)).fetchone()
                if row is None or row['payload_digest'] != claim.payload_digest:
                    raise RuntimeError("HOOK_OUTBOX_CLAIM_MISMATCH")
                if row['status'] == 'stored' and row['receipt_json'] == encoded:
                    conn.commit()
                    return True
                if row['status'] != 'attempting' or row['lease_id'] != claim.lease_id:
                    conn.rollback()
                    return False
                used = conn.execute('SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))+COALESCE(length(CAST(receipt_json AS BLOB)),0)),0) FROM hook_delivery_outbox').fetchone()[0]
                if used + len(encoded.encode('utf-8')) > self.max_storage_bytes:
                    raise RuntimeError("HOOK_OUTBOX_CAPACITY_EXCEEDED")
                conn.execute("UPDATE hook_delivery_outbox SET status='stored',receipt_json=?,lease_id=NULL,lease_expires_at=NULL,last_error=NULL,updated_at=? WHERE event_uuid=?",
                    (encoded, instant, claim.event_uuid))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def defer(self, claim: HookDeliveryClaim, *, now: str, next_attempt_at: str, error_code: str, blocked=False):
        instant, due = _instant(now), _instant(next_attempt_at)
        if due < instant or not rules.bounded(error_code, 64) or not error_code.replace('_','').isalnum() or type(blocked) is not bool:
            raise ValueError("HOOK_OUTBOX_RETRY_INVALID")
        with storage.db_connection() as conn:
            result = conn.execute("""UPDATE hook_delivery_outbox SET status=?,lease_id=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error=?,updated_at=? WHERE event_uuid=? AND payload_digest=? AND status='attempting' AND lease_id=?""",
                ('blocked' if blocked else 'retry_wait', due.isoformat(), error_code, instant.isoformat(), claim.event_uuid, claim.payload_digest, claim.lease_id))
            conn.commit()
            return result.rowcount == 1
