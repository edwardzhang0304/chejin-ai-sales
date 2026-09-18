"""A settled unknown remains no-resend, but must not block closing an obsolete correction."""
import base64
import copy
import json
import time
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_historical_text_correction import original_png_proposal, correction_case
from test_lead_followup_eligibility import isolated_db, http_api
from test_c2_identity_gate_receipts import harness
from test_gate_only_outbox import setup_gate
import test_c3_api as fixtures
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, SentAck
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.models.task import Task
from app.models.base import utcnow
from app.models.message_text_correction import MessageTextCorrection
from app.models.wechat import MessageEvent
from app.services import c3_service, worker_service
from chejin_worker_client import storage, text_correction_outbox as correction
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding


def _settle_send(http_api, worker, payload, send_result):
    # The shared fixture binds an existing friend directly, leaving its
    # auto-created add-friend task pending. Cancel only that fixture artifact
    # through the real admin route before testing the later chat send.
    with SessionLocal() as db:
        lead = db.get(WechatSessionBinding, payload['binding_id']).lead_id
        initial_tasks = list(db.scalars(select(Task.id).where(
            Task.lead_id == lead, Task.task_type == 'add_friend', Task.status == 'pending')))
    for task_id in initial_tasks:
        cancelled = http_api.post(f'/api/tasks/{task_id}/cancel',
            json={'reason': 'Existing friend in shared test fixture'})
        assert cancelled.status_code == 200, cancelled.text
    headers = fixtures._worker_headers(worker)
    started = http_api.post(f"/api/workers/{worker['id']}/run-status", headers=headers,
        json={'client_instance_id':'client-c3', 'run_status':'running', 'recover_from_fault':True})
    assert started.status_code == 200, started.text
    generated = fixtures._generate(fixtures._collect(payload['conversation_id'], payload['message_event_id'])['batch_id'])
    tid, aid = generated['task_id'], generated['reply_action_id']
    claim = http_api.post(f'/api/tasks/{tid}/claim', headers=headers, json={
        'worker_id':worker['id'], 'claim_source':'c2_conversation_flow', 'conversation_id':payload['conversation_id']})
    assert claim.status_code == 200, claim.text
    send = http_api.post(f'/api/reply-actions/{aid}/claim-send', headers=fixtures._task_lease_headers(worker, claim),
        json={'task_id':tid, 'worker_id':worker['id']})
    assert send.status_code == 200, send.text
    permit = send.json()['data']
    ack = http_api.post(f'/api/reply-actions/{aid}/sent-ack', headers=headers, json={
        'task_id':tid, 'worker_id':worker['id'], 'client_instance_id':'client-c3',
        'send_token':permit['send_token'], 'reply_text_hash':permit['reply_text_hash'],
        'send_result':send_result, 'action_phase':'confirmed' if send_result == 'sent' else 'trigger_attempted',
        'error_code':None if send_result == 'sent' else 'SEND_RESULT_UNKNOWN'})
    assert ack.status_code == 200, ack.text
    with SessionLocal() as db:
        task = db.get(Task, tid)
        assert task.status == ("completed" if send_result == "sent" else "failed")
        assert task.lease_owner_worker_id is None and task.lease_expires_at is None
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id == aid)) is not None
    return tid, aid, ack


def _end_business(http_api, worker, payload, ended):
    if ended == "invalid":
        with SessionLocal() as db:
            lead = db.get(WechatSessionBinding, payload["binding_id"]).lead_id
        response = http_api.post(f"/api/leads/{lead}/mark-invalid", json={"invalid_reason": "test_data"})
        assert response.status_code == 200, response.text
    else:
        with SessionLocal() as db:
            c3_service.create_deterministic_handoff_for_ingest(db,
                conversation_id=payload["conversation_id"], message_event_ids=[],
                reason_codes=["CUSTOMER_HIGH_INTENT"], trigger_key="closed-unknown-test")
            db.commit()

@pytest.mark.parametrize('ended', ['invalid', 'handoff'])
@pytest.mark.parametrize('send_result', ['sent', 'unknown'])
def test_formally_settled_old_send_does_not_block_business_closure(
        correction_case, http_api, harness, monkeypatch, tmp_path, send_result, ended):
    worker, payload, path = correction_case
    tid, aid, ack = _settle_send(http_api, worker, payload, send_result)
    _end_business(http_api, worker, payload, ended)
    headers = fixtures._worker_headers(worker)
    stopped = http_api.post(f"/api/workers/{worker['id']}/run-status", headers=headers,
        json={'client_instance_id':'client-c3', 'run_status':'faulted'})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()['data']['fault_recovery']['ready'], stopped.text
    response = http_api.post(path, json=payload, headers=headers)
    runner, _, bridge, _, _ = setup_gate(harness)
    binding = Binding(worker['id'], worker['worker_token'], 'client-c3', run_status='faulted')
    storage.save_binding(binding)
    runner.binding = binding
    api = WorkerApiClient(http_api.get('/healthz').url.removesuffix('/healthz') + '/api')
    runner.api = api
    runner._backend_confirmed_run_status = 'faulted'
    runner._restart_backend_probe_pending = False
    runner._restart_recovery_flow_id = ''
    runner._pending_run_status_sync = None
    runner._backend_fault_recovery = stopped.json()['data']['fault_recovery']
    runner._recovery_heartbeat_at = time.monotonic()
    runner.last_rpa_component_status, runner.last_wechat_status = 'ready', 'logged_in'
    bridge.sidecar_active = lambda: False
    monkeypatch.setattr(runner, 'post_update_runtime_health_snapshot', lambda: {'ready':True})
    assert runner._check_fault_recovery()['ready'], runner._check_fault_recovery()
    queued = copy.deepcopy(payload)
    image = base64.b64decode(queued.pop('image_base64'))
    outbox = correction.enqueue(queued, image, binding)
    finished = correction.replay_one(api, binding, storage.load_c2_outbox_entry(outbox))
    state = runner._check_fault_recovery()
    (tmp_path/'closure.json').write_text(json.dumps({'outcome':send_result, 'ack':ack.json(),
        'recovery':stopped.json()['data']['fault_recovery'], 'http_status':response.status_code,
        'closure':response.json(), 'local_recovery':state, 'finished':finished,
        'outbox_status':storage.load_c2_outbox_entry(outbox)['status'],
        'pending_outbox':storage.has_pending_c2_outbox()}, ensure_ascii=False, indent=2))
    assert finished and state['ready'], {'finished':finished, 'recovery':state, 'http':response.text}
    assert response.status_code == 422, response.text
    assert response.json()['data']['resolution']['kind'] == 'business_ended', response.text

    assert not storage.has_pending_c2_outbox()
    receipt = storage.load_c2_state(correction.RESULT_PREFIX + outbox)
    assert receipt['outcome'] == 'rejected'
    assert binding.run_status == 'faulted'  # Closing a proposal is not an auto-start.
    assert not bridge.message_reads and not bridge.sent_replies
    lead_b = fixtures._create_lead(name='客户B', phone='13800006789', remark_code='CJBTEST2')
    with SessionLocal() as db:
        tasks_b = list(db.scalars(select(Task).where(Task.lead_id == lead_b['id'], Task.status == 'pending')))
        assert len(tasks_b) == 1
        task_b_id = tasks_b[0].id
    runner._fault_recovery_state = state
    monkeypatch.setattr(runner, '_refresh_vision_credential', lambda _: True)
    assert runner.set_run_status('running')
    runner._process_fault_recovery()
    assert binding.run_status == storage.load_binding().run_status == 'running'
    _, task, _ = api.pull_task(binding)
    assert task and task.id == task_b_id
    assert api.claim_task(binding, task).id == task_b_id
    if ended == 'invalid':
        assert payload['conversation_id'] not in {t.conversation_id for t in api.get_wechat_read_targets(binding)}
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).run_status == 'running'
        if ended == 'handoff':
            # Handoff keeps passive sales-message listening, not AI sending.
            assert db.get(Conversation, payload['conversation_id']).status == 'waiting_sales_reply'
        assert db.get(ReplyAction, aid).status == ('sent' if send_result == 'sent' else 'unknown_send_result')
        assert db.get(Task, tid).status == ('completed' if send_result == 'sent' else 'failed')
        assert len(list(db.scalars(select(SentAck).where(SentAck.reply_action_id == aid)))) == 1
        assert db.get(MessageEvent, payload['message_event_id']).content == '手车'
        assert not list(db.scalars(select(MessageTextCorrection)))
    assert not bridge.message_reads and not bridge.sent_replies


@pytest.mark.parametrize('damage', ['missing_ack', 'wrong_token', 'wrong_task_link',
    'wrong_claimed_task', 'wrong_ack_worker', 'wrong_ack_result',
    'nonterminal_task', 'lease_owner', 'expired_lease_not_released', 'sending'])
def test_unsettled_or_mismatched_unknown_still_blocks_closure(correction_case, http_api, damage):
    worker, payload, path = correction_case
    tid, aid, _ = _settle_send(http_api, worker, payload, 'unknown')
    _end_business(http_api, worker, payload, 'invalid')
    # The positive is settled exclusively through HTTP. Break one persisted
    # proof fact here to verify the existing receipt gate is still mandatory.
    with SessionLocal() as db:
        task, action = db.get(Task, tid), db.get(ReplyAction, aid)
        ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == aid))
        if damage == 'missing_ack': db.delete(ack)
        elif damage == 'wrong_token': ack.send_token += '-wrong'
        elif damage == 'wrong_task_link': task.reply_action_id = None
        elif damage == 'wrong_claimed_task': action.claimed_task_id = 'wrong-original-task'
        elif damage == 'wrong_ack_worker': ack.worker_id = 'wrong-original-worker'
        elif damage == 'wrong_ack_result': ack.send_result = 'failed'
        elif damage == 'nonterminal_task': task.status = 'pending'
        elif damage == 'lease_owner': task.lease_owner_worker_id = worker['id']
        elif damage == 'expired_lease_not_released': task.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif damage == 'sending': action.status = 'sending'
        db.commit()
        assert not worker_service.fault_recovery_readiness(db, db.get(Worker, worker['id']))['ready']
    response = http_api.post(path, json=payload, headers=fixtures._worker_headers(worker))
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'HISTORICAL_TEXT_CORRECTION_BUSY'
    assert 'resolution' not in response.json()['data']
    with SessionLocal() as db:
        assert not list(db.scalars(select(MessageTextCorrection)))
        assert db.get(MessageEvent, payload['message_event_id']).content == '手车'
