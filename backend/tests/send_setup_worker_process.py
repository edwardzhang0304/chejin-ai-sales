"""Worker process harness: controlled desktop/model, actual Worker/HTTP/SQLite.

Only window I/O is replaced. Popen failure is an actual missing executable;
no harness code submits sent-ACK, finishes a Flow, or generates a reply.
"""
import json,os,sys,time,uuid
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch
import requests
from chejin_worker_client import storage,rpa_bridge,send_setup_recovery,omniauto_vision
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
from test_task_runner import FakeBridge,TaskRunnerTest
from apps.wechat_ai_customer_service.adapters import send_launch_journal as launches,send_request_admission
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar

base,w,rows,phase,mode=sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3]),sys.argv[4],sys.argv[5]
folder=Path(os.environ['CHEJIN_WORKER_HOME']).parent
api=WorkerApiClient(base+'/api')
if phase=='initial':
 binding=Binding(w['id'],w['worker_token'],'followup-test',run_status='running');storage.save_binding(binding)
else:
 binding=storage.load_binding();assert binding is not None
bridge=FakeBridge(RpaResult(ok=True,result_code='invite_sent'))
transport=rpa_bridge.RpaBridge();transport.mode='real'
bridge.send_transaction_journal_path=transport.send_transaction_journal_path
bridge.sidecar_active=transport.sidecar_active
bridge.active_artifact_dirs=transport.active_artifact_dirs
bridge.send_reply=transport.send_reply
native_messages=bridge.get_messages
screen_path=folder/'desktop-frames.json'
screen=json.loads(screen_path.read_text()) if screen_path.exists() else {}
def complete_frame(**kwargs):
 target=kwargs['display_name']
 if target in screen:
  bridge.get_messages_payloads.append({'ok':True,'messages':screen[target]})
 value=native_messages(**kwargs)
 # This controlled desktop contains the entire one-message conversation.
 # Declare that fixture boundary explicitly; FakeBridge's generic default is
 # intentionally incomplete and cannot authorize all-OLD pending recovery.
 value['tail_complete']=True
 # Native OCR normally supplies bubble geometry. Without it the generic mock
 # uses observation-index fallback, which correctly cannot prove an all-OLD
 # frame's persisted source order. Model this complete desktop's actual slot.
 for i,observation in enumerate(value['observations']):
  rect=[40,200+i*70,400,250+i*70]
  observation['bubble_rect']=rect
  observation['source_message']['bubble_rect']=rect
 return value
bridge.get_messages=complete_frame
physical=[];commands=[];errors=[];wire=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,
                  on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
runner.binding=binding
real_send=api.session.send

def exchange(request,**kw):
 data=json.loads(request.body) if request.body else {}
 route=request.url.removeprefix(api.base_url)
 entry={'path':route,'phase':phase,'method':request.method}
 if phase=='initial' and ((mode=='stop_offline' and route.endswith('/run-status') and data.get('run_status')=='faulted')
                         or (mode=='ack_offline' and route.endswith('/sent-ack'))):
  wire.append({**entry,'injected':'request_not_delivered'})
  raise requests.ConnectionError('controlled network outage before delivery')
 response=real_send(request,**kw)
 wire.append({**entry,'status':response.status_code})
 (folder/(phase+'-wire.json')).write_text(json.dumps(wire,ensure_ascii=False))
 if phase=='initial' and mode=='ack_response_lost' and route.endswith('/sent-ack') and response.status_code==200:
  os._exit(17)
 return response
api.session.send=exchange
native_popen=rpa_bridge.subprocess.Popen
native_fail=launches.fail

def fail(*args,**kw):
 value=native_fail(*args,**kw)
 if phase=='initial' and mode=='crash_before_stop':os._exit(17)
 return value
launches.fail=fail
native_update=launches.update
def update(*args,**kw):
 value=native_update(*args,**kw)
 if phase=='initial' and mode=='crash_finished_unknown' and kw.get('process_state')=='finished':os._exit(17)
 return value
launches.update=update

class UnknownProcess:
 def __init__(self,cmd,**kw):self.args=cmd;self.returncode=0;self.pid=123457
 def communicate(self,timeout=None):return json.dumps({'ok':False,'error_code':'RPA_SIDECAR_PROTOCOL_INVALID'}),''

class DesktopProcess:
 def __init__(self,command,**kw):
  self.args=command;self.returncode=0;self.pid=123456
  def option(flag):return command[command.index(flag)+1]
  args=Namespace(action='send',target=option('--target'),text=option('--text'),expected_context_guard='',
      action_journal=option('--action-journal'),expected_context_guard_file=option('--expected-context-guard-file'),
      expected_context_guard_sha256=option('--expected-context-guard-sha256'),send_task_id=option('--send-task-id'),send_action_id=option('--send-action-id'))
  guard,rejection=send_request_admission.admit(args)
  assert rejection is None,rejection
  physical.append({'target':args.target,'text':args.text,'action':args.send_action_id})
  # Controlled physical action and its authoritative confirmation. Use the
  # production comparator against ordered before/after observations.
  before=bridge.last_message_payload
  messages=[dict(o.get('source_message') or {}) for o in before['observations']]
  messages.append({'id':'sent-'+uuid.uuid4().hex,'sender_role':'self','type':'text','content':args.text,'ocr_confidence':1,
                   'bubble_rect':[40,200+len(messages)*70,400,250+len(messages)*70]})
  screen[args.target]=messages
  screen_path.write_text(json.dumps(screen,ensure_ascii=False))
  after=bridge._contractual_message_payload({'ok':True,'messages':messages,'sidecar_run_id':'controlled-send-frame'})
  old_sequence=[{'observation_id':o['observation_id'],'sender_role':o['sender_role'],'row_kind':o['row_kind'],'content_normalized':o.get('content_clean','')} for o in before['observations']]
  snapshot={'ok':True,'validation':{'confirmed_target':args.target},'input_region':{'has_visible_text':False},'observations':after['observations'],
      'message_sequence':[{'observation_id':o['observation_id'],'sender_role':o['sender_role'],'row_kind':o['row_kind'],'content_normalized':o.get('content_clean','')} for o in after['observations']]}
  confirmed=sidecar.confirm_reply_sent(1,target=args.target,text=args.text,exact=True,baseline_match_count=0,
      baseline_message_sequence=old_sequence,initial_snapshot=snapshot,max_attempts=1)
  assert confirmed['ok'],confirmed
  sidecar.write_action_phase_journal(args.action_journal,'confirmed',business_state='sent',business_result_confirmed=True)
  value={'ok':True,'sidecar_run_id':'controlled-physical-send','action_phase':'confirmed','physical_send_triggered':True,
      'send_result':{'ok':True,'confirmed':True,'result':'sent','action_phase':'confirmed','physical_send_triggered':True,'sent_confirmation':confirmed}}
  self.stdout=json.dumps(sidecar.sanitize_sidecar_contract_output(value))
 def communicate(self,timeout=None):return self.stdout,''
 def terminate(self):raise AssertionError('unexpected terminate of completed controlled desktop')
 def kill(self):raise AssertionError('unexpected kill of completed controlled desktop')

def create(command,**kw):
 if '--expected-context-guard-file' not in command:return native_popen(command,**kw)
 commands.append(command)
 if phase=='initial':
  if mode=='segmented' and len(commands)==1:return DesktopProcess(command,**kw)
  if mode=='crash_creating':os._exit(17)
  if mode=='crash_finished_unknown':return UnknownProcess(command,**kw)
  return native_popen([str(folder/'missing-native-executable'),*command[1:]],**kw)
 return DesktopProcess(command,**kw)
rpa_bridge.subprocess.Popen=create
omniauto_vision.vision_configuration_status=lambda:{'ready':True}

def dump(result):
 out={'phase':phase,'mode':mode,'result':result,'run_status':binding.run_status,'runtime':storage.load_runtime_control(),
      'pending_ack':storage.has_pending_reply_send_ack_outbox(),'outbox':storage.list_reply_send_ack_outbox(),
      'physical':physical,'command_count':len(commands),'locked':lock_summary().get('locked'),'errors':errors,'wire':wire}
 (folder/(phase+'-result.json')).write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str))
 print(json.dumps({k:v for k,v in out.items() if k not in ('outbox','wire')},ensure_ascii=False,default=str))

try:
 if phase=='initial':
  # Device capability setup only. Reply generation starts at normal ingest.
  h=api.session.post(api.base_url+f"/workers/{w['id']}/heartbeat",headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':'followup-test'},json={
     'client_instance_id':'followup-test','run_status':'running','running_status':'idle','rpa_component_status':'ready','wechat_status':'logged_in',
     'local_lock_summary':{'capabilities':{'reply_sequence_version':1,'pre_send_read_recovery_version':1,'pre_send_setup_recovery_version':1}}})
  assert h.status_code==200,h.text
  target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==rows[0]['conversation_id'])
  result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
  dump(result)
 else:
  # This harness drives each fresh C2 read below. Do not simultaneously fire
  # the unrelated periodic scan timer at the same controlled desktop. The
  # production recovery loops, UI locks and task claiming remain untouched.
  runner.last_c2_scan_at=time.monotonic()
  runner.last_c2_read_at=time.monotonic()
  runner.start(binding)
  end=time.monotonic()+35
  while time.monotonic()<end:
   if not storage.load_runtime_control().get('inflight_flow_id') and not storage.has_pending_reply_send_ack_outbox():break
   time.sleep(.1)
  assert not physical,physical
  assert binding.run_status=='faulted',{'run_status':binding.run_status,'locked':lock_summary()}
  assert not storage.has_pending_reply_send_ack_outbox(),storage.list_reply_send_ack_outbox()
  assert not storage.load_runtime_control().get('inflight_flow_id'),storage.load_runtime_control()
  if phase=='resume':
   end=time.monotonic()+15
   while time.monotonic()<end:
    if runner.fault_recovery_state().get('ready'):break
    time.sleep(.1)
   assert runner.fault_recovery_state().get('ready'),runner.fault_recovery_state()
   assert runner.set_run_status('running'),errors
   end=time.monotonic()+15
   while binding.run_status!='running' and time.monotonic()<end:time.sleep(.1)
   assert binding.run_status=='running',errors
   result=[]
   for row in rows:
    end=time.monotonic()+12
    while time.monotonic()<end:
     if not storage.load_runtime_control().get('inflight_flow_id') and not runner.task_lock.locked():break
     time.sleep(.05)
    target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==row['conversation_id'])
    read=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
    assert read.get('ok'),{'fresh_C2_read_not_admitted':read}
    result.append(read)
  else:result={}
  dump(result)
finally:
 runner._stop_task_lease_guard()
 runner.stop_for_update(timeout_seconds=5)
