"""Released binding history through HTTP, then same-task/Flow recovery. No UI actions."""
import threading
import time

import pytest
from sqlalchemy import select, text

from app.core.database import SessionLocal, engine
from app.models.task import Task
from app.models.worker import Worker
from app.schemas.lead import LeadCreate
from app.schemas.sales import SalesWorkerBindRequest
from app.services import lead_service, sales_service, task_service
import ast
import textwrap
from pathlib import Path


def old_function(path, name, namespace):
    # Exact functions extracted from 9c4871d, so CI needs no old Git checkout.
    source = (Path(__file__).parent / 'fixtures/sales_binding_v0979.py').read_text()
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = dict(namespace)
    exec(textwrap.dedent(ast.get_source_segment(source, node)), scope)
    return scope[name]

from test_rule_intersections import (
    http, pytestmark, data, ownership_case, headers, claim, record,
)


@pytest.mark.parametrize('first', ['rebind', 'create'])
def test_new_lead_creation_and_rebinding_share_current_owner(http, first):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    payload = {'customer_name': '新增与换绑交叉', 'phones': ['13800998877']}
    result, errors = [], []
    with SessionLocal() as first_db:
        actor = task_service.SYSTEM_TASK_LEASE_ACTOR
        if first == 'rebind':
            sales_service.bind_worker(first_db, sales['id'], SalesWorkerBindRequest(worker_id=wb['id']), actor)
        else:
            lead_service.create_lead(first_db, LeadCreate(**payload), actor)

        def second():
            try:
                response = (http.post('/api/leads', json=payload) if first == 'rebind'
                            else http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
                result.append(response)
            except BaseException as exc:
                errors.append(repr(exc))

        thread = threading.Thread(target=second)
        thread.start()
        observed = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as check:
                    observed = bool(check.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")))
                if observed:
                    break
                time.sleep(.01)
            assert observed, 'second connection did not enter an actual lock wait'
            first_db.commit()
        finally:
            first_db.rollback()
            thread.join(12)
        assert not thread.is_alive() and not errors, errors
        assert len(result) == 1
        data(result[0])
    saved = data(http.get('/api/tasks'))['items']
    record('review-create-rebind-' + first, {'lock_wait_observed': observed, 'response': result[0].json(), 'tasks': saved})
    assert len(saved) == 2
    assert all(t['sales_id'] == sales['id'] and t['worker_id'] == wb['id'] and t['status'] == 'pending' for t in saved)
    assert api.pull_task(ba)[1] is None


def test_invalid_target_rolls_back_entire_rebinding(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http, 2)
    before = data(http.get('/api/tasks'))['items']
    rejected = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': '00000000-0000-0000-0000-000000000099'})
    assert rejected.status_code == 404
    after = data(http.get('/api/tasks'))['items']
    assert before == after
    assert data(http.get(f"/api/sales/{sales['id']}"))['worker_id'] == wa['id']
    assert claim(http, tasks[0], ba).status_code == 200


@pytest.mark.parametrize('legacy_change', ['clear_then_bind', 'replace_then_pull'])
def test_legacy_registered_unclaimed_flow_must_not_be_migrated(http, monkeypatch, legacy_change):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    api.start_inflight_flow(ba, flow_id=task['id'], flow_kind='task')
    # Manufacture the historical hole only through the exact released bind
    # implementation and its HTTP route. No task/result/Flow is hand-written.
    old_bind = old_function('backend/app/services/sales_service.py', 'bind_worker', vars(sales_service))
    old_unblock = old_function('backend/app/services/task_service.py', 'unblock_sales_worker_tasks', vars(task_service))
    with monkeypatch.context() as legacy:
        legacy.setattr(sales_service, 'bind_worker', old_bind)
        legacy.setattr(task_service, 'unblock_sales_worker_tasks', old_unblock, raising=False)
        if legacy_change == 'clear_then_bind':
            data(http.delete(f"/api/sales/{sales['id']}/worker-binding"))
        else:
            data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
    original = data(http.get(f"/api/tasks/{task['id']}"))
    if legacy_change == 'clear_then_bind':
        current = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']})
    else:
        current = http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb))
    new_claim = claim(http, task, bb)
    with SessionLocal() as db:
        row = db.get(Task, task['id'])
        old_flow = dict(db.get(Worker, wa['id']).inflight_flow_state or {})
        persisted = {'task_id': row.id, 'task_worker': row.worker_id, 'task_status': row.status, 'old_flow': old_flow}
    finish = http.post(f"/api/workers/{wa['id']}/inflight-flow/finish", headers=headers(ba, task['id']),
                       json={'flow_id': task['id'], 'terminal_kind': 'task_terminal', 'client_instance_id': ba.client_instance_id})
    record('review-legacy-flow-' + legacy_change, {'original_task': original, 'new_request_status': current.status_code,
           'new_request': current.json(), 'new_claim_status': new_claim.status_code, 'new_claim': new_claim.json(),
           'persisted': persisted, 'old_finish_status': finish.status_code, 'old_finish': finish.json()})
    assert old_flow.get('flow_id') == task['id'], persisted
    assert new_claim.status_code == 409, 'Current candidate granted a new executor while the released original Flow remains active'


def test_original_success_receipt_replays_before_flow_finish_and_completed_task_does_not_move(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    api.start_inflight_flow(ba, flow_id=task['id'], flow_kind='task')
    claimed = data(claim(http, task, ba, task['id']))
    h = headers(ba, task['id'])
    h['X-Task-Lease-Fencing-Token'] = str(claimed['lease_fencing_token'])
    # The official receipt path uses settlement_only while the original Flow
    # remains registered. A normal invite-sent after closure is not a replay.
    completed = data(http.post(f"/api/tasks/{task['id']}/invite-sent", headers=h,
                               json={'settlement_only': True, 'remark': None}))
    saved = api.settle_task_success(ba, completed['success_receipt']).raw
    assert all(saved[k] == completed[k] for k in ['id','worker_id','sales_id','completed_at','result_code'])
    api.finish_inflight_flow(ba, flow_id=task['id'], terminal_kind='task_terminal')
    data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
    saved = data(http.get(f"/api/tasks/{task['id']}"))
    assert all(saved[k] == completed[k] for k in ['id','worker_id','sales_id','completed_at','result_code'])
    assert api.pull_task(bb)[1] is None
    record('review-original-receipt-and-completed-rebind', {'receipt': completed['success_receipt'],
           'completed': completed, 'after_rebind': saved, 'new_worker_has_no_task': True})


def legacy_rebind(http, monkeypatch, sales_id, new_id):
    old_bind = old_function('sales_service.py', 'bind_worker', vars(sales_service))
    old_unblock = old_function('task_service.py', 'unblock_sales_worker_tasks', vars(task_service))
    with monkeypatch.context() as legacy:
        legacy.setattr(sales_service, 'bind_worker', old_bind)
        legacy.setattr(task_service, 'unblock_sales_worker_tasks', old_unblock, raising=False)
        if new_id:
            data(http.post(f'/api/sales/{sales_id}/worker-binding', json={'worker_id':new_id}))
        else:
            data(http.delete(f'/api/sales/{sales_id}/worker-binding'))


@pytest.mark.parametrize('history', ['clear', 'replace'])
def test_no_flow_history_still_migrates_same_unclaimed_task(http, monkeypatch, history):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    original = tasks[0]
    legacy_rebind(http, monkeypatch, sales['id'], None if history == 'clear' else wb['id'])
    if history == 'clear':
        data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']}))
    fetched = data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))
    assert fetched['task']['id'] == original['id'] and fetched['task']['worker_id'] == wb['id']
    assert claim(http, original, bb).status_code == 200
    with SessionLocal() as db:
        assert not db.get(Worker, wa['id']).inflight_flow_state
    record('no-original-flow-'+history, fetched)


def test_same_binding_cannot_sneak_in_historical_migration(http, monkeypatch):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    api.start_inflight_flow(ba, flow_id=tasks[0]['id'], flow_kind='task')
    legacy_rebind(http, monkeypatch, sales['id'], wb['id'])
    result = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']})
    assert result.status_code == 409 and result.json()['code'] == 'SALES_WORKER_REBIND_UNSETTLED'
    assert data(http.get(f"/api/tasks/{tasks[0]['id']}"))['worker_id'] == wa['id']
    assert claim(http, tasks[0], bb).status_code == 409


@pytest.mark.parametrize('history', ['clear', 'replace'])
def test_explicit_cancellation_then_original_worker_recovers_same_sqlite_flow(http, monkeypatch, history):
    from chejin_worker_client import storage
    from test_rule_intersections import runner_for
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    owner, errors = runner_for(api, ba)
    assert owner._start_inflight_flow(ba, flow_id=task['id'], flow_kind='task')
    original_sqlite = str(storage.DB_FILE)
    legacy_rebind(http, monkeypatch, sales['id'], None if history == 'clear' else wb['id'])
    if history == 'clear':
        rejected = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']})
        assert rejected.status_code == 409
    else:
        assert data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))['task'] is None
    assert claim(http, task, bb).status_code == 409
    assert storage.load_runtime_control()['inflight_flow_id'] == task['id']
    assert data(http.get(f"/api/tasks/{task['id']}"))['status'] == 'pending'
    # Explicit existing operator cancellation, not a hidden side-effect of a
    # binding change. Keep the old task and audit; never make W2 finish it.
    cancelled = data(http.post(f"/api/tasks/{task['id']}/cancel", json={'reason':'测试中的明确取消：原 Flow 尚未 claim'}))
    assert cancelled['status'] == 'cancelled' and cancelled['worker_id'] == wa['id']
    # Use the same persisted Flow and SQLite with a fresh real Worker runtime.
    # No direct finish API call or writes to the Flow/result tables here.
    recovered, recovery_errors = runner_for(api, storage.load_binding())
    recovered.start(storage.load_binding())
    try:
        deadline = time.monotonic() + 8
        while storage.load_runtime_control().get('inflight_flow_id') and time.monotonic() < deadline:
            time.sleep(.05)
        assert str(storage.DB_FILE) == original_sqlite
        assert not storage.load_runtime_control().get('inflight_flow_id'), recovery_errors
        with SessionLocal() as db:
            assert not db.get(Worker, wa['id']).inflight_flow_state
            assert db.get(Task, task['id']).status == 'cancelled'
            assert db.get(Task, task['id']).worker_id == wa['id']
    finally:
        recovered.stop_for_update(timeout_seconds=10)
    finish_calls = [event for event in api.session.events if event['path'].endswith('/inflight-flow/finish')]
    assert len(finish_calls) == 1 and finish_calls[0]['http_status'] == 200
    assert not any(event['path'].endswith('/claim') for event in api.session.events)
    data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']}))
    saved = data(http.get(f"/api/tasks/{task['id']}"))
    assert saved['status'] == 'cancelled' and saved['worker_id'] == wa['id']
    record('original-worker-cancel-recovery-'+history, {'original_task':task['id'], 'sqlite':original_sqlite, 'finish_calls':finish_calls, 'task_after_binding':saved})


@pytest.mark.parametrize('first', ['register', 'rebind', 'cancel'])
def test_flow_registration_or_cancellation_serializes_with_rebinding(http, first):
    from app.schemas.worker import WorkerInflightFlowStartRequest
    from app.services import worker_service
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    results, errors = [], []
    with SessionLocal() as tx:
        actor = task_service.SYSTEM_TASK_LEASE_ACTOR
        if first == 'register':
            worker_service.start_inflight_flow(tx, tx.get(Worker, wa['id']), WorkerInflightFlowStartRequest(flow_id=task['id'], flow_kind='task'))
        elif first == 'cancel':
            task_service.cancel_task(tx, task['id'], '明确取消并发对照', actor)
        else:
            sales_service.bind_worker(tx, sales['id'], SalesWorkerBindRequest(worker_id=wb['id']), actor)
        def second():
            try:
                if first == 'rebind':
                    response = http.post(f"/api/workers/{wa['id']}/inflight-flow/start", headers=headers(ba), json={'flow_id':task['id'], 'flow_kind':'task'})
                else:
                    response = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']})
                results.append(response)
            except BaseException as exc:
                errors.append(repr(exc))
        thread = threading.Thread(target=second); thread.start()
        waited = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as check:
                    waited = bool(check.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")))
                if waited: break
                time.sleep(.01)
            assert waited
            tx.commit()
        finally:
            tx.rollback(); thread.join(12)
        assert not thread.is_alive() and not errors, errors
    assert len(results) == 1 and results[0].status_code == (200 if first == 'cancel' else 409), [r.text for r in results]
    with SessionLocal() as db:
        row = db.get(Task, task['id'])
        assert row.worker_id == (wb['id'] if first == 'rebind' else wa['id'])
        assert row.status == ('cancelled' if first == 'cancel' else 'pending')
        flow = dict(db.get(Worker, wa['id']).inflight_flow_state or {})
        assert bool(flow) == (first == 'register')
    record('flow-rebind-concurrency-'+first, {'observed_pg_lock_wait': waited, 'response':results[0].json(), 'task_owner':row.worker_id, 'task_status':row.status, 'old_flow':flow})
