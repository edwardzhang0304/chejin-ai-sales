"""Real HTTP/PG + production Worker loop; only desktop actions are controlled."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.base import utcnow
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.c3 import ReplyAction
from app.models.task import Task
from test_contract_equivalent_recovery import prepared_read, post
from test_lead_followup_eligibility import isolated_db, http_api, client, headers
from test_read_business_settlement import invalidate
from chejin_worker_client.models import WechatReadTarget
from chejin_worker_client.wechat_c2 import build_flow_gate_ingest_payload


def closed_failure():
    worker, row, original = prepared_read(closed=False)
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = WechatReadTarget(conversation_id=binding.conversation_id,
            display_name=binding.remark_code, remark_code=binding.remark_code,
            rpa_session_key=binding.rpa_session_key, unread_generation=original['unread_generation'],
            authorization_revision=original['authorization_revision'], read_reason='waiting_sales_reply')
    payload = build_flow_gate_ingest_payload(target, read_run_id=original['read_run_id'],
        error_code='MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS')
    base = f"/api/workers/{worker['id']}"
    assert client.post(base+'/run-status', headers=headers(worker),
        json={'client_instance_id':'followup-test','run_status':'faulted'}).status_code == 200
    response = client.post(base+'/inflight-flow/finish', headers=headers(worker,payload['read_run_id']),
        json={'flow_id':payload['read_run_id'],'conversation_id':payload['conversation_id'],
              'terminal_kind':'technical_failed','error_code':'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'})
    assert response.status_code == 200, response.text
    return worker, row, payload


def delete_binding(row):
    with SessionLocal() as db:
        db.delete(db.get(WechatSessionBinding, row['binding_id']))
        db.commit()


@pytest.mark.parametrize('binding_removed', [False, True])
def test_closed_failure_receipt_is_idempotent_and_never_creates_facts(binding_removed):
    worker, row, payload = closed_failure()
    if binding_removed:
        delete_binding(row)
    original = copy.deepcopy(payload)
    first = post(worker,payload)
    assert first.status_code == 200, first.text
    assert first.json()['data']['read_completion']['result'] == 'technical_failed'
    again = post(worker,payload,flow_header=False)
    assert again.status_code == 200, again.text
    assert first.json()['data'] == again.json()['data']
    assert payload == original
    with SessionLocal() as db:
        assert db.scalar(select(func.count(OperationLog.id)).where(
            OperationLog.event_type=='worker_closed_read_failure_received')) == 1
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
        assert db.get(Worker,worker['id']).run_status == 'faulted'
    changed=copy.deepcopy(payload)
    changed['evidence']['flow_gate_errors']=['UNPROVEN_NEW_FAILURE']
    assert post(worker,changed).status_code == 409


@pytest.mark.parametrize('defect', ['no_finish','owner','bound_at','contract','time','scope','terminal','missing_gate','observation','message','active_flow','instance','header'])
def test_failure_receipt_does_not_admit_unproven_or_fact_payload(defect):
    worker,row,payload=closed_failure()
    h=headers(worker,payload['read_run_id'])
    if defect=='missing_gate': payload['evidence']['flow_gate_errors']=[]
    if defect=='observation': payload['evidence']['observations']=[{'observation_id':'fake'}]
    if defect=='message': payload['messages']=[{'content':'not a failure report'}]
    if defect=='time': payload['evidence']['finished_at']=(utcnow()+timedelta(days=1)).isoformat()
    if defect=='scope': payload['conversation_id']='different-customer'
    if defect=='instance': h['X-Client-Instance-Id']='different'
    if defect=='header': h['X-Inflight-Flow-Id']='different'
    with SessionLocal() as db:
        finish=db.scalar(select(OperationLog).where(OperationLog.event_type=='worker_inflight_finished'))
        if defect=='no_finish': db.delete(finish)
        if defect=='owner': finish.operator_id='another-worker'
        if defect=='bound_at': db.get(Worker,worker['id']).bound_at=utcnow()
        if defect=='contract': finish.extra_metadata={'registered_read_contract':{'contract_revision':'bad','contract_sha256':'0'*64}}
        if defect=='terminal': finish.after_data={**finish.after_data,'terminal_kind':'completed'}
        if defect=='active_flow': db.get(Worker,worker['id']).inflight_flow_state={'flow_id':'different','status':'active'}
        db.commit()
    r=client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest",headers=h,json=payload)
    assert r.status_code in (400,401,409,422),r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type=='worker_closed_read_failure_received'))==0
        assert db.scalar(select(func.count(MessageEvent.id)))==0


@pytest.mark.parametrize('defect', [None,'payload','instance','bound_at'])
def test_saved_business_receipt_survives_deleted_binding_without_reviving_customer(defect):
    worker,row,payload=prepared_read(closed=True)
    invalidate(row)
    first=post(worker,payload)
    assert first.status_code==200,first.text
    delete_binding(row)
    h=headers(worker,payload['read_run_id'])
    if defect=='payload': payload['messages'][0]['content']+='changed'
    if defect=='instance': h['X-Client-Instance-Id']='different'
    if defect=='bound_at':
        with SessionLocal() as db:
            db.get(Worker,worker['id']).bound_at=utcnow();db.commit()
    result=client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest",headers=h,json=payload)
    if defect:
        assert result.status_code in (401,409),result.text
    else:
        assert result.status_code==200,result.text
        assert result.json()['data']==first.json()['data']
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id)))==0
        assert db.scalar(select(func.count(ReplyAction.id)))==0


def next_task(worker):
    # Only arrange a pending task. The real Worker must claim/execute/finish it.
    from app.enums import ContactType
    from app.services.lead_service import _contact_model
    from app.services.contact_utils import normalize_phone
    from app.models.lead import Lead
    from task_ownership_fixtures import owned_add_friend_task
    with SessionLocal() as db:
        lead=Lead(customer_name='Next customer',status='assigned',source_type='manual',
            source_name_snapshot='test',created_by='test',updated_by='test')
        db.add(lead);db.flush()
        db.add(_contact_model(lead.id,ContactType.phone,normalize_phone('13800009998'),True))
        task=owned_add_friend_task(db,lead_id=lead.id,worker_id=worker['id'],task_type='add_friend',status='pending')
        db.add(task);db.flush();task_id=task.id;db.commit()
    return task_id


@pytest.mark.parametrize('receipt_kind',['failure','saved_business'])
@pytest.mark.parametrize('mode',['normal','response_lost'])
def test_recovery_loop_explicit_start_claims_and_finishes_next_task(http_api,tmp_path,receipt_kind,mode):
    if receipt_kind=='failure':
        worker,row,payload=closed_failure()
        expected='confirmed'
    else:
        worker,row,payload=prepared_read(closed=True)
        invalidate(row)
        assert post(worker,payload).status_code==200
        expected='conversation_terminated'
    delete_binding(row)
    task_id=next_task(worker)
    root=Path(__file__).resolve().parents[2]
    request={'worker':worker,'payload':payload,'expected_outbox_status':expected,'mode':mode,
        'legacy_terminal_without_proof':receipt_kind=='saved_business',
        'next_task_id':task_id,'url':http_api.get('/healthz').url.removesuffix('/healthz')}
    request_file=tmp_path/'request.json';request_file.write_text(json.dumps(request))
    env={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in
        ('worker-client','worker-client/tests','worker-client/omniauto-rpa')),
        'CHEJIN_WORKER_HOME':str(tmp_path/'client'),'CHEJIN_RPA_MODE':'mock','CHEJIN_C2_ENABLED':'false',
        'CHEJIN_HEARTBEAT_INTERVAL':'0.1','CHEJIN_TASK_POLL_INTERVAL':'0.1'}
    p=subprocess.run([sys.executable,str(Path(__file__).with_name('worker_business_settlement_probe.py')),
        str(request_file),str(tmp_path/'worker-evidence.json')],env=env,cwd=root,capture_output=True,text=True,timeout=50)
    (tmp_path/'worker.log').write_text(p.stdout+p.stderr)
    assert p.returncode==0,p.stderr[-6000:]
    with SessionLocal() as db:
        task=db.get(Task,task_id)
        assert task.status=='completed' and task.result_code=='invite_sent'
        assert db.get(Worker,worker['id']).run_status=='running'
        assert not db.get(Worker,worker['id']).inflight_flow_state
        assert db.scalar(select(func.count(MessageEvent.id)))==0
        assert db.scalar(select(func.count(ReplyAction.id)))==0
