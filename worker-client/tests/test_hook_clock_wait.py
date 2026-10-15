"""Network clock waiting through the real business supervisor and raw receiver."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import json,time,threading
import pytest
from chejin_worker_client.hook_clock import HookBackendClock
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from test_hook_worker_services import runtime,projected_count
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_connection import callback


def stamped_peer(peer):
    data=json.loads(peer.body)
    data['data']['last_heartbeat_at']=datetime.now(timezone.utc).isoformat()
    peer.body=json.dumps(data).encode()


@pytest.mark.parametrize('wait_mode',['stale','slow'])
def test_clock_wait_blocks_dispatch_and_preserves_raw_intake(runtime,lab,backend_peer,tmp_path,wait_mode):
    runner,services,binding,profiles,errors,statuses,posted,_=runtime
    stamped_peer(backend_peer)
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    runner.tick_once()
    original=services.connection.prepared
    now=[original.account.response_received_monotonic]
    clock=HookBackendClock(monotonic=lambda:now[0]);clock.observe(original.account)
    account={k:getattr(original.registration.scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    calls=[]
    class Client:
        def call(self,*args,**kwargs):
            calls.append(args[2])
            return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'paused','inflight_flow':{},
                'bindings':[],'invitations':[],'retained_sends':[],'inflight_task':None}
    class Runtime:
        def __init__(self,*args,**kwargs):
            self.clock=clock;self.account=account;self.credentials=services._credentials;self.scope=original.registration.scope
            self.active_flow_id=self.active_task_id=None
    d=HookBusinessDispatcher(Client(),tmp_path/'business',lambda *a:pytest.fail('physical UI called'),
        lambda *a:pytest.fail('new startup called'),automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    services.dispatcher=d
    display=[];runner.on_task=display.append
    now[0]+=130
    if wait_mode=='slow':
        current=replace(original.account,server_time_utc=(datetime.fromisoformat(original.account.server_time_utc)+timedelta(seconds=130)).isoformat(),
            request_started_monotonic=now[0]-2,response_received_monotonic=now[0])
        services.connection._prepared=replace(original,account=current)
    backend_peer.server_close()
    runner.tick_once()  # Authenticated receiver is preserved during connection failure.
    assert services.connection.prepared is not None
    reports=[]
    def report(code,**kwargs):
        reports.append(code);runner.stop_event.set()
    d._report_wait=report
    services.business_loop(runner,lambda _:None)
    # The prior accepted anchor expired in both cases; a slow sample cannot renew it.
    expected='HOOK_BACKEND_CLOCK_STALE'
    assert reports==[expected] and calls==[] and not errors and not posted
    assert binding.run_status=='paused' and services.startup_check_state()['canStart'] is False
    callback(services.connection)
    services.projection.project_next(original.registration.scope)
    assert projected_count()==1
    assert all(path in {'/IsLoginStatus','/ConfigureMsgReciveFullURL'} for path,*_ in lab[0].requests)
    runner.stop_event.clear()
    # Fresh verified response arrives after the failed sample, under the same identity.
    now[0]+=.05
    fresh=replace(original.account,server_time_utc=(datetime.fromisoformat(original.account.server_time_utc)+timedelta(seconds=130.05)).isoformat(),
        request_started_monotonic=now[0]-.05,response_received_monotonic=now[0])
    services.connection._prepared=replace(original,account=fresh)
    d.tick(runner,services)
    assert len(calls)==1 and binding.run_status=='paused' and not errors
    assert display==[None]
    assert clock.now()==datetime.fromisoformat(fresh.server_time_utc)-timedelta(microseconds=1)
    assert services.connection.prepared.registration is original.registration


@pytest.mark.parametrize('bad_code',['HOOK_BACKEND_CLOCK_UNCONFIRMED','HOOK_BACKEND_CLOCK_CHANGED'])
def test_genuine_clock_fault_still_uses_original_stop(runtime,bad_code):
    runner,services,binding,_,errors,_,_,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    def tick(*_):
        runner.stop_event.set();raise ValueError(bad_code)
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01)
    services.business_loop(runner,lambda _:None)
    assert binding.run_status=='faulted' and len(errors)==1
    assert services.startup_check_state()['canStart'] is False
    assert services.startup_check_state()['faultTitle']


def test_fresh_connection_check_never_clears_original_fault(runtime,backend_peer,monkeypatch):
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    stamped_peer(backend_peer)
    services.dispatcher=SimpleNamespace(automatic_transcription_disabled=lambda:True,_report_wait=lambda _,**kwargs:None,prepare_startup_window=lambda *args:None)
    monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',lambda _:{'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}})
    runner.tick_once()
    assert services.startup_check_state()['canStart'] is True
    services._fault(runner,'HOOK_BACKEND_CLOCK_UNCONFIRMED')
    # Existing recovery guard remains the only way to release faulted status.
    runner.tick_once()
    assert binding.run_status=='faulted' and posted==['faulted']
    assert services.startup_check_state()['faultTitle']=='后台时间检查未通过'


def test_slow_connection_disables_start_until_fresh_response(runtime,backend_peer,monkeypatch):
    monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',lambda _:{'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}})
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    stamped_peer(backend_peer)
    services.dispatcher=SimpleNamespace(automatic_transcription_disabled=lambda:True,_report_wait=lambda _,**kwargs:None,prepare_startup_window=lambda *args:None)
    original=services.connection.prepare
    def slow(*args):
        prepared=original(*args)
        return replace(prepared,account=replace(prepared.account,request_started_monotonic=prepared.account.response_received_monotonic-2))
    monkeypatch.setattr(services.connection,'prepare',slow)
    runner.tick_once()
    assert services.startup_check_state()['title']=='等待后台连接稳定'
    assert not errors and not posted and binding.run_status=='paused'
    monkeypatch.setattr(services.connection,'prepare',original)
    runner.tick_once()
    assert services.startup_check_state()['canStart'] is True
    assert not errors and not posted and binding.run_status=='paused'


@pytest.mark.parametrize('code',['HOOK_RUNTIME_NOT_RUNNING','HOOK_SEND_LATEST_CONTEXT_STALE'])
def test_business_wait_does_not_turn_into_name_error(runtime,code):
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    def tick(*_):
        runner.stop_event.set();raise ValueError(code)
    waits=[];services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,_report_wait=lambda value,**kw:waits.append((value,kw)))
    services.business_loop(runner,lambda _:None)
    assert waits==[(code,{'context':'business'})] and not errors and not posted


@pytest.mark.parametrize('mode',['slow','stale'])
@pytest.mark.parametrize('other_wait',[False,True])
def test_actual_faulted_tick_clears_only_clock_wait_after_calibration(tmp_path,monkeypatch,mode,other_wait):
    from chejin_worker_client import storage
    from chejin_worker_client.hook_progress import HookProgressTimeline
    stamp=datetime(2026,10,13,tzinfo=timezone.utc);now=[10.05]
    def receipt(offset=0,start=10,end=10.05):
        return SimpleNamespace(server_time_utc=(stamp+timedelta(seconds=offset)).isoformat(),request_started_monotonic=start,response_received_monotonic=end)
    clock=HookBackendClock(monotonic=lambda:now[0]);clock.observe(receipt())
    scope=SimpleNamespace(self_wxid='synthetic-self',account_epoch='epoch',process_generation='generation')
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    prepared=SimpleNamespace(registration=SimpleNamespace(scope=scope),account=receipt())
    now[0]=140.05
    if mode=='slow':prepared.account=receipt(130,138.05,140.05)
    calls=[];timeline=HookProgressTimeline()
    record={'id':'handoff','batch_id':'batch','handoff_reason_code':'AI_ENGINE_RETRY_EXHAUSTED','created_at':'2026-10-12T13:23:29Z'}
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            calls.append(path);assert path.endswith('/native-work')
            return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'faulted','inflight_flow':{},'bindings':[{'binding_id':'binding','open_handoffs':[record]}],'invitations':[],'retained_sends':[],'inflight_task':None}
    class Runtime:
        def __init__(self,*args,**kwargs):
            self.account=account;self.scope=scope;self.credentials=SimpleNamespace(worker_id='worker');self.clock=clock;self.active_flow_id=self.active_task_id=None
        def sync(self,*args):pytest.fail('Faulted customer history ran')
        def reply_once(self,*args,**kwargs):pytest.fail('Faulted worker sent a reply')
    monkeypatch.setattr(storage,'load_runtime_control',lambda:{})
    monkeypatch.setattr(storage,'append_log',lambda *args,**kwargs:None)
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='faulted'),stop_event=threading.Event(),api=SimpleNamespace(),on_runtime_process=timeline.apply)
    services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=None),_credentials=None)
    d=HookBusinessDispatcher(Client(),tmp_path,None,None,automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    with pytest.raises(ValueError,match='CLOCK_RTT_UNCONFIRMED|CLOCK_STALE') as exc:d.tick(runner,services)
    d._report_wait(str(exc.value))
    if other_wait:d._report_wait('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE')
    assert any(r['title']=='等待后台响应稳定' for r in timeline.model())
    # The same receiver, runtime, customer hold and stopped intent remain.
    now[0]=140.1
    prepared.account=receipt(130.05,140.05,140.1)
    for _ in range(3):d.tick(runner,services)
    rows=timeline.model()
    assert not any(r['title']=='等待后台响应稳定' for r in rows)
    assert sum(r['title']=='已转人工，等待销售处理' for r in rows)==1
    # Authenticated business inventory success proves this owner's backend connection recovered.
    # A heartbeat alone still cannot clear a business owner's wait (separate test below).
    assert not any(r['title']=='等待连接恢复' for r in rows)
    assert 'business' not in d._pending_wait_phases
    assert runner.binding.run_status=='faulted' and not d._business_cycle_verified and len(calls)==4


def test_clock_wait_resolution_does_not_clear_other_owner_or_network_wait(tmp_path,monkeypatch):
    from chejin_worker_client import storage
    from chejin_worker_client.hook_progress import HookProgressTimeline
    monkeypatch.setattr(storage,'append_log',lambda *args,**kwargs:None)
    timeline=HookProgressTimeline();d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
    d._progress_runner=SimpleNamespace(on_runtime_process=timeline.apply)
    d._report_wait('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED',context='business')
    d._report_wait('HOOK_BACKEND_CLOCK_STALE',context='connection')
    d._report_wait('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE',context='business')
    d.resolve_wait('business',phase='clock_wait')
    assert len(timeline.model())==2 and 'clock_wait' in d._pending_wait_phases['connection']
    assert d._pending_wait_phases['business']=={'connection_wait'}
    d.resolve_wait('connection',phase='clock_wait')
    assert [r['title'] for r in timeline.model()]==['等待连接恢复']


@pytest.mark.parametrize('business_wait',[None,'HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED','HOOK_BACKEND_TEMPORARILY_UNAVAILABLE','uncalibrated'])
@pytest.mark.parametrize('intent',['paused','running','faulted'])
def test_connection_clock_wait_recovers_with_real_wait_registry(runtime,backend_peer,tmp_path,monkeypatch,business_wait,intent):
    """A transient slow heartbeat must not latch its own start-button gate."""
    from chejin_worker_client.hook_progress import HookProgressTimeline
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    stamped_peer(backend_peer)
    binding.run_status=intent
    body=json.loads(backend_peer.body);body['data']['run_status']=intent
    backend_peer.body=json.dumps(body).encode()
    timeline=HookProgressTimeline();runner.on_runtime_process=timeline.apply
    d=HookBusinessDispatcher(None,tmp_path,None,lambda *args:None,automatic_transcription_disabled=lambda:True)
    services.dispatcher=d;d._progress_runner=runner
    # The independent business owner has already verified its clock.
    d._business_clock_ready=True
    monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',lambda _:{'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}})
    original=services.connection.prepare
    def slow(*args):
        prepared=original(*args)
        return replace(prepared,account=replace(prepared.account,request_started_monotonic=prepared.account.response_received_monotonic-2))
    monkeypatch.setattr(services.connection,'prepare',slow)
    runner.tick_once()
    assert d.pending_wait_snapshot()['connection']=={'clock_wait'}
    assert not services.startup_check_state()['canStart']
    runner.tick_once()  # A second slow sample must keep the gate closed.
    assert not services.startup_check_state()['canStart']
    if business_wait=='uncalibrated':d._business_clock_ready=False
    elif business_wait:d._report_wait(business_wait,context='business')
    monkeypatch.setattr(services.connection,'prepare',original)
    for _ in range(3):runner.tick_once()
    assert 'connection' not in d.pending_wait_snapshot()
    assert services.startup_check_state()['canStart'] is (business_wait is None)
    assert ('business' in d.pending_wait_snapshot()) is (business_wait not in (None,'uncalibrated'))
    if business_wait is None:
        assert not any(row['title']=='等待后台响应稳定' for row in timeline.model())
    assert binding.run_status==intent and not errors and not posted


def test_wait_diagnostics_are_deduplicated_per_owner_and_recovery(tmp_path,monkeypatch):
    from chejin_worker_client import storage
    records=[]
    monkeypatch.setattr(storage,'append_log',lambda *args,**kw:records.append(kw))
    d=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
    code='HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED'
    for owner in ('connection','business'):
        for _ in range(3):d._report_wait(code,context=owner,clock_check={'reason':'slow_response','receipt_rtt_seconds':2.1})
    assert [r['metadata']['wait_owner'] for r in records]==['connection','business']
    assert all(r['metadata']['clock_check']['receipt_rtt_seconds']==2.1 for r in records)
    d.resolve_wait('connection',phase='clock_wait')
    assert d.pending_wait_snapshot()=={'business':{'clock_wait'}}
    d._report_wait(code,context='connection',clock_check={'reason':'slow_response','receipt_rtt_seconds':1.5})
    assert len(records)==3
    d._report_wait(code,context='business',clock_check={'reason':'wide_uncertainty','uncertainty_seconds':1.1})
    assert len(records)==4


def test_business_supervisor_preserves_clock_wait_evidence(runtime):
    from chejin_worker_client.hook_clock import ClockWaitError
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    evidence={'reason':'slow_response','receipt_rtt_seconds':2.5}
    def tick(*_):
        runner.stop_event.set()
        raise ClockWaitError('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED',**evidence)
    waits=[]
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,_report_wait=lambda code,**kw:waits.append((code,kw)))
    services.business_loop(runner,lambda _:None)
    assert waits==[('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED',{'context':'business','clock_check':evidence})]
    assert not errors and not posted and binding.run_status=='paused'
