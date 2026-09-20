"""Real HTTP/PG/Worker/SQLite; synthetic historical records, no Windows claim."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from app.contracts import read_recovery
from app.contracts.shared_rules import shared_adapter
from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.c3 import MessageBatch, ReplyAction
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import WechatMessageIngestRequest
from app.services import worker_service
from app.services.wechat_service import _validate_v3_request_contract
from test_contract_equivalent_recovery import use_contract
from test_lead_followup_eligibility import fixture_rows, headers, isolated_db, http_api
from test_wechat_c2_api import _v3_ingest_payload, _v3_message, _fact_settlement_payload, _v3_failed_voice_message

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ('0.9.92', '0.9.75', '0.9.78', '0.9.80', '0.9.85', '0.9.86')


def frozen(revision):
    return json.loads((ROOT / f'contracts/recovery/c2_contract_v3_{revision}.json').read_text())


def prepared_frozen_read(http_api, monkeypatch, revision, closed):
    worker, rows = fixture_rows()
    row = rows[0]
    historical = frozen(revision)
    sha = shared_adapter('contract_rules').contract_sha256(historical)
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': binding.id, 'conversation_id': binding.conversation_id,
                  'rpa_session_key': binding.rpa_session_key, 'unread_generation': binding.unread_generation}
        remark = binding.remark_code
    payload = _v3_ingest_payload(target, remark, read_run_id='original-frozen-read', read_reason='waiting_sales_reply',
        messages=[_v3_message(f'original-{i}', role='customer', message_type='text',
                             content=f'原始问题 {i}', screen_order=i) for i in (1, 2)])
    # Construct a synthetic old wire fixture BEFORE persistence. Once captured,
    # no body/revision/SHA or database state is converted to make recovery pass.
    use_contract(payload, {'contract_revision': revision, 'contract_sha256': sha})
    _validate_v3_request_contract(WechatMessageIngestRequest.model_validate(payload), contract=historical)
    endpoint = '/api/workers/' + worker['id']
    # Simulate the previous server's contract label/hash providers only during
    # original registration. The real route, ownership/locking and writes run.
    with monkeypatch.context() as old_server:
        old_server.setattr(worker_service, 'contract_revision', lambda: revision)
        old_server.setattr(worker_service, 'contract_sha256', lambda: sha)
        response = http_api.post(endpoint + '/inflight-flow/start', headers=headers(worker), json={
            'flow_id': payload['read_run_id'], 'flow_kind': 'c2_read', 'conversation_id': row['conversation_id'],
            'unread_generation': payload['unread_generation'], 'authorization_revision': payload['authorization_revision']})
        assert response.status_code == 200, response.text
    response = http_api.post(endpoint + '/run-status', headers=headers(worker),
        json={'client_instance_id': 'followup-test', 'run_status': 'faulted'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['pending_read_recovery']['ready']
    if closed:
        response = http_api.post(endpoint + '/inflight-flow/finish', headers=headers(worker, payload['read_run_id']), json={
            'flow_id': payload['read_run_id'], 'terminal_kind': 'technical_failed',
            'conversation_id': row['conversation_id'], 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
        assert response.status_code == 200, response.text
    return worker, row, payload


def worker_process(tmp_path, request, phase):
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request, ensure_ascii=False))
    env = {**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_RPA_MODE': 'mock',
        'CHEJIN_UI_LOCK_LEASE_SECONDS': '1', 'CHEJIN_HEARTBEAT_INTERVAL': '.1', 'CHEJIN_TASK_POLL_INTERVAL': '.1',
        'PYTHONPATH': os.pathsep.join(str(ROOT / p) for p in ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa'))}
    output = tmp_path / (phase + '.json')
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('worker_sequence_recovery_probe.py')),
        str(path), phase, str(output)], env=env, cwd=ROOT, capture_output=True, text=True, timeout=35)
    (tmp_path / (phase + '.log')).write_text(proc.stdout + proc.stderr)
    assert proc.returncode == 0, proc.stderr[-7000:]
    return json.loads(output.read_text())


@pytest.mark.parametrize('revision', VERSIONS)
@pytest.mark.parametrize('closed', [False, True])
def test_original_contract_read_recovers_same_sqlite_and_settles_once(http_api, monkeypatch, tmp_path, revision, closed):
    worker, row, payload = prepared_frozen_read(http_api, monkeypatch, revision, closed)
    from chejin_worker_client.pending_read_recovery import package_recovery_capability
    backend_cap = read_recovery.read_recovery_capability()
    assert backend_cap == package_recovery_capability()
    assert {'revision': revision, 'sha256': payload['contract_sha256']} in backend_cap['contracts']
    request = {'worker': worker, 'payload': payload, 'closed': closed, 'response_lost': True,
               'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    capture = worker_process(tmp_path, request, 'capture')
    before_db = tmp_path / 'client/worker_client.sqlite3'
    inode = before_db.stat().st_ino
    result = worker_process(tmp_path, request, 'resume')
    assert before_db.stat().st_ino == inode
    assert result['before_sha256'] == result['after_sha256']
    assert result['response_loss_injected'] and result['outbox'] == 'confirmed'
    with sqlite3.connect('file:' + str(before_db) + '?mode=ro', uri=True) as db:
        stored = db.execute('SELECT payload_json FROM c2_ingest_outbox WHERE outbox_id=?', (capture['outbox'],)).fetchone()[0]
        assert json.loads(stored) == payload
        assert db.execute("SELECT COUNT(*) FROM c2_message_ledger WHERE ingest_state='waiting'").fetchone()[0] == 0
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        assert owner.run_status == 'faulted' and not owner.inflight_flow_state
        assert db.scalar(select(func.count(MessageEvent.id))) == 2
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
        facts = list(db.scalars(select(MessageEvent)))
        assert all(f.raw_payload['contract_revision'] == revision for f in facts)
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_inflight_finished')) == 1


@pytest.mark.parametrize('closed', [False, True])
@pytest.mark.parametrize('revision', ['0.9.85', '0.9.86'])
def test_disabling_migration_blocks_real_recovery_then_same_sqlite_retries(http_api, monkeypatch, tmp_path, closed, revision):
    worker, row, payload = prepared_frozen_read(http_api, monkeypatch, revision, closed)
    request = {'worker': worker, 'payload': payload, 'closed': closed,
               'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    worker_process(tmp_path, request, 'capture')
    with monkeypatch.context() as disabled:
        predecessor = '_pre_send_read_predecessor' if revision == '0.9.86' else '_sequence_read_predecessor'
        disabled.setattr(shared_adapter('contract_rules'), predecessor, lambda current: None)
        # A same-labelled development server may accept the original technical
        # failure receipt before ingestion; this must not settle the read or
        # resume work. Actual cross-release recovery retains the stricter gate.
        from app.contracts.c2 import contract_revision
        rejected = worker_process(tmp_path, {**request, 'expect_rejected': True,
            'allow_original_technical_finish': revision == contract_revision()}, 'blocked')
        # The same success assertion used by the positive is false while the
        # migration is off, before any retry under restored production rules.
        with pytest.raises(AssertionError):
            assert rejected['outbox'] == 'confirmed'
        with SessionLocal() as db:
            assert db.scalar(select(func.count(MessageEvent.id))) == 0
            assert db.scalar(select(func.count(ReplyAction.id))) == 0
            assert db.get(Worker, worker['id']).run_status == 'faulted'
    recovered = worker_process(tmp_path, request, 'retry')
    assert recovered['outbox'] == 'confirmed'
    assert rejected['before_sha256'] == rejected['after_sha256'] == recovered['before_sha256'] == recovered['after_sha256']


@pytest.mark.parametrize('damage', ['worker', 'customer', 'flow', 'sha', 'changed_read_rule', 'unknown_rule',
                                  'missing_finish', 'sending', 'unknown_send_result'])
@pytest.mark.parametrize('revision', ['0.9.92', '0.9.85', '0.9.86'])
def test_frozen_read_rejects_invalid_proof_and_unsettled_sends(http_api, monkeypatch, damage, revision):
    worker, row, original = prepared_frozen_read(http_api, monkeypatch, revision, True)
    payload = copy.deepcopy(original)
    supplied = headers(worker, payload['read_run_id'])
    if damage == 'worker': supplied['X-Worker-Token'] = 'other-worker-token'
    if damage == 'customer': payload['conversation_id'] = 'other-customer'
    if damage == 'flow':
        payload = json.loads(json.dumps(payload).replace(payload['read_run_id'], 'never-registered'))
        supplied['X-Inflight-Flow-Id'] = payload['read_run_id']
    if damage == 'sha': payload['contract_sha256'] = '0' * 64
    if damage in ('changed_read_rule', 'unknown_rule'):
        altered = frozen(revision)
        if damage == 'unknown_rule': altered['unreviewed_rule'] = True
        else: altered['message_identity_contract']['duplicate_identity_invariants'].remove('sender_role')
        use_contract(payload, {'contract_revision': revision, 'contract_sha256': shared_adapter('contract_rules').contract_sha256(altered)})
    with SessionLocal() as db:
        if damage == 'missing_finish':
            db.delete(db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
        if damage in ('sending', 'unknown_send_result'):
            batch = MessageBatch(conversation_id=row['conversation_id'], status='generated')
            db.add(batch); db.flush()
            db.add(ReplyAction(batch_id=batch.id, conversation_id=row['conversation_id'],
                               claimed_by_worker_id=worker['id'], status=damage))
        db.commit()
    response = http_api.post('/api/workers/' + worker['id'] + '/wechat/messages/ingest', headers=supplied, json=payload)
    assert response.status_code in (401, 409), response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.get(Worker, worker['id']).run_status == 'faulted'


@pytest.mark.parametrize('revision', VERSIONS)
def test_frozen_fact_only_keeps_its_dedicated_authorization(http_api, monkeypatch, revision):
    worker, row, original = prepared_frozen_read(http_api, monkeypatch, revision, True)
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': binding.id, 'conversation_id': binding.conversation_id,
                  'rpa_session_key': binding.rpa_session_key, 'unread_generation': binding.unread_generation}
        remark = binding.remark_code
    source = 'original-failed-voice'
    payload = _fact_settlement_payload(target, remark, transaction_id='original-voice-transaction',
        source_keys=[source], settlement_mode='fact_only', action_kind='voice', original_read_run_id=original['read_run_id'],
        messages=[_v3_failed_voice_message(source, role='customer', screen_order=1, reason='VOICE_TRANSCRIBE_PARTIAL')])
    use_contract(payload, {'contract_revision': revision, 'contract_sha256': original['contract_sha256']})
    endpoint = '/api/workers/' + worker['id']
    params = {'recovery_transaction_id': 'original-voice-transaction', 'action_kind': 'voice',
        'source_message_key_digest': hashlib.sha256(source.encode()).hexdigest(),
        'original_authorization_revision': payload['authorization_revision'], 'original_read_run_id': payload['read_run_id']}
    authorization = http_api.get(endpoint + '/wechat/conversations/' + row['conversation_id'] + '/read-authorization',
                                 headers=headers(worker), params=params)
    assert authorization.status_code == 200, authorization.text
    data = authorization.json()['data']
    assert not data['allowed'] and data['authorization_scope'] == 'fact_settlement'
    supplied = {**headers(worker), 'X-C2-Settlement-Token': data['settlement_token']}
    for _ in range(2):
        response = http_api.post(endpoint + '/wechat/messages/ingest', headers=supplied, json=payload)
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 1
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
