"""Independent cross-boundary regression. No production writes or physical sends."""
import json, os, subprocess, sys
from pathlib import Path
import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import http_api, isolated_db
import test_c3_api as c3t
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.models.worker import Worker
from app.models.audit import OperationLog

SCRIPT = r'''
import json,sys,time
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.models import Binding,RpaResult,ReplySendClaim
from chejin_worker_client.storage import save_binding,load_binding,load_runtime_control,save_reply_send_intent,load_reply_send_ack_outbox,save_c2_state
from chejin_worker_client.action_journal import action_journal_path
req=json.loads(Path(sys.argv[1]).read_text())
class Transport(WorkerApiClient):
 def __init__(self):
  super().__init__(req['url']+'/api');self.acks=0;self.fail_ack=True;self.finishes=0;self.heartbeats=0;self.pulls=0
  self.finish_failures=0;self.finish_boundaries=[]
 def _request(self,method,path,**kwargs):
  if path.endswith('/sent-ack'):
   self.acks+=1
   if self.fail_ack: raise TimeoutError('independent: ack network unavailable once')
  if path.endswith('/inflight-flow/finish'):
   self.finishes+=1
   if req.get('finish_failure'):
    boundary={'outbox':load_reply_send_ack_outbox(req['claim']['reply_action_id'])['status'],
     'flow':load_runtime_control()['inflight_flow_id'],'run_status':load_binding().run_status}
    self.finish_boundaries.append(boundary)
    if not self.finish_failures:
     self.finish_failures+=1
     if req['finish_failure']=='after':
      response=super()._request(method,path,**kwargs)
      assert response['finished'] is True
     raise TimeoutError('controlled: finish request or response lost once')
  if path.endswith('/heartbeat'):self.heartbeats+=1
  if path.endswith('/pull'):self.pulls+=1
  return super()._request(method,path,**kwargs)
class Desktop(FakeBridge):
 def __init__(self): super().__init__(RpaResult(ok=True,result_code='unused'));self.actions=0
 def probe(self):return ('ready','logged_in')
 def sidecar_active(self):return False
 def prepare_startup_layout_for_new_transaction(self):return {'ok':True}
 def run_add_friend(self,*args,**kwargs):self.actions+=1;raise AssertionError('must not do new physical work')
api=Transport();bridge=Desktop();errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
binding=Binding(**req['binding'],run_status='running');save_binding(binding);runner.binding=binding
claim=ReplySendClaim(**req['claim'])
assert runner._start_inflight_flow(binding,flow_id=claim.task_id,flow_kind='chat_reply',conversation_id=req['conversation_id'])
save_c2_state(runner._inflight_finish_receipt_key(claim.task_id),{'terminal_kind':'task_terminal','conversation_id':req['conversation_id'],'error_code':None})
save_reply_send_intent(reply_action_id=claim.reply_action_id,task_id=claim.task_id,send_token=claim.send_token,reply_text_hash=claim.reply_text_hash)
assert not runner._queue_and_submit_reply_send_ack(binding,claim,send_result='sent',action_phase='confirmed',reply_text_hash=claim.reply_text_hash)
first_finish_error=None
if req['mode'].startswith('finish_pending'):
 try:runner._finish_inflight_flow(binding,claim.task_id,terminal_kind='task_terminal',conversation_id=req['conversation_id'])
 except RuntimeError as exc:first_finish_error=str(exc)
 assert first_finish_error=='RUNTIME_INFLIGHT_SENT_ACK_PENDING',first_finish_error
api.fail_ack=False
runner.set_run_status('paused')
if req['mode']=='finish_pending_without_new_priority':
 # Test-only differential control: leave all original replay/finish guards intact.
 runner._retry_pending_flow_finish=lambda binding:False
runner.start(load_binding())
try:
 deadline=time.monotonic()+8
 while time.monotonic()<deadline:
  outbox=load_reply_send_ack_outbox(claim.reply_action_id)
  if outbox.get('status')=='confirmed' and not load_runtime_control()['inflight_flow_id']:break
  time.sleep(.05)
 result={'mode':req['mode'],'initial_finish_error':first_finish_error,'acks':api.acks,'finishes':api.finishes,'heartbeats':api.heartbeats,'pulls':api.pulls,
  'outbox':{k:outbox.get(k) for k in ['status','attempt_count','last_error']},
  'runtime':load_runtime_control(),'run_status':runner.binding.run_status,'can_start':runner._can_start_new_flow(),'wait_reason':runner.flow_finish_wait_reason,
  'thread_health':runner.post_update_runtime_health_snapshot(),'physical_actions':bridge.actions,'errors':errors}
finally:runner.stop_for_update(timeout_seconds=10)
print(json.dumps(result))
'''

@pytest.mark.parametrize('mode',['without_pending_finish','finish_pending','finish_pending_without_new_priority'])
def test_pending_ack_still_replays_after_finish_was_deferred(http_api,monkeypatch,tmp_path,mode):
 monkeypatch.setattr(c3t,'client',http_api)
 w,conversation=c3t._setup_bound_conversation()
 message=c3t._ingest(w,conversation['conversation_id'],'independent-ack-dependency','想了解15万SUV')
 generated=c3t._generate(c3t._collect(conversation['conversation_id'],message)['batch_id'])
 task_id=generated['task_id'];action_id=generated['reply_action_id']
 claim_task=http_api.post(f'/api/tasks/{task_id}/claim',headers=c3t._worker_headers(w),json={'worker_id':w['id'],'claim_source':'c2_conversation_flow','conversation_id':conversation['conversation_id']})
 assert claim_task.status_code==200,claim_task.text
 send=http_api.post(f'/api/reply-actions/{action_id}/claim-send',headers=c3t._task_lease_headers(w,claim_task),json={'task_id':task_id,'worker_id':w['id']})
 assert send.status_code==200,send.text
 s=send.json()['data']
 request={'mode':mode,'url':http_api.get('/healthz').url.removesuffix('/healthz'),'conversation_id':conversation['conversation_id'],
  'binding':{'worker_id':w['id'],'worker_token':w['worker_token'],'client_instance_id':'client-c3'},
  'claim':{k:s.get(k) for k in ['reply_action_id','task_id','send_token','reply_text','reply_text_hash','conversation_id','rpa_session_key','expire_at']}}
 request_file=tmp_path/'request.json';request_file.write_text(json.dumps(request))
 env={**os.environ,'PYTHONPATH':os.pathsep.join(str(p) for p in [c3t.WORKER_CLIENT_ROOT,c3t.WORKER_CLIENT_ROOT/'tests',c3t.OMNIAUTO_ROOT]),
  'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_C2_ENABLED':'true','CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1'}
 proc=subprocess.run([sys.executable,'-B','-c',SCRIPT,str(request_file)],capture_output=True,text=True,timeout=30,env=env)
 assert proc.returncode==0,proc.stderr
 evidence=json.loads(proc.stdout.splitlines()[-1])
 with SessionLocal() as db:
  evidence['backend']={'task_status':db.get(Task,task_id).status,'action_status':db.get(ReplyAction,action_id).status,
   'ack_count':len(list(db.scalars(select(SentAck.id).where(SentAck.reply_action_id==action_id)))),
   'flow':db.get(Worker,w['id']).inflight_flow_state}
 (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
 assert evidence['thread_health']['ready'] and evidence['heartbeats']>=1 and evidence['physical_actions']==0,evidence
 assert evidence['outbox']['status']=='confirmed',evidence
 assert evidence['backend']['task_status']=='completed' and evidence['backend']['ack_count']==1,evidence
 assert not evidence['runtime']['inflight_flow_id'] and not evidence['backend']['flow'],evidence
 assert not evidence['can_start'] and evidence['run_status']=='paused' and evidence['runtime']['pause_requested'],evidence
 assert evidence['pulls']==0,evidence


TASK_FINALLY_SCRIPT = SCRIPT.split('api=Transport();')[0] + r'''
from chejin_worker_client.models import Task
from chejin_worker_client.storage import load_c2_state
api=Transport();bridge=Desktop();errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
claim=ReplySendClaim(**req['claim'])
if req['stage'] != 'resume':
 binding=Binding(**req['binding'],run_status='running');save_binding(binding);runner.binding=binding
 def completed_physical_boundary(binding,task,mode):
  # Sending/context are controlled fixtures. Original _execute_task finally,
  # sent_ack storage, retry loops and backend settlement are never replaced.
  save_reply_send_intent(reply_action_id=claim.reply_action_id,task_id=claim.task_id,send_token=claim.send_token,reply_text_hash=claim.reply_text_hash)
  assert not runner._queue_and_submit_reply_send_ack(binding,claim,send_result='sent',action_phase='confirmed',reply_text_hash=claim.reply_text_hash)
  runner.set_run_status(req['run_status'])
 runner._execute_c2_reply_recovery=completed_physical_boundary
 task=Task.from_api(req['task'])
 if req['stage']=='warm':
  runner.set_run_status('paused');runner.start(load_binding());binding=runner.binding
  deadline=time.monotonic()+5
  while runner._restart_backend_probe_pending and time.monotonic()<deadline:time.sleep(.02)
  assert not runner._restart_backend_probe_pending
 with runner._restart_recovery_lock,runner._new_work_admission_lock:
  if req['stage']=='warm':runner.set_run_status('running')
  runner._execute_task(binding,task,'pending')
  receipt=load_c2_state(runner._inflight_finish_receipt_key(task.id))
  assert receipt['finish_stage']=='dependencies' and runner.current_task is None
  assert api.finishes==0 and api.acks==1
  before={'stage':receipt['finish_stage'],'restart_flow':runner._restart_recovery_flow_id,'wait':runner.flow_finish_wait_reason}
  if req['stage']=='warm':assert before['restart_flow'] is None
  api.fail_ack=False
 if req['stage']=='prepare':
  print(json.dumps({'before':before,'physical_actions':bridge.actions}));sys.exit(0)
else:
 api.fail_ack=False;before={};runner.start(load_binding())
try:
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  outbox=load_reply_send_ack_outbox(claim.reply_action_id)
  if outbox.get('status')=='confirmed' and not load_runtime_control()['inflight_flow_id']:break
  time.sleep(.05)
 result={'before':before,'outbox':outbox['status'],'runtime':load_runtime_control(),
  'acks':api.acks,'finishes':api.finishes,'pulls':api.pulls,'run_status':runner.binding.run_status,
  'physical_actions':bridge.actions,'health':runner.post_update_runtime_health_snapshot(),
  'finish_failures':api.finish_failures,'finish_boundaries':api.finish_boundaries}
finally:runner.stop_for_update(timeout_seconds=10)
print(json.dumps(result))
'''


@pytest.mark.parametrize('lifecycle,run_status,finish_failure',[
 ('warm','paused',None),('restart','paused',None),
 ('warm','faulted',None),('restart','faulted',None),
 ('warm','faulted','before'),('restart','paused','after'),
])
def test_task_finally_dependency_recovery_across_lifecycle_and_stop(http_api,monkeypatch,tmp_path,lifecycle,run_status,finish_failure):
 monkeypatch.setattr(c3t,'client',http_api)
 w,conversation=c3t._setup_bound_conversation()
 message=c3t._ingest(w,conversation['conversation_id'],'dependency-task-finally','了解车辆信息')
 generated=c3t._generate(c3t._collect(conversation['conversation_id'],message)['batch_id'])
 task_id,action_id=generated['task_id'],generated['reply_action_id']
 claim_task=http_api.post(f'/api/tasks/{task_id}/claim',headers=c3t._worker_headers(w),json={'worker_id':w['id'],'claim_source':'c2_conversation_flow','conversation_id':conversation['conversation_id']})
 assert claim_task.status_code==200,claim_task.text
 send=http_api.post(f'/api/reply-actions/{action_id}/claim-send',headers=c3t._task_lease_headers(w,claim_task),json={'task_id':task_id,'worker_id':w['id']})
 assert send.status_code==200,send.text
 data=send.json()['data']
 request={'run_status':run_status,'finish_failure':finish_failure,'url':http_api.get('/healthz').url.removesuffix('/healthz'),'conversation_id':conversation['conversation_id'],
  'binding':{'worker_id':w['id'],'worker_token':w['worker_token'],'client_instance_id':'client-c3'},
  'task':claim_task.json()['data'],
  'claim':{k:data.get(k) for k in ['reply_action_id','task_id','send_token','reply_text','reply_text_hash','conversation_id','rpa_session_key','expire_at']}}
 evidence={}
 for stage in (['warm'] if lifecycle=='warm' else ['prepare','resume']):
  input_file=tmp_path/'request.json';input_file.write_text(json.dumps({**request,'stage':stage}))
  proc=subprocess.run([sys.executable,'-B','-c',TASK_FINALLY_SCRIPT,str(input_file)],capture_output=True,text=True,timeout=35,
   env={**os.environ,'PYTHONPATH':os.pathsep.join(str(p) for p in [c3t.WORKER_CLIENT_ROOT,c3t.WORKER_CLIENT_ROOT/'tests',c3t.OMNIAUTO_ROOT]),
    'CHEJIN_WORKER_HOME':str(tmp_path/'same-worker'),'CHEJIN_C2_ENABLED':'true','CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1'})
  assert proc.returncode==0,proc.stderr
  evidence[stage]=json.loads(proc.stdout.splitlines()[-1])
 final=evidence['warm' if lifecycle=='warm' else 'resume']
 with SessionLocal() as db:
  evidence['backend']={'task_status':db.get(Task,task_id).status,
   'action_status':db.get(ReplyAction,action_id).status,
   'ack_count':len(list(db.scalars(select(SentAck.id).where(SentAck.reply_action_id==action_id)))),
   'flow':db.get(Worker,w['id']).inflight_flow_state,
   'finish_count':len(list(db.scalars(select(OperationLog.id).where(
    OperationLog.event_type=='worker_inflight_finished',OperationLog.target_id==task_id))))}
 (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
 assert final['outbox']=='confirmed' and not final['runtime']['inflight_flow_id'],evidence
 assert final['finishes']==(2 if finish_failure else 1) and final['physical_actions']==0 and final['pulls']==0,evidence
 assert final['finish_failures']==(1 if finish_failure else 0),evidence
 if finish_failure:
  assert len(final['finish_boundaries'])==2,evidence
  assert all(b=={'outbox':'confirmed','flow':task_id,'run_status':run_status} for b in final['finish_boundaries']),evidence
 assert final['run_status']==run_status and final['runtime']['pause_requested'] and final['health']['ready'],evidence
 assert evidence['backend']['task_status']=='completed' and evidence['backend']['action_status']=='sent',evidence
 assert evidence['backend']['ack_count']==evidence['backend']['finish_count']==1,evidence
 assert not evidence['backend']['flow'],evidence
