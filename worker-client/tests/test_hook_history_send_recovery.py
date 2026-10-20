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


@pytest.mark.parametrize('result', ['observed', 'pending', 'timeout'])
def test_restart_reconciles_numeric_history_without_replaying_original_send(
        history_lab, retained, result):
    server, gateway, process, scope, delivery, _, db = history_lab
    root, credentials, original_scope, binding, metadata, folder = retained
    private = root / 'restarted-worker'
    sends = private / 'sends'
    sends.mkdir(parents=True)
    (root / credentials.worker_id).rename(sends / credentials.worker_id)
    folder = sends / credentials.worker_id / original_scope.account_epoch / metadata['reply_action_id']
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    metadata = {**metadata, 'original_process_generation': original_scope.process_generation}
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

    def sync(current_binding, *, reconciliation):
        assert current_binding == binding
        assert reconciliation.process_generation == original_scope.process_generation
        assert reconciliation.journal_sha256 == metadata['journal_sha256']
        assert not hasattr(reconciliation, 'reply_text')
        synchronizer = HookHistorySynchronizer(gateway, captures.append, lambda: (0, 0))
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
                    'deadline_elapsed': result == 'timeout'}

        def native_send_settle(self, cred, permit, outcome):
            assert calls[-1] == 'observe' and result != 'pending'
            calls.append('settle')
            return {'local_send_observed': result == 'observed'}

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
    assert dispatcher._recover_send(binding, metadata) is (result != 'pending')
    assert calls == ['history', 'observe'] + ([] if result == 'pending' else ['settle'])
    receipt = private / 'send-receipts' / f"{metadata['reply_action_id']}.json"
    assert receipt.exists() is (result != 'pending')
    if receipt.exists():
        assert json.loads(receipt.read_bytes()) == {'local_send_observed': result == 'observed'}
        assert dispatcher.runtime._send_result_pending is (result != 'observed')
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not any(path == '/SendTextMsg' for path, *_ in server.requests)
    assert not list(private.rglob('not-triggered.json'))
