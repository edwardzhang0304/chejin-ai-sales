"""Persist a synthetic old read, exit, then recover that SQLite in a new process.

Capture is fixture construction, not an old Windows executable. Resume uses
unmodified production Worker threads and HTTP; all desktop calls are forbidden.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from chejin_worker_client import storage
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import lock_summary
from test_task_runner import FakeBridge

req = json.loads(Path(sys.argv[1]).read_text())
mode = sys.argv[2]
output = Path(sys.argv[3])
payload = req['payload']
outbox = storage.c2_outbox_id(payload)
if mode == 'capture':
    binding = Binding(req['worker']['id'], req['worker']['worker_token'], 'followup-test', run_status='faulted')
    storage.save_binding(binding)
    for item in payload['messages']:
        storage.save_c2_ledger_terminal(conversation_id=payload['conversation_id'], source_message_key=item['source_message_key'],
            origin_read_run_id=payload['read_run_id'], dedupe_key=item['dedupe_key'], message_type=item['message_type'],
            terminal_state=item['item_state'], ingest_state='waiting')
    assert storage.enqueue_c2_outbox(payload) == outbox
    storage.mark_c2_outbox_capability_paused(outbox, 'MESSAGE_CONTRACT_REVISION_MISMATCH')
    if not req['closed']:
        storage.begin_runtime_flow(payload['read_run_id'], 'c2_read')
        storage.save_c2_state('inflight_finish_receipt:' + payload['read_run_id'], {
            'terminal_kind': 'technical_failed', 'conversation_id': payload['conversation_id'],
            'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
    output.write_text(json.dumps({'outbox': outbox, 'status': storage.load_c2_outbox_entry(outbox)['status']}))
    sys.exit(0)

binding = storage.load_binding()
assert binding and binding.run_status == 'faulted'
assert storage.load_c2_outbox_entry(outbox)['payload'] == payload
database = output.parent / 'client/worker_client.sqlite3'
def original_bytes():
    with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
        return db.execute('SELECT payload_json FROM c2_ingest_outbox WHERE outbox_id=?', (outbox,)).fetchone()[0].encode()
before = original_bytes()
api = WorkerApiClient(req['url'] + '/api')
send = api.session.send
exchanges, physical, errors = [], [], []
lost = False
def transport(request, **kwargs):
    global lost
    response = send(request, **kwargs)
    if request.url.endswith(('/messages/ingest', '/inflight-flow/finish', '/tasks/pull', '/claim')):
        body = json.loads(request.body) if request.body else {}
        exchanges.append({'path': request.url.split('/api')[-1], 'status': response.status_code,
                          'body': body, 'response': response.json()})
        if (req.get('response_lost') and not lost and response.status_code == 200
                and request.url.endswith('/messages/ingest')):
            lost = True
            raise TimeoutError('controlled response lost after backend commit')
    return response
api.session.send = transport
bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
bridge.sidecar_active = lambda: False
def forbidden(*args, **kwargs):
    physical.append('unexpected_desktop_operation')
    raise AssertionError('Stored read recovery must not read, click, type or send')
for name in ('get_messages', 'list_sessions', 'locate_chat', 'send_reply', 'run_add_friend',
             'prepare_voice_action', 'execute_voice_action', 'transcribe_voice',
             'prepare_image_action', 'execute_image_action'):
    if hasattr(bridge, name): setattr(bridge, name, forbidden)
noop = lambda *_: None
runner = TaskRunner(api, bridge, on_profile=noop, on_status=noop, on_step=noop, on_task=noop,
                    on_result=noop, on_error=errors.append, can_pull_tasks=lambda: False)
runner.start(binding)
try:
    deadline = time.monotonic() + (3 if req.get('expect_rejected') else 18)
    while time.monotonic() < deadline:
        if storage.load_c2_outbox_entry(outbox)['status'] == 'confirmed' and runner.fault_recovery_state()['ready']:
            break
        time.sleep(.1)
    result = {'outbox': storage.load_c2_outbox_entry(outbox)['status'], 'runtime': storage.load_runtime_control(),
        'blockers': storage.update_install_business_blockers(), 'recovery': runner.fault_recovery_state(),
        'status': storage.load_binding().run_status, 'lock': lock_summary(), 'physical': physical,
        'http': exchanges, 'errors': errors, 'response_loss_injected': lost,
        'before_sha256': hashlib.sha256(before).hexdigest(), 'after_sha256': hashlib.sha256(original_bytes()).hexdigest()}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert original_bytes() == before
    assert not physical and result['status'] == 'faulted'
    if req.get('expect_rejected'):
        assert result['outbox'] != 'confirmed'
        assert result['blockers']['pending_c2_outbox'] > 0
        assert not any(e['path'].endswith('/inflight-flow/finish') for e in exchanges)
    else:
        assert result['outbox'] == 'confirmed', result
        assert not result['runtime'].get('inflight_flow_id'), result
        assert result['blockers']['pending_c2_outbox'] == result['blockers']['waiting_ledger'] == 0, result
        assert result['recovery']['ready'] and not result['lock'].get('locked'), result
        assert bool(req.get('response_lost')) == lost
finally:
    runner.stop_for_update(timeout_seconds=5)
