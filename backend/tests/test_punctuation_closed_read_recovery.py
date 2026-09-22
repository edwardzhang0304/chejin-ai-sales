"""Frozen legacy mapping failures through HTTP/PG and the real SQLite recovery loop.

Frames are constructed inputs. No test supplies a correspondence proof or
marks a local Outbox confirmed; the production server must verify old text.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select, func
import test_c3_api as api
from test_pre_send_checkpoint_order import FrameInputHTTP, async_generation
from test_lead_followup_eligibility import http_api, isolated_db
from test_contract_equivalent_recovery import WORKER_RECOVERY
from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.models.task import Task
from app.models.c3 import Conversation, ReplyAction


@pytest.fixture
def closed_mapping(http_api, monkeypatch, async_generation):
    class LegacyFrame:
        enabled = False
        def get(self, *args, **kwargs):
            return http_api.get(*args, **kwargs)
        def post(self, path, **kwargs):
            if self.enabled and path.endswith('/messages/ingest'):
                body = deepcopy(kwargs['json'])
                body['evidence']['observations'][1]['content_clean'] = price.replace(',', '.')
                self.payload = deepcopy(body)
                flow = body['read_run_id']
                start = http_api.post(prefix + '/inflight-flow/start', headers=headers, json={
                    'flow_id': flow, 'flow_kind': 'c2_read', 'conversation_id': body['conversation_id'],
                    'unread_generation': body['unread_generation'],
                    'authorization_revision': body['authorization_revision']})
                assert start.status_code == 200, start.text
                kwargs.update(json=body, headers={**headers, 'X-Inflight-Flow-Id': flow})
            response = http_api.post(path, **kwargs)
            if self.enabled and path.endswith('/messages/ingest'):
                self.response = response
            return response

    transport = LegacyFrame()
    monkeypatch.setattr(api, 'client', FrameInputHTTP(transport))
    worker, target = api._setup_bound_conversation()
    prefix = f"/api/workers/{worker['id']}"
    headers = api._worker_headers(worker)
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        owner.local_lock_summary = {'capabilities': {'text_correspondence_version': 2}}
        conv = db.get(Conversation, target['conversation_id'])
        conv.status, conv.friend_state = 'waiting_user_reply', 'friend_active'
        for task in db.scalars(select(Task).where(Task.task_type == 'add_friend')):
            task.status = 'cancelled'
        db.commit()
    async_generation['suppress'] = True
    price = '198,000是换电的价格，还是买断电池包的价格？'
    originals = ['唯一开场', price, '唯一末句']
    keys = sorted([f'punctuation-{i}' for i in range(4)], key=lambda k: hashlib.sha256(k.encode()).hexdigest()[:12])
    for key, text in zip(keys, originals):
        api._ingest(worker, target['conversation_id'], key, text)
    transport.enabled = True
    with pytest.raises(AssertionError):
        api._ingest(worker, target['conversation_id'], keys[-1], '请继续介绍这款车')
    assert transport.response.status_code == 409, transport.response.text
    assert 'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE' in transport.response.text
    payload = transport.payload
    assert not payload['evidence']['sequence_alignment_evidence'].get('text_correspondence')
    fault = http_api.post(prefix + '/run-status', headers=headers,
        json={'client_instance_id': 'client-c3', 'run_status': 'faulted'})
    assert fault.status_code == 200, fault.text
    finish = http_api.post(prefix + '/inflight-flow/finish',
        headers={**headers, 'X-Inflight-Flow-Id': payload['read_run_id']}, json={
            'flow_id': payload['read_run_id'], 'terminal_kind': 'technical_failed',
            'conversation_id': target['conversation_id'], 'error_code': 'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE'})
    assert finish.status_code == 200, finish.text
    return worker, payload, originals


def replay(http_api, worker, payload):
    return http_api.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload,
        headers={**api._worker_headers(worker), 'X-Inflight-Flow-Id': payload['read_run_id']})


def test_frozen_mapping_failure_is_verified_and_replayed_idempotently(http_api, closed_mapping):
    worker, payload, originals = closed_mapping
    frozen = deepcopy(payload)
    for _ in range(2):
        response = replay(http_api, worker, payload)
        assert response.status_code == 200, response.text
    assert payload == frozen
    with SessionLocal() as db:
        events = list(db.scalars(select(MessageEvent).order_by(MessageEvent.ingested_at)))
        assert [e.content for e in events[:3]] == originals
        assert len(events) == 4
        assert db.get(Worker, worker['id']).run_status == 'faulted'
        assert db.scalar(select(func.count(OperationLog.id)).where(
            OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 1


@pytest.mark.parametrize('field', ['body', 'mapping', 'suffix'])
def test_accepted_recovery_receipt_cannot_be_reused_for_a_changed_frame(http_api, closed_mapping, field):
    worker, payload, _ = closed_mapping
    accepted = replay(http_api, worker, payload)
    assert accepted.status_code == 200, accepted.text
    if field == 'body':
        payload['evidence']['observations'][1]['content_clean'] = '另一句话'
    elif field == 'mapping':
        payload['evidence']['sequence_alignment_evidence']['matched_pairs'][1]['worker_stable_id'] = 'wrong-id'
    else:
        payload['messages'][0]['content'] = '改变新消息'
    changed = replay(http_api, worker, payload)
    assert changed.status_code in {400, 409, 422}, changed.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 4
        assert db.scalar(select(func.count(OperationLog.id)).where(
            OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 1


@pytest.mark.parametrize('damage', ['wrong_mapping', 'low_score', 'wrong_role', 'unknown_send', 'new_generation', 'other_failure'])
def test_closed_mapping_recovery_retains_original_guards(http_api, closed_mapping, damage):
    worker, payload, _ = closed_mapping
    if damage == 'wrong_mapping':
        payload['evidence']['sequence_alignment_evidence']['matched_pairs'][1]['worker_stable_id'] = 'wrong-id'
    if damage == 'low_score':
        payload['evidence']['observations'][1]['content_clean'] = '完全不同的内容'
    if damage == 'wrong_role':
        payload['evidence']['observations'][1]['sender_role'] = 'self'
    with SessionLocal() as db:
        if damage == 'unknown_send':
            db.add(ReplyAction(batch_id='synthetic', conversation_id=payload['conversation_id'],
                claimed_by_worker_id=worker['id'], status='unknown_send_result'))
        if damage == 'new_generation':
            binding = db.scalar(select(WechatSessionBinding).where(
                WechatSessionBinding.conversation_id == payload['conversation_id']))
            binding.unread_generation += 1
        if damage == 'other_failure':
            log = db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished'))
            log.after_data = {**log.after_data, 'error_code': 'UNRELATED_FAILURE'}
        db.commit()
    response = replay(http_api, worker, payload)
    assert response.status_code in {400, 409, 422}, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 3
        assert db.scalar(select(func.count(OperationLog.id)).where(
            OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 0


@pytest.mark.parametrize('mode', ['normal', 'response_lost'])
def test_real_worker_replays_then_explicit_start_recovers(http_api, closed_mapping, tmp_path, mode):
    worker, payload, _ = closed_mapping
    root = Path(__file__).resolve().parents[2]
    request = {'worker': worker, 'payload': payload, 'mode': mode,
        'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request, ensure_ascii=False))
    env = {**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_RPA_MODE': 'mock',
        'CHEJIN_C2_ENABLED': 'true', 'CHEJIN_HEARTBEAT_INTERVAL': '0.1', 'CHEJIN_TASK_POLL_INTERVAL': '0.1'}
    process = subprocess.run([sys.executable, '-c', WORKER_RECOVERY.replace('followup-test', 'client-c3'), str(path)],
        cwd=root, env=env, capture_output=True, text=True, timeout=35)
    (tmp_path / 'worker.log').write_text(process.stdout + process.stderr)
    assert process.returncode == 0, process.stderr[-5000:]
    result = json.loads(process.stdout.splitlines()[-1])
    assert result['explicit_recovery'] == 'running' and result['physical_actions'] == []
    assert result['response_loss_injected'] == (mode == 'response_lost')
    (tmp_path / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
