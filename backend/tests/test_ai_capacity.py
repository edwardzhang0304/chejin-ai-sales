"""Real cross-process SQLite capacity, process death, migration and C3 integration."""
from dataclasses import replace
from datetime import timedelta
import importlib.util,json,os,subprocess,sys,uuid
from pathlib import Path
import pytest
from sqlalchemy import create_engine,select,func,text,inspect
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.database import Base,SessionLocal,engine as business_engine
from app.models.c3 import Conversation,MessageBatch,ReplyAction
from app.models.ai_capacity import AIGenerationSlot
from app.models.base import utcnow
from app.services.ai_capacity_service import acquire_call_slot,release_call_slot,HOST
from app.services import c3_service


def seed(engine,n):
    values=[]
    with Session(engine) as db:
        for i in range(n):
            conversation,batch=str(uuid.uuid4()),str(uuid.uuid4())
            db.add(Conversation(conversation_id=conversation));db.add(MessageBatch(id=batch,conversation_id=conversation))
            values.append((conversation,batch))
        db.commit()
    return values


@pytest.fixture(params=['sqlite','postgres'])
def capacity_db(tmp_path,request):
    if request.param=='postgres':
        uri=os.environ.get('HOOK_TEST_POSTGRES_URL')
        if not uri:pytest.skip('Explicit local PostgreSQL fixture not supplied')
        assert uri.startswith('postgresql+psycopg://postgres@127.0.0.1:') and uri.endswith('/hook_test')
        engine=create_engine(uri)
        Base.metadata.drop_all(engine)
    else:
        # The business fixture may use PostgreSQL, which registers shared models
        # with a schema. SQLite's separate capacity database uses the same full
        # metadata without PostgreSQL namespaces; do not omit any tables/tests.
        engine=create_engine(f'sqlite:///{tmp_path}/global-capacity.sqlite3').execution_options(
            schema_translate_map={'wechat_ai_customer_service':None})
    Base.metadata.create_all(engine)
    try:yield engine,seed(engine,25)
    finally:engine.dispose()


def acquire(engine,pair,limit=20):
    return acquire_call_slot(engine,conversation_id=pair[0],batch_id=pair[1],generation_attempt=1,limit=limit)


def child(engine,pair,limit):
    actor=Path(__file__).with_name('ai_capacity_process_actor.py')
    return subprocess.Popen([sys.executable,str(actor),str(engine.url),*pair,str(limit)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ))


def finish(process):
    if process.poll() is None:process.communicate('\n',timeout=10)
    assert process.returncode==0


def test_default_twenty_slots_are_global_and_twenty_first_other_process_waits(capacity_db):
    engine,values=capacity_db
    slots=[acquire(engine,pair) for pair in values[:20]]
    assert all(slots)
    other=child(engine,values[20],20)
    result=json.loads(other.stdout.readline());finish(other)
    assert result['acquired'] is False
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(AIGenerationSlot))==20
    assert release_call_slot(engine,slots.pop())
    other=child(engine,values[20],20)
    try:
        result=json.loads(other.stdout.readline());assert result['acquired']
        with Session(engine) as db:assert db.scalar(select(func.count()).select_from(AIGenerationSlot))==20
    finally:finish(other)
    for slot in slots:assert release_call_slot(engine,slot)


def test_same_customer_and_living_old_call_cannot_be_reclaimed(capacity_db):
    engine,values=capacity_db
    first=acquire(engine,values[0],3);assert first
    with Session(engine) as db:
        row=db.get(AIGenerationSlot,first.token);row.acquired_at=utcnow()-timedelta(days=365);db.commit()
    other=child(engine,values[0],3)
    result=json.loads(other.stdout.readline());finish(other)
    assert not result['acquired']
    assert acquire(engine,values[0],3) is None
    assert not release_call_slot(engine,replace(first,token=str(uuid.uuid4())))
    assert release_call_slot(engine,first)


def test_dead_same_host_owner_is_recovered_without_age_expiration(capacity_db):
    engine,values=capacity_db
    other=child(engine,values[0],1)
    result=json.loads(other.stdout.readline());assert result['acquired']
    assert acquire(engine,values[1],1) is None
    other.terminate();other.communicate(timeout=10)
    recovered=acquire(engine,values[1],1);assert recovered
    with Session(engine) as db:assert db.get(AIGenerationSlot,result['token']) is None
    release_call_slot(engine,recovered)


def test_uninspectable_foreign_host_is_not_automatically_reclaimed(capacity_db):
    engine,values=capacity_db
    slot=acquire(engine,values[0],1)
    with Session(engine) as db:
        row=db.get(AIGenerationSlot,slot.token);row.owner_host='f'*64;row.owner_pid=999999999
        row.acquired_at=utcnow()-timedelta(days=365);db.commit()
    assert acquire(engine,values[1],1) is None
    assert release_call_slot(engine,slot)


def test_migration_preserves_batches_and_refuses_drop_of_live_owner(tmp_path):
    path=Path(__file__).parents[1]/'alembic/versions/20261012_0040_ai_capacity.py'
    spec=importlib.util.spec_from_file_location('ai_capacity_migration',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    engine=create_engine(f'sqlite:///{tmp_path}/migration.sqlite3')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE conversations (conversation_id VARCHAR(36) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE message_batches (id VARCHAR(36) PRIMARY KEY)'))
        conn.execute(text("INSERT INTO message_batches VALUES ('old-batch')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert conn.execute(text('SELECT generation_capacity_wait_count FROM message_batches')).scalar_one()==0
            conn.execute(text("INSERT INTO ai_generation_slots VALUES ('token','conv','old-batch',1,'host',123,'start','2026-10-12')"))
            with pytest.raises(RuntimeError,match='live generation'):module.downgrade()
            conn.execute(text('DELETE FROM ai_generation_slots'));module.downgrade()
            assert conn.execute(text('SELECT id FROM message_batches')).scalar_one()=='old-batch'
            assert 'ai_generation_slots' not in inspect(conn).get_table_names()


def test_c3_queued_capacity_preserves_dispatch_fence_and_failure_retry_budget(monkeypatch):
    from test_c3_api import setup_function,_setup_bound_conversation,_ingest,_collect
    from app.services.ai_adapter import AIEngineDecision
    setup_function()
    from app.services import ai_capacity_service
    original=ai_capacity_service.acquire_call_slot
    monkeypatch.setattr(ai_capacity_service,'acquire_call_slot',lambda *args,**kwargs:None)
    worker,binding=_setup_bound_conversation()
    with SessionLocal() as db:
        db.get(Conversation,binding['conversation_id']).status='ai_active';db.commit()
    mid=_ingest(worker,binding['conversation_id'],'capacity-queue-test','预算15万元，请帮我看看')
    batch=_collect(binding['conversation_id'],mid)
    with SessionLocal() as db:
        claim=c3_service.claim_message_batch_generation(db,batch_id=batch['batch_id']);db.commit()
    with SessionLocal() as db:
        row=db.get(MessageBatch,batch['batch_id'])
        baseline_generation=row.generation_attempt_count-1
        baseline_wait=row.generation_capacity_wait_count
    for index in range(4):
        with SessionLocal() as db:
            if index:
                claim=c3_service.claim_message_batch_generation(db,batch_id=batch['batch_id']);db.commit()
            result=c3_service.generate_for_batch(db,batch_id=batch['batch_id'],expected_generation_attempt=claim['attempt']);db.commit()
            row=db.get(MessageBatch,batch['batch_id'])
            assert result['decision']=='queued' and row.status=='collecting'
            assert row.generation_capacity_wait_count==baseline_wait+index+1 and row.generation_attempt_count==baseline_generation+index+1
            assert row.error_code is None and db.scalar(select(func.count()).select_from(ReplyAction))==0
    monkeypatch.setattr(ai_capacity_service,'acquire_call_slot',original)
    calls=[]
    class Provider:
        def generate_reply_decision(self,**kwargs):
            calls.append(kwargs)
            raise RuntimeError('synthetic Provider failure')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Provider)
    with SessionLocal() as db:
        claim=c3_service.claim_message_batch_generation(db,batch_id=batch['batch_id']);db.commit()
        result=c3_service.generate_for_batch(db,batch_id=batch['batch_id'],expected_generation_attempt=claim['attempt']);db.commit()
        row=db.get(MessageBatch,batch['batch_id'])
        assert len(calls)==1 and result['decision']=='retry_later'
        assert row.status=='retry_wait' and row.generation_attempt_count==baseline_generation+5 and row.generation_capacity_wait_count==baseline_wait+4
        assert db.scalar(select(func.count()).select_from(AIGenerationSlot))==0
