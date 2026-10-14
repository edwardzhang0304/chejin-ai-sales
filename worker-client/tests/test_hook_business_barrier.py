from types import SimpleNamespace
import json
import pytest
from test_hook_gateway import lab
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


def runtime(lab):
    rt=HookFormalRuntime.__new__(HookFormalRuntime);rt.scope=lab[3];rt.fault_check=lambda:False
    return rt


def persist(lab,peer='wxid_customer_A',group=False,audio=False):
    scope=lab[3]
    message={'msgsvrid':'10001','msgtype':'34' if audio else '1','msg':'测试',
        'fromid':peer,'toid':'123@chatroom' if group else scope.self_wxid,'fromtype':'2' if group else '1'}
    if audio:message.update(msg='',voice_data='02',voice_len='1',fromid='',toid='')
    body={'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msgnumber':'1','msglist':[message]}
    return NativeInbox(max_body_bytes=4096,max_inbox_bytes=65536).persist(json.dumps(body).encode(),scope)


def project(lab):
    p=NativeProjectionStore(max_projection_bytes=65536,max_entries=10)
    while p.project_next(lab[3]) is not None:pass


def test_unrelated_groups_do_not_invalidate_customer_barrier(lab):
    rt=runtime(lab);b={'peer_wxid':'wxid_customer_A','voice_jobs':[]}
    persist(lab);project(lab);first=rt.barrier(b)
    persist(lab,group=True);persist(lab,peer='wxid_another_customer');project(lab)
    assert rt.barrier(b)==first
    persist(lab);assert rt.barrier(b)[1]==1
    project(lab);assert rt.barrier(b)[0]>first[0]


def test_later_audio_without_peer_still_invalidates_known_parent(lab):
    rt=runtime(lab);b={'peer_wxid':'wxid_customer_A','voice_jobs':[{'native_msg_id':'10001'}]}
    first=rt.barrier(b);persist(lab,audio=True)
    assert rt.barrier(b)[0]>first[0] and rt.barrier(b)[1]==1


def test_changed_projection_is_not_trusted_for_scoped_barrier(lab):
    persist(lab);project(lab)
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_observation_projection SET projection_json='{}'");conn.commit()
    with pytest.raises(ValueError,match='PROJECTION_EVIDENCE_CHANGED'):
        runtime(lab).barrier({'peer_wxid':'wxid_customer_A','voice_jobs':[]})
