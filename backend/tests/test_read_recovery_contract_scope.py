"""Real HTTP/PG contract ownership; synthetic records, no desktop/model calls."""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.contracts.c2 import c2_contract_v3, contract_revision, contract_sha256
from app.contracts.shared_rules import shared_adapter
from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.c3 import MessageBatch, ReplyAction
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services import worker_service
from test_contract_equivalent_recovery import use_contract
from test_lead_followup_eligibility import fixture_rows, headers, isolated_db, http_api
from test_wechat_c2_api import _v3_ingest_payload, _v3_message

ROOT = Path(__file__).resolve().parents[2]


def prepare(http_api, monkeypatch, *, original, payload_kind, closed):
    worker, rows = fixture_rows(eligible=True)
    row = rows[0]
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': binding.id, 'conversation_id': binding.conversation_id,
                  'rpa_session_key': binding.rpa_session_key, 'unread_generation': binding.unread_generation}
        remark = binding.remark_code
    current = c2_contract_v3()
    historical = json.loads((ROOT / 'contracts/recovery/c2_contract_v3_0.9.86.json').read_text())
    rules = shared_adapter('contract_rules')
    selected = copy.deepcopy(historical if payload_kind in {'historical', 'historical_label'} else current)
    if payload_kind.endswith('label'):
        selected['contract_revision'] = '0.9.87' if selected['contract_revision'] == '0.9.86' else '0.9.86'
    pair = {'contract_revision': selected['contract_revision'], 'contract_sha256': rules.contract_sha256(selected)}
    registered = historical if original else current
    registered_pair = {'contract_revision': registered['contract_revision'],
                       'contract_sha256': rules.contract_sha256(registered)}
    payload = _v3_ingest_payload(target, remark, read_run_id='scope-read', read_reason='normal_due',
        messages=[_v3_message('scope-message', role='customer', message_type='text',
                             content='想了解一下车辆。', screen_order=1)])
    use_contract(payload, pair)
    endpoint = '/api/workers/' + worker['id']
    # Only historical setup substitutes the old server's version providers.
    # A fresh Flow always uses the real current server without overrides.
    with monkeypatch.context() as old_server:
        if original:
            old_server.setattr(worker_service, 'contract_revision', lambda: registered_pair['contract_revision'])
            old_server.setattr(worker_service, 'contract_sha256', lambda: registered_pair['contract_sha256'])
        response = http_api.post(endpoint + '/inflight-flow/start', headers=headers(worker), json={
            'flow_id': payload['read_run_id'], 'flow_kind': 'c2_read', 'conversation_id': row['conversation_id'],
            'unread_generation': payload['unread_generation'], 'authorization_revision': payload['authorization_revision']})
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        state = db.get(Worker, worker['id']).inflight_flow_state
        assert all(state[k] == v for k, v in registered_pair.items())
        if not original:
            assert state['contract_revision'] == contract_revision()
            assert state['contract_sha256'] == contract_sha256()
    if closed:
        response = http_api.post(endpoint + '/run-status', headers=headers(worker), json={
            'client_instance_id': 'followup-test', 'run_status': 'faulted'})
        assert response.status_code == 200, response.text
        receipt = {'flow_id': payload['read_run_id'], 'terminal_kind': 'technical_failed',
                   'conversation_id': row['conversation_id'], 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'}
        for _ in range(2):
            response = http_api.post(endpoint + '/inflight-flow/finish',
                                     headers=headers(worker, payload['read_run_id']), json=receipt)
            assert response.status_code == 200, response.text
        with SessionLocal() as db:
            records = list(db.scalars(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
            assert len(records) == 1
            assert records[0].extra_metadata['registered_read_contract'] == registered_pair
    return worker, payload, registered_pair


def ingest(http_api, worker, payload):
    return http_api.post('/api/workers/' + worker['id'] + '/wechat/messages/ingest',
                         headers=headers(worker, payload['read_run_id']), json=payload)


@pytest.mark.parametrize('closed', [False, True])
@pytest.mark.parametrize('original,payload_kind,allowed', [
    (False, 'historical', False), (False, 'current_label', True),
    (True, 'historical', True), (True, 'historical_label', True),
])
def test_read_contract_must_match_its_original_flow(http_api, monkeypatch, tmp_path,
                                                  original, payload_kind, allowed, closed):
    worker, payload, registered_pair = prepare(http_api, monkeypatch, original=original,
                                              payload_kind=payload_kind, closed=closed)
    before = copy.deepcopy(payload)
    response = ingest(http_api, worker, payload)
    with SessionLocal() as db:
        evidence = {'original_flow': original, 'closed': closed, 'registered_contract': registered_pair,
                    'submitted_revision': payload['contract_revision'], 'status': response.status_code,
                    'response': response.json(), 'messages': db.scalar(select(func.count(MessageEvent.id))),
                    'batches': db.scalar(select(func.count(MessageBatch.id))),
                    'replies': db.scalar(select(func.count(ReplyAction.id)))}
    (tmp_path / 'admission.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    assert response.status_code == (200 if allowed else 409), 'unexpected contract admission: ' + str(evidence)
    assert evidence['messages'] == int(allowed)
    assert payload == before
    if allowed:
        repeated = ingest(http_api, worker, payload)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['data']['duplicated_count'] == 1
        with SessionLocal() as db:
            event = db.scalar(select(MessageEvent))
            assert event.raw_payload['contract_revision'] == payload['contract_revision']
            assert event.raw_payload['contract_sha256'] == payload['contract_sha256']
    else:
        assert evidence['batches'] == evidence['replies'] == 0
    if closed:
        with SessionLocal() as db:
            owner = db.get(Worker, worker['id'])
            assert owner.run_status == 'faulted' and not owner.inflight_flow_state


def test_published_closed_proof_without_new_metadata_still_recovers(http_api, monkeypatch):
    worker, payload, _ = prepare(http_api, monkeypatch, original=True, payload_kind='historical', closed=True)
    # Synthetic legacy audit fixture: published 086 did not write this new
    # optional metadata. Original finish/body/owner/time remain unchanged.
    with SessionLocal() as db:
        finish = db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished'))
        original = copy.deepcopy(finish.after_data)
        finish.extra_metadata = None
        db.commit()
    for _ in range(2):
        response = ingest(http_api, worker, payload)
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 1
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
        assert db.get(Worker, worker['id']).run_status == 'faulted'
        finish = db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished'))
        assert finish.after_data == original and finish.extra_metadata is None


@pytest.mark.parametrize('damaged', [None, {}, {'contract_revision': '0.9.86', 'contract_sha256': '0' * 64}])
def test_present_but_invalid_original_contract_is_not_legacy(http_api, monkeypatch, damaged):
    worker, payload, _ = prepare(http_api, monkeypatch, original=True, payload_kind='historical', closed=True)
    with SessionLocal() as db:
        finish = db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished'))
        finish.extra_metadata = {'registered_read_contract': damaged}
        db.commit()
    assert ingest(http_api, worker, payload).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
