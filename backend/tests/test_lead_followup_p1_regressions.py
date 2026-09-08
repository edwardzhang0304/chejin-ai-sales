"""P1 regressions: real PostgreSQL/HTTP/Worker; synthetic UI and customers.

No physical WeChat execution is claimed. Network-boundary process exits model
lost responses; the restarted production loops choose and submit the receipt.
Set FOLLOWUP_P1_EVIDENCE_DIR to retain request/result and database evidence.
"""
import os
import json
import subprocess
import sys
from pathlib import Path
import pytest
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows, headers
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from test_lead_followup_eligibility import client, invalidate

ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('mode', ['normal', 'partition', 'lost_response_restart', 'confirmed_restart'])
def test_invalidation_after_final_authorization_before_ingest(http_api, tmp_path, mode):
    w, rows = fixture_rows()
    # Local HTTP fixture owns the loopback URL; derive it from a real request.
    response = http_api.get('/healthz')
    base = response.url.removesuffix('/healthz')
    script = r'''
import json,sys,requests,os,time
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding,WechatReadTarget,RpaResult
from chejin_worker_client.storage import save_binding,load_runtime_control,has_pending_c2_outbox,load_c2_state
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
base,w,row=sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3])
mode,phase=sys.argv[4:6]
exchange_file=Path(os.environ['CHEJIN_WORKER_HOME']).parent/'exchanges.json'
api=WorkerApiClient(base+'/api')
binding=Binding(w['id'],w['worker_token'],'followup-test',run_status='running' if phase=='read' else 'paused')
save_binding(binding)
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
if mode=='partition' and phase=='read':
    bridge.get_messages_payloads=[{'messages':[{'id':'text-'+str(i),'type':'text','sender_role':'customer','content':str(i)+':'+'x'*16000} for i in range(32)]}]
errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
runner.binding=binding
head={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':'followup-test'}
if phase=='read':
    targets=requests.get(base+'/api/workers/'+w['id']+'/wechat/sessions/read-targets',headers=head,timeout=10).json()['data']['targets']
    target=WechatReadTarget.from_api(next(x for x in targets if x['conversation_id']==row['conversation_id']))
original=api.session.send
exchanges=json.loads(exchange_file.read_text()) if exchange_file.exists() else []
def record(value):
    exchanges.append(value)
    exchange_file.write_text(json.dumps(exchanges))
def send(request,**kwargs):
    if phase=='read' and request.url.endswith('/inflight-flow/finish') and mode=='confirmed_restart':
        record({'phase':phase,'crash':'after_outbox_confirmed_before_flow_finish','receipt':load_c2_state(runner._inflight_finish_receipt_key(load_runtime_control()['inflight_flow_id']))})
        os._exit(17)
    if phase=='read' and request.url.endswith('/wechat/messages/ingest'):
        r=requests.post(base+'/api/leads/'+row['lead_id']+'/mark-invalid',json={'invalid_reason':'test_data'},timeout=10)
        r.raise_for_status()
    response=original(request,**kwargs)
    if request.url.endswith('/wechat/messages/ingest') or request.url.endswith('/inflight-flow/finish'):
        body=json.loads(request.body)
        record({'phase':phase,'url':request.url,'status':response.status_code,'response':response.json(),'partition':body.get('evidence',{}).get('ingest_partition'),'finish_request':body if request.url.endswith('/inflight-flow/finish') else None})
        if phase=='read' and mode=='lost_response_restart' and request.url.endswith('/wechat/messages/ingest'):
            os._exit(17)  # HTTP committed, process dies before Worker consumes response.
    return response
api.session.send=send
if phase=='read':
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False)
else:
    runner.start(binding)
    deadline=time.monotonic()+20
    while load_runtime_control()['inflight_flow_id'] and time.monotonic()<deadline:
        time.sleep(.1)
    runner.stop_for_update(timeout_seconds=5)
    assert not bridge.message_reads, bridge.c2_operation_order
    result={}  # Restart has no synchronous read result; assert actual HTTP/DB settlement below.
print(json.dumps({'result_ok':result.get('ok'),'error':result.get('error_code'),'exchanges':exchanges,'runtime':load_runtime_control(),'locked':lock_summary().get('locked'),'pending':has_pending_c2_outbox(),'errors':errors},default=str))
'''
    path=tmp_path/'run_case.py'
    path.write_text(script)
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'), 'CHEJIN_RPA_MODE':'mock', 'CHEJIN_UI_LOCK_LEASE_SECONDS':'1', 'CHEJIN_TASK_POLL_INTERVAL':'0.1',
         'PYTHONPATH':os.pathsep.join([str(ROOT/p) for p in ('worker-client','worker-client/tests','worker-client/omniauto-rpa')]+[os.environ.get('PYTHONPATH','')])}
    result=subprocess.run([sys.executable,str(path),base,json.dumps(w),json.dumps(rows[0]),mode,'read'],env=env,capture_output=True,text=True,timeout=60)
    if mode.endswith('_restart'):
        assert result.returncode==17,result.stderr
        result=subprocess.run([sys.executable,str(path),base,json.dumps(w),json.dumps(rows[0]),mode,'restart'],env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    evidence=json.loads(result.stdout.strip().splitlines()[-1])
    evidence_file('ingest-cancel-'+mode+'.json', evidence)
    assert any(x.get('url','').endswith('/wechat/messages/ingest') and x['status']==200 for x in evidence['exchanges']), evidence
    assert not evidence['runtime']['inflight_flow_id'],evidence
    if not mode.endswith('_restart'):
        assert evidence['error']=='LEAD_INVALID' and not evidence['result_ok'],evidence
    assert not evidence['pending'] and not evidence['locked'],evidence
    finishes=[x for x in evidence['exchanges'] if x.get('finish_request')]
    assert len(finishes)==1 and finishes[0]['status']==200, evidence
    assert finishes[0]['finish_request']['terminal_kind']=='read_cancelled',evidence
    ingests=[x for x in evidence['exchanges'] if x.get('url','').endswith('/wechat/messages/ingest')]
    if mode=='partition':
        assert len(ingests)>1 and ingests[-1]['partition']['index']==ingests[-1]['partition']['count'], evidence
        assert ingests[-1]['response']['data']['read_completion']['result']=='cancelled'
    elif mode=='lost_response_restart':
        assert len(ingests)==2 and ingests[-1]['response']['data']['duplicated_count']==1,evidence
    else:
        assert len(ingests)==1,evidence
    with SessionLocal() as db:
        assert not db.get(Worker,w['id']).inflight_flow_state
        from app.models.wechat import MessageEvent
        assert db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==rows[0]['conversation_id']))==(32 if mode=='partition' else 1)
        assert db.scalar(select(func.count()).select_from(ReplyAction))==0

def test_inconclusive_restore_read_must_not_clear_fresh_read_gate():
    from test_wechat_c2_api import _v3_ingest_payload
    from app.services.followup_eligibility import require_conversation_followup
    w,rows=fixture_rows()
    row=rows[0]
    invalidate(row['lead_id'])
    assert client.post('/api/leads/'+row['lead_id']+'/restore').status_code==200
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id'])
        assert b.followup_restore_pending
        b.unread_generation=1
        b.unread_hint=True
        db.commit()
    payload=_v3_ingest_payload({'id':row['binding_id'],'conversation_id':row['conversation_id']},'CJ3N95EU',
                              read_run_id='restore-inconclusive', messages=[],read_reason='waiting_sales_reply',unread_generation=1)
    response=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",json=payload,headers=headers(w))
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,row['binding_id'])
        evidence={'response':response.json(),'followup_restore_pending':binding.followup_restore_pending}
        try:
            require_conversation_followup(db,row['conversation_id'],require_fresh_read=True)
            evidence['fresh_read_gate']='allowed'
        except Exception as exc:
            evidence['fresh_read_gate']=getattr(exc,'code',type(exc).__name__)
        evidence_file('restore-inconclusive.json', evidence)
        assert response.json()['data']['read_completion']['result']=='retry_required',evidence
        assert binding.followup_restore_pending,evidence


from sqlalchemy import select, func, event, text
from app.models.c3 import MessageBatch, ReplyAction, HandoffEvent
from app.models.lead import Lead
from app.errors import AppError


def evidence_file(name, evidence):
    root = Path(os.environ.get('FOLLOWUP_P1_EVIDENCE_DIR', '/private/tmp/chejin-followup-p1-evidence'))
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))


@pytest.mark.parametrize('defect', ['missing_proof', 'frame_invalidated', 'history_gap', 'identity_unresolved', 'digest_mismatch', 'technical_failure', 'none'])
@pytest.mark.parametrize('unread_generation', [0, 1])
def test_restore_gate_requires_confirmed_complete_frame(defect, unread_generation):
    from test_wechat_c2_api import _production_worker_payload_for_test
    from app.services.followup_eligibility import require_conversation_followup
    w, rows = fixture_rows(); row = rows[0]
    invalidate(row['lead_id'])
    assert client.post('/api/leads/'+row['lead_id']+'/restore').status_code == 200
    with SessionLocal() as db:
        b = db.get(WechatSessionBinding, row['binding_id'])
        b.unread_generation = unread_generation
        b.unread_hint = bool(unread_generation)
        db.commit()
    payload = _production_worker_payload_for_test(binding={'id':row['binding_id'], 'conversation_id':row['conversation_id'], 'rpa_session_key':'test-0', 'unread_generation':unread_generation}, remark_code='CJ3N95EU', read_run_id='restore-'+defect, observations=[])
    e = payload['evidence']
    if defect == 'missing_proof': e.pop('send_context_guard')
    if defect == 'frame_invalidated': e['ui_frame_invalidated'] = True
    if defect == 'history_gap': e['history_gap'] = True
    if defect == 'identity_unresolved': e['flow_gate_errors'] = ['MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS']
    if defect == 'digest_mismatch': e['send_context_guard']['sequence_sha256'] = '0'*64
    if defect == 'technical_failure': e['flow_gate_errors'] = ['C2_IMAGE_IDENTITY_CONTRACT_INVALID']
    e['flow_gate_details']=[{'error_code':code,'position_source':'position_unavailable'} for code in e['flow_gate_errors']]
    response = client.post(f"/api/workers/{w['id']}/wechat/messages/ingest", json=payload, headers=headers(w))
    with SessionLocal() as db:
        b = db.get(WechatSessionBinding, row['binding_id'])
        proof = {'response':response.json(), 'pending':b.followup_restore_pending, 'consumed':b.consumed_unread_generation, 'handoff_count':db.scalar(select(func.count()).select_from(HandoffEvent)), 'batches':db.scalar(select(func.count()).select_from(MessageBatch)), 'replies':db.scalar(select(func.count()).select_from(ReplyAction))}
        evidence_file(f'restore-{defect}-{unread_generation}.json', proof)
        if defect == 'none':
            assert response.status_code == 200, proof
            assert response.json()['data']['read_completion']['result'] == 'no_change', proof
            assert not b.followup_restore_pending, proof
            require_conversation_followup(db, row['conversation_id'], require_fresh_read=True)
            assert b.consumed_unread_generation == unread_generation
        else:
            assert b.followup_restore_pending, proof
            with pytest.raises(AppError) as blocked:
                require_conversation_followup(db, row['conversation_id'], require_fresh_read=True)
            assert blocked.value.code == 'MESSAGE_AUTHORIZATION_REVISION_EXPIRED'
            assert proof['batches'] == proof['replies'] == 0
            assert proof['handoff_count'] == 2  # Both original sales Handoffs remain.
            assert b.consumed_unread_generation == 0
            if defect == 'technical_failure':
                assert response.status_code == 409 and response.json()['code']=='C2_IMAGE_IDENTITY_CONTRACT_INVALID',proof
            else:
                assert response.status_code == 200,proof
            if response.status_code == 200:
                assert response.json()['data']['read_completion']['result'] == 'retry_required', proof
            else:
                assert response.status_code == 409, proof


@pytest.mark.parametrize('first_path', ['finish', 'invalid'])
def test_finish_invalid_transactions_serialize_on_lead(first_path):
    """Schedule actual PG lock contention; no mocked lock or final state.

    The auditor's barrier waited for invalidation to own Lead *inside* the
    finish callback. That assumes the defective order and stalls correct
    Lead-first code. Here PG's own Lock wait establishes the overlap.
    """
    import threading, time
    from app.core.database import engine
    from app.models.task import Task
    from app.models.base import utcnow
    from app.services import worker_service, lead_service
    from app.schemas.worker import WorkerInflightFlowFinishRequest
    from app.schemas.lead import MarkInvalidRequest
    from test_lead_followup_eligibility import reply_fixture, actor
    from test_c3_api import _worker_headers
    if engine.dialect.name != 'postgresql': pytest.skip('PostgreSQL row-lock test')
    w, binding, lead_id, generated, lease = reply_fixture()
    task_id = generated['task_id']; conversation_id = binding['conversation_id']
    with SessionLocal() as db:
        db.get(WechatSessionBinding, binding['id']).recovery_hold = {'status':'active', 'first_seen_at':utcnow().isoformat(), 'error_code':'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'}
        db.commit()
    r = client.post(f"/api/workers/{w['id']}/inflight-flow/start", headers=_worker_headers(w), json={'flow_id':task_id, 'flow_kind':'chat_reply', 'conversation_id':conversation_id})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/reply-actions/{generated['reply_action_id']}/claim-send", headers={**_worker_headers(w), 'X-Task-Lease-Fencing-Token':str(lease), 'X-Inflight-Flow-Id':task_id}, json={'task_id':task_id, 'worker_id':w['id']})
    assert r.status_code == 200, r.text
    permit = r.json()['data']
    r = client.post(f"/api/reply-actions/{generated['reply_action_id']}/sent-ack", headers={**_worker_headers(w), 'X-Inflight-Flow-Id':task_id}, json={'task_id':task_id, 'worker_id':w['id'], 'client_instance_id':'client-c3', 'send_token':permit['send_token'], 'reply_text_hash':permit['reply_text_hash'], 'send_result':'sent', 'action_phase':'confirmed', 'sidecar_run_id':'synthetic-physical-receipt'})
    assert r.status_code == 200, r.text
    with SessionLocal() as db: assert db.get(Task, task_id).status == 'completed'
    root_locked = threading.Event(); release_first = threading.Event()
    pids = {}; orders = {'finish':[], 'invalid':[]}; errors = []
    def after_lock(conn, cursor, statement, parameters, context, executemany):
        path = threading.current_thread().name
        if path not in orders or 'FOR UPDATE' not in statement.upper(): return
        orders[path].append(statement.split('FROM ',1)[1].split()[0])
        if path == first_path and len(orders[path]) == 1:
            root_locked.set()
            if not release_first.wait(8): raise TimeoutError('Test scheduling timeout')
    def work(path):
        try:
            with SessionLocal() as db:
                pids[path] = db.scalar(text('SELECT pg_backend_pid()'))
                db.execute(text("SET LOCAL statement_timeout = '10s'"))
                if path == 'finish':
                    worker_service.finish_inflight_flow(db, db.get(Worker,w['id']), WorkerInflightFlowFinishRequest(flow_id=task_id, conversation_id=conversation_id, terminal_kind='task_terminal'), actor())
                else:
                    lead_service.mark_invalid(db, lead_id, MarkInvalidRequest(invalid_reason='test_data'), actor())
                db.commit()
        except Exception as exc: errors.append({'path':path, 'type':type(exc).__name__, 'error':str(exc)})
    second_path = 'invalid' if first_path == 'finish' else 'finish'
    threads = [threading.Thread(target=work, args=(p,), name=p) for p in [first_path, second_path]]
    event.listen(engine, 'after_cursor_execute', after_lock)
    lock_wait = None
    try:
        threads[0].start(); assert root_locked.wait(5)
        threads[1].start()
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            with engine.connect() as observer:
                pid = pids.get(second_path)
                if pid:
                    lock_wait = observer.execute(text('SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid=:pid'), {'pid':pid}).mappings().one()
                    if lock_wait['wait_event_type'] == 'Lock': break
            time.sleep(.02)
        assert lock_wait and lock_wait['wait_event_type'] == 'Lock', (lock_wait, errors)
    finally:
        release_first.set()
        for t in threads:
            if t.ident: t.join(15)
        event.remove(engine, 'after_cursor_execute', after_lock)
    with SessionLocal() as db:
        proof = {'first':first_path, 'lock_orders':orders, 'wait':dict(lock_wait or {}), 'errors':errors, 'threads_alive':[t.is_alive() for t in threads], 'flow':db.get(Worker,w['id']).inflight_flow_state, 'lead_status':db.get(Lead,lead_id).status, 'task_status':db.get(Task,task_id).status}
    evidence_file('locks-'+first_path+'.json', proof)
    assert not errors and not any(proof['threads_alive']), proof
    assert all('lead' in orders[path][0] for path in orders), proof
    assert not proof['flow'] and proof['lead_status'] == 'invalid' and proof['task_status'] == 'completed', proof


@pytest.mark.parametrize('first_read', ['inconclusive', 'partition', 'new'])
def test_restore_only_final_clean_read_can_create_new_batch(first_read):
    """Real HTTP accepts facts, then only a clean complete read opens C3."""
    import copy
    from test_wechat_c2_api import _v3_message
    from app.models.base import utcnow
    from app.models.c3 import Conversation
    from app.models.wechat import MessageEvent
    from app.services.followup_eligibility import require_conversation_followup
    w, rows = fixture_rows(); row = rows[0]
    with SessionLocal() as db:
        for h in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==row['conversation_id'])):
            # Explicit fixture: a customer under AI, unlike fixture_rows' sales Handoff.
            h.closed_at = utcnow()
        c = db.get(Conversation,row['conversation_id']); c.status='waiting_user_reply'; c.ai_enabled=True
        b = db.get(WechatSessionBinding,row['binding_id']); b.last_read_conversation_status='waiting_user_reply'
        db.commit()
    invalidate(row['lead_id'])
    assert client.post('/api/leads/'+row['lead_id']+'/restore').status_code==200
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);b.unread_generation=1;b.unread_hint=True;db.commit()
    binding={'id':row['binding_id'],'conversation_id':row['conversation_id'],'rpa_session_key':'test-0','unread_generation':1}
    # Explicit manual message fixture; real backend validation and guard builder.
    from test_wechat_c2_api import _v3_ingest_payload
    message=_v3_message('restore-fact',role='customer',message_type='text',content='需要了解这辆车',screen_order=1)
    payload=_v3_ingest_payload(binding,'CJ3N95EU',read_run_id='restore-facts',messages=[message],read_reason='waiting_user_reply',unread_generation=1)
    # Complete nonempty frame proof uses the production guard builder over
    # the actual observation, never a patched completeness predicate.
    from test_wechat_c2_api import build_send_context_guard
    e=payload['evidence'];e['tail_complete']=True
    guard=build_send_context_guard(e['observations'],layout_evidence={'ok':True,'layout_snapshot_id':'synthetic-restore','chat_header_bounds':[0,0,1000,100],'message_viewport_bounds':[0,100,1000,800],'input_bounds':[0,800,1000,1000]})
    e['send_context_guard']=guard;e['business_projection']=guard['sequence']
    responses=[]
    if first_read!='new':
        first=copy.deepcopy(payload)
        if first_read=='inconclusive': first['evidence'].pop('send_context_guard')
        else:
            first['evidence']['ingest_partition']={'group_id':'restore-facts','index':1,'count':2,'expected_source_message_keys':['restore-fact']}
            first['evidence']['flow_gate_errors']=['C2_INGEST_PARTITION_INCOMPLETE']
            first['evidence']['flow_gate_details']=[{'error_code':'C2_INGEST_PARTITION_INCOMPLETE','position_source':'position_unavailable'}]
        r=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",headers=headers(w),json=first)
        assert r.status_code==200,r.text;responses.append(r.json())
        with SessionLocal() as db:
            assert db.get(WechatSessionBinding,row['binding_id']).followup_restore_pending
            assert db.scalar(select(func.count()).select_from(MessageBatch))==0
            assert db.scalar(select(func.count()).select_from(MessageEvent))==1
            with pytest.raises(AppError):require_conversation_followup(db,row['conversation_id'],require_fresh_read=True)
        if first_read=='inconclusive':
            clean=_v3_ingest_payload(binding,'CJ3N95EU',read_run_id='restore-clean-new-run',messages=[message],read_reason='waiting_user_reply',unread_generation=1)
            clean['evidence'].update({k:e[k] for k in ['tail_complete','send_context_guard','business_projection']})
            payload=clean
        else: payload['evidence']['ingest_partition']={'group_id':'restore-facts','index':2,'count':2,'expected_source_message_keys':['restore-fact']}
    r=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",headers=headers(w),json=payload)
    responses.append(r.json())
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id'])
        batches=list(db.scalars(select(MessageBatch)))
        proof={'first_read':first_read,'responses':responses,'pending':b.followup_restore_pending,'batches':[{'id':x.id,'status':x.status,'message_event_ids':x.message_event_ids} for x in batches],'facts':db.scalar(select(func.count()).select_from(MessageEvent))}
        evidence_file('restore-followup-'+first_read+'.json',proof)
        assert r.status_code==200,proof
        assert not b.followup_restore_pending and len(batches)==1 and batches[0].status=='reply_action_created',proof
        assert len(batches[0].message_event_ids)==1 and proof['facts']==1,proof
        require_conversation_followup(db,row['conversation_id'],require_fresh_read=True)
    # A second restore must not revive the reply just invalidated above.
    original_batch_id=batches[0].id
    invalidate(row['lead_id'])
    assert client.post('/api/leads/'+row['lead_id']+'/restore').status_code==200
    again=_v3_ingest_payload(binding,'CJ3N95EU',read_run_id='second-restore-clean',messages=[message],read_reason='waiting_user_reply',unread_generation=1)
    again['evidence'].update({k:e[k] for k in ['tail_complete','send_context_guard','business_projection']})
    r=client.post(f"/api/workers/{w['id']}/wechat/messages/ingest",headers=headers(w),json=again)
    assert r.status_code==200,r.text
    from app.models.task import Task
    with SessionLocal() as db:
        assert not db.get(WechatSessionBinding,row['binding_id']).followup_restore_pending
        assert db.scalar(select(func.count()).select_from(MessageBatch))==1
        assert db.get(MessageBatch,original_batch_id).status=='cancelled'
        assert list(db.scalars(select(Task.status)))==['cancelled']
        evidence_file('restore-old-reply-preserved-'+first_read+'.json', {'response':r.json(),'old_batch_status':'cancelled','task_statuses':list(db.scalars(select(Task.status))),'batch_count':db.scalar(select(func.count()).select_from(MessageBatch))})
