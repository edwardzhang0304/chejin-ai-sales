import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import isolated_db, http_api
from test_contract_equivalent_recovery import prepared_read
from app.core.database import SessionLocal
from app.models.lead import Lead
from app.models.worker import Worker
from app.models.audit import OperationLog
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.c3 import ReplyAction
from app.services.lead_service import _contact_model
from app.services.contact_utils import normalize_phone
from app.enums import ContactType
from task_ownership_fixtures import owned_add_friend_task
from sqlalchemy import select, func

@pytest.mark.parametrize('first_accepted', [False, True])
def test_partial_accepted_partition_recovery_reaches_next_customer(http_api, tmp_path, first_accepted):
    worker, row, payload = prepared_read(closed=False, message_count=30)
    # Synthetic transport weight exercises the production splitter unchanged;
    # it is not represented as a real screenshot or customer conversation.
    for item in payload['messages']:
        item['raw_payload']['voice_transcription_meta'] = {'transport_padding': 'x' * 80_000}
    with SessionLocal() as db:
        lead = Lead(customer_name='Independent customer B', status='assigned', source_type='manual', source_name_snapshot='test', created_by='test', updated_by='test')
        db.add(lead); db.flush()
        db.add(_contact_model(lead.id, ContactType.phone, normalize_phone('13800009998'), True))
        task = owned_add_friend_task(db, lead_id=lead.id, worker_id=worker['id'], task_type='add_friend', status='pending')
        db.add(task); db.flush(); task_id = task.id; db.commit()
    request = {'worker': worker, 'lead_id': row['lead_id'], 'payload': payload, 'first_accepted': first_accepted,
        'next_task_id': task_id, 'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    request_path = tmp_path / 'request.json'; request_path.write_text(json.dumps(request, ensure_ascii=False))
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / p) for p in ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa')),
        'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_C2_ENABLED': 'false',
        'CHEJIN_HEARTBEAT_INTERVAL': '0.1', 'CHEJIN_TASK_POLL_INTERVAL': '0.1'}
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('worker_partition_recovery_probe.py')), str(request_path), str(tmp_path / 'worker-evidence.json')],
        cwd=root, env=env, capture_output=True, text=True, timeout=50)
    (tmp_path / 'worker.log').write_text(proc.stdout + proc.stderr)
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        state = {'run_status': owner.run_status, 'flow': owner.inflight_flow_state,
                 'facts': db.scalar(select(func.count(MessageEvent.id))), 'replies': db.scalar(select(func.count(ReplyAction.id))),
                 'proofs': [r.after_data for r in db.scalars(select(OperationLog).where(OperationLog.event_type == 'worker_read_business_settled'))]}
        (tmp_path / 'backend-evidence.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
    assert proc.returncode == 0, proc.stderr[-7000:]

import hashlib
from test_wechat_c2_api import _v3_message, _v3_ingest_payload
from test_lead_followup_eligibility import headers

@pytest.mark.parametrize('message_type', ['voice', 'image'])
@pytest.mark.parametrize('ended', [False, True])
def test_media_dedicated_exit_handles_flow_ended_before_revocation(http_api, tmp_path, message_type, ended):
    worker, row, original = prepared_read(closed=False, message_count=1)
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': binding.id, 'conversation_id': binding.conversation_id,
                  'rpa_session_key': binding.rpa_session_key, 'unread_generation': binding.unread_generation}
        remark = binding.remark_code
    payload = _v3_ingest_payload(target, remark, read_run_id=original['read_run_id'],
        messages=[_v3_message('completed-original-media', role='customer', message_type=message_type,
            content='Completed original media result', screen_order=1,
            raw_extra={'voice_transcription': 'Completed original media result', 'voice_duration_seconds': 5} if message_type == 'voice' else {})])
    base = '/api/workers/' + worker['id']
    stop = http_api.post(base + '/run-status', headers=headers(worker),
        json={'client_instance_id': 'followup-test', 'run_status': 'faulted'})
    assert stop.status_code == 200, stop.text
    if ended:
        finish = http_api.post(base + '/inflight-flow/finish', headers=headers(worker, payload['read_run_id']),
            json={'flow_id': payload['read_run_id'], 'terminal_kind': 'technical_failed',
                  'conversation_id': row['conversation_id'], 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
        assert finish.status_code == 200, finish.text
    invalid = http_api.post('/api/leads/' + row['lead_id'] + '/mark-invalid', json={'invalid_reason': 'test_data'})
    assert invalid.status_code == 200, invalid.text
    normal = http_api.post(base + '/wechat/messages/ingest', headers=headers(worker, payload['read_run_id']), json=payload)
    assert normal.status_code == 409 and normal.json()['code'] == 'C2_FACT_SETTLEMENT_REQUIRED', normal.text
    keys = sorted(item['source_message_key'] for item in payload['messages'])
    params = {'recovery_transaction_id': 'original-media-action-transaction', 'action_kind': message_type,
              'source_message_key_digest': hashlib.sha256('\n'.join(keys).encode()).hexdigest(),
              'original_authorization_revision': payload['authorization_revision']}
    dedicated = http_api.get(base + '/wechat/conversations/' + row['conversation_id'] + '/read-authorization',
        headers=headers(worker, None if ended else payload['read_run_id']), params=params)
    with SessionLocal() as db:
        logs = [{'event': r.event_type, 'after': r.after_data, 'metadata': r.extra_metadata}
                for r in db.scalars(select(OperationLog).where(OperationLog.event_type.in_(
                    ['worker_inflight_finished', 'lead_followup_revoked']))) ]
        state = {'ended': ended, 'message_type': message_type, 'normal_status': normal.status_code,
            'normal_response': normal.json(), 'dedicated_status': dedicated.status_code,
            'dedicated_response': dedicated.json(), 'history': logs,
            'facts': db.scalar(select(func.count(MessageEvent.id))), 'replies': db.scalar(select(func.count(ReplyAction.id)))}
    (tmp_path / 'observed.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
    assert dedicated.status_code == 200, dedicated.text
    assert dedicated.json()['data']['recovery_decision'] == 'settle_without_ui'


@pytest.mark.parametrize('damage', ['revision', 'flow', 'instance', 'rebound', 'finish', 'revocation'])
@pytest.mark.parametrize('keep_customer_valid', [False, True])
def test_ended_media_authorization_rejects_unproven_original_owner(http_api, damage, keep_customer_valid):
    from app.models.base import utcnow
    worker, row, original = prepared_read(closed=True, message_count=1)
    if not keep_customer_valid or damage == 'revocation':
        assert http_api.post('/api/leads/' + row['lead_id'] + '/mark-invalid',
                             json={'invalid_reason': 'test_data'}).status_code == 200
    with SessionLocal() as db:
        if damage == 'rebound':
            db.get(Worker, worker['id']).bound_at = utcnow()
        if damage == 'finish':
            db.delete(db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
        if damage == 'revocation':
            # Negative fixture: the customer was invalidated, but its required
            # revocation evidence is absent. A valid customer needs no such log.
            db.delete(db.scalar(select(OperationLog).where(OperationLog.event_type == 'lead_followup_revoked')))
        db.commit()
    supplied = headers(worker)
    if damage == 'instance': supplied['X-Client-Instance-Id'] = 'different'
    params = {'recovery_transaction_id': 'original-media-action', 'action_kind': 'voice',
        'source_message_key_digest': hashlib.sha256(b'original-media').hexdigest(),
        'original_authorization_revision': 'different' if damage == 'revision' else original['authorization_revision'],
        'original_read_run_id': 'different' if damage == 'flow' else original['read_run_id']}
    response = http_api.get('/api/workers/' + worker['id'] + '/wechat/conversations/' + row['conversation_id']
                            + '/read-authorization', headers=supplied, params=params)
    assert response.status_code in {401, 409}, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.get(Worker, worker['id']).run_status == 'faulted'
