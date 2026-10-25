"""Actual Worker projections and bridge guards; no new check or physical action."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chejin_worker_client import startup_check_state as checks
from chejin_worker_client.models import Binding,WorkerProfile
from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest


@pytest.mark.parametrize('code,title',[
 ('HOOK_COMPONENT_VERSION_MISMATCH','微信版本'),
 ('HOOK_COMPONENT_HASH_OR_LOAD_MISMATCH','Hook 组件'),
 ('HOOK_ACCOUNT_NOT_CONFIRMED','账号核验'),
 ('HOOK_CALLBACK_REGISTRATION_UNCONFIRMED','消息接收'),
 ('HOOK_MESSAGE_SAVE_FAILED','消息保存'),
 ('HOOK_BACKEND_NETWORK_FAILED','后台连接'),
 ('HOOK_NATIVE_AUTO_TEXT_UNCONFIRMED','语音设置')])
def test_specific_safe_check_reason(code,title):
    state=checks.failed(code)
    assert state['state']=='failed' and state['canStart'] is False
    assert title in state['title'] and state['message']
    assert code not in json.dumps(state,ensure_ascii=False)


def test_waiting_and_failed_cannot_start_but_pause_is_never_blocked():
    with _headless_web_ui_module() as module:
        window=WebUiBindingBehaviorTest._window(module,Binding('synthetic-worker','synthetic-token','client','paused'))
        window.runner.set_run_status=Mock();window._publish=Mock()
        window.update_coordinator=SimpleNamespace(note_operator_pause=Mock())
        state=checks.checking()
        window.runner.hook_services=SimpleNamespace(startup_check_state=lambda:dict(state))
        window.bridge.startAccepting();window.runner.set_run_status.assert_not_called()
        state=checks.failed('HOOK_NOT_LOGGED_IN')
        window.bridge.startAccepting();window.runner.set_run_status.assert_not_called()
        window.bridge.pauseAccepting();window.runner.set_run_status.assert_called_once_with('paused')
        state=checks.passed()
        window.bridge.startAccepting();assert window.runner.set_run_status.call_args.args==('running',)


def test_recovered_checks_do_not_automatically_start_or_change_binding():
    with _headless_web_ui_module() as module:
        binding=Binding('synthetic-worker','synthetic-token','client','paused')
        window=WebUiBindingBehaviorTest._window(module,binding)
        state=checks.failed('HOOK_COMPONENT_HASH_OR_LOAD_MISMATCH')
        window.runner.hook_services=SimpleNamespace(startup_check_state=lambda:dict(state))
        window.runner.set_run_status=Mock()
        assert not window._startup_check_state()['canStart']
        state=checks.passed()
        assert window._startup_check_state()['canStart']
        window.runner.set_run_status.assert_not_called();assert binding.run_status=='paused'


def test_real_worker_state_artifacts_for_react_dom(monkeypatch):
    import os
    with _headless_web_ui_module() as module:
        monkeypatch.setattr(module,'_log_rows',lambda:[])
        monkeypatch.setattr(module,'latest_incident',lambda:None)
        monkeypatch.setattr(module,'lock_summary',lambda:{})
        window=WebUiBindingBehaviorTest._window(module,Binding('synthetic-worker','synthetic-token','client','paused'))
        window.connection_status='bound'
        window.profile=WorkerProfile('synthetic-worker','测试销售','paused',rpa_component_status='ready',wechat_status='logged_in')
        state=checks.checking()
        window.runner.hook_services=SimpleNamespace(startup_check_state=lambda:dict(state))
        artifacts=[]
        for state in [checks.checking(),checks.failed('HOOK_COMPONENT_VERSION_MISMATCH'),checks.failed('HOOK_ACCOUNT_NOT_CONFIRMED'),checks.passed()]:
            artifacts.append(window._state())
            assert artifacts[-1]['model']['startupCheck']==state
        assert [value['screen'] for value in artifacts]==['paused-empty','automation-unavailable','automation-unavailable','paused-empty']
        from chejin_worker_client.hook_worker_services import HookWorkerServices
        service=HookWorkerServices(SimpleNamespace(on_persistence_failure=None),None,
            intake_interval_seconds=1,projection_batch_size=10)
        window.runner.hook_services=service
        window.runner.set_run_status=Mock()
        original_steps=[{'state':'error','title':'已停止接单','groupId':'system'},
                        {'state':'waiting','title':'原发送结果待核对','groupId':'customer'}]
        window.hook_progress_timeline=SimpleNamespace(model=lambda **_:list(original_steps))
        for run in ('paused','faulted','running'):
            window.binding.run_status=run
            for state in (checks.checking(),checks.failed('HOOK_NOT_LOGGED_IN'),checks.passed()):
                service._startup_check=state
                projection=window._state()
                artifacts.append(projection)
                assert projection['model']['startupCheck']==service.startup_check_state()
                assert projection['model']['status']['acceptingIntent']==(run=='running')
                if state['state']=='checking':
                    assert projection['model']['status']['receiveState']=='检查中'
                assert window.binding.run_status==run
                assert projection['model']['hookProcessSteps']==original_steps
        window.runner.set_run_status.assert_not_called()
        target=os.environ.get('CHEJIN_STARTUP_UI_STATES')
        if target:Path(target).write_text(json.dumps(artifacts,ensure_ascii=False,indent=2))


def test_persistence_callback_reports_save_failure_and_keeps_original_stop_callback():
    from chejin_worker_client.hook_worker_services import HookWorkerServices
    service=HookWorkerServices(SimpleNamespace(on_persistence_failure=None), None,
        intake_interval_seconds=1, projection_batch_size=10)
    callback=Mock();service.attach(Binding('11111111-1111-4111-8111-111111111111','synthetic-token','client','paused'),callback)
    service.connection.on_persistence_failure()
    callback.assert_called_once()
    assert service.startup_check_state()['title']=='消息保存检查未通过'


def test_stopped_runtime_cannot_reuse_old_check_pass_on_next_start():
    from chejin_worker_client.hook_worker_services import HookWorkerServices
    service=HookWorkerServices(SimpleNamespace(close=Mock()), None,
        intake_interval_seconds=1, projection_batch_size=10)
    service._startup_check=checks.passed();service._display_fault=None
    service.close()
    service.connection.close.assert_called_once()
    assert service.startup_check_state()['canStart'] is False
    assert service.startup_check_state()['state']=='checking'
