"""Version-only upgrades and already-finished reads through real HTTP/storage."""
import copy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from app.contracts.c2 import c2_contract_v3
from app.core.database import SessionLocal, engine
from app.models.audit import OperationLog
from app.models.base import utcnow
from app.models.c3 import ReplyAction
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from test_lead_followup_eligibility import client, fixture_rows, headers, isolated_db, http_api
from test_wechat_c2_api import _v3_ingest_payload, _v3_message


def contract_pair(revision, *, changed=False):
    contract = copy.deepcopy(c2_contract_v3())
    contract['contract_revision'] = revision
    if changed:
        contract['unreviewed_business_rule'] = True
    digest = hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return {'contract_revision': revision, 'contract_sha256': digest}


def use_contract(payload, pair):
    """Synthetic fixture only; production never rewrites stored evidence."""
    if isinstance(payload, dict):
        for key, value in list(payload.items()):
            if key in pair:
                payload[key] = pair[key]
            else:
                use_contract(value, pair)
    elif isinstance(payload, list):
        for item in payload:
            use_contract(item, pair)


def prepared_read(revision='0.9.78', *, closed=False, message_count=4, eligible=False):
    worker, rows = fixture_rows(eligible=eligible)
    row = rows[0]
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': binding.id, 'conversation_id': binding.conversation_id,
                  'rpa_session_key': binding.rpa_session_key, 'unread_generation': binding.unread_generation}
        remark = binding.remark_code
    flow = 'read-compatible-original'
    payload = _v3_ingest_payload(target, remark, read_run_id=flow,
        read_reason='waiting_user_reply' if eligible else 'waiting_sales_reply',
        messages=[_v3_message(f'original-message-{i}', role='customer', message_type='text',
                              content=f'测试消息 {i}', screen_order=i) for i in range(1, message_count + 1)])
    use_contract(payload, contract_pair(revision))
    path = f"/api/workers/{worker['id']}"
    response = client.post(path + '/inflight-flow/start', headers=headers(worker), json={
        'flow_id': flow, 'flow_kind': 'c2_read', 'conversation_id': row['conversation_id'],
        'unread_generation': payload['unread_generation'], 'authorization_revision': payload['authorization_revision']})
    assert response.status_code == 200, response.text
    if closed:
        response = client.post(path + '/run-status', headers=headers(worker),
            json={'client_instance_id': 'followup-test', 'run_status': 'faulted'})
        assert response.status_code == 200, response.text
        response = client.post(path + '/inflight-flow/finish', headers=headers(worker, flow), json={
            'flow_id': flow, 'terminal_kind': 'technical_failed', 'conversation_id': row['conversation_id'],
            'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
        assert response.status_code == 200, response.text
    return worker, row, payload


def post(worker, payload, *, flow_header=True):
    return client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload,
                       headers=headers(worker, payload['read_run_id'] if flow_header else None))


@pytest.mark.parametrize('revision', ['0.9.75', '0.9.78', '0.9.80', '0.10.12'])
def test_same_rules_are_accepted_without_a_per_release_whitelist(revision):
    worker, row, payload = prepared_read(revision)
    original = copy.deepcopy(payload)
    first = post(worker, payload)
    assert first.status_code == 200, first.text
    assert first.json()['data']['ingested_count'] == 4
    assert payload == original
    with SessionLocal() as db:
        messages = db.scalars(select(MessageEvent).where(MessageEvent.conversation_id == row['conversation_id'])).all()
        assert len(messages) == 4
        assert all(message.raw_payload['contract_revision'] == revision for message in messages)


@pytest.mark.parametrize('flow_header', [True, False])
def test_finished_version_rejection_can_deliver_once_and_remain_faulted(flow_header):
    worker, row, payload = prepared_read(closed=True)
    first = post(worker, payload, flow_header=flow_header)
    assert first.status_code == 200, first.text
    assert first.json()['data']['ingested_count'] == 4
    repeated = post(worker, payload, flow_header=flow_header)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['data']['duplicated_count'] == 4
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        assert owner.run_status == 'faulted' and not owner.inflight_flow_state
        assert db.scalar(select(func.count(MessageEvent.id)).where(MessageEvent.conversation_id == row['conversation_id'])) == 4
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 1
        assert db.scalar(select(func.count(ReplyAction.id)).where(ReplyAction.status.in_(['sending', 'sent']))) == 0


@pytest.mark.parametrize('corruption', ['changed_rule', 'wrong_hash', 'wrong_revision', 'wrong_raw_revision',
    'wrong_header', 'wrong_flow', 'wrong_customer', 'wrong_instance', 'rebound', 'no_finish',
    'other_failure', 'active_other_flow', 'new_authorization', 'new_generation', 'later_read', 'future_evidence', 'unknown_send'])
def test_recovery_rejects_changes_or_missing_original_ownership(corruption):
    worker, row, payload = prepared_read(closed=True)
    supplied_headers = headers(worker, payload['read_run_id'])
    if corruption == 'changed_rule': use_contract(payload, contract_pair('0.9.78', changed=True))
    if corruption == 'wrong_hash': payload['contract_sha256'] = '0' * 64
    if corruption == 'wrong_revision': payload['contract_revision'] = '0.9.80'
    if corruption == 'wrong_raw_revision': payload['messages'][0]['raw_payload']['contract_revision'] = '0.9.80'
    if corruption == 'wrong_header': supplied_headers['X-Inflight-Flow-Id'] = 'different-flow'
    if corruption == 'wrong_flow':
        payload = json.loads(json.dumps(payload).replace(payload['read_run_id'], 'never-registered'))
        supplied_headers['X-Inflight-Flow-Id'] = payload['read_run_id']
    if corruption == 'wrong_customer': payload['conversation_id'] = 'different-customer'
    if corruption == 'wrong_instance': supplied_headers['X-Client-Instance-Id'] = 'different-instance'
    if corruption == 'future_evidence': payload['evidence']['finished_at'] = (utcnow() + timedelta(days=1)).isoformat()
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        binding = db.get(WechatSessionBinding, row['binding_id'])
        finish = db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished'))
        if corruption == 'rebound': owner.bound_at = utcnow() + timedelta(seconds=1)
        if corruption == 'no_finish': db.delete(finish)
        if corruption == 'other_failure': finish.after_data = {**finish.after_data, 'error_code': 'UNRELATED_FAILURE'}
        if corruption == 'active_other_flow': owner.inflight_flow_state = {'flow_id': 'other', 'status': 'active'}
        if corruption == 'new_authorization': binding.authorization_revision += 1
        if corruption == 'new_generation': binding.unread_generation += 1
        if corruption == 'later_read':
            binding.last_read_run_id = 'later-read'; binding.last_read_completed_at = utcnow() + timedelta(seconds=1)
        if corruption == 'unknown_send':
            db.add(ReplyAction(batch_id='synthetic', conversation_id=row['conversation_id'],
                               claimed_by_worker_id=worker['id'], status='unknown_send_result'))
        db.commit()
    response = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=supplied_headers)
    assert response.status_code in {401, 409}, response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 0


def test_accepted_recovery_cannot_add_or_change_original_messages():
    worker, _, payload = prepared_read(closed=True)
    assert post(worker, payload).status_code == 200
    changed = copy.deepcopy(payload)
    changed['messages'][0]['content'] = 'different content'
    assert post(worker, changed).status_code == 409
    changed = copy.deepcopy(payload)
    changed['messages'].pop()
    changed['evidence']['observations'].pop()
    changed['evidence']['slot_ledger_states'].pop()
    changed['evidence']['sequence_alignment_evidence']['new_suffix_observation_ids'].pop()
    assert post(worker, changed).status_code == 409


def test_finished_read_recovers_all_original_partitions_with_duplicate_delivery():
    from chejin_worker_client.c2_outbox_recovery import split_ingest_payload
    worker, row, payload = prepared_read(closed=True, message_count=30)
    for message in payload['messages']:
        message['raw_payload']['voice_transcription_meta'] = {'transport_padding': 'x' * 80_000}
    parts = split_ingest_payload(payload)
    assert len(parts) >= 2
    for part in parts:
        response = post(worker, part)
        assert response.status_code == 200, response.text
        repeated = post(worker, part)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['data']['duplicated_count'] == len(part['messages'])
    assert response.json()['data']['ingest_partition']['complete'] is True
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id)).where(MessageEvent.conversation_id == row['conversation_id'])) == 30
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_closed_read_messages_recovered')) == len(parts)


WORKER_RECOVERY = r'''
import hashlib, json, sys, time
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import (save_binding, load_binding, enqueue_c2_outbox,
    mark_c2_outbox_capability_paused, save_c2_ledger_terminal, load_c2_outbox_entry,
    update_install_business_blockers)
req=json.loads(Path(sys.argv[1]).read_text())
payload=req['payload']
binding=Binding(req['worker']['id'],req['worker']['worker_token'],'followup-test',run_status='faulted')
save_binding(binding)
for item in payload['messages']:
    save_c2_ledger_terminal(conversation_id=payload['conversation_id'],source_message_key=item['source_message_key'],
        origin_read_run_id=payload['read_run_id'],dedupe_key=item['dedupe_key'],message_type=item['message_type'],
        terminal_state=item['item_state'],ingest_state='waiting')
outbox=enqueue_c2_outbox(payload)
mark_c2_outbox_capability_paused(outbox,'WORKER_INFLIGHT_FLOW_MISMATCH')
before=load_c2_outbox_entry(outbox)['payload']
api=WorkerApiClient(req['url']+'/api')
send=api.session.send
exchanges=[]
lost=False
def transport(request,**kwargs):
    global lost
    ingest=request.url.endswith('/messages/ingest')
    if ingest and req['mode']=='old_without_header':request.headers.pop('X-Inflight-Flow-Id',None)
    response=send(request,**kwargs)
    if ingest:
        exchanges.append({'status':response.status_code,'flow_header':request.headers.get('X-Inflight-Flow-Id')})
        if req['mode']=='response_lost' and response.status_code==200 and not lost:
            lost=True
            raise TimeoutError('controlled loss after server commit')
    return response
api.session.send=transport
physical=[]
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
bridge.sidecar_active=lambda:False
def forbidden(*args,**kwargs):
    physical.append('unexpected_ui')
    raise AssertionError('Stored message replay must not read, click or send')
for name in ('list_sessions','get_messages','locate_chat','send_reply','run_add_friend','prepare_voice_action',
             'execute_voice_action','transcribe_voice','prepare_image_action','execute_image_action'):
    if hasattr(bridge,name):setattr(bridge,name,forbidden)
noop=lambda *_:None
errors=[]
runner=TaskRunner(api,bridge,on_profile=noop,on_status=noop,on_step=noop,on_task=noop,
                  on_result=noop,on_error=errors.append,can_pull_tasks=lambda:False)
runner.start(binding)
try:
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        current=load_c2_outbox_entry(outbox)
        if current['status']=='confirmed' and runner.fault_recovery_state()['ready']:break
        time.sleep(.1)
    assert current['status']=='confirmed', {'state':current['status'],'errors':errors,'http':exchanges}
    assert current['payload']==before, 'Original message body was rewritten'
    assert runner.binding.run_status==load_binding().run_status=='faulted'
    assert runner.fault_recovery_state()['ready'],runner.fault_recovery_state()
    blockers=update_install_business_blockers()
    assert blockers['pending_c2_outbox']==0 and blockers['waiting_ledger']==0,blockers
    assert runner.set_run_status('running'),errors
    deadline=time.monotonic()+10
    while runner.binding.run_status!='running' and time.monotonic()<deadline:time.sleep(.1)
    assert runner.binding.run_status==load_binding().run_status=='running',errors
    assert not physical,physical
    print(json.dumps({'messages':len(payload['messages']),'outbox_status':current['status'],
          'original_payload_unchanged':True,'explicit_recovery':'running','physical_actions':physical,
          'http':exchanges,'response_loss_injected':lost,'pending_messages':blockers['pending_c2_outbox']}))
finally:
    runner.stop_for_update(timeout_seconds=5)
'''


@pytest.mark.parametrize('mode', ['normal', 'response_lost', 'old_without_header'])
def test_real_worker_start_replays_old_outbox_then_explicit_start_clears_fault(http_api, tmp_path, mode):
    worker, row, payload = prepared_read(closed=True)
    root = Path(__file__).resolve().parents[2]
    request = {'worker': worker, 'payload': payload, 'mode': mode,
               'url': http_api.get('/healthz').url.removesuffix('/healthz')}
    request_path = tmp_path / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False))
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / p) for p in
           ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa')),
           'CHEJIN_WORKER_HOME': str(tmp_path / 'client'), 'CHEJIN_RPA_MODE': 'mock',
           'CHEJIN_C2_ENABLED': 'true', 'CHEJIN_HEARTBEAT_INTERVAL': '0.1',
           'CHEJIN_TASK_POLL_INTERVAL': '0.1'}
    result = subprocess.run([sys.executable, '-c', WORKER_RECOVERY, str(request_path)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=35)
    (tmp_path / 'worker.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr[-3500:]
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence['response_loss_injected'] == (mode == 'response_lost')
    assert evidence['explicit_recovery'] == 'running' and not evidence['physical_actions']
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).run_status == 'running'
        assert db.scalar(select(func.count(MessageEvent.id)).where(MessageEvent.conversation_id == row['conversation_id'])) == 4
    (tmp_path / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Row locking requires isolated PostgreSQL')
@pytest.mark.parametrize('competing_payload', [False, True])
def test_concurrent_closed_read_retries_freeze_one_original_batch(http_api, competing_payload):
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    worker, row, payload = prepared_read(closed=True)
    other = copy.deepcopy(payload)
    if competing_payload:
        other['messages'][0]['content'] = '另一条测试消息'
        assert other != payload
    url = http_api.get('/healthz').url.removesuffix('/healthz') + f"/api/workers/{worker['id']}/wechat/messages/ingest"
    barrier = Barrier(2)
    def deliver(body):
        barrier.wait(timeout=5)
        response = requests.post(url, json=body, headers=headers(worker, payload['read_run_id']), timeout=10)
        return response.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deliver, [payload, other]))
    assert sorted(results) == ([200, 409] if competing_payload else [200, 200])
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id)).where(MessageEvent.conversation_id == row['conversation_id'])) == 4
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_type == 'worker_closed_read_messages_recovered')) == 1
