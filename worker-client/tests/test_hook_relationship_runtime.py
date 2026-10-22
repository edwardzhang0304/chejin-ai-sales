from types import SimpleNamespace
import pytest
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_gateway import HookLocalError


@pytest.mark.parametrize('code,transient',[
    ('HOOK_LOCAL_TRANSPORT_FAILED',True),('HOOK_LOCAL_RESPONSE_INVALID',True),
    ('HOOK_HISTORY_ACCOUNT_MISMATCH',False),('HOOK_PROCESS_GENERATION_CHANGED',False),
    ('HOOK_PROCESS_ACTIVE_CALL_QUARANTINED',False)])
def test_relationship_query_failure_never_becomes_deletion_or_swallows_identity(code,transient,monkeypatch):
    from chejin_worker_client import storage
    logs=[];monkeypatch.setattr(storage,'append_log',lambda *a,**kw:logs.append((a,kw)))
    def fail(*a):raise HookLocalError(code)
    runtime=object.__new__(HookFormalRuntime)
    runtime.gateway=SimpleNamespace(friend_list=fail);runtime.process=object();runtime.scope=object()
    # There is deliberately no client: failures cannot issue state mutations.
    if transient:
        runtime.review_relationships([{'binding_id':'unused'}]);assert len(logs)==1
        assert logs[0][1]['metadata']=={'reason_code':code}
    else:
        with pytest.raises(HookLocalError,match=code):runtime.review_relationships([{'binding_id':'unused'}])
        assert not logs
