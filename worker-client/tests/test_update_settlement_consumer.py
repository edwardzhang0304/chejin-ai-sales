"""Real SQLite/outbox consumer; update intent never creates another action."""
import pytest

from chejin_worker_client import storage
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests, trigger_emergency_stop
from test_c2_identity_gate_receipts import harness
from test_gate_only_outbox import setup_gate, ledger_rows


@pytest.mark.parametrize('offline', [False, True])
def test_update_tick_delivers_original_ocr_outbox_without_new_checks(harness, offline):
    runner, api, bridge, binding, payload = setup_gate(harness)
    api.inflight_flow_id = None
    api.task_lease_fencing_tokens = {}
    runner._backend_confirmed_run_status = 'faulted'
    outbox = storage.enqueue_c2_outbox(payload)
    before = ledger_rows()
    if offline:
        api.message_ingest_error = ConnectionError('Synthetic backend offline')
    runner.begin_update_check(lambda: {'update_request_id': 'test-settlement'})
    runner._tick_once = lambda: pytest.fail('New background checks during update')
    runner.tick_once()
    saved = storage.load_c2_outbox_entry(outbox)
    assert (saved['status'] == 'confirmed') is not offline
    assert saved['payload'] == payload
    assert ledger_rows() == before
    assert not bridge.message_reads and not bridge.sent_replies
    assert storage.load_binding().run_status == 'faulted'
    assert storage.load_runtime_control()['update_no_new_work']
    if offline:
        assert not runner.update_install_safety_snapshot()['safe']


@pytest.mark.parametrize('defect', [None, 'other_flow', 'api_flow', 'backend_flow',
    'terminal', 'faulted', 'stopping', 'emergency', 'new_process', 'no_gate', 'new_pause'])
def test_original_flow_permission_requires_all_existing_identities(harness, defect):
    runner, api, _, _, _ = setup_gate(harness)
    reset_emergency_stop_for_tests()
    runner._apply_local_run_status('running')
    storage.begin_runtime_flow('original-flow', 'chat_reply')
    api.inflight_flow_id = 'original-flow'
    runner._backend_inflight_flow_state = {'flow_id': 'original-flow', 'status': 'draining'}
    runner.begin_update_check(lambda: {'update_request_id': 'test-original'})
    flow = 'original-flow'
    if defect == 'other_flow': flow = 'another-flow'
    elif defect == 'api_flow': api.inflight_flow_id = 'another-flow'
    elif defect == 'backend_flow': runner._backend_inflight_flow_state['flow_id'] = 'another-flow'
    elif defect == 'terminal': runner._backend_inflight_flow_state['status'] = 'finished'
    elif defect == 'faulted': runner._apply_local_run_status('faulted')
    elif defect == 'stopping': runner.stop_event.set()
    elif defect == 'emergency': trigger_emergency_stop(reason='synthetic', origin='test')
    elif defect == 'new_process': runner._update_quiescing_request_id = None
    elif defect == 'no_gate': storage.set_update_new_work_gate(False)
    elif defect == 'new_pause': runner._apply_local_run_status('paused')
    try:
        assert runner.update_flow_continuation_allowed(flow) is (defect is None)
        if defect is None:
            assert runner.update_read_retry_revision() == runner._update_original_revision
        elif defect == 'new_pause':
            assert runner.update_read_retry_revision() == runner._run_status_revision
            assert runner.update_read_retry_revision() != runner._update_original_revision
        assert not runner._can_start_new_flow()
    finally:
        reset_emergency_stop_for_tests()
