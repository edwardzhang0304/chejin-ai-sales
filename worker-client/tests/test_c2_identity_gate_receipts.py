"""Synthetic external API/UI fixtures; real Worker comparison/outbox/SQLite."""
import pytest
from test_task_runner import (FakeApi, FakeBridge,
                              identity_checkpoint_for_facts)
from chejin_worker_client.models import Binding, WechatReadTarget, RpaResult
from chejin_worker_client.storage import load_runtime_control, load_c2_state, read_logs


@pytest.fixture
def harness():
    from test_task_runner import TaskRunnerTest
    case = TaskRunnerTest()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.mark.parametrize('read_result,error,terminal', [
    ('retry_required', 'C2_UNREAD_RESULT_INCONCLUSIVE', 'retry_required'),
    ('technical_failed', 'C2_UNREAD_RESULT_REPEATEDLY_INCONCLUSIVE', 'technical_failed'),
    ('new_facts', None, 'read_confirmed'),
])
def test_identity_gate_preserves_actual_backend_terminal(harness, read_result, error, terminal):
    api = FakeApi(None)
    api.message_ingest_read_completion = {'result': read_result, 'error_code': error}
    bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
    bridge.get_messages_payloads = [{'messages': [{'id': 'now', 'type': 'text', 'sender_role': 'customer', 'content': '完全不同的新画面'}]}]
    runner, _ = harness.make_runner(api, bridge)
    binding = Binding('worker-test', 'test-token', 'instance-test', run_status='running')
    target = WechatReadTarget(conversation_id='conv-gate', display_name='CJTEST01', remark_code='CJTEST01',
        rpa_session_key='test-session', authorization_revision='revision-conv-gate', unread_generation=1,
        raw={'identity_checkpoint': identity_checkpoint_for_facts('conv-gate', [{'content': '已读旧画面'}])})
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=False, wait_for_brain=False)
    assert result['error_code'] == 'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS', result
    assert result['result']['read_completion']['result'] == read_result
    assert len(api.message_payloads) == 1 and api.message_payloads[0]['messages'] == []
    assert any(':'+terminal+':' in x for x in api.inflight_flow_events), api.inflight_flow_events
    assert not load_runtime_control()['inflight_flow_id']
    assert not any(x['event'] == 'inflight_flow_finish_failed' for x in read_logs(limit=100))


def test_confirmed_gate_replay_reuses_durable_retry_receipt(harness):
    api = FakeApi(None)
    api.message_ingest_read_completion = {'result': 'retry_required', 'error_code': 'C2_UNREAD_RESULT_INCONCLUSIVE'}
    runner, _ = harness.make_runner(api, FakeBridge(RpaResult(ok=True, result_code='unused')))
    binding = Binding('worker-test', 'test-token', 'instance-test', run_status='running')
    target = WechatReadTarget(conversation_id='conv-gate', display_name='CJTEST01', remark_code='CJTEST01',
                             rpa_session_key='test-session', authorization_revision='revision-conv-gate')
    args = dict(binding=binding, target=target, read_run_id='same-gate',
                error_code='MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS', identity_errors=[])
    first = runner._report_identity_failure_gate(**args)
    second = runner._report_identity_failure_gate(**args)
    assert first['ok'] and second['ok']
    assert second['result']['read_completion'] == first['result']['read_completion']
    assert len(api.message_payloads) == 1
    receipt = load_c2_state(runner._inflight_finish_receipt_key('same-gate'))
    assert receipt['terminal_kind'] == 'retry_required'


def test_failed_gate_delivery_keeps_outbox_and_is_not_reported_success(harness):
    from chejin_worker_client.storage import has_pending_c2_outbox
    api = FakeApi(None)
    api.message_ingest_error = ConnectionError('synthetic offline boundary')
    runner, _ = harness.make_runner(api, FakeBridge(RpaResult(ok=True, result_code='unused')))
    binding = Binding('worker-test', 'test-token', 'instance-test', run_status='running')
    target = WechatReadTarget(conversation_id='conv-gate', display_name='CJTEST01', remark_code='CJTEST01',
                             rpa_session_key='test-session', authorization_revision='revision-conv-gate')
    delivery = runner._report_identity_failure_gate(binding=binding, target=target, read_run_id='offline-gate',
                error_code='MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS', identity_errors=[])
    assert delivery['ok'] is False
    assert has_pending_c2_outbox()
    assert not load_c2_state(runner._inflight_finish_receipt_key('offline-gate'))


@pytest.mark.parametrize('defect', ['other_flow', 'other_customer', 'no_completion_time', 'no_retry_time', 'wrong_error', 'unknown_result', 'technical_result', 'network'])
def test_legacy_receipt_is_not_rewritten_without_exact_backend_proof(harness, defect):
    from chejin_worker_client.storage import save_c2_state
    runner, _ = harness.make_runner(FakeApi(None), FakeBridge(RpaResult(ok=True, result_code='unused')))
    binding = Binding('worker-test', 'test-token', 'instance-test', run_status='paused')
    key=runner._inflight_finish_receipt_key('old-flow')
    original={'terminal_kind':'read_confirmed','conversation_id':'conv-gate','error_code':None}
    save_c2_state(key,original)
    completion={'read_run_id':'old-flow','result':'retry_required','error_code':'C2_UNREAD_RESULT_INCONCLUSIVE',
                'completed_at':'2026-09-09T07:00:00Z','next_read_due_at':'2026-09-09T07:00:05Z'}
    snapshot={'conversation_id':'conv-gate','allowed':False,'read_completion':completion}
    if defect=='other_flow': completion['read_run_id']='other-flow'
    elif defect=='other_customer': snapshot['conversation_id']='other-customer'
    elif defect=='no_completion_time': completion.pop('completed_at')
    elif defect=='no_retry_time': completion.pop('next_read_due_at')
    elif defect=='wrong_error': completion['error_code']='UNRELATED_ERROR'
    elif defect=='unknown_result': completion['result']='unknown'
    elif defect=='technical_result': completion.update(result='technical_failed',error_code='C2_UNREAD_RESULT_REPEATEDLY_INCONCLUSIVE')
    def read(*args,**kwargs):
        if defect=='network': raise ConnectionError('controlled offline')
        return snapshot
    runner.api.get_wechat_read_authorization=read
    if defect=='network':
        with pytest.raises(ConnectionError): runner._refresh_rejected_read_finish_receipt(binding,flow_id='old-flow',conversation_id='conv-gate')
    else:
        assert runner._refresh_rejected_read_finish_receipt(binding,flow_id='old-flow',conversation_id='conv-gate') is None
    assert load_c2_state(key)==original
