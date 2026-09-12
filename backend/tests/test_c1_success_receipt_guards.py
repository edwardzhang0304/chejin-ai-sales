"""Real HTTP success settlement: no new lease, wrong identity rejected, idempotent."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db,http_api,fixture_rows
import test_worker_failure_consistency as submitted
from test_c1_success_receipt_http import BOUNDARY
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.task import Task,TaskEvent
from app.models.audit import OperationLog
from app.models.worker import Worker
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils

@pytest.fixture
def pending_success(http_api,tmp_path,monkeypatch):
    worker,rows=fixture_rows()
    with SessionLocal() as db:
        db.add(_contact_model(rows[0]['lead_id'],ContactType.phone,contact_utils.normalize_phone('13800005555'),True))
        db.add(Task(lead_id=rows[0]['lead_id'],worker_id=worker['id'],task_type='add_friend',status='pending'));db.commit()
    monkeypatch.setattr(submitted,'COMMON',submitted.COMMON.replace('raise ConnectionError(',"raise __import__('requests').ConnectionError(")+BOUNDARY)
    request={'base_url':http_api.get('/healthz').url.removesuffix('/healthz')+'/api','worker_id':worker['id'],'token':worker['worker_token'],'result_code':'invite_sent','interruption':'invite-sent:before'}
    receipt=submitted.run_worker(tmp_path,"""
from chejin_worker_client.storage import load_c2_state
runner.tick_once()
flow=load_runtime_control()['inflight_flow_id']
print(json.dumps(load_c2_state(runner._inflight_finish_receipt_key(flow))['task_success']))
""",request)
    h={'X-Worker-Token':worker['worker_token'],'X-Client-Instance-Id':receipt['client_instance_id'],'X-Inflight-Flow-Id':receipt['flow_id'],'X-Task-Lease-Fencing-Token':str(receipt['lease_fencing_token'])}
    return worker,receipt,h,{'settlement_only':True,'remark':receipt['remark']}

def counts(task_id):
    with SessionLocal() as db:
        return [len(list(db.scalars(select(TaskEvent).where(TaskEvent.task_id==task_id,TaskEvent.event_type=='completed')))),len(list(db.scalars(select(OperationLog).where(OperationLog.target_id==task_id,OperationLog.event_type=='task_success_receipt_confirmed'))))]

@pytest.mark.parametrize('change',['flow','fence','client','worker_token','owner','completed','task_type','flow_kind'])
def test_wrong_identity_does_not_settle_success(http_api,pending_success,change):
    worker,r,h,body=pending_success
    if change in ('flow','fence','client','worker_token'):
        key={'flow':'X-Inflight-Flow-Id','fence':'X-Task-Lease-Fencing-Token','client':'X-Client-Instance-Id','worker_token':'X-Worker-Token'}[change]
        h[key]='999' if change=='fence' else 'wrong-identity'
    else:
        with SessionLocal() as db:
            task=db.get(Task,r['task_id'])
            if change=='owner':task.lease_owner_client_instance_id='wrong-instance'
            if change=='completed':task.status='completed'
            if change=='task_type':task.task_type='chat_reply'
            if change=='flow_kind':
                w=db.get(Worker,worker['id']);w.inflight_flow_state={**w.inflight_flow_state,'flow_kind':'read'}
            db.commit()
    response=http_api.post('/api/tasks/'+r['task_id']+'/invite-sent',headers=h,json=body)
    assert response.status_code in (401,403,409),response.text
    assert counts(r['task_id'])==[0,0]
    with SessionLocal() as db:
        assert db.get(Task,r['task_id']).status==('completed' if change=='completed' else 'running')
        assert db.get(Worker,worker['id']).inflight_flow_state['flow_id']==r['flow_id']

@pytest.mark.parametrize('field',['remark','result_code','bound_at','task_state'])
def test_confirmed_success_is_immutable(http_api,pending_success,field):
    worker,r,h,body=pending_success;path='/api/tasks/'+r['task_id']+'/invite-sent'
    first=http_api.post(path,headers=h,json=body)
    assert first.status_code==200,first.text
    assert first.json()['data']['success_receipt']==r
    if field=='remark':body['remark']='altered-result'
    elif field=='result_code':path=path.replace('/invite-sent','/already-friend')
    else:
        with SessionLocal() as db:
            if field=='bound_at':db.get(Worker,worker['id']).bound_at=utcnow()+timedelta(seconds=1)
            else:db.get(Task,r['task_id']).error_code='OTHER'
            db.commit()
    response=http_api.post(path,headers=h,json=body)
    assert response.status_code==409,response.text
    assert counts(r['task_id'])==[1,1]

def test_same_success_concurrent_retry_confirms_once(http_api,pending_success):
    worker,r,h,body=pending_success
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:http_api.post('/api/tasks/'+r['task_id']+'/invite-sent',headers=h,json=body),range(2)))
    assert [x.status_code for x in results]==[200,200],[x.text for x in results]
    assert all(x.json()['data']['success_receipt']==r for x in results)
    assert counts(r['task_id'])==[1,1]

def test_expired_lease_allows_settlement_only(http_api,pending_success):
    worker,r,h,body=pending_success
    with SessionLocal() as db:
        db.get(Task,r['task_id']).lease_expires_at=utcnow()-timedelta(seconds=1);db.commit()
    path='/api/tasks/'+r['task_id']+'/invite-sent'
    normal=http_api.post(path,headers=h,json={**body,'settlement_only':False})
    assert normal.status_code==409,normal.text
    finish=http_api.post('/api/workers/'+worker['id']+'/inflight-flow/finish',headers=h,json={'flow_id':r['flow_id'],'terminal_kind':'task_terminal'})
    assert finish.status_code==409,finish.text
    settled=http_api.post(path,headers=h,json=body)
    assert settled.status_code==200,settled.text
    assert settled.json()['data']['success_receipt']==r
    assert counts(r['task_id'])==[1,1]
    with SessionLocal() as db:
        task=db.get(Task,r['task_id'])
        assert task.status=='completed' and task.lease_expires_at is None
        assert task.lease_fencing_token==r['lease_fencing_token']
