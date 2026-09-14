"""Real HTTP / PostgreSQL / restarted Worker / SQLite, physical boundary only."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.lead import Lead
from app.models.wechat import MessageEvent
from app.models.c3 import ReplyAction
from app.models.task import Task
from app.models.worker import Worker
from app.services.lead_service import _contact_model
from app.services.contact_utils import normalize_phone
from app.enums import ContactType
from task_ownership_fixtures import owned_add_friend_task
from test_contract_equivalent_recovery import prepared_read
from test_lead_followup_eligibility import isolated_db, http_api


def next_customer_task(worker):
    with SessionLocal() as db:
        lead = Lead(customer_name='Media recovery next customer', status='assigned', source_type='manual',
                    source_name_snapshot='test', created_by='test', updated_by='test')
        db.add(lead); db.flush()
        db.add(_contact_model(lead.id, ContactType.phone, normalize_phone('13800009997'), True))
        task = owned_add_friend_task(db, lead_id=lead.id, worker_id=worker['id'], task_type='add_friend', status='pending')
        db.add(task); db.flush(); task_id = task.id; db.commit()
    return task_id


@pytest.mark.parametrize('message_type', ['voice', 'image'])
@pytest.mark.parametrize('ordinary_outbox', [False, True])
@pytest.mark.parametrize('keep_customer_valid', [False, True])
def test_ended_media_owner_recovers_then_explicit_start_completes_next_customer(
        http_api, tmp_path, message_type, ordinary_outbox, keep_customer_valid):
    worker, row, payload = prepared_read(closed=False, message_count=1, eligible=True)
    task_id = next_customer_task(worker)
    request = {'worker': worker, 'lead_id': row['lead_id'], 'payload': payload,
        'next_task_id': task_id, 'message_type': message_type, 'ordinary_outbox': ordinary_outbox,
        'keep_customer_valid': keep_customer_valid,
        'stop_after_recovery_checks': keep_customer_valid and ordinary_outbox,
        'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    request_path = tmp_path / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False))
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / p) for p in (
        'worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa')),
        'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_C2_ENABLED': 'false',
        'CHEJIN_HEARTBEAT_INTERVAL': '.1', 'CHEJIN_TASK_POLL_INTERVAL': '.1'}
    command = [sys.executable, str(Path(__file__).with_name('worker_media_recovery_probe.py')),
               str(request_path), str(tmp_path / 'worker-evidence.json')]
    preparation = subprocess.run([*command, 'prepare'], cwd=root, env=env,
                                capture_output=True, text=True, timeout=35)
    (tmp_path / 'prepare.log').write_text(preparation.stdout + preparation.stderr)
    assert preparation.returncode == 0, preparation.stderr[-1800:]
    # A separate OS process opens exactly the same SQLite and Journal directory.
    # No fixture repair or acknowledgements occur between the two processes.
    proc = subprocess.run([*command, 'recover'], cwd=root, env=env,
                         capture_output=True, text=True, timeout=65)
    (tmp_path / 'worker.log').write_text(proc.stdout + proc.stderr)
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        facts = list(db.scalars(select(MessageEvent)))
        result = {'facts': [{'id': f.id, 'type': f.message_type, 'state': f.item_state,
                            'source_key': f.source_message_key, 'content': f.content} for f in facts],
            'replies': db.scalar(select(func.count(ReplyAction.id))),
            'task_status': db.get(Task, task_id).status, 'flow': owner.inflight_flow_state}
        (tmp_path / 'backend-evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert proc.returncode == 0, proc.stderr[-6000:]
    if keep_customer_valid and ordinary_outbox:
        # Normal A facts can create a legitimate priority reply. Do not change
        # customer eligibility or delete that task to manufacture a B success.
        assert any(f.message_type == message_type for f in facts)
        assert result['task_status'] == 'pending' and not result['flow'], result
        return
    assert len(facts) == 1 and facts[0].message_type == message_type
    assert result['replies'] == 0
    assert result['task_status'] == 'completed' and not result['flow'], result


@pytest.mark.parametrize('kind', ['voice', 'image'])
@pytest.mark.parametrize('item_state', ['completed', 'failed'])
@pytest.mark.parametrize('released_contract', [False, True])
@pytest.mark.parametrize('keep_customer_valid', [False, True])
def test_existing_media_outbox_keeps_original_fact_and_completes_next_customer(
        http_api, tmp_path, kind, item_state, released_contract, keep_customer_valid):
    """Protocol fixture for a previously formed media fact, not a live OCR claim.

    Unlike the completed-media tests above, the input is an existing typed
    Outbox fixture. Real Worker/HTTP/SQLite must preserve its original outcome.
    """
    from app.models.wechat import WechatSessionBinding
    from test_wechat_c2_api import _fact_settlement_payload, _v3_failed_image_message, _v3_failed_voice_message, _v3_message
    from test_contract_equivalent_recovery import use_contract
    from app.contracts.read_recovery import compatible_read_contract
    from app.schemas.wechat import WechatMessageIngestRequest
    from app.services.wechat_service import _validate_v3_request_contract
    from test_lead_followup_eligibility import headers
    worker, row, original = prepared_read(closed=False, message_count=1, eligible=True)
    with SessionLocal() as db:
        bound = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': bound.id, 'conversation_id': bound.conversation_id, 'rpa_session_key': bound.rpa_session_key,
                  'unread_generation': bound.unread_generation}
        remark = bound.remark_code
    source = 'existing-' + item_state + '-' + kind
    builder = _v3_failed_voice_message if kind == 'voice' else _v3_failed_image_message
    code = 'VOICE_TRANSCRIBE_PARTIAL' if kind == 'voice' else 'C2_IMAGE_SOURCE_INVALID'
    message = (builder(source, role='customer', screen_order=1, reason=code) if item_state == 'failed' else
        _v3_message(source, role='customer', message_type=kind, content='Original completed media', screen_order=1,
            raw_extra={'voice_transcription': 'Original completed media', 'voice_duration_seconds': 5} if kind == 'voice' else {}))
    payload = _fact_settlement_payload(target, remark, transaction_id='existing-media-transaction',
        source_keys=[source], settlement_mode='fact_only', action_kind=kind,
        original_read_run_id=original['read_run_id'],
        messages=[message])
    if released_contract:
        frozen = json.loads((Path(__file__).resolve().parents[2] / 'contracts/recovery/c2_contract_v3_0.9.80.json').read_text())
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert digest == '43f8c07e3660d790c39f3b348dcce9fb1e2c0bed243b41cff6669a658995e380'
        use_contract(payload, {'contract_revision': frozen['contract_revision'], 'contract_sha256': digest})
        assert compatible_read_contract(payload['contract_revision'], payload['contract_sha256']) == frozen
        _validate_v3_request_contract(WechatMessageIngestRequest.model_validate(payload), contract=frozen)
    (tmp_path / 'original-payload.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    task_id = next_customer_task(worker)
    base = '/api/workers/' + worker['id']
    assert http_api.post(base + '/run-status', headers=headers(worker),
        json={'client_instance_id': 'followup-test', 'run_status': 'faulted'}).status_code == 200
    finished = http_api.post(base + '/inflight-flow/finish', headers=headers(worker, payload['read_run_id']),
        json={'flow_id': payload['read_run_id'], 'terminal_kind': 'technical_failed',
              'conversation_id': row['conversation_id'], 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
    assert finished.status_code == 200, finished.text
    if not keep_customer_valid:
        assert http_api.post('/api/leads/' + row['lead_id'] + '/mark-invalid',
                            json={'invalid_reason': 'test_data'}).status_code == 200
    request = {'worker': worker, 'payload': payload, 'next_task_id': task_id, 'mode': 'fact_settlement',
               'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    path = tmp_path / 'request.json'; path.write_text(json.dumps(request))
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / p) for p in (
        'worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa')),
        'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_C2_ENABLED': 'false',
        'CHEJIN_HEARTBEAT_INTERVAL': '.1', 'CHEJIN_TASK_POLL_INTERVAL': '.1'}
    process = subprocess.run([sys.executable, str(Path(__file__).with_name('worker_business_settlement_probe.py')),
        str(path), str(tmp_path / 'worker-evidence.json')], cwd=root, env=env, capture_output=True, text=True, timeout=50)
    (tmp_path / 'worker.log').write_text(process.stdout + process.stderr)
    assert process.returncode == 0, process.stderr[-2000:]
    with SessionLocal() as db:
        fact = db.scalar(select(MessageEvent).where(MessageEvent.source_message_key == source))
        assert fact and fact.item_state == item_state
        assert fact.error_code == (code if item_state == 'failed' else None)
        assert db.get(Task, task_id).status == 'completed'
        assert not db.get(Worker, worker['id']).inflight_flow_state
        assert db.scalar(select(func.count(ReplyAction.id))) == 0
    import sqlite3
    with sqlite3.connect('file:' + str(tmp_path / 'client/worker_client.sqlite3') + '?mode=ro', uri=True) as db:
        assert db.execute('SELECT terminal_state,ingest_state FROM c2_message_ledger WHERE source_message_key=?',
                          (source,)).fetchone() == (item_state, 'confirmed')
        assert json.loads(db.execute('SELECT payload_json FROM c2_ingest_outbox').fetchone()[0]) == payload
