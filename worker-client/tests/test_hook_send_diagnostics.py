"""Synthetic native HTTP/SQLite observations; no real WeChat or business writes."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace as NS
import zipfile

import pytest
from test_hook_gateway import lab
from test_hook_history import history_lab
from test_hook_native_sender import setup
from chejin_worker_client.hook_history import HookHistoryCapture, HookHistoryQuery
from chejin_worker_client.hook_history_store import HookHistoryPageStore
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.hook_send_diagnostics import (
    SendHistoryDiagnostics, diagnostic_path, MAX_BYTES, MAX_EVENTS, _append,
)
from chejin_worker_client.shared_rules import hook_history_rules as rules
from chejin_worker_client.diagnostic_bundle import export_bundle

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def seed(root):
    permit = NS(task_id='task-send', reply_action_id='action-send')
    journal = root / 'transactions/hook/sends/worker/epoch/action-send'
    journal.mkdir(parents=True)
    intent = {'task_id': permit.task_id, 'reply_action_id': permit.reply_action_id,
              'reply_text_hash': hashlib.sha256('synthetic reply'.encode()).hexdigest(),
              'attempted_at': NOW.isoformat(), 'send_token': 'secret-do-not-export'}
    (journal / 'intent.json').write_text(json.dumps(intent))
    (journal / 'trigger.json').write_text('{}')
    return permit, journal


def test_zero_id_manual_same_text_and_disappearance_remain_distinct(history_lab, tmp_path):
    server, gateway, process, scope, delivery, _, db = history_lab
    permit, journal = seed(tmp_path)
    before = {p: p.read_bytes() for p in journal.iterdir()}
    db.execute('DELETE FROM MSG')
    db.execute('ALTER TABLE MSG ADD COLUMN Status INTEGER')
    db.execute('INSERT INTO MSG VALUES(751,0,1,1,1791543697,1,1,1,?,?,?)',
               (delivery.peer_wxid, 'synthetic reply', 1))

    def observe():
        recorder = SendHistoryDiagnostics(tmp_path, permit, journal, process, now=lambda: NOW)
        captured = []
        def save(capture):
            captured.append(capture)
            recorder.capture(capture)
        sync = HookHistorySynchronizer(gateway, save, lambda: (0, 0), send_diagnostics=True)
        _, coverage = sync.capture(process, scope, delivery, '1791543600')
        recorder.finish()
        assert 'diagnostic_status' not in json.dumps(coverage)
        assert len(captured) == 8  # Same two-pass query count, no extra Status query.
        return rules.validate_coverage(coverage, delivery.peer_wxid, 1791543600)

    assert observe()['reason_code'] == rules.OUTGOING_ID_PENDING
    assert observe()['reason_code'] == rules.OUTGOING_ID_PENDING  # coalesces repeats
    db.execute('INSERT INTO MSG VALUES(752,9002,1,1,1791543797,2,2,2,?,?,?)',
               (delivery.peer_wxid, 'synthetic reply', 2))
    assert observe()['reason_code'] == rules.OUTGOING_ID_PENDING
    db.execute('DELETE FROM MSG WHERE localId=751')
    assert observe()['ready']
    db.execute('UPDATE MSG SET Status=3 WHERE localId=752')
    assert observe()['ready']
    path = diagnostic_path(tmp_path, permit.task_id, permit.reply_action_id)
    data = json.loads(path.read_bytes())
    events = data['events']
    assert len(events) == 4 and events[0]['repeat_count'] == 2
    rows = [e['observation']['databases'][0]['rows'] for e in events]
    assert [[r['local_id'] for r in group] for group in rows] == [['751'], ['751','752'], ['752'], ['752']]
    assert rows[0][0]['server_id'] == '0' and rows[1][1]['server_id'] == '9002'
    assert rows[-1][0]['status'] == '3'
    assert 'synthetic reply' not in path.read_text() and 'secret-do-not-export' not in path.read_text()
    assert {p: p.read_bytes() for p in journal.iterdir()} == before
    assert not any(route == '/SendTextMsg' for route, *_ in server.requests)


@pytest.mark.parametrize('status', [None, '2', {'unexpected': 'value'}])
def test_status_diagnostic_does_not_enter_proof_or_break_restart(history_lab, status):
    _, gateway, process, _, delivery, catalog, _ = history_lab
    query = HookHistoryQuery(catalog.database('MSG0.db'), 'page', delivery, upper_local_id='4', page_size=2)
    original = gateway.history_capture(process, catalog, query)
    payload = original.payload
    for row in payload['data']:
        row['diagnostic_status'] = status
    source = HookHistoryCapture(catalog, replace(query, include_send_status=True), json.dumps(payload).encode())
    assert 'diagnostic_status' not in json.dumps(source.query.validate_response(source.payload))
    store = HookHistoryPageStore(max_storage_bytes=65536)
    result = store.append(source, captured_at=NOW.isoformat())
    restored = HookHistoryPageStore(max_storage_bytes=65536).read(result['event_uuid'])
    assert restored['raw_body'] == source.raw_body
    assert 'diagnostic_status' not in restored['projection_json']
    payload['data'][0]['StrTalker'] = 'wxid_wrong_peer'
    with pytest.raises(ValueError, match='PAGE_SCOPE_INVALID'):
        HookHistoryCapture(catalog, source.query, json.dumps(payload).encode())


def test_missing_status_and_failed_read_are_explicit(history_lab, tmp_path):
    _, gateway, process, scope, delivery, _, _ = history_lab
    permit, journal = seed(tmp_path)
    recorder = SendHistoryDiagnostics(tmp_path, permit, journal, process)
    captures = []
    def save(capture):
        recorder.capture(capture)
        captures.append(capture)
        if capture.query.kind == 'page':
            raise OSError('sensitive-detail-must-not-leak')
    sync = HookHistorySynchronizer(gateway, save, lambda: (0, 0), send_diagnostics=True)
    with pytest.raises(OSError) as caught:
        sync.capture(process, scope, delivery, '1791543600')
    recorder.finish(caught.value)
    assert not any(c.query.include_send_status for c in captures)
    data = json.loads(diagnostic_path(tmp_path, permit.task_id, permit.reply_action_id).read_bytes())
    event = data['events'][0]['observation']
    assert event['read_completed'] is False and event['error_type'] == 'OSError'
    assert event['databases'][0]['status_capability'] == 'column_not_available'
    assert 'sensitive-detail' not in json.dumps(data)


def test_submission_and_real_export_keep_return_and_mask_credentials(lab, tmp_path):
    server, _, process, scope, sender, permit, _, refresh, _ = setup(lab, tmp_path)
    root = tmp_path / 'home'
    sender.private_root = root / 'transactions/hook/sends'
    sender.diagnostic_root = root
    result = sender.execute(permit, process, scope, refresh)
    assert result['hook_submission_flag'] is True
    path = diagnostic_path(root, permit.task_id, permit.reply_action_id)
    value = json.loads(path.read_bytes())
    event = value['events'][0]['observation']
    assert event['hook_return'] == '1' and event['component_sha256'] == process.dll_sha256
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    destination = tmp_path / 'diagnostic.zip'
    export_bundle(root, destination)
    with zipfile.ZipFile(destination) as archive:
        name = f'tasks/{permit.task_id}/diagnostics/send-{permit.reply_action_id}.json'
        assert json.loads(archive.read(name))['events'][0]['observation']['hook_return'] == '1'
        assert '发送取证说明.txt' in archive.namelist()
        raw = b'\n'.join(archive.read(n) for n in archive.namelist())
        assert permit.send_token.encode() not in raw and permit.reply_text.encode() not in raw
        assert any(n.endswith('/response.json') for n in archive.namelist())
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == 1


def test_failed_diagnostic_write_does_not_change_send_or_original_receipts(lab, tmp_path, monkeypatch, caplog):
    import chejin_worker_client.hook_send_diagnostics as diagnostic
    server, _, process, scope, sender, permit, _, refresh, _ = setup(lab, tmp_path)
    sender.diagnostic_root = tmp_path
    monkeypatch.setattr(diagnostic, '_append', lambda *a: (_ for _ in ()).throw(OSError('secret-path')))
    result = sender.execute(permit, process, scope, refresh)
    assert result['call_completed'] and result['automatic_resend_allowed'] is False
    assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == 1
    assert 'HOOK_SEND_DIAGNOSTICS_WRITE_FAILED' in caplog.text and 'secret-path' not in caplog.text
    assert list(sender.private_root.rglob('outcome.json'))


def test_bounded_transitions_keep_first_latest_and_report_gaps(tmp_path):
    for index in range(MAX_EVENTS + 12):
        _append(tmp_path, 'task', 'action', {'kind': 'test', 'value': index}, NOW.isoformat())
    path = diagnostic_path(tmp_path, 'task', 'action')
    value = json.loads(path.read_bytes())
    assert len(value['events']) == MAX_EVENTS and value['omitted_events'] == 12
    assert value['events'][0]['observation']['value'] == 0
    assert value['events'][-1]['observation']['value'] == MAX_EVENTS + 11
    assert path.stat().st_size < MAX_BYTES


def test_legacy_missing_send_diagnostics_explicit_in_zip(tmp_path):
    permit, _ = seed(tmp_path / 'home')
    destination = tmp_path / 'legacy.zip'
    export_bundle(tmp_path / 'home', destination)
    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read('收集清单.json'))
        assert manifest['partial'] is True
        assert any(e.get('reason') == 'send_diagnostics_unavailable_legacy_or_capture_failed'
                   for e in manifest['sources'])


@pytest.mark.parametrize('backend_failure', [False, True])
def test_actual_runtime_retained_sync_records_before_backend_and_preserves_proof(history_lab, tmp_path, backend_failure):
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.hook_sender import RetainedNativeSendPermit
    from chejin_worker_client.hook_retention import HistoryArtifactBudget
    _, gateway, process, scope, delivery, _, db = history_lab
    basic, original = seed(tmp_path / 'old-home')
    rt = HookFormalRuntime.__new__(HookFormalRuntime)
    rt.private_root = tmp_path / 'new-home/transactions/hook'
    rt.credentials = NS(worker_id='worker')
    rt.scope, rt.process, rt.gateway = scope, process, gateway
    rt.account = {k: getattr(scope, k) for k in ('self_wxid', 'account_epoch', 'process_generation')}
    rt.history_budget = HistoryArtifactBudget(rt.private_root, maximum_bytes=4*1024*1024, retention_days=30)
    rt.clock = NS(now=lambda: NOW)
    rt.active_flow_id = None
    rt._history_profile = {}
    rt._history_pending_contexts = {}
    rt.heartbeat = lambda **kw: None
    rt.projection = NS(project_next=lambda _: None)
    rt.barrier = lambda _: (0, 0)
    rt.delivery = lambda _: delivery
    permit = RetainedNativeSendPermit('worker', 'instance', basic.task_id, basic.reply_action_id,
        scope.self_wxid, scope.account_epoch, process.generation, 1, basic.task_id, 'secret',
        source_journal_root=original)
    db.execute('ALTER TABLE MSG ADD COLUMN Status INTEGER')
    db.execute("UPDATE MSG SET StrContent='synthetic reply',MsgSvrID='0',Status=1 WHERE localId=2")
    path = diagnostic_path(tmp_path / 'new-home', basic.task_id, basic.reply_action_id)
    def backend(*args, **kwargs):
        assert kwargs['reconciliation'] is permit
        coverage = args[4]
        assert 'diagnostic_status' not in json.dumps(coverage)
        assert path.is_file()
        if backend_failure:
            raise RuntimeError('synthetic backend unavailable')
        return {'history_ready': False, 'reason_code': rules.OUTGOING_ID_PENDING}
    rt.client = NS(history_coverage=backend)
    binding = {'binding_id': delivery.binding_id, 'peer_wxid': delivery.peer_wxid,
               'history_since_create_time': '1791543600'}
    original_bytes = {p: p.read_bytes() for p in original.iterdir()}
    if backend_failure:
        with pytest.raises(RuntimeError, match='backend unavailable'):
            rt._sync(binding, show_progress=False, reconciliation=permit)
    else:
        assert rt._sync(binding, show_progress=False, reconciliation=permit)['reason_code'] == rules.OUTGOING_ID_PENDING
    event = json.loads(path.read_bytes())['events'][0]['observation']
    assert event['read_completed'] and event['gaps'] == []
    assert event['databases'][0]['rows'][0]['server_id'] == '0'
    assert {p: p.read_bytes() for p in original.iterdir()} == original_bytes


def test_status_projection_paginates_numeric_ids_without_changing_queries(history_lab):
    _, gateway, process, scope, delivery, _, db = history_lab
    gateway.max_response_bytes = 2*1024*1024
    db.execute('DELETE FROM MSG')
    db.execute('ALTER TABLE MSG ADD COLUMN Status INTEGER')
    db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?,?)', [
        (i, str(10000+i), 1, 1, 1791543697, i, i, i, delivery.peer_wxid, 'synthetic reply', None)
        for i in range(98, 205)])
    saved = []
    sync = HookHistorySynchronizer(gateway, saved.append, lambda: (0, 0), send_diagnostics=True)
    _, coverage = sync.capture(process, scope, delivery, '1791543600')
    assert rules.validate_coverage(coverage, delivery.peer_wxid, 1791543600)['ready']
    pages = [c for c in saved if c.query.kind == 'page']
    assert len(pages) == 4 and [len(c.payload['data']) for c in pages] == [100, 7, 100, 7]
    assert all(c.query.include_send_status for c in pages)
    assert 'diagnostic_status' not in json.dumps(coverage)


@pytest.mark.parametrize('finished_first', [False, True])
@pytest.mark.parametrize('finished_count', [0, 12])
def test_unfinished_originals_precede_supplemental_diagnostics(lab, tmp_path, finished_first, finished_count):
    import uuid
    server, _, process, scope, sender, permit, _, refresh, _ = setup(lab, tmp_path)
    root = tmp_path / 'home'
    sender.private_root, sender.diagnostic_root = root / 'transactions/hook/sends', root
    receipts = root / 'transactions/hook/send-receipts'
    receipts.mkdir(parents=True)
    pending = [replace(permit, task_id=str(uuid.uuid4()), reply_action_id=str(uuid.uuid4())) for _ in range(3)]
    completed = [replace(permit, task_id=str(uuid.uuid4()), reply_action_id=str(uuid.uuid4())) for _ in range(finished_count)]
    for item in (completed + pending if finished_first else pending + completed):
        sender.execute(item, process, scope, refresh)
        if item in completed:
            # Only completion classification is a fixture; send files come from producer.
            (receipts / (item.reply_action_id + '.json')).write_text(json.dumps({'automatic_resend_allowed': False}))
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    from chejin_worker_client.diagnostic_bundle import Collector
    dest = tmp_path / 'small.zip'
    Collector(root, dest, max_bytes=12000).run()
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
        required = []
        for item in pending:
            prefix = f'receipts/hook/sends/{item.worker_id}/{item.account_epoch}/{item.reply_action_id}/'
            required.extend(prefix + name for name in ('intent.json','trigger.json','outcome.json','response.json'))
        assert set(required) <= set(names)
        diagnostics = [n for n in names if '/diagnostics/send-' in n]
        assert diagnostics and min(names.index(n) for n in diagnostics) > max(names.index(n) for n in required)
        manifest = json.loads(z.read('收集清单.json'))
        if completed:
            assert any(e.get('reason') == 'total_size_limit' for e in manifest['sources'])
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == 3 + finished_count


@pytest.mark.parametrize('limit', ['bytes', 'files', 'time'])
def test_current_evidence_exceeding_budget_is_never_reported_complete(tmp_path, monkeypatch, limit):
    import chejin_worker_client.diagnostic_bundle as bundle
    permit, journal = seed(tmp_path / 'home')
    (journal / 'outcome.json').write_text(json.dumps({'call_completed': True, 'automatic_resend_allowed': False}))
    _append(tmp_path / 'home', permit.task_id, permit.reply_action_id, {'kind': 'synthetic'}, NOW.isoformat())
    kwargs = {'max_bytes': 80} if limit == 'bytes' else {'max_seconds': 0} if limit == 'time' else {}
    if limit == 'files':
        monkeypatch.setattr(bundle, 'MAX_FILES', 2)
    dest = tmp_path / 'limited.zip'
    bundle.Collector(tmp_path / 'home', dest, **kwargs).run()
    with zipfile.ZipFile(dest) as z:
        manifest = json.loads(z.read('收集清单.json'))
        assert manifest['partial'] is True
        reasons = {e.get('reason') for e in manifest['sources']}
        assert reasons & {'total_size_limit', 'priority_budget_limit', 'discovery_limit', 'time_or_file_limit'}
