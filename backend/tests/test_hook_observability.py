"""Actual native proof → issued timing identity → existing authenticated queue."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.c3 import MessageBatch
from app.models.observability import ProcessStageRun
from app.services.observability_service import process_run_id_for_batch
from test_hook_business_http import clean, client
from test_hook_history_business_http import bound, coverage, upload


def setup(monkeypatch):
    monkeypatch.setattr(get_settings(), 'observability_enabled', True)
    w, t, p, binding = bound()
    response, body = upload(w, p, binding, coverage(binding), monkeypatch)
    assert response.status_code == 200, response.text
    return w, binding, response.json()['data'], body


def event(data, **changes):
    now = datetime.now(timezone.utc).isoformat()
    item = {'process_run_id': data['process_run_id'], 'stage_run_id': str(uuid.uuid4()),
            'conversation_id': data['conversation_id'], 'stage_name': 'hook.voice_transcription',
            'component': 'worker', 'attempt': 1, 'started_at': now, 'ended_at': now,
            'execution_duration_ms': 123, 'status': 'succeeded'}
    return {**item, **changes}


def send(w, item):
    return client.post(f"/api/workers/{w['id']}/observability/stage-events",
        headers={'X-Worker-Token': w['worker_token'], 'X-Client-Instance-Id': w['instance']},
        json={'events': [item]})


def test_native_history_and_brain_share_issued_process_without_ocr_scan(monkeypatch):
    w, binding, data, body = setup(monkeypatch)
    with SessionLocal() as db:
        batch = db.get(MessageBatch, data['message_batch']['batch_id'])
        assert process_run_id_for_batch(db, batch) == data['process_run_id']
        assert db.scalar(select(ProcessStageRun.worker_id).where(
            ProcessStageRun.process_run_id == data['process_run_id'],
            ProcessStageRun.stage_name == 'hook.history_verified')) == w['id']
    item = event(data)
    first = send(w, item);repeat = send(w, item)
    assert first.status_code == repeat.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ProcessStageRun).where(
            ProcessStageRun.stage_run_id == item['stage_run_id'])) == 1


def test_fabricated_native_process_id_is_rejected(monkeypatch):
    w, _, data, _ = setup(monkeypatch)
    result = send(w, event(data, process_run_id=str(uuid.uuid4())))
    assert result.status_code == 403 and result.json()['code'] == 'OBSERVABILITY_PROCESS_RUN_NOT_ISSUED'


def test_wrong_customer_scope_is_rejected_even_with_issued_process(monkeypatch):
    w, _, data, _ = setup(monkeypatch)
    result = send(w, event(data, conversation_id=str(uuid.uuid4())))
    assert result.status_code == 403


def test_unknown_native_stage_is_rejected_without_loosening_existing_contract(monkeypatch):
    w, _, data, _ = setup(monkeypatch)
    result = send(w, event(data, stage_name='hook.arbitrary_send_permission'))
    assert result.status_code == 400 and result.json()['code'] == 'OBSERVABILITY_STAGE_NAME_INVALID'
