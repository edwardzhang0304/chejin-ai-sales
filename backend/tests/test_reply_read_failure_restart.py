"""Formal Worker parent + same SQLite after process exit + real HTTP/PostgreSQL.

Only the desktop read boundary is controlled. No task/Flow settlement is mocked.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import HandoffEvent, ReplyAction, SentAck
from app.models.task import Task
from app.models.worker import Worker
from test_reply_sequence_http import generated_group, first_claim, ack, fixtures
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation

ERROR = 'C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED'
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r'''
import json,sys,time,os
from pathlib import Path
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding,RpaResult,WechatReadTarget
from chejin_worker_client import storage
from chejin_worker_client.task_runner import TaskRunner
from test_task_runner import FakeBridge
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar

base,raw,phase,mode=sys.argv[1:]
case=json.loads(raw)
api=WorkerApiClient(base+'/api')
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
bridge.sidecar_active=lambda:False  # This desktop double never launches a sidecar process.
reads=[]
def read(**kwargs):
 reads.append(kwargs)
 if phase=='seed_sequence':
  # Crash after the formal batch owner saved the continuation, before desktop I/O.
  print(json.dumps({'marker':storage.load_c2_state('reply_sequence_flow:'+case['flow_id'])}),flush=True)
  os._exit(0)
 return sidecar.sanitize_sidecar_contract_output(sidecar.exception_payload_for_sidecar(
   RuntimeError('C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED')))
bridge.get_messages=read
errors=[]
results=[]
runner=TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,
 on_step=lambda _:None,on_task=lambda _:None,on_result=results.append,on_error=errors.append)
events=[]
original=api.session.send
def send(request,**kwargs):
 is_failure=request.url.endswith('/tasks/'+case['task_id']+'/fail')
 if is_failure and phase=='read' and mode=='offline':
  events.append({'url':request.url,'offline':True})
  raise ConnectionError('offline before request')
 response=original(request,**kwargs)
 events.append({'url':request.url,'status':response.status_code})
 if is_failure and phase=='read' and mode=='lost_response':
  assert response.status_code==200,response.text
  raise ConnectionError('lost response after server commit')
 return response
api.session.send=send
if phase in {'read','seed_sequence'}:
 binding=Binding(case['worker']['id'],case['worker']['worker_token'],'client-c3',run_status='running')
 storage.save_binding(binding)
 runner.binding=binding
 entry=case.get('entry') if phase=='read' else None
 if entry in {'pending','running'}:
  pulled,task,reason=api.pull_task(binding)
  assert pulled=='pending' and task.id==case['task_id'],(pulled,task,reason)
  if entry=='running':
   api.claim_task(binding,task,claim_source='c2_conversation_flow',conversation_id=case['conversation_id'])
   pulled,task,reason=api.pull_task(binding)
   assert pulled=='running' and task.id==case['task_id'],(pulled,task,reason)
  # Only global vision configuration is controlled; no API key or task evidence is changed.
  runner.last_c2_vision_preflight_at=time.monotonic()
  runner.c2_vision_preflight_ready=True
  runner._execute_task(binding,task,pulled)
  assert reads and results[-1].error_code=='C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED',(results,errors)
 elif entry=='sequence':
  assert storage.load_runtime_control()['inflight_flow_id']==case['flow_id']
  from chejin_worker_client.ui_lock import lock_summary
  deadline=time.monotonic()+8
  while lock_summary().get('locked') and time.monotonic()<deadline:time.sleep(.1)
  assert not lock_summary().get('locked'),lock_summary()
  # The prior process saved this marker through the ordinary batch owner.
  marker=storage.load_c2_state('reply_sequence_flow:'+case['flow_id'])
  assert marker['batch_id']==case['batch_id'] and marker['conversation_id']==case['conversation_id']
  runner.tick_once()  # Heartbeat/reconciliation precede the formal sequence resume entry.
  assert reads,(errors,events,storage.read_logs(limit=20))
 else:
  assert runner._start_inflight_flow(binding,flow_id=case['flow_id'],flow_kind='c2_read',
   conversation_id=case['conversation_id'],unread_generation=0)
  target=WechatReadTarget.from_api(api.get_wechat_read_authorization(binding,case['conversation_id']))
  result=runner._wait_and_send_current_c3_batch(binding=binding,target=target,
   batch_id=case['batch_id'],cancel_check=lambda:False)
  assert reads,(result,errors)
  assert result['error_code']=='C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED',result
  assert result['reply_task_settled'] is False,result
  # The outer Flow owner must be blocked until the task receipt arrives.
  try:
   runner._finish_inflight_flow(binding,case['flow_id'],terminal_kind='technical_failed',
    conversation_id=case['conversation_id'],error_code=result['error_code'])
  except RuntimeError as exc:
   assert str(exc)=='RUNTIME_INFLIGHT_REPLY_READ_FAILURE_PENDING',str(exc)
  else:raise AssertionError('Flow finished before unsent reply task')
 assert binding.run_status=='faulted'
 receipt=storage.load_c2_state(runner._inflight_finish_receipt_key(case['flow_id']))
 assert receipt['reply_read_failure']['task_id']==case['task_id'],receipt
 assert receipt['reply_read_failure']['conversation_id']==case['conversation_id'],receipt
 assert storage.load_runtime_control()['inflight_flow_id']==case['flow_id']
 assert not receipt.get('reply_read_failure_confirmed'),receipt
else:
 binding=storage.load_binding()
 assert binding and binding.run_status=='faulted'
 assert storage.load_runtime_control()['inflight_flow_id']==case['flow_id']
 runner.start(binding)
 deadline=time.monotonic()+15
 while storage.load_runtime_control()['inflight_flow_id'] and time.monotonic()<deadline:time.sleep(.1)
 assert not reads and not bridge.message_reads,'restart performed a desktop read'
 assert not storage.load_runtime_control()['inflight_flow_id'],(errors,events)
 assert storage.load_binding().run_status=='faulted','recovery auto-started new work'
 # Use the real UI Start handler and background recovery, not a local status edit.
 from test_layout_recovery import click_start
 deadline=time.monotonic()+15
 while not runner.fault_recovery_state().get('ready') and time.monotonic()<deadline:time.sleep(.1)
 assert runner.fault_recovery_state().get('ready'),runner.fault_recovery_state()
 click_start(runner)
 deadline=time.monotonic()+10
 while binding.run_status!='running' and time.monotonic()<deadline:time.sleep(.05)
 assert binding.run_status=='running',(runner.fault_recovery_state(),errors)
 runner.stop_for_update(timeout_seconds=5)
 assert storage.load_binding().run_status=='running'
 receipt={}
assert not bridge.sent_replies
print(json.dumps({'receipt':receipt,'events':events,'errors':errors,'reads':len(reads)},default=str))
'''


def run_worker_phase(http_api, tmp_path, case, phase, mode):
    script = tmp_path / 'worker.py'
    script.write_text(SCRIPT)
    env = {**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path/'sqlite'), 'CHEJIN_RPA_MODE': 'mock',
        'CHEJIN_UI_LOCK_LEASE_SECONDS': '1', 'PYTHONPATH': os.pathsep.join([
            str(ROOT/'worker-client'), str(ROOT/'worker-client/tests'),
            str(ROOT/'worker-client/omniauto-rpa'), os.environ.get('PYTHONPATH', '')])}
    base = http_api.get('/healthz').url.removesuffix('/healthz')
    process = subprocess.run([sys.executable, str(script), base, json.dumps(case), phase, mode],
                             env=env, capture_output=True, text=True, timeout=50)
    (tmp_path/(phase+'.stdout')).write_text(process.stdout)
    (tmp_path/(phase+'.stderr')).write_text(process.stderr)
    assert process.returncode == 0, process.stdout+process.stderr
    return json.loads(process.stdout.splitlines()[-1])


@pytest.mark.parametrize('after_first', [False, True], ids=['before-send', 'between-segments'])
@pytest.mark.parametrize('mode', ['offline', 'lost_response'])
def test_unsent_read_failure_survives_restart(http_api, monkeypatch, tmp_path,
                                            async_generation, after_first, mode):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    flow_id = 'original-reply-read-flow'
    response = http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",
        headers=fixtures._worker_headers(worker), json={'flow_id':flow_id,'flow_kind':'c2_read',
            'conversation_id':binding['conversation_id'],'unread_generation':0})
    assert response.status_code == 200, response.text
    if after_first:
        ack(http_api, worker, first_claim(http_api, worker, binding, ids[0], flow_id=flow_id), flow_id=flow_id)
    with SessionLocal() as db:
        task_id = db.scalar(select(Task.id).where(Task.reply_action_id == ids[int(after_first)]))
    case = dict(worker=worker, conversation_id=binding['conversation_id'],
                flow_id=flow_id, batch_id=batch_id, task_id=task_id)
    original = run_worker_phase(http_api, tmp_path, case, 'read', mode)
    assert original['receipt']['reply_read_failure']['failure_step'] == (
        'reply_sequence_read' if after_first else 'pre_send_refresh')
    with SessionLocal() as db:
        assert db.get(Task,task_id).status == ('pending' if mode=='offline' else 'failed')
        assert db.get(Worker,worker['id']).inflight_flow_state['flow_id'] == flow_id
    recovered = run_worker_phase(http_api, tmp_path, case, 'restart', mode)
    assert recovered['reads'] == 0
    assert any(x['url'].endswith('/tasks/'+task_id+'/fail') and x.get('status')==200
               for x in recovered['events'])
    with SessionLocal() as db:
        task = db.get(Task,task_id)
        assert task.status == 'failed' and task.error_code == ERROR
        assert not task.lease_owner_worker_id
        assert all(db.get(ReplyAction,i).status in {'cancelled','superseded'} for i in ids[int(after_first):])
        assert db.query(HandoffEvent).filter(HandoffEvent.batch_id==batch_id).count() == 0
        assert db.query(SentAck).count() == int(after_first)
        if after_first:assert db.get(ReplyAction,ids[0]).status=='sent'
        restored = db.get(Worker,worker['id'])
        assert not restored.inflight_flow_state and restored.run_status=='running'


@pytest.mark.parametrize('entry', ['pending', 'running', 'sequence'])
@pytest.mark.parametrize('mode', ['offline', 'lost_response'])
def test_recovered_task_read_failure_survives_restart(
        http_api, monkeypatch, tmp_path, async_generation, entry, mode):
    worker, binding, batch_id, ids = generated_group(http_api, monkeypatch)
    if entry == 'sequence':
        flow_id = 'interrupted-reply-sequence'
        started = http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",
            headers=fixtures._worker_headers(worker), json={'flow_id': flow_id, 'flow_kind': 'c2_read',
                'conversation_id': binding['conversation_id'], 'unread_generation': 0})
        assert started.status_code == 200, started.text
        ack(http_api, worker, first_claim(http_api, worker, binding, ids[0], flow_id=flow_id), flow_id=flow_id)
    with SessionLocal() as db:
        task_id = db.scalar(select(Task.id).where(Task.reply_action_id == ids[int(entry == 'sequence')]))
    case = dict(worker=worker, conversation_id=binding['conversation_id'],
                flow_id=flow_id if entry == 'sequence' else task_id,
                batch_id=batch_id, task_id=task_id, entry=entry)
    if entry == 'sequence':
        seed = run_worker_phase(http_api, tmp_path, case, 'seed_sequence', mode)
        assert seed['marker']['batch_id'] == batch_id
    original = run_worker_phase(http_api, tmp_path, case, 'read', mode)
    record = original['receipt']['reply_read_failure']
    assert record['flow_kind'] == ('c2_read' if entry == 'sequence' else 'chat_reply')
    assert record['failure_step'] == 'pre_send_refresh'
    assert (record['lease_fencing_token'] > 0) == (entry == 'running')
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == (
            ('running' if entry == 'running' else 'pending') if mode == 'offline' else 'failed')
        assert db.get(Worker, worker['id']).inflight_flow_state['flow_id'] == case['flow_id']
    recovered = run_worker_phase(http_api, tmp_path, case, 'restart', mode)
    assert recovered['reads'] == 0
    assert any(event['url'].endswith('/tasks/'+task_id+'/fail') and event.get('status') == 200
               for event in recovered['events'])
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == 'failed' and task.error_code == ERROR
        assert task.lease_fencing_token == record['lease_fencing_token']
        assert not task.lease_owner_worker_id
        assert all(db.get(ReplyAction, identity).status in {'cancelled', 'superseded'}
                   for identity in ids[int(entry == 'sequence'):])
        assert db.query(HandoffEvent).filter(HandoffEvent.batch_id == batch_id).count() == 0
        assert db.query(SentAck).count() == int(entry == 'sequence')
        if entry == 'sequence':
            assert db.get(ReplyAction, ids[0]).status == 'sent'
        restored = db.get(Worker, worker['id'])
        assert not restored.inflight_flow_state and restored.run_status == 'running'


@pytest.mark.parametrize('damage', ['flow', 'client', 'customer', 'phase', 'fencing',
                                  'sending', 'sent', 'unknown'])
def test_failure_receipt_never_relabels_other_scope_or_send_result(
        http_api, monkeypatch, async_generation, damage):
    worker,binding,batch_id,ids = generated_group(http_api,monkeypatch)
    flow_id='scoped-failure-flow'
    headers={**fixtures._worker_headers(worker),'X-Inflight-Flow-Id':flow_id}
    started=http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",headers=headers,
        json={'flow_id':flow_id,'flow_kind':'c2_read','conversation_id':binding['conversation_id'],
              'unread_generation':0})
    assert started.status_code==200,started.text
    token=0
    if damage in {'sending','sent','unknown'}:
        claim=first_claim(http_api,worker,binding,ids[0],flow_id=flow_id)
        if damage!='sending':
            ack(http_api,worker,claim,flow_id=flow_id,result=damage,
                code='SEND_RESULT_UNKNOWN' if damage=='unknown' else None)
    with SessionLocal() as db:
        task=db.scalar(select(Task).where(Task.reply_action_id==ids[0]))
        task_id,token,before=task.id,task.lease_fencing_token,task.status
        before_action=db.get(ReplyAction,ids[0]).status
    record={'task_id':task_id,'worker_id':worker['id'],'client_instance_id':'client-c3',
        'conversation_id':binding['conversation_id'],'flow_id':flow_id,'flow_kind':'c2_read',
        'lease_fencing_token':token,'error_code':ERROR,'failure_step':'pre_send_refresh'}
    headers['X-Task-Lease-Fencing-Token']=str(token)
    if damage=='flow':headers['X-Inflight-Flow-Id']='different-flow'
    if damage=='client':headers['X-Client-Instance-Id']='different-client'
    if damage=='customer':record['conversation_id']='different-customer'
    if damage=='phase':record['failure_step']='after_send'
    if damage=='fencing':
        record['lease_fencing_token']=token+1
        headers['X-Task-Lease-Fencing-Token']=str(token+1)
    response=http_api.post(f'/api/tasks/{task_id}/fail',headers=headers,json={
        'error_code':ERROR,'failure_step':record['failure_step'],
        'failure_remark':ERROR,'evidence':{'reply_read_failure':record}})
    assert response.status_code in {401,403,409},response.text
    if damage in {'sending','sent','unknown'}:
        assert response.json()['code']=='REPLY_ACTION_SENT_ACK_REQUIRED',response.text
    with SessionLocal() as db:
        assert db.get(Task,task_id).status==before
        assert db.get(ReplyAction,ids[0]).status==before_action


def test_expired_original_task_lease_can_only_settle_unsent_result(
        http_api,monkeypatch,async_generation):
    worker,binding,batch_id,ids=generated_group(http_api,monkeypatch)
    headers={**fixtures._worker_headers(worker),'X-Inflight-Flow-Id':'expired-read-flow'}
    response=http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",headers=headers,
        json={'flow_id':'expired-read-flow','flow_kind':'c2_read',
              'conversation_id':binding['conversation_id'],'unread_generation':0})
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        task_id=db.scalar(select(Task.id).where(Task.reply_action_id==ids[0]))
    claimed=http_api.post(f'/api/tasks/{task_id}/claim',headers=headers,
        json={'worker_id':worker['id'],'claim_source':'c2_conversation_flow',
              'conversation_id':binding['conversation_id']})
    assert claimed.status_code==200,claimed.text
    headers.update(fixtures._task_lease_headers(worker,claimed))
    with SessionLocal() as db:
        task=db.get(Task,task_id)
        token=task.lease_fencing_token
        task.lease_expires_at=utcnow()-timedelta(seconds=5)
        db.commit()
    body={'error_code':ERROR,'failure_step':'pre_send_refresh','failure_remark':ERROR}
    for _ in range(2):
        failed=http_api.post(f'/api/tasks/{task_id}/fail',headers=headers,json=body)
        assert failed.status_code==200,failed.text
    with SessionLocal() as db:
        task=db.get(Task,task_id)
        assert task.status=='failed' and task.lease_fencing_token==token
        assert task.lease_expires_at is None and task.lease_owner_worker_id is None
        assert db.get(ReplyAction,ids[0]).status=='cancelled'
        assert not list(db.scalars(select(SentAck)))


@pytest.mark.parametrize('regression',['missing_save','legacy_handoff'])
def test_reversing_fix_breaks_the_formal_worker_positive(
        http_api,monkeypatch,tmp_path,async_generation,regression):
    if regression=='missing_save':
        broken="""from chejin_worker_client import reply_read_failure
reply_read_failure.save=lambda *args,**kwargs:case['flow_id']
"""
    else:
        broken="""from chejin_worker_client import task_runner
task_runner.PRE_SEND_TERMINAL_TECHNICAL_ERRORS -= {'C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED'}
"""
    assert SCRIPT.count('events=[]')==1
    monkeypatch.setattr(sys.modules[__name__],'SCRIPT',SCRIPT.replace('events=[]',broken+'events=[]'))
    expected = ("'reply_task_settled': True" if regression=='missing_save'
                else 'Flow finished before unsent reply task')
    with pytest.raises(AssertionError,match=expected):
        test_unsent_read_failure_survives_restart(
            http_api,monkeypatch,tmp_path,async_generation,False,'offline')


@pytest.mark.parametrize('entry', ['pending', 'running', 'sequence'])
def test_reversing_recovery_identity_breaks_same_http_positive(
        http_api, monkeypatch, tmp_path, async_generation, entry):
    # Restore precisely the old omission in the actual caller, plus its former
    # optional default. Keep the positive's inputs and assertions unchanged.
    broken = r'''
if phase=='read':
 import ast,inspect,textwrap
 from chejin_worker_client import reply_sequence_runtime
 function=(reply_sequence_runtime.resume_reply_sequence if case['entry']=='sequence'
           else TaskRunner._execute_c2_reply_recovery)
 tree=ast.parse(textwrap.dedent(inspect.getsource(function)))
 removed=0
 for node in ast.walk(tree):
  if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='_settle_chat_reply_context_failure_before_unlock':
   before=len(node.keywords)
   node.keywords=[k for k in node.keywords if k.arg!='conversation_id']
   removed+=before-len(node.keywords)
 assert removed==(1 if case['entry']=='sequence' else 3),removed
 namespace={}
 exec(compile(tree,'<restored-missing-recovery-identity>','exec'),function.__globals__,namespace)
 replacement=namespace[function.__name__]
 if case['entry']=='sequence':reply_sequence_runtime.resume_reply_sequence=replacement
 else:TaskRunner._execute_c2_reply_recovery=replacement
 TaskRunner._settle_chat_reply_context_failure_before_unlock.__kwdefaults__['conversation_id']=''
'''
    monkeypatch.setattr(sys.modules[__name__], 'SCRIPT', SCRIPT.replace('events=[]', broken+'\nevents=[]'))
    with pytest.raises(AssertionError, match="KeyError: 'reply_read_failure'"):
        test_recovered_task_read_failure_survives_restart(
            http_api, monkeypatch, tmp_path, async_generation, entry, 'offline')
