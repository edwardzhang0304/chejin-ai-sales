"""Real Worker process; only Windows physical actions and transport faults are synthetic."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys
import time

from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.storage import (
    save_binding, load_binding, enqueue_c2_outbox, mark_c2_outbox_capability_paused,
    save_c2_ledger_terminal, load_c2_outbox_entry, load_runtime_control, update_install_business_blockers, c2_outbox_id,
)
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
from test_task_runner import FakeBridge


request = json.loads(Path(sys.argv[1]).read_text())
payload = request['payload']
binding = Binding(request['worker']['id'], request['worker']['worker_token'], 'followup-test', run_status='faulted')
if request.get('reuse_existing'):
    saved_binding = load_binding()
    assert saved_binding and saved_binding.worker_id == binding.worker_id and saved_binding.run_status == 'faulted'
    binding = saved_binding
    outbox_id = c2_outbox_id(payload)
    assert load_c2_outbox_entry(outbox_id)['payload'] == payload, 'Cannot replace the original persisted fixture'
else:
    save_binding(binding)
    for message in payload['messages']:
        save_c2_ledger_terminal(conversation_id=payload['conversation_id'], source_message_key=message['source_message_key'],
            origin_read_run_id=payload['read_run_id'], dedupe_key=message['dedupe_key'], message_type=message['message_type'],
            terminal_state=message['item_state'], ingest_state='waiting')
    outbox_id = enqueue_c2_outbox(payload)
    mark_c2_outbox_capability_paused(outbox_id, 'WORKER_INFLIGHT_FLOW_MISMATCH')
    if request.get('legacy_terminal_without_proof'):
        from chejin_worker_client.storage import db_connection
        with db_connection() as db:
            # Historical input fixture only, before the real runner starts.
            # No modern proof is supplied and no successful state is patched.
            db.execute("UPDATE c2_ingest_outbox SET status='conversation_terminated',last_error='LEAD_INVALID' WHERE outbox_id=?",(outbox_id,))
            db.commit()
    if request['mode'] == 'active_flow':
        from chejin_worker_client.storage import begin_runtime_flow, save_c2_state
        begin_runtime_flow(payload['read_run_id'], 'c2_read')
        save_c2_state('inflight_finish_receipt:' + payload['read_run_id'], {
            'terminal_kind': 'technical_failed', 'conversation_id': payload['conversation_id'],
            'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
    if request['mode'] == 'partitions':
        from chejin_worker_client.c2_outbox_recovery import split_ingest_payload
        from chejin_worker_client.storage import transition_c2_outbox
        for part in split_ingest_payload(payload):
            enqueue_c2_outbox(part)
        transition_c2_outbox(outbox_id, status='split_pending', error='C2_INGEST_PAYLOAD_TOO_LARGE')
        transition_c2_outbox(outbox_id, status='split_completed', error=None)
api = WorkerApiClient(request['url'] + '/api')
exchanges, errors, physical = [], [], []
clicked = False
lost = False
storage_failure = False
rollback_snapshot = None
original_send = api.session.send
transport_failures = []
heartbeat_times = []
capability_wait_started = time.monotonic()


def old_records_settled():
    from chejin_worker_client.storage import has_pending_c2_outbox
    expected = ('confirmed' if request['mode'] == 'fact_settlement' else
                'split_completed' if request['mode'] == 'partitions' else 'conversation_terminated')
    expected = request.get('expected_outbox_status', expected)
    return load_c2_outbox_entry(outbox_id)['status'] == expected and not has_pending_c2_outbox()


def transport(prepared, **kwargs):
    global lost
    path = prepared.url.split('/api')[-1]
    if path.endswith('/heartbeat'):
        heartbeat_times.append(time.monotonic())
    if path.endswith('/messages/ingest'):
        if request['mode'] == 'old_backend':
            assert time.monotonic() - capability_wait_started >= 2, 'No doomed ingest before capability refresh'
        if request['mode'] in {'html_502', 'json_503', 'disconnect', 'timeout'} and len(transport_failures) < 3:
            import requests
            transport_failures.append(time.monotonic())
            if request['mode'] == 'disconnect':
                raise requests.ConnectionError('Controlled transport disconnect')
            if request['mode'] == 'timeout':
                raise requests.Timeout('Controlled transport timeout')
            response = requests.Response()
            response.status_code = 502 if request['mode'] == 'html_502' else 503
            response._content = (b'<html>Temporary gateway failure</html>' if request['mode'] == 'html_502'
                                 else b'{"code":"TEMPORARY_TEST_ERROR","message":"retry later"}')
            response.url = prepared.url
            return response
    response = original_send(prepared, **kwargs)
    if (request['mode'] == 'old_backend' and path.endswith(('/heartbeat', '/run-status', '/client-profile'))
            and time.monotonic() - capability_wait_started < 2 and response.status_code == 200):
        body = response.json()
        body.get('data', {}).get('pending_read_recovery', {}).pop('terminal_settlement_protocol_version', None)
        response._content = json.dumps(body).encode()
    if path.endswith(('/messages/ingest', '/run-status', '/pull', '/claim', '/inflight-flow/finish', '/success-settlement', '/invite-sent')):
        exchanges.append({'path': path, 'status': response.status_code,
                          'request': json.loads(prepared.body) if prepared.body else None,
                          'response': response.json(), 'clicked': clicked})
    if (request['mode'] in {'response_lost', 'partitions'} and path.endswith('/messages/ingest')
            and response.status_code == 200 and not lost):
        lost = True
        raise TimeoutError('Controlled response loss after server commit')
    return response


api.session.send = transport

if os.environ.get('CHEJIN_TEST_DISABLE_TERMINAL_CONSUMPTION') == '1':
    # Negative control: HTTP succeeds but the new Worker consumer is absent.
    # The normal positive assertion must fail on the still-pending Outbox.
    import chejin_worker_client.task_runner as runner_module
    runner_module.settle_c2_outbox = lambda *args, **kwargs: None

if request['mode'] in {'save_before', 'save_inside', 'save_after_commit'}:
    import chejin_worker_client.storage as storage
    import chejin_worker_client.task_runner as runner_module
    original_settle = runner_module.settle_c2_outbox
    original_connection = storage.db_connection

    @contextmanager
    def interrupted_connection():
        with original_connection() as connection:
            class ConnectionBoundary:
                def __getattr__(self, name):
                    return getattr(connection, name)

                def execute(self, sql, parameters=()):
                    global storage_failure, rollback_snapshot
                    result = connection.execute(sql, parameters)
                    if request['mode'] == 'save_inside' and 'UPDATE c2_message_ledger SET terminal_state=?' in sql and not storage_failure:
                        storage_failure = True
                        with sqlite3.connect('file:' + str(storage.DB_FILE) + '?mode=ro', uri=True) as reader:
                            rollback_snapshot = {
                                'waiting_ledger': reader.execute("SELECT COUNT(*) FROM c2_message_ledger WHERE ingest_state='waiting'").fetchone()[0],
                                'terminal_outbox': reader.execute("SELECT COUNT(*) FROM c2_ingest_outbox WHERE status='conversation_terminated'").fetchone()[0],
                            }
                        raise sqlite3.OperationalError('Controlled failure inside SQLite transaction')
                    return result
            yield ConnectionBoundary()

    def interrupted_settle(*args, **kwargs):
        global storage_failure
        if request['mode'] == 'save_before' and not storage_failure:
            storage_failure = True
            raise OSError('Controlled failure before SQLite transaction')
        result = original_settle(*args, **kwargs)
        if request['mode'] == 'save_after_commit' and not storage_failure:
            storage_failure = True
            raise OSError('Controlled failure after successful SQLite commit')
        return result

    storage.db_connection = interrupted_connection
    runner_module.settle_c2_outbox = interrupted_settle


class PhysicalBoundary(FakeBridge):
    def sidecar_active(self):
        return False

    def run_add_friend(self, task, emit_step, cancel_check=None):
        assert clicked and runner.binding.run_status == 'running'
        assert old_records_settled()
        assert task.id == request['next_task_id'], 'Old customer must never be acted on'
        physical.append(task.id)
        assert physical == [request['next_task_id']], 'No repeated physical action'
        return super().run_add_friend(task, emit_step, cancel_check)

    def get_messages(self, *args, **kwargs):
        raise AssertionError('Old message recovery must not read WeChat')

    def locate_chat(self, *args, **kwargs):
        raise AssertionError('Old message recovery must not locate WeChat')


bridge = PhysicalBoundary(RpaResult(ok=True, result_code='invite_sent', message='Synthetic physical result for customer B'))
noop = lambda *_: None
runner = TaskRunner(api, bridge, on_profile=noop, on_status=noop, on_step=noop,
                    on_task=noop, on_result=noop, on_error=errors.append)
runner.start(load_binding())
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if old_records_settled() and runner.fault_recovery_state()['ready']:
            break
        time.sleep(.1)
    current = load_c2_outbox_entry(outbox_id)
    assert old_records_settled(), {'status': current['status'], 'last_error': current['last_error'], 'errors': errors}
    assert current['payload'] == payload
    assert runner.binding.run_status == load_binding().run_status == 'faulted'
    assert not physical
    assert not any(item['path'].endswith('/claim') for item in exchanges)
    assert runner.fault_recovery_state()['ready'], runner.fault_recovery_state()
    before_click = {'status': load_binding().run_status, 'recovery': runner.fault_recovery_state(),
                    'blockers': update_install_business_blockers()}
    # This is the production command called by both original Start buttons.
    # Native Windows widget/transport is separately labelled, not simulated here.
    clicked = True
    assert runner.set_run_status('running'), errors
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        finished = [item for item in exchanges if item['path'].endswith('/inflight-flow/finish')
                    and item['status'] == 200 and item['request']['flow_id'] == request['next_task_id']]
        if finished and not load_runtime_control().get('inflight_flow_id') and not lock_summary().get('locked'):
            break
        time.sleep(.1)
    assert physical == [request['next_task_id']], {'physical': physical, 'errors': errors}
    assert finished, {'errors': errors, 'exchanges': exchanges}
    assert runner.binding.run_status == load_binding().run_status == 'running'
    assert not load_runtime_control().get('inflight_flow_id') and not lock_summary().get('locked')
    assert any(item['path'].endswith('/claim') and item['status'] == 200 for item in exchanges)
    if transport_failures:
        assert len(transport_failures) == 3
        assert all(right - left >= .8 for left, right in zip(transport_failures, transport_failures[1:])), transport_failures
        assert any(transport_failures[0] < item < transport_failures[-1] for item in heartbeat_times), 'Heartbeat must continue during retries'
    Path(sys.argv[2]).write_text(json.dumps({'before_click': before_click, 'exchanges': exchanges,
        'transport_failure_times': transport_failures, 'heartbeat_count': len(heartbeat_times),
        'physical_customer_b_only': physical, 'response_loss_injected': lost,
        'storage_failure_injected': storage_failure, 'rollback_snapshot': rollback_snapshot,
        'after': {'status': load_binding().run_status, 'blockers': update_install_business_blockers(),
                  'flow': load_runtime_control().get('inflight_flow_id'), 'locked': lock_summary().get('locked')}},
        ensure_ascii=False, indent=2))
finally:
    runner.stop_for_update(timeout_seconds=5)
