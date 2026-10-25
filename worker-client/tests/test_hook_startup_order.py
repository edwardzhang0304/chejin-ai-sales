"""Startup sequence through real connection/SQLite; Win32 movement is simulated."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from test_hook_worker_services import runtime
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from chejin_worker_client import storage, ui_lock
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_progress import HookProgressTimeline

GEOMETRY={'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}}


def configure(runtime,monkeypatch):
 runner,services,binding,*rest=runtime
 runner.binding=binding;services.attach(binding,lambda:None)
 calls=[]
 def prepare(prepared,gateway):
  assert services.startup_window_position_payload() is None
  calls.append('normalize_wechat')
 def geometry(process):
  assert calls and calls[0]=='normalize_wechat'
  calls.append('publish_new_geometry');return GEOMETRY
 services.dispatcher=SimpleNamespace(automatic_transcription_disabled=lambda:True,
     prepare_startup_window=Mock(side_effect=prepare),_report_wait=Mock(),diagnostic_context={})
 monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',geometry)
 return runner,services,binding,calls,rest


@pytest.mark.parametrize('run_status',['paused','faulted','running'])
def test_startup_normalizes_before_worker_geometry_even_when_stopped(runtime,backend_peer,monkeypatch,run_status):
 runner,services,binding,calls,rest=configure(runtime,monkeypatch)
 binding.run_status=run_status
 body=json.loads(backend_peer.body);body['data']['run_status']=run_status;backend_peer.body=json.dumps(body).encode()
 events=[];runner.on_runtime_process=events.append
 services.tick(runner)
 assert calls==['normalize_wechat','publish_new_geometry']
 assert services.startup_window_position_payload()==GEOMETRY
 assert services.startup_window_ready_for(services.connection.prepared)
 assert services.startup_check_state()['canStart']
 assert binding.run_status==run_status
 services.tick(runner)
 assert services.dispatcher.prepare_startup_window.call_count==1
 assert calls.count('normalize_wechat')==1
 if run_status=='faulted':
  ready=[e for e in events if e.get('phase')=='ready']
  assert ready and ready[-1]['note'] is None
  assert rest[3]==[]  # No POST changing run status to running.


def test_busy_startup_does_not_publish_old_geometry_or_fault(runtime,monkeypatch):
 runner,services,binding,calls,_=configure(runtime,monkeypatch)
 prepare=services.dispatcher.prepare_startup_window
 prepare.side_effect=ui_lock.UiLockError(ui_lock.UI_LOCK_BUSY,"Busy fixture")
 services.tick(runner)
 assert binding.run_status=='paused'
 assert not services.startup_check_state()['canStart']
 assert services.startup_window_position_payload() is None
 assert not services.startup_window_ready_for(services.connection.prepared)
 prepare.side_effect=lambda *args:calls.append('normalize_wechat')
 services.tick(runner)
 assert services.startup_check_state()['canStart']
 assert services.startup_window_position_payload()==GEOMETRY


def test_failed_normalization_never_publishes_worker_position(runtime,monkeypatch):
 runner,services,binding,calls,_=configure(runtime,monkeypatch)
 services.dispatcher.prepare_startup_window.side_effect=ValueError('HOOK_C1_STARTUP_WINDOW_UNVERIFIED')
 services.tick(runner)
 assert services.startup_window_position_payload() is None
 assert not services.startup_window_ready_for(services.connection.prepared)
 assert binding.run_status=='faulted'
 assert services.startup_check_state()['title']=='微信窗口调整未通过'


def test_retained_flow_defers_movement_until_original_settlement(runtime,monkeypatch):
 runner,services,binding,calls,_=configure(runtime,monkeypatch)
 storage.begin_runtime_flow('retained-startup-flow','task')
 services.tick(runner)
 assert not calls and services.startup_window_position_payload() is None
 assert not services.startup_check_state()['canStart']
 assert storage.load_runtime_control()['inflight_flow_id']=='retained-startup-flow'
 storage.finish_runtime_flow('retained-startup-flow')
 services.tick(runner)
 assert calls==['normalize_wechat','publish_new_geometry']


def test_dispatcher_startup_normalizes_once_per_approved_account_and_generation(tmp_path):
 startup=Mock();d=HookBusinessDispatcher(None,tmp_path,None,startup,automatic_transcription_disabled=lambda:True)
 scope=SimpleNamespace(self_wxid='self',account_epoch='epoch',process_generation='one')
 prepared=SimpleNamespace(process=object(),registration=SimpleNamespace(scope=scope));gateway=object()
 d.prepare_startup_window(prepared,gateway);d.prepare_startup_window(prepared,gateway)
 assert startup.call_count==1
 scope.process_generation='two';d.prepare_startup_window(prepared,gateway)
 assert startup.call_count==2


def test_timeline_hides_removed_fault_resume_explanation():
 t=HookProgressTimeline()
 t.apply({'event':'hook_progress','context':'startup','phase':'ready','state':'done','note':'resume_required'})
 assert not t.model()[0]['description']


def test_real_gui_places_worker_only_after_wechat_preparation(runtime,monkeypatch):
 from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
 runner,services,binding,calls,_=configure(runtime,monkeypatch)
 with _headless_web_ui_module() as module:
  window=WebUiBindingBehaviorTest._window(module,None)
  window.runner=runner;window.rpa_bridge=object();window.move=Mock(side_effect=lambda *args:calls.append('move_worker'))
  available=SimpleNamespace(x=lambda:0,y=lambda:0,width=lambda:1536,height=lambda:864)
  screen=SimpleNamespace(availableGeometryChanged=Mock(), availableGeometry=lambda:available,devicePixelRatio=lambda:1.25)
  module.QGuiApplication=SimpleNamespace(screenAt=lambda _:screen,primaryScreen=lambda:screen)
  window._position_next_to_wechat_once()
  assert calls==[] and not window._startup_position_attempted
  runner.on_profile=lambda _:window._position_next_to_wechat_once()
  services.tick(runner)
  assert calls==['normalize_wechat','publish_new_geometry','move_worker']
  window.move.assert_called_once_with(872,12)
  services.tick(runner)
  window.move.assert_called_once()


def test_actual_startup_ui_shows_window_order_and_saved_fault(runtime,backend_peer,monkeypatch):
 import os
 from pathlib import Path
 from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
 runner,services,binding,calls,_=configure(runtime,monkeypatch)
 binding.run_status='faulted'
 body=json.loads(backend_peer.body);body['data']['run_status']='faulted';backend_peer.body=json.dumps(body).encode()
 with _headless_web_ui_module() as module:
  monkeypatch.setattr(module,'_log_rows',lambda:[]);monkeypatch.setattr(module,'latest_incident',lambda:None);monkeypatch.setattr(module,'lock_summary',lambda:{})
  w=WebUiBindingBehaviorTest._window(module,binding)
  w.runner=runner;w.connection_status='bound';w._publish=Mock();w.rpa_bridge=object();w.move=Mock()
  available=SimpleNamespace(x=lambda:0,y=lambda:0,width=lambda:1536,height=lambda:864)
  screen=SimpleNamespace(availableGeometryChanged=Mock(), availableGeometry=lambda:available,devicePixelRatio=lambda:1.25)
  module.QGuiApplication=SimpleNamespace(screenAt=lambda _:screen,primaryScreen=lambda:screen)
  runner.on_runtime_process=w.on_runtime_process
  def on_profile(profile):
   w.profile=profile;w._position_next_to_wechat_once()
  runner.on_profile=on_profile
  services.tick(runner)
  runner._fault_recovery_state={'ready':True,'checking':False,'reason':'旧记录已确认，可点击开始接单恢复。'}
  value=w._state();titles=[s['title'] for s in value['model']['hookProcessSteps']]
  assert titles.index('调整微信窗口位置和大小')<titles.index('将 Worker 放到微信旁边')
  assert value['model']['status']['receiveState']=='可以恢复接单'
  assert binding.run_status=='faulted'  # Display readiness never starts accepting work.
  assert value['model']['startupCheck']['canStart']
  assert not any('上次故障停止状态仍保留' in s.get('description','') for s in value['model']['hookProcessSteps'])
  target=os.environ.get('CHEJIN_STARTUP_ORDER_UI_STATE')
  if target:Path(target).write_text(json.dumps([value],ensure_ascii=False,indent=2))


def test_pending_window_blocks_new_work_but_allows_confirmed_old_flow_to_finish(lab,backend_peer,tmp_path):
 import threading,uuid
 from test_hook_connection import setup
 manager,credentials,state,response=setup(lab,backend_peer)
 prepared=manager.prepare(credentials,state);scope=prepared.registration.scope
 account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
 flow_id=str(uuid.uuid4());task_id=str(uuid.uuid4())
 private=tmp_path/'dispatcher';(private/'flows').mkdir(parents=True)
 (private/'flows'/f'{flow_id}.json').write_text(json.dumps({'flow_id':flow_id}))
 storage.begin_runtime_flow(flow_id,'task')
 task={'id':task_id,'task_type':'chat_reply','status':'completed','lease_fencing_token':1}
 calls=[];finished=[]
 inventory={'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'running',
     'inflight_flow':{'flow_id':flow_id,'flow_kind':'task','conversation_id':'conversation'},
     'inflight_task':task,'retained_sends':[],'bindings':[],'invitations':[]}
 class Client:
  def call(self,credentials,method,path,body=None,**kwargs):
   calls.append(path);assert path.endswith('native-work');return inventory
 class Runtime:
  def __init__(self,*args,**kwargs):
   self.credentials=credentials;self.account=account;self.scope=scope
   self.active_flow_id=self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda _:None)
  def finish(self,*args):
   finished.append(args);storage.finish_runtime_flow(flow_id);self.active_flow_id=self.active_task_id=None
 runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),api=SimpleNamespace(),on_task=lambda _:None)
 services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:False)
 d=HookBusinessDispatcher(Client(),private,None,Mock(side_effect=AssertionError('no window movement from dispatcher')),
     automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
 try:
  d.tick(runner,services)
  assert finished==[(task_id,flow_id,1,'conversation')]
  assert storage.load_runtime_control().get('inflight_flow_id') is None
  inventory.update(inflight_flow={},inflight_task=None)
  d.tick(runner,services)
  assert all(path.endswith('native-work') for path in calls)
  assert len(finished)==1
 finally:manager.close()
