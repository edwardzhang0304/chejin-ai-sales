"""Real model projection: display changes never change recovery admission."""
import copy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from test_web_ui_binding_behavior import _headless_web_ui_module, WebUiBindingBehaviorTest
from chejin_worker_client.models import Binding


def display_state(case):
    ready, checking = case == 'ready', case == 'checking'
    status = {'ready':'可以恢复接单','checking':'正在恢复接单','blocked':'已停止接单'}.get(case,'暂停接单')
    run = case if case in ('running','paused') else 'faulted'
    with _headless_web_ui_module() as module, patch.object(module,'_log_rows',return_value=[]), patch.object(module,'latest_incident',return_value=None), patch.object(module,'lock_summary',return_value={}):
        window = WebUiBindingBehaviorTest._window(module, Binding('fixture','fixture-token','fixture-client',run_status=run))
        window.connection_status = 'online'
        window.runner.hook_services = SimpleNamespace(startup_check_state=lambda:{'state':'passed','title':'运行环境检查通过','message':'','canStart':True})
        window.runner.fault_recovery_state = lambda:{'ready':ready,'checking':checking,'reason':'旧记录已处理完成' if ready else '等待安全检查','statusText':status}
        current = [{'state':'done','title':'运行环境检查通过','groupId':'startup'}, {'state':'done','title':'已转人工，等待销售处理','groupId':'current-customer'}]
        if run == 'faulted':current.append({'state':'error','title':'已停止接单','groupId':'system'})
        historical = [{'state':'done','title':'历史人工交接不应展示','groupId':'old-customer'}]
        original = copy.deepcopy((current,historical))
        requested = []
        def timeline_model(*,historical=None):
            requested.append(historical)
            return copy.deepcopy(current if historical is False else original[1])
        window.hook_progress_timeline = SimpleNamespace(model=timeline_model)
        result = {'screen':window._screen(),'model':window._model()}
        assert 'hookHistorySteps' not in result['model']
        assert requested == [False]
        assert (current,historical) == original
        assert window.binding.run_status == run
        if run == 'faulted':assert result['model']['status']['receiveState'] == status
        return result


@pytest.mark.parametrize('case',['ready','checking','blocked','running','paused'])
def test_recovery_projection_and_history_preservation(case):
    display_state(case)
