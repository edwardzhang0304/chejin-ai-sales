"""Synthetic customer fixtures; real routes/storage, no mocked settlement.

The PostgreSQL tests require an explicitly isolated test database. SQLite
coverage is not used as evidence of concurrent row locking.
"""
from datetime import timedelta
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.core.database import Base, engine, SessionLocal
from app.models.base import utcnow
from app.models.lead import Lead
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from app.models.c3 import Conversation, MessageBatch, ReplyAction, HandoffEvent
from app.models.audit import OperationLog
from app.models.task import Task
from app.schemas.lead import MarkInvalidRequest
from app.services import lead_service, wechat_service
from app.services.followup_eligibility import require_followup
from app.errors import AppError

client = TestClient(app)


@pytest.fixture
def http_api():
    import socket, requests, uvicorn
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.02)
    assert server.started
    session=requests.Session()
    class LocalHTTP:
        def get(self,path,**kwargs):return session.get(f'http://127.0.0.1:{port}'+path,timeout=10,**kwargs)
        def post(self,path,**kwargs):return session.post(f'http://127.0.0.1:{port}'+path,timeout=10,**kwargs)
    try:yield LocalHTTP()
    finally:
        session.close();server.should_exit=True;thread.join(5);sock.close()


@pytest.fixture(autouse=True)
def isolated_db():
    assert 'test' in str(engine.url.database), 'Never run against business data'
    Base.metadata.drop_all(engine)
    if engine.dialect.name == "postgresql":
        from sqlalchemy.schema import CreateSchema
        with engine.begin() as connection:
            for schema in {table.schema for table in Base.metadata.tables.values() if table.schema}:
                connection.execute(CreateSchema(schema, if_not_exists=True))
    Base.metadata.create_all(engine)


def fixture_rows():
    """Persist a historical listening customer and another unaffected customer."""
    response = client.post('/api/workers', json={'worker_name':'Followup synthetic Worker','enabled':True})
    assert response.status_code == 200, response.text
    w = response.json()['data']
    response = client.post(f"/api/workers/{w['id']}/client-bind", json={
        'worker_token': w['worker_token'], 'client_instance_id':'followup-test'})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        worker = db.get(Worker,w['id'])
        worker.run_status='running'
        worker.online_status='online'
        worker.rpa_component_status='ready'
        worker.wechat_status='logged_in'
        worker.last_heartbeat_at=utcnow()
        values=[]
        for index in range(2):
            lead=Lead(customer_name='Synthetic',status='assigned',source_type='manual',source_name_snapshot='test',created_by='test',updated_by='test')
            db.add(lead);db.flush()
            conversation=Conversation(lead_id=lead.id,worker_id=worker.id,status='waiting_sales_reply')
            db.add(conversation);db.flush()
            db.add(HandoffEvent(conversation_id=conversation.conversation_id,handoff_reason_code="AI_ENGINE_RETRY_EXHAUSTED",notify_status="succeeded"))
            binding=WechatSessionBinding(conversation_id=conversation.conversation_id,lead_id=lead.id,
                worker_id=worker.id,remark_code=['CJ3N95EU','CJDZSKVN'][index],display_name='Synthetic',
                rpa_session_key=f'test-{index}',row_fingerprint=f'row-{index}',bind_status='bound',
                listen_status='listening',allow_listening=True,last_read_conversation_status='waiting_sales_reply',next_read_due_at=utcnow()-timedelta(minutes=5))
            db.add(binding);db.flush()
            values.append({'lead_id':lead.id,'conversation_id':conversation.conversation_id,'binding_id':binding.id})
        db.commit()
    return w,values


def headers(w,flow=None):
    result={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':'followup-test'}
    if flow:result['X-Inflight-Flow-Id']=flow
    return result


def invalidate(lead_id):
    r=client.post(f'/api/leads/{lead_id}/mark-invalid',json={'invalid_reason':'test_data'})
    assert r.status_code==200,r.text
    return r


@pytest.mark.parametrize('historical_invalid',[False,True])
def test_real_http_invalid_never_dispatched_and_cannot_be_claimed(http_api,historical_invalid,tmp_path):
    import json,os
    from pathlib import Path
    w,rows=fixture_rows();task_ids=[]
    with SessionLocal() as db:
        for row in rows:
            task=Task(lead_id=row['lead_id'],worker_id=w['id'],task_type='add_friend',status='pending')
            db.add(task);db.flush();task_ids.append(task.id)
        db.commit()
    before=http_api.get(f"/api/workers/{w['id']}/tasks/pull",headers=headers(w))
    assert before.status_code==200,before.text
    assert before.json()["data"]["task"]["id"]==task_ids[0]
    before_targets=http_api.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
    assert {x['conversation_id'] for x in before_targets.json()['data']['targets']}=={x['conversation_id'] for x in rows}
    if historical_invalid:
        # Explicit historical fixture: leave its task pending and binding
        # listening to prove filtering does not depend on data repair.
        with SessionLocal() as db:db.get(Lead,rows[0]['lead_id']).status='invalid';db.commit()
    else:
        invalid=http_api.post(f"/api/leads/{rows[0]['lead_id']}/mark-invalid",json={'invalid_reason':'test_data'})
        assert invalid.status_code==200,invalid.text
    rounds=[]
    for iteration in range(10):
        pulled=http_api.get(f"/api/workers/{w['id']}/tasks/pull",headers=headers(w))
        targets=http_api.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
        assert pulled.status_code==targets.status_code==200
        task=pulled.json()['data']['task']
        selected=[x['conversation_id'] for x in targets.json()['data']['targets']]
        assert task['id']==task_ids[1] and task['lead_id']==rows[1]['lead_id'],task
        assert selected==[rows[1]['conversation_id']],selected
        rounds.append({'round':iteration+1,'task_id':task['id'],'lead_id':task['lead_id'],'read_conversation_ids':selected})
    denied=http_api.post(f"/api/tasks/{task_ids[0]}/claim",json={'worker_id':w['id']},headers=headers(w))
    assert denied.status_code==409 and denied.json()['code']=='LEAD_INVALID',denied.text
    authorization=http_api.get(f"/api/workers/{w['id']}/wechat/conversations/{rows[0]['conversation_id']}/read-authorization",headers=headers(w))
    assert authorization.status_code==200 and authorization.json()['data']['allowed'] is False
    assert authorization.json()['data']['error_code']=='LEAD_INVALID'
    with SessionLocal() as db:
        invalid_task=db.get(Task,task_ids[0]);valid_task=db.get(Task,task_ids[1])
        after={'invalid_lead_status':db.get(Lead,rows[0]['lead_id']).status,
               'invalid_task_status':invalid_task.status,'invalid_task_claimed_at':str(invalid_task.claimed_at),
               'valid_task_status':valid_task.status,'invalid_binding_listen_status':db.get(WechatSessionBinding,rows[0]['binding_id']).listen_status}
        assert invalid_task.claimed_at is None and valid_task.status=='pending'
        assert invalid_task.status==('pending' if historical_invalid else 'cancelled')
    evidence={'fixture':'Synthetic customers and tasks; production HTTP routes, real Worker authentication, real database; no Windows UI or production data',
              'database':engine.dialect.name,'historical_invalid':historical_invalid,'before':{'rows':rows,'task_ids':task_ids,'initial_read_count':2},
              'dispatch_rounds':rounds,'direct_claim':{'http_status':denied.status_code,'code':denied.json()['code']},
              'authorization':{'allowed':False,'error_code':'LEAD_INVALID'},'after':after}
    output=Path(os.environ.get('CHEJIN_FOLLOWUP_EVIDENCE_DIR',str(tmp_path)))
    output.mkdir(parents=True,exist_ok=True)
    (output/f'dispatch-{engine.dialect.name}-historical-{historical_invalid}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))


def test_invalid_stops_repeated_dispatch_preserves_other_customer_and_configuration():
    w,rows=fixture_rows()
    with SessionLocal() as db:
        original=db.get(WechatSessionBinding,rows[0]['binding_id']).authorization_revision
    invalidate(rows[0]['lead_id']);invalidate(rows[0]['lead_id'])
    for _ in range(3):
        response=client.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
        assert response.status_code==200,response.text
        assert {x['remark_code'] for x in response.json()['data']['targets']}=={'CJDZSKVN'}
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,rows[0]['binding_id'])
        assert binding.authorization_revision==original+1
        assert binding.allow_listening and binding.listen_status=='listening'
        assert db.get(Conversation,rows[0]['conversation_id']).status=='waiting_sales_reply'
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.event_type=='lead_followup_revoked'))==1


def test_historical_invalid_is_filtered_without_reconciliation():
    w,rows=fixture_rows()
    with SessionLocal() as db:
        db.get(Lead,rows[0]['lead_id']).status='invalid';db.commit()
    response=client.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
    assert response.status_code==200,response.text
    assert all(x['conversation_id']!=rows[0]['conversation_id'] for x in response.json()['data']['targets'])


def test_batch_invalid_uses_same_service_and_does_not_revoke_twice():
    w,rows=fixture_rows()
    from app.services.lead_service import batch_mark_invalid
    for _ in range(2):
        with SessionLocal() as db:
            result=batch_mark_invalid(db,[row['lead_id'] for row in rows],MarkInvalidRequest(invalid_reason='test_data'),actor())
            assert result['succeeded']==2
            db.commit()
    with SessionLocal() as db:
        assert [db.get(WechatSessionBinding,row['binding_id']).authorization_revision for row in rows]==[2,2]
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.event_type=='lead_followup_revoked'))==2


def test_binding_conversation_conflict_cannot_reassign_or_bypass_invalidity():
    w,rows=fixture_rows()
    with SessionLocal() as db:
        db.get(Conversation,rows[0]['conversation_id']).lead_id=rows[1]['lead_id'];db.commit()
    response=client.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
    assert response.status_code==409
    assert response.json()['code']=='MESSAGE_TARGET_IDENTITY_MISMATCH'
    with SessionLocal() as db:assert db.get(Conversation,rows[0]['conversation_id']).lead_id==rows[1]['lead_id']


def test_legacy_069_contract_is_not_a_supported_mixed_rollout():
    from test_wechat_c2_api import _v3_ingest_payload
    w,rows=fixture_rows();row=rows[0]
    payload=_v3_ingest_payload({'id':row['binding_id'],'conversation_id':row['conversation_id']},'CJ3N95EU',read_run_id='legacy-contract-read',messages=[])
    # Immutable evidence: machine contract from source commit 221bcdf,
    # bundled with 0.9.69; this is not a claim about a Windows EXE test.
    payload['contract_revision']='0.9.68'
    payload['contract_sha256']='5575934ae7c39fef08c7bc0c7bd02e0b321907c55eefd5096cd679ecc4aebd48'
    response=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",json=payload,headers=headers(w))
    assert response.status_code==409
    assert response.json()['code']=='MESSAGE_CONTRACT_REVISION_MISMATCH'


def test_revoked_flow_cancels_and_other_customer_can_start():
    w,rows=fixture_rows();flow='read-followup-synthetic'
    start={'flow_id':flow,'flow_kind':'c2_read','conversation_id':rows[0]['conversation_id'],'unread_generation':0}
    response=client.post(f"/api/workers/{w['id']}/inflight-flow/start",json=start,headers=headers(w))
    assert response.status_code==200,response.text
    invalidate(rows[0]['lead_id'])
    authorization=client.get(f"/api/workers/{w['id']}/wechat/conversations/{rows[0]['conversation_id']}/read-authorization",headers=headers(w,flow))
    assert authorization.status_code==200,authorization.text
    assert authorization.json()['data']['allowed'] is False
    assert authorization.json()['data']['error_code']=='LEAD_INVALID'
    denied=client.post(f"/api/workers/{w['id']}/inflight-flow/start",json=start,headers=headers(w,flow))
    assert denied.status_code==409 and denied.json()['code']=='LEAD_INVALID'
    finish={'flow_id':flow,'terminal_kind':'read_cancelled','conversation_id':rows[0]['conversation_id'],'error_code':'LEAD_INVALID'}
    response=client.post(f"/api/workers/{w['id']}/inflight-flow/finish",json=finish,headers=headers(w,flow))
    assert response.status_code==200,response.text
    start.update(flow_id='read-other',conversation_id=rows[1]['conversation_id'])
    assert client.post(f"/api/workers/{w['id']}/inflight-flow/start",json=start,headers=headers(w)).status_code==200


def test_cancellation_without_revocation_is_rejected():
    w,rows=fixture_rows();flow='read-not-revoked'
    start={'flow_id':flow,'flow_kind':'c2_read','conversation_id':rows[0]['conversation_id'],'unread_generation':0}
    assert client.post(f"/api/workers/{w['id']}/inflight-flow/start",json=start,headers=headers(w)).status_code==200
    response=client.post(f"/api/workers/{w['id']}/inflight-flow/finish",json={**start,'terminal_kind':'read_cancelled','error_code':'LEAD_INVALID'},headers=headers(w,flow))
    assert response.status_code==409


@pytest.mark.parametrize('restore_before_settlement',[False,True])
def test_original_read_facts_settle_but_old_ticket_never_starts_new_flow(restore_before_settlement):
    from test_wechat_c2_api import _v3_ingest_payload, _v3_message
    from app.models.wechat import MessageEvent
    w,rows=fixture_rows();row=rows[0];flow='synthetic-original-read'
    payload=_v3_ingest_payload({'id':row['binding_id'],'conversation_id':row['conversation_id']},'CJ3N95EU',
        read_run_id=flow,messages=[_v3_message('synthetic-old-action',role='customer',message_type='text',content='测试已读取事实',screen_order=1)])
    old_revision=payload['authorization_revision']
    start={'flow_id':flow,'flow_kind':'c2_read','conversation_id':row['conversation_id'],'unread_generation':0,'authorization_revision':old_revision}
    r=client.post(f"/api/workers/{w['id']}/inflight-flow/start",json=start,headers=headers(w));assert r.status_code==200,r.text
    invalidate(row['lead_id'])
    if restore_before_settlement:assert client.post(f"/api/leads/{row['lead_id']}/restore").status_code==200
    for _ in range(2):
        r=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",json=payload,headers=headers(w,flow))
        assert r.status_code==200,r.text
        assert r.json()['data']['state_transition_applied'] is False
        assert r.json()['data']['read_completion']['result']=='cancelled'
    r=client.post(f"/api/workers/{w['id']}/inflight-flow/finish",json={
        'flow_id':flow,'terminal_kind':'read_cancelled','conversation_id':row['conversation_id'],'error_code':'LEAD_INVALID'},headers=headers(w,flow))
    assert r.status_code==200,r.text
    r=client.post(f"/api/workers/{w['id']}/inflight-flow/start",json={**start,'flow_id':'old-ticket-new-flow'},headers=headers(w))
    assert r.status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id']))==1
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.scalar(select(func.count()).select_from(ReplyAction))==0


def test_revoked_media_uses_original_settlement_then_finishes_without_new_reply():
    import hashlib
    from test_wechat_c2_api import _fact_settlement_payload, _v3_failed_image_message
    from app.models.wechat import MessageEvent, WechatRecoverySettlement
    from app.services.followup_eligibility import token_for_revision

    w, rows = fixture_rows()
    row = rows[0]
    flow, transaction, source = 'revoked-media-flow', 'revoked-media-transaction', 'revoked-image'
    old_token = token_for_revision(row['binding_id'], 1)
    response = client.post(f"/api/workers/{w['id']}/inflight-flow/start", headers=headers(w), json={
        'flow_id': flow, 'flow_kind': 'c2_read', 'conversation_id': row['conversation_id'],
        'unread_generation': 0, 'authorization_revision': old_token})
    assert response.status_code == 200, response.text
    invalidate(row['lead_id'])
    params = {'recovery_transaction_id': transaction, 'action_kind': 'image',
              'source_message_key_digest': hashlib.sha256(source.encode()).hexdigest(),
              'original_authorization_revision': old_token}
    url = f"/api/workers/{w['id']}/wechat/conversations/{row['conversation_id']}/read-authorization"
    denied = client.get(url, headers=headers(w, flow), params={**params, 'original_authorization_revision': 'wrong'})
    assert denied.status_code == 409 and denied.json()['code'] == 'MESSAGE_AUTHORIZATION_REVISION_EXPIRED'
    authorized = client.get(url, headers=headers(w, flow), params=params)
    assert authorized.status_code == 200, authorized.text
    permission = authorized.json()['data']
    assert permission['recovery_decision'] == 'settle_without_ui' and permission['settlement_mode'] == 'fact_only'
    payload = _fact_settlement_payload({'id': row['binding_id'], 'conversation_id': row['conversation_id']}, 'CJ3N95EU',
        transaction_id=transaction, source_keys=[source], settlement_mode='fact_only',
        messages=[_v3_failed_image_message(source, role='customer', screen_order=1, reason='C2_IMAGE_SOURCE_INVALID')])
    payload['read_run_id'] = flow
    payload['evidence']['slot_ledger_states'][0]['origin_read_run_id'] = flow
    receipt_headers = {**headers(w, flow), 'X-C2-Settlement-Token': permission['settlement_token']}
    for _ in range(2):
        receipt = client.post(f"/api/workers/{w['id']}/wechat/messages/ingest", json=payload, headers=receipt_headers)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()['data']['state_transition_applied'] is False
    finished = client.post(f"/api/workers/{w['id']}/inflight-flow/finish", headers=headers(w, flow), json={
        'flow_id': flow, 'conversation_id': row['conversation_id'], 'terminal_kind': 'read_cancelled', 'error_code': 'LEAD_INVALID'})
    assert finished.status_code == 200, finished.text
    with SessionLocal() as db:
        message = db.scalar(select(MessageEvent))
        assert message.read_run_id == flow and message.source_message_key == source
        assert db.scalar(select(func.count()).select_from(MessageEvent)) == 1
        assert db.scalar(select(WechatRecoverySettlement)).status == 'settled'
        assert db.scalar(select(func.count()).select_from(MessageBatch)) == 0
        assert db.scalar(select(func.count()).select_from(ReplyAction)) == 0
        assert db.get(Conversation, row['conversation_id']).status == 'waiting_sales_reply'
        assert not db.get(Worker, w['id']).inflight_flow_state


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Real PostgreSQL migration required')
def test_migration_preserves_existing_binding_configuration():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    path = Path(__file__).parents[1] / 'alembic/versions/20260907_0034_lead_followup_eligibility.py'
    spec = importlib.util.spec_from_file_location('followup_test_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # Actual migration operations against a separate, transaction-local test
    # schema. Rollback removes the schema and never touches the route fixtures.
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text('CREATE SCHEMA followup_migration_test'))
            connection.execute(text('SET LOCAL search_path TO followup_migration_test'))
            connection.execute(text('CREATE TABLE wechat_session_bindings (id TEXT PRIMARY KEY, listen_status TEXT NOT NULL, authorization_revision INTEGER NOT NULL)'))
            connection.execute(text("INSERT INTO wechat_session_bindings VALUES ('synthetic-existing-binding', 'paused', 7)"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            row = connection.execute(text('SELECT * FROM wechat_session_bindings')).mappings().one()
            assert row['listen_status'] == 'paused' and row['authorization_revision'] == 7
            assert row['followup_invalidated_revision'] is None and row['followup_restore_pending'] is False
            migration.downgrade()
            row = connection.execute(text('SELECT * FROM wechat_session_bindings')).mappings().one()
            assert dict(row) == {'id': 'synthetic-existing-binding', 'listen_status': 'paused', 'authorization_revision': 7}
        finally:
            transaction.rollback()


def test_restored_listener_requires_fresh_read_before_c4_or_recovery_timer():
    w,rows=fixture_rows();row=rows[0]
    with SessionLocal() as db:
        conversation=db.get(Conversation,row['conversation_id'])
        conversation.next_recall_at=utcnow()-timedelta(days=1)
        db.commit()
    invalidate(row['lead_id'])
    assert client.post(f"/api/leads/{row['lead_id']}/restore").status_code==200
    for _ in range(3):
        r=client.get(f"/api/workers/{w['id']}/wechat/sessions/read-targets",headers=headers(w))
        assert r.status_code==200,r.text
        target=next(x for x in r.json()['data']['targets'] if x['conversation_id']==row['conversation_id'])
        assert target['read_reason']=='waiting_sales_reply'
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,row['binding_id']).followup_restore_pending
        assert db.get(Conversation,row['conversation_id']).status=='waiting_sales_reply'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==2
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_restore_does_not_change_paused_configuration_or_revive_tasks():
    w,rows=fixture_rows()
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,rows[0]['binding_id']);binding.listen_status='paused'
        db.add(Task(lead_id=rows[0]['lead_id'],worker_id=w['id'],task_type='add_friend',status='pending'))
        db.commit()
    invalidate(rows[0]['lead_id'])
    response=client.post(f"/api/leads/{rows[0]['lead_id']}/restore")
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,rows[0]['binding_id'])
        assert binding.listen_status=='paused' and binding.followup_restore_pending
        assert binding.authorization_revision==3
        assert db.scalar(select(Task.status).where(Task.lead_id==rows[0]['lead_id']))=='cancelled'
        assert db.get(Conversation,rows[0]['conversation_id']).status=='waiting_sales_reply'


@pytest.mark.parametrize('invalid_first',[True,False])
def test_postgres_two_connection_serialization(invalid_first):
    if engine.dialect.name!='postgresql':
        pytest.skip('Requires isolated PostgreSQL; SQLite does not prove row locking')
    w,rows=fixture_rows();lead_id=rows[0]['lead_id']
    from app.core.request_context import ActorContext
    actor=ActorContext(operator_id='00000000-0000-0000-0000-000000000001',operator_name='Test',role='authenticated',ip_address=None,user_agent=None,request_id='followup-test')
    held=threading.Event();release=threading.Event();second_done=threading.Event();results=[]
    def invalidate_op(db):lead_service.mark_invalid(db,lead_id,MarkInvalidRequest(invalid_reason='test_data'),actor)
    def authorize_op(db):require_followup(db,lead_id)
    def first():
        with SessionLocal() as db:
            (invalidate_op if invalid_first else authorize_op)(db)
            held.set();assert release.wait(5);db.commit()
    def second():
        assert held.wait(5)
        with SessionLocal() as db:
            try:
                (authorize_op if invalid_first else invalidate_op)(db);db.commit();results.append('allowed')
            except AppError as exc:db.rollback();results.append(exc.code)
        second_done.set()
    t1=threading.Thread(target=first);t2=threading.Thread(target=second)
    t1.start();t2.start();assert held.wait(5)
    assert not second_done.wait(.2), 'Second connection must wait for Lead row lock'
    release.set();t1.join(6);t2.join(6)
    assert not t1.is_alive() and not t2.is_alive()
    assert results==(['LEAD_INVALID'] if invalid_first else ['allowed'])


def actor():
    from app.core.request_context import ActorContext
    return ActorContext(operator_id='00000000-0000-0000-0000-000000000001',operator_name='Test',role='authenticated',ip_address=None,user_agent=None,request_id='followup-test')


def reply_fixture():
    from test_c3_api import _setup_bound_conversation, _ingest, _collect, _generate, _worker_headers
    w,binding=_setup_bound_conversation()
    event_id=_ingest(w,binding['conversation_id'],'followup-synthetic-text','您好')
    generated=_generate(_collect(binding['conversation_id'],event_id)['batch_id'])
    assert generated.get('task_id'),generated
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,binding['id'])
        lead=db.get(Lead,b.lead_id) if b.lead_id else None
        if lead is None:
            lead=Lead(customer_name='Synthetic',status='assigned',source_type='manual',source_name_snapshot='test',created_by='test',updated_by='test')
            db.add(lead);db.flush()
        b.lead_id=lead.id
        db.get(Conversation,b.conversation_id).lead_id=lead.id
        db.get(Task,generated['task_id']).lead_id=lead.id
        db.commit();lead_id=lead.id
    claim=client.post(f"/api/tasks/{generated['task_id']}/claim",json={
        'worker_id':w['id'],'current_step':'chat_reply_claimed','claim_source':'c2_conversation_flow',
        'conversation_id':binding['conversation_id']},headers=_worker_headers(w))
    assert claim.status_code==200,claim.text
    return w,binding,lead_id,generated,int(claim.json()['data']['lease_fencing_token'])


@pytest.mark.parametrize('restore_before_ack',[False,True])
@pytest.mark.parametrize('send_result',['sent','failed','unknown'])
def test_permit_before_invalid_ack_settles_without_new_work(restore_before_ack,send_result):
    from test_c3_api import _worker_headers
    from app.models.c3 import SentAck
    w,binding,lead_id,g,lease=reply_fixture()
    h={**_worker_headers(w),'X-Task-Lease-Fencing-Token':str(lease)}
    permit=client.post(f"/api/reply-actions/{g['reply_action_id']}/claim-send",json={'task_id':g['task_id'],'worker_id':w['id']},headers=h)
    assert permit.status_code==200,permit.text
    permit=permit.json()['data']
    with SessionLocal() as db:
        before_handoff=db.scalar(select(func.count()).select_from(HandoffEvent))
        before_reply=db.scalar(select(func.count()).select_from(ReplyAction))
    invalidate(lead_id)
    if restore_before_ack:assert client.post(f'/api/leads/{lead_id}/restore').status_code==200
    denied=client.post(f"/api/reply-actions/{g['reply_action_id']}/claim-send",json={'task_id':g['task_id'],'worker_id':w['id']},headers=h)
    assert denied.status_code==409,denied.text
    payload={'task_id':g['task_id'],'worker_id':w['id'],'client_instance_id':'client-c3',
             'send_token':permit['send_token'],'reply_text_hash':permit['reply_text_hash'],
             'send_result':send_result,'action_phase':{'sent':'confirmed','failed':'not_attempted','unknown':'trigger_attempted'}[send_result],'sidecar_run_id':'synthetic-already-sent'}
    for _ in range(2):
        ack=client.post(f"/api/reply-actions/{g['reply_action_id']}/sent-ack",json=payload,headers=_worker_headers(w))
        assert ack.status_code==200,ack.text
        if 'task' in ack.json()['data']:
            assert ack.json()['data']['task']['status']==('completed' if send_result=='sent' else 'failed')
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SentAck))==1
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==before_handoff
        assert db.scalar(select(func.count()).select_from(ReplyAction))==before_reply
        assert db.get(Worker,w['id']).current_task is None


@pytest.mark.parametrize('invalid_first',[True,False])
def test_postgres_actual_claim_send_race(invalid_first):
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL only')
    from app.services.c3_service import claim_send
    w,binding,lead_id,g,lease=reply_fixture()
    held=threading.Event();release=threading.Event();done=threading.Event();results=[];errors=[]
    def invalidate_op(db):lead_service.mark_invalid(db,lead_id,MarkInvalidRequest(invalid_reason='test_data'),actor())
    def permit_op(db):return claim_send(db,reply_action_id=g['reply_action_id'],task_id=g['task_id'],worker_id=w['id'],client_instance_id='client-c3',lease_fencing_token=lease)
    def first():
        try:
            with SessionLocal() as db:
                (invalidate_op if invalid_first else permit_op)(db)
                held.set();assert release.wait(8);db.commit()
        except Exception as exc:errors.append(repr(exc));held.set()
    def second():
        try:
            assert held.wait(8)
            with SessionLocal() as db:
                try:
                    (permit_op if invalid_first else invalidate_op)(db);db.commit();results.append('allowed')
                except AppError as exc:db.rollback();results.append(exc.code)
        except Exception as exc:errors.append(repr(exc))
        finally:done.set()
    t1=threading.Thread(target=first);t2=threading.Thread(target=second)
    t1.start();t2.start();assert held.wait(8)
    blocked=not done.wait(.2);release.set();t1.join(9);t2.join(9)
    assert not errors,errors
    assert blocked and not t1.is_alive() and not t2.is_alive()
    assert results==(['LEAD_INVALID'] if invalid_first else ['allowed'])
    with SessionLocal() as db:
        action=db.get(ReplyAction,g['reply_action_id'])
        assert bool(action.send_token)==(not invalid_first)
        assert action.status==('cancelled' if invalid_first else 'sending')


def test_force_generation_cannot_revive_invalidated_batch_after_restore():
    from app.services import c3_service
    w,binding,lead_id,g,lease=reply_fixture()
    with SessionLocal() as db:batch_id=db.get(ReplyAction,g['reply_action_id']).batch_id
    invalidate(lead_id)
    assert client.post(f'/api/leads/{lead_id}/restore').status_code==200
    with SessionLocal() as db:
        before=db.scalar(select(func.count()).select_from(ReplyAction))
        assert c3_service.claim_message_batch_generation(db,batch_id=batch_id,force=True)['run'] is False
        assert c3_service.generate_for_batch(db,batch_id=batch_id,force=True).get('task_id') is None
        assert db.scalar(select(func.count()).select_from(ReplyAction))==before


def test_reconciliation_preview_is_readonly_and_apply_idempotent():
    from app.services.followup_eligibility import reconcile_invalid_followup
    w,rows=fixture_rows()
    with SessionLocal() as db:db.get(Lead,rows[0]['lead_id']).status='invalid';db.commit()
    with SessionLocal() as db:
        preview=reconcile_invalid_followup(db,actor())
        assert preview['mode']=='preview' and preview['items'][0]['binding_ids']==[rows[0]['binding_id']]
        assert db.get(WechatSessionBinding,rows[0]['binding_id']).authorization_revision==1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.event_type=='lead_followup_revoked'))==0
        db.rollback()
    for _ in range(2):
        with SessionLocal() as db:reconcile_invalid_followup(db,actor(),apply=True);db.commit()
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,rows[0]['binding_id']).authorization_revision==2
        assert db.get(WechatSessionBinding,rows[1]['binding_id']).authorization_revision==1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.event_type=='lead_followup_revoked'))==1


@pytest.mark.parametrize('restore_while_generating',[False,True])
def test_late_brain_result_cannot_create_reply(monkeypatch,restore_while_generating):
    from test_c3_api import _ingest, _collect, _generate
    from app.services import c3_service
    from types import SimpleNamespace
    w,binding,lead_id,g,lease=reply_fixture()
    generated_batches=[]
    underlying=c3_service.get_ai_engine_adapter()
    def slow_provider(**kwargs):
        # Controlled Provider boundary: the real service released its DB
        # transaction before this call. No batch/finalization is faked here.
        generated_batches.append(kwargs['message_batch']['id'])
        result=underlying.generate_reply_decision(**kwargs)
        invalidate(lead_id)
        if restore_while_generating:assert client.post(f'/api/leads/{lead_id}/restore').status_code==200
        return result
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:SimpleNamespace(generate_reply_decision=slow_provider))
    message=_ingest(w,binding['conversation_id'],'followup-next-turn','请帮我看看')
    assert len(generated_batches)==1,generated_batches
    batch_id=generated_batches[0]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReplyAction).where(ReplyAction.batch_id==batch_id))==0
        assert db.get(MessageBatch,batch_id).status=='cancelled'


@pytest.mark.parametrize("case", ["read_before_action", "task_after_action"])
def test_real_http_worker_subprocess_automatically_cancels_and_releases(tmp_path, case):
    import json, os, socket, subprocess, sys
    from pathlib import Path
    import uvicorn
    w,rows=fixture_rows()
    if case == "task_after_action":
        with SessionLocal() as db:
            task=Task(lead_id=rows[0]['lead_id'],worker_id=w['id'],task_type='add_friend',status='pending')
            db.add(task);db.flush();w['test_task_id']=task.id;db.commit()
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.02)
    assert server.started
    # This is the production Worker entry, production HTTP client and local
    # SQLite. Only physical Windows calibration is an explicit synthetic
    # fixture. Any search/click/read/send is an assertion failure.
    script=tmp_path/'worker_case.py'
    script.write_text('''import json,sys,requests
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding,WechatReadTarget,Task,RpaResult
from chejin_worker_client.storage import save_binding,load_runtime_control,has_pending_c2_outbox
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
base,w,lead,conv=sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],sys.argv[4]
api=WorkerApiClient(base+'/api')
binding=Binding(w['id'],w['worker_token'],'followup-test',run_status='running')
save_binding(binding)
calls=[]
def observe(response,*args,**kwargs):
    if response.request.url.endswith('/inflight-flow/start') and response.status_code==200 and 'test_task_id' not in w:
        result=requests.post(base+'/api/leads/'+lead+'/mark-invalid',json={'invalid_reason':'test_data'},timeout=10)
        result.raise_for_status()
    if response.request.url.endswith('/inflight-flow/finish'):
        calls.append({'request':json.loads(response.request.body),'status':response.status_code})
    return response
api.session.hooks['response']=[observe]
class NoPhysicalWork:
    trigger_count=0
    def run_add_friend(self,task,emit_step,cancel_check=None):
        assert 'test_task_id' in w
        self.trigger_count+=1
        assert self.trigger_count==1
        result=requests.post(base+'/api/leads/'+lead+'/mark-invalid',json={'invalid_reason':'test_data'},timeout=10)
        result.raise_for_status()
        return RpaResult(ok=True,result_code='invite_sent',message='synthetic already-triggered action')
    def prepare_startup_layout_for_new_transaction(self):return {'ok':True,'synthetic_fixture':True}
    def __getattr__(self,name):raise AssertionError('Unexpected physical operation: '+name)
bridge=NoPhysicalWork()
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=lambda x:None)
runner.binding=binding
if 'test_task_id' in w:
    task=Task(w['test_task_id'],'add_friend','pending')
    runner._execute_task(binding,task,'pending')
    result={'error_code':None}
else:
    payload=requests.get(base+'/api/workers/'+w['id']+'/wechat/sessions/read-targets',headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':'followup-test'},timeout=10).json()['data']
    target=WechatReadTarget.from_api(next(x for x in payload['targets'] if x['conversation_id']==conv))
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True)
print(json.dumps({'error_code':result.get('error_code'),'finish':calls,'runtime':load_runtime_control(),'locked':lock_summary().get('locked'),'outbox_pending':has_pending_c2_outbox(),'status':binding.run_status}))
''')
    root=Path(__file__).resolve().parents[2]
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock',
         'PYTHONPATH':str(root/'worker-client')+os.pathsep+str(root/'worker-client'/'omniauto-rpa')}
    try:
        proc=subprocess.run([sys.executable,str(script),f'http://127.0.0.1:{port}',json.dumps(w),rows[0]['lead_id'],rows[0]['conversation_id']],env=env,capture_output=True,text=True,timeout=30)
        assert proc.returncode==0,proc.stderr
        result=json.loads(proc.stdout.strip().splitlines()[-1])
        assert result['error_code']==('LEAD_INVALID' if case=='read_before_action' else None),result
        assert len(result['finish'])==1 and result['finish'][0]['status']==200,result
        assert result['finish'][0]['request']['terminal_kind']==('read_cancelled' if case=='read_before_action' else 'task_terminal')
        assert not result['runtime']['inflight_flow_id'] and not result['locked'] and not result['outbox_pending']
        assert result['status']=='running'
        with SessionLocal() as db:
            assert db.get(Worker,w['id']).inflight_flow_state=={}
            if case=='task_after_action':
                task=db.get(Task,w['test_task_id'])
                assert task.status=='completed' and task.result_code=='invite_sent'
                assert task.lease_owner_worker_id is None
        output = Path(os.environ.get('CHEJIN_FOLLOWUP_EVIDENCE_DIR', str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        evidence = {'fixture': 'Synthetic customers and physical action/calibration; production Worker subprocess, HTTP and database; no Windows UI',
                    'case': case, 'database': engine.dialect.name, 'error_code': result['error_code'],
                    'automatic_finish': result['finish'], 'local_flow_id': result['runtime']['inflight_flow_id'],
                    'ui_locked': result['locked'], 'outbox_pending': result['outbox_pending'],
                    'worker_status': result['status'], 'backend_flow_empty': True}
        (output / f'worker-auto-closure-{case}.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    finally:
        server.should_exit=True;thread.join(5);sock.close()
