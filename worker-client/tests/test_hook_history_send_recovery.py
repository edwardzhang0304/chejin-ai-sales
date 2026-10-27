"""Original send journals + real SQLite/loopback reads; backend settlement is simulated."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from test_hook_gateway import lab
from test_hook_history import history_lab
from test_hook_retained_send import retained
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.shared_rules import hook_history_rules


@pytest.mark.parametrize('result', ['observed', 'pending', 'timeout', 'restricted'])
@pytest.mark.parametrize('changing_history', [None, 'rows', 'catalog'])
def test_restart_reconciles_numeric_history_without_replaying_original_send(
        history_lab, retained, result, changing_history):
    server, gateway, process, scope, delivery, _, db = history_lab
    root, credentials, original_scope, binding, metadata, folder = retained
    private = root / 'restarted-worker'
    sends = private / 'sends'
    sends.mkdir(parents=True)
    (root / credentials.worker_id).rename(sends / credentials.worker_id)
    folder = sends / credentials.worker_id / original_scope.account_epoch / metadata['reply_action_id']
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    metadata = {**metadata, 'original_process_generation': original_scope.process_generation}
    # RCV-1: the old process's unknown call survives the restart. Only explicit
    # platform proof admits reads in the verified new generation; the original
    # business journal below must still be reconciled, never resent.
    from chejin_worker_client import hook_active_calls, storage
    hook_active_calls.reserve(original_scope.process_generation,'old-unknown-call','send_text')
    hook_active_calls.finish(original_scope.process_generation,'old-unknown-call',False)
    gateway.generation_exited=lambda generation:generation==original_scope.process_generation
    scope = replace(scope, account_epoch=original_scope.account_epoch)
    delivery = replace(delivery, account_epoch=scope.account_epoch)
    db.execute('DELETE FROM MSG')
    db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)', [
        (9, '10009', 1, 0, 1791543697, 9, 9, 9, delivery.peer_wxid, 'synthetic inbound'),
        (10, '10010', 1, int(result == 'observed'), 1791543698, 10, 10, 10,
         delivery.peer_wxid, 'synthetic reply'),
    ])
    checked = {}
    calls = []
    captures = []
    mutate = [False]

    def save(capture):
        captures.append(capture)
        if changing_history == 'rows' and mutate[0] and capture.query.kind == 'count':
            mutate[0] = False
            db.execute("UPDATE MSG SET StrContent='synthetic updated inbound' WHERE localId=9")

    def sync(current_binding, *, reconciliation):
        assert current_binding == binding
        assert reconciliation.process_generation == original_scope.process_generation
        assert reconciliation.journal_sha256 == metadata['journal_sha256']
        assert not hasattr(reconciliation, 'reply_text')
        synchronizer = HookHistorySynchronizer(gateway, save, lambda: (0, 0))
        _, coverage = synchronizer.capture(process, scope, delivery, '1791543600')
        checked.update(hook_history_rules.validate_coverage(coverage, delivery.peer_wxid, 1791543600))
        assert checked['ready']
        calls.append('history')
        return {'history_ready': True}

    class Client:
        def observe_send(self, cred, permit, outcome):
            assert calls[-1] == 'history' and cred == credentials
            assert outcome['automatic_resend_allowed'] is False
            assert outcome['completed_at'] == '2026-10-11T11:47:13+00:00'
            calls.append('observe')
            send_rows = [row for row in checked['text_rows'] if row['IsSender'] == '1']
            return {'unique_local_send_observed': len(send_rows) == 1,
                    'deadline_elapsed': result == 'timeout',
                    'early_settlement_reason': 'HOOK_FRIEND_VERIFICATION_REQUIRED' if result=='restricted' else None}

        def native_send_settle(self, cred, permit, outcome):
            assert calls[-1] == 'observe' and result != 'pending'
            calls.append('settle')
            return {'local_send_observed': result == 'observed',
                    'ack': {'send_result': 'sent' if result == 'observed' else 'unknown'}}

    def restarted_dispatcher():
        dispatcher = HookBusinessDispatcher(Client(), private, lambda *a: None, lambda *a: None,
                                             automatic_transcription_disabled=lambda: True)
        dispatcher.runtime = SimpleNamespace(credentials=credentials, scope=scope, sync=sync,
                                             progress=lambda *a: None, active_task_id=metadata['task_id'])
        return dispatcher

    # Reproduce the old failed page using the original lexical ORDER BY.
    query = server.query_response
    server.query_response = lambda body: query({
        **body, 'sql': body['sql'].replace('ORDER BY MSG.localId ASC', 'ORDER BY localId ASC'),
    })
    with pytest.raises(ValueError, match='HOOK_HISTORY_PAGE_CURSOR_INVALID'):
        restarted_dispatcher()._recover_send(binding, metadata)
    assert calls == []
    assert not (private / 'send-receipts').exists()
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before

    # A new dispatcher uses the same original credentials/journals and fixed read.
    server.query_response = query
    dispatcher = restarted_dispatcher()
    from chejin_worker_client.hook_progress import HookProgressTimeline
    from copy import deepcopy
    timeline=HookProgressTimeline();events=[]
    def progress(event):events.append(event);timeline.apply(event)
    dispatcher._progress_runner=SimpleNamespace(on_runtime_process=progress)
    if changing_history:
        import threading
        from chejin_worker_client.hook_worker_services import HookWorkerServices
        mutate[0] = True
        def change_catalog(path):
            if changing_history == 'catalog' and mutate[0] and path == '/QueryDB':
                mutate[0] = False
                server.catalog_entries[0]['dbhandle'] = 'reopened-handle'
        server.on_request = change_catalog
        runner = SimpleNamespace(binding=SimpleNamespace(run_status='paused'), stop_event=threading.Event())
        waits=[]; faults=[]; completed=[]
        def tick(*args):
            if waits:
                assert calls == []
                assert {p.name:p.read_bytes() for p in folder.iterdir()} == before
                assert not (private/'send-receipts').exists()
            completed.append(dispatcher._recover_send(binding,metadata))
            runner.stop_event.set()
        def fault(runner,code,**kwargs):
            faults.append(code); runner.stop_event.set()
        services=HookWorkerServices.__new__(HookWorkerServices)
        services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,
            _report_wait=lambda code,**kwargs:waits.append(code))
        services._fault=fault
        services.business_loop(runner,lambda _:None)
        expected_code = 'HOOK_HISTORY_CHANGED_DURING_READ' if changing_history == 'rows' else 'HOOK_HISTORY_CATALOG_CHANGED'
        assert not faults and waits==[expected_code]
        assert completed==[result!='pending'] and runner.binding.run_status=='paused'
    else:
        assert dispatcher._recover_send(binding, metadata) is (result != 'pending')
    assert calls == ['history', 'observe'] + ([] if result == 'pending' else ['settle'])
    receipt = private / 'send-receipts' / f"{metadata['reply_action_id']}.json"
    assert receipt.exists() is (result != 'pending')
    if receipt.exists():
        assert json.loads(receipt.read_bytes()) == {'local_send_observed': result == 'observed',
            'ack': {'send_result': 'sent' if result == 'observed' else 'unknown'}}
        assert dispatcher.runtime._send_result_pending is False
        assert dispatcher.runtime._finish_note == ('sent' if result == 'observed' else 'send_unknown')
    if result=='pending':
        first=deepcopy(timeline.steps)
        for _ in range(2):assert dispatcher._recover_send(binding,metadata) is False
        assert timeline.steps==first
        assert all(e.get('state')=='waiting' for e in events if e.get('phase')=='send_confirm')
    else:
        assert timeline.latest[(metadata['task_id'],'send_confirm')]['state']=='done'
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not any(path == '/SendTextMsg' for path, *_ in server.requests)
    assert hook_active_calls.unresolved(original_scope.process_generation)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT old_generation FROM hook_process_recovery').fetchone()[0]==original_scope.process_generation
    assert not list(private.rglob('not-triggered.json'))


@pytest.mark.parametrize('deadline,restricted', [(False,False),(True,False),(False,True)])
def test_secondary_retained_entry_uses_same_unknown_gate_and_original_deadline(retained,deadline,restricted):
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    root,credentials,scope,binding,metadata,folder=retained
    source=root/'original';(source/'sends').mkdir(parents=True)
    (root/credentials.worker_id).rename(source/'sends'/credentials.worker_id)
    folder=source/'sends'/credentials.worker_id/scope.account_epoch/metadata['reply_action_id']
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    calls=[]
    runtime=HookFormalRuntime.__new__(HookFormalRuntime)
    runtime.scope=scope;runtime.credentials=credentials;runtime.private_root=root/'restored'
    def sync(b,*,show_progress,reconciliation):
        assert b==binding and reconciliation.journal_sha256==metadata['journal_sha256']
        calls.append('history')
        return {'history_ready':False,'reason_code':hook_history_rules.OUTGOING_ID_PENDING,
                'reconciliation_only':True,'recovery_settlement_allowed':True}
    def observe(*args):calls.append('observe');return {'deadline_elapsed':deadline,'early_settlement_reason':'HOOK_FRIEND_VERIFICATION_REQUIRED' if restricted else None}
    def settle(*args):calls.append('settle');return {'local_send_observed':False,'automatic_resend_allowed':False}
    runtime.sync=sync;runtime.client=SimpleNamespace(observe_send=observe,native_send_settle=settle)
    if deadline or restricted:
        result=runtime.settle_retained_send(binding,source,metadata)
        assert result=={'local_send_observed':False,'automatic_resend_allowed':False}
        assert calls==['history','observe','settle']
    else:
        with pytest.raises(ValueError,match='HOOK_SEND_CONFIRMATION_PENDING'):
            runtime.settle_retained_send(binding,source,metadata)
        assert calls==['history','observe']
        assert not list(runtime.private_root.rglob('*.json'))
    assert before=={p.name:p.read_bytes() for p in folder.iterdir()}


@pytest.mark.parametrize('completed,reason,expected',[(False,'HOOK_FRIEND_VERIFICATION_REQUIRED',False),
    (True,'untrusted_reason',False),(True,'HOOK_FRIEND_VERIFICATION_REQUIRED',True)])
def test_early_closure_needs_completed_local_call_and_server_reason(completed,reason,expected):
    from chejin_worker_client.hook_sender import send_observation_can_settle
    assert send_observation_can_settle({'early_settlement_reason':reason},{'call_completed':completed}) is expected
