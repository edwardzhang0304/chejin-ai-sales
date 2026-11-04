"""Real HTTP lifecycle, synthetic contacts/UI effects; no external service calls."""
from copy import deepcopy
from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.contracts.shared_rules import shared_adapter
from app.services import hook_history_service
from test_hook_business_http import clean, post, body, proof, client
from test_hook_history_business_http import bound as checked_bound, coverage, upload

def bound():
    return checked_bound(confirm_initial=False)
from test_hook_native_send_http import beginning, permitted


def invite_request(b):
    return {'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
            'confirm_end_previous':True,'reason':'测试：运营确认结束旧托管后重新邀请'}


def reinvite(b, change=None):
    payload=invite_request(b);payload.update(change or {})
    return client.post(f"/api/leads/{b['lead_id']}/wechat/reinvite",json=payload)


def observation(w,p,b,friends,change=None):
    payload={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
             'friend_list':friends,'raw_evidence_digest':shared_adapter('hook_contract_rules').digest(friends)}
    payload.update(change or {})
    return post(w,f"/workers/{w['id']}/wechat/native-relationships/observe",payload)


def finish_invite(w,p,task):
    tid=task['id']
    r=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':tid,'flow_kind':'task'});assert r.status_code==200,r.text
    r=post(w,f'/tasks/{tid}/claim',{'worker_id':w['id']},{'X-Inflight-Flow-Id':tid});assert r.status_code==200,r.text
    headers={'X-Inflight-Flow-Id':tid,'X-Task-Lease-Fencing-Token':str(r.json()['data']['lease_fencing_token'])}
    r=post(w,f'/tasks/{tid}/hook-invite/prepare',p,headers);assert r.status_code==200,r.text
    prep=r.json()['data']
    r=post(w,f'/tasks/{tid}/hook-invite/submitted',proof(p,prep),headers);assert r.status_code==200,r.text
    r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':tid,'terminal_kind':'task_terminal'},headers);assert r.status_code==200,r.text
    return prep


@pytest.mark.parametrize('case',['matched','absent','empty','duplicate','invalid','changed'])
def test_relationship_observation_never_infers_deletion_or_stops_worker(case):
    w,t,p,b=bound()
    good={'wxid':b['peer_wxid'],'markname':b['remark_code']}
    rows={'matched':[good],'absent':[{'wxid':'wxid_other','markname':'other'}],
          'empty':[],'duplicate':[good,good],'invalid':[],
          'changed':[{**good,'markname':'已改为普通备注'}]}[case]
    friends={'code':9 if case=='invalid' else 0,'data':rows}
    token = None
    for round_index in range(2):
        r=observation(w,p,b,friends, {'confirmation_token':token} if token else {});assert r.status_code==200,r.text
        result=r.json()['data'];assert result['deletion_confirmed'] is False and result['authorization_changed'] is False
        assert result['review_required']==(case=='changed' and round_index==1)
        token = result.get('confirmation_token')
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);worker=db.get(Worker,w['id'])
        assert binding.bind_status=='bound' and binding.authorization_revision==b['binding_revision']
        assert worker.run_status=='running'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==(1 if case=='changed' else 0)
        conv=db.get(Conversation,b['conversation_id']);assert conv.ai_enabled==(case!='changed')
    # A later matching contact cannot silently restore AI.
    observation(w,p,b,{'code':0,'data':[good]})
    if case=='changed':
        with SessionLocal() as db:assert not db.get(Conversation,b['conversation_id']).ai_enabled


@pytest.mark.parametrize('field,value',[('confirm_end_previous',False),('confirm_end_previous',1),
    ('binding_revision',999),('reason','   '),('binding_id',str(uuid.uuid4()))])
def test_reinvite_requires_exact_confirmation_and_current_authority(field,value):
    w,t,p,b=bound();r=reinvite(b,{field:value});assert r.status_code in {400,409,422},r.text
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,b['binding_id']).bind_status=='bound'
        assert db.scalar(select(func.count()).select_from(Task))==1


def test_explicit_reinvite_full_c1_new_binding_chain_and_old_proof_retained(monkeypatch):
    w,t,p,b=bound();original=deepcopy(b)
    for round_number in range(2):
        with SessionLocal() as db:
            old=db.get(WechatSessionBinding,b['binding_id']);ctx=deepcopy(old.native_context)
        r=reinvite(b);assert r.status_code==200,r.text
        task=r.json()['data']['task']
        again=reinvite(b);assert again.status_code==200 and not again.json()['data']['created']
        assert again.json()['data']['task']['id']==task['id']
        with SessionLocal() as db:
            old=db.get(WechatSessionBinding,b['binding_id'])
            assert old.bind_status=='disabled' and not old.deleted_at
            assert all(old.native_context[k]==ctx[k] for k in ctx if k!='business_enabled')
        prep=finish_invite(w,p,task)
        # No manual/reused account candidate can replace the scoped predecessor.
        bad=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,p,prep,('wxid_other',)))
        assert bad.status_code==409
        r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,p,prep));assert r.status_code==200,r.text
        new=r.json()['data'];assert post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,p,prep)).json()['data']==new
        assert new['binding_id']!=b['binding_id'] and new['conversation_id']!=b['conversation_id']
        with SessionLocal() as db:
            old=db.get(WechatSessionBinding,b['binding_id']);assert old.replacement_binding_id==new['binding_id']
            assert db.get(Conversation,old.conversation_id).status=='closed'
            assert db.get(WechatSessionBinding,new['binding_id']).native_context['history_ready'] is False
        # Original task can never re-enable its disabled binding.
        if round_number==0:
            old_prep={'remark_name':original['remark_code']}
            assert post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,old_prep)).status_code==409
        b=new
    assert observation(w,p,b,{'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}).status_code==200
    result,_=upload(w,p,b,coverage(b),monkeypatch);assert result.status_code==200,result.text
    assert result.json()['data']['history_ready'] and result.json()['data']['message_batch']
    # Follow the replacement all the way through a new reply and durable task finish.
    from app.services import c3_service, hook_send_service
    with SessionLocal() as db:
        made=c3_service.generate_for_batch(db,batch_id=result.json()['data']['message_batch']['batch_id'],native_client_admitted=True)
        db.commit()
    assert made['decision']=='send_reply'
    tid,aid=made['task_id'],made['reply_action_id']
    r=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':tid,'flow_kind':'chat_reply','conversation_id':b['conversation_id']});assert r.status_code==200,r.text
    h={'X-Inflight-Flow-Id':tid}
    r=post(w,f'/tasks/{tid}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},h);assert r.status_code==200,r.text
    h['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    path=f"/workers/{w['id']}/wechat/native-replies/{aid}"
    r=post(w,path+'/claim',{**p,'task_id':tid,'history_state_digest':result.json()['data']['history_state_digest']},h);assert r.status_code==200,r.text
    permit=r.json()['data'];since=int(b['history_since_create_time'])
    now=lambda seconds:datetime.fromtimestamp(since+seconds,timezone.utc).isoformat()
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    start={**p,'task_id':tid,'history_state_digest':permit['history_state_digest'],
        'send_token':permit['send_token'],'journal_sha256':'d'*64,'attempted_at':now(40)}
    r=post(w,path+'/begin',start,h);assert r.status_code==200 and r.json()['data']['may_call_hook'],r.text
    assert not post(w,path+'/begin',start,h).json()['data']['may_call_hook']
    evidence=coverage(b)
    for scan in evidence['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[0],'localId':'7803','MsgSvrID':'8000000000000000003','IsSender':'1',
            'CreateTime':str(since+40),'Sequence':str((since+40)*1000),'MsgSequence':'780013','StrContent':permit['reply_text']})
        d['row_count']=d['count_after']='3';d['upper_local_id']='7803';scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    request={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':evidence,'coverage_digest':shared_adapter('hook_contract_rules').digest(evidence)}
    assert post(w,f"/workers/{w['id']}/wechat/native-history/coverage",request).status_code==200
    r=post(w,path+'/settle',{**p,'task_id':tid,'send_token':permit['send_token'],'journal_sha256':start['journal_sha256'],
        'completed_at':now(41), 'local_call_window':{'source':'physical_call_v1','started_at':now(40),'completed_at':now(41),'elapsed_seconds':1.0},'call_completed':True},h);assert r.status_code==200,r.text
    assert r.json()['data']['local_send_observed'] and not r.json()['data']['recipient_delivery_confirmed']
    r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':tid,'terminal_kind':'task_terminal','conversation_id':b['conversation_id']},h);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.get(Task,tid).status=='completed'
        assert not db.get(Worker,w['id']).inflight_flow_state.get('flow_id')
        assert db.get(WechatSessionBinding,original['binding_id']).bind_status=='disabled'


def test_failed_reinvite_round_can_be_explicitly_replaced_not_late_bound():
    w,t,p,b=bound();r=reinvite(b);assert r.status_code==200,r.text
    task=r.json()['data']['task'];prep=finish_invite(w,p,task)
    with SessionLocal() as db:
        old=db.get(WechatSessionBinding,b['binding_id']);revision=old.authorization_revision
    new_round=reinvite({**b,'binding_revision':revision});assert new_round.status_code==200,new_round.text
    assert new_round.json()['data']['task']['id']!=task['id']
    stale=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,p,prep))
    assert stale.status_code==409 and stale.json()['code']=='HOOK_INVITE_AUTHORIZATION_RETIRED',stale.text
    inventory=post(w,f"/workers/{w['id']}/wechat/native-work",p).json()['data']
    assert task['id'] not in [i['source_task_id'] for i in inventory['invitations']]


@pytest.mark.parametrize('decision',['send_reply','reply_then_handoff'])
def test_established_remark_change_preserves_original_send_eligibility(decision,monkeypatch):
    w,p,b,h,permit,value=permitted(monkeypatch)
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);action.decision=decision;db.commit()
    friends={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'改备注'}]}
    first=observation(w,p,b,friends).json()['data']
    assert first['state']=='established' and not first['initial_check_pending']
    start={**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],'clock_protocol_version':1,
        'send_token':permit['send_token'],'journal_sha256':'c'*64,'attempted_at':datetime.now(timezone.utc).isoformat()}
    r=post(w,f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/begin",start,h)
    if decision=='send_reply':
        assert r.status_code==200 and r.json()['data']['may_call_hook'],r.text
    else:
        # Setting the decision alone does not create human-handoff authority.
        assert r.status_code==409 and r.json()['code']=='REPLY_THEN_HANDOFF_NOT_ELIGIBLE',r.text
    with SessionLocal() as db:
        assert ('native_send_attempt' in db.get(ReplyAction,permit['reply_action_id']).ai_payload)==(decision=='send_reply')


def test_retirement_keeps_actual_send_original_reconciliation_and_pause(monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);original=deepcopy(action.ai_payload)
    r=reinvite(b);assert r.status_code==200,r.text
    assert post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'}).status_code==200
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[0],'localId':'7803','MsgSvrID':'8000000000000000003','IsSender':'1',
            'CreateTime':str(int(b['history_since_create_time'])+40),'Sequence':str((int(b['history_since_create_time'])+40)*1000),
            'MsgSequence':'780013','StrContent':permit['reply_text']})
        d['row_count']=d['count_after']='3';d['upper_local_id']='7803';scan['observed_at']=now(50)
    reconcile={**p,'reply_action_id':permit['reply_action_id'],'task_id':permit['task_id'],
        'flow_id':permit['flow_id'],'lease_fencing_token':permit['lease_fencing_token'],
        'send_token':permit['send_token'],'journal_sha256':start['journal_sha256']}
    request={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value),
        'reconciliation':reconcile}
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    observed=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",request);assert observed.status_code==200,observed.text
    result=post(w,path+'/settle',{**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'journal_sha256':start['journal_sha256'],'completed_at':now(41), 'local_call_window':{'source':'physical_call_v1','started_at':now(40),'completed_at':now(41),'elapsed_seconds':1.0},'call_completed':True},h)
    assert result.status_code==200,result.text
    assert result.json()['data']['local_send_observed'] and not result.json()['data']['automatic_resend_allowed']
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id'])
        assert action.ai_payload['native_send_identity']==original['native_send_identity']
        assert action.ai_payload['native_send_attempt']==original['native_send_attempt']
        assert db.get(Task,permit['task_id']).status=='completed'
        assert db.get(Worker,w['id']).run_status=='paused'
        assert db.get(WechatSessionBinding,b['binding_id']).bind_status=='disabled'


@pytest.mark.parametrize('change', ['wrong_epoch','wrong_revision','wrong_digest','disabled'])
def test_relationship_observation_cannot_mutate_foreign_or_retired_authority(change):
    w,t,p,b=bound();fields={}
    if change=='wrong_epoch':fields['account_epoch']=str(uuid.uuid4())
    if change=='wrong_revision':fields['binding_revision']=999
    if change=='wrong_digest':fields['raw_evidence_digest']='b'*64
    if change=='disabled':assert reinvite(b).status_code==200
    r=observation(w,p,b,{'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'changed'}]},fields)
    assert r.status_code==409,r.text
    with SessionLocal() as db:assert not db.scalar(select(HandoffEvent))


def test_retirement_before_physical_begin_uses_original_no_trigger_receipt(monkeypatch):
    w,p,b,h,permit,value=permitted(monkeypatch)
    assert reinvite(b).status_code==200
    path=f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}"
    start={**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],'clock_protocol_version':1,
        'send_token':permit['send_token'],'journal_sha256':'c'*64,'attempted_at':datetime.now(timezone.utc).isoformat()}
    r=post(w,path+'/begin',start,h);assert r.status_code==409 and r.json()['code']=='HOOK_AUTHORIZATION_RETIRED',r.text
    no_trigger={'reply_action_id':permit['reply_action_id'],'task_id':permit['task_id'],'phase':'not_triggered',
        'reason_code':'HOOK_SEND_LATEST_CONTEXT_STALE','intent_sha256':None,'observed_at':datetime.now(timezone.utc).isoformat()}
    request={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'proof':no_trigger,'proof_digest':shared_adapter('hook_contract_rules').digest(no_trigger)}
    result=post(w,path+'/not-triggered',request,h);assert result.status_code==200,result.text
    assert not result.json()['data']['physical_send_attempted']
    assert post(w,path+'/not-triggered',request,h).json()['data']==result.json()['data']
    with SessionLocal() as db:
        assert 'native_send_attempt' not in db.get(ReplyAction,permit['reply_action_id']).ai_payload
        assert db.get(Task,permit['task_id']).status in {'completed','failed','cancelled','canceled'}
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id==permit['reply_action_id'])).action_phase=='not_attempted'


def test_pending_invite_and_failed_confirmation_do_not_create_another_round():
    w,t,p,b=bound();first=reinvite(b);assert first.status_code==200
    with SessionLocal() as db:revision=db.get(WechatSessionBinding,b['binding_id']).authorization_revision
    second=reinvite({**b,'binding_revision':revision});assert second.status_code==409
    assert second.json()['code']=='HOOK_REINVITE_TASK_PENDING'
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Task))==2


def test_generic_evidence_route_cannot_forge_reinvite_authorization():
    from test_hook_business_http import claimed
    w,t,p,h,prep=claimed()
    r=post(w,f"/tasks/{t['id']}/evidences",{'evidence_type':'hook_reinvite_authorization',
        'metadata':{'previous_binding_id':str(uuid.uuid4()),'previous_revision':1}},h)
    assert r.status_code==409 and r.json()['code']=='HOOK_INVITE_PROOF_ROUTE_REQUIRED'


def test_client_relationship_read_reuses_real_http_and_confirms_changed_contact(identity_client,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    w,t,p,b=bound();calls=[]
    runtime=object.__new__(HookFormalRuntime)
    runtime.private_root=tmp_path/'hook'
    runtime.scope=SimpleNamespace(**p)
    runtime.account=p
    runtime.process=SimpleNamespace(generation=p['process_generation'])
    runtime.credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    from chejin_worker_client.hook_formal_client import HookFormalClient
    runtime.client=HookFormalClient(identity_client.base_url,5)
    def read(process,scope):
        calls.append('friend_list')
        return SimpleNamespace(payload={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'人工变更备注'}]})
    runtime.gateway=SimpleNamespace(friend_list=read)
    post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'})
    runtime.review_relationships([b])
    assert calls==['friend_list','friend_list']
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,b['binding_id']).native_context['relationship_review_required']
        assert db.scalar(select(HandoffEvent)).risk_flags==['contact_identity_unconfirmed']
        assert db.get(Worker,w['id']).run_status=='paused'

# Actual loopback transport is limited to the isolated backend.
from test_hook_account_client import identity_client


@pytest.mark.parametrize('claimed',['before','after'])
def test_operator_retirement_during_claim_finishes_original_flow_without_worker_fault(claimed,monkeypatch,identity_client,tmp_path):
    from app.services import c3_service
    w,t,p,b=checked_bound();r,_=upload(w,p,b,coverage(b),monkeypatch)
    with SessionLocal() as db:
        made=c3_service.generate_for_batch(db,batch_id=r.json()['data']['message_batch']['batch_id'],native_client_admitted=True);db.commit()
    tid=made['task_id'];h={'X-Inflight-Flow-Id':tid}
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':tid,'flow_kind':'chat_reply','conversation_id':b['conversation_id']}).status_code==200
    if claimed=='after':
        c=post(w,f'/tasks/{tid}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},h);assert c.status_code==200
        h['X-Task-Lease-Fencing-Token']=str(c.json()['data']['lease_fencing_token'])
    assert reinvite(b).status_code==200
    if claimed=='before':
        result=post(w,f'/tasks/{tid}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},h)
    else:
        result=post(w,f"/workers/{w['id']}/wechat/native-replies/{made['reply_action_id']}/claim",{**p,'task_id':tid,
            'history_state_digest':r.json()['data']['history_state_digest']},h)
    assert result.status_code==409 and result.json()['code']=='HOOK_AUTHORIZATION_RETIRED',result.text
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.hook_formal_client import HookFormalClient
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    from chejin_worker_client import storage
    runtime=object.__new__(HookFormalRuntime)
    runtime.client=HookFormalClient(identity_client.base_url,5)
    runtime.credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    runtime.private_root=tmp_path/'runtime';runtime.active_flow_id=tid;runtime.active_task_id=tid
    storage.begin_runtime_flow(tid,'chat_reply')
    token=int(h.get('X-Task-Lease-Fencing-Token','0'))
    runtime.finish(tid,tid,token,b['conversation_id'])
    assert not storage.load_runtime_control().get('inflight_flow_id')
    assert runtime.active_flow_id is None
    assert (runtime.private_root/'finished'/f'{tid}.json').exists()
    with SessionLocal() as db:
        assert db.get(Worker,w['id']).run_status=='running'
        assert not db.get(Worker,w['id']).inflight_flow_state.get('flow_id')
        assert 'native_send_attempt' not in (db.get(ReplyAction,made['reply_action_id']).ai_payload or {})


def test_human_chat_and_matching_contact_do_not_clear_identity_review(monkeypatch):
    w,t,p,b=bound()
    friends={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'人工备注'}]}
    first=observation(w,p,b,friends).json()['data']
    assert observation(w,p,b,friends,{'confirmation_token':first['confirmation_token']}).status_code==200
    value=coverage(b)
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0]
        rows[-1]['IsSender']='1';rows[-1]['StrContent']='人工消息：我来处理'
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert observation(w,p,b,{'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}).status_code==200
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,b['binding_id']).native_context['relationship_review_required']
        assert not db.get(Conversation,b['conversation_id']).ai_enabled
        assert db.scalar(select(HandoffEvent)).closed_at is None


@pytest.mark.parametrize('status',['bound','disabled'])
def test_idempotent_binding_does_not_ignore_unrelated_conflicting_records(status):
    w,t,p,b=bound()
    with SessionLocal() as db:
        old=db.get(WechatSessionBinding,b['binding_id'])
        db.add(WechatSessionBinding(worker_id=old.worker_id,lead_id=old.lead_id,sales_id=old.sales_id,
            native_self_wxid=old.native_self_wxid,native_peer_wxid='wxid_other_identity',remark_code='CJOTHER1',
            display_name='Synthetic other binding',bind_status=status,listen_status='disabled',allow_listening=False))
        db.commit()
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,{'remark_name':b['remark_code']}))
    assert r.status_code==409 and r.json()['code']=='HOOK_BINDING_CONFLICT',r.text


@pytest.mark.parametrize('case',['other_peer','other_epoch','other_revision','outgoing','no_parent','digest'])
def test_retirement_archival_voice_preserves_original_identity_checks(monkeypatch,case):
    from test_hook_native_media import media_coverage,voice_payload
    from app.models.wechat import MessageEvent
    from app.models.hook_native import HookVoiceReceipt
    w,t,p,b=bound();value=media_coverage(b)
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    payload=voice_payload(w,p,b,mid)
    assert reinvite(b).status_code==200
    if case=='other_peer':payload['peer_wxid']='wxid_wrong_peer'
    if case=='other_epoch':payload['account_epoch']=str(uuid.uuid4())
    if case=='other_revision':payload['binding_revision']+=1
    if case=='no_parent':payload['native_msg_id']='9999999999999999999'
    if case=='digest':payload['attempt_digest']='c'*64
    if case=='outgoing':
        with SessionLocal() as db:
            parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
            parent.sender_role='self';db.commit()
    rejected=post(w,f"/workers/{w['id']}/wechat/native-voice/result",payload)
    assert rejected.status_code==409,rejected.text
    with SessionLocal() as db:
        assert not list(db.scalars(select(HookVoiceReceipt)))
        assert db.get(WechatSessionBinding,b['binding_id']).bind_status=='disabled'


@pytest.mark.parametrize('case',['peer','revision','proof','reason'])
def test_retirement_archival_callback_does_not_accept_unrelated_evidence(case):
    from test_hook_native_http import prepared,post as store
    from app.models.hook_native import HookNativeReceipt
    w,payload=prepared()
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,payload['binding_id'])
        ctx=dict(binding.native_context)
        ctx.update(business_enabled=False,retired_authorization_revision=1)
        binding.native_context=ctx;binding.bind_status='disabled';binding.allow_listening=False
        binding.disable_reason='operator_reinvite';binding.authorization_revision=2
        evidence=db.get(TaskEvidence,ctx['invite_evidence_id'])
        evidence.extra_metadata={**evidence.extra_metadata,'backend_validated':True}
        if case=='peer':binding.native_peer_wxid='wxid_other_peer'
        if case=='revision':binding.native_context={**ctx,'retired_authorization_revision':2}
        if case=='proof':evidence.extra_metadata={**evidence.extra_metadata,'backend_validated':False}
        if case=='reason':binding.disable_reason='operator_disabled'
        db.commit()
    rejected=store(w,payload);assert rejected.status_code==409,rejected.text
    with SessionLocal() as db:assert not list(db.scalars(select(HookNativeReceipt)))


@pytest.mark.parametrize('mode',['retired_current','retired_missing','retired_old','retired_other_account','active_current'])
def test_retired_callback_after_process_restart_requires_current_and_original_scope(mode):
    from test_hook_native_http import prepared,post as store
    from test_hook_account_http import heartbeat,context
    from app.models.hook_native import HookNativeReceipt
    from app.models.wechat import MessageEvent
    w,payload=prepared()
    assert store(w,payload).status_code==200
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,payload['binding_id'])
        ctx=dict(binding.native_context)
        ctx['business_enabled']=mode=='active_current'
        if mode!='active_current':
            ctx['retired_authorization_revision']=1
            binding.bind_status='disabled';binding.allow_listening=False
            binding.disable_reason='operator_reinvite';binding.authorization_revision=2
        binding.native_context=ctx
        evidence=db.get(TaskEvidence,ctx['invite_evidence_id'])
        evidence.extra_metadata={**evidence.extra_metadata,'backend_validated':True}
        db.commit()
    result=heartbeat(w,context(process_generation='process-2',account_epoch=payload['account_epoch']))
    assert result.status_code==200 and not result.json()['data']['hook_context']['blocked']
    current={k:payload[k] for k in ('self_wxid','account_epoch','process_generation')}
    current['process_generation']='process-2'
    if mode=='retired_old':current['process_generation']='process-1'
    if mode=='retired_other_account':current['self_wxid']='wxid_other_account'
    response=post(w,f"/workers/{w['id']}/wechat/native-events/{payload['event_uuid']}/dispatch",
        None if mode=='retired_missing' else current)
    assert response.status_code==(200 if mode=='retired_current' else 409),response.text
    if mode=='retired_current':
        assert response.json()['data']['dispatch_status']=='authorization_retired'
        assert response.json()['data']['can_send'] is False
    with SessionLocal() as db:
        receipt=db.scalar(select(HookNativeReceipt))
        assert receipt.payload_json==payload and receipt.payload_json['process_generation']=='process-1'
        assert not list(db.scalars(select(MessageEvent)))


def test_transient_remark_change_preserves_welcome_and_manual_pause():
    w,t,p,b=bound()
    with SessionLocal() as db:
        worker=db.get(Worker,w['id']);worker.run_status='paused';db.commit()
        before=[(x.id,x.status) for x in db.scalars(select(Task))]
    changed={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':''}]}
    first=observation(w,p,b,changed).json()['data']
    assert first['recheck_required'] and not first['review_required']
    inventory=post(w,f"/workers/{w['id']}/wechat/native-work",p).json()['data']
    assert next(x for x in inventory['bindings'] if x['binding_id']==b['binding_id'])['relationship_confirmation_pending']
    # Repeating the first request does not confirm a remark change.
    repeated=observation(w,p,b,changed).json()['data']
    assert repeated['confirmation_token']==first['confirmation_token'] and not repeated['review_required']
    with SessionLocal() as db:
        from app.services.hook_relationship_service import ensure_send_authorization
        from app.errors import AppError
        binding=db.get(WechatSessionBinding,b['binding_id'])
        with pytest.raises(AppError) as caught:ensure_send_authorization(binding)
        assert caught.value.code=='HOOK_CONTACT_REMARK_CONFIRMATION_PENDING'
        assert db.get(Conversation,b['conversation_id']).ai_enabled
        assert not db.scalar(select(HandoffEvent))
    good={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}
    done=observation(w,p,b,good,{'confirmation_token':first['confirmation_token']})
    assert done.status_code==200,done.text
    data=done.json()['data'];assert not data['review_required'] and not data['recheck_required']
    assert data['comparison_evidence']['first']['peer_objects'][0]['fields'][1]['value']==''
    assert data['comparison_evidence']['second']['state']=='matched'
    with SessionLocal() as db:
        ensure_send_authorization(db.get(WechatSessionBinding,b['binding_id']))
        assert db.get(Conversation,b['conversation_id']).ai_enabled
        assert db.get(Worker,w['id']).run_status=='paused'
        assert [(x.id,x.status) for x in db.scalars(select(Task))]==before
    assert observation(w,p,b,changed,{'confirmation_token':first['confirmation_token']}).status_code==409


def test_unknown_confirmation_holds_without_handoff_and_requires_consecutive_changes():
    w,t,p,b=bound();friends={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'changed'}]}
    first=observation(w,p,b,friends).json()['data']
    unknown=observation(w,p,b,{'code':0,'data':[]},{'confirmation_token':first['confirmation_token']}).json()['data']
    assert unknown['recheck_required'] and not unknown['review_required']
    third=observation(w,p,b,friends,{'confirmation_token':unknown['confirmation_token']}).json()['data']
    assert third['recheck_required'] and not third['review_required']
    fourth=observation(w,p,b,friends,{'confirmation_token':third['confirmation_token']}).json()['data']
    assert fourth['review_required'] and not fourth['recheck_required']
    with SessionLocal() as db:
        evidence=db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type=='hook_relationship_review'))
        pair=evidence.extra_metadata['confirmation_evidence']
        assert pair['first']['state']==pair['second']['state']=='remark_changed'
        assert pair['second']['peer_objects'][0]['fields'][1]['value']=='changed'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1


def test_expired_confirmation_never_mutates_and_new_cycle_rearms(monkeypatch):
    from datetime import timedelta
    from app.services import hook_relationship_service as service
    w,t,p,b=bound();friends={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'changed'}]}
    first=observation(w,p,b,friends).json()['data'];now=service.utcnow()
    monkeypatch.setattr(service,'utcnow',lambda:now+timedelta(seconds=301))
    stale=observation(w,p,b,friends,{'confirmation_token':first['confirmation_token']})
    assert stale.status_code==409 and stale.json()['code']=='HOOK_RELATIONSHIP_CONFIRMATION_STALE'
    restarted=observation(w,p,b,friends).json()['data']
    assert restarted['recheck_required'] and restarted['confirmation_token']!=first['confirmation_token']
    with SessionLocal() as db:assert not db.scalar(select(HandoffEvent))


def test_established_relationship_cannot_reenter_pending_before_begin(monkeypatch):
    w,p,b,h,permit,value=permitted(monkeypatch)
    first=observation(w,p,b,{'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'changed'}]}).json()['data']
    assert first['state']=='established' and not first['initial_check_pending']
    inventory=post(w,f"/workers/{w['id']}/wechat/native-work",p).json()['data']
    assert not inventory['relationship_bindings']
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id'])
        assert action.status=='sending' and 'native_send_attempt' not in action.ai_payload
        assert not db.get(WechatSessionBinding,b['binding_id']).native_context.get('relationship_confirmation')
        assert not db.scalar(select(HandoffEvent))


def test_intervening_normal_poll_cannot_confirm_old_changed_read():
    w,t,p,b=bound();changed={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':'changed'}]}
    good={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}
    first=observation(w,p,b,changed).json()['data']
    normal=observation(w,p,b,good).json()['data']
    assert normal['confirmation_token']!=first['confirmation_token']
    third=observation(w,p,b,changed,{'confirmation_token':normal['confirmation_token']}).json()['data']
    assert not third['review_required'] and third['recheck_required']
    with SessionLocal() as db:assert not db.scalar(select(HandoffEvent))


def test_initial_check_guards_flow_before_any_send_claim_then_reuses_same_reply(monkeypatch):
    from app.services import c3_service
    w,t,p,b=bound()
    r,_=upload(w,p,b,coverage(b),monkeypatch);assert r.status_code==200,r.text
    result=r.json()['data']
    with SessionLocal() as db:
        made=c3_service.generate_for_batch(db,batch_id=result['message_batch']['batch_id'],native_client_admitted=True)
        db.commit()
    assert made['decision']=='send_reply',made
    flow={'flow_id':made['task_id'],'flow_kind':'chat_reply','conversation_id':b['conversation_id']}
    changed={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':''}]}
    first=observation(w,p,b,changed).json()['data']
    for _ in range(2):  # Same behavior on the next polling/restart request.
        blocked=post(w,f"/workers/{w['id']}/inflight-flow/start",flow)
        assert blocked.status_code==409 and blocked.json()['code']=='HOOK_CONTACT_REMARK_CONFIRMATION_PENDING',blocked.text
        with SessionLocal() as db:
            assert not (db.get(Worker,w['id']).inflight_flow_state or {}).get('flow_id')
            assert db.get(ReplyAction,made['reply_action_id']).status=='queued'
            assert not db.get(ReplyAction,made['reply_action_id']).send_token
    good={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}
    done=observation(w,p,b,good,{'confirmation_token':first['confirmation_token']})
    assert done.status_code==200 and not done.json()['data']['initial_check_pending'],done.text
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",flow).status_code==200
    headers={'X-Inflight-Flow-Id':made['task_id']}
    r=post(w,f"/tasks/{made['task_id']}/claim",{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},headers)
    assert r.status_code==200,r.text
    headers['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    r=post(w,f"/workers/{w['id']}/wechat/native-replies/{made['reply_action_id']}/claim",
        {**p,'task_id':made['task_id'],'history_state_digest':result['history_state_digest'],'clock_protocol_version':1},headers)
    assert r.status_code==200 and r.json()['data']['reply_action_id']==made['reply_action_id'],r.text
    # Late old observations never put an admitted send back into the initial hold.
    assert observation(w,p,b,changed).json()['data']['state']=='established'
    with SessionLocal() as db:
        assert db.get(ReplyAction,made['reply_action_id']).status=='sending'
        assert not db.scalar(select(HandoffEvent))


@pytest.mark.parametrize('existing_other_flow',[False,True])
def test_initial_pending_inventory_allows_other_customer_actual_dispatch(monkeypatch,tmp_path,existing_other_flow):
    """Real API inventory/batches/Flow/claim/begin; synthetic model and physical Hook boundary."""
    import threading,time
    from types import SimpleNamespace
    from app.services import c3_service
    from chejin_worker_client import storage
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.api import ApiError
    w,t,p,a=bound()
    newlead=client.post('/api/leads',json={'customer_name':'synthetic second customer','phones':['13800000004']})
    assert newlead.status_code==200,newlead.text
    pull=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
    task=pull.json()['data']['task'];assert task
    prep=finish_invite(w,p,task)
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,p,prep,('wxid_customer_B',)))
    assert r.status_code==200,r.text
    b=r.json()['data']
    good={'code':0,'data':[{'wxid':b['peer_wxid'],'markname':b['remark_code']}]}
    assert observation(w,p,b,good).status_code==200
    histories={};made={}
    for offset,binding in enumerate([a,b]):
        value=coverage(binding)
        for scan in value['scans']:
            for row in scan['databases'][0]['pages'][0]:
                row['MsgSvrID']=str(int(row['MsgSvrID'])+offset*100)
        r,_=upload(w,p,binding,value,monkeypatch);assert r.status_code==200,r.text
        histories[binding['binding_id']]=r.json()['data']
        with SessionLocal() as db:
            result=c3_service.generate_for_batch(db,batch_id=r.json()['data']['message_batch']['batch_id'],native_client_admitted=True)
            db.commit()
        assert result['decision']=='send_reply',result
        made[binding['binding_id']]=result
    observation(w,p,a,{'code':0,'data':[{'wxid':a['peer_wxid'],'markname':''}]})
    scope=SimpleNamespace(**p);credentials=SimpleNamespace(worker_id=w['id'],client_instance_id=w['instance'])
    monkeypatch.setattr(storage,'APP_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker_client.sqlite3')
    class LocalClient:
        def call(self,creds,method,path,data=None,**kw):
            headers={}
            if kw.get('flow_id'):headers['X-Inflight-Flow-Id']=kw['flow_id']
            if kw.get('token'):headers['X-Task-Lease-Fencing-Token']=str(kw['token'])
            r=(post(w,path,data,headers) if method=='POST' else client.get('/api'+path,headers={
                'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance'],**headers}))
            if r.status_code>=400:raise ApiError(r.json()['code'],r.text,r.status_code)
            return r.json()['data']
    api=LocalClient();sends=[]
    class Runtime:
        def __init__(self,*args,**kw):
            self.credentials=credentials;self.account=p;self.scope=scope;self.active_flow_id=None;self.active_task_id=None
            self.client=api;self.private_root=tmp_path/'transactions'/'hook';self.clock=SimpleNamespace(observe=lambda x:None)
        def heartbeat(self,**kw):pass
        def barrier(self,binding):return (0,0)
        def sync(self,binding):return histories[binding['binding_id']]
        def reply_once(self,binding,data,*,history):
            task=data['task'];action=data['reply_action']
            flow=self.active_flow_id or HookFormalRuntime.start_flow(self,task,binding['conversation_id'])
            claimed=HookFormalRuntime.claim(self,task,flow,binding['conversation_id'])
            permit=api.call(credentials,'POST',f"/workers/{w['id']}/wechat/native-replies/{action['id']}/claim",
                {**p,'task_id':task['id'],'history_state_digest':history['history_state_digest'],'clock_protocol_version':1},
                flow_id=flow,token=claimed['lease_fencing_token'])
            began=api.call(credentials,'POST',f"/workers/{w['id']}/wechat/native-replies/{action['id']}/begin",
                {**p,'task_id':task['id'],'history_state_digest':history['history_state_digest'],'clock_protocol_version':1,
                 'send_token':permit['send_token'],'journal_sha256':'c'*64,'attempted_at':datetime.now(timezone.utc).isoformat()},
                flow_id=flow,token=claimed['lease_fencing_token'])
            assert began['may_call_hook'];sends.append(binding['binding_id'])
            return {'status':'synthetic_physical_boundary'}
    runtime=Runtime()
    if existing_other_flow:HookFormalRuntime.start_flow(runtime,{'id':made[b['binding_id']]['task_id']},b['conversation_id'])
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),api=SimpleNamespace(),
        _can_start_new_flow=lambda:True,_worker_transaction_barrier_ready=lambda *a,**kw:True)
    prepared=SimpleNamespace(registration=SimpleNamespace(scope=scope),account=None)
    services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=None),_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(api,runtime.private_root,lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=lambda *a,**kw:runtime)
    # A remains waiting after an already attempted second read failed. No test-created inventory.
    dispatcher._relationship_read_due=time.monotonic()+60
    dispatcher._housekeeping_due=time.monotonic()+60
    dispatcher.tick(runner,services)
    assert sends==[b['binding_id']]
    with SessionLocal() as db:
        assert db.get(ReplyAction,made[a['binding_id']]['reply_action_id']).status=='queued'
        assert not db.get(ReplyAction,made[a['binding_id']]['reply_action_id']).send_token
        assert not db.get(WechatSessionBinding,b['binding_id']).native_context.get('relationship_confirmation')
