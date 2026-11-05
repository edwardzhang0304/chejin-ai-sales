"""Ordinary internal generation uses durable claims; no second scheduler or force policy."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select
import test_c3_api as api
from app.core.config import get_settings
from app.models.c3 import Conversation, MessageBatch, HandoffEvent, ReplyAction
from app.services import c3_service as service
from app.services.ai_adapter import AIEngineDecision


@pytest.fixture
def pending(monkeypatch):
    api.setup_function()
    calls=[]
    class Provider:
        def generate_reply_decision(self, **kwargs):
            calls.append(deepcopy(kwargs))
            return AIEngineDecision(decision='retry_later', guard_result='failed',
                error_code='AI_ENGINE_REVIEW_UNAVAILABLE', suggested_action='retry_later',
                raw_payload={'omniauto_brain_result':{'model_call_accounting':{
                    'failure_category':'service_unavailable', 'logical_call_count':2,
                    'http_attempt_count':2}}})
    provider=Provider()
    monkeypatch.setattr(service,'get_ai_engine_adapter',lambda:provider)
    worker,binding=api._setup_bound_conversation()
    mid=api._ingest(worker,binding['conversation_id'],'synthetic-claim-one','想了解购车需求')
    bid=api._collect(binding['conversation_id'],mid)['batch_id']
    assert len(calls)==1
    return worker,binding,bid,provider,calls


def due_clock(monkeypatch,bid):
    with api.SessionLocal() as db:
        batch=db.get(MessageBatch,bid)
        clock=batch.generated_at+timedelta(seconds=get_settings().c3_batch_retry_delay_seconds+1)
    monkeypatch.setattr(service,'utcnow',lambda:clock)
    return clock


def generate(bid, **kwargs):
    with api.SessionLocal() as db:
        result=service.generate_for_batch(db,batch_id=bid,**kwargs)
        db.commit()
        return result


def test_inflight_duplicate_and_other_customer_do_not_share_attempt(monkeypatch,pending):
    worker,binding,bid,provider,calls=pending
    api._create_lead(name='另一位合成客户',phone='13800009999',remark_code='C3OTHER2')
    other=api._scan(worker,remark_code='C3OTHER2',rpa_session_key='wx-c3-row-002',scan_id='scan-c3-002')
    assert other['conversation_id'] != binding['conversation_id']
    with api.SessionLocal() as db:
        conversation=db.get(Conversation,other['conversation_id'])
        conversation.friend_state='friend_active';conversation.status='waiting_sales_reply';db.commit()
    mid=api._ingest(worker,other['conversation_id'],'synthetic-claim-two','想了解购车需求')
    other_bid=api._collect(other['conversation_id'],mid)['batch_id']
    assert other_bid != bid
    clock=due_clock(monkeypatch,other_bid)
    entered,release=Event(),Event()
    original=provider.generate_reply_decision
    def blocking(**kwargs):
        if kwargs['message_batch']['id']==bid:
            entered.set()
            assert release.wait(10), 'test did not release synthetic provider'
        return original(**kwargs)
    monkeypatch.setattr(provider,'generate_reply_decision',blocking)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(generate,bid)
        try:
            assert entered.wait(10), future.result(timeout=1) if future.done() else "provider not reached"
            with api.SessionLocal() as db:
                saved=db.get(MessageBatch,bid)
                assert saved.generation_attempt_count==2 and saved.status=='generating'
                snapshot=deepcopy(saved.ai_response_snapshot)
            duplicate=api._generate(bid)
            assert duplicate['batch']['generation_attempt_count']==2
            assert duplicate['batch']['status']=='generating'
            assert len(calls)==2
            stale=generate(bid,expected_generation_attempt=1)
            assert stale['error_code']=='MESSAGE_BATCH_GENERATION_CLAIM_STALE'
            with api.SessionLocal() as db:
                assert db.get(MessageBatch,bid).ai_response_snapshot==snapshot
            # A separate customer may proceed while the first provider is held.
            second=api._generate(other_bid)
            assert second['decision']=='handoff' and len(calls)==3
        finally:
            release.set()
        assert future.result(timeout=10)['decision']=='handoff'
    assert len(calls)==4
    with api.SessionLocal() as db:
        for current in (bid,other_bid):
            saved=db.get(MessageBatch,current)
            assert saved.generation_attempt_count==2
            totals=saved.ai_response_snapshot['generation_accounting_totals']
            assert totals['recorded_attempt_count']==2 and totals['logical_call_count']==4
            assert len(list(db.scalars(select(HandoffEvent).where(HandoffEvent.batch_id==current))))==1
            assert not list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==current)))


def test_committed_claim_survives_new_session_and_exhausts_without_extra_provider(monkeypatch,pending):
    _,_,bid,_,calls=pending
    clock=due_clock(monkeypatch,bid)
    with api.SessionLocal() as db:
        claim=service.claim_message_batch_generation(db,batch_id=bid)
        assert claim['run'] and claim['attempt']==2
        db.commit()
    # No in-memory owner is needed: a new request sees the committed claim.
    current=api._generate(bid)
    assert current['batch']['status']=='generating' and len(calls)==1
    expired=clock+timedelta(seconds=get_settings().c3_batch_stale_after_seconds+1)
    monkeypatch.setattr(service,'utcnow',lambda:expired)
    terminal=api._generate(bid)
    assert terminal['decision']=='handoff' and len(calls)==1
    repeated=api._generate(bid)
    assert repeated['handoff_event_id']
    with api.SessionLocal() as db:
        saved=db.get(MessageBatch,bid)
        assert saved.generation_attempt_count==2
        assert len(list(db.scalars(select(HandoffEvent).where(HandoffEvent.batch_id==bid))))==1


def assert_force_rejected(bid, call_count):
    """All three real entry points reject before touching durable state or model."""
    from app.errors import AppError
    from app.models.task import Task
    def snapshot():
        with api.SessionLocal() as db:
            return {model.__tablename__:[{column.name:deepcopy(getattr(row,column.name))
                    for column in model.__table__.columns}
                    for row in db.scalars(select(model).order_by(model.__table__.primary_key.columns.values()[0]))]
                    for model in (MessageBatch,ReplyAction,HandoffEvent,Task)}
    before=snapshot();count=call_count()
    response=api.client.post(f'/api/internal/message-batches/{bid}/generate',
        json={'force':True},headers=api.INTERNAL_HEADERS)
    assert response.status_code==400
    assert 'MESSAGE_BATCH_FORCE_GENERATION_DISABLED' in response.text
    for entry in (service.claim_message_batch_generation,service.generate_for_batch):
        with api.SessionLocal() as db:
            with pytest.raises(AppError) as rejected:
                entry(db,batch_id=bid,force=True)
            assert rejected.value.code=='MESSAGE_BATCH_FORCE_GENERATION_DISABLED'
            assert rejected.value.status_code==400
            db.commit()
    # Holding an expected attempt does not authorize force either.
    with api.SessionLocal() as db:
        with pytest.raises(AppError) as rejected:
            service.generate_for_batch(db,batch_id=bid,force=True,expected_generation_attempt=1)
        assert rejected.value.code=='MESSAGE_BATCH_FORCE_GENERATION_DISABLED'
        db.commit()
    assert snapshot()==before and call_count()==count


@pytest.mark.parametrize('state',['retry_wait','generating','exhausted','human'])
def test_force_is_rejected_without_mutation_in_each_pending_state(monkeypatch,pending,state):
    _,binding,bid,_,calls=pending
    if state!='retry_wait':
        with api.SessionLocal() as db:
            row=db.get(MessageBatch,bid)
            if state=='generating':
                row.status='generating';row.generation_started_at=service.utcnow()
            elif state=='exhausted':
                row.generation_attempt_count=get_settings().c3_batch_recovery_max_attempts
            else:
                db.get(Conversation,binding['conversation_id']).status='waiting_sales_reply'
            db.commit()
    assert_force_rejected(bid,lambda:len(calls))


@pytest.mark.parametrize('state',['collecting','retry_wait'])
def test_restored_exhausted_ordinary_batch_cannot_start_an_extra_attempt(monkeypatch,pending,state):
    _,_,bid,_,calls=pending
    clock=due_clock(monkeypatch,bid)
    with api.SessionLocal() as db:
        row=db.get(MessageBatch,bid)
        row.status=state
        row.generation_attempt_count=get_settings().c3_batch_recovery_max_attempts
        db.commit()
    result=api._generate(bid)
    assert result['decision']=='handoff' and len(calls)==1
    repeated=api._generate(bid)
    assert repeated['handoff_event_id'] and len(calls)==1
    with api.SessionLocal() as db:
        row=db.get(MessageBatch,bid)
        assert row.generation_attempt_count==get_settings().c3_batch_recovery_max_attempts
        assert len(list(db.scalars(select(HandoffEvent).where(HandoffEvent.batch_id==bid))))==1
