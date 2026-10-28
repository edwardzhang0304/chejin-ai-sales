"""Real local HTTP/SQLite action lock; Windows process/UI and backend permit simulated."""
from datetime import datetime, timezone, timedelta
import hashlib
import uuid
import pytest
from test_hook_gateway import lab
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_sender import HookNativeSender, NativeSendPermit
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client import ui_lock


def setup(lab,tmp_path):
    server,gateway,process,scope,*_=lab
    worker=str(uuid.uuid4());instance='instance-native-send'
    credentials=HookWorkerCredentials(worker,'test-worker-token',instance)
    text='预算15万元，明天下午联系🙂\n第二行'
    value={'schema_version':'hook-native-send-v1','native_send_ready':True,'worker_id':worker,'client_instance_id':instance,
        'task_id':str(uuid.uuid4()),'reply_action_id':str(uuid.uuid4()),'self_wxid':scope.self_wxid,'peer_wxid':'wxid_customer_A',
        'account_epoch':str(uuid.uuid4()),'process_generation':process.generation,'history_state_digest':'a'*64,
        'lease_fencing_token':1,'send_token':'test-send-token','reply_text':text,'reply_text_hash':hashlib.sha256(text.encode()).hexdigest(),
        'expire_at':(datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat()}
    from dataclasses import replace
    scope=replace(scope,account_epoch=value['account_epoch']);permit=NativeSendPermit.capture(value)
    class Client:
        calls=0
        begin_effect=None
        def native_send_begin(self,*_):
            self.calls+=1
            if self.begin_effect:self.begin_effect()
            return {'may_call_hook':True,'duplicated':False}
    class Refresh:
        seq=0
        def barrier(self):return (self.seq,0)
        def __call__(self,_):return self.barrier()
    client=Client();refresh=Refresh();waits=[]
    def wait(seconds):
        assert not ui_lock.lock_summary()['locked'];waits.append(seconds)
    sender=HookNativeSender(gateway,client,credentials,tmp_path/'send-private',wait=wait,delay=lambda:1.25)
    def response(path):server.raw_override=b'{"SendTextMsg":"1"}' if path=='/SendTextMsg' else None
    server.on_request=response
    return server,gateway,process,scope,sender,permit,client,refresh,waits


def test_native_send_first_action_waits_outside_lock_and_calls_once(lab,tmp_path):
    server,gateway,process,scope,sender,permit,client,refresh,waits=setup(lab,tmp_path)
    result=sender.execute(permit,process,scope,refresh)
    assert result['call_completed'] and result['hook_submission_flag'] and not result['automatic_resend_allowed']
    requests=[r for r in server.requests if r[0]=='/SendTextMsg']
    assert len(requests)==1 and requests[0][1]=={'toid':permit.peer_wxid,'msg':permit.reply_text}
    assert waits==[1.25] and client.calls==1
    assert all(item['locked'] for item in server.lock_observations)
    assert not ui_lock.lock_summary()['locked']
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):sender.execute(permit,process,scope,refresh)
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1 and waits==[1.25]


def test_new_callback_between_begin_and_hook_stops_before_trigger(lab,tmp_path):
    server,gateway,process,scope,sender,permit,client,refresh,waits=setup(lab,tmp_path)
    client.begin_effect=lambda:setattr(refresh,'seq',1)
    with pytest.raises(HookLocalError,match='NEW_OBSERVATION'):sender.execute(permit,process,scope,refresh)
    assert not any(r[0]=='/SendTextMsg' for r in server.requests)
    assert list((tmp_path/'send-private').rglob('intent.json')) and not list((tmp_path/'send-private').rglob('trigger.json'))
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):sender.execute(permit,process,scope,refresh)


def test_hook_transport_unknown_quarantines_account_and_never_repeats(lab,tmp_path,monkeypatch):
    server,gateway,process,scope,sender,permit,client,refresh,waits=setup(lab,tmp_path)
    original=gateway._response
    calls=[]
    def fail(route,payload,**kwargs):
        if route=='SendTextMsg':
            calls.append(1);raise HookLocalError('HOOK_LOCAL_TRANSPORT_FAILED',outcome_unknown=True)
        return original(route,payload,**kwargs)
    monkeypatch.setattr(gateway,'_response',fail)
    result=sender.execute(permit,process,scope,refresh)
    assert not result['call_completed'] and result['quarantined'] and calls==[1]
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):sender.execute(permit,process,scope,refresh)
    from dataclasses import replace
    another=replace(permit,reply_action_id=str(uuid.uuid4()),task_id=str(uuid.uuid4()))
    with pytest.raises(ValueError,match='QUARANTINED'):sender.execute(another,process,scope,refresh)
    assert calls==[1]


def test_send_captures_both_clocks_at_original_call(lab,tmp_path):
    server,gateway,process,scope,sender,permit,client,refresh,_=setup(lab,tmp_path)
    class BackendClock:
        def now(self):return datetime.now(timezone.utc)-timedelta(seconds=2)
    sender.clock=BackendClock()
    # HookNativeSender stores now at construction; use the same injected boundary.
    sender.now=sender.clock.now
    outcome=sender.execute(permit,process,scope,refresh)
    window=outcome['local_call_window']
    assert window['source']=='physical_call_v1'
    gap=(datetime.fromisoformat(window['completed_at'])-datetime.fromisoformat(outcome['completed_at'])).total_seconds()
    assert 1.8<gap<2.1
    assert window['elapsed_seconds']>=0
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
