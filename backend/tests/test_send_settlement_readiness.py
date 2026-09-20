"""One settlement standard for fault recovery and release, with real HTTP/PG.

AI generation and Feishu transport are controlled; no desktop action is run.
The timeout and lease-expiry owners produce the terminal records under test.
"""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import ReplyAction, SentAck, HandoffEvent, Conversation
from app.models.task import Task, TaskEvent
from app.models.worker import Worker
from app.services import c3_service, feishu_service
from app.services.release_readiness import release_readiness
from app.services.worker_service import has_unsettled_worker_send
from test_pre_send_read_recovery import setup_receipt
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation


def terminal_timeout(http, monkeypatch, *, lease_first=True, late_ack=False):
    class Transport:
        def lookup_open_id(self, *args): return 'synthetic-open-id'
        def send_text_message(self, *args): return None
    monkeypatch.setattr(feishu_service, 'get_feishu_adapter', Transport)
    worker, ids, ack_path, headers, body = setup_receipt(http, monkeypatch, permit=True, stopped=False)
    if lease_first:
        with SessionLocal() as db:
            db.get(Task, ids['task_id']).lease_expires_at = utcnow() - timedelta(seconds=1)
            from app.services.task_service import pull_task_for_worker
            expired = pull_task_for_worker(db, db.get(Worker, worker['id']))
            assert expired['reason'] == 'TASK_LEASE_EXPIRED', expired
            db.commit()
    with SessionLocal() as db:
        assert c3_service.recover_stale_sending_reply_action(db, reply_action_id=ids['reply_action_id'], now=utcnow()+timedelta(days=1))
        db.commit()
        handoff = db.scalar(select(HandoffEvent).where(HandoffEvent.batch_id==db.get(ReplyAction,ids['reply_action_id']).batch_id))
        handoff_id = handoff.id
        assert handoff.notify_status == 'succeeded'
    if late_ack:
        body.update(send_result='unknown',action_phase='trigger_attempted',evidence={},error_code='SEND_RESULT_UNKNOWN')
        response=http.post(ack_path,headers=headers,json=body)
        assert response.status_code==200,response.text
    stopped=http.post(f"/api/workers/{worker['id']}/run-status", headers=headers,
                     json={'client_instance_id':'client-c3','run_status':'faulted'})
    assert stopped.status_code==200,stopped.text
    finished=http.post(f"/api/workers/{worker['id']}/inflight-flow/finish",headers=headers,
                       json={'flow_id':ids['flow_id'],'terminal_kind':'task_terminal','conversation_id':ids['conversation_id']})
    assert finished.status_code==200,finished.text
    return worker,ids,ack_path,headers,body,handoff_id


def persisted_snapshot(db):
    return {model.__tablename__:[{c.name:deepcopy(getattr(row,c.name)) for c in model.__table__.columns}
                                for row in db.scalars(select(model).order_by(model.__table__.columns[0]))]
            for model in (ReplyAction,Task,TaskEvent,SentAck,HandoffEvent)}


@pytest.mark.parametrize('mode',['lease_first','timeout_first','late_unknown_ack'])
def test_server_terminal_is_settled_without_rewriting_unknown(http_api,monkeypatch,async_generation,tmp_path,mode):
    worker,ids,path,headers,body,_=terminal_timeout(http_api,monkeypatch,
                lease_first=mode!='timeout_first',late_ack=mode=='late_unknown_ack')
    with SessionLocal() as db:
        before=persisted_snapshot(db)
        ready=release_readiness(db)
        assert ready['ready'],ready
        assert not has_unsettled_worker_send(db,db.get(Worker,worker['id']))
        assert persisted_snapshot(db)==before  # Read-only classification; no synthetic ACK/data cleanup.
        assert db.get(ReplyAction,ids['reply_action_id']).status=='unknown_send_result'
        assert bool(db.scalar(select(SentAck.id)))==(mode=='late_unknown_ack')
    response=http_api.post(f"/api/workers/{worker['id']}/run-status",headers=headers,
                          json={'run_status':'running','client_instance_id':'client-c3','recover_from_fault':True})
    assert response.status_code==200,response.text
    rejected=http_api.post(f"/api/reply-actions/{ids['reply_action_id']}/claim-send",headers=headers,
                           json={'task_id':ids['task_id'],'worker_id':worker['id']})
    assert rejected.status_code==409,rejected.text
    with SessionLocal() as db:
        assert persisted_snapshot(db)==before
        assert db.get(Conversation,ids['conversation_id']).status=='waiting_sales_reply'
    (tmp_path/'result.json').write_text(json.dumps({'mode':mode,'ready':ready,'recover_http':response.status_code,
                    'old_send_http':rejected.status_code,'no_records_rewritten':True},indent=2))


@pytest.mark.parametrize('damage',[
    'task_running','task_owner','claim_task','lease_worker','lease_client','lease_expiry','lease_renewed',
    'missing_event','event_owner','event_error','event_status','missing_handoff','handoff_action',
    'handoff_batch','handoff_result','handoff_reason','no_permit','no_claim_time','wrong_error','sending',
    'invalid_ack','empty_permit','event_predates_claim','handoff_predates_claim','handoff_conversation','handoff_error','deleted_task',
])
def test_incomplete_terminal_evidence_still_blocks_both_gates(http_api,monkeypatch,async_generation,damage):
    worker,ids,_,headers,_,handoff_id=terminal_timeout(http_api,monkeypatch)
    with SessionLocal() as db:
        t=db.get(Task,ids['task_id']);a=db.get(ReplyAction,ids['reply_action_id']);h=db.get(HandoffEvent,handoff_id)
        e=db.scalar(select(TaskEvent).where(TaskEvent.task_id==t.id,TaskEvent.event_type=='failed'))
        if damage=='task_running':t.status='running'
        elif damage=='task_owner':t.worker_id=None
        elif damage=='claim_task':a.claimed_task_id='not-the-original-task'
        elif damage=='lease_worker':t.lease_owner_worker_id=worker['id']
        elif damage=='lease_client':t.lease_owner_client_instance_id='still-held'
        elif damage=='lease_expiry':t.lease_expires_at=utcnow()+timedelta(hours=1)
        elif damage=='lease_renewed':t.lease_last_renewed_at=utcnow()
        elif damage=='missing_event':db.delete(e)
        elif damage=='event_owner':e.worker_id=None
        elif damage=='event_error':e.error_code='UNRELATED_FAILURE'
        elif damage=='event_status':e.to_status='completed'
        elif damage=='missing_handoff':db.delete(h)
        elif damage=='handoff_action':h.ai_payload={**h.ai_payload,'reply_action_id':'another-action'}
        elif damage=='handoff_batch':h.batch_id='another-batch'
        elif damage=='handoff_result':h.ai_payload={**h.ai_payload,'send_result':'sent'}
        elif damage=='handoff_reason':h.handoff_reason_code='UNRELATED_FAILURE'
        elif damage=='empty_permit':a.send_token=''
        elif damage=='event_predates_claim':e.created_at=a.sending_claimed_at-timedelta(seconds=1)
        elif damage=='handoff_predates_claim':h.created_at=a.sending_claimed_at-timedelta(seconds=1)
        elif damage=='handoff_conversation':h.conversation_id='another-conversation'
        elif damage=='handoff_error':h.ai_payload={**h.ai_payload,'error_code':'OTHER_ERROR'}
        elif damage=='deleted_task':t.deleted_at=utcnow()
        elif damage=='no_permit':a.send_token=None
        elif damage=='no_claim_time':a.sending_claimed_at=None
        elif damage=='wrong_error':a.error_code='UNRELATED_FAILURE'
        elif damage=='sending':a.status='sending'
        elif damage=='invalid_ack':db.add(SentAck(reply_action_id=a.id,task_id=t.id,worker_id=worker['id'],
               client_instance_id='client-c3',send_token='wrong-token',send_result='unknown',action_phase='trigger_attempted'))
        db.commit()
        assert has_unsettled_worker_send(db,db.get(Worker,worker['id']))
        assert release_readiness(db)['pending_messages_or_send']
    denied=http_api.post(f"/api/workers/{worker['id']}/run-status",headers=headers,
                        json={'client_instance_id':'client-c3','run_status':'running','recover_from_fault':True})
    assert denied.status_code==409,denied.text


@pytest.mark.parametrize('blocker',['flow','lock','running','notification','generation'])
def test_settled_send_does_not_bypass_other_release_barriers(http_api,monkeypatch,async_generation,blocker):
    worker,ids,_,_,_,handoff_id=terminal_timeout(http_api,monkeypatch)
    with SessionLocal() as db:
        w=db.get(Worker,worker['id'])
        if blocker=='flow':w.inflight_flow_state={'flow_id':'unsettled-another-flow'}
        elif blocker=='lock':w.local_lock_summary={'locked':True}
        elif blocker=='running':w.run_status='running'
        elif blocker=='notification':db.get(HandoffEvent,handoff_id).notify_status='pending'
        else:
            from app.models.c3 import MessageBatch
            b=db.get(MessageBatch,db.get(ReplyAction,ids['reply_action_id']).batch_id);b.active=True;b.status='generating'
        db.commit()
        assert not release_readiness(db)['ready']
