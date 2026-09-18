"""Original PNG OCR plus correction HTTP/DB; old server facts are test fixtures.

This file does not claim Worker replay, real generation or Windows acceptance.
The private incident is optional for generic CI, mandatory in incident replay.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import select

import test_c3_api as fixtures
from test_lead_followup_eligibility import isolated_db, http_api
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck
from app.models.message_text_correction import MessageTextCorrection
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services.followup_eligibility import token_for_revision
from app.services.message_effective_text import effective_versions, effective_context_digest
from app.contracts.shared_rules import shared_adapter

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def original_png_proposal(tmp_path_factory):
    folder = os.environ.get("CHEJIN_CORRECTION_INCIDENT")
    runtime = os.environ.get("CHEJIN_REAL_OCR_PYTHON")
    if not folder or not runtime:
        pytest.skip("Private original PNG and real OCR runtime must be explicitly supplied")
    incident = Path(folder)
    with sqlite3.connect('file:' + str(incident/'worker.sqlite3') + '?mode=ro&immutable=1', uri=True) as db:
        payloads = [json.loads(r[0]) for r in db.execute("select payload_json from c2_ingest_outbox where status='confirmed'")]
    found = [(p, m) for p in payloads for m in p.get('messages', [])
             if m.get('source_message_key') == 'source:a0e4a43639119671e7c9c70c0ee76b229feacdd1']
    assert len(found) == 1
    payload, message = found[0]
    relative = 'artifacts/' + payload['evidence']['screenshot'].replace('\\', '/').split('/artifacts/')[1]
    images = [r for r in json.loads((incident/'before-send/index.json').read_text()) if r['archive_member'] == relative]
    assert len(images) == 1
    image_path = Path(images[0]['path'])
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == images[0]['sha256']
    evidence_dir = tmp_path_factory.mktemp('original-ocr')
    source = {"message_event_id": "pending-fixture", "source_message_key": message['source_message_key'],
        "origin_read_run_id": payload['read_run_id'], "original_text_sha256": hashlib.sha256(message['content'].encode()).hexdigest(),
        "effective_version": 0, "raw_payload": message['raw_payload'], "evidence": payload['evidence']}
    (evidence_dir/'source.json').write_text(json.dumps(source, ensure_ascii=False))
    script = evidence_dir/'read_original.py'
    script.write_text('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.wechat_ai_customer_service.adapters.historical_text_correction import build_original_image_proposal
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import run_ocr
source=json.loads(Path(sys.argv[2]).read_text())
result=build_original_image_proposal(image_bytes=Path(sys.argv[3]).read_bytes(),original=source,
 authorization={'conversation_id':'pending-fixture','binding_id':'pending-fixture','authorization_revision':'pending-fixture'},ocr_runner=run_ocr)
Path(sys.argv[4]).write_text(json.dumps(result,ensure_ascii=False))
''')
    result = subprocess.run([runtime, str(script), str(ROOT/'worker-client/omniauto-rpa'),
        str(evidence_dir/'source.json'), str(image_path), str(evidence_dir/'proposal.json')], capture_output=True, text=True, timeout=60)
    (evidence_dir/'ocr.stdout').write_text(result.stdout)
    (evidence_dir/'ocr.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    proposal = json.loads((evidence_dir/'proposal.json').read_text())
    assert proposal['corrected_text'] == '二手车'
    return source, proposal


@pytest.fixture
def correction_case(original_png_proposal, http_api, monkeypatch):
    monkeypatch.setattr(fixtures, 'client', http_api)
    worker, target = fixtures._setup_bound_conversation()
    source, request = copy.deepcopy(original_png_proposal)
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == target['conversation_id']))
        conversation = db.get(Conversation, target['conversation_id'])
        conversation.status = 'waiting_user_reply'
        current = db.get(Worker, worker['id'])
        current.run_status, current.inflight_flow_state = 'faulted', {}
        current.local_lock_summary = {'capabilities': {'historical_text_correction_version': 1}}
        event = MessageEvent(conversation_id=target['conversation_id'], binding_id=binding.id,
            lead_id=binding.lead_id, worker_id=worker['id'], rpa_session_key=binding.rpa_session_key,
            read_run_id=source['origin_read_run_id'], source_message_key=source['source_message_key'],
            dedupe_key='original-fixture', sender_role='customer', message_type='text', content='手车',
            item_state='completed', raw_payload=source['raw_payload'], evidence=source['evidence'])
        db.add(event); db.flush()
        request.update(conversation_id=target['conversation_id'], binding_id=binding.id, message_event_id=event.id,
            authorization_revision=token_for_revision(binding.id, int(binding.authorization_revision or 1)))
        request['proof_sha256'] = shared_adapter('historical_text_correction').correction_digest(request)
        db.commit()
    path = f"/api/workers/{worker['id']}/wechat/message-text-corrections"
    return worker, request, path


def submit(case):
    worker, request, path = case
    return fixtures.client.post(path, json=request, headers=fixtures._worker_headers(worker))


def test_original_image_is_corrected_once_while_faulted_without_new_work(correction_case):
    worker, payload, _ = correction_case
    result = submit(correction_case)
    assert result.status_code == 200, result.text
    saved = result.json()['data']
    assert saved['effective_text'] == '二手车' and saved['effective_version'] == 1
    again = submit(correction_case)
    assert again.status_code == 200, again.text
    assert again.json()['data']['correction_id'] == saved['correction_id']
    with SessionLocal() as db:
        event = db.get(MessageEvent, payload['message_event_id'])
        assert event.content == '手车'
        assert effective_versions(db, [event])[event.id]['text'] == '二手车'
        assert len(list(db.scalars(select(MessageTextCorrection)))) == 1
        assert not list(db.scalars(select(MessageBatch)))
        assert not list(db.scalars(select(ReplyAction)))
        current = db.get(Worker, worker['id'])
        assert current.run_status == 'faulted' and not current.inflight_flow_state


@pytest.mark.parametrize('damage', ['wrong_source', 'wrong_read', 'wrong_observation', 'wrong_original',
    'wrong_hash', 'outside_roi', 'swallowed_neighbour', 'wrong_anchor', 'capture_without_digest',
    'wrong_version', 'foreign_conversation', 'ordinary_ingest_mixed', 'wrong_ocr_text'])
def test_invalid_original_proof_preserves_original(correction_case, damage):
    _, request, _ = correction_case
    if damage == 'wrong_source': request['source_message_key'] += '-wrong'
    elif damage == 'wrong_read': request['original_read_run_id'] += '-wrong'
    elif damage == 'wrong_observation': request['original_observation_id'] += '-wrong'
    elif damage == 'wrong_original': request['original_text_sha256'] = '0' * 64
    elif damage == 'wrong_hash': request['proof']['image_sha256'] = '0' * 64
    elif damage == 'outside_roi': request['proof']['crop_rect'][0] = -1
    elif damage == 'swallowed_neighbour': request['proof']['bubble_rect'][3] = 600
    elif damage == 'wrong_anchor': request['proof']['anchors'][0]['observed_text'] += '伪'
    elif damage == 'capture_without_digest': request['proof']['provenance'] = 'capture_digest'
    elif damage == 'wrong_version': request['expected_effective_version'] = 1
    elif damage == 'foreign_conversation': request['conversation_id'] = 'foreign'
    elif damage == 'ordinary_ingest_mixed': request['messages'] = []
    elif damage == 'wrong_ocr_text': request['corrected_text'] = '三手车'
    request['proof_sha256'] = shared_adapter('historical_text_correction').correction_digest(request)
    response = submit(correction_case)
    assert response.status_code == (400 if damage == 'ordinary_ingest_mixed' else 422), response.text
    with SessionLocal() as db:
        assert not list(db.scalars(select(MessageTextCorrection)))
        assert db.get(MessageEvent, request['message_event_id']).content == '手车'


def test_retry_with_changed_bytes_is_not_accepted_as_a_duplicate(correction_case):
    import base64
    first = submit(correction_case)
    assert first.status_code == 200, first.text
    _, request, _ = correction_case
    request['image_base64'] = base64.b64encode(b'changed bytes with unchanged claimed digest').decode()
    again = submit(correction_case)
    assert again.status_code == 422, again.text
    with SessionLocal() as db:
        assert len(list(db.scalars(select(MessageTextCorrection)))) == 1
        assert db.get(MessageEvent, request['message_event_id']).content == '手车'


def test_migration_preserves_original_and_refuses_to_drop_confirmed_correction(correction_case, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from app.core.database import engine
    from test_migration_rollback_safety import _load_migration
    migration = _load_migration('20260918_0036_message_text_correction.py')
    with engine.begin() as connection:
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        before = list(connection.execute(text('SELECT * FROM message_events')).mappings())
        migration.downgrade()
        migration.upgrade()
        assert list(connection.execute(text('SELECT * FROM message_events')).mappings()) == before
    response = submit(correction_case)
    assert response.status_code == 200, response.text
    with engine.begin() as connection:
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        before = list(connection.execute(text('SELECT * FROM message_text_corrections')).mappings())
        with pytest.raises(RuntimeError, match='Cannot discard confirmed'):
            migration.downgrade()
        assert list(connection.execute(text('SELECT * FROM message_text_corrections')).mappings()) == before


@pytest.mark.parametrize('chunked', [False, True])
def test_near_four_mib_png_over_actual_http(correction_case, chunked):
    """Synthetic transport padding; not reported as unmodified incident PNG."""
    import base64, struct, zlib
    worker, request, path = correction_case
    original = base64.b64decode(request['image_base64'])
    size = 4 * 1024 * 1024 - 1024
    data = b'\0' * (size - len(original) - 12)
    kind = b'npAd'  # Private ancillary chunk: same pixels, distinct test bytes.
    chunk = struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    image = original[:-12] + chunk + original[-12:]
    assert len(image) == size
    request['image_base64'] = base64.b64encode(image).decode()
    request['proof'].update(provenance='capture_digest', image_sha256=hashlib.sha256(image).hexdigest())
    request['proof_sha256'] = shared_adapter('historical_text_correction').correction_digest(request)
    with SessionLocal() as db:
        event = db.get(MessageEvent, request['message_event_id'])
        event.evidence = {**event.evidence, 'screenshot_sha256': request['proof']['image_sha256'],
            'screenshot_digest_recorded_at': request['proof']['digest_recorded_at'],
            'screenshot_digest_provenance': 'capture_digest'}
        db.commit()
    encoded = json.dumps(request).encode()
    assert 5 * 1024 * 1024 < len(encoded) < 6 * 1024 * 1024
    if chunked:
        response = fixtures.client.post(path, data=(encoded[i:i+32768] for i in range(0,len(encoded),32768)),
            headers={**fixtures._worker_headers(worker), 'Content-Type':'application/json'})
    else:
        response = submit(correction_case)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        row = db.scalar(select(MessageTextCorrection))
        assert row.image_bytes == image
        assert 'image_base64' not in json.dumps(row.proof)
        assert db.get(MessageEvent, request['message_event_id']).content == '手车'


def test_chunked_correction_over_six_mib_never_reaches_storage(correction_case):
    worker, request, path = correction_case
    response = fixtures.client.post(path, data=(b' ' * 65536 for _ in range(97)),
        headers={**fixtures._worker_headers(worker), 'Content-Type':'application/json'})
    assert response.status_code == 413, response.text
    assert response.json()['code'] == 'HISTORICAL_TEXT_CORRECTION_TOO_LARGE'
    with SessionLocal() as db:
        assert not list(db.scalars(select(MessageTextCorrection)))


@pytest.mark.parametrize('state', ['generating', 'queued_second', 'unknown'])
def test_existing_work_is_invalidated_only_if_proven_unsent(correction_case, state):
    worker, request, _ = correction_case
    with SessionLocal() as db:
        batch = MessageBatch(conversation_id=request['conversation_id'], status='generating' if state == 'generating' else 'reply_action_created',
            active=state == 'generating', message_event_ids=[request['message_event_id']], message_count=1)
        db.add(batch); db.flush()
        if state != 'generating':
            first = ReplyAction(batch_id=batch.id, conversation_id=batch.conversation_id, status='sent', current=False,
                segment_index=1, segment_count=2, reply_text='已发第一段', reply_text_hash='a'*64)
            db.add(first); db.flush()
            second = ReplyAction(batch_id=batch.id, conversation_id=batch.conversation_id,
                status='unknown_send_result' if state == 'unknown' else 'queued', current=True,
                segment_index=2, segment_count=2, predecessor_reply_action_id=first.id,
                reply_text='未发第二段', reply_text_hash='b'*64)
            db.add(second); db.flush()
            db.add(Task(task_type='chat_reply', status='pending', worker_id=worker['id'],
                reply_action_id=second.id))
            first_id, second_id = first.id, second.id
        batch_id = batch.id
        db.commit()
    response = submit(correction_case)
    assert response.status_code == (409 if state == 'unknown' else 200), response.text
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        current = db.get(Worker, worker['id'])
        assert current.run_status == 'faulted' and not current.inflight_flow_state
        if state == 'unknown':
            assert not list(db.scalars(select(MessageTextCorrection)))
            assert batch.status == 'reply_action_created'
        else:
            assert batch.status == 'cancelled'
            binding = db.get(WechatSessionBinding, request['binding_id'])
            pending = binding.last_scan_snapshot['pre_send_read_pending']
            assert pending['message_event_ids'] == [request['message_event_id']]
            assert pending['cause'] == 'historical_text_correction'
            assert pending['effective_context_digest'] == effective_context_digest(db, batch.conversation_id)
            if state == 'queued_second':
                assert db.get(ReplyAction, first_id).status == 'sent'
                assert db.get(ReplyAction, second_id).status == 'cancelled'
