"""Actual correction HTTP/PG/SQLite; reuse archived OCR output, no new OCR claim."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import isolated_db, http_api
from test_historical_text_correction import correction_case, original_png_proposal
from test_c2_identity_gate_receipts import harness
from test_gate_only_outbox import setup_gate
from chejin_worker_client import storage, text_correction_outbox as correction
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from app.models.task import Task
from sqlalchemy import select
import test_c3_api as backend



def setup_runner(case, http_api, harness, monkeypatch):
    worker, request, _ = case
    runner, _, bridge, _, _ = setup_gate(harness)
    binding = Binding(worker['id'], worker['worker_token'], 'client-c3', run_status='faulted')
    runner.binding = binding
    storage.save_binding(binding)
    runner.api = WorkerApiClient(http_api.get('/healthz').url.removesuffix('/healthz')+'/api')
    runner._backend_confirmed_run_status = 'faulted'
    runner._restart_backend_probe_pending = False
    runner._restart_recovery_flow_id = ''
    runner._pending_run_status_sync = None
    bridge.sidecar_active = lambda: False
    runner.last_rpa_component_status, runner.last_wechat_status = 'ready', 'logged_in'
    monkeypatch.setattr(runner, 'post_update_runtime_health_snapshot', lambda: {'ready': True})
    def heartbeat():
        profile = runner.api.heartbeat(binding, running_status='idle', current_task=None,
            rpa_component_status='ready', wechat_status='logged_in',
            local_lock_summary={'capabilities': {'historical_text_correction_version': 1}})
        runner._backend_fault_recovery = profile.fault_recovery
        runner._recovery_heartbeat_at = time.monotonic()
        return profile.fault_recovery
    heartbeat()
    assert runner._check_fault_recovery()['ready']
    payload = copy.deepcopy(request)
    image = base64.b64decode(payload.pop('image_base64'))
    outbox = correction.enqueue(payload, image, binding)
    return runner, binding, bridge, heartbeat, outbox


def test_busy_http_instruction_agrees_with_worker_retry(correction_case, http_api, harness, monkeypatch, tmp_path):
    worker, request, path = correction_case
    runner, binding, bridge, heartbeat, outbox = setup_runner(correction_case, http_api, harness, monkeypatch)
    # Historical outstanding backend work is a precondition, never auto-settled by this test.
    with SessionLocal() as db:
        db.get(Worker, worker['id']).inflight_flow_state = {'status':'active', 'flow_id':'older-flow',
            'flow_kind':'c2_read', 'conversation_id':request['conversation_id']}
        db.commit()
    response = http_api.post(path, json=request, headers=backend._worker_headers(worker))
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'HISTORICAL_TEXT_CORRECTION_BUSY'
    assert not runner._replay_c2_outbox(binding)
    row = storage.load_c2_outbox_entry(outbox)
    assert row['status'] == 'retry_waiting'
    result = {'http':response.json(), 'outbox_status':row['status'], 'desktop_reads':len(bridge.message_reads),
              'desktop_sends':len(bridge.sent_replies)}
    (tmp_path/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert response.json()['data']['recovery_action'] == 'retry', result
    assert response.json()['data']['retryable'] is True, result


@pytest.mark.parametrize('prior_rejection', [False, True])
def test_closed_customer_is_not_global_block_after_old_rejection(
        correction_case, http_api, harness, monkeypatch, tmp_path, prior_rejection):
    worker, request, path = correction_case
    runner, binding, bridge, heartbeat, outbox = setup_runner(correction_case, http_api, harness, monkeypatch)
    with SessionLocal() as db:
        lead_id = db.get(WechatSessionBinding, request['binding_id']).lead_id
    def invalidate():
        result = http_api.post(f'/api/leads/{lead_id}/mark-invalid', json={'invalid_reason':'test_data'})
        assert result.status_code == 200, result.text
    first = None
    if prior_rejection:
        # Normal management operations change authorization while an old frozen proposal remains.
        # Do not mutate the queue, old request, binding identity or authorization by hand.
        invalidate()
        restored = http_api.post(f'/api/leads/{lead_id}/restore')
        assert restored.status_code == 200, restored.text
        first = http_api.post(path, json=request, headers=backend._worker_headers(worker))
        assert first.status_code == 422, first.text
        assert first.json()['data']['reason'] == 'current_authorization_changed', first.text
        assert runner._replay_c2_outbox(binding)
        assert storage.load_c2_outbox_entry(outbox)['status'] == 'correction_rejected'
    invalidate()
    # B is created through the real lead route, not manufactured as a successful claim.
    lead_b = backend._create_lead(name='客户B', phone='13800006789', remark_code='CJBTEST2')
    with SessionLocal() as db:
        tasks_b = list(db.scalars(select(Task).where(Task.lead_id == lead_b['id'], Task.status == 'pending')))
        assert len(tasks_b) == 1
    for _ in range(3):
        assert runner._replay_c2_outbox(binding)
        server = heartbeat()
    state = runner._check_fault_recovery()
    # Independent read-only diagnostic of what the same backend request can return now.
    # This rejection writes no correction and is NOT fed back to repair the local queue.
    diagnostic = http_api.post(path, json=request, headers=backend._worker_headers(worker))
    assert diagnostic.status_code == 422, diagnostic.text
    assert diagnostic.json()['data']['resolution']['kind'] == 'business_ended', diagnostic.text
    before_click = copy.deepcopy(storage.load_binding()).run_status
    runner._fault_recovery_state = state
    monkeypatch.setattr(runner, '_refresh_vision_credential', lambda _: True)
    admitted = runner.set_run_status('running')
    if admitted:
        runner._process_fault_recovery()
    probe = subprocess.run([sys.executable, '-c',
        "import json; from chejin_worker_client import storage,text_correction_outbox as c; "
        "b=storage.load_binding(); print(json.dumps({'db':str(storage.DB_FILE),'reason':c.recovery_block_reason(b),"
        "'waiting':len(storage.list_c2_outbox_waiting()),'run_status':b.run_status},ensure_ascii=False))"],
        env={**os.environ, 'CHEJIN_WORKER_HOME':str(storage.APP_DIR)},
        capture_output=True, text=True, timeout=20)
    assert probe.returncode == 0, probe.stderr
    restart = json.loads(probe.stdout)
    assert restart['db'] == str(storage.DB_FILE)
    assert bool(restart['reason']) == (not state['ready'])
    result = {'prior_rejection':prior_rejection, 'first_rejection':first.json() if first is not None else None,
        'current_backend_resolution':diagnostic.json(), 'backend_recovery':server,
        'local_recovery':state, 'start_admitted':admitted, 'binding_before_click':before_click,
        'binding_after_click':binding.run_status,
        'outbox_status':storage.load_c2_outbox_entry(outbox)['status'],
        'same_database_new_process':restart,
        'waiting_count':len(storage.list_c2_outbox_waiting()),
        'pending_B_tasks':len(tasks_b), 'desktop_reads':len(bridge.message_reads),
        'desktop_sends':len(bridge.sent_replies),
        'limits':'Original PNG plus archived OCR proposal reused; no OCR rerun/Windows. Window/thread health controlled ready.'}
    (tmp_path/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    assert server['ready'], result
    assert state['ready'], result
    assert admitted and binding.run_status == 'running', result
    _, task, _ = runner.api.pull_task(binding)
    assert task and task.id == tasks_b[0].id
    assert runner.api.claim_task(binding, task).id == task.id
    assert not bridge.message_reads and not bridge.sent_replies
    if prior_rejection:
        original = storage.load_c2_state(correction.RESULT_PREFIX + outbox)
        assert original['reason'] == 'HISTORICAL_TEXT_CORRECTION_REJECTED'
        assert 'resolution' not in original
        assert storage.load_c2_state(correction.RESOLUTION_PREFIX + outbox)['resolution']['kind'] == 'business_ended'



@pytest.mark.parametrize('defect',['active_business','invalid_proof','unsettled_send'])
def test_resolution_lookup_never_applies_correction_or_bypasses_guards(correction_case,http_api,defect):
    from app.models.message_text_correction import MessageTextCorrection
    from app.models.c3 import MessageBatch, ReplyAction
    from app.contracts.shared_rules import shared_adapter
    worker,request,path=correction_case
    with SessionLocal() as db:
        lead_id=db.get(WechatSessionBinding,request['binding_id']).lead_id
    if defect!='active_business':
        assert http_api.post(f'/api/leads/{lead_id}/mark-invalid',json={'invalid_reason':'test_data'}).status_code==200
    if defect=='invalid_proof':
        request['proof']['anchors'][0]['observed_text']+='wrong'
        request['proof_sha256']=shared_adapter('historical_text_correction').correction_digest(request)
    if defect=='unsettled_send':
        with SessionLocal() as db:
            batch=MessageBatch(conversation_id=request['conversation_id'],trigger_type='customer_message',
                trigger_key='pending-send',status='ready',active=False)
            db.add(batch);db.flush()
            db.add(ReplyAction(conversation_id=request['conversation_id'],batch_id=batch.id,
                status='unknown_send_result',current=False,reply_text='unconfirmed',reply_text_hash='a'*64,send_token='pending'))
            db.commit()
    result=http_api.post(path+'/resolution',json=request,headers=backend._worker_headers(worker))
    assert result.status_code==(409 if defect=='unsettled_send' else 422),result.text
    assert 'resolution' not in result.json()['data']
    with SessionLocal() as db:
        assert not list(db.scalars(select(MessageTextCorrection)))


@pytest.mark.parametrize('code,status,expected',[
    ('HISTORICAL_TEXT_CORRECTION_BUSY',409,'retry'),
    ('HISTORICAL_TEXT_CORRECTION_BUSY',422,'capability_paused'),
    ('UNKNOWN_TEST_ERROR',409,'capability_paused'),
    ('UNKNOWN_TEST_ERROR',503,'retry'),
    ('MESSAGE_CONTRACT_REVISION_MISMATCH',500,'capability_paused'),
])
def test_http_flags_and_worker_use_identical_recovery_rule(code,status,expected):
    from app.api.response import error_response
    from chejin_worker_client.api import ApiError
    from chejin_worker_client.transaction_outcomes import classify_outbox_recovery
    body=json.loads(error_response(status,code,'test',{'retryable':True}).body)
    assert body['data']['recovery_action']==expected
    assert body['data']['retryable'] is (expected=='retry')
    assert classify_outbox_recovery(ApiError(code,'test',status,body['data']))==expected
