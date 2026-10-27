"""Actual task API lifecycle; UI confirmation is a simulated test effect."""
import hashlib
from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy import select, func
from app.core.database import Base, SessionLocal, engine
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.models.c3 import Conversation
from app.models.worker import Worker
from app.models.sales import Sales
from app.contracts.shared_rules import shared_adapter
from test_hook_account_http import client, worker, context, heartbeat
from test_hook_account_client import identity_client

@pytest.fixture(autouse=True)
def clean():
    # Explicit DATABASE_URL test runner only; never accept a default DB.
    assert '/private/tmp/chejin-hook-formal-20261011/test-' in str(engine.url)
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)

def post(w,path,data,extra=None):
    return client.post('/api'+path,json=data,headers={'X-Worker-Token':w['worker_token'],
        'X-Client-Instance-Id':w['instance'],**(extra or {})})

def claimed(process_generation="process-1",pending=False):
    w=worker('formal-worker')
    with SessionLocal() as db:
        db.scalar(select(Sales).where(Sales.worker_id==w['id'])).sales_name='销售小张';db.commit()
    hook_context=context();hook_context['process_generation']=process_generation
    a=heartbeat(w,hook_context).json()['data']['hook_context']
    r=post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'running'})
    assert r.status_code==200,r.text
    lead=client.post('/api/leads',json={'customer_name':'Hook formal test','phones':['13800000003']}).json()['data']
    r=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
    assert r.status_code==200,r.text
    t=r.json()['data']['task'];assert t and t['business_object']['lead']['id']==lead['id']
    assert t['verify_message']=='您好，我是车金二手车的销售小张，您刚咨询过二手车'
    assert t['remark_name']==t['remark_code']
    if pending:
        p={'self_wxid':a['self_wxid'],'account_epoch':a['account_epoch'],'process_generation':process_generation}
        return w,t,p
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':t['id'],'flow_kind':'task'}).status_code==200
    r=post(w,f"/tasks/{t['id']}/claim",{'worker_id':w['id']},{'X-Inflight-Flow-Id':t['id']});assert r.status_code==200,r.text
    h={'X-Inflight-Flow-Id':t['id'],'X-Task-Lease-Fencing-Token':str(r.json()['data']['lease_fencing_token'])}
    p={'self_wxid':a['self_wxid'],'account_epoch':a['account_epoch'],'process_generation':process_generation}
    prep=post(w,f"/tasks/{t['id']}/hook-invite/prepare",p,h);assert prep.status_code==200,prep.text
    return w,t,p,h,prep.json()['data']

def proof(p,prep):
    return {**p,'attempted_at':datetime.now(timezone.utc).isoformat(),'preparation_id':prep['preparation_id'],'form_verified':True,
        'greeting_sha256':hashlib.sha256(prep['verify_message'].encode()).hexdigest(),
        'remark_sha256':hashlib.sha256(prep['remark_name'].encode()).hexdigest(),
        'greeting_copy_calls':1,'greeting_paste_calls':1,'remark_copy_calls':1,'remark_paste_calls':1,
        'confirm_click_calls':1,'form_closed':True,'submit_journal_sha256':'a'*64}

def body(t,p,prep,peers=('wxid_customer_A',)):
    friends={'code':0,'data':[{'wxid':v,'markname':prep['remark_name']} for v in peers]}
    return {**p,'source_task_id':t['id'],'friend_list':friends,'raw_evidence_digest':shared_adapter('hook_contract_rules').digest(friends)}

def submitted():
    w,t,p,h,prep=claimed();payload=proof(p,prep)
    r=post(w,f"/tasks/{t['id']}/hook-invite/submitted",payload,h);assert r.status_code==200,r.text
    assert r.json()['data']['task']['status']=='completed' and not r.json()['data']['customer_bound']
    assert post(w,f"/tasks/{t['id']}/hook-invite/submitted",payload,h).status_code==200
    r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':t['id'],'terminal_kind':'task_terminal'},h);assert r.status_code==200,r.text
    return w,t,p,h,prep

def test_formal_task_fields_and_acceptance_binding():
    w,t,p,h,prep=submitted();path=f"/workers/{w['id']}/wechat/native-bindings"
    r=post(w,path,body(t,p,prep,()));assert r.status_code==409,r.text
    first=post(w,path,body(t,p,prep));assert first.status_code==200,first.text
    assert post(w,path,body(t,p,prep)).json()['data']==first.json()['data']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WechatSessionBinding))==1
        assert db.scalar(select(func.count()).select_from(Conversation))==1
        assert db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type=='hook_invite_submission')).extra_metadata['backend_validated'] is True

@pytest.mark.parametrize('change',['greeting','remark','generation','token','second_paste','unknown','unprepared','bool_count','numeric_true'])
def test_wrong_submission_is_atomic(change):
    w,t,p,h,prep=claimed();payload=proof(p,prep)
    if change in {'greeting','remark'}:payload[change+'_sha256']='b'*64
    if change=='generation':payload['process_generation']='process-2'
    if change=='token':h['X-Task-Lease-Fencing-Token']='999'
    if change=='second_paste':payload['greeting_paste_calls']=2
    if change=='unknown':payload['form_closed']=False
    if change=='bool_count':payload['confirm_click_calls']=True
    if change=='numeric_true':payload['form_verified']=1
    if change=='unprepared':payload['preparation_id']=str(uuid.uuid4())
    r=post(w,f"/tasks/{t['id']}/hook-invite/submitted",payload,h);assert r.status_code in {400,409,422},r.text
    with SessionLocal() as db:
        assert db.get(Task,t['id']).status=='running'
        assert db.scalar(select(func.count()).select_from(WechatSessionBinding))==0
        assert db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.evidence_type=='hook_invite_submission'))==0

@pytest.mark.parametrize('change',['multiple','other_account','other_epoch','altered_digest','laboratory_proof','already_friend','disabled'])
def test_binding_requires_unique_formal_origin(change):
    w,t,p,h,prep=submitted();b=body(t,p,prep)
    if change=='multiple':b=body(t,p,prep,('wxid_customer_A','wxid_customer_B'))
    if change=='other_account':heartbeat(w,context('wxid_sales_B'))
    if change=='other_epoch':b['account_epoch']=str(uuid.uuid4())
    if change=='altered_digest':b['raw_evidence_digest']='b'*64
    if change in {'laboratory_proof','already_friend','disabled'}:
        with SessionLocal() as db:
            if change=='laboratory_proof':
                e=db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type=='hook_invite_submission'));e.extra_metadata={**e.extra_metadata,'backend_validated':False}
            if change=='already_friend':db.get(Task,t['id']).result_code='already_friend'
            if change=='disabled':db.get(Worker,w['id']).enabled=False
            db.commit()
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",b);assert r.status_code in {400,401,409},r.text
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(WechatSessionBinding))==0

def test_generic_evidence_cannot_forge_origin():
    w,t,p,h,prep=claimed();r=post(w,f"/tasks/{t['id']}/evidences",{'evidence_type':'hook_invite_submission','metadata':{'backend_validated':True}},h)
    assert r.status_code==409 and r.json()['code']=='HOOK_INVITE_PROOF_ROUTE_REQUIRED'


@pytest.mark.parametrize("report_clock_failed", [False, True])
def test_worker_formal_c1_real_http_preserves_fields_and_replays_only_receipt(tmp_path, identity_client, monkeypatch, report_clock_failed):
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    from chejin_worker_client.hook_formal_client import HookFormalClient
    from chejin_worker_client.hook_formal_c1 import HookFormalC1
    from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI
    from chejin_worker_client.api import ApiError
    w,t,p,h,initial=claimed();token=int(h['X-Task-Lease-Fencing-Token'])
    api=HookFormalClient(identity_client.base_url,5)
    c1=HookFormalC1(api,HookWorkerCredentials(w['id'],w['worker_token'],w['instance']),tmp_path)
    prep=c1.prepare(t['id'],token,p)
    assert prep['verify_message']==t['verify_message'] and prep['remark_name']==t['remark_code']
    class ClipboardReplay(ReplayUI):
        def __init__(self):
            super().__init__();self.clipboard_field_events=[]
        def replace(self,frame,text):
            super().replace(frame,text)
            if frame.name.startswith('fields'):
                self.clipboard_field_events.append({'frame':frame.name,'copy_calls':1,'paste_calls':1,
                    'clipboard_match':True,'completion_guard_passed':True})
        def capture(self,name):
            f=super().capture(name)
            if name=='lookup':
                for row in f.rows:row['text']=row['text'].replace('13800000003',prep['primary_phone'])
            return f
    wrong_ui=ClipboardReplay()
    with pytest.raises(ValueError,match='PREPARATION_IDENTITY'):
        c1.execute_prepared(t['id'],token+1,prep,lambda:wrong_ui)
    assert wrong_ui.actions==[]
    if report_clock_failed:
        class ExpiringClock:
            calls = 0
            def now(self):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError('synthetic statistics clock expired after submit')
                return datetime.now(timezone.utc)
            def evidence(self):
                return {}
        c1.clock = ExpiringClock()
    ui=ClipboardReplay();outcome=c1.execute_prepared(t['id'],token,prep,lambda:ui)
    assert outcome.get('formal_submission_proof_saved') is True,outcome
    with SessionLocal() as db:assert db.get(Task,t['id']).status=='running'
    calls=[];original=HookFormalClient.submitted
    def fail_once(self,*args):
        calls.append(1)
        if len(calls)==1:raise ApiError('HOOK_BACKEND_NETWORK_FAILED','simulated response loss',0)
        return original(self,*args)
    monkeypatch.setattr(HookFormalClient,'submitted',fail_once)
    with pytest.raises(ApiError):c1.settle(t['id'],token)
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):
        c1.execute_prepared(t['id'],token,prep,ClipboardReplay)
    r=c1.settle(t['id'],token);assert r['task']['result_code']=='invite_sent'
    assert c1.settle(t['id'],token)==r and len(calls)==2
    assert ui.actions.count('confirm_invite_once')==1
    assert len(ui.clipboard_field_events)==2
    assert post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':t['id'],'terminal_kind':'task_terminal'},h).status_code==200
    bound=api.bind_friend(c1.credentials,t['id'],p,body(t,p,prep)['friend_list'])
    assert bound['can_ingest_messages'] and bound['source_task_id']==t['id']


def native_payload(w,grant,*,mid='6594133108973694888',text='预算12.5万元\n先不要报价🙂',source='native_callback'):
    rules=shared_adapter('hook_contract_rules')
    p={'transport':'hook','schema_version':'hook-v1','worker_id':w['id'],'client_instance_id':w['instance'],
        'self_wxid':grant['self_wxid'],'account_epoch':grant['account_epoch'],'process_generation':'process-1',
        'binding_id':grant['binding_id'],'binding_revision':grant['binding_revision'],'peer_wxid':grant['peer_wxid'],
        'source_event_uuid':str(uuid.uuid4()),'receive_seq':'1','raw_evidence_digest':'a'*64,
        'observed_at':'2026-10-11T04:00:00+00:00',
        'messages':[{'native_msg_id':mid,'entry_position':0,'direction':'incoming','message_type':'text','content':text,
            'source':source,'source_revision':'b'*64,'source_time_raw':'2026-10-11 12:00:00' if source=='native_callback' else '1791691200',
            'media_stage':None,'ordering_fields':{} if source=='native_callback' else
                {'localId':'8510','CreateTime':'1791691200','Sequence':'0','MsgServerSeq':'0','MsgSequence':'772263536'},
            'database':None if source=='native_callback' else 'MSG0.db',
            'database_generation':None if source=='native_callback' else 'c'*64,
            'from_wxid':grant['peer_wxid'],'to_wxid':grant['self_wxid'],'conversation_type':'private','raw_direction':'2' if source=='native_callback' else '0'}]}
    p['event_uuid']=rules.delivery_id(p['source_event_uuid'],w['id'],w['instance'],p['account_epoch'],p['binding_id'],p['binding_revision'])
    return p


def bound():
    w,t,p,h,prep=submitted()
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,prep));assert r.status_code==200,r.text
    return w,r.json()['data']


def store_dispatch(w,p):
    stored=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",p);assert stored.status_code==200,stored.text
    return post(w,f"/workers/{w['id']}/wechat/native-events/{p['event_uuid']}/dispatch",None)


def test_callback_and_history_are_one_fact_without_false_order_or_ai():
    from app.models.wechat import MessageEvent
    from app.models.c3 import MessageBatch
    from app.models.hook_native import HookNativeReceipt
    w,grant=bound();p=native_payload(w,grant)
    r=store_dispatch(w,p);assert r.status_code==200,r.text
    assert r.json()['data']['ai_dispatch']=='history_coverage_required' and not r.json()['data']['can_send']
    repeated=store_dispatch(w,p);assert repeated.json()['data']['new_message_event_ids']==[]
    history=store_dispatch(w,native_payload(w,grant,source='native_history'));assert history.status_code==200,history.text
    assert history.json()['data']['message_event_ids']==r.json()['data']['message_event_ids']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageEvent))==1
        fact=db.scalar(select(MessageEvent))
        assert fact.raw_payload['native_msg_id']=='6594133108973694888'
        assert fact.occurred_at is None and fact.observation_order is None
        assert fact.content==p['messages'][0]['content']
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert all(row.dispatch_status=='facts_stored' for row in db.scalars(select(HookNativeReceipt)))


def test_conflicting_native_id_does_not_overwrite_fact_or_start_ai():
    from app.models.wechat import MessageEvent
    from app.models.c3 import MessageBatch
    w,grant=bound();assert store_dispatch(w,native_payload(w,grant)).status_code==200
    bad=store_dispatch(w,native_payload(w,grant,text='different text'))
    assert bad.status_code==409 and bad.json()['code']=='HOOK_MESSAGE_ID_CONFLICT'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageEvent))==1
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_saved_transport_receipt_does_not_bypass_revoked_business_authority():
    from app.models.wechat import MessageEvent
    w,grant=bound();p=native_payload(w,grant)
    assert post(w,f"/workers/{w['id']}/wechat/messages/native-observations",p).status_code==200
    with SessionLocal() as db:
        db.get(WechatSessionBinding,grant['binding_id']).authorization_revision+=1;db.commit()
    r=post(w,f"/workers/{w['id']}/wechat/native-events/{p['event_uuid']}/dispatch",None)
    assert r.status_code==409 and r.json()['code']=='HOOK_BINDING_REVISION_STALE'
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(MessageEvent))==0


def test_native_facts_cannot_bypass_coverage_gate_through_original_ai_collector():
    from app.services.c3_service import collect_message_batch
    from app.errors import AppError
    from app.models.c3 import MessageBatch
    w,grant=bound();r=store_dispatch(w,native_payload(w,grant));assert r.status_code==200
    event_id=r.json()['data']['message_event_ids'][0]
    with SessionLocal() as db:
        with pytest.raises(AppError) as error:
            collect_message_batch(db,conversation_id=grant['conversation_id'],trigger_message_event_id=event_id)
        assert error.value.code=='HOOK_HISTORY_COVERAGE_REQUIRED'
        db.rollback()
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_old_client_receipt_cannot_be_dispatched_under_another_instance():
    from app.services.hook_message_service import dispatch_facts
    from app.errors import AppError
    w,grant=bound();payload=native_payload(w,grant)
    assert post(w,f"/workers/{w['id']}/wechat/messages/native-observations",payload).status_code==200
    with SessionLocal() as db:
        with pytest.raises(AppError) as error:
            dispatch_facts(db,db.get(Worker,w['id']),'another-instance',payload['event_uuid'])
        assert error.value.code=='HOOK_REQUEST_IDENTITY_MISMATCH'
