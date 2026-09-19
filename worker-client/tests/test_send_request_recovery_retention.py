"""Focused independent counterexamples; no product source edits or desktop I/O."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace, MethodType
import hashlib
import io
import zipfile

import pytest

from test_send_request_file import original, material, TEXT
from test_pre_send_read_recovery_budget import failure
from chejin_worker_client import storage, rpa_bridge, send_request_evidence, send_setup_recovery, pre_send_read_recovery
from chejin_worker_client.task_runner import TaskRunner
from apps.wechat_ai_customer_service.adapters import send_launch_journal as launches
from apps.wechat_ai_customer_service.adapters import send_request_file as files


def test_package_reference_survives_failure_immediately_after_atomic_write(original, monkeypatch):
    bridge, journal_path, context = original
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='local-test-only', reply_text_hash=context['reply_text_hash'],
        intent_evidence={'pre_send_setup_context': context})
    native = launches.update
    def fail_only_recording_prepared(*args, **kwargs):
        if kwargs.get('process_state') == 'prepared':
            raise OSError('controlled journal write failure after request file was committed')
        return native(*args, **kwargs)
    monkeypatch.setattr(launches, 'update', fail_only_recording_prepared)
    with pytest.raises(OSError, match='after request file'):
        bridge.send_reply(target='CJTEST01', rpa_session_key='', text=TEXT,
            task_id=context['task_id'], reply_action_id=context['reply_action_id'], expected_context_guard={'history': ['original']})
    committed = list((rpa_bridge.CONFIG.app_dir / 'ipc').glob('*.json'))
    assert len(committed) == 1
    assert launches.read(journal_path)[0]['send_launch_attempts'][-1]['process_state'] == 'preparing'
    runner = SimpleNamespace(bridge=bridge, current_ui_lock=None,
        binding=SimpleNamespace(run_status='faulted'), _pending_run_status_sync=None,
        set_run_status=lambda value: True)
    runner._reply_send_ack_payload = MethodType(TaskRunner._reply_send_ack_payload, runner)
    assert send_setup_recovery.prepare_receipt_recovery(runner, runner.binding)
    recovered = storage.load_reply_send_ack_outbox(context['reply_action_id'])
    assert recovered['ack_payload']['evidence']['pre_send_setup_failure']['process_state'] == 'not_called'
    refs = recovered['ack_payload']['evidence']['send_request_files']
    # This is a unit-level retention check, not a claim of a real server ACK.
    # Model the normal post-ACK state only after the real recovery adapter ran.
    storage.mark_reply_send_ack_confirmed(context['reply_action_id'])
    journal_path.unlink()
    runner.binding.run_status = 'running'
    deleted = send_request_evidence.cleanup(runner, now=datetime.now(timezone.utc) + timedelta(days=60))
    assert refs and deleted == 1 and not committed[0].exists(), {
        'recovered_references': refs, 'deleted_count': deleted, 'orphan_exists': committed[0].exists()}


def test_existing_ocr_receipt_recovery_preserves_committed_package_reference(original):
    """Arrange the durable two-read failure state; exercise actual receipt recovery."""
    bridge, journal_path, context = original
    args, ref, raw, attempt = material(original)
    second = failure(identity='second-capture')
    launches.update(journal_path, attempt['launch_attempt_id'], allowed={'creating'},
        process_state='finished', action_phase='not_attempted', physical_send_triggered=False,
        read_failure_fact=second)
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='local-test-only', reply_text_hash=context['reply_text_hash'],
        intent_evidence={'pre_send_setup_context': context})
    allowed, record = pre_send_read_recovery.reserve(context, failure(identity='first-capture'), target='CJTEST01')
    assert allowed
    record = pre_send_read_recovery.complete_attempt(context['reply_action_id'], failure=second)
    proof = pre_send_read_recovery.terminal_proof(record, phase_proof=second['phase_proof'], input_state='unverified')
    pre_send_read_recovery.save_settlement(record, proof=proof, request={'receipt_kind': 'sent_ack'})
    runner = SimpleNamespace(bridge=bridge, current_ui_lock=None, current_task=None,
        binding=SimpleNamespace(run_status='faulted'), set_run_status=lambda value: True)
    runner._reply_send_ack_payload = MethodType(TaskRunner._reply_send_ack_payload, runner)
    assert send_setup_recovery.prepare_receipt_recovery(runner, runner.binding)
    assert pre_send_read_recovery.prepare_receipt_recovery(runner, runner.binding)
    recovered = storage.load_reply_send_ack_outbox(context['reply_action_id'])
    assert 'pre_send_read_failure' in recovered['ack_payload']['evidence']
    assert recovered['ack_payload']['evidence'].get('send_request_files') == [ref], {
        'evidence_keys': list(recovered['ack_payload']['evidence']),
        'request_still_exists': Path(ref['path']).exists(),
        'journal_will_be_deleted_after_ack': True}


@pytest.mark.parametrize('phase', ['confirmed', 'trigger_attempted'])
def test_original_ocr_recovery_retains_files_for_sent_or_unknown(original, phase):
    bridge, path, context = original
    args, ref, raw, attempt = material(original)
    second = failure(identity='second-capture')
    launches.update(path, attempt['launch_attempt_id'], allowed={'creating'}, process_state='finished',
        read_failure_fact=second, action_phase='not_attempted', physical_send_triggered=False)
    journal, _ = launches.read(path)
    journal['action_phase'] = phase
    for item in journal['items'].values(): item['action_phase'] = phase
    launches._write(path, journal)
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='test-only', reply_text_hash=context['reply_text_hash'], intent_evidence={'pre_send_setup_context': context})
    allowed, record = pre_send_read_recovery.reserve(context, failure(identity='first-capture'), target='CJTEST01')
    assert allowed
    runner = SimpleNamespace(bridge=bridge, current_ui_lock=None, current_task=None,
        binding=SimpleNamespace(run_status='faulted'), set_run_status=lambda _: True)
    runner._reply_send_ack_payload = MethodType(TaskRunner._reply_send_ack_payload, runner)
    assert send_setup_recovery.prepare_receipt_recovery(runner, runner.binding)
    assert pre_send_read_recovery.prepare_receipt_recovery(runner, runner.binding)
    ack = storage.load_reply_send_ack_outbox(context['reply_action_id'])
    assert ack['ack_payload']['send_result'] == ('sent' if phase == 'confirmed' else 'unknown')
    assert ack['ack_payload']['evidence']['send_request_files'] == [ref]


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'unknown'])
def test_all_finalization_paths_keep_refs_before_journal_removal(original, outcome):
    bridge, path, context = original
    args, ref, raw, attempt = material(original)
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='test-only', reply_text_hash=context['reply_text_hash'])
    body = {'send_result': outcome, 'action_phase': 'confirmed' if outcome == 'sent' else 'trigger_attempted',
            'evidence': {'unchanged_proof': {'original': True}}}
    storage.finalize_reply_send_ack(reply_action_id=context['reply_action_id'], ack_payload=body)
    ack = storage.load_reply_send_ack_outbox(context['reply_action_id'])
    assert ack['ack_payload']['evidence']['send_request_files'] == [ref]
    assert body['evidence'] == {'unchanged_proof': {'original': True}}  # No mutation of the caller's frozen proof.
    path.unlink()
    # A second durable finalization retains the original reference even with no journal.
    storage.finalize_reply_send_ack(reply_action_id=context['reply_action_id'], ack_payload=body)
    assert storage.load_reply_send_ack_outbox(context['reply_action_id'])['ack_payload'] == ack['ack_payload']
    with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
        omissions = []
        entries = send_request_evidence.export_files(archive, secrets=set(), max_bytes=100000, omissions=omissions)
        assert archive.read('ipc/'+Path(ref['path']).name) == raw
        assert len(entries) == 1 and not omissions


def test_reference_is_durable_before_temporary_file_can_survive_crash(original, monkeypatch):
    bridge, path, context = original
    class Crash(BaseException): pass
    def crash_before_rename(source, target):
        refs = launches.references(path)
        assert len(refs) == 1 and refs[0]['temporary_path'] == str(source)
        assert refs[0]['path'] == str(target)
        assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == refs[0]['sha256']
        raise Crash()
    monkeypatch.setattr(files.os, 'rename', crash_before_rename)
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='test-only', reply_text_hash=context['reply_text_hash'], intent_evidence={'pre_send_setup_context': context})
    with pytest.raises(Crash):
        bridge.send_reply(target='CJTEST01', rpa_session_key='', text=TEXT, task_id=context['task_id'],
            reply_action_id=context['reply_action_id'], expected_context_guard={'history': []})
    ref = launches.references(path)[0]
    runner = SimpleNamespace(bridge=bridge, current_ui_lock=None, _pending_run_status_sync=None,
        binding=SimpleNamespace(run_status='faulted'), set_run_status=lambda _: True)
    runner._reply_send_ack_payload = MethodType(TaskRunner._reply_send_ack_payload, runner)
    assert send_setup_recovery.prepare_receipt_recovery(runner, runner.binding)
    ack = storage.load_reply_send_ack_outbox(context['reply_action_id'])
    assert ack['ack_payload']['evidence']['send_request_files'] == [ref]
    storage.mark_reply_send_ack_confirmed(context['reply_action_id'])  # Unit retention model, not HTTP evidence.
    path.unlink()
    runner.binding.run_status = 'running'
    assert send_request_evidence.cleanup(runner, now=datetime.now(timezone.utc)+timedelta(days=60)) == 1
    assert not Path(ref['temporary_path']).exists()


def test_reference_save_failure_cannot_leave_an_unregistered_file(original, monkeypatch):
    bridge, path, context = original
    def reject(*args, **kwargs): raise OSError('controlled reference persistence failure')
    monkeypatch.setattr(launches, 'record_request', reject)
    monkeypatch.setattr(bridge, '_call_omniauto', lambda *a, **k: pytest.fail('process must not be started'))
    result = bridge.send_reply(target='CJTEST01', rpa_session_key='', text=TEXT, task_id=context['task_id'],
        reply_action_id=context['reply_action_id'], expected_context_guard={'history': []})
    assert result['pre_send_setup_failure']['process_state'] == 'not_called'
    assert not list((rpa_bridge.CONFIG.app_dir/'ipc').iterdir())


def test_modified_file_is_retained_and_cleanup_failure_does_not_raise(original):
    bridge, path, context = original
    args, ref, raw, attempt = material(original)
    storage.save_reply_send_intent(reply_action_id=context['reply_action_id'], task_id=context['task_id'],
        send_token='test-only', reply_text_hash=context['reply_text_hash'])
    storage.finalize_reply_send_ack(reply_action_id=context['reply_action_id'], ack_payload={'send_result': 'failed'})
    storage.mark_reply_send_ack_confirmed(context['reply_action_id'])
    Path(ref['path']).write_bytes(b'not the original bytes')
    runner = SimpleNamespace(bridge=bridge, binding=SimpleNamespace(run_status='running'), _pending_run_status_sync=None)
    assert send_request_evidence.cleanup(runner, now=datetime.now(timezone.utc)+timedelta(days=60)) == 0
    assert Path(ref['path']).exists()
    assert send_request_evidence.retains_files(storage.load_reply_send_ack_outbox(context['reply_action_id']))
