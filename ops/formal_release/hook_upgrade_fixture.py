"""Synthetic bound Hook history for immutable Windows upgrade acceptance.

No WeChat process, DLL or readiness is fabricated. The real packaged runtime
must detect unavailable WeChat and retain its fault while the updater runs.
Only the isolated runner's generated data is ever read or written here.
"""
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

THREADS = ('task_runner', 'thread_monitor', 'hook_intake', 'hook_business')
TABLES = ('hook_raw_inbox', 'hook_observation_projection', 'hook_delivery_outbox', 'hook_voice_attempts')


def seed_retained_install(data, worker_id, instance_id):
    """Additional install-only fixture; do not change the ordinary button case."""
    from datetime import datetime, timezone
    from chejin_worker_client import storage
    from chejin_worker_client.hook_sender import RetainedNativeSendPermit
    now = datetime.now(timezone.utc).isoformat()
    flow = '00000000-0000-4000-8000-000000000015'
    action = '00000000-0000-4000-8000-000000000014'
    epoch = '00000000-0000-4000-8000-000000000002'
    scope = SimpleNamespace(self_wxid='wxid_synthetic_sales', account_epoch=epoch,
                            process_generation='synthetic-old-process')
    private = Path(data) / 'transactions' / 'hook'
    root = private / 'sends' / worker_id / epoch / action
    root.mkdir(parents=True)
    intent = {'schema_version': 'hook-send-journal-v1', 'task_id': flow,
              'reply_action_id': action, **vars(scope), 'peer_wxid': 'wxid_synthetic_customer',
              'send_token': 'synthetic-retained-token', 'attempted_at': now}
    def save(name, value):
        (root / name).write_text(json.dumps(value), encoding='utf-8')
    save('intent.json', intent)
    digest = hashlib.sha256((root / 'intent.json').read_bytes()).hexdigest()
    save('trigger.json', {'phase': 'trigger_attempted', 'intent_sha256': digest})
    save('permit.json', {**intent, 'flow_id': flow, 'lease_fencing_token': 1})
    save('outcome.json', {'task_id': flow, 'reply_action_id': action,
                         'journal_sha256': digest, 'call_completed': False,
                         'error_code': 'HOOK_CALL_TIMEOUT', 'completed_at': now,
                         'automatic_resend_allowed': False})
    metadata = {'task_id': flow, 'reply_action_id': action, 'flow_id': flow,
                'lease_fencing_token': 1, 'journal_sha256': digest}
    RetainedNativeSendPermit.load(private / 'sends',
        SimpleNamespace(worker_id=worker_id, client_instance_id=instance_id), scope,
        {'peer_wxid': 'wxid_synthetic_customer'}, metadata)
    storage.begin_runtime_flow(flow, 'chat_reply')
    state = {'flow_id': flow, 'flow_kind': 'chat_reply', 'status': 'draining',
             'conversation_id': 'synthetic-retained-conversation', 'pause_requested_at': now}
    (private / 'flows').mkdir(exist_ok=True)
    (private / 'flows' / (flow + '.json')).write_text(json.dumps(state), encoding='utf-8')
    return state


def seed(data, api_base_url, worker_id, instance_id, now):
    from chejin_worker_client import storage
    from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
    from chejin_worker_client.hook_projection import NativeProjectionStore
    from chejin_worker_client.hook_outbox import HookDeliveryScope, HookDeliveryOutbox
    from chejin_worker_client.hook_voice import HookVoiceAudio, HookVoiceTranscriber
    from chejin_worker_client.hook_sender import RetainedNativeSendPermit
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]
    hook_port, receiver_port = port(), port()
    while receiver_port == hook_port:
        receiver_port = port()
    deployment = {'revision': 'synthetic-unavailable-wechat',
                  'wechat_exe': r'C:\SyntheticUnavailableWeChat\WeChat.exe',
                  'dll_sha256': 'a' * 64, 'hook_api_port': hook_port, 'receiver_port': receiver_port}
    settings = {'hook_installation_v1': {'schema_version': 1, 'api_base_url': api_base_url},
                'hook_deployment_v1': deployment, 'wechat_transport_v1': {'mode': 'hook'},
                'hook_native_auto_text_confirmation_v1': {'deployment_revision': deployment['revision'],
                                                         'disabled': True, 'operator_confirmed': True}}
    with storage.db_connection() as db:
        db.executemany('INSERT INTO client_settings VALUES (?,?,?)',
                       [(key, json.dumps(value), now) for key, value in settings.items()])
        db.commit()
    scope = HookReceiveScope('wxid_synthetic_sales', '00000000-0000-4000-8000-000000000002', 'synthetic-old-process', hook_port)
    peer = 'wxid_synthetic_customer'
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=1048576)
    audio = b'\x02#!SILK_V3synthetic-upgrade-audio'
    for message in [
        {'msgtype': '1', 'msgsvrid': '12345678901', 'msg': 'synthetic completed text'},
        {'msgtype': '34', 'msgsvrid': '12345678902', 'msg': '<msg><voicemsg length="%s"/></msg>' % len(audio)},
        {'msgtype': '34', 'msgsvrid': '12345678902', 'msg': '', 'voice_data': audio.hex(), 'voice_len': str(len(audio))},
    ]:
        raw = {'ServerPort': str(hook_port), 'selfwxid': scope.self_wxid, 'sendorrecv': '2',
               'msgnumber': '1', 'msglist': [{'fromtype': '1', 'fromid': peer, 'toid': scope.self_wxid,
                                           'time': '2026-10-16 12:00:00', **message}]}
        inbox.persist(json.dumps(raw).encode(), scope)
    projection = NativeProjectionStore(max_projection_bytes=1048576, max_entries=100)
    projected = []
    while (value := projection.project_next(scope)) is not None:
        projected.append(value)
    delivery = HookDeliveryScope(worker_id, instance_id, scope.self_wxid, scope.account_epoch,
                                 '00000000-0000-4000-8000-000000000003', 1, peer)
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(projected[0]['receive_seq'], delivery, now=now)
    claim = outbox.claim_next(delivery, now=now, lease_seconds=10)
    assert claim is not None
    assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now=now)
    voice = HookVoiceAudio(scope, peer, '12345678902', audio)
    transcriber = HookVoiceTranscriber(max_audio_bytes=4096, max_text_bytes=4096)
    transcriber._reserve(voice)
    transcriber._finish(voice, 'transcribed', response=b'synthetic response', text='synthetic completed transcript')
    # A genuine retained-journal reader validates the synthetic completed files.
    private = Path(data) / 'transactions' / 'hook'
    root = private / worker_id / scope.account_epoch / '00000000-0000-4000-8000-000000000004'
    root.mkdir(parents=True)
    intent = {'schema_version': 'hook-send-journal-v1', 'task_id': '00000000-0000-4000-8000-000000000005',
              'reply_action_id': '00000000-0000-4000-8000-000000000004', 'self_wxid': scope.self_wxid,
              'account_epoch': scope.account_epoch, 'process_generation': scope.process_generation,
              'peer_wxid': peer, 'send_token': 'synthetic-send-token', 'attempted_at': now}
    (root / 'intent.json').write_text(json.dumps(intent), encoding='utf-8')
    sha = hashlib.sha256((root / 'intent.json').read_bytes()).hexdigest()
    (root / 'trigger.json').write_text(json.dumps({'phase': 'trigger_attempted', 'intent_sha256': sha}), encoding='utf-8')
    (root / 'outcome.json').write_text(json.dumps({'task_id': '00000000-0000-4000-8000-000000000005', 'reply_action_id': '00000000-0000-4000-8000-000000000004',
        'journal_sha256': sha, 'call_completed': True, 'hook_submission_flag': True,
        'completed_at': now, 'automatic_resend_allowed': False}), encoding='utf-8')
    RetainedNativeSendPermit.load(private, SimpleNamespace(worker_id=worker_id, client_instance_id=instance_id),
        scope, {'peer_wxid': peer}, {'task_id': '00000000-0000-4000-8000-000000000005', 'reply_action_id': '00000000-0000-4000-8000-000000000004',
                                  'flow_id': '00000000-0000-4000-8000-000000000006', 'lease_fencing_token': 1})


def snapshot(data):
    import sqlite3
    with sqlite3.connect(Path(data) / 'worker_client.sqlite3') as db:
        result = {}
        for table in TABLES:
            exists = db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()
            if exists:
                rows = db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
                assert rows, 'Hook fixture must contain representative rows: ' + table
                result[table] = [[{'bytes_sha256': hashlib.sha256(v).hexdigest()} if isinstance(v, bytes) else v
                                  for v in row] for row in rows]
        files = {p.relative_to(data).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((Path(data) / 'transactions' / 'hook').rglob('*.json'))}
        if result:
            assert set(result) == set(TABLES) and len(files) >= 3, 'Incomplete Hook preservation fixture'
        return {'tables': result, 'journal_files': files}


def assert_health(marker):
    health = marker['runtime_health']
    assert health['binding_state'] == 'bound'
    assert set(health['required_threads']) == set(THREADS), 'Hook runtime required-thread mismatch'
    assert not health['startup_failures']
    for name in THREADS:
        assert health['threads'][name] == {'entered_loop': True, 'alive': True}, 'Hook loop unhealthy: ' + name


def assert_environment_fault(before, after):
    """Only unavailable WeChat may change paused -> faulted before clicking.

    Compare every other field/row/file; keep both checkpoints in the report.
    This is not a waiver of the updater's immutable post-exit snapshot.
    """
    assert len(before['binding']) == len(after['binding']) == 1
    assert before['binding'][0][3] in {'paused', 'faulted'}
    assert after['binding'][0][3] == 'faulted'
    import copy
    normalized = copy.deepcopy(after)
    row = list(normalized['binding'][0])
    row[3] = before['binding'][0][3]
    normalized['binding'] = [tuple(row)]
    assert normalized == before, 'Unexpected data change before update environment fault'


def environment_fault(data, *, not_before, excluded_ids=()):
    """Require a fresh result from this process lifetime, never an old fault."""
    import sqlite3
    from datetime import datetime
    with sqlite3.connect(Path(data) / 'worker_client.sqlite3') as db:
        rows = db.execute("SELECT id,created_at,error_code FROM local_logs WHERE event='hook_connection_fault'").fetchall()
        current = [(identity, code) for identity, created, code in rows
                   if identity not in excluded_ids and datetime.fromisoformat(created).timestamp() >= not_before]
        if not current:
            return None
        codes = sorted({code for _, code in current})
        assert codes == ['HOOK_PROCESS_MISSING_OR_AMBIGUOUS'], 'Unexpected current-process Hook fault: ' + str(codes)
        statuses = db.execute('SELECT run_status FROM binding').fetchall()
        assert statuses == [('faulted',)], 'Faulted stopped state was not retained'
        return {'log_ids': [identity for identity, _ in current], 'codes': codes, 'run_status': 'faulted'}
