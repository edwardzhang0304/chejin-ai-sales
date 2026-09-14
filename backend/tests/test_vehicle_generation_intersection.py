"""Catalog editing during an in-flight Brain decision: no action/claim mocks.

Uses existing historical conversation fixture (explicit DB friend state),
synthetic completed customer text and controlled Brain process result.
Real BackgroundTasks scheduling and HTTP/PostgreSQL; no actual LLM or WeChat actions.
"""
import json
import os
from pathlib import Path
import time
import threading
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation,MessageBatch,ReplyAction,ReplyActionVehicleFact
from app.models.task import Task
from app.models.vehicle import KnowledgeItem
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
import test_c3_api as helper
from test_rule_intersections import http, data, record, pytestmark

@pytest.mark.parametrize('timing',['unchanged','before_brain_reads','during_brain','after_generation','unlist_during_brain','unrelated_during_brain','price_changed_back','retry_current','intermediate_price_then_restore'])
def test_vehicle_change_must_not_certify_an_old_price_as_current(http,monkeypatch,timing):
    from app.core.config import get_settings
    from app.api.routes import wechat as routes
    from fastapi import BackgroundTasks
    monkeypatch.setattr(get_settings(), "c3_ai_adapter_mode", "real")
    scheduled = []
    original_add_task = BackgroundTasks.add_task
    def observe_schedule(self, func, *args, **kwargs):
        scheduled.append(func.__name__)
        return original_add_task(self, func, *args, **kwargs)
    monkeypatch.setattr(BackgroundTasks, "add_task", observe_schedule)
    completed = threading.Event()
    original_generate = routes._generate_message_batch
    def observe_completion(*args, **kwargs):
        try:
            return original_generate(*args, **kwargs)
        finally:
            completed.set()
    monkeypatch.setattr(routes, "_generate_message_batch", observe_completion)
    if os.environ.get('AUDIT_DISABLE_AUTO_CALLBACK')=='1':
        from app.api.routes import wechat as routes
        monkeypatch.setattr(routes,'_generate_message_batch',lambda *args,**kwargs:None)
    monkeypatch.setattr(helper,'client',http)
    vehicle_id=helper._create_listed_vehicle()
    worker,binding=helper._setup_bound_conversation()
    # The shared historical fixture uses waiting_sales_reply without a handoff.
    # Use a valid historical active customer waiting for the next user message;
    # this is seed data only, never a generated batch/action/claim result.
    with SessionLocal() as db:
        conversation=db.get(Conversation,binding['conversation_id'])
        conversation.status='waiting_user_reply'
        db.commit()
    unrelated_id = helper._create_listed_vehicle() if timing == 'unrelated_during_brain' else None
    monkeypatch.setattr(get_settings(), 'c3_batch_retry_delay_seconds', 0)
    trace=[]
    def change():
        response=(http.post(f'/api/vehicles/{vehicle_id}/unlist') if timing=='unlist_during_brain'
                  else http.put(f'/api/vehicles/{vehicle_id}',json={'public_price':11.66}))
        changed=data(response)
        trace.append({'step':'vehicle_api_commit','price':changed['public_price'],'listing':changed['listing_status']})
    if timing=='before_brain_reads':change()
    def brain_boundary(**kwargs):
        # This price was correct when Brain obtained the catalog; no stale or
        # malformed output is injected before a real catalog update.
        if timing == 'intermediate_price_then_restore':
            change()
        with SessionLocal() as db:
            vehicle=db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id==vehicle_id))
            price=vehicle.payload['data']['price']
            initial_fingerprint=c3_service._vehicle_fact_fingerprint(db,vehicle)
            batches=list(db.scalars(select(MessageBatch)))
            actions=list(db.scalars(select(ReplyAction)))
            trace.append({'step':'brain_read','price':price,'fingerprint':initial_fingerprint,'batches':[b.status for b in batches],'action_count':len(actions)})
            assert len(batches)==1 and batches[0].status=='generating'
            assert len(actions)==0
        first_attempt = sum(step['step'] == 'brain_read' for step in trace) == 1
        if timing in {'during_brain','unlist_during_brain','price_changed_back','retry_current'} and first_attempt:
            change()
            if timing == 'price_changed_back':
                data(http.put(f'/api/vehicles/{vehicle_id}', json={'public_price':12.88}))
        if timing == 'intermediate_price_then_restore':
            data(http.put(f'/api/vehicles/{vehicle_id}', json={'public_price':12.88}))
        if unrelated_id:
            data(http.put(f'/api/vehicles/{unrelated_id}', json={'public_price':11.66}))
        reply=f'这款车目前在售，公开售价是{price}万元。'
        return {'rule_name':'customer_service_brain_reply','adoptable':True,'visible_reply_source':'brain_plan.reply_segments','reply_text':reply,'guard_verdict':'pass',
                'brain_plan':{'recommended_action':'send_reply','confidence':0.95,'evidence_used':{'product_ids':[vehicle_id]},'evidence_refs':[f'product_master:{vehicle_id}'],
                              'facts_claimed':[{'fact_type':'price','value':f'{price}万元','source_level':'product_master','source_id':vehicle_id}], 'reply_segments':[reply]}}
    adapter=RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter,'_load_config',lambda:{'customer_service_brain':{'provider':'openai','api_key':'isolated-test-only','model':'controlled-brain'}})
    monkeypatch.setattr(adapter,'_run_brain_isolated',brain_boundary)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:adapter)
    helper._ingest(worker,binding['conversation_id'],'vehicle-rule-'+timing,'这款车现在多少钱？')
    assert completed.wait(5), "automatic BackgroundTasks generation did not execute"
    assert len(scheduled) == 1, scheduled
    # Check automatic production callback FIRST; never call _generate or insert
    # an action/task to make the expected outcome appear.
    with SessionLocal() as db:
        batches=list(db.scalars(select(MessageBatch)));actions=list(db.scalars(select(ReplyAction)))
        assert len(batches)==1
        batch_state={'status':batches[0].status,'error_code':batches[0].error_code,'response':batches[0].ai_response_snapshot}
        generated={'batch_id':batches[0].id,'batch':batch_state,'action_count':len(actions)}
        if actions:
            assert len(actions)==1
            a=actions[0];task=db.scalar(select(Task).where(Task.reply_action_id==a.id))
            generated.update(action_id=a.id,task_id=task.id,reply=a.reply_text,action_status=a.status)
    if not trace:
        record('vehicle-callback-missing',generated)
    assert trace,generated
    if timing=='after_generation':change()
    claim=None;send=None;stored=None
    if generated.get('task_id'):
        claim=http.post(f"/api/tasks/{generated['task_id']}/claim",json={'worker_id':worker['id'],'current_step':'chat_reply_claimed','claim_source':'c2_conversation_flow','conversation_id':binding['conversation_id']},headers=helper._worker_headers(worker))
        if claim.status_code==200:
            send=http.post(f"/api/reply-actions/{generated['action_id']}/claim-send",json={'task_id':generated['task_id'],'worker_id':worker['id']},headers=helper._task_lease_headers(worker,claim))
        with SessionLocal() as db:
            vehicle=db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id==vehicle_id))
            fact=db.get(ReplyActionVehicleFact,{'reply_action_id':generated['action_id'],'vehicle_id':vehicle_id})
            action=db.get(ReplyAction,generated['action_id'])
            stored={'catalog_price':vehicle.payload['data']['price'],'actual_fingerprint':c3_service._vehicle_fact_fingerprint(db,vehicle),'action_fingerprint':fact.fact_fingerprint if fact else None,'action_status':action.status,'send_token_issued':bool(action.send_token),'reply':action.reply_text}
    record('vehicle-'+timing,{'trace':trace,'generated':generated,'claim':{'status':claim.status_code,'body':claim.json()} if claim is not None else None,'send':{'status':send.status_code,'body':send.json()} if send is not None else None,'stored':stored,'boundary':'Real backend routes and real adapter/context bridge; only Brain process result and its config are controlled; synthetic input, PostgreSQL, loopback HTTP, one actual BackgroundTasks call; no Windows/no physical send/no actual model'})
    if timing in {'unchanged','before_brain_reads','unrelated_during_brain'}:
        assert send is not None and send.status_code==200,generated
        assert str(stored['catalog_price']) in generated['reply']
    else:
        assert send is None or send.status_code>=400,'outdated price passed final claim-send gate and obtained a send token'

    if timing == 'retry_current':
        assert generated['action_count'] == 0 and generated['batch']['status'] == 'retry_wait'
        assert generated['batch']['error_code'] == 'REPLY_ACTION_VEHICLE_FACT_STALE'
        completed.clear()
        polled = http.get(f"/api/workers/{worker['id']}/wechat/message-batches/{generated['batch_id']}", headers=helper._worker_headers(worker))
        data(polled)
        assert completed.wait(5), 'normal Worker polling did not automatically retry generation'
        assert len(scheduled) == 2
        with SessionLocal() as db:
            actions = list(db.scalars(select(ReplyAction)))
            reply_tasks = list(db.scalars(select(Task).where(Task.task_type == 'chat_reply')))
            assert len(actions) == len(reply_tasks) == 1
            action, task = actions[0], reply_tasks[0]
            assert '11.66' in action.reply_text and '12.88' not in action.reply_text
            assert task.reply_action_id == action.id
            action_id, task_id = action.id, task.id
            retry_evidence = {'reply': action.reply_text, 'scheduled': scheduled,
                              'attempt_count': db.get(MessageBatch, generated['batch_id']).generation_attempt_count}
        claim_response = http.post(f'/api/tasks/{task_id}/claim', json={'worker_id':worker['id'], 'claim_source':'c2_conversation_flow', 'conversation_id':binding['conversation_id']}, headers=helper._worker_headers(worker))
        data(claim_response)
        send_response = http.post(f'/api/reply-actions/{action_id}/claim-send', json={'worker_id':worker['id'], 'task_id':task_id}, headers=helper._task_lease_headers(worker, claim_response))
        data(send_response)
        retry_evidence['claim_send_status'] = send_response.status_code
        record('vehicle-retry-current-completed', retry_evidence)
