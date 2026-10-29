"""Real localhost HTTP and independent PostgreSQL transactions; synthetic data only.

Run only with FEIYU_PG_TEST=1 and a loopback database named feiyu_verification.
Admin authentication is substituted; Feiyu authentication and all business writes
are real. No Worker/WeChat/provider action is executed.
"""
import copy
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import Request
from sqlalchemy import select, text

from app.core.database import Base, SessionLocal, engine
from app.core.auth import require_admin_auth
from app.main import create_app
from app.models.feiyu import FeiyuLeadReceipt
from app.models.lead import Lead, LeadNote, LeadAssignment, LeadDuplicateEvent, AssignmentRoundRobinState
from app.models.task import Task
from app.services import feiyu_lead_service as service
from app.services.task_service import task_to_worker_execution_detail

URL = '/api/integrations/feiyu/leads'
SECRET = 'synthetic-feiyu-http-secret'
HEADERS = {'X-Feiyu-Forward-Secret': SECRET, 'X-Feiyu-Account-ID': 'account-a'}
pytestmark = pytest.mark.skipif(os.environ.get('FEIYU_PG_TEST') != '1', reason='explicit isolated PostgreSQL opt-in required')


def wait_until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check(): return
        time.sleep(.02)
    raise AssertionError('condition not reached before deadline')


class Server:
    def __init__(self):
        self.start()

    def start(self):
        app = create_app()
        def admin(request: Request):
            request.state.auth_actor = {'operator_id': '00000000-0000-0000-0000-000000000001',
                'operator_name': 'Synthetic PG acceptance', 'actor_type': 'admin_account', 'session_id': 'synthetic'}
        app.dependency_overrides[require_admin_auth] = admin
        self.sock = socket.socket(); self.sock.bind(('127.0.0.1', 0)); self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='critical'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.sock]}, daemon=True)
        self.thread.start(); wait_until(lambda: self.server.started)
        self.base = f'http://127.0.0.1:{self.port}'

    def stop(self):
        self.server.should_exit = True; self.thread.join(10)
        assert not self.thread.is_alive()
        self.sock.close()

    def post(self, payload, path=URL, headers=None):
        return httpx.post(self.base + path, json=payload, headers=HEADERS if headers is None else headers, timeout=20)


@pytest.fixture
def api(monkeypatch):
    assert engine.dialect.name == 'postgresql'
    assert engine.url.host in {'127.0.0.1', 'localhost'} and engine.url.database == 'feiyu_verification'
    from app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, 'feiyu_forward_secret', SECRET)
    monkeypatch.setattr(settings, 'feiyu_account_ids', 'account-a,account-b')
    with engine.connect() as c: assert c.execute(text('SHOW transaction_isolation')).scalar() == 'read committed'
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS wechat_ai_customer_service"))
    Base.metadata.create_all(engine)
    server = Server()
    for i in (1, 2):
        r = server.post({'sales_name': f'Synthetic {i}', 'phone': f'1390000000{i}', 'enabled': True, 'sort_order': i}, '/api/sales', {})
        assert r.status_code == 200, r.text
    yield server
    server.stop(); engine.dispose()


def payload(clue='clue-a', phone='13800000001'):
    return {'customer_name': 'Synthetic customer', 'phones': [phone], 'remark': 'Synthetic first note',
            'custom_fields': {'feiyu': {'id': clue}}}


def snapshot():
    with SessionLocal() as db:
        leads = list(db.scalars(select(Lead).order_by(Lead.id)))
        receipts = list(db.scalars(select(FeiyuLeadReceipt).order_by(FeiyuLeadReceipt.clue_id)))
        tasks = list(db.scalars(select(Task)))
        assignments = list(db.scalars(select(LeadAssignment)))
        cursor = db.get(AssignmentRoundRobinState, 1)
        return {'leads': [{'id': x.id, 'sales_id': x.sales_id, 'source': x.source_type, 'duplicate_count': x.duplicate_count} for x in leads],
            'notes': [{'id': x.id, 'lead_id': x.lead_id, 'content': x.content} for x in db.scalars(select(LeadNote).order_by(LeadNote.id))],
            'receipts': [{'id': x.id, 'account': x.account_id, 'clue': x.clue_id, 'lead_id': x.lead_id, 'status': x.response_status, 'body': x.response_body} for x in receipts],
            'tasks': [{'id': x.id, 'lead_id': x.lead_id, 'type': x.task_type, 'output': task_to_worker_execution_detail(x)} for x in tasks],
            'assignments': [{'id': x.id, 'lead_id': x.lead_id, 'sales_id': x.to_sales_id, 'cursor_after': x.round_robin_cursor_after} for x in assignments],
            'duplicates': sorted(x.id for x in db.scalars(select(LeadDuplicateEvent))),
            'cursor': cursor.current_sales_id if cursor else None}


def evidence(name, **data):
    root = os.environ.get('FEIYU_EVIDENCE_DIR')
    if root:
        p = Path(root); p.mkdir(parents=True, exist_ok=True)
        (p / (name+'.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def race(api, requests):
    barrier = threading.Barrier(len(requests))
    def run(spec):
        barrier.wait(10)
        p, path = spec
        return api.post(p, path, HEADERS if path == URL else {})
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(run, requests))


def assert_single_business(state, repeats=0):
    assert len(state['leads']) == len(state['tasks']) == len(state['assignments']) == 1
    assert state['leads'][0]['duplicate_count'] == len(state['duplicates']) == repeats
    assert len(state['notes']) == repeats + 1
    lead = state['leads'][0]
    assert lead['sales_id'] == state['assignments'][0]['sales_id']
    assert state['cursor'] == state['assignments'][0]['cursor_after']
    assert state['cursor'] != lead['sales_id']  # two sales: cursor names the next owner
    task = state['tasks'][0]
    assert task['lead_id'] == lead['id'] and task['type'] == 'add_friend'
    assert task['output']['remark_code'].startswith('CJ')


def test_ten_simultaneous_exact_deliveries_and_persistent_replay(api):
    p = payload(); before = snapshot()
    responses = race(api, [(p, URL)] * 10)
    assert all(r.status_code == 200 and r.content == responses[0].content for r in responses)
    state = snapshot(); assert_single_business(state); assert len(state['receipts']) == 1
    api.stop(); engine.dispose(); api.start()
    replay = api.post(p)
    assert replay.content == responses[0].content and snapshot() == state
    evidence('exact-ten-and-restart', request=p, before=before, responses=[r.json() for r in responses], after=state)


@pytest.mark.parametrize('manual_first', [False, True])
def test_phone_race_distinct_clues_and_manual(api, manual_first):
    p = payload('first'); q = payload('second')
    path = '/api/leads' if manual_first else URL
    responses = race(api, [(p, path), (q, URL)])
    assert sorted(r.status_code for r in responses) == [200, 409]
    state = snapshot(); assert_single_business(state, repeats=1)
    assert len(state['receipts']) == (1 if manual_first else 2)
    winner = 0 if responses[0].status_code == 200 else 1
    assert state['leads'][0]['source'] == ('manual' if manual_first and winner == 0 else 'feiyu')
    assert api.post(q).content == responses[1].content
    if not manual_first: assert api.post(p).content == responses[0].content
    assert snapshot() == state
    evidence('phone-race-'+str(manual_first), responses=[r.json() for r in responses], after=state)


def test_invalid_and_changed_requests_have_no_side_effect(api):
    p = payload(); first = api.post(p); assert first.status_code == 200
    before = snapshot()
    changed = copy.deepcopy(p); changed['remark'] = 'Changed second submission'
    missing = copy.deepcopy(p); missing['custom_fields'] = {}
    forged = copy.deepcopy(p); forged['custom_fields']['remark_code'] = 'CJFORGED'
    cases = [(payload(phone='13800000002'), HEADERS, 409), (changed, HEADERS, 409),
             (missing, HEADERS, 400), (forged, HEADERS, 400), (p, {**HEADERS, 'X-Feiyu-Account-ID': 'unauthorized'}, 403)]
    responses = [api.post(q, headers=h) for q,h,_ in cases]
    assert [r.status_code for r in responses] == [code for _,_,code in cases]
    assert responses[1].json()['data']['update_applied'] is False
    assert snapshot() == before and api.post(p).content == first.content
    evidence('rejections', before=before, responses=[r.json() for r in responses], after=snapshot())


@pytest.mark.parametrize("existing_customer", [False, True])
def test_before_commit_failure_rolls_back_business_receipt_and_cursor(api, monkeypatch, existing_customer):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    original = service._save_receipt
    def save(db, **kwargs):
        db.info['synthetic_fail_commit'] = True
        return original(db, **kwargs)
    def fail(db):
        if db.info.pop('synthetic_fail_commit', False): raise RuntimeError('synthetic commit failure')
    if existing_customer:
        assert api.post(payload('manual'), '/api/leads', {}).status_code == 200
    before = snapshot(); event.listen(Session, 'before_commit', fail)
    try:
        with monkeypatch.context() as m:
            m.setattr(service, '_save_receipt', save)
            failed = api.post(payload())
            assert failed.status_code == 500
        assert snapshot() == before
    finally: event.remove(Session, 'before_commit', fail)
    good = api.post(payload()); assert good.status_code == (409 if existing_customer else 200)
    after = snapshot(); assert_single_business(after, repeats=int(existing_customer)); assert len(after['receipts']) == 1
    evidence('commit-rollback-'+str(existing_customer), before=before, failed_status=failed.status_code, retry=good.json(), after=after)


def test_waiting_request_completes_after_lock_owner_rolls_back(api, monkeypatch):
    entered = threading.Event(); release = threading.Event(); original = service._save_receipt
    first = [True]
    def save(db, **kwargs):
        if first[0]:
            first[0] = False; entered.set()
            assert release.wait(10)
            raise RuntimeError('synthetic rollback while holding locks')
        return original(db, **kwargs)
    def waiting():
        with engine.connect() as c:
            return c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'")).scalar() > 0
    with monkeypatch.context() as m, ThreadPoolExecutor(max_workers=2) as pool:
        m.setattr(service, '_save_receipt', save)
        a = pool.submit(api.post, payload()); assert entered.wait(10)
        b = pool.submit(api.post, payload())
        try: wait_until(waiting)
        finally: release.set()
        r1, r2 = a.result(15), b.result(15)
    assert [r1.status_code,r2.status_code] == [500,200]
    after = snapshot(); assert_single_business(after); assert len(after['receipts']) == 1
    assert api.post(payload()).content == r2.content
    evidence('lock-rollback', lock_wait_observed=True, statuses=[r1.status_code,r2.status_code], after=after)


def test_committed_request_with_closed_http_connection_replays_once(api):
    p = payload(); body = json.dumps(p).encode()
    with socket.create_connection(('127.0.0.1', api.port), timeout=5) as s:
        lines = [f'POST {URL} HTTP/1.1',f'Host: 127.0.0.1:{api.port}','Content-Type: application/json',
                 f'Content-Length: {len(body)}',*[f'{k}: {v}' for k,v in HEADERS.items()],'Connection: close','','']
        s.sendall('\r\n'.join(lines).encode()+body)
        s.shutdown(socket.SHUT_WR)  # deliberately never read the acknowledgement
    wait_until(lambda: len(snapshot()['receipts']) == 1)
    before = snapshot(); r = api.post(p)
    assert r.status_code == 200 and r.json() == before['receipts'][0]['body']
    assert snapshot() == before; assert_single_business(before)
    evidence('lost-http-response', after=before, replay=r.json())


def test_negative_control_missing_identity_lock_breaks_concurrent_ack(api, monkeypatch):
    """Prove the HTTP concurrency oracle detects a removed idempotency lock."""
    barrier = threading.Barrier(2)
    original_create = service.lead_service.create_lead

    def gated_create(*args, **kwargs):
        # Both requests have read no receipt before either creates the lead.
        barrier.wait(10)
        return original_create(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(service, '_lock_identity', lambda *args: None)
        patch.setattr(service.lead_service, 'create_lead', gated_create)
        responses = race(api, [(payload(), URL)] * 2)
    assert sorted(r.status_code for r in responses) == [200, 500]
    after = snapshot()
    assert_single_business(after)
    assert len(after['receipts']) == 1
    assert api.post(payload()).status_code == 200
    evidence('negative-control-no-identity-lock', statuses=[r.status_code for r in responses], after=after)
