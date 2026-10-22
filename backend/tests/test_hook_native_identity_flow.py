"""Synthetic legacy identities through real HTTP, history and retained recovery.

No live WeChat, production data or external AI. Original quarantine is preserved;
only newly authorized complete history can fill a formerly rejected message.
"""
from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction, SentAck, MessageBatch
from app.models.task import Task
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from app.schemas.hook_identity import NativeWechatIdentity
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_outbox import HookDeliveryOutbox, HookDeliveryScope
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from test_hook_fault_recovery_http import clean, local_storage, retained_scenario
from test_hook_account_client import identity_client
from test_hook_recovery_closure import fault, resume
from test_hook_business_http import submitted, body, post, store_dispatch
from test_hook_history_business_http import coverage, upload
import test_hook_history_business_http as history_fixture


@pytest.mark.parametrize('value', ['nativecustomer2468', 'Legacy-Customer_42', 'wxid_customer_A',
    '12345678901', 12345678901, 'nativeuser ', 'gh_official', 'filehelper', 'FILEHELPER',
    'group@chatroom', 'worker@openim', 'wxid_', 'ab/cdef'])
def test_http_schema_uses_exact_shared_identity_rule(value):
    adapter = TypeAdapter(NativeWechatIdentity)
    if rules.wxid(value):
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.parametrize('peer', ['nativecustomer2468', 'Legacy-Customer_42'])
def test_upgrade_keeps_quarantined_callback_and_repairs_from_authorized_history(monkeypatch, peer):
    w, task, account, headers, prep = submitted()
    # Exact shortcode proof still required; format compatibility alone grants nothing.
    bad = post(w, f"/workers/{w['id']}/wechat/native-bindings", body(task, account, prep, (peer, peer)))
    assert bad.status_code == 409
    response = post(w, f"/workers/{w['id']}/wechat/native-bindings", body(task, account, prep, (peer,)))
    assert response.status_code == 200, response.text
    binding = response.json()['data']
    value = coverage(binding)
    original_row = value['scans'][0]['databases'][0]['pages'][0][0]
    receive = HookReceiveScope(**account, hook_api_port=30001)
    callback = {'ServerPort':'30001', 'selfwxid':account['self_wxid'], 'sendorrecv':'2', 'msgnumber':'1',
        'msglist':[{'msgsvrid':original_row['MsgSvrID'], 'msgtype':'1', 'fromtype':'1',
            'fromid':peer, 'toid':account['self_wxid'], 'msg':original_row['StrContent'],
            'time':datetime.fromtimestamp(int(original_row['CreateTime']), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}]}
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=1048576)
    raw = json.dumps(callback).encode()
    inbox.persist(raw, receive)
    projector = NativeProjectionStore(max_projection_bytes=1048576, max_entries=200)
    old = projector.project_next(receive)
    # Construct the previous release's saved quarantine, before exercising upgrade.
    old['contract_version'] = 'hook-observation-projection-v2'
    old['observations'][0].update(disposition='quarantined', peer_wxid=None, content=None,
        reason_code='HOOK_PRIVATE_IDENTITY_REQUIRED')
    encoded = rules.canonical(old)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with storage.db_connection() as db:
        db.execute('UPDATE hook_observation_projection SET contract_version=?,projection_json=?,projection_digest=?',
            (old['contract_version'], encoded, digest)); db.commit()
        raw_before = tuple(db.execute('SELECT raw_body,raw_digest FROM hook_raw_inbox').fetchone())
    assert projector.project_next(receive) is None
    delivery = HookDeliveryScope(w['id'], w['instance'], account['self_wxid'], account['account_epoch'],
        binding['binding_id'], binding['binding_revision'], peer)
    out = HookDeliveryOutbox(max_storage_bytes=1048576)
    now = datetime.now(timezone.utc).isoformat()
    assert out.enqueue_projection(old['receive_seq'], delivery, now=now) is None
    repaired, _ = upload(w, account, binding, value, monkeypatch)
    assert repaired.status_code == 200 and repaired.json()['data']['history_ready'], repaired.text
    first_batch = repaired.json()['data']['message_batch']['batch_id']
    # A subsequent live callback of the same native message remains a single fact.
    inbox.persist(raw, receive)
    fresh = projector.project_next(receive)
    out.enqueue_projection(fresh['receive_seq'], delivery, now=now)
    claim = out.claim_next(delivery, now=now, lease_seconds=10)
    dispatched = store_dispatch(w, claim.envelope)
    assert dispatched.status_code == 200, dispatched.text
    again, _ = upload(w, account, binding, value, monkeypatch)
    assert again.status_code == 200 and again.json()['data']['message_batch']['batch_id'] == first_batch
    with SessionLocal() as db:
        messages = list(db.scalars(select(MessageEvent)))
        assert len(messages) == 2
        assert sum(m.raw_payload['native_msg_id'] == original_row['MsgSvrID'] for m in messages) == 1
        assert db.scalar(select(func.count()).select_from(MessageBatch)) == 1
    with storage.db_connection() as db:
        assert tuple(db.execute('SELECT projection_json,projection_digest FROM hook_observation_projection WHERE receive_seq=?',
            (old['receive_seq'],)).fetchone()) == (encoded, digest)
        assert tuple(db.execute('SELECT raw_body,raw_digest FROM hook_raw_inbox WHERE receive_seq=?',
            (old['receive_seq'],)).fetchone()) == raw_before


@pytest.mark.parametrize('peer', ['nativecustomer2468', 'Legacy-Customer_42'])
@pytest.mark.parametrize('outgoing', [True, False])
def test_legacy_original_send_settles_then_explicitly_resumes_without_resend(identity_client, tmp_path, monkeypatch, peer, outgoing):
    # Change only the simulated friend-list input; all product routes remain real.
    monkeypatch.setattr(history_fixture, 'body', lambda t, p, prep: body(t, p, prep, (peer,)))
    w, account, binding, headers, permit, value, dispatcher, runner, services = retained_scenario(
        identity_client, tmp_path, monkeypatch, invalidate=False, trigger=True, outgoing=outgoing)
    assert binding['peer_wxid'] == permit['peer_wxid'] == peer
    root = dispatcher.private_root/'sends'/w['id']/account['account_epoch']/permit['reply_action_id']
    originals = {path: path.read_bytes() for path in root.glob('*.json')}
    fault(w)
    runner.binding.run_status = 'faulted'
    for _ in range(3):
        dispatcher.tick(runner, services)
    assert all(path.read_bytes() == content for path, content in originals.items())
    with SessionLocal() as db:
        assert db.get(Task, permit['task_id']).status in {'completed', 'failed'}
        assert not db.get(Worker, w['id']).inflight_flow_state
        assert db.get(Worker, w['id']).run_status == 'faulted'
        action = db.get(ReplyAction, permit['reply_action_id'])
        result = action.ai_payload['native_send_settlement']['result']
        assert result['local_send_observed'] is outgoing
        assert not result['recipient_delivery_confirmed'] and not result['automatic_resend_allowed']
        assert db.scalar(select(func.count()).select_from(SentAck)) == 1
        assert action.send_token == permit['send_token']
    resume(w, account, identity_client)
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize('native_self', ['nativesales2468', 'Legacy-Sales_42'])
def test_legacy_self_heartbeat_still_blocks_account_switch(native_self):
    from test_hook_account_http import worker, heartbeat, context
    w = worker()
    first = heartbeat(w, context(native_self)).json()['data']['hook_context']
    assert first['self_wxid'] == native_self and not first['blocked']
    response = heartbeat(w, context('anothernativeuser', account_epoch=first['account_epoch']), run_status='running')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['run_status'] == 'faulted'
    assert data['hook_context']['reason_code'] == 'HOOK_ACCOUNT_CHANGED'
    assert data['hook_context']['self_wxid'] == native_self
    assert data['hook_context']['account_epoch'] == first['account_epoch']
