"""Real image-order HTTP, PostgreSQL, automatic generation and claim-send.

Only the Brain subprocess boundary is controlled; the production input-evidence
producer is called on its actual invocation. Runtime producer tests are separate.
No physical Windows action or live model is claimed.
"""
from io import BytesIO
import json
import os
import threading
from PIL import Image
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.c3 import Conversation, MessageBatch, ReplyAction, ReplyActionVehicleFact
from app.models.task import Task
from app.models.vehicle import KnowledgeItem, VehicleImage
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.vehicle_fact_policy import POLICY
from app.contracts.shared_rules import shared_adapter
import test_c3_api as helper
from test_rule_intersections import http, data, record, pytestmark


def png(color):
    out=BytesIO();Image.new('RGB',(3,3),color).save(out,format='PNG');return out.getvalue()


@pytest.mark.parametrize('case',[
    'during_generation','queued','segments','image_reference','image_evidence','image_plan',
    'missing_evidence','model_self_declaration','price','unlist','new_image','changed_bytes',
])
def test_order_policy_at_generation_and_actual_claim(http,monkeypatch,case):
    from app.api.routes import wechat as routes
    monkeypatch.setattr(helper,'client',http)
    vehicle_id=helper._create_listed_vehicle()
    data(http.post(f'/api/vehicles/{vehicle_id}/images',files={'files':('second.png',png('blue'),'image/png')},headers=helper.HEADERS))
    images=data(http.get(f'/api/vehicles/{vehicle_id}',headers=helper.HEADERS))['images']
    image_ids=[row['id'] for row in images];assert len(image_ids)==2
    worker,binding=helper._setup_bound_conversation()
    data(http.post(f"/api/workers/{worker['id']}/heartbeat",json={'client_instance_id':'client-c3','local_lock_summary':{'capabilities':{'reply_sequence_version':1}}},headers=helper._worker_headers(worker)))
    with SessionLocal() as db:
        db.get(Conversation,binding['conversation_id']).status='waiting_user_reply';db.commit()
    complete=threading.Event();original=routes._generate_message_batch;calls=[]
    if os.environ.get('AUDIT_DISABLE_IMAGE_ORDER_POLICY')=='1':
        monkeypatch.setattr(c3_service,'policy_for_generated_group',lambda *a,**kw:{})
    def completed(*a,**kw):
        try:return original(*a,**kw)
        finally:complete.set()
    monkeypatch.setattr(routes,'_generate_message_batch',completed)
    if os.environ.get('AUDIT_DISABLE_AUTO_CALLBACK')=='1':
        monkeypatch.setattr(routes,'_generate_message_batch',lambda *a,**kw:complete.set())
    def reorder():
        return data(http.put(f'/api/vehicles/{vehicle_id}/images/order',json={'image_ids':list(reversed(image_ids))},headers=helper.HEADERS))
    def brain_boundary(**kwargs):
        invocation=kwargs['invocation'];calls.append(invocation)
        with SessionLocal() as db:
            vehicle=db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id==vehicle_id))
            price=vehicle.payload['data']['price']
        if case=='during_generation':reorder()
        reply=f'这款车目前在售，公开售价是{price}万元。'
        if case=='image_reference':reply='第一张图片是该车，公开售价是12.88万元。'
        segments=[reply+'这款车适合日常城市通勤，可以根据每天的行驶里程和停车条件再考虑是否合适。',
                  '到店前可以先跟顾问预约时间，看看实车的空间和座椅是否符合平时使用习惯，再确认具体配置和车况，让购车决定更踏实一些。'] if case=='segments' else [reply]
        plan={'recommended_action':'send_reply','confidence':.95,'evidence_used':{'product_ids':[vehicle_id]},
              'evidence_refs':[f'product_master:{vehicle_id}'],
              'facts_claimed':[{'fact_type':'price','value':f'{price}万元','source_level':'product_master','source_id':vehicle_id}],
              'reply_segments':segments}
        if case=='image_plan':plan['media_plan']=[{'type':'image','image_id':image_ids[0]}]
        input_facts={'current_message':{'clean_text':invocation['combined'],'message_ids':[r['id'] for r in invocation['batch']]},
                     'conversation':invocation['target_state']['conversation_context'],
                     'target':{'conversation_id':binding['conversation_id']},'evidence':{'product':{'id':vehicle_id,'price':price}}}
        if case=='image_evidence':input_facts['evidence']['photo']={'id':image_ids[0]}
        result={'rule_name':'customer_service_brain_reply','adoptable':True,'visible_reply_source':'brain_plan.reply_segments',
                'reply_text':'\n'.join(segments),'guard_verdict':'pass','brain_plan':plan,
                'brain_input_summary':{'input_dependency_evidence':shared_adapter('image_order_dependencies').input_dependency_evidence(input_facts)}}
        if case in {'missing_evidence','model_self_declaration'}:result.pop('brain_input_summary')
        if case=='model_self_declaration':result['vehicle_fact_policy']=dict(POLICY)
        return result
    adapter=RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter,'_load_config',lambda:{'customer_service_brain':{'provider':'openai','api_key':'isolated-test-only','model':'controlled-brain'}})
    monkeypatch.setattr(adapter,'_run_brain_isolated',brain_boundary)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:adapter)
    helper._ingest(worker,binding['conversation_id'],'sort-'+case,'这款车现在多少钱？')
    assert complete.wait(8),'automatic HTTP callback did not complete'
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        assert len(actions)==(2 if case=='segments' else 1),[(b.status,b.error_code) for b in db.scalars(select(MessageBatch))]
        action=actions[0];task=db.scalar(select(Task).where(Task.reply_action_id==action.id))
        aid,tid=action.id,task.id
        policy=action.ai_payload.get('vehicle_fact_policy')
        approved=case not in {'image_reference','image_evidence','image_plan','missing_evidence','model_self_declaration'}
        assert all(a.ai_payload.get('vehicle_fact_policy')==(POLICY if approved else None) for a in actions)
        generation=db.get(MessageBatch,action.batch_id).ai_request_snapshot['vehicle_fact_generation_snapshot']['vehicles'][vehicle_id]
        assert generation['image_order_independent_fingerprint']
    if case!='during_generation':reorder()
    if case=='price':data(http.put(f'/api/vehicles/{vehicle_id}',json={'public_price':11.66},headers=helper.HEADERS))
    if case=='unlist':data(http.post(f'/api/vehicles/{vehicle_id}/unlist',headers=helper.HEADERS))
    if case=='new_image':data(http.post(f'/api/vehicles/{vehicle_id}/images',files={'files':('third.png',png('green'),'image/png')},headers=helper.HEADERS))
    if case=='changed_bytes':
        # Storage corruption/replacement under an existing ID is a controlled
        # external mutation; the final claim must independently reject it.
        with SessionLocal() as db:db.get(VehicleImage,image_ids[0]).sha256='f'*64;db.commit()
    claim=http.post(f'/api/tasks/{tid}/claim',json={'worker_id':worker['id'],'claim_source':'c2_conversation_flow','conversation_id':binding['conversation_id']},headers=helper._worker_headers(worker))
    send=None
    if claim.status_code==200:
        send=http.post(f'/api/reply-actions/{aid}/claim-send',json={'task_id':tid,'worker_id':worker['id']},headers=helper._task_lease_headers(worker,claim))
    record('image-order-'+case,{'policy':policy,'brain_calls':len(calls),'claim_status':claim.status_code,
        'claim_body':claim.json(),'send_status':send.status_code if send is not None else None,'send_body':send.json() if send is not None else None})
    if case in {'during_generation','queued','segments'}:
        assert send is not None and send.status_code==200
        with SessionLocal() as db:
            actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
            assert actions[0].status=='sending'
            if case=='segments':assert actions[1].status=='queued'
    else:
        assert send is None or send.status_code>=400
    assert len(calls)==1


@pytest.mark.parametrize('kind',['duplicate','foreign'])
def test_order_rejects_duplicate_or_foreign_image_ids(http,monkeypatch,kind):
    monkeypatch.setattr(helper,'client',http)
    vid=helper._create_listed_vehicle()
    current=data(http.get(f'/api/vehicles/{vid}',headers=helper.HEADERS))['images'][0]['id']
    if kind=='duplicate':ids=[current,current]
    else:
        other=helper._create_listed_vehicle()
        ids=[data(http.get(f'/api/vehicles/{other}',headers=helper.HEADERS))['images'][0]['id']]
    response=http.put(f'/api/vehicles/{vid}/images/order',json={'image_ids':ids},headers=helper.HEADERS)
    assert response.status_code==(400 if kind=='duplicate' else 409),response.text
    if kind=='foreign':assert response.json()['code']=='VEHICLE_IMAGE_ORDER_INCOMPLETE'
    assert data(http.get(f'/api/vehicles/{vid}',headers=helper.HEADERS))['images'][0]['id']==current
