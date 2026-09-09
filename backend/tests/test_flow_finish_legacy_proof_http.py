"""Real HTTP/PG and Worker subprocess; synthetic legacy local receipt, no UI."""
import json, os, subprocess, sys
import pytest
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import http_api, isolated_db
import test_wechat_c2_api as c2t
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding

CHILD = r'''
import json,sys,time
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.storage import save_binding,load_binding,begin_runtime_flow,request_runtime_pause,save_c2_state,load_c2_state,load_runtime_control
req=json.loads(Path(sys.argv[1]).read_text())
class Transport(WorkerApiClient):
 def __init__(self):
  super().__init__(req['url']+'/api');self.proofs=0;self.finishes=[];self.heartbeats=0;self.pulls=0;self.proof_results=[]
 def _request(self,method,path,**kw):
  if 'read-authorization' in path:
   self.proofs+=1
   if req['mode'].startswith('proof_timeout') and self.proofs==1:raise TimeoutError('one proof transport failure')
   result=super()._request(method,path,**kw)
   self.proof_results.append(result.get('read_completion'))
   return result
  if path.endswith('/inflight-flow/finish'):
   record={'request':dict(kw.get('json') or {})};self.finishes.append(record)
   if req['mode']=='finish_timeout_once' and len(self.finishes)==1:
    record['outcome']='TimeoutError';raise TimeoutError('one finish transport failure')
   try:
    result=super()._request(method,path,**kw);record['outcome']='success';return result
   except Exception as exc:
    record['outcome']=getattr(exc,'code',type(exc).__name__);raise
  if path.endswith('/heartbeat'):self.heartbeats+=1
  if path.endswith('/pull'):self.pulls+=1
  return super()._request(method,path,**kw)
class Desktop(FakeBridge):
 def __init__(self):super().__init__(RpaResult(ok=True,result_code='unused'))
 def probe(self):return ('ready','logged_in')
 def sidecar_active(self):return False
 def prepare_startup_layout_for_new_transaction(self):return {'ok':True}
import chejin_worker_client.task_runner as worker_module
_original_save=worker_module.save_c2_state
_save_failures=[]
def save_proof(key,payload):
 if req['mode'].startswith('proof_save_') and key.startswith('inflight_finish_receipt:') and payload.get('terminal_kind')=='retry_required' and not _save_failures:
  _save_failures.append(True)
  if req['mode']=='proof_save_after':_original_save(key,payload)
  raise OSError('one proof persistence failure')
 return _original_save(key,payload)
worker_module.save_c2_state=save_proof
api=Transport();bridge=Desktop();errors=[]
binding=Binding(**req['binding'],run_status='paused');save_binding(binding)
begin_runtime_flow(req['flow_id'],'c2_read');request_runtime_pause()
# Synthetic legacy on-disk receipt, matching the known old success-vs-retry bug.
save_c2_state('inflight_finish_receipt:'+req['flow_id'],{'terminal_kind':'read_confirmed','conversation_id':req['conversation_id'],'error_code':None})
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
if req['mode']=='proof_timeout_without_priority':runner._retry_pending_flow_finish=lambda binding:False
runner.start(load_binding())
try:
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  if not load_runtime_control()['inflight_flow_id']:break
  time.sleep(.05)
 evidence={'mode':req['mode'],'proof_save_failures':len(_save_failures),'proofs':api.proofs,'proof_results':api.proof_results,'finishes':api.finishes,
  'heartbeats':api.heartbeats,'pulls':api.pulls,'runtime':load_runtime_control(),
  'receipt':load_c2_state('inflight_finish_receipt:'+req['flow_id']),
  'run_status':runner.binding.run_status,'can_start':runner._can_start_new_flow(),
  'health':runner.post_update_runtime_health_snapshot(),'wait':runner.flow_finish_wait_reason,
  'physical':{'locates':len(bridge.locate_chats),'reads':len(bridge.message_reads),'sends':len(bridge.sent_replies)},'errors':errors}
finally:runner.stop_for_update(timeout_seconds=10)
print(json.dumps(evidence))
'''

@pytest.mark.parametrize('mode',['proof_online','proof_timeout_once','finish_timeout_once','proof_timeout_without_priority','proof_save_before','proof_save_after'])
def test_formal_legacy_terminal_correction_survives_network_failure(http_api,monkeypatch,tmp_path,mode):
    monkeypatch.setattr(c2t,'client',http_api)
    worker=c2t._create_worker(); c2t._create_sales(worker['id'])
    c2t._create_lead('Independent legacy recovery fixture','13896676693')
    remark=c2t._pull_remark_code(worker)
    scan=http_api.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result",json=c2t._scan_payload(remark),headers=c2t._worker_headers(worker))
    assert scan.status_code==200,scan.text
    binding=scan.json()['data']['bindings'][0]; flow='independent-legacy-http-flow'
    headers={**c2t._worker_headers(worker),'X-Inflight-Flow-Id':flow}
    start=http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",json={'flow_id':flow,'flow_kind':'c2_read','conversation_id':binding['conversation_id'],'unread_generation':binding['unread_generation']},headers=c2t._worker_headers(worker))
    assert start.status_code==200,start.text
    ingest=http_api.post(f"/api/workers/{worker['id']}/wechat/messages/ingest",json=c2t._v3_ingest_payload(binding,remark,read_run_id=flow,messages=[],read_reason='visible_unread'),headers=headers)
    assert ingest.status_code==200,ingest.text
    completion=ingest.json()['data']['read_completion']; assert completion['result']=='retry_required',completion
    paused=http_api.post(f"/api/workers/{worker['id']}/run-status",json={'client_instance_id':'client-a','run_status':'paused'},headers=c2t._worker_headers(worker))
    assert paused.status_code==200,paused.text
    request={'url':http_api.get('/healthz').url.removesuffix('/healthz'),'mode':mode,'flow_id':flow,'conversation_id':binding['conversation_id'],
      'binding':{'worker_id':worker['id'],'worker_token':worker['worker_token'],'client_instance_id':'client-a'}}
    input_file=tmp_path/'input.json';input_file.write_text(json.dumps(request))
    result=subprocess.run([sys.executable,'-B','-c',CHILD,str(input_file)],capture_output=True,text=True,timeout=35,
      env={**os.environ,'PYTHONPATH':os.pathsep.join(str(p) for p in [c2t.WORKER_CLIENT_ROOT,c2t.WORKER_CLIENT_ROOT/'tests',c2t.OMNIAUTO_ROOT]),
       'CHEJIN_WORKER_HOME':str(tmp_path/'same-worker'),'CHEJIN_C2_ENABLED':'true','CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1'})
    assert result.returncode==0,result.stderr
    evidence=json.loads(result.stdout.splitlines()[-1]);evidence['formal_ingest_completion']=completion
    with SessionLocal() as db:
      w=db.get(Worker,worker['id']);b=db.get(WechatSessionBinding,binding['id'])
      evidence['backend']={'flow':w.inflight_flow_state,'run_status':w.run_status,'read_result':b.last_read_result,'read_run_id':b.last_read_run_id,'read_completed':str(b.last_read_completed_at),'next_read_due':str(b.next_read_due_at)}
    (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['proof_save_failures']==(1 if mode.startswith('proof_save_') else 0),evidence
    assert evidence['health']['ready'] and evidence['heartbeats']>0 and not evidence['pulls'],evidence
    assert not any(evidence['physical'].values()),evidence
    assert evidence['runtime']['pause_requested'] and evidence['run_status']=='paused' and not evidence['can_start'],evidence
    assert not evidence['runtime']['inflight_flow_id'] and not evidence['backend']['flow'],evidence
    assert sum(x['outcome']=='success' for x in evidence['finishes'])==1,evidence
