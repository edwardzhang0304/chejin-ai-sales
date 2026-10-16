"""Late media versus a newer turn: real dispatcher/Brain/send, synthetic effects.

The saved voice producer is exercised directly as an already-started operation;
this is not authorization to start transcription during an open handoff.
Windows/Hook and external model responses are substitutes. No DB state writes.
"""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import json
import sqlite3
import threading
import time

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.c3 import MessageBatch, ReplyAction, HandoffEvent, SentAck
from app.models.hook_native import HookVoiceReceipt
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_hook_business_http import clean
from test_hook_history_business_http import bound
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from test_hook_formal_runtime import drive_reply
from test_partial_reply_recovery_provider import controlled_recovery_provider
from test_hook_voice import AUDIO, persist
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_voice import capture_voice_audio, process_voice_call_unresolved
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.models import Binding


@pytest.mark.parametrize('state', ['uncertain', 'failed', 'transcribed'])
@pytest.mark.parametrize('phase', ['inventory', 'generating', 'queued', 'send_begin', 'send_observed', 'settled'])
def test_new_turn_survives_old_voice_result_at_actual_pipeline_boundary(
        identity_client, lab, connection_factory, monkeypatch, tmp_path, state, phase):
    w, _, _, binding = bound()
    server, gateway, *_ = lab
    manager, credentials, prepared = connection_factory(w)
    client = HookFormalClient(identity_client.base_url, 5)
    root = tmp_path / 'runtime'
    runtime = HookFormalRuntime(client, credentials, gateway, prepared, root)
    dispatcher = HookBusinessDispatcher(client, root, lambda *a: pytest.fail('unexpected invitation'),
        lambda *a: None, automatic_transcription_disabled=lambda: True)
    table = sqlite3.connect(':memory:', check_same_thread=False)
    table.row_factory = sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries = [{'dbpath': r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db', 'dbhandle': 'late-batch'}]
    server.query_response = lambda body: {'data': [dict(r) for r in table.execute(body['sql']).fetchall()]}
    while int(time.time()) <= int(binding['history_since_create_time']): time.sleep(.02)

    def append(kind, sender, text):
        n = table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0] + 1
        now = int(time.time()); mid = str(8100000000000000000 + n)
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
            (n, mid, kind, sender, now, now * 1000 + n, 0, n, binding['peer_wxid'], text))
        return mid

    append(3, 0, '<msg><img md5="synthetic"/></msg>')
    assert runtime.sync(binding)['history_ready']
    parent = append(34, 0, f'<msg><voicemsg length="{len(AUDIO)}"/></msg>')
    assert runtime.sync(binding)['reason_code'] == 'HOOK_VOICE_PENDING'
    persist(lab, metadata=True, audio=True, mid=parent, data=AUDIO, scope=runtime.scope)
    audio = capture_voice_audio(runtime.scope, binding['peer_wxid'], parent, max_audio_bytes=4096)
    server.voice_response = {'status': 0 if state == 'failed' else 1,
        'text': '此前问题已经由销售回答' if state == 'transcribed' else ''}
    old_generation = prepared.process.generation
    if state == 'uncertain':
        with pytest.raises(HookLocalError, match='VOICE_RESPONSE_INVALID'):
            dispatcher.voice.transcribe_once(gateway, prepared.process, audio, automatic_transcription_disabled=lambda: True)
        assert process_voice_call_unresolved(old_generation)
        with pytest.raises(HookLocalError, match='QUARANTINED'): gateway.login_status(prepared.process)
        manager.close()
        server.current_process = replace(prepared.process, started_at=prepared.process.started_at + 1)
        manager, credentials, prepared = connection_factory(w)
        runtime = HookFormalRuntime(client, credentials, gateway, prepared, root)
        assert runtime.sync(binding)['reason_code'] == 'HOOK_VOICE_PENDING'
    else:
        assert dispatcher.voice.transcribe_once(gateway, prepared.process, audio,
            automatic_transcription_disabled=lambda: True)['state'] == state
    saved = dispatcher.voice.saved_parent(runtime.scope, binding['peer_wxid'], parent)
    assert saved['state'] == state
    append(1, 1, '我是销售，此前的问题已经处理，请问还有什么想了解的？')
    append(1, 0, '现在想了解十五万元左右的车型，暂时不预约看车。')
    delivered = []; checkpoints = []
    batch_id = None

    def deliver():
        assert not delivered
        with SessionLocal() as db:
            b = db.get(MessageBatch, batch_id)
            a = db.scalar(select(ReplyAction).where(ReplyAction.batch_id == batch_id))
            checkpoints.append({'batch_status': b.status, 'action_status': a.status if a else None,
                'digest': db.get(WechatSessionBinding, binding['binding_id']).native_context['history_state_digest']})
            if phase == 'generating': assert b.status == 'generating'
            if phase == 'queued': assert a and a.status == 'queued'
            if phase in {'send_begin', 'send_observed'}: assert a and a.status == 'sending'
            if phase == 'settled': assert a and a.status == 'sent'
        delivered.append(client.voice_result(credentials, runtime.account, binding, saved))
        assert not delivered[0]['history_coverage_required']

    if phase == 'inventory':
        # Real tick obtains old voice inventory, then history creates the new
        # customer batch BEFORE saved_parent is uploaded in that same tick.
        runner = SimpleNamespace(binding=Binding(w['id'], w['worker_token'], w['instance'], 'running'),
            stop_event=threading.Event(), api=SimpleNamespace(inflight_flow_id=None), current_task=None,
            on_task=lambda task: None, can_pull_tasks=lambda: False, _can_start_new_flow=lambda: False)
        services = SimpleNamespace(connection=manager, _credentials=credentials,
            startup_window_ready_for=lambda p: p is manager.prepared)
        dispatcher.tick(runner, services)
        assert list((root / 'voice-receipts').rglob('*.json'))
    synced = runtime.sync(binding)
    assert synced['history_ready'] and synced['message_batch']['next_step'] == 'generate', synced
    batch_id = synced['message_batch']['batch_id']
    before_digest = synced['history_state_digest']

    original_call = HookFormalClient.call
    def observe_call(self, *args, **kwargs):
        value = original_call(self, *args, **kwargs)
        path = args[2]
        if not delivered:
            if phase == 'queued' and args[1] == 'GET' and (value.get('reply_action') or {}).get('status') == 'queued': deliver()
            if phase == 'send_begin' and path.endswith('/begin'): deliver()
        return value
    monkeypatch.setattr(HookFormalClient, 'call', observe_call)

    def on_hook(path):
        server.raw_override = None
        if path == '/SendTextMsg':
            append(1, 1, server.requests[-1][1]['msg'])
            if phase == 'send_observed': deliver()
            server.raw_override = b'{"SendTextMsg":"1"}'
    server.on_request = on_hook

    class Requests(list):
        def append(self, request):
            super().append(request)
            if phase == 'generating' and request['model'] == 'recovery-author': deliver()
    requests = Requests()
    engine = str(Path(__file__).resolve().parents[2] / 'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT', engine); monkeypatch.setattr(get_settings(), 'c3_omniauto_root', engine)
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', RealOmniAutoAIEngineAdapter)
    with controlled_recovery_provider(monkeypatch, '好的，先按十五万元左右了解，您更喜欢轿车还是SUV？', requests):
        sent = drive_reply(runtime, binding, batch_id)
    assert len(sent['sends']) == 1 and sent['sends'][0]['local_send_observed'], sent
    if phase == 'settled': deliver()
    assert phase == 'inventory' or len(delivered) == 1
    assert [r['model'] for r in requests] == ['recovery-author', 'recovery-reviewer']
    # Replayed receipt and subsequent history must not reopen, cancel, or resend.
    repeat = client.voice_result(credentials, runtime.account, binding, saved)
    if delivered: assert repeat == delivered[0]
    assert runtime.sync(binding)['history_ready']
    assert not drive_reply(runtime, binding, batch_id, max_wait_seconds=5)['sends']
    with SessionLocal() as db:
        batches = list(db.scalars(select(MessageBatch)))
        action = db.scalar(select(ReplyAction).where(ReplyAction.batch_id == batch_id))
        assert len(batches) == 1 and action.status == 'sent'
        assert db.get(Task, action.claimed_task_id).status == 'completed'
        assert len(list(db.scalars(select(SentAck)))) == 1
        assert all(h.closed_at for h in db.scalars(select(HandoffEvent)))
        voice = db.scalar(select(MessageEvent).where(MessageEvent.message_type == 'voice'))
        receipt = db.scalar(select(HookVoiceReceipt))
        assert voice.item_state == ('completed' if state == 'transcribed' else state)
        assert receipt.payload_json['audio_sha256'] == saved['audio_sha256']
        assert receipt.payload_json['audio_bytes'] == saved['audio_bytes']
        assert action.ai_payload['native_send_identity']['history_state_digest'] == before_digest
    assert dispatcher.voice.saved_parent(runtime.scope, binding['peer_wxid'], parent) == saved
    assert process_voice_call_unresolved(old_generation) is (state == 'uncertain')
    assert len([r for r in server.requests if r[0] == '/Voice2Text']) == 1
    assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == 1
    (tmp_path / 'late-batch-chain.json').write_text(json.dumps({'phase': phase, 'state': state,
        'batch_id': batch_id, 'reply_action_id': action.id, 'checkpoints': checkpoints,
        'provider_models': [r['model'] for r in requests], 'sends': len(sent['sends'])}, ensure_ascii=False, indent=2))
    table.close()
