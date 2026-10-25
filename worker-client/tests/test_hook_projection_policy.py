"""Production entry constructors and actual 128 MiB SQLite boundary, no filler SQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
import uuid
import zipfile

import pytest
from chejin_worker_client import storage, hook_factory
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_inbox import NativeInbox, HookReceiveScope
from chejin_worker_client.hook_projection import create_projection_store, NativeProjectionStore, PROJECTION_MAX_BYTES, PROJECTION_VERSION
from chejin_worker_client.hook_facts import callback_observations
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_outbox import HookDeliveryScope, HookDeliveryOutbox
from chejin_worker_client.hook_retention import retire_inbox
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.diagnostic_bundle import export_bundle
from test_hook_projection import local_db

SCOPE = HookReceiveScope('wxid_policy_sales', '00000000-0000-4000-8000-000000000071', 'policy-process', 30251)


def callback(mid, text, peer='wxid_policy_A'):
    return json.dumps({'ServerPort': '30251', 'selfwxid': SCOPE.self_wxid, 'sendorrecv': '2', 'msgnumber': '1',
        'msglist': [{'msgsvrid': str(mid), 'msgtype': '1', 'fromtype': '1', 'fromid': peer,
        'toid': SCOPE.self_wxid, 'msg': text, 'time': '2026-10-01 10:00:00'}]}).encode()


def production_entries(monkeypatch, root):
    # External service constructors are inert; both real construction paths
    # must choose the shared policy themselves. No projector is substituted.
    monkeypatch.setattr(hook_factory, '_setting', lambda _: {'mode': 'hook'})
    monkeypatch.setattr(hook_factory, 'load_hook_deployment', lambda: NS(revision='synthetic'))
    for name in ('HookGateway', 'HookIdentityClient', 'HookConnectionManager', 'HookFormalClient', 'HookBusinessDispatcher'):
        monkeypatch.setattr(hook_factory, name, lambda *a, **kw: NS())
    services = hook_factory.create_hook_services(NS(base_url='http://127.0.0.1:1'))
    client = NS(base_url='http://127.0.0.1:1', timeout_seconds=5, max_response_bytes=2097152)
    runtime = HookFormalRuntime(client, HookWorkerCredentials(str(uuid.uuid4()), 'synthetic', 'instance'), NS(),
        NS(clock=NS(), process=NS(), registration=NS(scope=SCOPE)), root / 'transactions/hook')
    return services.projection, runtime.projection


def usage():
    with storage.db_connection() as db:
        return db.execute('SELECT coalesce(sum(length(cast(projection_json AS BLOB))),0) FROM hook_observation_projection').fetchone()[0]


@pytest.mark.parametrize('order', ['intake_first', 'business_first', 'parallel'])
def test_production_entries_share_boundary_restart_and_legal_retirement(local_db, monkeypatch, order):
    intake, business = production_entries(monkeypatch, local_db)
    assert intake.max_projection_bytes == business.max_projection_bytes == PROJECTION_MAX_BYTES == 128 * 1024 * 1024
    inbox = NativeInbox(max_body_bytes=2*1024*1024, max_inbox_bytes=128*1024*1024)
    # Fixed producer clock gives reproducible byte boundaries and legal age.
    monkeypatch.setattr('chejin_worker_client.hook_inbox.utc_now_iso', lambda: '2026-10-01T02:00:00+00:00')
    first = inbox.persist(callback(100001, 'synthetic first message'*20), SCOPE)
    intake.project_next(SCOPE)
    n = 100002
    legacy_conflict_reproduced = False
    while PROJECTION_MAX_BYTES - usage() > 2*1024*1024:
        inbox.persist(callback(n, 'x'*(1024*1024)), SCOPE)
        if not legacy_conflict_reproduced and usage() > 32*1024*1024:
            with pytest.raises(RuntimeError, match='HOOK_PROJECTION_CAPACITY_EXCEEDED'):
                NativeProjectionStore(max_projection_bytes=32*1024*1024, max_entries=200).project_next(SCOPE)
            legacy_conflict_reproduced = True
        (intake if n % 2 else business).project_next(SCOPE)
        n += 1
    # Build the exact final callback using the official fact producer's size.
    # Only official inbox/projector functions write the database.
    raw = callback(n, 'x')
    with storage.db_connection() as db:
        seq = db.execute('SELECT max(receive_seq)+1 FROM hook_raw_inbox').fetchone()[0]
    payload = {'contract_version': PROJECTION_VERSION, 'event_uuid': str(uuid.uuid4()), 'receive_seq': seq,
        'raw_evidence_digest': 'a'*64, 'observed_at': '2026-10-01T02:00:00+00:00', 'status': 'projected',
        'reason_code': None, 'observations': []}
    payload['observations'] = [asdict(o) for o in callback_observations(raw, SCOPE,
        event_uuid=payload['event_uuid'], receive_seq=seq, max_entries=200)]
    overhead = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()) - 1
    remaining = PROJECTION_MAX_BYTES - usage()
    assert 0 < remaining - overhead < 2*1024*1024
    inbox.persist(callback(n, 'x'*(remaining-overhead)), SCOPE)
    def run(pair):
        if order == 'parallel':
            with ThreadPoolExecutor(2) as pool:
                return list(pool.map(lambda p: p.project_next(SCOPE), pair))
        pair = pair if order == 'intake_first' else pair[::-1]
        return [p.project_next(SCOPE) for p in pair]
    assert sum(v is not None for v in run([intake, business])) == 1
    assert legacy_conflict_reproduced
    assert usage() == PROJECTION_MAX_BYTES
    pending = inbox.persist(callback(n+1, 'pending after exact boundary'), SCOPE)
    for entry in (intake, business, create_projection_store()):
        with pytest.raises(RuntimeError, match='HOOK_PROJECTION_CAPACITY_EXCEEDED'):
            entry.project_next(SCOPE)
    storage.append_log('ERROR', 'hook_projection_failed', 'HOOK_PROJECTION_CAPACITY_EXCEEDED',
        metadata={'receive_seq': pending['receive_seq'], 'reason_code': 'HOOK_PROJECTION_CAPACITY_EXCEEDED'})
    with storage.db_connection() as db:
        original = dict(db.execute('SELECT * FROM hook_raw_inbox WHERE receive_seq=?', (pending['receive_seq'],)).fetchone())
    path = export_bundle(local_db, local_db/'capacity.zip')
    with zipfile.ZipFile(path) as z:
        logs = json.loads(z.read('logs/latest_logs.json'))
        assert any('HOOK_PROJECTION_CAPACITY_EXCEEDED' in str(v) for v in logs)
        assert any(r['receive_seq'] == pending['receive_seq'] for r in json.loads(z.read('state/hook_raw_inbox.json'))['rows'])
    # Only one acknowledged, old small package is legally eligible to retire.
    target = HookDeliveryScope(str(uuid.uuid4()), 'instance', SCOPE.self_wxid, SCOPE.account_epoch,
        str(uuid.uuid4()), 1, 'wxid_policy_A')
    outbox = HookDeliveryOutbox(max_storage_bytes=32*1024*1024)
    outbox.enqueue_projection(first['receive_seq'], target, now='2026-10-01T02:00:00+00:00')
    claim = outbox.claim_next(target, now='2026-10-01T02:00:00+00:00', lease_seconds=30)
    assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now='2026-10-01T02:00:00+00:00')
    retired = retire_inbox(local_db/'transactions/hook', retention_days=7, now=datetime(2026,10,25,tzinfo=timezone.utc), limit=1000)
    assert retired['retired_rows'] == 1 and retired['pending_records_deleted'] is False
    assert create_projection_store().project_next(SCOPE)['receive_seq'] == pending['receive_seq']
    with storage.db_connection() as db:
        assert dict(db.execute('SELECT * FROM hook_raw_inbox WHERE receive_seq=?',(pending['receive_seq'],)).fetchone()) == original
    assert create_projection_store().project_next(SCOPE) is None
