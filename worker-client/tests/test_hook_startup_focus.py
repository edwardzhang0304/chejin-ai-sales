"""Real startup/heartbeat/ZIP chain, synthetic Win32 and OCR only."""
import json
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_hook_search_startup_cache import calibration
from test_hook_worker_services import runtime
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_failure_evidence_boundaries import home
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_startup_window import StartupWindowWait, StartupWindowError
from chejin_worker_client.diagnostic_bundle import export_bundle
from chejin_worker_client import storage
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as flow


def actual_window(calibration):
    native,gateway,process,state,ops,root=calibration
    foreground=[22]
    native._ops=ops
    ops.win32gui.EnumWindows=lambda visit,arg:visit(11,arg)
    ops.win32gui.GetForegroundWindow=lambda:foreground[0]
    ops.normalize_wechat_window=Mock(side_effect=lambda *a,**kw:{
        'ok':True,'after':ops.get_window_geometry(11),'after_dpi_scale':1.0})
    now=[0.0]
    native._startup_attempts.clock=lambda:now[0]
    return native,gateway,process,state,ops,root,foreground,now


def test_denied_activation_waits_then_passively_recovers_without_extra_captures(calibration,home):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    # Real prepare returns foreground_unconfirmed. No synthetic report inserted.
    with pytest.raises(StartupWindowError,match='FOREGROUND_REQUIRED'):
        n.startup(p,g,{})
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==0
    assert not n.startup_layout_ready()
    for _ in range(20):
        with pytest.raises(StartupWindowWait,match='RETRY_WAIT'):n.startup(p,g,{})
    now[0]=10
    for _ in range(20):
        with pytest.raises(StartupWindowWait,match='FOREGROUND_REQUIRED'):n.startup(p,g,{})
    assert n._startup_attempts.count==1 and o.activate_window.call_count==1
    fg[0]=11  # Simulated operator activation, not an automatic callback.
    n.startup(p,g,{})
    assert n.startup_layout_ready()
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==1
    assert o.run_ocr.call_count==1
    o.win32api.mouse_event.assert_not_called()
    reports=[json.loads(p.read_text()) for p in (root/'artifacts/tasks').glob('*/diagnostics/startup-check.json')]
    failed=next(r for r in reports if r['status']=='failed')
    passed=next(r for r in reports if r['status']=='passed')
    assert failed['window_result']['reason']=='hook_c1_foreground_unconfirmed'
    assert failed['foreground_after']['is_wechat_main'] is False
    assert passed['allow_activation'] is False and passed['attempt']==2
    assert [r['name'] for r in passed['phases']]==[
        'account_before_window','load_ocr_and_find_window','prepare_window',
        'account_after_window','search_calibration','account_after_calibration']
    assert all(r['elapsed_ms']>=0 for r in passed['phases'])
    assert passed['evidence']['frames'][0]['ocr_elapsed_ms']>=0
    # Shared exporter consumes native reports, no manual copying into ZIP.
    bundle=export_bundle(root,root/'exports')
    with zipfile.ZipFile(bundle) as z:
        entries=[json.loads(z.read(name)) for name in z.namelist() if name.endswith('/diagnostics/startup-check.json')]
    assert any(r.get('window_result',{}).get('reason')=='hook_c1_foreground_unconfirmed' for r in entries)
    assert any(r['status']=='passed' and r['attempt']==2 for r in entries)


def test_retries_are_bounded_with_original_guard_evidence_and_account_reset(calibration):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    fg[0]=11
    original=o.run_ocr.side_effect
    def steal(image):
        result=original(image)
        fg[0]=22
        return result
    o.run_ocr.side_effect=steal
    scope=SimpleNamespace(self_wxid='wxid_synthetic',account_epoch='one')
    for attempt in range(3):
        fg[0]=11;now[0]=attempt*11
        with pytest.raises(StartupWindowError,match='FOREGROUND_REQUIRED'):n.startup(p,g,scope)
        assert not n.startup_layout_ready()
    fg[0]=11;now[0]=100
    with pytest.raises(StartupWindowWait,match='RETRY_EXHAUSTED'):n.startup(p,g,scope)
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==3
    reports=[json.loads(p.read_text()) for p in (root/'artifacts/tasks').glob('*/diagnostics/initialization.json')]
    assert len(reports)==3
    assert all(r['guard_failures'][-1]['condition']=='foreground_changed' for r in reports)
    assert all(r['evidence']['frames'][0]['ocr']=='saved' for r in reports)
    # A new verified account generation is a new startup, old cache never reused.
    scope.account_epoch='two';o.run_ocr.side_effect=original
    n.startup(p,g,scope)
    assert n.startup_layout_ready() and n._startup_attempts.count==0
    assert o.activate_window.call_count==2
    o.win32api.mouse_event.assert_not_called()


def test_successful_layout_revalidation_does_not_exhaust_failure_budget(calibration):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    fg[0]=11
    for _ in range(5):
        n._search_control_cache.clear()
        n.startup(p,g,{})
        assert n.startup_layout_ready() and n._startup_attempts.count==0
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==5
    n._search_control_cache.clear();fg[0]=22
    with pytest.raises(StartupWindowWait,match='FOREGROUND_REQUIRED'):n.startup(p,g,{})
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==5


def test_actual_heartbeat_wait_preserves_paused_flow_and_recovers_without_start(runtime,calibration,monkeypatch):
    runner,services,binding,profiles,errors,statuses,posted,_=runtime
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    runner.binding=binding;services.attach(binding,lambda:None)
    # Runtime process fixture uses a different PID; all Win32 facts refer to it.
    real_process=services.connection.gateway.inspect()
    o.win32process.GetWindowThreadProcessId=lambda h:(1,real_process.pid)
    dispatcher=HookBusinessDispatcher(None,root,None,n.startup,
        automatic_transcription_disabled=lambda:True,startup_layout_check=n.startup_layout_ready,
        startup_retry_check=n.check_startup_retry)
    dispatcher._business_clock_ready=True
    services.dispatcher=dispatcher
    monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',
        lambda p:{'ok':True,'geometry':{'left':12,'top':12,'right':1072,'bottom':752,'width':1060,'height':740}})
    events=[];runner.on_runtime_process=events.append
    # Existing unsettled flow blocks even the first activation.
    storage.begin_runtime_flow('retained-startup','task')
    services.tick(runner)
    assert o.activate_window.call_count==0 and binding.run_status=='paused'
    storage.finish_runtime_flow('retained-startup')
    services.tick(runner)
    assert binding.run_status=='faulted' and o.activate_window.call_count==1
    assert not services.startup_check_state()['canStart']
    now[0]=11
    for _ in range(4):services.tick(runner)
    assert o.activate_window.call_count==1 and len(errors)==1
    assert not services.startup_check_state()['canStart']
    assert services.startup_window_position_payload() is None
    assert services.health_snapshot()['ready']  # callback/intake remains available
    waiting=[e for e in events if e.get('phase')=='wechat_window' and e.get('state')=='waiting']
    assert waiting and waiting[-1]['note']=='startup_foreground'
    fg[0]=11
    services.tick(runner)
    assert services.startup_check_state()['canStart']
    assert services.startup_window_position_payload()['ok']
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==1
    assert binding.run_status=='faulted' and 'running' not in posted
    services.tick(runner)
    assert o.try_image_grab.call_count==1  # completed startup remains cached
    o.win32api.mouse_event.assert_not_called()


def test_slow_ocr_keeps_15_second_guard_and_exports_its_elapsed_time(calibration,monkeypatch):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    fg[0]=11
    monotonic=[100.0]
    monkeypatch.setattr(flow.time,'monotonic',lambda:monotonic[0])
    original=o.run_ocr.side_effect
    def slow(image):
        result=original(image);monotonic[0]+=16
        return result
    o.run_ocr.side_effect=slow
    with pytest.raises(StartupWindowError,match='FRAME_EXPIRED'):n.startup(p,g,{})
    assert not n.startup_layout_ready() and o.try_image_grab.call_count==1
    path=next((root/'artifacts/tasks').glob('*/diagnostics/initialization.json'))
    report=json.loads(path.read_text())
    assert report['guard_failures'][-1]['condition']=='frame_expired'
    assert report['guard_failures'][-1]['observed']['age_seconds']>=16
    assert report['evidence']['frames'][0]['ocr_elapsed_ms']==16000
    o.win32api.mouse_event.assert_not_called()


def test_focus_lost_inside_native_constructor_keeps_original_diagnostics(calibration):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    fg[0]=11
    original=o.ensure_dpi_awareness_status
    reads=[0]
    def switch():
        reads[0]+=1
        if reads[0]==3:fg[0]=22  # During NativeUI initialization, after prepare.
        return original()
    o.ensure_dpi_awareness_status=switch
    with pytest.raises(StartupWindowError,match='FOREGROUND_REQUIRED'):n.startup(p,g,{})
    report=json.loads(next((root/'artifacts/tasks').glob('*/diagnostics/initialization.json')).read_text())
    assert report['guard_failures'][-1]['condition']=='foreground_changed'
    assert report['exception']['operation']=='create_ui'
    assert o.try_image_grab.call_count==0


@pytest.mark.parametrize('recover',[False,True])
def test_ocr_initialization_failure_uses_bounded_real_heartbeat_and_keeps_reports(runtime,calibration,monkeypatch,recover):
    from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
    runner,services,binding,profiles,errors,statuses,posted,_=runtime
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    runner.binding=binding;services.attach(binding,lambda:None)
    process=services.connection.gateway.inspect()
    o.win32process.GetWindowThreadProcessId=lambda h:(1,process.pid)
    fg[0]=11
    loaded_window=n._window
    n._ops=None;del n._window  # Use the actual loader, including RapidOCR.
    monkeypatch.setitem(sys.modules,'win32gui',o.win32gui)
    monkeypatch.setitem(sys.modules,'win32process',o.win32process)
    monkeypatch.setattr(sidecar,'_OCR_IMPORT_ERROR',None)
    engine=Mock(side_effect=RuntimeError('synthetic OCR initialization failure'))
    monkeypatch.setattr(sidecar,'RapidOCR',engine)
    for key in ('UI_ACTION_GUARD_PATH','UI_ACTION_AUDIT_PATH','RENDER_RECOVERY_GUARD_PATH'):
        monkeypatch.setattr(sidecar,key,getattr(sidecar,key))
    dispatcher=HookBusinessDispatcher(None,root,None,n.startup,
        automatic_transcription_disabled=lambda:True,startup_layout_check=n.startup_layout_ready,
        startup_retry_check=n.check_startup_retry)
    dispatcher._business_clock_ready=True;services.dispatcher=dispatcher
    if recover:
        services.tick(runner)
        assert engine.call_count==1 and not services.startup_check_state()['canStart']
        # The engine is now available; reuse the original synthetic Win32/OCR
        # boundary and exercise the real remaining startup/heartbeat chain.
        n._ops=o;n._window=loaded_window;now[0]=11
        monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',
            lambda p:{'ok':True,'geometry':{'left':12,'top':12,'right':1072,'bottom':752,'width':1060,'height':740}})
        services.tick(runner)
        assert services.startup_check_state()['canStart']
        assert n._startup_attempts.count==0 and n.startup_layout_ready()
        assert binding.run_status=='faulted' and 'running' not in posted
        assert o.try_image_grab.call_count==1
        o.activate_window.assert_not_called()
        return
    for attempt in range(3):
        now[0]=attempt*11
        services.tick(runner)
        assert engine.call_count==attempt+1
        for _ in range(3):services.tick(runner)
        assert engine.call_count==attempt+1  # Precheck must never reload OCR.
        assert not services.startup_check_state()['canStart']
    now[0]=100
    for _ in range(5):services.tick(runner)
    assert engine.call_count==3 and n._startup_attempts.count==3
    assert services.startup_check_state()['title']=='请重开 Worker 校验微信窗口'
    reports=[json.loads(p.read_text()) for p in (root/'artifacts/tasks').glob('*/diagnostics/startup-check.json')]
    assert len(reports)==3
    assert all(r['phases'][-1]['name']=='load_ocr_and_find_window' and
               r['phases'][-1]['status']=='failed' for r in reports)
    assert len(list((root/'artifacts/tasks').glob('*/diagnostics/initialization.json')))==3
    bundle=export_bundle(root,root/'exports')
    with zipfile.ZipFile(bundle) as z:
        exported=[json.loads(z.read(name)) for name in z.namelist() if name.endswith('/diagnostics/startup-check.json')]
    assert len(exported)==3 and all(r['phases'][-1]['status']=='failed' for r in exported)
    assert binding.run_status=='faulted' and 'running' not in posted
    o.activate_window.assert_not_called();o.try_image_grab.assert_not_called()


@pytest.mark.parametrize('operation',['EnumWindows','GetForegroundWindow'])
def test_foreground_observation_failure_is_read_only_reported_and_recovers(calibration,home,monkeypatch,operation):
    n,g,p,s,o,root,fg,now=actual_window(calibration)
    with pytest.raises(StartupWindowError,match='FOREGROUND_REQUIRED'):n.startup(p,g,{})
    now[0]=11;fg[0]=11
    original=getattr(o.win32gui,operation)
    monkeypatch.setattr(o.win32gui,operation,Mock(side_effect=OSError('synthetic window read failure')))
    for _ in range(3):
        with pytest.raises(StartupWindowWait,match='OBSERVATION_FAILED'):n.startup(p,g,{})
    assert n._startup_attempts.count==1 and o.activate_window.call_count==1
    assert o.try_image_grab.call_count==0
    bundle=export_bundle(root,root/'exports')
    with zipfile.ZipFile(bundle) as z:
        reports=[json.loads(z.read(name)) for name in z.namelist() if name.endswith('/diagnostics/window-observation.json')]
    assert len(reports)==1 and reports[0]['read_only']
    assert reports[0]['phase']=='foreground_observation' and reports[0]['elapsed_ms']>=0
    monkeypatch.setattr(o.win32gui,operation,original)
    n.startup(p,g,{})
    assert n.startup_layout_ready() and n._startup_probe_error is None
    assert o.activate_window.call_count==1 and o.try_image_grab.call_count==1
