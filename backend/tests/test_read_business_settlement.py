"""Original requests through HTTP and business invalidation; no DB repair."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.c3 import ReplyAction
from app.models.wechat import MessageEvent
from app.models.worker import Worker
from app.models.task import Task
from app.contracts.shared_rules import shared_adapter
from test_contract_equivalent_recovery import prepared_read, post
from test_lead_followup_eligibility import client, isolated_db, http_api, headers
from test_contract_equivalent_recovery import use_contract


def invalidate(row):
    response = client.post('/api/leads/' + row['lead_id'] + '/mark-invalid', json={'invalid_reason': 'test_data'})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('flow_state', ['active', 'draining', 'ended'])
def test_valid_original_read_recovery_automatically_creates_a_reply(flow_state):
    worker, row, payload = prepared_read(closed=flow_state == 'ended', eligible=True)
    if flow_state == 'draining':
        response = client.post(f"/api/workers/{worker['id']}/run-status", headers=headers(worker),
                               json={'client_instance_id': 'followup-test', 'run_status': 'faulted'})
        assert response.status_code == 200, response.text
    response = post(worker, payload)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 4
        assert db.scalar(select(func.count(ReplyAction.id))) == 1, response.text
        generated = db.scalar(select(ReplyAction))
        assert generated.conversation_id == row['conversation_id']
    # The natural result is asserted before this idempotency probe.
    repeated = post(worker, payload)
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(ReplyAction.id))) == 1


@pytest.mark.parametrize('closed', [False, True])
def test_original_revoked_read_settles_without_message_or_reply(closed):
    worker, row, payload = prepared_read(closed=closed)
    original = copy.deepcopy(payload)
    invalidate(row)
    first = post(worker, payload)
    assert first.status_code == 200, first.text
    data = first.json()['data']
    assert data['recovery_action'] == 'conversation_terminated'
    assert data['accepted_source_message_keys'] == []
    assert 'message_batch' not in data and 'read_completion' not in data
    from app.api.routes.wechat import _ingest_telemetry_terminal
    assert _ingest_telemetry_terminal(data) == ('cancelled', 'LEAD_INVALID')
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        shared_adapter('read_settlement').validate_settlement(payload, data, worker_id=owner.id,
            client_instance_id=owner.client_instance_id, bound_at=owner.bound_at.isoformat())
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id)).where(
            OperationLog.event_type == 'worker_read_business_settled')) == 1
        assert bool(owner.inflight_flow_state) is not closed
    repeated = post(worker, payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['data'] == data
    assert payload == original
    changed = copy.deepcopy(payload)
    changed['messages'][0]['content'] = 'changed original'
    rejected = post(worker, changed)
    assert rejected.status_code == 409, rejected.text


@pytest.mark.parametrize('first_operation', ['invalidate', 'ingest'])
def test_invalidation_and_ingest_are_ordered_by_the_same_business_lock(http_api, monkeypatch, first_operation):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from app.services import followup_eligibility
    worker, row, payload = prepared_read(closed=False)
    entered, release = threading.Event(), threading.Event()
    original_lock = followup_eligibility.lock_leads

    def hold_first_lock(db, ids):
        result = original_lock(db, ids)
        if row['lead_id'] in result and not entered.is_set():
            entered.set()
            assert release.wait(5), 'Controlled lock barrier timed out'
        return result

    monkeypatch.setattr(followup_eligibility, 'lock_leads', hold_first_lock)
    def request(operation):
        if operation == 'invalidate':
            return http_api.post('/api/leads/' + row['lead_id'] + '/mark-invalid', json={'invalid_reason': 'test_data'})
        return http_api.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload,
                             headers=headers(worker, payload['read_run_id']))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, first_operation)
        assert entered.wait(5)
        second = pool.submit(request, 'ingest' if first_operation == 'invalidate' else 'invalidate')
        try:
            time.sleep(.2)
            assert not second.done(), 'Conflicting business request must wait for the real PostgreSQL lock'
        finally:
            release.set()
        responses = [first.result(timeout=8), second.result(timeout=8)]
    assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
    settled = post(worker, payload)
    assert settled.status_code == 200, settled.text
    result = settled.json()['data']
    accepted = 4 if first_operation == 'ingest' else 0
    assert len(result['accepted_source_message_keys']) == accepted
    assert len(result['recovery_settlement']['source_message_keys']) == 4 - accepted
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == accepted
        assert not db.scalar(select(ReplyAction.id).where(ReplyAction.status.in_(['sending', 'sent'])))


def test_committed_settlement_replay_preserves_a_different_current_flow_after_restore():
    worker, row, payload = prepared_read(closed=True)
    invalidate(row)
    first = post(worker, payload)
    assert first.status_code == 200, first.text
    assert client.post('/api/leads/' + row['lead_id'] + '/restore').status_code == 200
    path = f"/api/workers/{worker['id']}"
    resumed = client.post(path + '/run-status', headers=headers(worker), json={
        'client_instance_id': 'followup-test', 'run_status': 'running', 'recover_from_fault': True})
    assert resumed.status_code == 200, resumed.text
    targets = client.get(path + '/wechat/sessions/read-targets', headers=headers(worker))
    assert targets.status_code == 200, targets.text
    target = next(item for item in targets.json()['data']['targets'] if item['conversation_id'] != row['conversation_id'])
    started = client.post(path + '/inflight-flow/start', headers=headers(worker), json={
        'flow_id': 'different-current-flow', 'flow_kind': 'c2_read',
        'conversation_id': target['conversation_id'], 'unread_generation': target['unread_generation'],
        'authorization_revision': target['authorization_revision']})
    assert started.status_code == 200, started.text
    with SessionLocal() as db:
        other = dict(db.get(Worker, worker['id']).inflight_flow_state)
    repeated = post(worker, payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['data'] == first.json()['data']
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).inflight_flow_state == other
        assert db.scalar(select(func.count(MessageEvent.id))) == 0


def test_fault_settlement_is_local_to_its_worker():
    worker, row, payload = prepared_read(closed=True)
    created = client.post('/api/workers', json={'worker_name': 'Other unaffected Worker', 'enabled': True})
    assert created.status_code == 200, created.text
    other = created.json()['data']
    path = f"/api/workers/{other['id']}"
    assert client.post(path + '/client-bind', json={'worker_token': other['worker_token'],
        'client_instance_id': 'other-instance'}).status_code == 200
    other_headers = {'X-Worker-Token': other['worker_token'], 'X-Client-Instance-Id': 'other-instance'}
    heartbeat = {'client_instance_id': 'other-instance', 'running_status': 'idle',
                 'rpa_component_status': 'ready', 'wechat_status': 'logged_in'}
    assert client.post(path + '/heartbeat', headers=other_headers, json=heartbeat).status_code == 200
    assert client.post(path + '/run-status', headers=other_headers,
                       json={'client_instance_id': 'other-instance', 'run_status': 'running'}).status_code == 200
    invalidate(row)
    assert post(worker, payload).status_code == 200
    after = client.post(path + '/heartbeat', headers=other_headers, json=heartbeat)
    assert after.status_code == 200 and after.json()['data']['run_status'] == 'running', after.text
    assert client.get(path + '/tasks/pull', headers=other_headers).status_code == 200
    wrong_owner = client.post(path + '/wechat/messages/ingest', headers=other_headers, json=payload)
    assert wrong_owner.status_code == 409, wrong_owner.text
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).run_status == 'faulted'
        assert db.get(Worker, other['id']).run_status == 'running'


@pytest.mark.parametrize('restore_before_first_settlement', [False, True])
def test_restoring_customer_does_not_revive_revoked_original_batch(restore_before_first_settlement):
    worker, row, payload = prepared_read(closed=True)
    invalidate(row)
    first = None
    if not restore_before_first_settlement:
        first = post(worker, payload)
        assert first.status_code == 200, first.text
    restored = client.post('/api/leads/' + row['lead_id'] + '/restore')
    assert restored.status_code == 200, restored.text
    repeated = post(worker, payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['data']['recovery_settlement']['disposition'] == 'business_cancelled'
    if first is not None:
        assert first.json()['data'] == repeated.json()['data']
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(ReplyAction.id))) == 0


@pytest.mark.parametrize('damage', ['token', 'instance', 'rebound', 'flow', 'conversation', 'authorization', 'no_finish', 'no_revocation', 'sha', 'header'])
def test_terminal_settlement_rejects_unproven_ownership(damage):
    from app.models.base import utcnow
    worker, row, payload = prepared_read(closed=True)
    if damage != 'no_revocation':
        invalidate(row)
    supplied = headers(worker, payload['read_run_id'])
    if damage == 'token': supplied['X-Worker-Token'] = 'invalid'
    if damage == 'instance': supplied['X-Client-Instance-Id'] = 'different'
    if damage == 'header': supplied['X-Inflight-Flow-Id'] = 'different'
    if damage == 'flow':
        payload = json.loads(json.dumps(payload).replace(payload['read_run_id'], 'different'))
        supplied['X-Inflight-Flow-Id'] = payload['read_run_id']
    if damage == 'conversation': payload['conversation_id'] = 'different'
    if damage == 'authorization': payload['authorization_revision'] = 'different'
    if damage == 'sha': payload['contract_sha256'] = '0' * 64
    with SessionLocal() as db:
        if damage == 'rebound': db.get(Worker, worker['id']).bound_at = utcnow()
        if damage == 'no_finish':
            db.delete(db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
        if damage == 'no_revocation':
            from app.models.wechat import WechatSessionBinding
            db.get(WechatSessionBinding, row['binding_id']).authorization_revision += 1
        db.commit()
    rejected = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=supplied)
    assert rejected.status_code in {401, 409, 422}, rejected.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_read_business_settled')) == 0


def test_partition_receipts_preserve_accepted_facts_and_require_complete_group_before_finish():
    from chejin_worker_client.c2_outbox_recovery import split_ingest_payload
    worker, row, payload = prepared_read(closed=False, message_count=30)
    for message in payload['messages']:
        message['raw_payload']['voice_transcription_meta'] = {'transport_padding': 'x' * 80_000}
    parts = split_ingest_payload(payload)
    assert len(parts) >= 2
    accepted = post(worker, parts[0])
    assert accepted.status_code == 200, accepted.text
    with SessionLocal() as db:
        accepted_ids = set(db.scalars(select(MessageEvent.id)))
        assert len(accepted_ids) == len(parts[0]['messages'])
    invalidate(row)
    finish_body = {'flow_id': payload['read_run_id'], 'conversation_id': row['conversation_id'],
                   'terminal_kind': 'read_cancelled', 'error_code': 'LEAD_INVALID'}
    url = f"/api/workers/{worker['id']}/inflight-flow/finish"
    unfinished = client.post(url, headers=headers(worker, payload['read_run_id']), json=finish_body)
    assert unfinished.status_code == 409, unfinished.text
    for part in parts[1:]:
        response = post(worker, part)
        assert response.status_code == 200, response.text
        data = response.json()['data']
        assert not data['accepted_source_message_keys']
        assert set(data['recovery_settlement']['source_message_keys']) == {item['source_message_key'] for item in part['messages']}
    finished = client.post(url, headers=headers(worker, payload['read_run_id']), json=finish_body)
    assert finished.status_code == 200, finished.text
    # Only after natural completion: a delayed accepted-part replay remains
    # idempotent. It must not manufacture proof needed to finish the old Flow.
    result = post(worker, parts[0])
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert data['recovery_settlement']['source_message_keys'] == []
    assert set(data['accepted_source_message_keys']) == {item['source_message_key'] for item in parts[0]['messages']}
    with SessionLocal() as db:
        assert set(db.scalars(select(MessageEvent.id))) == accepted_ids
        assert not db.get(Worker, worker['id']).inflight_flow_state
        assert db.scalar(select(func.count(ReplyAction.id))) == 0


@pytest.mark.parametrize('mode', ['normal', 'active_flow', 'partitions', 'old_backend',
    'html_502', 'json_503', 'disconnect', 'timeout',
    'response_lost', 'save_before', 'save_inside', 'save_after_commit'])
def test_real_worker_settles_then_starts_and_finishes_next_customer(http_api, tmp_path, mode):
    from app.enums import ContactType
    from app.services.lead_service import _contact_model
    from app.services.contact_utils import normalize_phone
    from task_ownership_fixtures import owned_add_friend_task
    from app.models.lead import Lead
    worker, row, payload = prepared_read(closed=mode != 'active_flow', message_count=30 if mode == 'partitions' else 4)
    if mode == 'active_flow':
        stopped = client.post(f"/api/workers/{worker['id']}/run-status", headers=headers(worker),
                             json={'client_instance_id': 'followup-test', 'run_status': 'faulted'})
        assert stopped.status_code == 200, stopped.text
    if mode == 'partitions':
        for message in payload['messages']:
            message['raw_payload']['voice_transcription_meta'] = {'transport_padding': 'x' * 80_000}
    root = Path(__file__).resolve().parents[2]
    frozen = json.loads((root / 'contracts/recovery/c2_contract_v3_0.9.78.json').read_text())
    digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Fixture construction only: a published .78 message contract, not a
    # relabelled candidate contract. Runtime receives these immutable bytes.
    use_contract(payload, {'contract_revision': '0.9.78', 'contract_sha256': digest})
    invalidate(row)
    with SessionLocal() as db:
        lead_b = Lead(customer_name='Customer B', status='assigned', source_type='manual',
                      source_name_snapshot='test', created_by='test', updated_by='test')
        db.add(lead_b); db.flush()
        db.add(_contact_model(lead_b.id, ContactType.phone, normalize_phone('13800009998'), True))
        task = owned_add_friend_task(db, lead_id=lead_b.id, worker_id=worker['id'], task_type='add_friend', status='pending')
        db.add(task); db.flush()
        task_id = task.id
        db.commit()
    request = {'worker': worker, 'payload': payload, 'mode': mode, 'next_task_id': task_id,
               'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    request_path, evidence_path = tmp_path / 'request.json', tmp_path / 'worker-evidence.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False))
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / path) for path in
        ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa')),
        'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_RPA_MODE': 'mock',
        'CHEJIN_C2_ENABLED': 'false', 'CHEJIN_HEARTBEAT_INTERVAL': '0.1', 'CHEJIN_TASK_POLL_INTERVAL': '0.1'}
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('worker_business_settlement_probe.py')),
                           str(request_path), str(evidence_path)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=50)
    (tmp_path / 'worker.log').write_text(proc.stdout + proc.stderr)
    assert proc.returncode == 0, proc.stderr[-4500:]
    evidence = json.loads(evidence_path.read_text())
    assert evidence['response_loss_injected'] == (mode in {'response_lost', 'partitions'})
    assert evidence['storage_failure_injected'] == mode.startswith('save_')
    if mode == 'save_inside':
        assert evidence['rollback_snapshot'] == {'waiting_ledger': 4, 'terminal_outbox': 0}
    with SessionLocal() as db:
        settled_task = db.get(Task, task_id)
        owner = db.get(Worker, worker['id'])
        assert settled_task.status == 'completed' and settled_task.result_code == 'invite_sent'
        assert settled_task.lease_owner_worker_id is None and settled_task.lease_expires_at is None
        assert not owner.inflight_flow_state and owner.current_task is None
        assert owner.run_status == 'running'
        assert db.scalar(select(func.count(MessageEvent.id)).where(MessageEvent.conversation_id == row['conversation_id'])) == 0
        assert db.scalar(select(func.count(ReplyAction.id)).where(ReplyAction.conversation_id == row['conversation_id'])) == 0
