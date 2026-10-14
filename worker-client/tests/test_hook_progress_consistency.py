"""Actual inbox identity and runtime sync; no real Windows or AI operations."""
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib,json
import pytest
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_voice import persist,MID,PEER,AUDIO
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_progress import HookProgressTimeline
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher


def evt(phase,state='current',context='customer',note=None):
 return {'event':'hook_progress','context':context,'phase':phase,'state':state,'time':'20:26:00','note':note}


def rt(lab):
 result=HookFormalRuntime.__new__(HookFormalRuntime);result.scope=lab[3]
 return result


def test_audio_supplement_duplicates_and_our_echo_keep_one_customer_context(lab):
 runtime=rt(lab);binding={'binding_id':'binding','peer_wxid':PEER}
 persist(lab,metadata=True);first=runtime._history_progress_context(binding)
 persist(lab,audio=True,fields={'fromid':'','toid':'','fromtype':''})
 persist(lab,metadata=True)
 persist(lab,metadata=True,mid='999',fields={'fromid':lab[3].self_wxid,'toid':PEER})
 assert runtime._history_progress_context(binding)==first==(f'binding:{MID}',True)
 persist(lab,metadata=True,mid='888')
 assert runtime._history_progress_context(binding)==('binding:888',True)
 # A later duplicate of the older voice does not move progress backwards.
 persist(lab,metadata=True)
 assert runtime._history_progress_context(binding)==('binding:888',True)


def test_system_acceptance_outgoing_and_groups_do_not_become_customer_receive_steps(lab):
 runtime=rt(lab);scope=lab[3];binding={'binding_id':'binding','peer_wxid':PEER}
 from chejin_worker_client.shared_rules import hook_history_rules
 for message in [
  {'msgtype':'1','fromtype':'1','fromid':PEER,'toid':scope.self_wxid,'msg':hook_history_rules.FRIEND_ACCEPTANCE_NOTICE},
  {'msgtype':'1','fromtype':'1','fromid':scope.self_wxid,'toid':PEER,'msg':'our reply'},
  {'msgtype':'1','fromtype':'2','fromid':PEER,'toid':scope.self_wxid,'msg':'group'},
 ]:
  message['msgsvrid']='123'
  event={'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msglist':[message]}
  NativeInbox(max_body_bytes=4096,max_inbox_bytes=65536).persist(json.dumps(event).encode(),scope)
 assert runtime._history_progress_context(binding)==('binding:initial',False)


def test_done_send_confirm_and_history_never_reopen_from_polls():
 timeline=HookProgressTimeline()
 for phase in ['history','send_confirm']:
  timeline.apply(evt(phase,'done'))
  for state in ['waiting','current','done']:
   timeline.apply(evt(phase,state,note='send_wait'))
 assert [(x['title'],x['state']) for x in timeline.model()]==[('读取并补齐客户消息','done'),('确认发送结果','done')]


def test_real_recovery_removes_only_the_resolved_transient_condition():
 t=HookProgressTimeline();t.apply(evt('voice'))
 t.apply(evt('clock_wait','waiting','connection','clock_wait'))
 t.apply(evt('connection_wait','waiting','connection','connection_wait'))
 t.apply(evt('connection_wait','done','connection'))
 assert [x['title'] for x in t.model()]==['语音转文字','等待后台响应稳定']
 t.apply(evt('clock_wait','done','connection'))
 assert t.model()==[{'title':'语音转文字','state':'current','time':'20:26:00','description':''}]


def test_clock_delay_is_distinct_from_backend_unavailable(tmp_path):
 events=[];runner=SimpleNamespace(on_runtime_process=events.append)
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 d._progress_runner=runner
 # Logs are unrelated to presentation and must still be retained.
 from unittest.mock import patch
 with patch('chejin_worker_client.hook_dispatcher.storage.append_log'):
  d._report_wait('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED')
  d._report_wait('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE')
 assert [x['phase'] for x in events]==['clock_wait','connection_wait']


def test_runtime_read_completes_before_transcription_and_internal_rescans_are_silent(lab,tmp_path,monkeypatch):
 runtime=rt(lab);scope=runtime.scope;events=[]
 runtime.lifecycle_runner=SimpleNamespace(on_runtime_process=events.append)
 runtime.private_root=tmp_path/'private';runtime.process=lab[2];runtime.gateway=lab[1]
 from chejin_worker_client.hook_retention import HistoryArtifactBudget
 runtime.active_flow_id=None
 runtime.history_budget=HistoryArtifactBudget(runtime.private_root,maximum_bytes=1024*1024,retention_days=30)
 runtime.credentials=SimpleNamespace(worker_id='worker');runtime.account={}
 runtime.heartbeat=Mock();runtime.barrier=Mock(return_value=(2,0))
 runtime.projection=SimpleNamespace(project_next=lambda _:None)
 runtime.clock=SimpleNamespace(now=lambda:datetime.now(timezone.utc));runtime.history_retry_events=[]
 runtime.delivery=lambda _:SimpleNamespace();runtime.drain=Mock()
 runtime.client=SimpleNamespace(history_coverage=Mock(return_value={'history_ready':False,'reason_code':'HOOK_VOICE_PENDING'}))
 binding={'binding_id':'binding','peer_wxid':PEER,'history_since_create_time':'0'}
 persist(lab,metadata=True);persist(lab,audio=True,fields={'fromid':'','toid':'','fromtype':''})
 scans=[{'databases':[]},{'databases':[{'pages':[[{'MsgSvrID':MID}]]}]}]
 class Scan:
  count=0
  profile={}
  completed_barrier=(2,0)
  def __init__(self,*args,**kwargs):pass
  def capture(self,*args,**kwargs):
   Scan.count+=1
   return f'capture-{Scan.count}',{'scans':scans}
 monkeypatch.setattr('chejin_worker_client.hook_formal_runtime.HookHistorySynchronizer',Scan)
 assert not runtime.sync(binding)['history_ready']
 assert [(e['phase'],e['state']) for e in events if e['event']=='hook_progress']==[('messages_saved','done'),('history','current'),('history','done')]
 runtime.progress(MID,'voice');runtime.progress(MID,'voice','done')
 before=len(events)
 runtime.client.history_coverage.return_value={'history_ready':True}
 runtime.sync(binding,show_progress=False)
 assert len(events)==before and runtime.client.history_coverage.call_count==2
 runtime.sync(binding)
 t=HookProgressTimeline()
 for e in events:t.apply(e)
 assert [(x['title'],x['state']) for x in t.model()]==[('接收并保存客户消息','done'),('读取并补齐客户消息','done'),('语音转文字','done')]
 assert runtime.client.history_coverage.call_count==3


def test_progress_identity_tampering_still_stops(lab):
 runtime=rt(lab);binding={'binding_id':'binding','peer_wxid':PEER};persist(lab,metadata=True)
 with storage.db_connection() as db:db.execute("UPDATE hook_raw_inbox SET raw_digest='changed'");db.commit()
 with pytest.raises(ValueError,match='RAW_EVIDENCE_CHANGED'):runtime._history_progress_context(binding)



def test_heartbeat_cannot_clear_a_business_wait_and_shared_wait_has_one_row():
 t=HookProgressTimeline()
 t.apply(evt('connection_wait','waiting','business','connection_wait'))
 t.apply(evt('connection_wait','waiting','connection','connection_wait'))
 assert len(t.model())==1
 t.apply(evt('connection_wait','done','connection'))
 assert len(t.model())==1 and t.model()[0]['state']=='waiting'
 t.apply(evt('connection_wait','done','business'))
 assert t.model()==[]


@pytest.mark.parametrize('cycle',['success','gate','new_wait','exception'])
def test_business_wait_is_resolved_only_by_completed_successful_cycle(tmp_path,cycle):
 from unittest.mock import patch
 events=[];runner=SimpleNamespace(on_runtime_process=events.append)
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 d._progress_runner=runner
 with patch('chejin_worker_client.hook_dispatcher.storage.append_log'):
  d._report_wait('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE')
  def run(*args):
   d._business_cycle_verified=cycle!='gate'
   if cycle=='new_wait':d._report_wait('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE')
   if cycle=='exception':raise ValueError('synthetic')
  d._tick=run
  if cycle=='exception':
   with pytest.raises(ValueError):d.tick(runner,None)
  else:d.tick(runner,None)
 assert [e['state'] for e in events]==(['waiting','done'] if cycle=='success' else ['waiting','waiting'] if cycle=='new_wait' else ['waiting'])
 assert ('business' not in d._pending_wait_phases)==(cycle=='success')



def test_old_completed_ai_results_never_reappear_as_new_processing(tmp_path):
 events=[];runner=SimpleNamespace(on_runtime_process=events.append)
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 old={'processing':False,'terminal':True,'decision':'send_reply',
      'reply_action':{'status':'sent'},'task':{'status':'completed'}}
 for _ in range(3):
  for bid in ['old_welcome','old_voice']:
   assert not d._publish_batch_progress(runner,bid,old)
 assert events==[] and not d._presented_batches
 d._publish_initial_read_finished(runner,{},history_ready=True,pending=False)
 d._publish_initial_read_finished(runner,{},history_ready=True,pending=False)
 assert [e['phase'] for e in events]==['sync_finished'] and events[0]['state']=='done'


@pytest.mark.parametrize('condition',['history_incomplete','voice_pending','active_flow','active_task','new_ai'])
def test_no_initial_end_marker_while_real_work_or_missing_proof_remains(tmp_path,condition):
 events=[];runner=SimpleNamespace(on_runtime_process=events.append)
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 inventory={}
 if condition=='active_flow':inventory={'inflight_flow':{'flow_id':'flow'}}
 if condition=='active_task':inventory={'inflight_task':{'id':'task','status':'running'}}
 if condition=='new_ai':d._presented_batches.add('new')
 d._publish_initial_read_finished(runner,inventory,history_ready=condition!='history_incomplete',pending=condition=='voice_pending')
 assert not events


def test_new_ai_and_registered_queued_work_still_show_their_real_progress(tmp_path):
 events=[];runner=SimpleNamespace(on_runtime_process=events.append)
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 assert d._publish_batch_progress(runner,'new',{'processing':True})
 assert not d._publish_batch_progress(runner,'new',{'processing':False,'terminal':True,'decision':'no_reply'})
 assert [(e['phase'],e['state']) for e in events]==[('decision','current'),('decision','done'),('finished','done')]
 assert d._publish_batch_progress(runner,'queued',{'reply_action':{'status':'queued'},'task':{'status':'pending'}})
 assert events[-1]['phase']=='decision' and events[-1]['state']=='done'


def test_real_dispatcher_paused_reconciliation_polls_old_results_without_fake_ai(lab,backend_peer,tmp_path):
 import uuid,threading,os
 from test_hook_connection import setup
 manager,credentials,state,response=setup(lab,backend_peer);prepared=manager.prepare(credentials,state)
 scope=prepared.registration.scope
 account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
 bids=[str(uuid.uuid4()),str(uuid.uuid4())];calls=[];events=[]
 binding={'binding_id':str(uuid.uuid4()),'binding_revision':1,'peer_wxid':PEER,'conversation_id':str(uuid.uuid4()),'history_since_create_time':'1','voice_jobs':[],'batch_ids':bids}
 class Client:
  def call(self,credentials,method,path,body=None,**kwargs):
   calls.append(path)
   if path.endswith('native-work'):
    return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'paused','inflight_flow':{},'bindings':[binding],'invitations':[],'retained_sends':[],'inflight_task':None}
   assert any(path.split('?')[0].endswith('/'+bid) for bid in bids)
   assert path.endswith('?observe_only=true')
   return {'observation_only':True,'processing':False,'terminal':True,'decision':'send_reply','reply_action':{'status':'sent'},'task':{'status':'completed'}}
 class Runtime:
  def __init__(self,*args,**kwargs):
   self.credentials=credentials;self.account=account;self.scope=scope;self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda _:None)
  def barrier(self,b):return (0,0)
  def sync(self,b):return {'history_ready':True}
  def reply_once(self,*args,**kwargs):raise AssertionError('Old result triggered sending')
 runner=SimpleNamespace(binding=SimpleNamespace(run_status='paused'),stop_event=threading.Event(),api=SimpleNamespace(),on_runtime_process=events.append,_can_start_new_flow=lambda:False)
 services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
 d=HookBusinessDispatcher(Client(),tmp_path/'continuous',None,None,automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
 try:
  d.tick(runner,services);d.tick(runner,services)
  assert len(calls)==6 and [e['phase'] for e in events]==['sync_finished']
  assert runner.binding.run_status=='paused'
 finally:manager.close()
