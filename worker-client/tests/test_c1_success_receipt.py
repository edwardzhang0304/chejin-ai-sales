"""Persistence and proof guards; production HTTP replay is in backend tests."""
from dataclasses import replace
from unittest.mock import patch
import pytest
from test_c1_failure_receipt import runner_case
from chejin_worker_client.models import RpaResult
from chejin_worker_client.storage import load_c2_state, load_runtime_control, save_c2_state
import chejin_worker_client.task_runner as module

@pytest.mark.parametrize('code', ['invite_sent', 'already_friend'])
@pytest.mark.parametrize('damage', ['missing_proof', 'wrong_fence', 'running_task', 'different_task', 'wrong_result'])
def test_unproven_success_never_releases_flow(runner_case, code, damage):
    runner, api, bridge = runner_case
    bridge.result = RpaResult(ok=True, result_code=code)
    original = api.settle_task_success
    def damaged(binding, receipt):
        task = original(binding, receipt)
        if damage == 'missing_proof': return replace(task, raw={})
        if damage == 'wrong_fence': return replace(task, raw={'success_receipt': {**receipt, 'lease_fencing_token': 999}})
        if damage == 'running_task': return replace(task, status='running')
        if damage == 'different_task': return replace(task, id='wrong-task')
        return replace(task, result_code='already_friend' if code == 'invite_sent' else 'invite_sent')
    with patch.object(api, 'settle_task_success', side_effect=damaged):
        runner.tick_once()
        runner._flow_finish_retry_at = 0
        runner.tick_once()
    saved = load_c2_state(runner._inflight_finish_receipt_key(api.task.id))
    assert saved['task_success_confirmed'] is False
    assert load_runtime_control()['inflight_flow_id'] == api.task.id
    assert len(bridge.tasks) == 1
    assert not any(e.startswith('finish:') for e in api.inflight_flow_events)

@pytest.mark.parametrize('field', ['worker_id','client_instance_id','flow_id','task_id'])
def test_mismatched_saved_success_cannot_be_posted(runner_case, field):
    runner, api, bridge = runner_case
    bridge.result = RpaResult(ok=True, result_code='invite_sent')
    with patch.object(api, 'settle_task_success', side_effect=TimeoutError('lost request')):
        runner.tick_once()
    key = runner._inflight_finish_receipt_key(api.task.id)
    saved = load_c2_state(key)
    saved['task_success'][field] = 'wrong-identity'
    save_c2_state(key, saved)
    with patch.object(api, 'settle_task_success', wraps=api.settle_task_success) as send:
        runner._flow_finish_retry_at = 0
        runner.tick_once()
        send.assert_not_called()
    assert load_runtime_control()['inflight_flow_id'] == api.task.id
    assert len(bridge.tasks) == 1

def test_unsaved_success_cannot_be_sent_or_release_flow(runner_case):
    from chejin_worker_client.api import ApiError
    runner, api, bridge = runner_case
    bridge.result = RpaResult(ok=True, result_code='invite_sent')
    api.finish_inflight_error = ApiError('WORKER_INFLIGHT_TASK_PENDING','Still running',409)
    original = module.save_c2_state
    def fail_save(key, value, **kwargs):
        if value.get('task_success'): raise OSError('controlled persistence failure')
        return original(key, value, **kwargs)
    with patch.object(module,'save_c2_state',side_effect=fail_save), patch.object(api,'settle_task_success') as send:
        runner.tick_once()
        send.assert_not_called()
    assert load_runtime_control()['inflight_flow_id'] == api.task.id
    assert not runner._can_start_new_flow()
    assert len(bridge.tasks) == 1

@pytest.mark.parametrize('status', ['paused', 'faulted'])
def test_success_settlement_preserves_existing_stop(runner_case, status):
    from chejin_worker_client.storage import save_binding, load_binding
    runner, api, bridge = runner_case
    bridge.result = RpaResult(ok=True, result_code='already_friend')
    with patch.object(api,'settle_task_success',side_effect=TimeoutError('lost request')):
        runner.tick_once()
    runner.binding.run_status = status
    save_binding(runner.binding)
    runner._flow_finish_retry_at = 0
    runner.tick_once()
    assert runner.binding.run_status == load_binding().run_status == status
    assert not load_runtime_control()['inflight_flow_id']
    assert len(bridge.tasks) == 1
