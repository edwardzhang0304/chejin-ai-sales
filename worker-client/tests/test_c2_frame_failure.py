"""Technical frame failures: real Worker/SQLite, external API/UI doubles."""
import pytest
from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge, identity_checkpoint
from chejin_worker_client.models import Binding, WechatReadTarget, RpaResult
from chejin_worker_client.storage import load_runtime_control, read_logs
from chejin_worker_client.task_runner import C2_FRAME_TECHNICAL_ERROR_CODES


@pytest.mark.parametrize('code', sorted(C2_FRAME_TECHNICAL_ERROR_CODES))
def test_bad_frame_stops_before_another_search_and_finishes_flow(harness, code):
    api=FakeApi(None)
    bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
    bridge.locate_payloads=[{'ok':False,'error_code':code,'reason':'avatar_association_unresolved',
        'avatar_evidence':{'row_bounds':[10,20,30,40]}}]
    runner,_=harness.make_runner(api,bridge)
    import os
    if os.environ.get('CHEJIN_CJ35_DISABLE_WORKER_FIX')=='1':
        import ast,subprocess,types
        from pathlib import Path
        import chejin_worker_client.task_runner as module
        path='worker-client/chejin_worker_client/task_runner.py'
        raw=subprocess.check_output(['git','show','8a155f0ce2d65d163f3af4efb09e3c3700aa8b25:'+path],cwd=Path(__file__).resolve().parents[2],text=True)
        cls=next(n for n in ast.parse(raw).body if isinstance(n,ast.ClassDef) and n.name=='TaskRunner')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_read_one_wechat_target')
        namespace=dict(vars(module));exec(compile(ast.Module(body=[method],type_ignores=[]),path,'exec'),namespace)
        runner._read_one_wechat_target=types.MethodType(namespace[method.name],runner)
    binding=Binding('worker-test','test-token','instance-test',run_status='running')
    runner.binding=binding
    target=WechatReadTarget(conversation_id='conv-frame',display_name='CJTEST01',remark_code='CJTEST01',
        rpa_session_key='test',authorization_revision='revision-conv-frame',unread_generation=1,
        raw={'identity_checkpoint':identity_checkpoint()})
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=False,wait_for_brain=False)
    assert result['worker_faulted'] and result['flow_terminal_kind']=='technical_failed', result
    assert len(bridge.locate_chats)==1,bridge.c2_operation_order
    assert not bridge.message_reads and not bridge.sent_replies
    assert runner.binding.run_status=='faulted'
    assert not load_runtime_control()['inflight_flow_id']
    assert any(':technical_failed:' in e for e in api.inflight_flow_events),api.inflight_flow_events
    assert not api.message_payloads
    logs=read_logs(limit=100)
    assert any(e['event']=='c2_frame_evidence_technical_failed' for e in logs)
    assert not any(e['event']=='inflight_flow_finish_failed' for e in logs)


def test_partial_top_without_history_is_not_an_empty_baseline(harness):
    api=FakeApi(None);bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
    runner,_=harness.make_runner(api,bridge)
    target=WechatReadTarget(conversation_id='conv-partial',display_name='CJTEST01',remark_code='CJTEST01',
        rpa_session_key='test',authorization_revision='revision-partial',raw={'identity_checkpoint':identity_checkpoint()})
    payload=bridge._contractual_message_payload({'messages':[{'id':'new','type':'text','sender_role':'customer','content':'下一条完整消息'}],
        'frame_id':'synthetic-partial','top_message_fragment':[{'bounds':[10,20,30,40]}]})
    _,errors=runner._align_initial_identity_frame(target=target,sidecar_payload=payload,read_run_id='partial')
    assert errors and errors[0]['reason']=='partial_top_message_without_history',errors
    assert not api.message_payloads and not bridge.sent_replies
