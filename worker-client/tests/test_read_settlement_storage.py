"""SQLite proof-consumer unit boundaries; HTTP/next-task tests live in backend/tests."""
import copy

import pytest

from chejin_worker_client import storage
from chejin_worker_client.models import Binding
from chejin_worker_client.shared_rules import read_settlement


@pytest.fixture
def original(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    binding = Binding('worker', 'token', 'instance', run_status='faulted')
    bound_at = '2026-09-14T00:00:00+00:00'
    payload = {'read_run_id': 'read-original', 'conversation_id': 'customer',
        'authorization_revision': 'revoked-original', 'contract_revision': '0.9.78',
        'contract_sha256': 'a' * 64, 'messages': [
            {'source_message_key': key, 'dedupe_key': key, 'message_type': 'text', 'content': key}
            for key in ['accepted', 'cancelled']]}
    for key, state in [('accepted', 'confirmed'), ('cancelled', 'waiting')]:
        storage.save_c2_ledger_terminal(conversation_id='customer', source_message_key=key,
            origin_read_run_id='read-original', dedupe_key=key, message_type='text',
            terminal_state='completed', ingest_state=state, result={'original': key})
    outbox = storage.enqueue_c2_outbox(payload)
    identity = read_settlement.partition_identity(payload)
    identity.pop('expected_source_message_keys')
    result = {'recovery_action': 'conversation_terminated', 'accepted_source_message_keys': ['accepted'],
        'results': [{'source_message_key': 'accepted', 'ingest_result': 'duplicated', 'message_event_id': 'original-server-message'}],
        'recovery_settlement': {**identity, 'protocol_version': 1, 'proof_id': 'proof-original',
            'disposition': 'business_cancelled', 'reason_code': 'LEAD_INVALID', 'worker_id': 'worker',
            'client_instance_id': 'instance', 'bound_at': bound_at, 'settled_at': bound_at,
            'source_message_keys': ['cancelled']}}
    return binding, bound_at, payload, outbox, result


@pytest.mark.parametrize('damage', ['worker_id', 'client_instance_id', 'bound_at', 'flow_id', 'conversation_id',
    'authorization_revision', 'contract_sha256', 'payload_sha256', 'partition_index', 'proof_id', 'coverage', 'accepted'])
def test_invalid_proof_never_changes_waiting_records(original, damage):
    binding, bound_at, payload, outbox, result = original
    before = storage.load_c2_ledger_entry('customer', 'cancelled')
    corrupted = copy.deepcopy(result)
    if damage == 'coverage': corrupted['recovery_settlement']['source_message_keys'] = ['invented']
    elif damage == 'accepted': corrupted['results'] = []
    else: corrupted['recovery_settlement'][damage] = '' if damage == 'proof_id' else 'different'
    with pytest.raises((ValueError, TypeError)):
        storage.settle_c2_outbox(outbox, corrupted, binding, server_bound_at=bound_at)
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'waiting'
    assert storage.load_c2_outbox_entry(outbox)['payload'] == payload
    assert storage.load_c2_ledger_entry('customer', 'cancelled') == before
    assert storage.load_c2_state('read_settlement:' + outbox) == {}
    assert storage.has_pending_c2_outbox()


@pytest.mark.parametrize('active_flow', ['read-original', 'unrelated', None])
def test_mixed_settlement_preserves_confirmed_facts_and_only_owns_matching_receipt(original, active_flow):
    binding, bound_at, payload, outbox, result = original
    confirmed = storage.load_c2_ledger_entry('customer', 'accepted')
    if active_flow:
        storage.begin_runtime_flow(active_flow, 'c2_read')
        storage.save_c2_state('inflight_finish_receipt:' + active_flow, {
            'conversation_id': 'customer' if active_flow == 'read-original' else 'other-customer',
            'terminal_kind': 'technical_failed', 'error_code': 'ORIGINAL_ERROR'})
    storage.settle_c2_outbox(outbox, result, binding, server_bound_at=bound_at)
    storage.settle_c2_outbox(outbox, result, binding, server_bound_at=bound_at)
    assert storage.load_c2_ledger_entry('customer', 'accepted') == confirmed
    cancelled = storage.load_c2_ledger_entry('customer', 'cancelled')
    assert (cancelled['terminal_state'], cancelled['ingest_state']) == ('failed', 'not_required')
    assert storage.load_c2_outbox_entry(outbox)['payload'] == payload
    assert not storage.has_pending_c2_outbox()
    assert storage.load_runtime_control()['inflight_flow_id'] == active_flow
    if active_flow:
        receipt = storage.load_c2_state('inflight_finish_receipt:' + active_flow)
        assert receipt['terminal_kind'] == ('read_cancelled' if active_flow == 'read-original' else 'technical_failed')


def test_corrupted_saved_proof_cannot_make_the_shared_barrier_pass(original):
    binding, bound_at, _, outbox, result = original
    storage.settle_c2_outbox(outbox, result, binding, server_bound_at=bound_at)
    damaged = copy.deepcopy(result)
    damaged['recovery_settlement'].pop('worker_id')
    storage.save_c2_state('read_settlement:' + outbox, damaged)
    assert storage.has_pending_c2_outbox()
    assert storage.update_install_business_blockers()['pending_c2_outbox'] == 1
