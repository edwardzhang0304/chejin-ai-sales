from datetime import datetime,timezone
import copy,json,uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HandoffEvent,MessageBatch,ReplyAction
from app.models.task import Task,TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.services.hook_work_service import with_open_handoffs
from test_hook_business_http import clean,bound,post


def setup_handoff():
 w,grant=bound()
 with SessionLocal() as db:
  conv=db.get(Conversation,grant['conversation_id']);conv.status='waiting_sales_reply'
  batch=MessageBatch(conversation_id=conv.conversation_id,status='handoff_created',active=False,decision='handoff',error_code='AI_ENGINE_RETRY_EXHAUSTED');db.add(batch);db.flush()
  event=HandoffEvent(conversation_id=conv.conversation_id,batch_id=batch.id,handoff_reason_code='AI_ENGINE_RETRY_EXHAUSTED',reason_detail='PRIVATE-CUSTOMER-CONTENT',ai_payload={'private':'PRIVATE-AI-RESPONSE'},created_at=datetime(2026,10,12,13,23,29,tzinfo=timezone.utc));db.add(event);db.commit();event_id=event.id
 return w,grant,event_id

def snapshot():
 with SessionLocal() as db:
  return {'handoffs':[(x.id,x.status,x.closed_at,x.reason_detail) for x in db.scalars(select(HandoffEvent))], 'conversations':[(x.conversation_id,x.status,x.ai_enabled,x.handoff_reason_code) for x in db.scalars(select(Conversation))], 'tasks':[(x.id,x.status,x.result_code) for x in db.scalars(select(Task))], 'actions':[(x.id,x.status) for x in db.scalars(select(ReplyAction))]}

def work(w,grant):
 payload={k:grant[k] for k in ('self_wxid','account_epoch')};payload.update(process_generation='process-1',retained_flow_id=None)
 r=post(w,f"/workers/{w['id']}/wechat/native-work",payload);assert r.status_code==200,r.text
 return r.json()['data']

def test_actual_native_work_restores_open_handoff_without_mutation_or_new_permission():
 w,grant,eid=setup_handoff();before=snapshot();data=work(w,grant)
 record=data['bindings'][0]['open_handoffs'][0]
 assert record['id']==eid and record['handoff_reason_code']=='AI_ENGINE_RETRY_EXHAUSTED'
 assert record['created_at']=='2026-10-12T13:23:29+00:00'
 assert set(record)=={'id','batch_id','handoff_reason_code','created_at','progress_native_ids'}
 assert 'PRIVATE-' not in json.dumps(data) and data['can_send'] is False and data['run_status']=='running'
 assert snapshot()==before

@pytest.mark.parametrize('terminal',['closed','deleted'])
def test_finished_handoff_is_not_restored_as_waiting_sales(terminal):
 w,grant,eid=setup_handoff()
 with SessionLocal() as db:
  event=db.get(HandoffEvent,eid)
  if terminal=='closed':event.closed_at=datetime.now(timezone.utc)
  else:event.deleted_at=datetime.now(timezone.utc)
  db.commit()
 assert work(w,grant)['bindings'][0]['open_handoffs']==[]

@pytest.mark.parametrize('field,value',[('binding_id','not-existing'),('binding_revision',999),('peer_wxid','wxid_other'),('conversation_id','other-conversation')])
def test_enrichment_cannot_leak_other_binding_handoff(field,value):
 w,grant,eid=setup_handoff();data=work(w,grant);bad={**data['bindings'][0],field:value}
 with SessionLocal() as db:
  assert with_open_handoffs(db,w['id'],grant['self_wxid'],grant['account_epoch'],[bad])==[]

@pytest.mark.parametrize('revocation',['proof','allow_listening','account_epoch','invalid_lead'])
def test_proof_or_scope_revoked_between_authoritative_check_and_view_is_rechecked(revocation):
 w,grant,eid=setup_handoff();item=work(w,grant)['bindings'][0]
 with SessionLocal() as db:
  binding=db.get(WechatSessionBinding,grant['binding_id'])
  if revocation=='proof':db.get(TaskEvidence,binding.native_context['invite_evidence_id']).extra_metadata={}
  elif revocation=='allow_listening':binding.allow_listening=False
  elif revocation=='account_epoch':binding.native_context={**binding.native_context,'account_epoch':'other-epoch'}
  else:
   from app.models.lead import Lead
   db.get(Lead,binding.lead_id).status='invalid'
  db.commit()
  assert with_open_handoffs(db,w['id'],grant['self_wxid'],grant['account_epoch'],[item])==[]
