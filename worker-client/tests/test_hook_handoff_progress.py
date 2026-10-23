from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import copy,pytest
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_progress import HookProgressTimeline,emit_progress
from chejin_worker_client.models import Binding,WorkerProfile
from chejin_worker_client import startup_check_state as checks
from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest

@pytest.fixture
def runtime(tmp_path):
 timeline=HookProgressTimeline();events=[]
 def receive(e):events.append(e);timeline.apply(e)
 runner=SimpleNamespace(on_runtime_process=receive,binding=SimpleNamespace(run_status='running'))
 d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 return d,runner,timeline,events

def fact(reason='AI_ENGINE_RETRY_EXHAUSTED'):
 return {'id':'event-A','batch_id':'batch-A','handoff_reason_code':reason,'created_at':'2026-10-12T13:23:29+00:00'}

def terminal():
 return {'processing':False,'terminal':True,'batch_status':'handoff_created','decision':'handoff','error_code':'AI_ENGINE_RETRY_EXHAUSTED','handoff_event':{**fact(),'closed_at':None}}

def test_completed_ai_failure_has_clear_handoff_terminal_without_worker_stop(runtime):
 d,r,t,events=runtime
 d._publish_batch_progress(r,'batch-A',{'processing':True})
 for _ in range(3):assert not d._publish_batch_progress(r,'batch-A',terminal())
 rows=t.model()
 assert [x['title'] for x in rows]==['AI 处理客户消息','已转人工，等待销售处理']
 assert rows[0]['state']=='error' and rows[0]['description']=='AI 回复处理失败，已按规则转人工。'
 assert rows[-1]['state']=='done' and '此客户' in rows[-1]['description']
 assert not any(x['state']=='current' for x in rows)
 assert r.binding.run_status=='running'
 assert all(e['phase']!='stopped' for e in events)

@pytest.mark.parametrize('mutate',[lambda d:d.update(terminal=False),lambda d:d.update(processing=True),lambda d:d.update(handoff_event=None),lambda d:d['handoff_event'].update(batch_id='other-batch'),lambda d:d['handoff_event'].update(closed_at='2026-10-13T00:00:00Z')])
def test_error_or_unproved_handoff_does_not_claim_customer_transferred(runtime,mutate):
 d,r,t,_=runtime;d._presented_batches.add('batch-A');data=terminal();mutate(data)
 d._publish_batch_progress(r,'batch-A',data)
 assert not any(x['title']=='已转人工，等待销售处理' for x in t.model())

@pytest.mark.parametrize('reason',['CUSTOMER_HIGH_INTENT','HOOK_IMAGE_REQUIRES_HUMAN','CUSTOMER_REQUEST_HUMAN'])
def test_other_authoritative_handoff_reasons_do_not_invent_ai_error(runtime,reason):
 d,r,t,_=runtime;record=fact(reason)
 d._publish_open_handoffs(r,{'binding_id':'binding','open_handoffs':[record]})
 assert [x['title'] for x in t.model()]==['已转人工，等待销售处理']
 assert t.model()[0]['state']=='done'


def test_restart_restores_saved_handoff_without_model_or_send_and_no_fake_new_time(runtime):
 d,r,t,_=runtime
 for _ in range(3):d._publish_open_handoffs(r,{'binding_id':'binding','open_handoffs':[fact()]})
 assert len(t.model())==2 and not d._presented_batches
 expected=datetime.fromisoformat(fact()['created_at']).astimezone()
 assert t.model()[-1]['time']==expected.strftime('%H:%M:%S' if expected.date()==datetime.now().date() else '%m-%d %H:%M')
 d._publish_initial_read_finished(r,{},history_ready=True,pending=False)
 assert len(t.model())==3
 assert t.model(historical=False)[-1]['title']=='本次消息核对已结束'


def test_unsupported_old_server_cannot_close_saved_handoff(runtime):
 d,r,t,_=runtime;d._publish_open_handoffs(r,{'binding_id':'binding','open_handoffs':[fact()]})
 d._publish_open_handoffs(r,{'binding_id':'binding'})
 assert len(t.model())==2
 d._publish_open_handoffs(r,{'binding_id':'binding','open_handoffs':[]})
 assert t.model()[-1]['title']=='本次交接事项已处理'
 assert r.binding.run_status=='running'
 assert 'AI' not in t.model()[-1]['description']


def test_one_customer_handoff_does_not_suppress_other_customer_model(runtime):
 d,r,t,_=runtime;d._publish_open_handoffs(r,{'binding_id':'binding-A','open_handoffs':[fact()]})
 assert d._publish_batch_progress(r,'batch-B',{'processing':True})
 assert t.model(historical=False)[-1]['state']=='current' and t.model(historical=False)[-1]['title']=='AI 处理客户消息'
 assert r.binding.run_status=='running'


def test_real_worker_model_keeps_accepting_and_clear_handoff(runtime):
 d,_,_,_=runtime
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client','running'))
   w.connection_status='bound';w._publish=Mock();w.profile=WorkerProfile('worker','测试销售','running',rpa_component_status='ready',wechat_status='logged_in')
   w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
   d._publish_open_handoffs(SimpleNamespace(on_runtime_process=w.on_runtime_process),{'binding_id':'binding','open_handoffs':[fact()]})
   state=w._state();assert state['model']['status']['receiveState']=='接单中'
   assert 'hookHistorySteps' not in state['model']
   assert w.hook_progress_timeline.model(historical=True)[-1]['title']=='已转人工，等待销售处理'
   assert not any(x['state']=='current' for x in state['model']['hookProcessSteps'])


def test_invalid_saved_time_never_looks_like_a_new_failure():
 events=[];emit_progress(SimpleNamespace(on_runtime_process=events.append),'batch','handoff','done','handoff',occurred_at='bad-private-value')
 assert events[0]['time']=='' and 'private' not in str(events[0])


def test_normal_business_handoff_finishes_its_own_ai_marker(runtime):
 d,r,t,_=runtime
 d._publish_batch_progress(r,'batch-A',{'processing':True})
 d._publish_batch_progress(r,'batch-B',{'processing':True})
 value=terminal();value['error_code']='CUSTOMER_HIGH_INTENT';value['handoff_event']['handoff_reason_code']='CUSTOMER_HIGH_INTENT'
 assert not d._publish_batch_progress(r,'batch-A',value)
 assert t.model()[0]['state']=='done' and t.model()[1]['state']=='current'
 assert t.model()[-1]['title']=='已转人工，等待销售处理'
 assert not any(x['state']=='error' for x in t.model())


@pytest.mark.parametrize('clock_error',[None,'HOOK_BACKEND_CLOCK_CHANGED'])
def test_actual_faulted_dispatcher_tick_restores_handoff_before_clock_gate(tmp_path,monkeypatch,clock_error):
 import threading
 from chejin_worker_client import storage
 scope=SimpleNamespace(self_wxid='synthetic-self',account_epoch='epoch',process_generation='generation')
 account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
 prepared=SimpleNamespace(registration=SimpleNamespace(scope=scope),account=object())
 timeline=HookProgressTimeline();calls=[];effects=[]
 def receive(e):timeline.apply(e)
 class Client:
  def call(self,credentials,method,path,body=None,**kwargs):
   calls.append((method,path))
   assert method=='POST' and path.endswith('/native-work')
   return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'faulted','inflight_flow':{},'bindings':[{'binding_id':'binding','open_handoffs':[fact()]}],'invitations':[],'retained_sends':[],'inflight_task':None}
 class Clock:
  def observe(self,_):
   if clock_error:raise ValueError(clock_error)
 class Runtime:
  def __init__(self,*args,**kwargs):
   self.account=account;self.scope=scope;self.credentials=SimpleNamespace(worker_id='worker');self.active_flow_id=self.active_task_id=None;self.clock=Clock()
  def sync(self,*args):pytest.fail('Faulted worker ran customer history')
  def reply_once(self,*args,**kwargs):pytest.fail('Faulted worker sent a reply')
 monkeypatch.setattr(storage,'load_runtime_control',lambda:{})
 runner=SimpleNamespace(binding=SimpleNamespace(run_status='faulted'),stop_event=threading.Event(),api=SimpleNamespace(),on_runtime_process=receive,on_task=lambda t:effects.append(t))
 services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=None),_credentials=None,startup_window_ready_for=lambda _:pytest.fail('Faulted worker ran business'))
 d=HookBusinessDispatcher(Client(),tmp_path,None,None,automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
 for _ in range(3):
  if clock_error:
   with pytest.raises(ValueError,match=clock_error):d.tick(runner,services)
  else:d.tick(runner,services)
 rows=timeline.model()
 assert [r['title'] for r in rows]==['AI 处理客户消息','已转人工，等待销售处理']
 assert len(calls)==3 and not any(r['state']=='current' for r in rows)
 assert runner.binding.run_status=='faulted' and not d._business_cycle_verified
 assert all(t is None for t in effects)


def test_real_faulted_worker_model_has_saved_handoff_and_original_recovery_button(runtime):
 d,_,_,_=runtime
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client','faulted'))
   w.connection_status='bound';w._publish=Mock();w.profile=WorkerProfile('worker','测试销售','faulted',rpa_component_status='ready',wechat_status='logged_in')
   w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
   d._publish_open_handoffs(SimpleNamespace(on_runtime_process=w.on_runtime_process),{'binding_id':'binding','open_handoffs':[fact()]})
   state=w._state()
   assert 'hookHistorySteps' not in state['model']
   assert w.hook_progress_timeline.model(historical=True)[-1]['title']=='已转人工，等待销售处理'
   assert not any(x['state']=='current' for x in state['model']['hookProcessSteps'])
   assert w.binding.run_status=='faulted'


@pytest.mark.parametrize('placement_order',['before_restore','interleaved','after_ready'])
def test_real_worker_model_keeps_startup_group_before_restored_handoff(runtime,placement_order):
 d,_,_,_=runtime
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client','faulted'))
   w._publish=Mock();w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
   def done(phase):emit_progress(SimpleNamespace(on_runtime_process=w.on_runtime_process),'startup',phase,'done')
   done('environment');done('wechat_window')
   if placement_order=='before_restore':done('worker_window')
   if placement_order=='after_ready':done('ready')
   d._publish_open_handoffs(SimpleNamespace(on_runtime_process=w.on_runtime_process),{'binding_id':'binding','open_handoffs':[fact()]})
   if placement_order!='before_restore':done('worker_window')
   done('ready')
   rows=w._state()['model']['hookProcessSteps']
   assert [r['title'] for r in rows]==['检查运行环境','调整微信窗口位置和大小','将 Worker 放到微信旁边','运行环境检查通过']
   assert 'hookHistorySteps' not in w._state()['model']
   assert [r['title'] for r in w.hook_progress_timeline.model(historical=True)]==['AI 处理客户消息','已转人工，等待销售处理']
   assert w.binding.run_status=='faulted' and not any(r['state']=='current' for r in rows)


def test_all_startup_delivery_orders_keep_real_customer_order_and_states():
 from itertools import permutations
 from chejin_worker_client.hook_progress import STARTUP_PHASES,PHASES
 for order in permutations(STARTUP_PHASES):
  for split in range(5):
   timeline=HookProgressTimeline()
   def event(context,phase,state='done'):
    return {'event':'hook_progress','context':context,'phase':phase,'state':state,'time':'original'}
   for phase in order[:split]:timeline.apply(event('startup',phase))
   timeline.apply(event('batch-A','decision','error'));timeline.apply(event('batch-A','handoff'))
   timeline.apply(event('batch-B','decision','current'))
   for phase in order[split:]:timeline.apply(event('startup',phase))
   rows=timeline.model()
   assert [r['title'] for r in rows[:4]]==[PHASES[p] for p in STARTUP_PHASES]
   assert [r['state'] for r in rows[4:]]==['error','done','current']
   assert all(r['time']=='original' for r in rows)


def test_unknown_voice_handoff_does_not_invent_an_ai_attempt(runtime):
 d,r,t,events=runtime
 record={**fact('HOOK_VOICE_RESULT_UNCONFIRMED'),'batch_id':None,
     'created_at':datetime.now(timezone.utc).isoformat(),'progress_native_ids':['voice-parent']}
 for _ in range(3):d._publish_open_handoffs(r,{'binding_id':'binding','open_handoffs':[record]})
 rows=t.model()
 assert [x['title'] for x in rows]==['语音转文字','已转人工，等待销售处理']
 assert rows[0]['state']=='error' and '结果未确认' in rows[0]['description']
 assert '不会自动重试' in rows[0]['description']
 assert all(e.get('phase')!='decision' for e in events)


@pytest.mark.parametrize('run_status',['running','paused'])
def test_handoff_inventory_published_before_retained_send_pending_return(tmp_path,monkeypatch,run_status):
 import threading
 from chejin_worker_client import storage
 scope=SimpleNamespace(self_wxid='synthetic-self',account_epoch='epoch',process_generation='generation')
 account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
 prepared=SimpleNamespace(registration=SimpleNamespace(scope=scope),account=None)
 timeline=HookProgressTimeline()
 inventory={'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
  'run_status':run_status,'bindings':[{'binding_id':'binding','open_handoffs':[fact()]}],
  'inflight_flow':{},'invitations':[],'retained_sends':[{'binding_id':'binding'}]}
 client=SimpleNamespace(call=lambda *a,**kw:inventory)
 d=HookBusinessDispatcher(client,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
 d.runtime=SimpleNamespace(account=account,scope=scope,credentials=SimpleNamespace(worker_id='worker'))
 runner=SimpleNamespace(binding=SimpleNamespace(run_status=run_status),stop_event=threading.Event(),
  on_runtime_process=timeline.apply)
 services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared))
 monkeypatch.setattr(storage,'load_runtime_control',lambda:{})
 monkeypatch.setattr(d,'_observe_clock',lambda *a:None)
 monkeypatch.setattr(d,'_adopt_flow',lambda *a:None)
 monkeypatch.setattr(d,'_recover_send',lambda *a:False)
 for _ in range(2):d.tick(runner,services)
 assert any(row['title']=='已转人工，等待销售处理' for row in timeline.model())
 assert runner.binding.run_status==run_status


def test_persistent_owner_projection_does_not_flash_or_claim_ai_on_event_closure(runtime):
 d,r,t,events=runtime
 binding={'binding_id':'binding','remark_code':'CJTEST','open_handoffs':[],
          'reply_ownership':{'owner':'human','revision':2,'round_id':None}}
 for _ in range(5):d._publish_open_handoffs(r,binding)
 assert len([e for e in events if e.get('phase')=='reply_owner'])==1
 assert '销售' in t.model()[-1]['description']
 binding['reply_ownership']={'owner':'human','revision':3,'round_id':'round'}
 d._publish_open_handoffs(r,binding)
 assert '等待销售回复' in t.model()[-1]['description']
 binding['reply_ownership']={'owner':'ai','revision':3,'round_id':None}
 d._publish_open_handoffs(r,binding)
 assert 'AI' in t.model()[-1]['description']
 assert r.binding.run_status=='running'


def test_open_friend_hold_survives_restart_and_polling_without_hiding_real_checks(runtime):
 from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
 d,r,t,_=runtime
 binding={'binding_id':'binding-A','remark_code':'CJTESTA',
          'open_handoffs':[fact('HOOK_FRIEND_VERIFICATION_REQUIRED')]}
 original=copy.deepcopy(binding)
 rt=HookFormalRuntime.__new__(HookFormalRuntime);rt.lifecycle_runner=r
 rt.register_binding_progress(binding,'binding-A:initial',kind='reconcile')
 emit_progress(r,'binding-A:initial','history','waiting','history_wait')
 rt.register_binding_progress(binding,'retained-send',kind='batch')
 emit_progress(r,'retained-send','send_confirm','waiting','send_wait')
 other={'binding_id':'binding-B','remark_code':'CJTESTB'}
 rt.register_binding_progress(other,'binding-B:initial',kind='reconcile')
 emit_progress(r,'binding-B:initial','history','waiting','history_wait')
 for _ in range(3):
  d._publish_open_handoffs(r,binding)
  emit_progress(r,'binding-A:initial','history')
  emit_progress(r,'binding-A:initial','history','waiting','history_wait')
 current=t.model(historical=False)
 hold=next(row for row in current if row['title']=='需要好友验证，等待销售处理')
 assert hold['state']=='waiting' and '不会自动补发' in hold['description']
 assert hold['time']==t.model(historical=True)[-1]['time']
 assert not any(row['stepId']=='binding-A:initial:history' for row in current)
 assert any(row['stepId']=='binding-B:initial:history' for row in current)
 assert any(row['stepId']=='retained-send:send_confirm' for row in current)
 assert any(row['stepId']=='binding-A:initial:history' for row in t.model())
 assert binding==original and r.binding.run_status=='running'
 # An actual observation failure and a batch's pre-send read remain visible.
 emit_progress(r,'binding-A:initial','history','error','failed')
 emit_progress(r,'retained-send','history','waiting','history_wait')
 assert any(row['stepId']=='binding-A:initial:history' and row['state']=='error' for row in t.model(historical=False))
 assert any(row['stepId']=='retained-send:history' for row in t.model(historical=False))


def test_friend_hold_requires_authoritative_open_record_and_explicit_closure(runtime):
 d,r,t,_=runtime
 binding={'binding_id':'binding','remark_code':'CJTEST',
          'open_handoffs':[fact('HOOK_FRIEND_VERIFICATION_REQUIRED')]}
 d._publish_open_handoffs(r,binding)
 before=copy.deepcopy(t.model(historical=False))
 for records in (None,[None],[{'id':''}]):
  d._publish_open_handoffs(r,{**binding,'open_handoffs':records})
  assert t.model(historical=False)==before
 d._publish_open_handoffs(r,{**binding,'open_handoffs':[]})
 assert t.model(historical=False)[-1]['title']=='本次交接事项已处理'
 assert 'AI' not in t.model(historical=False)[-1]['description']
 d._publish_open_handoffs(r,{**binding,'open_handoffs':[{**binding['open_handoffs'][0],'id':'new-event'}]})
 assert t.model(historical=False)[-1]['title']=='需要好友验证，等待销售处理'
 assert t.model(historical=False)[-1]['state']=='waiting'


def test_real_worker_model_displays_current_friend_hold_while_keeps_original_pause(runtime):
 from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
 d,_,_,_=runtime
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client','paused'))
   w._publish=Mock();w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
   runner=SimpleNamespace(on_runtime_process=w.on_runtime_process)
   binding={'binding_id':'binding','remark_code':'CJTEST',
       'reply_ownership':{'owner':'human','round_id':None,'revision':1},
       'open_handoffs':[fact('HOOK_FRIEND_VERIFICATION_REQUIRED')]}
   d._publish_open_handoffs(runner,binding)
   rt=HookFormalRuntime.__new__(HookFormalRuntime);rt.lifecycle_runner=runner
   rt.register_binding_progress(binding,'binding:initial',kind='reconcile')
   for _ in range(3):
    emit_progress(runner,'binding:initial','history')
    emit_progress(runner,'binding:initial','history','waiting','history_wait')
   rows=w._state()['model']['hookProcessSteps']
   assert [row['title'] for row in rows]==['需要好友验证，等待销售处理']
   assert rows[0]['state']=='waiting' and w.binding.run_status=='paused'
   d._publish_open_handoffs(runner,{**binding,'open_handoffs':[]})
   rows=w._state()['model']['hookProcessSteps']
   assert any(row['title']=='当前回复负责人' and '销售持续负责' in row['description'] for row in rows)
   assert w.binding.run_status=='paused'
