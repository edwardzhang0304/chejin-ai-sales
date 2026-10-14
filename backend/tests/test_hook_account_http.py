from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.audit import OperationLog
from app.models.worker import Worker

client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def worker(name="Hook Worker", phone="13900000001", bind_sales=True):
    result = client.post('/api/workers', json={'worker_name': name, 'platform': 'windows'}).json()['data']
    if bind_sales:
        sales = client.post('/api/sales', json={'sales_name': name, 'phone': phone, 'enabled': True}).json()['data']
        response = client.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': result['id']})
        assert response.status_code == 200
    response = client.post(f"/api/workers/{result['id']}/client-bind",
        json={'worker_token': result['worker_token'], 'client_instance_id': name})
    assert response.status_code == 200
    result['instance'] = name
    return result


def context(wxid='wxid_sales_A', **fields):
    return {'self_wxid': wxid, 'process_generation': 'process-1', 'deployment_revision': 'deployment-1',
        'wechat_file_version': '3.9.10.16', 'dll_sha256': 'a' * 64, **fields}


def heartbeat(w, hook=None, **body):
    payload = {'running_status': 'idle', 'run_status': 'paused', 'client_instance_id': w['instance'],
        'rpa_component_status': 'ready', 'wechat_status': 'logged_in', **body}
    if hook is not None:
        payload['hook_context'] = hook
    return client.post(f"/api/workers/{w['id']}/heartbeat", json=payload,
        headers={'X-Worker-Token': w['worker_token']})


def test_first_hook_account_binds_via_verified_worker_and_repeat_keeps_epoch():
    w = worker()
    response = heartbeat(w, context(), run_status='running')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['run_status'] == 'paused'  # Registration is not an automatic start.
    hook = data['hook_context']
    assert hook['self_wxid'] == 'wxid_sales_A' and hook['account_epoch'] and not hook['blocked']
    repeated = heartbeat(w, context()).json()['data']['hook_context']
    assert repeated['account_epoch'] == hook['account_epoch']
    with SessionLocal() as db:
        logs = list(db.scalars(select(OperationLog).where(OperationLog.event_type == 'worker_hook_account_bound')))
        assert len(logs) == 1


def test_changed_account_fault_is_durable_and_preserves_owner_and_current_flow():
    w = worker()
    epoch = heartbeat(w, context()).json()['data']['hook_context']['account_epoch']
    with SessionLocal() as db:
        row = db.get(Worker, w['id'])
        row.inflight_flow_state = {'flow_id': 'existing-flow', 'status': 'active'}
        db.commit()
    response = heartbeat(w, context('wxid_sales_B', account_epoch=epoch), run_status='running')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['run_status'] == 'faulted'
    assert data['hook_context']['self_wxid'] == 'wxid_sales_A'
    assert data['hook_context']['account_epoch'] == epoch
    assert data['hook_context']['reason_code'] == 'HOOK_ACCOUNT_CHANGED'
    assert data['inflight_flow_state']['flow_id'] == 'existing-flow'
    assert data['inflight_flow_state']['status'] == 'draining'
    switched_back = heartbeat(w, context(account_epoch=epoch), run_status='running').json()['data']
    assert switched_back['hook_context']['blocked']
    assert switched_back['hook_context']['rebind_required']
    assert switched_back['hook_context']['reason_code'] == 'HOOK_ACCOUNT_CHANGED'
    assert switched_back['run_status'] == 'faulted'
    # Even after the original Flow finishes, explicit start cannot replace reset.
    with SessionLocal() as db:
        db.get(Worker, w['id']).inflight_flow_state = {}
        db.commit()
    start = client.post(f"/api/workers/{w['id']}/run-status",
        headers={'X-Worker-Token': w['worker_token']},
        json={'client_instance_id': w['instance'], 'run_status': 'running', 'recover_from_fault': True})
    assert start.status_code == 409 and start.json()['code'] == 'WORKER_FAULT_RECOVERY_NOT_READY'
    with SessionLocal() as db:
        assert db.scalar(select(OperationLog.id).where(OperationLog.event_type == 'worker_hook_account_blocked'))


def test_wrong_epoch_and_missing_hook_context_block_new_work():
    w = worker()
    heartbeat(w, context())
    wrong = heartbeat(w, context(account_epoch='old-epoch')).json()['data']
    assert wrong['run_status'] == 'faulted' and wrong['hook_context']['reason_code'] == 'HOOK_ACCOUNT_EPOCH_MISMATCH'
    missing = heartbeat(w).json()['data']
    assert missing['run_status'] == 'faulted' and missing['hook_context']['reason_code'] == 'HOOK_CONTEXT_REQUIRED'
    start = client.post(f"/api/workers/{w['id']}/run-status",
        headers={'X-Worker-Token': w['worker_token']},
        json={'client_instance_id': w['instance'], 'run_status': 'running', 'recover_from_fault': True})
    assert start.status_code == 409 and start.json()['code'] == 'WORKER_FAULT_RECOVERY_NOT_READY'


def test_same_wechat_cannot_be_bound_by_another_worker_even_when_first_is_paused():
    a = worker()
    b = worker('Hook Worker B', '13900000002')
    heartbeat(a, context())
    blocked = heartbeat(b, context()).json()['data']
    assert blocked['run_status'] == 'faulted'
    assert blocked['hook_context']['reason_code'] == 'HOOK_ACCOUNT_ALREADY_BOUND'
    assert blocked['hook_context']['self_wxid'] is None


def test_without_sales_cannot_acquire_account_and_wrong_client_cannot_change_owner():
    w = worker(bind_sales=False)
    result = heartbeat(w, context()).json()['data']
    assert result['hook_context']['reason_code'] == 'HOOK_SALES_BINDING_REQUIRED'
    assert result['hook_context']['self_wxid'] is None
    invalid = heartbeat(w, context(), client_instance_id='another-device')
    assert invalid.status_code == 401


def test_original_reset_rebind_rotates_epoch_and_old_token_is_invalid():
    w = worker()
    first = heartbeat(w, context()).json()['data']['hook_context']
    changed = heartbeat(w, context('wxid_sales_B')).json()['data']['hook_context']
    assert changed['rebind_required']
    reset = client.post(f"/api/workers/{w['id']}/reset-binding", json={})
    assert reset.status_code == 200
    assert heartbeat(w, context()).status_code == 401
    new = dict(w, worker_token=reset.json()['data']['worker_token'], instance='new-instance')
    bound = client.post(f"/api/workers/{new['id']}/client-bind",
        json={'worker_token': new['worker_token'], 'client_instance_id': new['instance']})
    assert bound.status_code == 200
    later = heartbeat(new, context('wxid_sales_B')).json()['data']['hook_context']
    assert not later['blocked'] and later['self_wxid'] == 'wxid_sales_B'
    assert later['account_epoch'] != first['account_epoch']


def test_original_ocr_heartbeat_retains_original_contract():
    w = worker()
    response = heartbeat(w)
    assert response.status_code == 200 and 'hook_context' not in response.json()['data']
