"""Synthetic catalog claims through real generation/claim/receipt routes; AI boundary controlled."""
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HandoffEvent,MessageBatch,ReplyAction,ReplyActionVehicleFact
from app.models.task import Task
from app.models.vehicle import KnowledgeItem
from app.services import c3_service
import test_c3_api as api

@pytest.fixture(autouse=True)
def isolated_database():
    api.setup_function()

class CatalogAdapter:
    def __init__(self,ids=(),source='product_master',during=None):
        self.ids=list(ids);self.source=source;self.during=during
    def generate_reply_decision(self,**kwargs):
        if self.during:self.during()
        reply='目前没有符合这两个品牌要求的在库车源。'
        return c3_service.AIEngineDecision(decision='send_reply',reply_text=reply,
            confidence=.95,guard_result='pass',raw_payload={'omniauto_brain_result':{'brain_plan':{
                'recommended_action':'send_reply','evidence_used':{'product_ids':self.ids},
                'facts_claimed':[{'fact_type':'inventory_absence','source_level':'product_master','source_id':self.source}],
                'reply_segments':[reply]}}})

def generate(monkeypatch,adapter):
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:adapter)
    w,b=api._setup_bound_conversation()
    with SessionLocal() as db:
        db.get(Conversation,b['conversation_id']).status='waiting_user_reply';db.commit()
    api._ingest(w,b['conversation_id'],'catalog-test-message','我只考虑这两个品牌。')
    # Observe the actual ingest callback; do not manually retry a failed batch.
    with SessionLocal() as db:
        batch=db.scalar(select(MessageBatch).where(MessageBatch.conversation_id==b['conversation_id']))
        assert batch is not None and batch.generation_attempt_count == 1
        r={'decision':batch.decision,'batch':{'id':batch.id,'error_code':batch.error_code}}
        action=db.scalar(select(ReplyAction).where(ReplyAction.batch_id==batch.id))
        if action:
            r.update(reply_action_id=action.id,task_id=db.scalar(select(Task.id).where(Task.reply_action_id==action.id)))
    return w,b,r

def claim(w,b,r):
    response=api.client.post(f"/api/tasks/{r['task_id']}/claim",json={'worker_id':w['id'],
        'claim_source':'c2_conversation_flow','conversation_id':b['conversation_id']},headers=api._worker_headers(w))
    assert response.status_code==200,response.text
    return response

def claim_send(w,r,claimed):
    return api.client.post(f"/api/reply-actions/{r['reply_action_id']}/claim-send",
        json={'worker_id':w['id'],'task_id':r['task_id']},headers=api._task_lease_headers(w,claimed))

@pytest.mark.parametrize('size,explicit',[(0,False),(1,False),(2,True)])
def test_catalog_claim_closes_without_false_handoff(monkeypatch,size,explicit):
    ids=[api._create_listed_vehicle() for _ in range(size)]
    w,b,r=generate(monkeypatch,CatalogAdapter(ids[:1] if explicit else []))
    assert r['decision']=='send_reply',r
    with SessionLocal() as db:
        a=db.get(ReplyAction,r['reply_action_id'])
        assert a.ai_payload['vehicle_catalog_snapshot']['vehicle_ids']==sorted(ids)
        facts=list(db.scalars(select(ReplyActionVehicleFact).where(ReplyActionVehicleFact.reply_action_id==a.id)))
        assert sorted(f.vehicle_id for f in facts)==sorted(ids)
    response=claim_send(w,r,claim(w,b,r));assert response.status_code==200,response.text
    send=response.json()['data']
    ack=api.client.post(f"/api/reply-actions/{r['reply_action_id']}/sent-ack",json={
        'worker_id':w['id'],'task_id':r['task_id'],'client_instance_id':'client-c3',
        'send_token':send['send_token'],'send_result':'sent','action_phase':'confirmed',
        'reply_text_hash':send['reply_text_hash'],'sidecar_run_id':'synthetic-catalog-receipt'},headers=api._worker_headers(w))
    assert ack.status_code==200,ack.text
    with SessionLocal() as db:
        assert db.get(ReplyAction,r['reply_action_id']).status=='sent'
        assert db.get(Task,r['task_id']).status=='completed'
        assert db.get(Conversation,b['conversation_id']).status=='waiting_user_reply'
        assert not list(db.scalars(select(HandoffEvent)))

@pytest.mark.parametrize('mutation',['price','unlist','new_listing'])
def test_catalog_change_during_generation_blocks(monkeypatch,mutation):
    vid=api._create_listed_vehicle()
    def change():
        if mutation=='new_listing':api._create_listed_vehicle()
        elif mutation=='unlist':assert api.client.post(f'/api/vehicles/{vid}/unlist',headers=api.HEADERS).status_code==200
        else:assert api.client.put(f'/api/vehicles/{vid}',json={'public_price':9.99},headers=api.HEADERS).status_code==200
    _,_,r=generate(monkeypatch,CatalogAdapter(during=change))
    assert r['batch']['error_code']=='REPLY_ACTION_VEHICLE_FACT_STALE',r
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        assert not list(db.scalars(select(Task).where(Task.task_type=='chat_reply')))

@pytest.mark.parametrize('mutation',['price','unlist','new_listing','missing_snapshot','missing_vehicle_snapshot'])
def test_catalog_final_gate_preserves_checks(monkeypatch,mutation):
    vid=api._create_listed_vehicle()
    w,b,r=generate(monkeypatch,CatalogAdapter());claimed=claim(w,b,r)
    with SessionLocal() as db:
        a=db.get(ReplyAction,r['reply_action_id'])
        v=db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id==vid))
        if mutation=='missing_snapshot':
            p=dict(a.ai_payload);p.pop('vehicle_catalog_snapshot');a.ai_payload=p
        elif mutation=='missing_vehicle_snapshot':db.delete(db.get(ReplyActionVehicleFact,{'reply_action_id':a.id,'vehicle_id':vid}))
        elif mutation=='new_listing':
            db.add(KnowledgeItem(tenant_id=v.tenant_id,layer=v.layer,category_id=v.category_id,
                product_id='',item_id='CJ_SYNTHETIC_NEW',status='active',payload=dict(v.payload)))
        elif mutation=='unlist':v.status='inactive'
        else:
            p=dict(v.payload);p['data']={**p['data'],'price':9.99};v.payload=p
        db.commit()
    response=claim_send(w,r,claimed)
    assert response.status_code==409,response.text
    assert response.json()['code']=='REPLY_ACTION_VEHICLE_FACT_STALE'
    with SessionLocal() as db:assert db.get(ReplyAction,r['reply_action_id']).send_token is None

@pytest.mark.parametrize('ids,source',[(['CJ_DOES_NOT_EXIST'],'product_master'),([], 'CJ_DOES_NOT_EXIST'),(['product_master'],'product_master')])
def test_unknown_explicit_ids_still_block(monkeypatch,ids,source):
    api._create_listed_vehicle()
    _,_,r=generate(monkeypatch,CatalogAdapter(ids,source))
    assert r['batch']['error_code']=='REPLY_ACTION_VEHICLE_FACT_STALE',r
    with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))

@pytest.mark.parametrize('initial_size',[0,1])
def test_new_listing_invalidates_queued_catalog_claim(monkeypatch,initial_size):
    for _ in range(initial_size):api._create_listed_vehicle()
    _,_,r=generate(monkeypatch,CatalogAdapter())
    api._create_listed_vehicle()
    with SessionLocal() as db:
        a=db.get(ReplyAction,r['reply_action_id'])
        assert a.status=='superseded' and a.send_token is None
        assert db.get(Task,r['task_id']).status=='cancelled'


def test_legacy_empty_catalog_action_without_checkpoint_blocks(monkeypatch):
    w,b,r=generate(monkeypatch,CatalogAdapter())
    claimed=claim(w,b,r)
    with SessionLocal() as db:
        a=db.get(ReplyAction,r['reply_action_id'])
        p=dict(a.ai_payload);p.pop('vehicle_catalog_snapshot');a.ai_payload=p;db.commit()
    response=claim_send(w,r,claimed)
    assert response.status_code==409
    assert response.json()['code']=='REPLY_ACTION_VEHICLE_FACT_STALE'

@pytest.mark.parametrize('mutation',['price','image_order'])
def test_archived_vehicle_edits_preserve_unchanged_active_catalog(monkeypatch,mutation):
    active=api._create_listed_vehicle()
    archived=api._create_listed_vehicle()
    assert api.client.post(f'/api/vehicles/{archived}/unlist',headers=api.HEADERS).status_code==200
    w,b,r=generate(monkeypatch,CatalogAdapter())
    if mutation=='price':
        response=api.client.put(f'/api/vehicles/{archived}',json={'public_price':9.99},headers=api.HEADERS)
    else:
        assert api.client.post(f'/api/vehicles/{archived}/images',files={'files':('extra.png',api.PNG_1X1,'image/png')},headers=api.HEADERS).status_code==200
        images=api.client.get(f'/api/vehicles/{archived}',headers=api.HEADERS).json()['data']['images']
        response=api.client.put(f'/api/vehicles/{archived}/images/order',json={'image_ids':[row['id'] for row in reversed(images)]},headers=api.HEADERS)
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        a=db.get(ReplyAction,r['reply_action_id'])
        assert a.status=='queued' and c3_service._stale_action_vehicle_ids(db,a)==[]
        assert a.ai_payload['vehicle_catalog_snapshot']['vehicle_ids']==[active]
    response=claim_send(w,r,claim(w,b,r))
    assert response.status_code==200,response.text
