"""Original .75 processes -> repaired backend and Worker, same PostgreSQL/SQLite.

Run with CHEJIN_OLD_075_ROOT pointing at release cd8763e's complete checkout.
The desktop/model are controlled boundaries. No contract, Outbox, receipt or
Flow is rewritten by the test. This is source integration, not Windows EXE UAT.
"""
import hashlib
import copy
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
import requests
from sqlalchemy import select, func

from test_lead_followup_eligibility import isolated_db, fixture_rows, headers
from test_c2_historical_ocr_settlement import WORKER, OLD_TEXT, NEW_QUESTION
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.worker import Worker
from app.models.sales import Sales
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.c3 import Conversation, HandoffEvent, ReplyAction
from app.models.task import Task

ROOT = Path(__file__).resolve().parents[2]
OLD_SHA = 'cd8763ed38ec1df1ff8054e49c3381e8a5322f62'
SERVER = '''
import os, json
from pathlib import Path
from fastapi import BackgroundTasks
import uvicorn
from app.main import app
from app.core.config import get_settings
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from app.api.routes import wechat
if os.environ.get('TEST_DISABLE_LEGACY_COMPAT') == '1':
    from app.services import read_recovery_service
    read_recovery_service.select_settlement_contract = lambda db, worker, payload: None
out=Path(os.environ['TEST_MODEL_EVENTS'])
def record(event):
    with out.open('a') as f: f.write(json.dumps({'event':event})+'\\n')
class ControlledModel:
    def generate_reply_decision(self, **kwargs):
        record('provider')
        return AIEngineDecision(decision='send_reply',reply_text='您好，请问您的预算是多少？',confidence=.95,guard_result='pass',evidence_refs=[],risk_flags=[],raw_payload={'adapter':'controlled'})
get_settings().c3_ai_adapter_mode='real'
c3_service.get_ai_engine_adapter=ControlledModel
add=BackgroundTasks.add_task
def schedule(self, fn, *args, **kwargs):
    if fn is wechat._generate_message_batch:
        record('scheduled')
        if os.environ.get('TEST_DISABLE_AUTOMATIC') == '1': return
    return add(self,fn,*args,**kwargs)
BackgroundTasks.add_task=schedule
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['TEST_PORT']),log_level='error',lifespan='off')
'''


@contextmanager
def backend_process(source, folder, label, *, reject_legacy=False):
    script = folder / 'server.py'
    script.write_text(SERVER)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PYTHONPATH': str(source / 'backend'),
           'C3_OMNIAUTO_ROOT': str(source / 'worker-client/omniauto-rpa'),
           'TEST_PORT': str(port), 'TEST_MODEL_EVENTS': str(folder / (label + '-model.jsonl'))}
    if label == 'old':
        env.pop('TEST_DISABLE_LEGACY_COMPAT', None)
        env.pop('TEST_DISABLE_AUTOMATIC', None)
    if reject_legacy:
        env['TEST_DISABLE_LEGACY_COMPAT'] = '1'
    with (folder / (label + '-server.log')).open('w') as log:
        process = subprocess.Popen([sys.executable, str(script)], env=env, cwd=source, stdout=log, stderr=log)
        url = f'http://127.0.0.1:{port}'
        try:
            for _ in range(200):
                assert process.poll() is None, (folder / (label + '-server.log')).read_text()
                try:
                    if requests.get(url + '/healthz', timeout=.3).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.05)
            else:
                pytest.fail('isolated backend did not start')
            yield url
        finally:
            process.terminate()
            process.wait(timeout=15)


def _replace_worker_fragment(script, old, new):
    # These edits instrument the real Worker driver. A changed upstream driver
    # must fail setup, not silently omit a UI guard or response-loss injection.
    count = script.count(old)
    assert count == 1, f"Worker fixture anchor must occur once; got {count}: {old!r}"
    return script.replace(old, new, 1)


@pytest.mark.parametrize("script", ["missing", "anchor anchor"])
def test_worker_fixture_rejects_missing_or_ambiguous_instrumentation(script):
    with pytest.raises(AssertionError, match="Worker fixture anchor must occur once"):
        _replace_worker_fragment(script, "anchor", "instrumented")


@pytest.mark.parametrize('transport', ['normal', 'ingest_response_lost', 'finish_response_lost', 'backend_rejects_then_recovers'])
def test_original_075_pending_read_survives_upgrade_and_changed_desktop(tmp_path, transport):
    configured = os.environ.get('CHEJIN_OLD_075_ROOT')
    if not configured:
        pytest.skip('requires the original complete .75 release checkout; never substitute current code')
    old = Path(configured)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=old, text=True).strip() == OLD_SHA
    worker, rows = fixture_rows()
    row, other = rows
    conv = row['conversation_id']
    with SessionLocal() as db:
        sales = Sales(sales_name='Synthetic recovery sales', phone='13800009992', worker_id=worker['id'], enabled=True)
        db.add(sales); db.flush()
        db.get(Conversation, conv).sales_id = sales.id
        db.get(WechatSessionBinding, row['binding_id']).sales_id = sales.id
        db.commit()
    # All desktop entry points fail if recovery tries to use the changed list.
    script = WORKER
    script = _replace_worker_fragment(script, 'runner.binding = binding', '''runner.binding = binding
live_recovered = None
bridge.current_conversation_id = "customer-B"
bridge.current_list_order = ["customer-B", "customer-A"]
ui_attempts = []
if mode.startswith('restart'):
    def forbidden_ui(*args, **kwargs):
        ui_attempts.append('unexpected_desktop_operation')
        raise AssertionError('Recovery must not inspect/click/type/send in changed desktop')
    for name in ('get_messages', 'list_sessions', 'locate_chat', 'send_reply', 'run_add_friend',
                 'prepare_startup_layout_for_new_transaction', 'verify_startup_layout_for_inflight_transaction',
                 'prepare_voice_action', 'execute_voice_action', 'transcribe_voice', 'prepare_image_action', 'execute_image_action'):
        if hasattr(bridge, name):
            setattr(bridge, name, forbidden_ui)
''')
    script = _replace_worker_fragment(script, 'with db_connection() as conn:', '''if mode == 'old_fault':
    assert runner.set_run_status('faulted')
with db_connection() as conn:''')
    script = _replace_worker_fragment(script, '            runner.stop_for_update(timeout_seconds=5)', '''            live_recovered = {'can_start': runner._can_start_new_flow(), 'status': runner.binding.run_status,
                              'saved_status': load_binding().run_status, 'flow_id': load_runtime_control()['inflight_flow_id']}
            runner.stop_for_update(timeout_seconds=5)''')
    script = _replace_worker_fragment(script, "'/read-targets'))", "'/read-targets', '/tasks/pull', '/claim'))")
    script = _replace_worker_fragment(script, '    response = send(request, **kwargs)', '''    response = send(request, **kwargs)
    lost_ingest = mode == 'restart_ingest_response_lost' and request.url.endswith('/messages/ingest')
    lost_finish = mode == 'restart_finish_response_lost' and request.url.endswith('/inflight-flow/finish')
    if (lost_ingest or lost_finish) and response.status_code == 200 and not injections:
        exchanges.append({'url': request.url, 'status': response.status_code, 'request': body, 'response': response.json(), 'response_lost': True})
        injections.append('accepted_response_lost')
        raise TimeoutError('controlled response loss AFTER backend accepted original request')
''')
    script = _replace_worker_fragment(script, "    if mode.startswith('restart'):", """    if mode == 'handoff':
        from chejin_worker_client.pending_read_recovery import package_recovery_capability
        bridge.sidecar_active = lambda: False  # No desktop process in this isolated boundary.
        runner.set_update_new_work_gate(True, update_request_id='recovery-test')
        before_retry = {'runtime': load_runtime_control(), 'status': binding.run_status}
        result = runner.update_pending_read_handoff_snapshot({'pending_read_recovery': package_recovery_capability()})
        runner.set_update_new_work_gate(False)
    elif mode.startswith('restart'):""")
    script = _replace_worker_fragment(script, "'physical_sends': len(bridge.sent_replies)", "'live_recovered': live_recovered, 'ui_attempts': ui_attempts, 'desktop': {'current': bridge.current_conversation_id, 'order': bridge.current_list_order}, 'physical_sends': len(bridge.sent_replies)")
    (tmp_path / 'worker.py').write_text(script)

    def run(source, url, label, frame, mode):
        path = tmp_path / (label + '-frame.json')
        path.write_text(json.dumps(frame, ensure_ascii=False))
        env = {**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path / 'data'), 'CHEJIN_RPA_MODE': 'mock',
               'CHEJIN_UI_LOCK_LEASE_SECONDS': '1', 'PYTHONPATH': os.pathsep.join(str(source / p) for p in
                   ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa'))}
        proc = subprocess.run([sys.executable, str(tmp_path / 'worker.py'), url, json.dumps(worker), conv, str(path), mode],
                              env=env, cwd=source, capture_output=True, text=True, timeout=45)
        (tmp_path / (label + '.stdout')).write_text(proc.stdout)
        (tmp_path / (label + '.stderr')).write_text(proc.stderr)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        (tmp_path / (label + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    history = {'frame_id': 'old-seed', 'messages': [{'id': 'old-self', 'type': 'text', 'sender_role': 'self', 'content': OLD_TEXT}]}
    frame = {'frame_id': 'old-current', 'messages': [
        {**history['messages'][0], 'id': 'current-self', 'content': OLD_TEXT.replace('ZX 2026', 'ZX2026')},
        {'id': 'new-customer-question', 'type': 'text', 'sender_role': 'customer', 'content': NEW_QUESTION}]}
    with backend_process(old, tmp_path, 'old') as url:
        seed = run(old, url, 'seed', history, 'seed')
        assert seed['result']['ok'], seed
        with SessionLocal() as db:
            original = db.scalar(select(MessageEvent).where(MessageEvent.conversation_id == conv))
            original_identity = (original.id, original.source_message_key, original.content)
            # Set up the next customer turn, never alter facts, Outbox or Flow.
            for handoff in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id == conv)):
                handoff.deleted_at = utcnow()
            db.get(Conversation, conv).status = 'waiting_user_reply'
            binding = db.get(WechatSessionBinding, row['binding_id'])
            binding.last_read_conversation_status = 'waiting_user_reply'
            binding.next_read_due_at = utcnow() - timedelta(seconds=1)
            db.commit()
        stuck = run(old, url, 'stuck-075', frame, 'old_fault')
        assert stuck['runtime']['inflight_flow_id'] and stuck['status'] == 'faulted', stuck
        assert any(e['status'] == 409 and e['response']['code'] == 'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE'
                   for e in stuck['exchanges'] if e['url'].endswith('/messages/ingest')), stuck
        assert any(e['status'] == 409 for e in stuck['exchanges'] if e['url'].endswith('/inflight-flow/finish')), stuck
    with SessionLocal() as db:
        before_flow = dict(db.get(Worker, worker['id']).inflight_flow_state)
        assert 'contract_revision' not in before_flow
    def original_receipt():
        with sqlite3.connect(tmp_path / 'data/worker_client.sqlite3') as db:
            return db.execute('SELECT value FROM c2_runtime_state WHERE key=?', ('inflight_finish_receipt:' + before_flow['flow_id'],)).fetchone()[0]
    if transport == 'backend_rejects_then_recovers':
        receipt_before = original_receipt()
        with backend_process(ROOT, tmp_path, 'strict', reject_legacy=True) as url:
            rejected = run(ROOT, url, 'backend-rejected', {}, 'restart')
        assert rejected['runtime']['inflight_flow_id'] == before_flow['flow_id'], rejected
        assert original_receipt() == receipt_before
        assert rejected['status'] == rejected['saved_status'] == 'faulted'
        assert not any(e['url'].endswith('/inflight-flow/finish') for e in rejected['exchanges'])
        assert rejected['reads'] == rejected['physical_sends'] == 0
        with SessionLocal() as db:
            assert db.get(Worker, worker['id']).inflight_flow_state['flow_id'] == before_flow['flow_id']
    # Exact original database survives process replacement. No state conversion.
    with backend_process(ROOT, tmp_path, 'new') as url:
        status = requests.post(url + f"/api/workers/{worker['id']}/run-status", headers=headers(worker), json={'run_status': 'faulted', 'client_instance_id': 'followup-test'}, timeout=10)
        assert status.status_code == 200, status.text
        assert status.json()['data']['pending_read_recovery']['ready']
        # Corrupt requests, not the durable original. All go through real HTTP.
        original_request = next(e['request'] for e in stuck['exchanges'] if e['url'].endswith('/messages/ingest'))
        protections = []
        for mutation in ('other_customer', 'other_flow', 'wrong_sha', 'wrong_version', 'nested_revision', 'changed_history'):
            body = copy.deepcopy(original_request)
            if mutation == 'other_customer': body['conversation_id'] = other['conversation_id']
            if mutation == 'other_flow':
                body['read_run_id'] = 'read-other'
                body['evidence']['read_run_id'] = 'read-other'
                for slot in body['evidence']['slot_ledger_states']:
                    if slot['fact_scope'] != 'historical': slot['origin_read_run_id'] = 'read-other'
            if mutation == 'wrong_sha': body['contract_sha256'] = '0' * 64
            if mutation == 'wrong_version': body['contract_revision'] = '0.9.74'
            if mutation == 'nested_revision': body['messages'][0]['raw_payload']['contract_revision'] = '0.9.76'
            if mutation == 'changed_history':
                historical = next(s for s in body['evidence']['slot_ledger_states'] if s['fact_scope'] == 'historical')
                observation = next(o for o in body['evidence']['observations'] if o['observation_id'] == historical['observation_id'])
                observation['content_clean'] = '售价99.9万，其他车辆'
            rejected = requests.post(url + f"/api/workers/{worker['id']}/wechat/messages/ingest", headers=headers(worker, before_flow['flow_id']), json=body, timeout=10)
            assert rejected.status_code == 409, (mutation, rejected.text)
            protections.append({'mutation': mutation, 'status': rejected.status_code, 'code': rejected.json()['code']})
        (tmp_path / 'rejected-requests.json').write_text(json.dumps(protections, indent=2))
        handed = run(ROOT, url, 'handoff', {}, 'handoff')
        assert handed['result']['safe'] and not handed['result']['settlement_complete'], handed
        assert handed['result']['pending_read_handoff']['flow_id'] == before_flow['flow_id']
        assert handed['reads'] == handed['physical_sends'] == 0
        after = run(ROOT, url, 'recovered-076', {'messages': [], 'current_customer': other['conversation_id']}, 'restart' if transport in {'normal', 'backend_rejects_then_recovers'} else 'restart_' + transport)
    assert after['ui_attempts'] == [] and after['physical_sends'] == after['reads'] == 0, after
    assert after['before_retry']['runtime']['inflight_flow_id'] == stuck['runtime']['inflight_flow_id']
    assert not after['runtime']['inflight_flow_id'] and not after['locked'], after
    assert after['status'] == after['saved_status'] == 'faulted' and not after['can_start'], after
    assert after['live_recovered'] == {'can_start': False, 'status': 'faulted', 'saved_status': 'faulted', 'flow_id': None}, after
    assert not any('/tasks/pull' in e['url'] or e['url'].endswith('/claim') for e in after['exchanges']), after
    assert {r['outbox_id']: r['payload_sha256'] for r in stuck['outbox']} == {r['outbox_id']: r['payload_sha256'] for r in after['outbox']}
    assert all(r['status'] == 'confirmed' for r in after['outbox'])
    ingests = [e for e in after['exchanges'] if e['url'].endswith('/messages/ingest')]
    assert len(ingests) == (2 if transport == 'ingest_response_lost' else 1) and all(e['status'] == 200 for e in ingests), ingests
    assert ingests[0]['request']['contract_revision'] == '0.9.75'
    assert ingests[0]['request']['conversation_id'] == conv
    finishes = [e for e in after['exchanges'] if e['url'].endswith('/inflight-flow/finish')]
    assert len(finishes) == (2 if transport == 'finish_response_lost' else 1) and all(e['status'] == 200 for e in finishes), finishes
    assert after['injections'] == (['accepted_response_lost'] if transport.endswith('response_lost') else [])
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        assert owner.run_status == 'faulted' and not owner.inflight_flow_state
        original = db.get(MessageEvent, original_identity[0])
        assert (original.id, original.source_message_key, original.content) == original_identity
        assert db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id == conv, MessageEvent.content == NEW_QUESTION)) == 1
        assert db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id == other['conversation_id'])) == 0
        # Assert automatic work before any manual idempotency probe (none here).
        assert db.scalar(select(func.count()).select_from(ReplyAction).where(ReplyAction.conversation_id == conv)) == 1, 'Automatic reply absent'
        assert db.scalar(select(func.count()).select_from(Task).where(Task.worker_id == worker['id'])) == 1
    model = [json.loads(line)['event'] for line in (tmp_path / 'new-model.jsonl').read_text().splitlines()]
    assert model == ['scheduled', 'provider']
    (tmp_path / 'verification.json').write_text(json.dumps({'source_075': OLD_SHA, 'before_flow': before_flow,
        'same_sqlite': True, 'same_flow': True, 'original_payload_unchanged': True, 'ui_operations': 0,
        'desktop_changed_to_B': after['desktop'], 'customer_A': conv, 'customer_B': other['conversation_id'],
        'automatic_generation': model, 'transport': transport, 'stopped_after_recovery': True, 'windows_exe_tested': False}, indent=2))
