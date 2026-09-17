"""Real HTTP/PG tests for original permits, never a substitute for a send."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_pre_send_read_recovery import setup_receipt
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from chejin_worker_client.api import WorkerApiClient, ApiError
from chejin_worker_client.models import Binding, Task as LocalTask, ReplySendClaim


def snapshot(ids):
    with SessionLocal() as db:
        objects = [db.get(ReplyAction, ids['reply_action_id']), db.get(Task, ids['task_id']),
                   db.scalar(select(WechatSessionBinding)), db.scalar(select(Worker))]
        return [{column.name: deepcopy(getattr(row, column.name))
                 for column in row.__table__.columns} for row in objects]


@pytest.mark.parametrize('state', ['expired', 'revoked', 'acknowledged', 'unknown', 'legacy_intact'])
def test_query_keeps_original_permit_and_never_mutates_state(http_api, monkeypatch, async_generation, state):
    worker, ids, ack_path, headers, body = setup_receipt(http_api, monkeypatch, permit=True)
    if state in {'acknowledged','unknown'}:
        sent = deepcopy(body)
        if state == 'unknown':
            sent.update(send_result='unknown',action_phase='trigger_attempted',evidence={},error_code='SEND_UNKNOWN')
        response=http_api.post(ack_path,headers=headers,json=sent)
        assert response.status_code==200,response.text
    with SessionLocal() as db:
        db.get(Task,ids['task_id']).lease_expires_at=utcnow()-timedelta(seconds=5)
        action=db.get(ReplyAction,ids['reply_action_id'])
        action.expire_at=utcnow()-timedelta(seconds=5)
        if state=='legacy_intact':
            action.ai_payload={key:value for key,value in action.ai_payload.items() if key!='send_claim_identity'}
        db.commit()
    if state=='revoked':
        with SessionLocal() as db:
            from app.models.lead import Lead
            lead=db.get(Lead,db.get(Task,ids['task_id']).lead_id)
            lead_id=lead.id
        response=http_api.post(f'/api/leads/{lead_id}/mark-invalid',json={'invalid_reason':'test_data'})
        # Route/authorization are checked explicitly below; no direct data
        # edit may stand in for real revocation if this fixture is stale.
        assert response.status_code==200,response.text
    before=snapshot(ids)
    for _ in range(2):
        response=http_api.post(f"/api/reply-actions/{ids['reply_action_id']}/claim-send",headers=headers,
                              json={'worker_id':worker['id'],'task_id':ids['task_id'],'settlement_only':True})
        assert response.status_code==200,response.text
        data=response.json()['data']
        assert data['send_token']==body['send_token'] and data['reply_text_hash']==ids['reply_text_hash']
        assert data['send_allowed'] is False and data['settlement_only'] is True
        assert not {'reply_text','rpa_session_key','authorization_revision','pre_send_fact_checkpoint'} & data.keys()
        if state=='unknown': assert data['ack']['send_result']=='unknown'
    assert snapshot(ids)==before
    with pytest.raises(ValueError,match='REPLY_SETTLEMENT_PERMIT_CANNOT_SEND'):
        ReplySendClaim.from_api(data)
    client=WorkerApiClient(str(http_api.get('/healthz').url).removesuffix('/healthz')+'/api')
    binding=Binding(worker['id'],worker['worker_token'],'client-c3')
    monkeypatch.setattr(client,'_request',lambda *a,**kw:data)
    with pytest.raises(ApiError,match='原结算许可不能用于发送'):
        client.claim_send(binding,LocalTask.from_api({'id':ids['task_id'],'task_type':'chat_reply','reply_action_id':ids['reply_action_id']}))


@pytest.mark.parametrize('bad',['flow','fencing','client','task','missing_snapshot','capability','never_issued'])
def test_query_rejects_unproven_identity_without_issuing_a_permit(http_api,monkeypatch,async_generation,bad):
    worker,ids,_,headers,_=setup_receipt(http_api,monkeypatch,permit=bad!='never_issued')
    body={'worker_id':worker['id'],'task_id':ids['task_id'],'settlement_only':True}
    if bad=='flow': headers['X-Inflight-Flow-Id']='another-flow'
    if bad=='fencing': headers['X-Task-Lease-Fencing-Token']='999999'
    if bad=='client': headers['X-Client-Instance-Id']='another-client'
    if bad=='task': body['task_id']='unrelated-task'
    with SessionLocal() as db:
        if bad=='missing_snapshot':
            action=db.get(ReplyAction,ids['reply_action_id'])
            action.ai_payload={k:v for k,v in action.ai_payload.items() if k!='send_claim_identity'}
            db.get(Task,ids['task_id']).lease_owner_client_instance_id=None
        if bad=='capability': db.get(Worker,worker['id']).local_lock_summary={}
        db.commit()
    before=snapshot(ids)
    response=http_api.post(f"/api/reply-actions/{ids['reply_action_id']}/claim-send",headers=headers,json=body)
    assert response.status_code in {401,403,409},response.text
    assert snapshot(ids)==before
