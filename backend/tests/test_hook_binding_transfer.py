"""HTTP succession lifecycle. Contact responses/AI are synthetic, no Windows I/O."""
from copy import deepcopy
from datetime import datetime, timezone
import uuid
from app.contracts.shared_rules import shared_adapter
from app.services import hook_send_service, hook_history_service
import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.services import hook_binding_transfer as transfer
from test_hook_business_http import clean, client, post, body
from test_hook_history_business_http import bound, coverage, upload
from test_hook_relationship_http import finish_invite
from test_hook_account_client import identity_client


def successor(monkeypatch, *, submitted=False, existing=None):
    w,t,p,b = existing or bound()
    r=client.post(f"/api/leads/{b['lead_id']}/mark-invalid",json={'invalid_reason':'test_data'})
    assert r.status_code==200,r.text
    # Replay the released behavior solely to prepare an already-submitted
    # incident. Production claim/pull gates are exercised separately below.
    with monkeypatch.context() as legacy:
        if submitted:legacy.setattr(transfer,'invitation_needs_review',lambda *a:False)
        r=client.post('/api/leads',json={'customer_name':'Synthetic successor','phones':['13800000003']})
        assert r.status_code==200,r.text
        lead=r.json()['data']
        with SessionLocal() as db:
            task=db.scalar(select(Task).where(Task.lead_id==lead['id']))
            task_id=task.id
            assert task.status==('pending' if submitted else 'blocked')
        prep=finish_invite(w,p,{'id':task_id}) if submitted else {'remark_name':b['remark_code']}
    url=f"/api/leads/{lead['id']}/wechat/transfer"
    preview=client.get(url);assert preview.status_code==200,preview.text
    choice=preview.json()['data']['candidate'];assert choice
    payload={k:choice[k] for k in ('binding_id','binding_revision','source_task_id')}
    payload.update(confirm_end_previous=True,reason='Synthetic operator succession')
    return w,t,p,b,lead,task_id,prep,url,payload


@pytest.mark.parametrize('submitted',[False,True])
def test_confirm_reverify_bind_history_ai_and_preserve_pause(monkeypatch,submitted):
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch,submitted=submitted)
    if submitted:
        conflict=post(w,f"/workers/{w['id']}/wechat/native-bindings",body({'id':tid},p,prep))
        assert conflict.status_code==409 and conflict.json()['code']=='HOOK_BINDING_CONFLICT'
    else:
        pulled=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={
            'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
        assert pulled.json()['data']['task'] is None
    assert post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'}).status_code==200
    with SessionLocal() as db:
        original={e.id:deepcopy(e.extra_metadata) for e in db.scalars(select(TaskEvidence).where(
            TaskEvidence.evidence_type.in_(['hook_invite_preparation','hook_invite_submission'])))}
    r=client.post(url,json=payload);assert r.status_code==200,r.text
    assert r.json()['data']['created']
    refreshed=client.get(url).json()['data']
    assert refreshed['candidate'] is None and refreshed['pending_verification']
    assert client.post(url,json=payload).json()['data']['created'] is False
    with SessionLocal() as db:
        assert db.get(Worker,w['id']).run_status=='paused'
        assert db.get(Conversation,b['conversation_id']).status=='closed'
        assert db.scalar(select(func.count()).select_from(WechatSessionBinding))==1
        task=db.get(Task,tid)
        assert task.status==('completed' if submitted else 'cancelled')
        for evidence_id,data in original.items():assert db.get(TaskEvidence,evidence_id).extra_metadata==data
    # The confirmation is not authority to accept a different wxid/username.
    path=f"/workers/{w['id']}/wechat/native-bindings"
    bad=post(w,path,body({'id':tid},p,prep,('wxid_someone_else',)))
    assert bad.status_code==409,bad.text
    r=post(w,path,body({'id':tid},p,prep));assert r.status_code==200,r.text
    new=r.json()['data'];assert new['lead_id']==lead['id'] and new['binding_id']!=b['binding_id']
    assert post(w,path,body({'id':tid},p,prep)).json()['data']==new
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,b['binding_id']).replacement_binding_id==new['binding_id']
        assert db.get(Worker,w['id']).run_status=='paused'
        # Real intake provenance must accept the transferred binding as well
        # as history coverage, not merely the bind endpoint's return value.
        transfer.require_binding_origin(db,db.get(WechatSessionBinding,new['binding_id']),db.get(Worker,w['id']))
    callback={**p,'transport':'hook','schema_version':'hook-v1','worker_id':w['id'],'client_instance_id':w['instance'],
        'binding_id':new['binding_id'],'binding_revision':new['binding_revision'],'peer_wxid':new['peer_wxid'],
        'source_event_uuid':str(uuid.uuid4()),'receive_seq':'1','raw_evidence_digest':'a'*64,
        'observed_at':datetime.now(timezone.utc).isoformat(),
        'messages':[{'native_msg_id':'8000000000000000001','entry_position':0,'direction':'incoming','message_type':'text',
            'content':'先看宝马','source':'native_callback','source_revision':'b'*64,
            'source_time_raw':datetime.now(timezone.utc).isoformat(),'media_stage':None,'ordering_fields':{},
            'database':None,'database_generation':None,'from_wxid':new['peer_wxid'],'to_wxid':p['self_wxid'],
            'conversation_type':'private','raw_direction':'2'}]}
    callback['event_uuid']=shared_adapter('hook_contract_rules').delivery_id(callback['source_event_uuid'],
        w['id'],w['instance'],p['account_epoch'],new['binding_id'],new['binding_revision'])
    intake=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",callback)
    assert intake.status_code==200,intake.text
    clock_binding = {**new, 'history_since_create_time': b['history_since_create_time']} if not submitted else new
    value = coverage(clock_binding)
    value['since_create_time'] = new['history_since_create_time']
    result,_=upload(w,p,clock_binding,value,monkeypatch)
    assert result.status_code==200,result.text
    assert result.json()['data']['history_ready']
    if not submitted:
        assert result.json()['data']['message_batch'] is None
        from test_hook_history_review import add_message
        value = add_message(value)
        result,_=upload(w,p,clock_binding,value,monkeypatch)
        assert result.status_code==200,result.text
        assert result.json()['data']['message_batch']

    assert post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'running'}).status_code==200
    from app.services import c3_service
    with SessionLocal() as db:
        batch=result.json()['data']['message_batch']['batch_id']
        generated=c3_service.generate_for_batch(db,batch_id=batch,native_client_admitted=True)
        assert generated['decision']=='send_reply',generated
        db.commit()
    tid,aid=generated['task_id'],generated['reply_action_id']
    r=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':tid,'flow_kind':'chat_reply','conversation_id':new['conversation_id']});assert r.status_code==200,r.text
    h={'X-Inflight-Flow-Id':tid}
    r=post(w,f'/tasks/{tid}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':new['conversation_id']},h);assert r.status_code==200,r.text
    h['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    path=f"/workers/{w['id']}/wechat/native-replies/{aid}"
    r=post(w,path+'/claim',{**p,'task_id':tid,'clock_protocol_version':1,'history_state_digest':result.json()['data']['history_state_digest']},h);assert r.status_code==200,r.text
    permit=r.json()['data'];since=int(clock_binding['history_since_create_time'])
    now=lambda seconds:datetime.fromtimestamp(since+seconds,timezone.utc).isoformat()
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    start={**p,'clock_protocol_version':1,'task_id':tid,'history_state_digest':permit['history_state_digest'],
        'send_token':permit['send_token'],'journal_sha256':'d'*64,'attempted_at':now(40)}
    r=post(w,path+'/begin',start,h);assert r.status_code==200 and r.json()['data']['may_call_hook'],r.text
    assert not post(w,path+'/begin',start,h).json()['data']['may_call_hook']
    evidence=deepcopy(value)
    for scan in evidence['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[0],'localId':'7804','MsgSvrID':'8000000000000000004','IsSender':'1',
            'CreateTime':str(since+40),'Sequence':str((since+40)*1000),'MsgSequence':'780014','StrContent':permit['reply_text']})
        d['row_count']=d['count_after']=str(len(rows));d['upper_local_id']='7804';scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    request={**p,'binding_id':new['binding_id'],'binding_revision':new['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':evidence,'coverage_digest':shared_adapter('hook_contract_rules').digest(evidence)}
    history=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",request)
    assert history.status_code==200,history.text
    r=post(w,path+'/settle',{**p,'task_id':tid,'send_token':permit['send_token'],'journal_sha256':start['journal_sha256'],
        'completed_at':now(41), 'local_call_window':{'source':'physical_call_v1','started_at':now(40),'completed_at':now(41),'elapsed_seconds':1.0},'call_completed':True},h);assert r.status_code==200,r.text
    assert r.json()['data']['local_send_observed'] and not r.json()['data']['recipient_delivery_confirmed']
    r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':tid,'terminal_kind':'task_terminal','conversation_id':new['conversation_id']},h);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.get(Task,tid).status=='completed'
        assert not db.get(Worker,w['id']).inflight_flow_state.get('flow_id')
        assert db.get(WechatSessionBinding,b['binding_id']).bind_status=='disabled'



@pytest.mark.parametrize('change',['revision','confirmation','worker_account','inflight','phone','active_old','ambiguous','rejected'])
def test_unsafe_confirmation_never_retires_or_reapplies(monkeypatch,change):
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch)
    from app.models.lead import Lead,LeadContact
    with SessionLocal() as db:
        before=deepcopy(db.get(WechatSessionBinding,b['binding_id']).native_context)
        if change=='revision':payload['binding_revision']+=1
        if change=='confirmation':payload['confirm_end_previous']=False
        if change=='worker_account':db.get(Worker,w['id']).hook_self_wxid='wxid_other_account'
        if change=='inflight':db.get(Worker,w['id']).inflight_flow_state={'flow_id':tid,'status':'active'}
        if change=='phone':db.scalar(select(LeadContact).where(LeadContact.lead_id==lead['id'])).contact_hash='other'
        if change=='rejected':db.get(Conversation,b['conversation_id']).status='rejected'
        if change=='active_old':db.get(Lead,b['lead_id']).status='assigned'
        if change=='ambiguous':
            old=db.get(WechatSessionBinding,b['binding_id'])
            db.add(WechatSessionBinding(lead_id=old.lead_id,sales_id=old.sales_id,worker_id=old.worker_id,
                display_name='Synthetic ambiguous contact',native_self_wxid=old.native_self_wxid,native_peer_wxid='wxid_other',remark_code='CJTEST2'))
        db.commit()
    r=client.post(url,json=payload);assert r.status_code==(400 if change=='confirmation' else 409),r.text
    with SessionLocal() as db:
        old=db.get(WechatSessionBinding,b['binding_id'])
        assert old.bind_status=='bound' and old.native_context==before
        assert db.get(Task,tid).status=='blocked'
        assert transfer.grant(db,tid) is None


def test_other_customer_is_still_claimable_and_stale_direct_claim_is_blocked(monkeypatch):
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch)
    from app.services import task_service
    # Even a stale pending task cannot bypass the new shared admission gate.
    with SessionLocal() as db:
        db.get(Task,tid).status='pending';db.commit()
    r=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':tid,'flow_kind':'task'})
    assert r.status_code==409,r.text
    r=client.post('/api/leads',json={'customer_name':'Another customer','phones':['13800000004']})
    assert r.status_code==200,r.text
    pulled=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={
        'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
    assert pulled.json()['data']['task']['lead_id']==r.json()['data']['id']


def test_transfer_preserves_original_send_identity_and_pause(monkeypatch):
    from test_hook_native_send_http import beginning
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);original=deepcopy(action.ai_payload)
    _,_,_,_,_,_,_,url,payload=successor(monkeypatch,existing=(w,{'id':b['source_task_id']},p,b))
    r=client.post(url,json=payload);assert r.status_code==200,r.text
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


def test_real_discovery_conflict_paused_poll_then_operator_resolution(identity_client,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.hook_formal_client import HookFormalClient
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    from chejin_worker_client.hook_progress import emit_progress,HookProgressTimeline
    from chejin_worker_client import storage
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch,submitted=True)
    assert post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'}).status_code==200
    timeline=HookProgressTimeline();events=[];logs=[]
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='paused'),on_runtime_process=lambda e:(events.append(e),timeline.apply(e)))
    runtime=object.__new__(HookFormalRuntime)
    runtime.scope=SimpleNamespace(**p);runtime.account=p;runtime.process=SimpleNamespace(generation=p['process_generation'])
    runtime.credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    runtime.client=HookFormalClient(identity_client.base_url,5);runtime.private_root=tmp_path/'runtime'
    runtime.gateway=SimpleNamespace(friend_list=lambda *a:SimpleNamespace(payload=body({'id':tid},p,prep)['friend_list']))
    runtime.progress=lambda *a:emit_progress(runner,*a)
    dispatcher=HookBusinessDispatcher(runtime.client,tmp_path/'dispatcher',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True)
    dispatcher.runtime=runtime
    monkeypatch.setattr(storage,'append_log',lambda *a,**kw:logs.append((a,kw)))
    for _ in range(3):
        dispatcher._friend_read_due=0
        dispatcher._discover_invitations(runner,[{'source_task_id':tid}])
    assert runner.binding.run_status=='paused'
    assert len(logs)==1 and logs[0][1]['task_id']==tid
    assert dispatcher.diagnostic_context=={'source_task_id':tid,'phase':'native_friend_binding'}
    assert len(timeline.steps)==1 and timeline.steps[0]['title']=='客户绑定冲突，等待运营处理'
    assert client.post(url,json=payload).status_code==200
    dispatcher._friend_read_due=0
    dispatcher._discover_invitations(runner,[{'source_task_id':tid}])
    assert timeline.steps[0]['state']=='done' and not dispatcher._binding_conflicts
    with SessionLocal() as db:assert db.get(Worker,w['id']).run_status=='paused'



def test_completed_invitation_with_registered_flow_is_not_overwritten(monkeypatch):
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch,submitted=True)
    with SessionLocal() as db:
        db.get(Worker,w['id']).inflight_flow_state={'flow_id':tid,'status':'active'}
        db.commit()
    response=client.post(url,json=payload)
    assert response.status_code==409 and response.json()['code']==transfer.BLOCK
    with SessionLocal() as db:
        assert transfer.grant(db,tid) is None
        assert db.get(WechatSessionBinding,b['binding_id']).bind_status=='bound'


def test_create_surfaces_transfer_without_granting_or_reinviting(monkeypatch):
    w,t,p,b,lead,tid,prep,url,payload=successor(monkeypatch)
    assert lead['next_action']=='review_wechat_transfer'
    with SessionLocal() as db:
        assert transfer.grant(db,tid) is None
        original=db.get(WechatSessionBinding,b['binding_id'])
        assert original.bind_status=='bound'
        assert original.replacement_binding_id is None
        assert db.get(Task,tid).claimed_at is None
        assert db.get(Task,tid).block_code==transfer.BLOCK
