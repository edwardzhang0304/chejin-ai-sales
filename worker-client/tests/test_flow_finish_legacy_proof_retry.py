"""Independent local state-machine probe. API/physical boundaries controlled."""
import json
import pytest
import test_task_runner as fixtures
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.api import ApiError
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.storage import begin_runtime_flow, request_runtime_pause, save_c2_state, load_c2_state, load_runtime_control

@pytest.mark.parametrize('mode', ['proof_online', 'proof_timeout_once', 'proof_timeout_without_priority'])
def test_legacy_proof_remains_reachable_after_transient_failure(tmp_path, mode):
    fixture = fixtures.TaskRunnerTest(); fixture.setUp()
    flow, conversation = 'independent-legacy-flow', 'independent-legacy-conversation'
    class Boundary(FakeApi):
        def __init__(self):
            super().__init__(None); self.proofs = 0; self.finishes = []
        def finish_inflight_flow(self, binding, **kwargs):
            self.finishes.append(dict(kwargs))
            if kwargs['terminal_kind'] != 'retry_required':
                raise ApiError('WORKER_INFLIGHT_FLOW_NOT_SETTLED', 'formal legacy result is retry_required', 409)
            return super().finish_inflight_flow(binding, **kwargs)
        def get_wechat_read_authorization(self, binding, conversation_id):
            self.proofs += 1
            if mode != 'proof_online' and self.proofs == 1:
                raise TimeoutError('one transient proof read failure')
            return {'conversation_id': conversation, 'allowed': False, 'read_completion': {
                'read_run_id': flow, 'result': 'retry_required',
                'error_code': 'C2_UNREAD_RESULT_INCONCLUSIVE',
                'completed_at': '2026-09-09T12:00:00Z', 'next_read_due_at': '2026-09-09T12:00:05Z'}}
    try:
        api = Boundary(); bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
        runner, _ = fixture.make_runner(api, bridge)
        binding = Binding('independent-worker', 'test-only-token', 'independent-instance', run_status='paused')
        runner.binding = binding
        begin_runtime_flow(flow, 'c2_read'); request_runtime_pause()
        runner._restart_recovery_flow_id = flow
        api.inflight_flow_id = flow
        api.inflight_flow_state = {'status': 'draining', 'flow_id': flow, 'flow_kind': 'c2_read', 'conversation_id': conversation}
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        save_c2_state(runner._inflight_finish_receipt_key(flow), {'terminal_kind': 'read_confirmed', 'conversation_id': conversation, 'error_code': None})
        if mode == 'proof_timeout_without_priority':
            runner._retry_pending_flow_finish = lambda binding: False
        for _ in range(8):
            runner._flow_finish_retry_at = 0  # Fake clock only; original tick/recovery/terminal proof checks intact.
            runner.tick_once()
        evidence = {'mode': mode, 'proof_requests': api.proofs, 'finish_requests': api.finishes,
            'runtime': load_runtime_control(), 'backend_flow': api.inflight_flow_state,
            'receipt': load_c2_state(runner._inflight_finish_receipt_key(flow)),
            'heartbeats': len(api.heartbeat_payloads), 'run_status': binding.run_status,
            'pulls': api.events.count('pull'), 'locates': len(bridge.locate_chats),
            'sends': len(bridge.sent_replies), 'wait_reason': runner.flow_finish_wait_reason}
        (tmp_path/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        assert evidence['heartbeats'] == 8 and not evidence['pulls'] and not evidence['sends'] and not evidence['locates'], evidence
        assert not evidence['runtime']['inflight_flow_id'] and not evidence['backend_flow'], evidence
        assert evidence['run_status'] == 'paused' and evidence['runtime']['pause_requested'], evidence
    finally:
        fixture.tearDown()


@pytest.mark.parametrize('defect', ['other_flow','other_customer','no_completion_time','no_retry_time','wrong_error','wrong_result','missing_proof','other_409','existing_proof'])
def test_pending_finish_cannot_relax_terminal_proof_or_other_409(defect):
    case=fixtures.TaskRunnerTest();case.setUp()
    flow,conversation='blocked-legacy-flow','blocked-legacy-conversation'
    class Boundary(FakeApi):
        def __init__(self):
            super().__init__(None);self.proofs=0;self.finishes=[]
        def finish_inflight_flow(self,binding,**kwargs):
            self.finishes.append(dict(kwargs))
            raise ApiError('RUNTIME_INFLIGHT_FLOW_MISMATCH' if defect=='other_409' else 'WORKER_INFLIGHT_FLOW_NOT_SETTLED','controlled rejection',409)
        def get_wechat_read_authorization(self,binding,conversation_id):
            self.proofs+=1
            completion={'read_run_id':flow,'result':'retry_required','error_code':'C2_UNREAD_RESULT_INCONCLUSIVE',
                'completed_at':'2026-09-09T12:00:00Z','next_read_due_at':'2026-09-09T12:00:05Z'}
            snapshot={'conversation_id':conversation,'allowed':False,'read_completion':completion}
            if defect=='other_flow':completion['read_run_id']='unrelated-flow'
            elif defect=='other_customer':snapshot['conversation_id']='unrelated-conversation'
            elif defect=='no_completion_time':completion.pop('completed_at')
            elif defect=='no_retry_time':completion.pop('next_read_due_at')
            elif defect=='wrong_error':completion['error_code']='UNRELATED_ERROR'
            elif defect=='wrong_result':completion['result']='technical_failed'
            elif defect=='missing_proof':snapshot.pop('read_completion')
            return snapshot
    try:
        api=Boundary();bridge=FakeBridge(RpaResult(ok=True,result_code='unused'));runner,_=case.make_runner(api,bridge)
        binding=Binding('worker-proof-guard','test-token','client-proof-guard',run_status='paused');runner.binding=binding
        begin_runtime_flow(flow,'c2_read');request_runtime_pause();runner._restart_recovery_flow_id=flow
        api.inflight_flow_id=flow;api.inflight_flow_state={'flow_id':flow,'flow_kind':'c2_read','status':'draining','conversation_id':conversation}
        receipt={'terminal_kind':'read_confirmed','conversation_id':conversation,'error_code':None}
        if defect=='existing_proof':receipt['read_completion']={'result':'new_facts','read_run_id':flow}
        save_c2_state(runner._inflight_finish_receipt_key(flow),receipt)
        for _ in range(5):
            runner._flow_finish_retry_at=0
            runner.tick_once()
        assert len(api.finishes)>=2 and all(item['terminal_kind']=='read_confirmed' for item in api.finishes)
        assert api.proofs==(0 if defect in {'other_409','existing_proof'} else len(api.finishes))
        assert load_runtime_control()['inflight_flow_id']==flow and api.inflight_flow_id==flow
        current=load_c2_state(runner._inflight_finish_receipt_key(flow))
        assert current['terminal_kind']=='read_confirmed'
        assert current.get('read_completion')==receipt.get('read_completion')
        assert binding.run_status=='paused' and load_runtime_control()['pause_requested']
        assert not api.events.count('pull') and not bridge.locate_chats and not bridge.sent_replies
    finally:case.tearDown()
