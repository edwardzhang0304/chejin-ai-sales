"""Connection lifecycle over real HTTP. Windows/DLL observations are fixtures."""
from dataclasses import replace
import json
import uuid
import threading
from urllib.request import Request, urlopen

import pytest

from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from chejin_worker_client import storage, ui_lock
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_backend import HookHeartbeatState, HookIdentityClient, HookWorkerCredentials
from chejin_worker_client.hook_connection import HookConnectionManager
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.hook_inbox import NativeInbox


def setup(lab, backend_peer):
    local,gateway,process,_,old_receiver,old_thread=lab
    old_receiver.shutdown();old_thread.join(timeout=2);old_receiver.server_close()
    credentials=HookWorkerCredentials(str(uuid.uuid4()),'isolated-worker-token','instance-A')
    state=HookHeartbeatState('idle','ready','logged_in',None,None,'{}')
    epoch,sales=str(uuid.uuid4()),str(uuid.uuid4())

    def response(*,blocked=False,rebind=False):
        identity={'self_wxid':local.wxid,'process_generation':local.current_process.generation,
            'deployment_revision':gateway.deployment.revision,'dll_sha256':gateway.deployment.dll_sha256,
            'wechat_file_version':'3.9.10.16'}
        backend_peer.body=json.dumps({'code':'OK','data':{
            'id':credentials.worker_id,'client_instance_id':credentials.client_instance_id,
            'run_status':'faulted' if blocked else 'paused',
            'hook_context':{**identity,'observed_self_wxid':local.wxid,
                'account_epoch':epoch,'sales_id':sales,'blocked':blocked,'rebind_required':rebind,
                'reason_code':'HOOK_ACCOUNT_CHANGED' if blocked else None}}}).encode()

    response()
    backend=HookIdentityClient(f'http://127.0.0.1:{backend_peer.server_port}/api',.3,65536)
    manager=HookConnectionManager(gateway,backend,NativeInbox(max_body_bytes=4096,max_inbox_bytes=65536),
        receiver_timeout_seconds=1,on_persistence_failure=lambda:None)
    return manager,credentials,state,response


def callback(manager):
    prepared=manager.prepared
    scope=prepared.registration.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
        'sendorrecv':'2','msgnumber':'1','msglist':[{'index':'1','msgtype':'1',
            'msgsvrid':'6594133108973694888','msg':'测试消息🙂','fromid':'wxid_customer_A','toid':scope.self_wxid}]}).encode()
    with urlopen(Request(prepared.registration.callback_url,data=raw,
            headers={'Content-Type':'application/json'}),timeout=2) as response:
        assert response.status==200


def test_prepare_authorizes_scope_before_listening_then_reuses_connection(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        first=manager.prepare(credentials,state)
        assert not first.business_ready and first.account.run_status=='paused'
        assert first.registration.scope.account_epoch==first.account.account_epoch
        callback(manager)
        with storage.db_connection() as conn:
            raw=conn.execute('SELECT * FROM hook_raw_inbox').fetchone()
            assert raw['account_epoch']==first.account.account_epoch
            assert raw['process_generation']==first.process.generation
        second=manager.prepare(credentials,state)
        assert second.registration is first.registration
        assert second.clock is first.clock
        assert [x[0] for x in lab[0].requests].count('/ConfigureMsgReciveFullURL')==1
        assert len(backend_peer.requests)==2
        assert all(x[2]['hook_context']['self_wxid']=='wxid_sales_A' for x in backend_peer.requests)
        assert not ui_lock.lock_summary()['locked']
    finally:manager.close()
    assert manager.prepared is None


def test_same_account_restart_registers_again_with_new_generation_not_new_epoch(lab,backend_peer):
    manager,credentials,state,response=setup(lab,backend_peer)
    try:
        first=manager.prepare(credentials,state)
        lab[0].current_process=replace(lab[2],started_at=lab[2].started_at+1)
        response()
        second=manager.prepare(credentials,state)
        assert second.process.generation!=first.process.generation
        assert second.clock is not first.clock and second.clock.anchor is None
        assert second.account.account_epoch==first.account.account_epoch
        assert second.registration.history_required
        assert [x[0] for x in lab[0].requests].count('/ConfigureMsgReciveFullURL')==2
        assert backend_peer.requests[-1][2]['hook_context']['account_epoch']==first.account.account_epoch
        callback(manager)
    finally:manager.close()


def test_account_change_closes_receiver_until_backend_reset(lab,backend_peer):
    manager,credentials,state,response=setup(lab,backend_peer)
    try:
        original=manager.prepare(credentials,state)
        lab[0].wxid='wxid_sales_B';response(blocked=True,rebind=True)
        with pytest.raises(HookLocalError,match='ACCOUNT_NOT_CONFIRMED'):manager.prepare(credentials,state)
        assert manager.prepared is None and not original.business_ready
        assert [x[0] for x in lab[0].requests].count('/ConfigureMsgReciveFullURL')==1
        lab[0].wxid='wxid_sales_A';response(blocked=True,rebind=True)
        with pytest.raises(HookLocalError):manager.prepare(credentials,state)
        assert manager.prepared is None
    finally:manager.close()


def test_backend_network_outage_keeps_same_account_passive_receiving(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        approved=manager.prepare(credentials,state)
        backend_peer.server_close()
        with pytest.raises(ApiError) as error:manager.prepare(credentials,state)
        assert error.value.code=='HOOK_BACKEND_NETWORK_FAILED'
        assert manager.prepared is approved
        callback(manager)
        with storage.db_connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1
    finally:manager.close()


def test_logout_waits_and_same_account_login_registers_again(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        first=manager.prepare(credentials,state)
        lab[0].login_status='0'
        assert manager.prepare(credentials,state) is None and manager.prepared is None
        assert len(backend_peer.requests)==2
        assert backend_peer.requests[-1][2]['wechat_status']=='logged_out'
        lab[0].login_status='3'
        second=manager.prepare(credentials,state)
        assert second.account.account_epoch==first.account.account_epoch
        assert [x[0] for x in lab[0].requests].count('/ConfigureMsgReciveFullURL')==2
    finally:manager.close()


def test_backend_rejection_or_bad_registration_never_leaves_active_receiver(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        backend_peer.status=401
        with pytest.raises(ApiError):manager.prepare(credentials,state)
        assert manager.prepared is None and not any(x[0]=='/ConfigureMsgReciveFullURL' for x in lab[0].requests)
        backend_peer.status=200;lab[0].configure_changes={'code':-1}
        with pytest.raises(HookLocalError):manager.prepare(credentials,state)
        assert manager.prepared is None and manager._receiver is None
    finally:manager.close()


def test_close_waits_for_inflight_callback_commit_without_deleting_evidence(lab,backend_peer,monkeypatch):
    manager,credentials,state,_=setup(lab,backend_peer)
    started,allow_commit,closed=threading.Event(),threading.Event(),threading.Event()
    real_persist=manager.inbox.persist

    def delayed(raw,scope):
        started.set()
        assert allow_commit.wait(timeout=3)
        return real_persist(raw,scope)

    monkeypatch.setattr(manager.inbox,'persist',delayed)
    errors=[]
    try:
        manager.prepare(credentials,state)
        def send():
            try:callback(manager)
            except Exception as exc:errors.append(exc)
        request=threading.Thread(target=send);request.start()
        assert started.wait(timeout=2)
        def stop():
            try:manager.close();closed.set()
            except Exception as exc:errors.append(exc)
        shutdown=threading.Thread(target=stop);shutdown.start()
        assert not closed.wait(timeout=.05)
        allow_commit.set()
        request.join(timeout=3);shutdown.join(timeout=3)
        assert not request.is_alive() and not shutdown.is_alive() and closed.is_set() and not errors
        with storage.db_connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1
        assert manager.prepared is None
    finally:
        allow_commit.set();manager.close()


@pytest.mark.parametrize('change', ['account', 'logout'])
def test_reused_receiver_rechecks_login_after_backend_round_trip(lab,backend_peer,monkeypatch,change):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        approved=manager.prepare(credentials,state)
        real_heartbeat=type(manager.backend).heartbeat
        def change_during_heartbeat(client,*args,**kwargs):
            receipt=real_heartbeat(client,*args,**kwargs)
            if change=='account':lab[0].wxid='wxid_sales_B'
            else:lab[0].login_status='0'
            return receipt
        monkeypatch.setattr(type(manager.backend),'heartbeat',change_during_heartbeat)
        if change=='account':
            with pytest.raises(HookLocalError,match='ACCOUNT_MISMATCH'):
                manager.prepare(credentials,state)
        else:
            assert manager.prepare(credentials,state) is None
        assert manager.prepared is None and manager._receiver is None
        assert [x[0] for x in lab[0].requests].count('/ConfigureMsgReciveFullURL')==1
        assert not approved.business_ready and not ui_lock.lock_summary()['locked']
    finally:manager.close()


def test_same_actor_receiver_rebuild_keeps_business_clock_identity(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        original=manager.prepare(credentials,state)
        manager.close()
        current=manager.prepare(credentials,state)
        assert current.registration is not original.registration
        assert current.registration.scope==original.registration.scope
        assert current.clock is original.clock
    finally:manager.close()


def test_changed_worker_credentials_never_reuse_previous_clock(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        original=manager.prepare(credentials,state)
        changed=replace(credentials,worker_token='synthetic-replaced-token')
        current=manager.prepare(changed,state)
        assert current.clock is not original.clock and current.clock.anchor is None
    finally:manager.close()
