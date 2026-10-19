"""Real popup classifier/recovery gates, synthetic Win32 and backend boundaries."""
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client import startup_check_state, storage
from chejin_worker_client.hook_factory import WindowsC1
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.models import Binding, RpaResult


@pytest.fixture
def recovery(tmp_path):
    scene={'popup':True,'minimized':False,'foreign':False}
    main={'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}
    popup={'left':320,'top':125,'right':770,'bottom':905,'width':450,'height':780}
    gui=SimpleNamespace(IsWindowVisible=lambda h:h==11 or scene['popup'],
        IsIconic=lambda h:scene['minimized'], GetClassName=lambda h:'WeChatMainWndForPC' if h==11 else 'SyntheticInviteWnd',
        GetWindow=lambda h,k:0, GetForegroundWindow=lambda:22,
        EnumWindows=lambda fn,arg:[fn(h,arg) for h in (11,22)])
    ops=SimpleNamespace(win32gui=gui,
        win32process=SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,123 if h==22 and scene['foreign'] else 99)),
        get_window_geometry=lambda h:main if h==11 else popup,window_dpi_scale=lambda h:1)
    native=WindowsC1(tmp_path)
    native._window=Mock(return_value=(ops,11))  # Only the native/DPI boundary.
    process=SimpleNamespace(pid=99)
    generation=Mock()
    connection=SimpleNamespace(prepared=SimpleNamespace(process=process),gateway=SimpleNamespace(generation_inspector=generation),
        update_safety_snapshot=lambda:{'checking':False,'process_generation':'synthetic-process'})
    services=HookWorkerServices(connection,Mock(),intake_interval_seconds=.2,projection_batch_size=3,
        dispatcher=SimpleNamespace(),recovery_window_check=native.check_recovery_window)
    services._startup_check=startup_check_state.passed()
    return services,scene,generation,native


def test_cached_startup_pass_cannot_hide_open_invite_and_close_allows_recovery(recovery):
    services,scene,generation,native=recovery
    assert services.startup_check_state()['canStart']
    check=services.fault_recovery_check()
    assert not check['canStart'] and check['title']=='请先关闭微信弹窗'
    scene['popup']=False
    assert services.fault_recovery_check()['canStart']
    assert generation.call_count==3  # failed scan then before/after successful scan
    assert services._startup_check==startup_check_state.passed()


def test_recovery_is_readonly_and_does_not_treat_other_app_as_wechat_popup(recovery):
    services,scene,generation,native=recovery
    scene['foreign']=True
    assert services.fault_recovery_check()['canStart']
    # Native boundary exposes only inspection functions: an attempt to activate,
    # resize, click, capture or submit would raise, not silently succeed.
    native._window.assert_called_once()


def test_failed_or_changed_process_never_becomes_recoverable(recovery):
    services,scene,generation,native=recovery
    scene['popup']=False
    generation.side_effect=[None,ValueError('HOOK_PROCESS_GENERATION_CHANGED')]
    assert not services.fault_recovery_check()['canStart']
    generation.side_effect=None
    scene['minimized']=True
    assert not services.fault_recovery_check()['canStart']
    scene['minimized']=False
    native._window.side_effect=RuntimeError('untrusted diagnostic text')
    check=services.fault_recovery_check()
    assert not check['canStart'] and 'untrusted' not in str(check)


def test_existing_blockers_precede_extra_local_window_check(recovery):
    services,scene,generation,native=recovery
    services._startup_check=startup_check_state.failed('HOOK_PROCESS_ACTIVE_CALL_QUARANTINED')
    assert not services.fault_recovery_check()['canStart']
    native._window.assert_not_called();generation.assert_not_called()


def test_business_adapter_missing_recovery_checker_fails_closed(recovery):
    services,*_=recovery
    services._recovery_window_check=None
    assert not services.fault_recovery_check()['canStart']


def test_original_recovery_button_rechecks_popup_after_previously_ready(harness,recovery,monkeypatch):
    services,scene,_,_=recovery
    api=FakeApi(None);api.task_lease_fencing_tokens={}
    runner,_=harness.make_runner(api,FakeBridge(RpaResult(ok=True,result_code='unused')))
    runner.hook_services=services
    runner.bridge.sidecar_active=lambda:False
    runner.binding=Binding('worker-test','test-token','instance-test',run_status='faulted')
    storage.save_binding(runner.binding)
    runner._backend_confirmed_run_status='faulted'
    runner._backend_fault_recovery={'protocol_version':1,'ready':True}
    runner._recovery_heartbeat_at=time.monotonic()
    runner.last_rpa_component_status='ready';runner.last_wechat_status='logged_in'
    monkeypatch.setattr(runner,'post_update_runtime_health_snapshot',lambda:{'ready':True})
    runner._check_fault_recovery()  # Surface unexpected fixture errors directly.
    runner._publish_fault_recovery()
    assert not runner.fault_recovery_state()['ready']
    assert '弹窗' in runner.fault_recovery_state()['reason']
    assert not runner._request_fault_recovery()
    scene['popup']=False
    runner._publish_fault_recovery()
    assert runner.fault_recovery_state()['ready']
    assert runner._request_fault_recovery()
    scene['popup']=True  # popup returns after the button had been enabled
    api.set_run_status=Mock(side_effect=AssertionError('must not enable accepting'))
    runner._process_fault_recovery()
    api.set_run_status.assert_not_called()
    assert runner.binding.run_status=='faulted'
    # Original unsettled-record gate must still dominate a closed window.
    scene['popup']=False
    runner._backend_fault_recovery={'protocol_version':1,'ready':False,'reason':'原任务待结算'}
    assert not runner._check_fault_recovery()['ready']


def test_original_recovery_can_resume_after_operator_closes_popup(harness,recovery,monkeypatch):
    services,scene,_,_=recovery
    api=FakeApi(None);api.task_lease_fencing_tokens={}
    # Keep the backend boundary explicit; actual recovery/SQLite stay real.
    original=api.set_run_status
    api.set_run_status=Mock(side_effect=lambda binding,status,**kwargs:original(binding,status))
    runner,_=harness.make_runner(api,FakeBridge(RpaResult(ok=True,result_code='unused')))
    runner.bridge.sidecar_active=lambda:False;runner.hook_services=services
    runner.binding=Binding('worker-test','test-token','instance-test',run_status='faulted')
    storage.save_binding(runner.binding)
    runner._backend_confirmed_run_status='faulted'
    runner._backend_fault_recovery={'protocol_version':1,'ready':True}
    runner._recovery_heartbeat_at=time.monotonic()
    runner.last_rpa_component_status='ready';runner.last_wechat_status='logged_in'
    monkeypatch.setattr(runner,'post_update_runtime_health_snapshot',lambda:{'ready':True})
    runner._publish_fault_recovery()
    assert not runner.fault_recovery_state()['ready']
    scene['popup']=False
    runner._publish_fault_recovery()
    assert runner.binding.run_status=='faulted'  # Never automatic resume.
    assert runner._request_fault_recovery()
    runner._process_fault_recovery()
    assert runner.binding.run_status=='running'
    assert storage.load_binding().run_status=='running'
    assert api.set_run_status.call_count==1
    assert api.set_run_status.call_args.kwargs=={'recover_from_fault':True}
