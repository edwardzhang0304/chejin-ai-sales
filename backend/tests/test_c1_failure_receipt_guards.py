"""Real HTTP protects the persisted failure's original identity and terminality."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
import test_worker_failure_consistency as submitted
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.task import Task, TaskEvent
from app.models.audit import OperationLog
from app.models.worker import Worker
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils


@pytest.fixture
def pending_receipt(http_api, tmp_path):
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        db.add(_contact_model(rows[0]['lead_id'], ContactType.phone,
                             contact_utils.normalize_phone('13800005555'), True))
        db.add(Task(lead_id=rows[0]['lead_id'], worker_id=worker['id'], task_type='add_friend', status='pending'))
        db.commit()
    request = {'base_url': http_api.get('/healthz').url.removesuffix('/healthz')+'/api',
               'worker_id': worker['id'], 'token': worker['worker_token'],
               'code': 'WECHAT_UI_LAYOUT_UNRESOLVED', 'interruption': 'fail:before'}
    result = submitted.run_worker(tmp_path, r'''
from chejin_worker_client.storage import load_c2_state
runner.tick_once()
flow = load_runtime_control()['inflight_flow_id']
print(json.dumps(load_c2_state(runner._inflight_finish_receipt_key(flow))['task_failure']))
''', request)
    h = {'X-Worker-Token': worker['worker_token'], 'X-Client-Instance-Id': result['client_instance_id'],
         'X-Inflight-Flow-Id': result['flow_id'], 'X-Task-Lease-Fencing-Token': str(result['lease_fencing_token'])}
    body = {key: result[key] for key in ('error_code', 'failure_step', 'failure_remark')}
    body['settlement_only'] = True
    return worker, result, h, body


def counts(task_id):
    with SessionLocal() as db:
        failed = list(db.scalars(select(TaskEvent).where(
            TaskEvent.task_id == task_id, TaskEvent.event_type == 'failed')))
        confirmed = list(db.scalars(select(OperationLog).where(
            OperationLog.target_id == task_id, OperationLog.event_type == 'task_failure_receipt_confirmed')))
        return [len(failed), len(confirmed)]


@pytest.mark.parametrize('change', ['flow', 'fence', 'client', 'worker_token', 'owner', 'completed', 'task_type', 'flow_kind'])
def test_wrong_original_identity_cannot_settle(http_api, pending_receipt, change):
    worker, receipt, h, body = pending_receipt
    if change in ('flow', 'fence', 'client', 'worker_token'):
        key = {'flow':'X-Inflight-Flow-Id', 'fence':'X-Task-Lease-Fencing-Token',
               'client':'X-Client-Instance-Id', 'worker_token':'X-Worker-Token'}[change]
        h[key] = '999' if change == 'fence' else 'wrong-identity'
    else:
        with SessionLocal() as db:
            task = db.get(Task, receipt['task_id'])
            if change == 'owner': task.lease_owner_client_instance_id = 'different-instance'
            if change == 'completed': task.status = 'completed'
            if change == 'task_type': task.task_type = 'chat_reply'
            if change == 'flow_kind':
                row = db.get(Worker, worker['id'])
                row.inflight_flow_state = {**row.inflight_flow_state, 'flow_kind':'read'}
            db.commit()
    response = http_api.post('/api/tasks/'+receipt['task_id']+'/fail', headers=h, json=body)
    assert response.status_code in (401,403,409), response.text
    assert counts(receipt['task_id']) == [0,0]
    with SessionLocal() as db:
        assert db.get(Task, receipt['task_id']).status == ('completed' if change == 'completed' else 'running')
        assert db.get(Worker, worker['id']).inflight_flow_state['flow_id'] == receipt['flow_id']


@pytest.mark.parametrize('field', ['error_code', 'failure_step', 'failure_remark', 'bound_at', 'task_state'])
def test_confirmed_receipt_cannot_be_changed(http_api, pending_receipt, field):
    worker, receipt, h, body = pending_receipt
    path = '/api/tasks/'+receipt['task_id']+'/fail'
    first = http_api.post(path, headers=h, json=body)
    assert first.status_code == 200, first.text
    assert first.json()['data']['failure_receipt'] == receipt
    if field in body: body[field] = 'different-original-result'
    else:
        with SessionLocal() as db:
            if field == 'bound_at': db.get(Worker,worker['id']).bound_at = utcnow()+timedelta(seconds=1)
            else: db.get(Task,receipt['task_id']).error_code = 'OTHER'
            db.commit()
    response = http_api.post(path, headers=h, json=body)
    assert response.status_code == 409, response.text
    assert counts(receipt['task_id']) == [1,1]


def test_same_receipt_concurrent_retry_only_confirms_once(http_api, pending_receipt):
    worker, receipt, h, body = pending_receipt
    def send(_):
        return http_api.post('/api/tasks/'+receipt['task_id']+'/fail', headers=h, json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert [r.status_code for r in results] == [200,200], [r.text for r in results]
    assert all(r.json()['data']['failure_receipt'] == receipt for r in results)
    assert counts(receipt['task_id']) == [1,1]


def test_expired_lease_only_allows_original_receipt_not_normal_work(http_api, pending_receipt):
    worker, receipt, h, body = pending_receipt
    with SessionLocal() as db:
        db.get(Task,receipt['task_id']).lease_expires_at = utcnow()-timedelta(seconds=1)
        db.commit()
    path = '/api/tasks/'+receipt['task_id']+'/fail'
    normal = http_api.post(path, headers=h, json={**body,'settlement_only':False})
    assert normal.status_code == 409, normal.text
    finish = http_api.post('/api/workers/'+worker['id']+'/inflight-flow/finish',headers=h,
        json={'flow_id':receipt['flow_id'],'terminal_kind':'task_terminal'})
    assert finish.status_code == 409, finish.text
    settled = http_api.post(path, headers=h, json=body)
    assert settled.status_code == 200, settled.text
    assert settled.json()['data']['failure_receipt'] == receipt
    assert counts(receipt['task_id']) == [1,1]
    with SessionLocal() as db:
        task = db.get(Task, receipt['task_id'])
        assert task.status == 'failed' and task.lease_expires_at is None
        assert task.lease_fencing_token == receipt['lease_fencing_token']
        assert db.get(Worker, worker['id']).run_status == 'faulted'
