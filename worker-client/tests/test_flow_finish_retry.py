"""Worker persistence/concurrency boundaries; physical bridge and HTTP are stubs.

Real HTTP and PostgreSQL coverage lives in backend/test_flow_finish_recovery.py.
"""
import threading
from unittest.mock import patch

import pytest

import test_task_runner as runner_fixtures
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.storage import (
    begin_runtime_flow, load_c2_state, load_runtime_control, save_c2_state,
    request_runtime_pause,
)
import chejin_worker_client.task_runner as module


@pytest.fixture
def runner_pair():
    helper = runner_fixtures.TaskRunnerTest()
    helper.setUp()
    api = FakeApi(None)
    bridge = FakeBridge(RpaResult(ok=True, result_code='invite_sent'))
    runner, _ = helper.make_runner(api, bridge)
    runner.binding = Binding('worker', 'fake-token', 'instance', run_status='running')
    begin_runtime_flow('same-flow', 'task')
    api.inflight_flow_id = 'same-flow'
    try:
        yield runner, api
    finally:
        helper.tearDown()


@pytest.mark.parametrize('failure', ['save_before', 'save_after', 'local_cleanup', 'kv_cleanup'])
def test_local_finish_persistence_failure_retries_without_releasing_admission(runner_pair, failure):
    runner, api = runner_pair
    target = {'save_before':'save_c2_state', 'save_after':'save_c2_state',
              'local_cleanup':'finish_runtime_flow', 'kv_cleanup':'clear_c2_state'}[failure]
    original = getattr(module, target)
    injected = []
    def fail_once(*args, **kwargs):
        if not injected:
            injected.append(True)
            if failure == 'save_after': original(*args, **kwargs)
            raise OSError('synthetic persistence failure')
        return original(*args, **kwargs)
    with patch.object(module, target, side_effect=fail_once):
        with pytest.raises(OSError):
            runner._finish_inflight_flow(runner.binding, 'same-flow', terminal_kind='task_terminal')
        assert not runner._can_start_new_flow()
        assert runner.flow_finish_wait_reason
        if failure in {'save_before','save_after'}: assert not api.inflight_flow_events
        runner._flow_finish_retry_at = 0
        assert runner._retry_pending_flow_finish(runner.binding)
    assert injected == [True]
    assert not load_runtime_control()['inflight_flow_id']
    assert not runner.flow_finish_wait_reason
    assert runner._can_start_new_flow()
    assert not load_c2_state(runner._inflight_finish_receipt_key('same-flow'))


def test_two_loop_owners_retry_once_and_preserve_pause(runner_pair):
    runner, api = runner_pair
    api.finish_inflight_error = TimeoutError('one timeout')
    with pytest.raises(TimeoutError):
        runner._finish_inflight_flow(runner.binding, 'same-flow', terminal_kind='task_terminal')
    api.finish_inflight_error = None
    runner.binding.run_status = 'paused'
    request_runtime_pause()
    runner._flow_finish_retry_at = 0
    barrier = threading.Barrier(3)
    errors = []
    def retry():
        try:
            barrier.wait(timeout=3)
            runner._retry_pending_flow_finish(runner.binding)
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait(timeout=3)
    for thread in threads: thread.join(timeout=3)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert len(api.inflight_flow_events) == 2
    assert not load_runtime_control()['inflight_flow_id']
    assert load_runtime_control()['pause_requested']
    assert runner.binding.run_status == 'paused' and not runner._can_start_new_flow()


@pytest.mark.parametrize('damage', ['worker_id', 'client_instance_id', 'flow_id'])
def test_finish_intent_wrong_identity_never_calls_http(runner_pair, damage):
    runner, api = runner_pair
    request = {'worker_id':'worker', 'client_instance_id':'instance', 'flow_id':'same-flow',
        'terminal_kind':'task_terminal', 'conversation_id':None, 'error_code':None}
    request[damage] = 'different'
    save_c2_state(runner._inflight_finish_receipt_key('same-flow'), {'finish_request':request})
    with pytest.raises(RuntimeError, match='RECEIPT_MISMATCH'):
        runner._retry_pending_flow_finish(runner.binding)
    assert not api.inflight_flow_events
    assert load_runtime_control()['inflight_flow_id'] == 'same-flow'
    assert not runner._can_start_new_flow()


def test_active_owner_and_retry_interval_prevent_duplicate_attempts(runner_pair):
    runner, api = runner_pair
    api.finish_inflight_error = TimeoutError('still unavailable')
    with pytest.raises(TimeoutError):
        runner._finish_inflight_flow(runner.binding, 'same-flow', terminal_kind='task_terminal')
    for _ in range(10): runner._retry_pending_flow_finish(runner.binding)
    assert len(api.inflight_flow_events) == 1
    runner._flow_finish_retry_at = 0
    runner.current_task = object()
    runner._retry_pending_flow_finish(runner.binding)
    assert len(api.inflight_flow_events) == 1
    runner.current_task = None
    runner.current_ui_lock = object()
    runner._retry_pending_flow_finish(runner.binding)
    assert len(api.inflight_flow_events) == 1
    runner.current_ui_lock = None
    runner._retry_pending_flow_finish(runner.binding)
    assert len(api.inflight_flow_events) == 2
    assert not runner._can_start_new_flow()


@pytest.mark.parametrize('payload', [None, {}, {'finished':False,'flow_id':'same-flow'}, {'finished':True,'flow_id':'different'}])
def test_invalid_http_confirmation_preserves_local_flow(runner_pair, payload):
    from chejin_worker_client.api import WorkerApiClient, ApiError
    runner, _ = runner_pair
    api = WorkerApiClient('http://127.0.0.1/api')
    api.inflight_flow_id = 'same-flow'
    runner.api = api
    with patch.object(api, '_request', return_value=payload):
        with pytest.raises(ApiError, match='缺少同一流程'):
            runner._finish_inflight_flow(runner.binding,'same-flow',terminal_kind='task_terminal')
    assert api.inflight_flow_id == 'same-flow'
    assert load_runtime_control()['inflight_flow_id'] == 'same-flow'
    assert runner.flow_finish_wait_reason and not runner._can_start_new_flow()


def _media_facts_precede_pending_finish_under_stop(kind, status, *, save_failure=False, read_terminal=None):
    from chejin_worker_client.models import WechatReadTarget
    from chejin_worker_client.storage import save_c2_ledger_terminal, load_c2_ledger_entry, checkpoint_c2_action_outcomes
    helper = runner_fixtures.TaskRunnerTest(); helper.setUp()
    flow_id, conversation_id = 'pending-media-flow', 'pending-media-conversation'
    api = FakeApi(None)
    target = WechatReadTarget(conversation_id=conversation_id, rpa_session_key='wx:rpa:v1:pending-media',
        display_name='CJMEDIA1', remark_code='CJMEDIA1', read_reason='fact_settlement', authorization_revision='revision-media')
    api.read_targets = [target]
    if read_terminal:
        api.message_ingest_read_completion = {'result':read_terminal, 'error_code':'TEST_READ_GATE'}
    bridge = FakeBridge(RpaResult(ok=True,result_code='unused'))
    runner, _ = helper.make_runner(api,bridge)
    binding = Binding('worker-media','fake-token','instance-media',run_status='running');runner.binding=binding
    stable_id, action_id, observation_id = 'worker-message-304', 'media-action', 'media-post'
    begin_runtime_flow(flow_id,'c2_read')
    save_c2_state(runner._inflight_finish_receipt_key(flow_id), {'terminal_kind':'read_confirmed','conversation_id':conversation_id})
    if kind == 'image_ledger':
        observation, source_key = runner_fixtures.committed_image_observation(conversation_id=conversation_id,
            observation_id=observation_id, stable_id=stable_id, action_id=action_id, content='人工车辆图片事实')
        save_c2_ledger_terminal(conversation_id=conversation_id,source_message_key=source_key,
            origin_read_run_id=flow_id,dedupe_key='media-dedupe',message_type='image',terminal_state='completed',
            ingest_state='waiting',result={'state':'completed','reason':'vision_ready',
                'authorization_revision':target.authorization_revision,'replayable_observation':observation})
    elif kind == 'voice_journal':
        runner_fixtures.initialize_confirmed_voice_recovery_journal(conversation_id=conversation_id,flow_id=flow_id,
            action_id=action_id,stable_id=stable_id,observation_id=observation_id,content='人工语音事实')
    else:
        observation, source_key = runner_fixtures.committed_voice_recovery_observation(conversation_id,
            stable_id=stable_id,observation_id=observation_id,action_id=action_id,content='人工语音事实')
        checkpoint_c2_action_outcomes(flow_id='media-action-flow',conversation_id=conversation_id,origin_read_run_id=flow_id,
            outcomes=[{'source_message_key':source_key,'origin_read_run_id':flow_id,'result':'completed',
                'evidence':{'action_kind':'voice','sender_role':'customer'},'terminal_payload':{'state':'completed',
                'transcribed_message':{'content_clean':observation['content_clean'],'sender_role':'customer',
                    'parent_voice_anchor_key':observation['parent_voice_anchor_key']},'replayable_observation':observation}}])
    api.inflight_flow_id = flow_id
    api.inflight_flow_state = {'status':'active','flow_id':flow_id,'flow_kind':'c2_read','conversation_id':conversation_id}
    try:
        if save_failure:
            with patch.object(module,'save_c2_state',side_effect=OSError('synthetic intent save failure')):
                with pytest.raises(OSError):
                    runner._finish_inflight_flow(binding,flow_id,terminal_kind='read_confirmed',conversation_id=conversation_id)
        else:
            with pytest.raises(RuntimeError,match='PENDING'):
                runner._finish_inflight_flow(binding,flow_id,terminal_kind='read_confirmed',conversation_id=conversation_id)
        assert not api.inflight_flow_events and runner._flow_finish_stage=='dependencies'
        runner.set_run_status(status)
        with patch.object(runner,'_execute_one_image_slot_vision',side_effect=AssertionError('must not run Vision')):
            for _ in range(5):
                runner._flow_finish_retry_at=0  # Local fake-clock boundary only; HTTP probes use real time.
                runner.tick_once()
                if not load_runtime_control()['inflight_flow_id']:break
        assert not load_runtime_control()['inflight_flow_id']
        assert len(api.message_payloads)==1
        assert len(api.inflight_flow_events)==1
        expected_terminal=read_terminal or 'read_confirmed'
        assert f':{expected_terminal}:' in api.inflight_flow_events[0]
        assert binding.run_status==status and load_runtime_control()['pause_requested']
        assert 'pull' not in api.events
        assert not bridge.locate_chats and not bridge.message_reads and not bridge.voice_transcribes
    finally: helper.tearDown()


@pytest.mark.parametrize('kind', ['image_ledger', 'voice_journal', 'sqlite_journal'])
@pytest.mark.parametrize('status', ['paused', 'faulted'])
def test_media_facts_precede_pending_finish_under_stop(kind, status):
    _media_facts_precede_pending_finish_under_stop(kind, status)


@pytest.mark.parametrize('save_failure', [False, True])
def test_dependency_recovery_retains_new_ingest_terminal_after_intent_save_failure(save_failure):
    _media_facts_precede_pending_finish_under_stop('image_ledger', 'paused',
        save_failure=save_failure, read_terminal='retry_required')
