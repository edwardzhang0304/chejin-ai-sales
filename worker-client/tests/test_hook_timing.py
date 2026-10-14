"""Timing is observable evidence, never a physical-action retry or gate."""
import json
import uuid
from unittest.mock import Mock
import pytest
from chejin_worker_client import hook_timing as timing
from test_hook_gateway import lab


@pytest.fixture
def captured(monkeypatch):
    events = []
    monkeypatch.setattr(timing.telemetry, 'allocate_stage_attempt', lambda *args: (1, str(uuid.uuid4())))
    monkeypatch.setattr(timing.telemetry, 'enqueue_stage_event', events.append)
    return events


def context():
    return {'process_run_id': str(uuid.uuid4()), 'conversation_id': 'conversation'}


def test_elapsed_uses_monotonic_and_backend_returned_identity(monkeypatch, captured):
    ticks = iter([100.0, 100.125])
    monkeypatch.setattr(timing.time, 'perf_counter', lambda: next(ticks))
    result = {**context(), 'private_body': '客户私有正文', 'token': 'private-token'}
    operation = Mock(return_value=result)
    assert timing.timed_call('hook.history_read', operation, result_context=True) is result
    assert operation.call_count == 1 and captured[0]['execution_duration_ms'] == 125
    assert captured[0]['process_run_id'] == result['process_run_id']
    encoded = json.dumps(captured, ensure_ascii=False)
    assert '客户私有正文' not in encoded and 'private-token' not in encoded


def test_failed_measurement_does_not_retry_or_cancel_operation(monkeypatch):
    monkeypatch.setattr(timing.telemetry, 'allocate_stage_attempt', Mock(side_effect=OSError('disk full')))
    operation = Mock(return_value={'completed': True})
    assert timing.timed_call('hook.send_call', operation, context=context()) == {'completed': True}
    operation.assert_called_once()


def test_operation_error_is_preserved_without_sensitive_error_text(captured):
    error = RuntimeError('private account/token/phone detail')
    operation = Mock(side_effect=error)
    with pytest.raises(RuntimeError) as raised:
        timing.timed_call('hook.send_call', operation, context=context())
    assert raised.value is error and operation.call_count == 1
    assert captured[0]['status'] == 'failed' and captured[0]['error_code'] == 'HOOK_OPERATION_FAILED'
    assert 'private account' not in json.dumps(captured)


@pytest.mark.parametrize('stage,result', [
    ('hook.voice_transcription', {'state': 'uncertain'}),
    ('hook.voice_transcription', {'state': 'failed', 'error_code': 'HOOK_VOICE_FAILED'}),
    ('hook.send_reconcile', {'local_send_observed': False}),
])
def test_unconfirmed_or_failed_results_do_not_measure_as_success(stage, result, captured):
    assert timing.timed_call(stage, lambda: result, context=context()) is result
    assert captured[0]['status'] == 'failed' and captured[0]['error_code']


def test_no_backend_identity_never_fabricates_a_process(monkeypatch):
    writer = Mock();monkeypatch.setattr(timing.telemetry, 'enqueue_stage_event', writer)
    assert timing.timed_call('hook.history_read', lambda: {'history_ready': False}, result_context=True) == {'history_ready': False}
    writer.assert_not_called()


def test_context_resolution_failure_cannot_change_business_return(captured):
    result = {'completed': True}
    resolver = Mock(side_effect=ValueError('unavailable diagnostics context'))
    assert timing.timed_call('hook.history_read', lambda: result, result_context=resolver) is result
    assert captured == []


def test_raw_ack_survives_diagnostic_disk_error(lab, monkeypatch):
    from test_hook_voice import persist
    from chejin_worker_client import storage
    monkeypatch.setattr(storage, 'append_log', Mock(side_effect=OSError('diagnostic failure')))
    persist(lab, metadata=True)
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0] == 1


def test_raw_timing_is_bound_to_one_original_packet_across_delivery_retries(lab, monkeypatch):
    from test_hook_voice import persist
    from chejin_worker_client import storage
    persist(lab, metadata=True)
    with storage.db_connection() as db:
        source = db.execute('SELECT event_uuid FROM hook_raw_inbox').fetchone()[0]
        original = db.execute('SELECT raw_digest FROM hook_raw_inbox').fetchone()[0]
    writer = Mock();monkeypatch.setattr(timing.telemetry, 'enqueue_existing_duration', writer)
    authority = {'raw_process_run_id': str(uuid.uuid4()), 'raw_source_event_uuid': source}
    timing.record_raw_delivery(authority);timing.record_raw_delivery(authority)
    assert writer.call_count == 2
    first, second = [call.kwargs for call in writer.call_args_list]
    assert first['stage_run_id'] == second['stage_run_id']
    assert first['conversation_id'] is None and first['execution_duration_ms'] >= 0
    with storage.db_connection() as db:
        assert db.execute('SELECT raw_digest FROM hook_raw_inbox').fetchone()[0] == original


def test_raw_packet_not_locally_recorded_cannot_fabricate_a_duration(monkeypatch, lab):
    writer = Mock();monkeypatch.setattr(timing.telemetry, 'enqueue_existing_duration', writer)
    timing.record_raw_delivery({'raw_process_run_id': str(uuid.uuid4()), 'raw_source_event_uuid': str(uuid.uuid4())})
    writer.assert_not_called()


def test_key_shaped_exception_is_not_exported_as_an_error_code(captured):
    marker = 'ABCDEF1234567890ABCDEF1234567890'
    with pytest.raises(RuntimeError):
        timing.timed_call('hook.send_call', Mock(side_effect=RuntimeError(marker)), context=context())
    assert captured[0]['error_code'] == 'HOOK_OPERATION_FAILED'
    assert marker not in json.dumps(captured)
