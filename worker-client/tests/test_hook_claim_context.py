"""Admitted history is claim context only, never physical-send authorization."""
import pytest
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


def runtime():
    rt=HookFormalRuntime.__new__(HookFormalRuntime)
    rt.register_batch_progress=lambda *a:None
    rt.progress=lambda *a:None
    rt.sync=lambda *a,**kw:pytest.fail('Admission repeated the history read')
    rt.active_flow_id=None
    return rt


def test_incomplete_history_cannot_claim_or_send():
    rt=runtime()
    rt.start_flow=lambda *a:pytest.fail('Incomplete history claimed a flow')
    data={'batch_id':'batch','reply_action':{'id':'action','status':'queued'},'task':{'id':'task'}}
    assert rt.reply_once({},data,history={'history_ready':False}) is None


@pytest.mark.parametrize('latest', [None, {'id':'different','status':'queued'}, {'id':'action','status':'cancelled'}])
def test_changed_or_cancelled_action_does_not_claim(latest):
    rt=runtime()
    rt.batch_view=lambda _:dict(reply_action=latest,task={'id':'task'})
    rt.start_flow=lambda *a:pytest.fail('Changed action claimed a flow')
    data={'batch_id':'batch','reply_action':{'id':'action','status':'queued'},'task':{'id':'task'}}
    assert rt.reply_once({},data,history={'history_ready':True,'history_state_digest':'a'*64}) is None
