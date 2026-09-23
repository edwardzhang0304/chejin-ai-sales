"""Recovery entry regression: real Worker/SQLite, controlled API and desktop."""
from copy import deepcopy
import json
import pytest
from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import Binding, RpaResult, Task
from chejin_worker_client import storage
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


@pytest.mark.parametrize('mode', ['pending', 'running'])
def test_recovered_reply_keeps_grouping_failure_receipt(harness, tmp_path, mode):
    task = harness.make_chat_reply_task(task_id='recover-bubble-task',status=mode)
    api = FakeApi(task)
    harness.authorize_chat_reply_target(api)
    bridge = FakeBridge(RpaResult(ok=True,result_code='unused'))
    runner, seen = harness.make_runner(api,bridge)
    binding = Binding('worker-1','test-token','client-1',run_status='running')
    runner.binding = binding
    storage.save_binding(binding)
    api._task_lease_token = lambda _: 1 if mode=='running' else 0
    delivered=[]
    def settle(_binding,record):
        delivered.append(deepcopy(record))
        return Task.from_api({'id':task.id,'task_type':'chat_reply','status':'failed',
            'error_code':record['error_code'],'reply_read_failure':deepcopy(record)})
    api.settle_reply_read_failure = settle
    reads=[]
    def read(**kwargs):
        reads.append(kwargs.get('target_mode'))
        return sidecar.sanitize_sidecar_contract_output(sidecar.exception_payload_for_sidecar(
            RuntimeError('C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED')))
    bridge.get_messages=read
    assert runner._start_inflight_flow(binding,flow_id=task.id,flow_kind='task',conversation_id='conv-1')
    runner._execute_c2_reply_recovery(binding,task,mode)
    receipt=storage.load_c2_state(runner._inflight_finish_receipt_key(task.id))
    proof={'mode':mode,'reads':reads,'delivered':delivered,'receipt':receipt,
           'events':api.events,'results':[vars(v) for v in seen['results']],
           'logs':storage.read_logs(limit=60),'status':binding.run_status}
    (tmp_path/'proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2,default=str))
    assert reads, proof
    assert seen['results'][-1].error_code=='C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED', proof
    assert binding.run_status=='faulted' and not bridge.sent_replies, proof
    assert receipt.get('reply_read_failure',{}).get('conversation_id')=='conv-1', proof
    assert len(delivered)==1 and receipt['reply_read_failure_confirmed'], proof
