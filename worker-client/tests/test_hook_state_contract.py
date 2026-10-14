"""Scenario contracts: real projection/dispatcher/UI; no customer/model replay."""
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
from chejin_worker_client.hook_progress import HookProgressTimeline,emit_progress,register_progress
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client import startup_check_state as checks,storage
from chejin_worker_client.models import Binding,WorkerProfile
from test_web_ui_binding_behavior import _headless_web_ui_module
import test_web_ui_binding_behavior as web_fixture
from test_hook_gateway import lab
from test_hook_progress_consistency import rt
from test_hook_voice import persist,MID,PEER

def harness(tmp_path,status='running'):
 t=HookProgressTimeline();events=[]
 def receive(e):events.append(e);t.apply(e)
 r=SimpleNamespace(on_runtime_process=receive,binding=SimpleNamespace(run_status=status))
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 return r,t,d,events

def register(r,context,**kw):register_progress(r,context,label='CJTEST1',**kw)

def test_real_round_aliases_keep_voice_ai_and_task_in_one_order(tmp_path):
 r,t,d,events=harness(tmp_path)
 binding={'binding_id':'b','remark_code':'CJTEST1'}
 runtime=HookFormalRuntime.__new__(HookFormalRuntime);runtime.lifecycle_runner=r
 runtime.register_binding_progress(binding,'b:123',aliases=['123'])
 for phase in ('messages_saved','history'):emit_progress(r,'b:123',phase,'done')
 emit_progress(r,'123','voice','current');emit_progress(r,'123','voice','done')
 data={'progress_native_ids':['123'],'task':{'id':'task-1'},'reply_action':{},'batch_status':'generating','processing':True}
 runtime.register_batch_progress(binding,'batch-1',data);d._publish_batch_progress(r,'batch-1',data)
 d._publish_batch_progress(r,'batch-1',{'processing':False,'batch_status':'reply_action_created'})
 for phase in ('send_conditions','send_delay','pre_send','send_submit','send_confirm'):
  emit_progress(r,'task-1',phase,'current');emit_progress(r,'task-1',phase,'done')
 emit_progress(r,'task-1','finished','done','sent')
 for _ in range(3):
  runtime.register_batch_progress(binding,'batch-1',data)
  emit_progress(r,'b:123','history');emit_progress(r,'task-1','send_confirm')
 rows=t.model()
 assert len({x['groupId'] for x in rows})==1
 assert [x['title'] for x in rows]==['接收并保存客户消息','读取并补齐客户消息','语音转文字','AI 处理客户消息','核验发送条件','等待发送间隔','发送前复核最新消息','向微信提交回复','确认发送结果','本轮回复已完成']
 assert all(x['state']=='done' for x in rows)
 assert sum(bool(x['groupTitle']) for x in rows)==1

@pytest.mark.parametrize('stage,expected',[('collecting','等待 AI 处理'),('retry_wait','等待 AI 重试'),('generating','AI 处理客户消息')])
@pytest.mark.parametrize('status',['running','paused'])
def test_queue_is_never_a_model_call(stage,expected,status,tmp_path):
 r,t,d,_=harness(tmp_path,status);register(r,'batch',kind='batch')
 d._publish_batch_progress(r,'batch',{'processing':True,'batch_status':stage})
 assert len(t.model())==1 and t.model()[0]['title']==expected
 assert t.model()[0]['state']==('current' if stage=='generating' else 'waiting')
 if status=='paused' and stage!='generating':assert '暂停' in t.model()[0]['description']

def test_real_history_exception_is_waiting_not_stuck_current(lab,tmp_path,monkeypatch):
 runtime=rt(lab);r,t,d,events=harness(tmp_path)
 runtime.lifecycle_runner=r;runtime.private_root=tmp_path;runtime.process=lab[2];runtime.gateway=lab[1]
 from chejin_worker_client.hook_retention import HistoryArtifactBudget
 runtime.active_flow_id=None
 runtime.history_budget=HistoryArtifactBudget(runtime.private_root,maximum_bytes=1024*1024,retention_days=30)
 runtime.heartbeat=Mock();runtime.projection=SimpleNamespace(project_next=lambda _:None)
 runtime.barrier=Mock();runtime.clock=SimpleNamespace(now=Mock());runtime.delivery=lambda _:None
 persist(lab,metadata=True)
 class Missing:
  def __init__(self,*a,**kw):pass
  def capture(self,*a,**kw):raise ValueError('HOOK_HISTORY_CALLBACK_NOT_LANDED')
 monkeypatch.setattr('chejin_worker_client.hook_formal_runtime.HookHistorySynchronizer',Missing)
 binding={'binding_id':'b','peer_wxid':PEER,'remark_code':'CJTEST1','history_since_create_time':'0'}
 with pytest.raises(ValueError,match='CALLBACK_NOT_LANDED'):runtime.sync(binding)
 # The same dispatcher transient handler used in the actual tick.
 context=runtime._history_context
 emit_progress(r,context,'history','waiting','callback_wait')
 assert t.model()[-1]['state']=='waiting' and '对应消息' in t.model()[-1]['description']
 assert t.model()[0]['time']!=''

def test_old_handoff_is_separate_and_read_round_does_not_downgrade_live(tmp_path):
 r,t,d,_=harness(tmp_path)
 register_progress(r,'startup',group='startup',kind='startup')
 emit_progress(r,'startup','ready','done')
 record={'id':'h','batch_id':'old','handoff_reason_code':'AI_ENGINE_RETRY_EXHAUSTED','created_at':'2026-10-12T13:23:29Z'}
 d._publish_open_handoffs(r,{'binding_id':'b','remark_code':'CJTEST1','open_handoffs':[record]})
 assert [x['title'] for x in t.model(historical=False)]==['运行环境检查通过']
 assert [x['title'] for x in t.model(historical=True)]==['AI 处理客户消息','已转人工，等待销售处理']
 register(r,'b:123');emit_progress(r,'b:123','history','done')
 record.update(batch_id=None,progress_native_ids=['123'],created_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
 d._publish_open_handoffs(r,{'binding_id':'b','remark_code':'CJTEST1','open_handoffs':[record]})
 assert any(x['title']=='已转人工，等待销售处理' for x in t.model(historical=False))

def test_fault_suspends_only_affected_round_and_keeps_finished_facts(tmp_path):
 r,t,_,_=harness(tmp_path)
 register(r,'task-A',group='A',kind='batch');register(r,'task-B',group='B',kind='batch')
 emit_progress(r,'task-A','history','done');emit_progress(r,'task-A','send_confirm')
 emit_progress(r,'task-B','decision')
 register(r,'fault',group='A',kind='batch');emit_progress(r,'fault','stopped','error','interrupted')
 rows=t.model()
 assert next(x for x in rows if x['title']=='读取并补齐客户消息')['state']=='done'
 assert next(x for x in rows if x['title']=='确认发送结果')['state']=='waiting'
 assert next(x for x in rows if x['title']=='AI 处理客户消息')['state']=='current'

def test_unconfirmed_send_cannot_claim_completed_round(tmp_path):
 r,t,_,_=harness(tmp_path);register(r,'task',kind='batch')
 emit_progress(r,'task','send_confirm','waiting','send_wait')
 emit_progress(r,'task','finished','waiting','send_wait')
 assert not any(x['title']=='本轮回复已完成' for x in t.model())
 emit_progress(r,'task','send_confirm','done');emit_progress(r,'task','finished','done','sent')
 assert len(t.model())==2 and t.model()[-1]['title']=='本轮回复已完成'

def test_multiple_customer_rounds_are_contiguous_and_same_phase_retry_updates(tmp_path):
 r,t,_,_=harness(tmp_path)
 for context in ('a','b'):register(r,context,kind='batch')
 for context,phase,state in [('a','history','done'),('b','history','done'),('a','decision','error'),('b','decision','current'),('a','decision','done')]:emit_progress(r,context,phase,state)
 assert [x['groupId'] for x in t.model()]==['a','a','b','b']
 assert len([x for x in t.model() if x['groupId']=='a' and x['title']=='AI 处理客户消息'])==1

def test_multiple_physical_segments_are_not_hidden_as_duplicate_send(tmp_path):
 r,t,_,_=harness(tmp_path)
 for segment in (1,2):
  register(r,f'task-{segment}',group='batch',kind='batch',segment=segment)
  emit_progress(r,f'task-{segment}','send_confirm','done')
 assert [x['title'] for x in t.model()]==['确认发送结果（第 1 段）','确认发送结果（第 2 段）']

@pytest.mark.parametrize('status,waiting,offline,expected',[
 ('running',True,False,'等待后台恢复'),('running',False,False,'接单中'),
 ('paused',True,False,'暂停接单'),('faulted',True,False,'已停止接单'),
 ('faulted',False,False,'已停止接单'),('running',False,True,'等待后台恢复'),('paused',True,True,'暂停接单')])
def test_actual_worker_header_preserves_operator_intent(status,waiting,offline,expected,tmp_path):
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=web_fixture.WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client',status));w._publish=Mock()
   w.connection_status='offline' if offline else 'bound'
   w.profile=WorkerProfile('worker','测试销售',status,rpa_component_status='ready',wechat_status='logged_in')
   w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed,dispatcher=SimpleNamespace(_pending_wait_phases={'business':{'connection_wait'}} if waiting else {}))
   # The OCR-era web fixture has a hardcoded obsolete label. Exercise the
   # actual runner projection while retaining all original UI assertions.
   from chejin_worker_client.task_runner import TaskRunner
   from types import MethodType
   from threading import RLock
   w.runner._run_status_intent_lock=RLock()
   w.runner._fault_recovery_state={'ready':False,'checking':False,'reason':'等待检查'}
   w.runner.fault_recovery_state=MethodType(TaskRunner.fault_recovery_state,w.runner)
   model=w._model()
   assert model['status']['receiveState']==expected
   assert model['status']['acceptingIntent']==(status=='running')
   assert w.binding.run_status==status

def test_connection_pass_cannot_hide_business_wait_in_start_gate():
 services=HookWorkerServices.__new__(HookWorkerServices)
 services._startup_check=checks.passed();services._display_fault=None
 services.dispatcher=SimpleNamespace(_pending_wait_phases={'business':{'clock_wait'}})
 assert not services.startup_check_state()['canStart']
 services.dispatcher._pending_wait_phases.clear()
 assert services.startup_check_state()['canStart']

def test_clock_observation_and_start_gate_have_one_authoritative_result(tmp_path):
 r,t,d,_=harness(tmp_path)
 service=HookWorkerServices.__new__(HookWorkerServices)
 service._startup_check=checks.passed();service._display_fault=None;service.dispatcher=d
 assert not service.startup_check_state()['canStart']
 runtime=SimpleNamespace(clock=SimpleNamespace(observe=Mock()))
 d._observe_clock(runtime,object());assert service.startup_check_state()['canStart']
 runtime.clock.observe.side_effect=ValueError('HOOK_BACKEND_CLOCK_CHANGED')
 with pytest.raises(ValueError):d._observe_clock(runtime,object())
 assert not service.startup_check_state()['canStart']
 assert '异常变化' in service.startup_check_state()['title']

def test_retry_and_terminal_handoff_leave_no_fake_current_model(tmp_path):
 r,t,d,_=harness(tmp_path);register(r,'batch',kind='batch')
 d._publish_batch_progress(r,'batch',{'processing':True,'batch_status':'generating'})
 d._publish_batch_progress(r,'batch',{'processing':True,'batch_status':'retry_wait'})
 assert not any(s['state']=='current' for s in t.model())
 d._publish_batch_progress(r,'batch',{'terminal':True,'processing':False,'decision':'handoff',
  'batch_status':'handoff_created','handoff_event':{'id':'h','batch_id':'batch','closed_at':None,
  'created_at':'2026-10-13T01:00:00Z','handoff_reason_code':'AI_ENGINE_RETRY_EXHAUSTED'}})
 assert not any(s['state'] in {'waiting','current'} for s in t.model())
 assert t.model()[-1]['title']=='已转人工，等待销售处理'

def test_trim_keeps_whole_rounds_and_open_human_hold(tmp_path):
 r,t,_,_=harness(tmp_path);t.max_steps=6
 register(r,'hold',kind='batch',historical=True);emit_progress(r,'hold','handoff','done','handoff')
 for context in ('round1','round2','round3'):
  register(r,context,kind='batch')
  for phase in ('history','decision','finished'):emit_progress(r,context,phase,'done')
 assert any(s['title']=='已转人工，等待销售处理' for s in t.model())
 for group in {s['groupId'] for s in t.model() if s['groupId']!='hold'}:
  assert len([s for s in t.model() if s['groupId']==group])==3

def test_clear_global_fault_keeps_customer_handoff_and_unknown_send(tmp_path):
 r,t,_,_=harness(tmp_path)
 register_progress(r,'fault',group='system',kind='system');emit_progress(r,'fault','stopped','error','interrupted')
 register(r,'customer',kind='batch');emit_progress(r,'customer','handoff','done','handoff')
 register(r,'unknown',kind='batch');emit_progress(r,'unknown','send_confirm','waiting','send_wait')
 t.apply({'event':'hook_progress_control','action':'fault_clear'})
 assert [x['title'] for x in t.model()]==['已转人工，等待销售处理','确认发送结果']
 assert t.model()[-1]['state']=='waiting'

def test_segmented_waterfall_keeps_each_actual_send_sequence_together(tmp_path):
 r,t,_,_=harness(tmp_path)
 for segment in (1,2):
  register(r,f'task-{segment}',group='batch',kind='batch',segment=segment)
  for phase in ('send_conditions','send_delay','pre_send','send_submit','send_confirm'):emit_progress(r,f'task-{segment}',phase,'done')
 rows=t.model()
 assert all('第 1 段' in x['title'] for x in rows[:5])
 assert all('第 2 段' in x['title'] for x in rows[5:])

@pytest.mark.parametrize('status,schedule,observe',[('paused',True,True),('faulted',True,True),('running',False,True),('running',True,False)])
def test_batch_observation_never_starts_new_model_when_locally_stopped(status,schedule,observe):
 from chejin_worker_client.hook_formal_runtime import batch_view_path
 r=SimpleNamespace(binding=SimpleNamespace(run_status=status),can_pull_tasks=lambda:schedule)
 assert batch_view_path(r,'worker','batch').endswith('?observe_only=true')==observe

def test_finished_handoff_has_no_running_focus(tmp_path):
 r,t,d,_=harness(tmp_path);register(r,'batch',kind='batch')
 d._publish_batch_progress(r,'batch',{'processing':True,'batch_status':'generating'})
 d._publish_batch_progress(r,'batch',{'processing':False,'terminal':True,'decision':'handoff','batch_status':'handoff_created',
  'handoff_event':{'id':'h','batch_id':'batch','closed_at':None,'created_at':'2026-10-13T01:00:00Z','handoff_reason_code':'AI_ENGINE_RETRY_EXHAUSTED'}})
 assert not any(s['focus'] for s in t.model())


def test_thousands_of_completed_rounds_release_display_metadata_only(tmp_path):
 from chejin_worker_client.hook_progress import forget_completed_progress_group
 r,t,_,_=harness(tmp_path);t.max_steps=11
 register_progress(r,'hold',group='hold',kind='batch',label='CJHOLD')
 emit_progress(r,'hold','handoff','done','handoff')
 for i in range(2000):
  context=f'round-{i}'
  register_progress(r,context,group=context,kind='batch',label='CJTEST')
  emit_progress(r,context,'history','done')
  emit_progress(r,context,'finished','done','no_reply')
  for group in t.take_retired_groups():forget_completed_progress_group(r,group)
 assert len(t.steps)<=11 and len(t.groups)<=6 and len(r._hook_progress_contexts)<=6
 assert 'hold' in r._hook_progress_contexts
 assert any(x['title']=='已转人工，等待销售处理' for x in t.model())


def test_closed_human_hold_can_retire_but_open_hold_survives(tmp_path):
 r,t,_,_=harness(tmp_path);t.max_steps=3
 for group in ('closed', 'open'):
  register_progress(r,group,group=group,kind='batch',label='CJTEST')
  emit_progress(r,group,'decision','error','handoff_ai_failure')
  emit_progress(r,group,'handoff','done','handoff')
 emit_progress(r,'closed','handoff_closed','done','handoff_closed')
 assert {row['groupId'] for row in t.model()}=={'open'}
 assert t.take_retired_groups()==('closed',)
