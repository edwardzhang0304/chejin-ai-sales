"""Real HTTP/PostgreSQL/Worker SQLite; physical WeChat replaced by fixture frames.

Gate-only cases use a controlled six-message current-frame prefix. The separate
historical case replays the original previous PNG, verifies all six persisted
projections, then lets the production Worker confirm the seventh (sent) item.
Current broken OCR is the exact eight-message incident replay. Neither case
claims to use the original customer's SQLite or physical WeChat sender.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.worker import Worker
from app.models.c3 import MessageBatch, ReplyAction, Conversation, HandoffEvent
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.task import Task

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r'''
import os,sys,json,time
from pathlib import Path
from test_task_runner import FakeBridge, TaskRunnerTest
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding,WechatReadTarget,RpaResult
from chejin_worker_client.storage import save_binding,load_runtime_control,has_pending_c2_outbox,load_c2_state
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
base,w,row,mode,phase=sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3]),sys.argv[4],sys.argv[5]
folder=Path(os.environ['CHEJIN_WORKER_HOME']).parent
api=WorkerApiClient(base+'/api')
binding=Binding(w['id'],w['worker_token'],'followup-test',run_status='paused' if phase=='restart' else 'running')
save_binding(binding)
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
inputs=json.loads((folder/'frames.json').read_text())
bridge.get_messages_payloads=[inputs['seed' if phase=='seed' else 'current']]
if mode=='historical_frame' and phase=='seed':
 bridge.get_messages_payloads=[inputs['seed'] for _ in range(10)]
 observed=inputs['post_send']['observations']
 bridge.send_payload={**bridge.send_payload, **TaskRunnerTest._confirmed_send_sidecar_result(observations=observed, confirmed_observation_id=observed[-2]['observation_id'], run_id='controlled-historical-send')}

errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
runner.binding=binding
path=folder/'exchanges.json'
exchanges=json.loads(path.read_text()) if path.exists() else []
original=api.session.send
def record(value):
 exchanges.append(value);path.write_text(json.dumps(exchanges,ensure_ascii=False,default=str))
def send(request,**kwargs):
 if phase=='read' and request.url.endswith('/inflight-flow/finish') and mode in {'confirmed_restart','legacy_confirmed_restart'}:
  record({'phase':phase,'crash':'after_confirmed_outbox','receipt':load_c2_state(runner._inflight_finish_receipt_key(load_runtime_control()['inflight_flow_id']))})
  os._exit(17)
 response=original(request,**kwargs)
 if request.url.endswith('/wechat/messages/ingest') or request.url.endswith('/inflight-flow/finish') or request.url.endswith('/sent-ack') or '/read-authorization' in request.url or request.url.split('?')[0].endswith('/read-targets'):
  body=json.loads(request.body) if request.body else None
  record({'phase':phase,'url':request.url,'status':response.status_code,'response':response.json(),'request':body})
  if phase=='read' and ((mode=='lost_response_restart' and request.url.endswith('/wechat/messages/ingest')) or (mode=='legacy_rejected_restart' and request.url.endswith('/inflight-flow/finish') and response.status_code==409)):
   os._exit(17)
 return response
api.session.send=send
if phase=='restart':
 runner.start(binding)
 deadline=time.monotonic()+15
 while load_runtime_control()['inflight_flow_id'] and time.monotonic()<deadline:time.sleep(.1)
 runner.stop_for_update(timeout_seconds=5)
 assert not bridge.message_reads,bridge.c2_operation_order
 result={}
else:
 targets=api.get_wechat_read_targets(binding)
 target=next(t for t in targets if t.conversation_id==row['conversation_id'])
 result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=(mode=='historical_frame' and phase=='seed'))
print(json.dumps({'phase':phase,'result':result,'runtime':load_runtime_control(),'locked':lock_summary().get('locked'),'pending':has_pending_c2_outbox(),'errors':errors,'exchanges':exchanges,'physical_sends':len(bridge.sent_replies),'identity_state':load_c2_state('message_identity:'+row['conversation_id'])},default=str,ensure_ascii=False))
'''


@pytest.mark.parametrize('mode', ['normal', 'confirmed_restart', 'lost_response_restart', 'corrected_frame', 'legacy_confirmed_restart', 'legacy_rejected_restart', 'historical_frame'])
def test_read_gate_settles_through_real_backend_and_restart(http_api, tmp_path, mode, monkeypatch):
    # Re-run real OCR for fixture generation: no hand-written projection.
    from test_avatar_text_rows import replay_frame, PROVENANCE
    current, _, _ = replay_frame('current.png', tmp_path/'current')
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import build_message_observations_v3, sanitize_sidecar_contract_output
    positive = mode in {'corrected_frame', 'historical_frame'}
    if positive:
        # Keep production BackgroundTasks dispatch. Only the external AI
        # boundary is controlled; generation/guard/persistence are untouched.
        from app.core.config import get_settings
        from app.services import c3_service
        from app.services.ai_adapter import MockOmniAutoAIEngineAdapter
        monkeypatch.setattr(get_settings(), 'c3_ai_adapter_mode', 'real')
        monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', MockOmniAutoAIEngineAdapter)
    seed = current['messages'][:-1]
    if mode=='historical_frame':
        previous, _, _ = replay_frame('previous.png', tmp_path/'previous')
        seed = previous['messages']
    broken = PROVENANCE['replayed_broken_messages']
    def frame(messages):
        return sanitize_sidecar_contract_output({'messages':messages,'observations':build_message_observations_v3(messages, {'detected':False})})
    (tmp_path/'frames.json').write_text(json.dumps({'seed':previous if mode=='historical_frame' else frame(seed),'current':current if positive else frame(broken),'post_send':current},ensure_ascii=False))
    w, rows = fixture_rows()
    row=rows[0]
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);b.remark_code='CJNPTTDE';b.display_name='CJNPTTDE';db.commit()
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    script=tmp_path/'worker_case.py';script.write_text(SCRIPT)
    source=os.environ.get('C2_GATE_WORKER_SOURCE',str(ROOT/'worker-client'))
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock','CHEJIN_UI_LOCK_LEASE_SECONDS':'1',
         'PYTHONPATH':os.pathsep.join([source,str(ROOT/'worker-client/tests'),str(ROOT/'worker-client/omniauto-rpa'),os.environ.get('PYTHONPATH','')])}
    legacy_source = None
    if mode.startswith('legacy_'):
        legacy_source = tmp_path/'released-0.9.71'
        shutil.copytree(ROOT/'worker-client/chejin_worker_client', legacy_source/'worker-client/chejin_worker_client', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'contracts', legacy_source/'contracts')
        # Use the exact released TaskRunner with the same development contract
        # as this test backend. This is source recovery, not EXE upgrade UAT.
        old_runner = subprocess.check_output(['git', 'show', '5e2811678f6a7e579494aebb386e081a077806fb:worker-client/chejin_worker_client/task_runner.py'], cwd=ROOT)
        (legacy_source/'worker-client/chejin_worker_client/task_runner.py').write_bytes(old_runner)
    def run(phase):
        phase_env = dict(env)
        if legacy_source and phase == 'read':
            phase_env['PYTHONPATH'] = str(legacy_source/'worker-client') + os.pathsep + env['PYTHONPATH']
        p=subprocess.run([sys.executable,str(script),base,json.dumps(w),json.dumps(row),mode,phase],env=phase_env,capture_output=True,text=True,timeout=45)
        (tmp_path/(phase+'.stdout')).write_text(p.stdout)
        (tmp_path/(phase+'.stderr')).write_text(p.stderr)
        return p
    if mode=='historical_frame':
        from app.services.ai_adapter import MockOmniAutoAIEngineAdapter, AIEngineDecision
        text=current['messages'][-2]['content'].replace('\n','')
        monkeypatch.setattr(MockOmniAutoAIEngineAdapter, 'generate_reply_decision',
            lambda self, **kw: AIEngineDecision(decision='send_reply',reply_text=text,confidence=.9,guard_result='pass',evidence_refs=[],risk_flags=[],raw_payload={'adapter':'controlled historical reply from real screenshot'}))
        with SessionLocal() as db:
            for h in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==row['conversation_id'])): h.deleted_at=utcnow()
            c=db.get(Conversation,row['conversation_id']);c.status='waiting_user_reply';c.friend_state='friend_active'
            sales=Sales(sales_name='Synthetic historical salesperson',phone='13800009992',worker_id=w['id'],enabled=True)
            db.add(sales);db.flush();db.get(Lead,row['lead_id']).sales_id=sales.id
            db.get(WechatSessionBinding,row['binding_id']).sales_id=sales.id;db.commit()
    seed_result=run('seed')
    assert seed_result.returncode==0,seed_result.stderr
    seed_output=json.loads(seed_result.stdout.splitlines()[-1])
    assert seed_output['result']['ok'],seed_output
    assert not seed_output['runtime']['inflight_flow_id'],seed_output
    if mode=='historical_frame':
        assert seed_output['physical_sends']==1,seed_output
        assert len(seed_output['identity_state']['ai_reply_receipts'])==1,seed_output
        with SessionLocal() as db:
            stored=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id']).order_by(MessageEvent.ingested_at,MessageEvent.source_message_key)))
            projections=[m.raw_payload['business_projection'] for m in stored]
            assert [(p['sender_role'],p['normalized_content_signature']) for p in projections]==[(p['sender_role'],p['normalized_content_signature']) for p in PROVENANCE['historical_message_projections']],projections
        seed_output['verified_original_history']=projections
        (tmp_path/'historical-seed-proof.json').write_text(json.dumps(seed_output,ensure_ascii=False,default=str))
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,row['binding_id'])
        binding.unread_generation=1;binding.unread_hint=True
        binding.next_read_due_at=utcnow()-timedelta(seconds=1)
        if mode=='corrected_frame':
            # Controlled setup: return this test conversation to AI ownership.
            for handoff in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==row['conversation_id'])):
                handoff.deleted_at=utcnow()
            conversation=db.get(Conversation,row['conversation_id'])
            conversation.status='waiting_user_reply';conversation.friend_state='friend_active'
            sales=Sales(sales_name='Synthetic salesperson',phone='13800009991',worker_id=w['id'],enabled=True)
            db.add(sales);db.flush()
            db.get(Lead,row['lead_id']).sales_id=sales.id
            binding.sales_id=sales.id
        db.commit()
    suppressed=[]
    if mode=='corrected_frame' and os.environ.get('C2_GATE_DISABLE_AUTOMATIC_GENERATION')=='1':
        from app.api.routes import wechat as routes
        monkeypatch.setattr(routes, '_generate_message_batch', lambda *args: suppressed.append(args))
    result=run('read')
    if mode.endswith('_restart'):
        assert result.returncode==17,result.stderr
        result=run('restart')
    assert result.returncode==0,result.stderr
    evidence=json.loads(result.stdout.splitlines()[-1])
    if mode=='historical_frame':
        read_targets=next(x['response']['data']['targets'] for x in evidence['exchanges'] if x.get('phase')=='read' and x.get('url','').split('?')[0].endswith('/read-targets'))
        actual_target=next(t for t in read_targets if t['conversation_id']==row['conversation_id'])
        assert len(actual_target['identity_checkpoint']['recent_messages'])==6,actual_target
        alignment=evidence['result']['payload']['evidence']['sequence_alignment_evidence']
        assert alignment['alignment_status']=='unique' and alignment['old_tail_fully_consumed'] is True,alignment
        assert [p['pre_index'] for p in alignment['matched_pairs']]==list(range(1,7)),alignment
        receipt=seed_output['identity_state']['ai_reply_receipts'][0]
        assert alignment['matched_pairs'][-1]['worker_stable_id']==receipt['worker_stable_id'],alignment
        assert alignment['new_suffix_observation_ids']==[current['observations'][-1]['observation_id']],alignment
        evidence['historical_alignment']=alignment
        evidence['historical_checkpoint']=actual_target['identity_checkpoint']
        evidence['historical_seed']=seed_output
    if positive:
        # Observe autonomous output BEFORE testing repeat requests. This must
        # fail when the ingestion callback is disabled; tests cannot make it.
        deadline=time.monotonic()+5
        while True:
            with SessionLocal() as db:
                batch=db.scalar(select(MessageBatch).where(MessageBatch.conversation_id==row['conversation_id']).order_by(MessageBatch.created_at.desc()))
                actions=list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==batch.id)))
                tasks=list(db.scalars(select(Task).where(Task.reply_action_id.in_([a.id for a in actions]))))
                automatic={'batch_id': batch.id if batch else None, 'action_ids':[a.id for a in actions], 'task_ids':[t.id for t in tasks], 'suppressed_callbacks':len(suppressed), 'dispatch_mode':'production BackgroundTasks; controlled external AI'}
            if len(actions)==len(tasks)==1 or time.monotonic()>=deadline: break
            time.sleep(.05)
        (tmp_path/'automatic-before-any-generation-call.json').write_text(json.dumps(automatic))
        assert len(actions)==len(tasks)==1, {'automatic_generation_missing': automatic}
        assert tasks[0].reply_action_id==actions[0].id and actions[0].batch_id==batch.id
        evidence['automatic_generation']=automatic
        from app.services import c3_service
        with SessionLocal() as db:
            evidence['duplicate_generation']=c3_service.generate_for_batch(db,batch_id=batch.id)
            db.commit()
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);worker=db.get(Worker,w['id'])
        evidence['database']={'last_read_result':b.last_read_result,'inflight':worker.inflight_flow_state,
            'message_count':db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id'])),
            'batches':db.scalar(select(func.count()).select_from(MessageBatch).where(MessageBatch.conversation_id==row['conversation_id'])),
            'replies':db.scalar(select(func.count()).select_from(ReplyAction)),
            'reply_tasks':db.scalar(select(func.count()).select_from(Task).where(Task.task_type=='chat_reply'))}
    directory=Path(os.environ.get('C2_GATE_EVIDENCE_DIR',str(tmp_path)))
    directory.mkdir(parents=True,exist_ok=True)
    (directory/(mode+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
    assert not evidence['pending'] and not evidence['locked'],evidence
    assert not evidence['runtime']['inflight_flow_id'] and not evidence['database']['inflight'],evidence
    finishes=[x for x in evidence['exchanges'] if x.get('phase')!='seed' and x.get('url','').endswith('/inflight-flow/finish')]
    successes=[f for f in finishes if f['status']==200]
    rejected=[f for f in finishes if f['status']==409]
    assert len(successes)==1 and len(rejected)==({'legacy_confirmed_restart':1,'legacy_rejected_restart':2}.get(mode,0)),evidence
    if mode.startswith('legacy_'):
        assert all(f['request']['terminal_kind']=='read_confirmed' and f['response']['code']=='WORKER_INFLIGHT_FLOW_NOT_SETTLED' for f in rejected),evidence
    terminal='read_confirmed' if positive else 'retry_required'
    assert successes[0]['request']['terminal_kind']==terminal,evidence
    assert evidence['database']['last_read_result']==('new_facts' if positive else 'retry_required'),evidence
    assert evidence['database']['message_count']==(8 if mode=='historical_frame' else 7 if positive else 6),evidence
    assert evidence['physical_sends']==0,evidence
    assert evidence['database']['replies']==(2 if mode=='historical_frame' else 1 if positive else 0),evidence
    assert evidence['database']['reply_tasks']==(2 if mode=='historical_frame' else 1 if positive else 0),evidence


@pytest.mark.parametrize('scope', ['same_flow','other_read','other_conversation','task_flow','not_completed'])
def test_recovery_authorization_exposes_only_exact_persisted_read(http_api, scope):
    w,rows=fixture_rows();row=rows[0]
    flow={'flow_id':'old-flow','flow_kind':'c2_read','conversation_id':row['conversation_id'],'status':'draining'}
    with SessionLocal() as db:
        worker=db.get(Worker,w['id']);worker.inflight_flow_state=flow
        b=db.get(WechatSessionBinding,row['binding_id']);b.last_read_run_id='old-flow'
        b.last_read_result='retry_required';b.last_read_completed_at=utcnow();b.next_read_due_at=utcnow()+timedelta(seconds=5)
        if scope=='other_read': b.last_read_run_id='other-flow'
        elif scope=='other_conversation': worker.inflight_flow_state={**flow,'conversation_id':rows[1]['conversation_id']}
        elif scope=='task_flow': worker.inflight_flow_state={**flow,'flow_kind':'task'}
        elif scope=='not_completed': b.last_read_completed_at=None
        db.commit()
        original=(b.updated_at,b.last_read_run_id,b.last_read_result,dict(worker.inflight_flow_state))
    url=f"/api/workers/{w['id']}/wechat/conversations/{row['conversation_id']}/read-authorization"
    headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':'followup-test','X-Inflight-Flow-Id':'old-flow'}
    response=http_api.get(url,headers=headers)
    assert response.status_code==200,response.text
    proof=response.json()['data'].get('read_completion')
    assert bool(proof)==(scope=='same_flow'),response.text
    if proof: assert proof['read_run_id']=='old-flow' and proof['result']=='retry_required'
    assert http_api.get(url,headers={**headers,'X-Worker-Token':'wrong-token'}).status_code in {401,403}
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);worker=db.get(Worker,w['id'])
        assert (b.updated_at,b.last_read_run_id,b.last_read_result,worker.inflight_flow_state)==original
