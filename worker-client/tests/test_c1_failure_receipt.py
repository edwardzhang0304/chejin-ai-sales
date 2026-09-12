"""Local proof/persistence guards; HTTP settlement is covered by backend tests."""
from dataclasses import replace
from unittest.mock import patch
import pytest
import test_task_runner as fixtures
from chejin_worker_client.models import Binding, Task, RpaResult
from chejin_worker_client.storage import load_c2_state, load_runtime_control, save_c2_state
import chejin_worker_client.task_runner as module


@pytest.fixture
def runner_case():
    helper = fixtures.TaskRunnerTest(); helper.setUp()
    api = fixtures.FakeApi(Task(id='original-c1', task_type='add_friend', status='pending', phone='13800000000'))
    bridge = fixtures.FakeBridge(RpaResult(ok=False, error_code='WECHAT_UI_LAYOUT_UNRESOLVED',
                                         failure_step='window_layout_calibration', message='Synthetic failure'))
    runner, seen = helper.make_runner(api, bridge)
    runner.binding = Binding('worker', 'synthetic-token', 'instance', run_status='running')
    try: yield runner, api, bridge
    finally: helper.tearDown()


@pytest.mark.parametrize('damage', ['missing_proof','wrong_fence','running_task','different_task'])
def test_unproven_failure_does_not_end_original_flow(runner_case, damage):
    runner, api, bridge = runner_case
    original = api.settle_task_failure
    def damaged(binding, receipt):
        task = original(binding, receipt)
        if damage == 'missing_proof': return replace(task, raw={})
        if damage == 'wrong_fence': return replace(task, raw={'failure_receipt':{**receipt,'lease_fencing_token':999}})
        if damage == 'running_task': return replace(task, status='running')
        return replace(task, id='other-task')
    with patch.object(api, 'settle_task_failure', side_effect=damaged):
        runner.tick_once()
        for _ in range(2):
            runner._flow_finish_retry_at = 0
            runner.tick_once()
    receipt = load_c2_state(runner._inflight_finish_receipt_key('original-c1'))
    assert receipt['task_failure_confirmed'] is False
    assert load_runtime_control()['inflight_flow_id'] == 'original-c1'
    assert runner.binding.run_status == 'faulted' and len(bridge.tasks) == 1
    assert not any(e.startswith('finish:') for e in api.inflight_flow_events)


def test_cannot_send_unsaved_failure_or_resume_intake(runner_case):
    runner, api, bridge = runner_case
    from chejin_worker_client.api import ApiError
    api.finish_inflight_error = ApiError('WORKER_INFLIGHT_TASK_PENDING', 'Task is still running', 409)
    original = module.save_c2_state
    def fail_save(key, value, **kwargs):
        if value.get('task_failure'): raise OSError('controlled original receipt persistence failure')
        return original(key, value, **kwargs)
    with patch.object(module, 'save_c2_state', side_effect=fail_save):
        runner.tick_once()
    assert runner.binding.run_status == 'faulted'
    assert load_runtime_control()['inflight_flow_id'] == 'original-c1'
    assert not any(event.startswith('fail:') for event in api.events)
    assert not runner._can_start_new_flow()
    assert len(bridge.tasks) == 1


@pytest.mark.parametrize('field', ['worker_id','client_instance_id','flow_id','task_id'])
def test_saved_failure_identity_mismatch_is_retained(runner_case, field):
    runner, api, bridge = runner_case
    with patch.object(api, 'settle_task_failure', side_effect=TimeoutError('one lost request')):
        runner.tick_once()
    key = runner._inflight_finish_receipt_key('original-c1')
    saved = load_c2_state(key)
    saved['task_failure'][field] = 'wrong-identity'
    save_c2_state(key, saved)
    runner._flow_finish_retry_at = 0
    with patch.object(api, 'settle_task_failure', wraps=api.settle_task_failure) as send:
        runner.tick_once()
        send.assert_not_called()
    assert load_c2_state(key)['task_failure_confirmed'] is False
    assert load_runtime_control()['inflight_flow_id'] == 'original-c1'
    assert runner.binding.run_status == 'faulted' and len(bridge.tasks) == 1


def test_environment_pause_survives_lost_failure_ack(runner_case):
    runner, api, bridge = runner_case
    bridge.result = RpaResult(ok=False, error_code='WECHAT_WINDOW_NOT_FOUND',
                             failure_step='wechat_window_found', message='Synthetic absent window')
    with patch.object(api, 'settle_task_failure', side_effect=TimeoutError('lost failure ack')):
        runner.tick_once()
    assert runner.binding.run_status == 'paused'
    runner._flow_finish_retry_at = 0
    runner.tick_once()
    assert not load_runtime_control()['inflight_flow_id']
    assert runner.binding.run_status == 'paused'
    assert len(bridge.tasks) == 1


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('code,initial,expected', [
    ('WECHAT_UI_LAYOUT_UNRESOLVED','running','faulted'),
    ('WECHAT_WINDOW_NOT_FOUND','running','paused'),
    ('WECHAT_WINDOW_NOT_FOUND','faulted','faulted'),
    ('PHONE_NOT_FOUND','running','running'),
])
def test_saved_failure_restores_stop_before_finish_even_if_confirmed(runner_case, confirmed, code, initial, expected):
    # Local persistence boundary; real crash+restart is covered by backend tests.
    from chejin_worker_client.storage import begin_runtime_flow, save_binding, load_binding
    runner, api, bridge = runner_case
    runner.binding.run_status=initial
    save_binding(runner.binding)
    begin_runtime_flow('original-c1','task')
    api.inflight_flow_id='original-c1'
    task=replace(api.task, lease_fencing_token=1)
    runner._save_add_friend_failure(runner.binding,task,RpaResult(
        ok=False,error_code=code,failure_step='synthetic',message='Original failure'))
    key=runner._inflight_finish_receipt_key(task.id)
    saved=load_c2_state(key)
    saved['task_failure_confirmed']=confirmed
    save_c2_state(key,saved)
    def assert_stop_before_post(binding,receipt):
        assert load_binding().run_status==expected
        assert binding.run_status==expected
        if expected != 'running': assert load_runtime_control()['pause_requested']
        return replace(task,status='failed',error_code=code,raw={'failure_receipt':receipt})
    with patch.object(api,'settle_task_failure',side_effect=assert_stop_before_post) as send:
        runner._retry_pending_flow_finish(runner.binding)
        assert send.call_count==(0 if confirmed else 1)
    assert load_binding().run_status==runner.binding.run_status==expected
    if expected != 'running': assert load_runtime_control()['pause_requested']
    assert not bridge.tasks


def test_confirmed_failure_can_finish_after_local_pointer_cleanup(runner_case):
    from chejin_worker_client.storage import begin_runtime_flow
    runner, api, bridge = runner_case
    begin_runtime_flow('original-c1','task')
    api.inflight_flow_id='original-c1'
    runner._save_add_friend_failure(runner.binding, replace(api.task,lease_fencing_token=1), bridge.result)
    runner._deliver_add_friend_failure(runner.binding,'original-c1')
    with patch.object(module,'clear_c2_state',side_effect=OSError('receipt cleanup interrupted')):
        with pytest.raises(OSError):
            runner._finish_inflight_flow(runner.binding,'original-c1',terminal_kind='task_terminal')
    assert not load_runtime_control()['inflight_flow_id']
    assert load_c2_state(runner._inflight_finish_receipt_key('original-c1'))['task_failure_confirmed']
    runner._flow_finish_retry_at=0
    runner._retry_pending_flow_finish(runner.binding)
    assert not load_c2_state(runner._inflight_finish_receipt_key('original-c1'))
    assert runner.binding.run_status=='faulted'
    assert not bridge.tasks
