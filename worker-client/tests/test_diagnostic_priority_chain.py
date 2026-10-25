"""Actual receive/project/delivery faults -> standalone exporter -> ZIP.

Synthetic native callback and downstream stored receipts; no WeChat or network.
"""
from dataclasses import replace
import json
import uuid
import zipfile

import pytest
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_projection import create_projection_store
from chejin_worker_client.hook_outbox import HookDeliveryOutbox, HookDeliveryScope
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.diagnostic_bundle import export_bundle
from test_hook_projection import local_db

NOW = '2026-10-01T02:00:00+00:00'


def produce(count, *, pending=False, completed=False):
    target = HookDeliveryScope(str(uuid.uuid4()), 'synthetic-instance', 'wxid_diag_sales', str(uuid.uuid4()),
        str(uuid.uuid4()), 1, 'wxid_diag_A')
    scope = HookReceiveScope(target.self_wxid, target.account_epoch, 'synthetic-process', 30251)
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=128*1024*1024)
    projector = create_projection_store()
    outbox = HookDeliveryOutbox(max_storage_bytes=32*1024*1024)
    events = []
    for i in range(count):
        destination = target if i < 4 or pending else replace(target, peer_wxid='wxid_diag_B', binding_id=str(uuid.uuid4()))
        raw = json.dumps({'ServerPort': '30251', 'selfwxid': scope.self_wxid, 'sendorrecv': '2', 'msgnumber': '1',
            'msglist': [{'msgsvrid': str(100001+i), 'msgtype': '1', 'fromtype': '1', 'fromid': destination.peer_wxid,
            'toid': target.self_wxid, 'msg': 'private synthetic body', 'time': '2026-10-01 10:00:00'}]}).encode()
        source = inbox.persist(raw, scope)
        p = projector.project_next(scope)
        event = outbox.enqueue_projection(p['receive_seq'], destination, now=NOW)
        events.append((event, source, destination))
        if pending or (not completed and i == 2):
            continue
        claim = outbox.claim_next(destination, now=NOW, lease_seconds=30)
        # Use a distinct client scope for pending/attempting cases so claiming
        # the next entry cannot accidentally consume an earlier pending row.
        if not completed and i in (0, 1):
            assert outbox.defer(claim, now=NOW, next_attempt_at='2026-10-02T02:00:00+00:00',
                error_code='ACCOUNT_CHANGED' if i == 0 else 'NETWORK_FAILED', blocked=i == 0)
        elif not completed and i == 3:
            # claim is the older pending row: it is now attempting; newest stays pending.
            pass
        else:
            assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)
    return events


def read(path):
    with zipfile.ZipFile(path) as z:
        return {name: json.loads(z.read(name)) for name in z.namelist() if name.endswith('.json')}


def rows_before():
    with storage.db_connection() as db:
        return {name: [tuple(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                for name in ('hook_raw_inbox','hook_observation_projection','hook_delivery_outbox')}


def acknowledge_voice(root, scope, peer, mid):
    from types import SimpleNamespace as NS
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    dispatcher = HookBusinessDispatcher(NS(voice_result=lambda *args: {'stored': True}),
        root/'transactions/hook', None, None, automatic_transcription_disabled=lambda: True)
    dispatcher.runtime = NS(scope=scope, credentials=NS(), account={})
    assert dispatcher._recover_voice_attempts([{'peer_wxid': peer, 'binding_id': 'binding',
        'binding_revision': 1, 'voice_jobs': [{'native_msg_id': mid}]}])


def test_old_unfinished_delivery_survives_recent_other_customer_history_and_desktop_job(local_db, monkeypatch):
    events = produce(1005)
    before = rows_before()
    # Actual desktop job uses the same collector on its background thread.
    from PySide6.QtCore import QCoreApplication, QTimer
    from chejin_worker_client.diagnostic_ui import ExportJob
    app = QCoreApplication.instance() or QCoreApplication([])
    job = ExportJob(local_db)
    completed, failures, ticks = [], [], []
    job.completed.connect(completed.append)
    job.failed.connect(failures.append)
    timer = QTimer(); timer.timeout.connect(lambda: ticks.append(True)); timer.start(1)
    assert job.start() and not job.start()
    import time
    deadline = time.monotonic()+15
    while not completed and not failures and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.001)
    timer.stop()
    assert completed and not failures and ticks
    evidence = read(completed[0])
    for table in ('hook_delivery_outbox','hook_raw_inbox','hook_observation_projection'):
        saved = evidence[f'state/{table}.json']
        assert len(saved['rows']) == 1000 and saved['truncated']
        assert {v[1]['receive_seq'] for v in events[:4]} <= {r['receive_seq'] for r in saved['rows']}
        assert saved['consistent_database_snapshot']
    out = evidence['state/hook_delivery_outbox.json']['rows']
    assert {r['status'] for r in out[:4]} == {'blocked','retry_wait','attempting','pending'}
    assert all(r['payload_json']['peer_wxid'] == 'wxid_diag_A' for r in out[:4])
    assert 'private synthetic body' not in json.dumps(evidence)
    assert evidence['export.json']['partial'] and not evidence['export.json']['business_actions_performed']
    assert rows_before() == before
    # Export again after reconstruction: no collector state is needed to find the old fault.
    repeated = read(export_bundle(local_db, local_db/'restart.zip'))
    assert repeated['state/hook_delivery_outbox.json']['rows'] == out
    assert rows_before() == before
    # Negative control: restore the old newest-only selector. The same actual
    # producers now lose all four unfinished rows from the final ZIP.
    from chejin_worker_client.diagnostic_selection import SnapshotPriority
    with monkeypatch.context() as control:
        control.setattr(SnapshotPriority, 'query', lambda self, table, fields:
            f'SELECT {fields}, 0 AS diagnostic_priority FROM "{table}" ORDER BY rowid DESC LIMIT ?')
        legacy = read(export_bundle(local_db, local_db/'legacy-control.zip'))
    assert not ({v[1]['receive_seq'] for v in events[:4]} &
                {r['receive_seq'] for r in legacy['state/hook_delivery_outbox.json']['rows']})
    assert legacy['export.json']['partial'] and rows_before() == before


def test_critical_overflow_is_bounded_and_reports_current_fault_priority(local_db):
    events = produce(1005, pending=True)
    current = events[-1][1]['receive_seq']
    storage.append_log('ERROR', 'synthetic_current_fault', 'synthetic', metadata={'receive_seq': current})
    before = rows_before()
    data = read(export_bundle(local_db, local_db/'overflow.zip'))
    for name in ('hook_raw_inbox','hook_observation_projection','hook_delivery_outbox'):
        rows = data[f'state/{name}.json']['rows']
        assert len(rows) == 1000 and rows[0]['receive_seq'] == current
    omissions = data['evidence-index/export.json']['omissions']
    assert any(r.get('reason') in {'critical_chain_limit', 'critical_rows_limit'} for r in omissions)
    assert rows_before() == before


@pytest.mark.parametrize('state', ['transcribing', 'failed', 'transcribed'])
def test_pending_voice_keeps_parent_audio_scope_and_interruption(local_db, state):
    from chejin_worker_client.hook_voice import HookVoiceAudio, HookVoiceTranscriber, capture_voice_audio
    from dataclasses import replace
    scope = HookReceiveScope('wxid_diag_sales', str(uuid.uuid4()), 'voice-process', 30251)
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=128*1024*1024)
    projector = create_projection_store()
    audio = b'\x02#!SILK_V3'+b'x'*32
    def receive(message, epoch=None):
        active = scope if epoch is None else replace(scope, account_epoch=epoch)
        raw = json.dumps({'ServerPort':'30251','selfwxid':active.self_wxid,'sendorrecv':'2',
            'msgnumber':'1','msglist':[{'msgtype':'34','msgsvrid':'900001',**message}]}).encode()
        p = inbox.persist(raw, active); projector.project_next(active); return p['receive_seq']
    parent = receive({'fromid':'wxid_voice_A','toid':scope.self_wxid,'fromtype':'1',
        'msg':f'<msg><voicemsg length="{len(audio)}"/></msg>'})
    supplement = receive({'voice_data':audio.hex().upper(), 'voice_len':str(len(audio))})
    captured = capture_voice_audio(scope, 'wxid_voice_A', '900001', max_audio_bytes=4096)
    voice = HookVoiceTranscriber(max_audio_bytes=4096, max_text_bytes=4096)
    voice._reserve(captured)  # Actual durable boundary before a killed native call.
    if state != 'transcribing':
        voice._finish(captured, state, response=b'synthetic response',
                      text='synthetic private transcription' if state == 'transcribed' else None,
                      error='HOOK_VOICE_TRANSCRIPTION_FAILED' if state == 'failed' else None)
    original = voice.saved_parent(scope, 'wxid_voice_A', '900001')
    if state == 'transcribing':
        recovered = voice.recover_interrupted(replace(scope,process_generation='restarted'), 'wxid_voice_A',
            '900001', generation_exited=lambda _: True)
        assert recovered['state'] == 'uncertain'
    other_scope = replace(scope, account_epoch=str(uuid.uuid4()))
    wrong_epoch = receive({'fromid':'wxid_voice_A','toid':scope.self_wxid,'fromtype':'1',
        'msg':f'<msg><voicemsg length="{len(audio)}"/></msg>'}, epoch=other_scope.account_epoch)
    # An unrelated *completed* voice is ordinary history; an unprocessed voice
    # in another account would itself deserve evidence priority, not exclusion.
    other_audio = HookVoiceAudio(other_scope, 'wxid_voice_A', '900001', audio)
    voice._reserve(other_audio)
    voice._finish(other_audio, 'transcribed', response=b'response', text='other epoch', error=None)
    acknowledge_voice(local_db, other_scope, 'wxid_voice_A', '900001')
    produce(1005)
    data = read(export_bundle(local_db, local_db/'voice.zip'))
    selected = data['state/hook_raw_inbox.json']['rows']
    assert {parent, supplement} <= {r['receive_seq'] for r in selected}
    assert wrong_epoch not in {r['receive_seq'] for r in selected}
    assert data['state/hook_voice_attempts.json']['rows'][0]['state'] == state
    if state == 'transcribing':
        interruption = data['state/hook_voice_interruptions.json']['rows'][0]['result_json']
        assert interruption['state'] == 'uncertain' and interruption['audio_sha256'] == original['audio_sha256']
    assert 'synthetic private transcription' not in json.dumps(data)
    assert voice.saved_parent(scope, 'wxid_voice_A', '900001') == original


def test_acknowledged_voice_history_does_not_consume_unreturned_chain_budget(local_db, monkeypatch):
    from types import SimpleNamespace as NS
    import hashlib
    from chejin_worker_client.hook_voice import HookVoiceAudio, capture_voice_audio
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    from chejin_worker_client.diagnostic_bundle import Collector
    from chejin_worker_client.diagnostic_selection import SnapshotPriority

    scope = HookReceiveScope('wxid_voice_budget', str(uuid.uuid4()), 'voice-generation', 30251)
    audio = b'\x02#!SILK_V3'+b'x'*32
    # Only the external response is synthetic. The real dispatcher saves the
    # receipts, using real retained transcriber outputs; no SQL or fixture files.
    client = NS(voice_result=lambda *args: {'stored': True})
    dispatcher = HookBusinessDispatcher(client, local_db/'transactions/hook', None, None,
                                       automatic_transcription_disabled=lambda: True)
    dispatcher.runtime = NS(scope=scope, credentials=NS(), account={})
    for i in range(1000):
        item = HookVoiceAudio(scope, 'wxid_voice_peer', str(200001+i), audio)
        dispatcher.voice._reserve(item)
        dispatcher.voice._finish(item, 'transcribed', response=b'response', text='synthetic old', error=None)
        assert dispatcher._recover_voice_attempts([{'peer_wxid': item.peer_wxid,
            'binding_id': 'binding', 'binding_revision': 1, 'voice_jobs': [{'native_msg_id': item.native_msg_id}]}])

    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=128*1024*1024)
    projector = create_projection_store()
    def receive(active, message):
        raw = json.dumps({'ServerPort': '30251', 'selfwxid': active.self_wxid, 'sendorrecv': '2',
            'msgnumber': '1', 'msglist': [{'msgtype': '34', 'msgsvrid': '900001', **message}]}).encode()
        seq = inbox.persist(raw, active)['receive_seq']; projector.project_next(active)
        return seq
    metadata = {'fromid': 'wxid_voice_peer', 'toid': scope.self_wxid, 'fromtype': '1',
                'msg': f'<msg><voicemsg length="{len(audio)}"/></msg>'}
    parent = receive(scope, metadata)
    supplement = receive(scope, {'voice_data': audio.hex(), 'voice_len': str(len(audio))})
    item = capture_voice_audio(scope, 'wxid_voice_peer', '900001', max_audio_bytes=4096)
    dispatcher.voice._reserve(item)
    dispatcher.voice._finish(item, 'transcribed', response=b'response', text='private pending text', error=None)
    other_scope = replace(scope, account_epoch=str(uuid.uuid4()))
    wrong_epoch = receive(other_scope, metadata)
    other_audio = HookVoiceAudio(other_scope, 'wxid_voice_peer', '900001', audio)
    dispatcher.voice._reserve(other_audio)
    dispatcher.voice._finish(other_audio, 'transcribed', response=b'response', text='other epoch', error=None)
    acknowledge_voice(local_db, other_scope, 'wxid_voice_peer', '900001')
    produce(1005)
    with storage.db_connection() as db:
        attempts = [dict(r) for r in db.execute('SELECT * FROM hook_voice_attempts ORDER BY rowid')]
    collector = Collector(local_db, local_db/'voice-backlog.zip')
    assert all(collector.voice_receipt_known(a) for a in attempts[:1000])
    assert not collector.voice_receipt_known(attempts[1000])
    assert collector.voice_receipt_known(attempts[1001])

    def sources():
        return {str(p.relative_to(local_db)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [storage.DB_FILE, *(local_db/'transactions').rglob('*.json')]}
    original = sources()
    result = read(export_bundle(local_db, local_db/'voice-backlog.zip'))
    assert sources() == original
    for table in ('hook_raw_inbox', 'hook_observation_projection'):
        selected = {r['receive_seq'] for r in result[f'state/{table}.json']['rows']}
        assert {parent, supplement} <= selected and wrong_epoch not in selected
    voice_rows = result['state/hook_voice_attempts.json']['rows']
    assert voice_rows[0]['native_msg_id'] == '900001' and len(voice_rows) == 1000
    assert 'private pending text' not in json.dumps(result)

    # Negative control restores the former first-1000 discovery boundary: later
    # terminal rows are not inspected for missing receipts. Same producer data.
    inspected = [0]
    known = Collector.voice_receipt_known
    def old_discovery(self, attempt):
        inspected[0] += 1
        return True if inspected[0] > 1000 else known(self, attempt)
    with monkeypatch.context() as old:
        old.setattr(Collector, 'voice_receipt_known', old_discovery)
        legacy = read(export_bundle(local_db, local_db/'voice-backlog-old.zip'))
    assert any(r['native_msg_id'] == '900001' for r in legacy['state/hook_voice_attempts.json']['rows'])
    assert not {parent, supplement} & {r['receive_seq'] for r in legacy['state/hook_raw_inbox.json']['rows']}
    assert sources() == original

    # Discovery remains bounded by the existing snapshot clock, with an
    # explicit reference when the time budget prevents inspecting a voice.
    omissions = []
    with storage.db_connection() as db:
        priority = SnapshotPriority(db, 1000, lambda *args, **kw: omissions.append(kw),
                                    collector.voice_receipt_known, deadline=0)
    assert not priority.voice_rows
    assert any(r.get('reason') == 'voice_receipt_time_limit' and r.get('voice_reference') for r in omissions)


@pytest.mark.parametrize('stage', ['before_enqueue', 'partial_same_peer', 'partial_other_peer'])
def test_drain_interruption_preserves_unacknowledged_projection_chain(local_db, monkeypatch, stage):
    from types import SimpleNamespace as NS
    from chejin_worker_client.hook_backend import HookWorkerCredentials
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.diagnostic_selection import SnapshotPriority
    scope = HookReceiveScope('wxid_drain_budget', str(uuid.uuid4()), 'drain-generation', 30251)
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=128*1024*1024)
    def message(mid, peer, group=False):
        return {'msgsvrid': mid, 'msgtype': '1', 'fromtype': '2' if group else '1',
                'fromid': peer, 'toid': scope.self_wxid, 'msg': 'synthetic private text'}
    def persist(messages):
        return inbox.persist(json.dumps({'ServerPort': '30251', 'selfwxid': scope.self_wxid,
            'sendorrecv': '2', 'msgnumber': str(len(messages)), 'msglist': messages}).encode(), scope)['receive_seq']
    ignored = persist([message('700000', 'synthetic@chatroom', True)])
    peer = 'wxid_drain_A'
    original_seq = persist([message('700001', peer), message('700002',
        'wxid_drain_B' if stage == 'partial_other_peer' else peer)])
    runtime = HookFormalRuntime(NS(base_url='http://127.0.0.1:1', timeout_seconds=5, max_response_bytes=2097152),
        HookWorkerCredentials(str(uuid.uuid4()), 'synthetic', 'instance'), NS(),
        NS(clock=NS(), process=NS(), registration=NS(scope=scope)), local_db/'transactions/hook')
    binding = {'binding_id': str(uuid.uuid4()), 'binding_revision': 1, 'peer_wxid': peer}
    enqueue = runtime.outbox.enqueue_projections
    def interrupted(sequences, delivery, **kwargs):
        if stage != 'before_enqueue':
            enqueue(sequences, delivery, now=NOW,
                    allowed_native_ids={'700001'} if stage == 'partial_same_peer' else None)
            claim = runtime.outbox.claim_next(delivery, now=NOW, lease_seconds=30)
            assert claim and len(claim.envelope['messages']) == 1
            assert runtime.outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)
        raise OSError('synthetic interruption before complete enqueue')
    monkeypatch.setattr(runtime.outbox, 'enqueue_projections', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        runtime.drain(binding)
    # Actual drain committed projections before the injected interruption.
    with storage.db_connection() as db:
        assert db.execute('SELECT 1 FROM hook_observation_projection WHERE receive_seq=?', (original_seq,)).fetchone()
    produce(1005, completed=True)
    original = rows_before()
    for name in ('drain-gap.zip', 'drain-gap-restart.zip'):
        result = read(export_bundle(local_db, local_db/name))
        for table in ('hook_raw_inbox', 'hook_observation_projection'):
            selected = {r['receive_seq'] for r in result[f'state/{table}.json']['rows']}
            assert original_seq in selected and ignored not in selected
        assert rows_before() == original
    # Negative control: restore the old discovery predicates (no missing
    # message coverage query); unchanged production rows lose the old chain.
    constructor = SnapshotPriority.__init__
    class OldQueries:
        def __init__(self, db): self.db = db
        def execute(self, query, *args):
            if "coalesce(json_extract(o.value,'$.reason_code'),'')!='HOOK_GROUP_EXCLUDED'" in query:
                return iter(())
            return self.db.execute(query, *args)
    def old_init(self, db, *args, **kwargs):
        constructor(self, OldQueries(db), *args, **kwargs)
    with monkeypatch.context() as old:
        old.setattr(SnapshotPriority, '__init__', old_init)
        legacy = read(export_bundle(local_db, local_db/'drain-gap-old.zip'))
    assert original_seq not in {r['receive_seq'] for r in legacy['state/hook_raw_inbox.json']['rows']}
    assert rows_before() == original
