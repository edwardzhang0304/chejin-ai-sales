"""Independent examples; no source patch and no production access.

Correction HTTP, PostgreSQL, queue and SQLite are real. Window/thread health
is supplied as ready to isolate the recovery decision; this is not Windows.
"""
import base64
import copy
import json
from pathlib import Path
import sys
import time

import pytest
from sqlalchemy import select
from test_historical_text_correction import original_png_proposal, correction_case
from test_lead_followup_eligibility import isolated_db, http_api
from test_c2_identity_gate_receipts import harness
from test_gate_only_outbox import setup_gate
from app.core.database import SessionLocal
from app.models.wechat import WechatSessionBinding
from chejin_worker_client import storage, text_correction_outbox as correction
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from app.models.worker import Worker
from app.models.c3 import Conversation
from app.models.message_text_correction import MessageTextCorrection
from app.models.wechat import MessageEvent
from app.contracts.shared_rules import shared_adapter
from app.models.task import Task
import test_c3_api as fixtures


@pytest.mark.parametrize('invalidate', [False, True])
def test_settled_customer_correction_does_not_permanently_disable_start(
        correction_case, http_api, harness, monkeypatch, tmp_path, invalidate):
    worker, request, _ = correction_case
    runner, _, bridge, _, _ = setup_gate(harness)
    binding = Binding(worker['id'], worker['worker_token'], 'client-c3', run_status='faulted')
    runner.binding = binding
    storage.save_binding(binding)
    api = WorkerApiClient(http_api.get('/healthz').url.removesuffix('/healthz') + '/api')
    runner.api = api
    runner._backend_confirmed_run_status = 'faulted'
    runner._restart_backend_probe_pending = False
    runner._restart_recovery_flow_id = ''
    runner._pending_run_status_sync = None
    bridge.sidecar_active = lambda: False
    runner.last_rpa_component_status, runner.last_wechat_status = 'ready', 'logged_in'
    monkeypatch.setattr(runner, 'post_update_runtime_health_snapshot', lambda: {'ready': True})
    def heartbeat():
        profile = api.heartbeat(binding, running_status='idle', current_task=None,
            rpa_component_status='ready', wechat_status='logged_in',
            local_lock_summary={'capabilities': {'historical_text_correction_version': 1}})
        runner._backend_fault_recovery = profile.fault_recovery
        runner._recovery_heartbeat_at = time.monotonic()
        return profile.fault_recovery
    before = heartbeat()
    assert runner._check_fault_recovery()['ready'], runner._check_fault_recovery()
    payload = copy.deepcopy(request)
    image = base64.b64decode(payload.pop('image_base64'))
    outbox = correction.enqueue(payload, image, binding)
    assert not runner._check_fault_recovery()['ready']
    if invalidate:
        with SessionLocal() as db:
            lead_id = db.get(WechatSessionBinding, request['binding_id']).lead_id
        response = http_api.post(f'/api/leads/{lead_id}/mark-invalid', json={'invalid_reason': 'test_data'})
        assert response.status_code == 200, response.text
    # No fake HTTP result and no manually confirmed terminal.
    assert correction.replay_one(api, binding, storage.load_c2_outbox_entry(outbox))
    server = heartbeat()
    snapshot = runner.update_install_safety_snapshot()
    state = runner._check_fault_recovery()
    result = {'invalidated': invalidate, 'before_server': before, 'after_server': server,
        'outbox': storage.load_c2_outbox_entry(outbox),
        'receipt': storage.load_c2_state(correction.RESULT_PREFIX + outbox),
        'safety': snapshot, 'recovery': state}
    # Do not duplicate PNG/base64 in the diagnostic summary.
    result['outbox']['payload'].pop('request', None)
    (tmp_path/'recovery.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    assert not storage.has_pending_c2_outbox()
    assert snapshot['settlement_complete'], snapshot
    assert server['ready'], server
    assert not bridge.message_reads and not bridge.sent_replies
    assert state['ready'], state
    if invalidate:
        assert result['receipt']['outcome'] == 'rejected'
        assert result['receipt']['resolution']['kind'] == 'business_ended'
        with SessionLocal() as db:
            assert not list(db.scalars(select(MessageTextCorrection)))
            assert db.get(MessageEvent, request['message_event_id']).content == '手车'
        # Customer B is queued normally. The user click must actually change
        # both sides and admit B; merely setting ready=True is insufficient.
        lead_b = fixtures._create_lead(name='客户B', phone='13800006789', remark_code='CJBTEST2')
        with SessionLocal() as db:
            # The real lead endpoint already assigns B and creates its task.
            # Do not manufacture an extra task just for the test.
            tasks_b = list(db.scalars(select(Task).where(Task.lead_id == lead_b['id'], Task.status == 'pending')))
            assert len(tasks_b) == 1
            task_b_id = tasks_b[0].id
    runner._fault_recovery_state = state
    monkeypatch.setattr(runner, '_refresh_vision_credential', lambda _: True)
    assert runner.set_run_status('running')
    runner._process_fault_recovery()
    assert binding.run_status == 'running', runner.fault_recovery_state()
    assert storage.load_binding().run_status == 'running'
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).run_status == 'running'
    if invalidate:
        _, task, _ = api.pull_task(binding)
        assert task and task.id == task_b_id
        claimed = api.claim_task(binding, task)
        assert claimed.id == task_b_id
        targets = api.get_wechat_read_targets(binding)
        assert request['conversation_id'] not in {t.conversation_id for t in targets}
    assert not bridge.message_reads and not bridge.sent_replies


@pytest.mark.parametrize('defect', ['invalid_proof', 'active_flow', 'unknown_send'])
def test_business_end_does_not_hide_invalid_proof_or_unsettled_work(correction_case, http_api, defect):
    from app.models.c3 import ReplyAction, MessageBatch
    worker, payload, path = correction_case
    with SessionLocal() as db:
        lead_id = db.get(WechatSessionBinding, payload['binding_id']).lead_id
    assert http_api.post(f'/api/leads/{lead_id}/mark-invalid', json={'invalid_reason': 'test_data'}).status_code == 200
    if defect == 'invalid_proof':
        payload['proof']['anchors'][0]['observed_text'] += '错误'
        payload['proof_sha256'] = shared_adapter('historical_text_correction').correction_digest(payload)
    else:
        with SessionLocal() as db:
            if defect == 'active_flow':
                db.get(Worker, worker['id']).inflight_flow_state = {'flow_id': 'unsettled-original',
                    'status': 'active', 'flow_kind': 'c2_read', 'conversation_id': payload['conversation_id']}
            else:
                batch = MessageBatch(conversation_id=payload['conversation_id'], trigger_type='customer_message',
                    trigger_key='unsettled-fixture', status='ready', active=False)
                db.add(batch); db.flush()
                db.add(ReplyAction(conversation_id=payload['conversation_id'], batch_id=batch.id,
                    status='unknown_send_result', current=False, reply_text='结果未确认',
                    reply_text_hash='a'*64, send_token='original-unknown-token'))
            db.commit()
    response = http_api.post(path, json=payload, headers=fixtures._worker_headers(worker))
    assert response.status_code == (422 if defect == 'invalid_proof' else 409), response.text
    assert 'resolution' not in response.json()['data']
    with SessionLocal() as db:
        assert not list(db.scalars(select(MessageTextCorrection)))
        assert db.get(MessageEvent, payload['message_event_id']).content == '手车'
