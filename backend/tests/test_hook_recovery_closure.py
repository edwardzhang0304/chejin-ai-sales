"""Lost HTTP acknowledgements and interrupted voice through real recovery/resume.

Only native process/audio input and fault injection are synthetic. Local journals,
HTTP handlers, backend state machines, dispatcher and explicit resume are real.
"""
from dataclasses import replace
from types import SimpleNamespace
import threading
import pytest
from sqlalchemy import select,func
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.task import Task,TaskEvidence
from app.models.c3 import ReplyAction,SentAck,HandoffEvent
from app.models.wechat import MessageEvent
from app.services import c3_service
from test_hook_fault_recovery_http import clean,local_storage,runtime_for,scheduler_for,retained_scenario
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_business_http import post
from test_hook_account_http import heartbeat,context
from test_hook_history_business_http import bound,coverage,upload
from test_hook_native_media import media_coverage
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_recovery import reconcile
from chejin_worker_client.hook_voice import HookVoiceTranscriber,HookVoiceAudio
from chejin_worker_client.hook_inbox import HookReceiveScope
from chejin_worker_client.hook_formal_c1 import _save


def fault(w):
    r=post(w,f"/workers/{w['id']}/run-status",{'run_status':'faulted','client_instance_id':w['instance']})
    assert r.status_code==200,r.text


def resume(w,account,identity_client):
    with SessionLocal() as db:assert db.get(Worker,w['id']).run_status=='faulted'
    assert not storage.load_runtime_control()['inflight_flow_id']
    h=context(**{k:account[k] for k in ('account_epoch','process_generation')})
    r=heartbeat(w,h,run_status='faulted');assert r.status_code==200,r.text
    # The actual local recovery button path plus HTTP backend. Only native
    # window/process and live-thread health are controlled environmental facts.
    import time
    from chejin_worker_client.api import WorkerApiClient
    from chejin_worker_client.models import Binding
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.hook_worker_services import HookWorkerServices
    from chejin_worker_client import startup_check_state
    connection=SimpleNamespace(prepared=SimpleNamespace(process=SimpleNamespace()),
        gateway=SimpleNamespace(generation_inspector=lambda _:None),
        update_safety_snapshot=lambda:{'checking':False,'process_generation':account['process_generation']})
    services=HookWorkerServices(connection,SimpleNamespace(),intake_interval_seconds=.2,projection_batch_size=3,
        recovery_window_check=lambda _:None)
    services._startup_check=startup_check_state.passed()
    errors=[]
    runner=TaskRunner(WorkerApiClient(identity_client.base_url),SimpleNamespace(sidecar_active=lambda:False),
        on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,
        on_result=lambda _:None,on_error=errors.append,hook_services=services)
    runner.binding=Binding(w['id'],w['worker_token'],w['instance'],'faulted');storage.save_binding(runner.binding)
    runner._backend_confirmed_run_status='faulted'
    runner._backend_fault_recovery=r.json()['data']['fault_recovery']
    runner._recovery_heartbeat_at=time.monotonic()
    runner.last_rpa_component_status='ready';runner.last_wechat_status='logged_in'
    runner.post_update_runtime_health_snapshot=lambda:{'ready':True}
    runner._publish_fault_recovery()
    assert runner.fault_recovery_state()['ready'],runner.fault_recovery_state()
    assert runner.binding.run_status=='faulted'
    assert runner._request_fault_recovery()
    runner._process_fault_recovery()
    assert not errors,errors
    assert runner.binding.run_status==storage.load_binding().run_status=='running'
    with SessionLocal() as db:assert db.get(Worker,w['id']).run_status=='running'



@pytest.mark.parametrize('lost_recovery_reply',[False,True])
@pytest.mark.parametrize('kind',['add_friend','chat_reply'])
def test_start_never_delivered_recovers_original_intent_and_explicit_resume(identity_client,lab,tmp_path,monkeypatch,kind,lost_recovery_reply):
    conv=None
    if kind=='add_friend':
        w,task,rt=runtime_for(identity_client,lab,tmp_path)
    else:
        w,_,p,b=bound();value=coverage(b)
        r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
        with SessionLocal() as db:
            out=c3_service.generate_for_batch(db,batch_id=r.json()['data']['message_batch']['batch_id'],native_client_admitted=True);db.commit()
        task={'id':out['task_id']};conv=b['conversation_id']
        from chejin_worker_client.hook_backend import HookWorkerCredentials
        from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
        from test_hook_formal_runtime import calibrated_clock
        _,gateway,process,*_=lab
        p={**p,'process_generation':process.generation}
        assert heartbeat(w,context(account_epoch=p['account_epoch'],process_generation=p['process_generation']),run_status='running').status_code==200
        credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
        scope=HookReceiveScope(**p,hook_api_port=30001)
        prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),
            clock=calibrated_clock(identity_client,credentials,gateway,process,scope))
        rt=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,gateway,prepared,tmp_path/'original-runtime')
    original=HookFormalClient.call
    def fail_start(self,credentials,method,path,body=None,**kwargs):
        if path.endswith('/inflight-flow/start'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic disconnected request',0)
        return original(self,credentials,method,path,body,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,'call',fail_start)
        with pytest.raises(ApiError):rt.start_flow(task,conv)
    intent=rt.private_root/'flows'/f"{task['id']}.json";before=intent.read_bytes()
    fault(w)
    d=HookBusinessDispatcher(rt.client,rt.private_root,None,None,automatic_transcription_disabled=lambda:True);d.runtime=rt
    runner=SimpleNamespace(api=SimpleNamespace(),current_task=None)
    def inventory():return rt.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",{**rt.account,'retained_flow_id':task['id']})
    inv=inventory();assert not inv['inflight_flow'].get('flow_id') and not inv['retained_flow_terminal']
    if lost_recovery_reply:
        def lose(self,credentials,method,path,body=None,**kwargs):
            result=original(self,credentials,method,path,body,**kwargs)
            if path.endswith('/native-recovery'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic lost terminal receipt',0)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(HookFormalClient,'call',lose)
            with pytest.raises(ApiError):d._adopt_flow(runner,inv)
        assert storage.load_runtime_control()['inflight_flow_id']==task['id']
    d._adopt_flow(runner,inv)
    assert intent.read_bytes()==before
    with SessionLocal() as db:
        assert db.get(Task,task['id']).status=='cancelled'
        assert db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.evidence_type=='hook_recovery_terminal'))==1
        assert not db.get(Worker,w['id']).inflight_flow_state
    resume(w,rt.account,identity_client)
    delayed=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task['id'],'flow_kind':'chat_reply' if conv else 'task',**({'conversation_id':conv} if conv else {})})
    assert delayed.status_code==409,delayed.text
    assert not any(path=='/SendTextMsg' for path,*_ in lab[0].requests)


@pytest.mark.parametrize('lost_recovery_reply',[False,True])
def test_lost_claim_response_closes_without_permit_or_send_then_resumes(identity_client,tmp_path,monkeypatch,lost_recovery_reply):
    w,p,b,h,permit,value,d,runner,services=retained_scenario(identity_client,tmp_path,monkeypatch,invalidate=False,trigger=False,outgoing=False)
    # HTTP claim is committed by fixture; emulate response lost before local save.
    root=d.private_root/'sends'/w['id']/p['account_epoch']/permit['reply_action_id']
    (root/'permit.json').unlink()
    original_intent=(d.private_root/'flows'/f"{permit['flow_id']}.json").read_bytes()
    fault(w);runner.binding.run_status='faulted'
    original=HookFormalClient.call
    if lost_recovery_reply:
        def lose(self,credentials,method,path,body=None,**kwargs):
            result=original(self,credentials,method,path,body,**kwargs)
            if path.endswith('/native-recovery'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','lost recovery receipt',0)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(HookFormalClient,'call',lose)
            with pytest.raises(ApiError):d.tick(runner,services)
    for _ in range(3):d.tick(runner,services)
    with SessionLocal() as db:
        task=db.get(Task,permit['task_id']);action=db.get(ReplyAction,permit['reply_action_id'])
        assert task.status in {'failed','cancelled'}
        assert db.scalar(select(SentAck)).send_result=='failed'
        assert action.send_token==permit['send_token']
        assert not action.ai_payload.get('native_send_attempt')
        assert not db.get(Worker,w['id']).inflight_flow_state
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
    assert not list(root.glob('*.json'))
    assert (d.private_root/'flows'/f"{permit['flow_id']}.json").read_bytes()==original_intent
    resume(w,p,identity_client)
    # Delayed old permit cannot pass begin after negative settlement.
    r=post(w,f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/begin",
        {**p,'task_id':permit['task_id'],'send_token':permit['send_token'],'history_state_digest':b['history_state_digest'],'clock_protocol_version':1,
        'journal_sha256':'a'*64,'attempted_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()},h)
    assert r.status_code==409,r.text


@pytest.mark.parametrize('old_alive',[False,True])
def test_interrupted_voice_uses_original_parent_manual_disposition_and_resume(identity_client,tmp_path,monkeypatch,old_alive):
    w,_,p,b=bound();r,_=upload(w,p,b,media_coverage(b),monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'));mid=parent.raw_payload['native_msg_id']
    oldscope=HookReceiveScope(**p,hook_api_port=30001)
    voice=HookVoiceTranscriber(max_audio_bytes=10000,max_text_bytes=10000)
    audio=HookVoiceAudio(oldscope,b['peer_wxid'],mid,b'\x02#!SILK_V3'+b'x'*33)
    voice._reserve(audio)  # Durable boundary just before a killed process.
    original=voice.saved_parent(oldscope,b['peer_wxid'],mid)
    p={**p,'process_generation':'new-generation'}
    r=heartbeat(w,context(account_epoch=p['account_epoch'],process_generation=p['process_generation']));assert r.status_code==200
    fault(w)
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    rt=SimpleNamespace(account=p,scope=HookReceiveScope(**p,hook_api_port=30001),
        credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance']))
    d=HookBusinessDispatcher(HookFormalClient(identity_client.base_url,5),tmp_path/'voice-private',None,None,automatic_transcription_disabled=lambda:True)
    rt.clock=SimpleNamespace(observe=lambda _:None)
    d.runtime=rt;d.voice=voice
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='faulted'),stop_event=threading.Event(),api=SimpleNamespace(),current_task=None)
    services=SimpleNamespace(connection=SimpleNamespace(prepared=SimpleNamespace(registration=SimpleNamespace(scope=rt.scope),account=None)),
        _credentials=rt.credentials,startup_window_ready_for=lambda _:False)
    from chejin_worker_client import hook_process
    monkeypatch.setattr(hook_process,'inspect_windows_generation_exited',lambda generation:not old_alive)
    inv=d.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",p)
    if old_alive:
        with pytest.raises(Exception,match='HOOK_VOICE_ORIGINAL_PROCESS_NOT_EXITED'):d.tick(runner,services)
        with SessionLocal() as db:assert db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice')).item_state=='metadata_observed'
    else:
        d.tick(runner,services) # Recovery must run even while the Worker is faulted.
        receipts = {f: f.read_bytes() for f in d.private_root.rglob('*.json')}
        assert receipts
        assert d._recover_voice_attempts(inv['bindings']) is False  # No new work after persisted receipt.
        assert all(f.read_bytes() == value for f, value in receipts.items())
        with SessionLocal() as db:
            assert db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice')).item_state=='uncertain'
            assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        fresh=d.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",p)
        assert not any(x['voice_jobs'] for x in fresh['bindings'])
        resume(w,p,identity_client)
    assert voice.saved_parent(oldscope,b['peer_wxid'],mid)==original

@pytest.mark.parametrize('change',['account','epoch','fence','flow','original_generation','action','begun'])
def test_lost_claim_recovery_rejects_changed_authority_or_physical_begin(identity_client,tmp_path,monkeypatch,change):
    w,p,b,h,permit,_,d,runner,services=retained_scenario(identity_client,tmp_path,monkeypatch,
        invalidate=False,trigger=change=='begun',outgoing=False)
    body={**p,'kind':'send_claim','task_id':permit['task_id'],'flow_id':permit['flow_id'],
        'flow_kind':'chat_reply','conversation_id':b['conversation_id'],'reply_action_id':permit['reply_action_id'],
        'original_process_generation':p['process_generation'],'lease_fencing_token':permit['lease_fencing_token'],'evidence_digest':'b'*64}
    if change=='account':body['self_wxid']='wxid_other'
    if change=='epoch':body['account_epoch']='00000000-0000-0000-0000-000000000000'
    if change=='fence':body['lease_fencing_token']+=1
    if change=='flow':body['flow_id']='00000000-0000-0000-0000-000000000000'
    if change=='original_generation':body['original_process_generation']='other-process'
    if change=='action':body['reply_action_id']='00000000-0000-0000-0000-000000000000'
    before={f:f.read_bytes() for f in d.private_root.rglob('*.json')}
    r=post(w,f"/workers/{w['id']}/wechat/native-recovery",body);assert r.status_code==409,r.text
    with SessionLocal() as db:
        assert not db.scalar(select(SentAck))
        assert db.get(Worker,w['id']).inflight_flow_state
        assert db.get(ReplyAction,permit['reply_action_id']).send_token==permit['send_token']
    assert all(f.read_bytes()==raw for f,raw in before.items())


def test_voice_interruption_persistence_failure_keeps_original_attempt_and_recovers(monkeypatch,tmp_path):
    from contextlib import contextmanager
    scope=HookReceiveScope('wxid_sales_A','00000000-0000-0000-0000-000000000001','old-process',30001)
    v=HookVoiceTranscriber(max_audio_bytes=1000,max_text_bytes=1000)
    audio=HookVoiceAudio(scope,'wxid_customer_A','123',b'\x02#!SILK_V3'+b'test')
    v._reserve(audio);before=v.saved_parent(scope,audio.peer_wxid,'123')
    new=replace(scope,process_generation='new-process')
    import sqlite3
    connection=storage.db_connection
    @contextmanager
    def fail_write():
        with connection() as db:
            class Failing:
                def execute(self,sql,*args):
                    if sql.startswith('INSERT INTO hook_voice_interruptions'):raise sqlite3.OperationalError('disk full')
                    return db.execute(sql,*args)
                def commit(self):return db.commit()
            yield Failing()
    with monkeypatch.context() as patch:
        patch.setattr(storage,'db_connection',fail_write)
        with pytest.raises(sqlite3.Error):v.recover_interrupted(new,audio.peer_wxid,'123',generation_exited=lambda _:True)
    assert v.saved_parent(scope,audio.peer_wxid,'123')==before
    result=v.recover_interrupted(new,audio.peer_wxid,'123',generation_exited=lambda _:True)
    assert result['state']=='uncertain' and result['text'] is None
    assert v.recover_interrupted(new,audio.peer_wxid,'123',generation_exited=lambda _:True)==result
    assert v.saved_parent(scope,audio.peer_wxid,'123')==before
    with pytest.raises(Exception,match='ORIGINAL_PROCESS_NOT_EXITED'):
        v.recover_interrupted(scope,audio.peer_wxid,'123',generation_exited=lambda _:True)

@pytest.mark.parametrize('registered',[False,True])
def test_start_recovery_never_cancels_started_work(identity_client,lab,tmp_path,registered):
    w,t,rt=runtime_for(identity_client,lab,tmp_path)
    rt.start_flow(t)
    intent={'task_id':t['id'],'flow_id':t['id'],'flow_kind':'task','conversation_id':None}
    if registered:
        result=reconcile(rt,'flow_start',intent,evidence={'original':'test'})
        assert result['status']=='registered'
    else:
        rt.claim(t,t['id'])
        # Model a legacy inconsistent backend state; it is not absence proof.
        with SessionLocal() as db:
            db.get(Worker,w['id']).inflight_flow_state={};db.commit()
        with pytest.raises(ApiError) as error:reconcile(rt,'flow_start',intent,evidence={'original':'test'})
        assert error.value.code=='HOOK_RECOVERY_START_UNCONFIRMED'
    with SessionLocal() as db:
        assert db.get(Task,t['id']).status==('pending' if registered else 'running')
        assert not db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type=='hook_recovery_terminal'))


def test_interrupted_voice_diagnostic_export_contains_original_and_terminal_evidence(tmp_path):
    import json,zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    scope=HookReceiveScope('wxid_sales_A','00000000-0000-0000-0000-000000000001','old-process',30001)
    v=HookVoiceTranscriber(max_audio_bytes=1000,max_text_bytes=1000)
    audio=HookVoiceAudio(scope,'wxid_customer_A','123',b'\x02#!SILK_V3'+b'test')
    v._reserve(audio)
    v.recover_interrupted(replace(scope,process_generation='new-process'),audio.peer_wxid,'123',generation_exited=lambda _:True)
    target=tmp_path/'diagnostic.zip'
    export_bundle(tmp_path,target)
    with zipfile.ZipFile(target) as z:
        assert json.loads(z.read('state/hook_voice_attempts.json'))['rows'][0]['state']=='transcribing'
        assert json.loads(z.read('state/hook_voice_interruptions.json'))['rows'][0]['result_json']['state']=='uncertain'


def test_generic_evidence_cannot_manufacture_recovery_terminal():
    from test_hook_business_http import claimed
    w,t,p,h,prep=claimed()
    r=post(w,f"/tasks/{t['id']}/evidences",{'evidence_type':'hook_recovery_terminal',
        'metadata':{'request':{},'result':{'status':'not_started','may_call_hook':False}}},h)
    assert r.status_code==409,r.text
    assert r.json()['code']=='HOOK_RECOVERY_PROOF_ROUTE_REQUIRED'
    with SessionLocal() as db:
        assert not db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type=='hook_recovery_terminal'))
        assert db.get(Task,t['id']).status=='running'
