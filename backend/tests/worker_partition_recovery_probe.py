"""Independent real Worker/HTTP/SQLite probe; only physical WeChat is replaced."""
import json
from pathlib import Path
import sys
import time
import requests

from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client import storage
from chejin_worker_client.c2_outbox_recovery import split_ingest_payload
from chejin_worker_client.task_runner import TaskRunner
from test_task_runner import FakeBridge

request = json.loads(Path(sys.argv[1]).read_text())
evidence_path = Path(sys.argv[2])
payload = request['payload']
binding = Binding(request['worker']['id'], request['worker']['worker_token'], 'followup-test', run_status='running')
storage.save_binding(binding)
for item in payload['messages']:
    storage.save_c2_ledger_terminal(conversation_id=payload['conversation_id'], source_message_key=item['source_message_key'],
        origin_read_run_id=payload['read_run_id'], dedupe_key=item['dedupe_key'], message_type=item['message_type'],
        terminal_state=item['item_state'], ingest_state='waiting')
parent = storage.enqueue_c2_outbox(payload)
parts = split_ingest_payload(payload)
assert len(parts) >= 2
part_ids = [storage.enqueue_c2_outbox(part) for part in parts]
storage.transition_c2_outbox(parent, status='split_pending', error='C2_INGEST_PAYLOAD_TOO_LARGE')
storage.transition_c2_outbox(parent, status='split_completed', error=None)
storage.begin_runtime_flow(payload['read_run_id'], 'c2_read')
api = WorkerApiClient(request['url'] + '/api')
api.inflight_flow_id = payload['read_run_id']
exchanges, errors, physical = [], [], []
original_send = api.session.send
def transport(prepared, **kwargs):
    response = original_send(prepared, **kwargs)
    path = prepared.url.split('/api')[-1]
    if path.endswith(('/messages/ingest', '/inflight-flow/finish', '/run-status', '/claim')):
        body = json.loads(prepared.body) if prepared.body else {}
        exchanges.append({'path': path, 'status': response.status_code, 'response': response.json(),
            'partition': body.get('evidence', {}).get('ingest_partition'), 'terminal_kind': body.get('terminal_kind')})
    return response
api.session.send = transport
class Boundary(FakeBridge):
    def sidecar_active(self): return False
    def run_add_friend(self, task, emit_step, cancel_check=None):
        assert task.id == request['next_task_id']
        physical.append(task.id)
        return super().run_add_friend(task, emit_step, cancel_check)
    def get_messages(self, *args, **kwargs): raise AssertionError('Recovery must not read WeChat')
    def locate_chat(self, *args, **kwargs): raise AssertionError('Recovery must not locate WeChat')
noop = lambda *_: None
runner = TaskRunner(api, Boundary(RpaResult(ok=True, result_code='invite_sent', message='Controlled physical result')),
    on_profile=noop, on_status=noop, on_step=noop, on_task=noop, on_result=noop, on_error=errors.append)
runner.binding = binding
if request['first_accepted']:
    delivered = runner._attempt_c2_outbox_delivery(binding=binding, payload=parts[0], outbox_id=part_ids[0], operation='original_read')
    assert delivered['ok'], str(delivered)
    assert storage.load_c2_outbox_entry(part_ids[0])['status'] == 'confirmed'
# The sole variant is normal acceptance of the first part BEFORE invalidation.
# No test re-posts confirmed work, consumes a receipt or closes a Flow for recovery.
invalid = requests.post(request['url'] + '/api/leads/' + request['lead_id'] + '/mark-invalid', json={'invalid_reason': 'test_data'}, timeout=10)
assert invalid.status_code == 200, invalid.text
api.set_run_status(binding, 'faulted')
binding.run_status = 'faulted'
storage.save_binding(binding)
storage.save_c2_state('inflight_finish_receipt:' + payload['read_run_id'], {
    'terminal_kind': 'technical_failed', 'conversation_id': payload['conversation_id'], 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
before = {'part_states': [storage.load_c2_outbox_entry(i)['status'] for i in part_ids],
          'waiting_ids': [r['outbox_id'] for r in storage.list_c2_outbox_waiting()]}
runner.start(storage.load_binding())
try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not storage.has_pending_c2_outbox() and runner.fault_recovery_state()['ready']: break
        time.sleep(.1)
    after = {'ready': runner.fault_recovery_state(), 'part_states': [storage.load_c2_outbox_entry(i)['status'] for i in part_ids],
             'blockers': storage.update_install_business_blockers(), 'runtime': storage.load_runtime_control(),
             'exchanges': exchanges, 'errors': errors, 'physical': physical}
    evidence_path.write_text(json.dumps({'first_accepted': request['first_accepted'], 'before': before, 'after': after}, ensure_ascii=False, indent=2))
    assert runner.fault_recovery_state()['ready'], after
    assert not physical
    assert runner.set_run_status('running'), errors
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if physical and not storage.load_runtime_control().get('inflight_flow_id'): break
        time.sleep(.1)
    assert physical == [request['next_task_id']], physical
    assert not storage.load_runtime_control().get('inflight_flow_id')
    after['next_customer_completed'] = True
    evidence_path.write_text(json.dumps({'first_accepted': request['first_accepted'], 'before': before, 'after': after}, ensure_ascii=False, indent=2))
finally:
    runner.stop_for_update(timeout_seconds=5)
