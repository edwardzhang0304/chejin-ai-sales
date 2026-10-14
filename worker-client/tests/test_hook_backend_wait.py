"""Real HTTP failures: preserve approved intake without authorizing new work."""
import json
from types import SimpleNamespace
import pytest
from chejin_worker_client.api import ApiError
from chejin_worker_client import storage
from test_hook_worker_services import runtime,projected_count
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer,values
from test_hook_connection import callback


@pytest.mark.parametrize('status',[500,502,503,504,408,429])
def test_unavailable_heartbeat_has_no_retry_or_account_scope(backend_peer,status):
    backend_peer.status=status;backend_peer.body=b'not JSON: secret-worker-token'
    client,credentials,identity,state=values(backend_peer)
    with pytest.raises(ApiError) as exc:client.heartbeat(credentials,identity,state)
    assert exc.value.code=='HOOK_BACKEND_TEMPORARILY_UNAVAILABLE' and exc.value.status_code==status
    assert len(backend_peer.requests)==1 and exc.value.data is None
    assert 'secret-worker-token' not in str(exc.value)


@pytest.mark.parametrize('status',[401,403,409])
def test_authorization_rejection_is_not_a_transient_wait(backend_peer,status):
    backend_peer.status=status;backend_peer.body=b'{"code":"REJECTED"}'
    client,credentials,identity,state=values(backend_peer)
    with pytest.raises(ApiError) as exc:client.heartbeat(credentials,identity,state)
    assert exc.value.code=='HOOK_ACCOUNT_REQUEST_REJECTED' and exc.value.status_code==status


def test_server_500_retains_receiver_records_and_recovers_same_account(runtime,backend_peer):
    runner,services,binding,_,errors,statuses,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed);runner.tick_once()
    original=services.connection.prepared
    backend_peer.status=500
    for _ in range(3):runner.tick_once()
    assert services.connection.prepared is original and binding.run_status=='paused'
    assert services.startup_check_state()['title']=='等待后台恢复连接' and not services.startup_check_state()['canStart']
    assert not errors and not posted and 'connecting' in statuses
    callback(services.connection);services.projection.project_next(original.registration.scope)
    assert projected_count()==1
    backend_peer.status=200;runner.tick_once()
    assert services.connection.prepared.registration is original.registration
    assert services.startup_check_state()['canStart'] and binding.run_status=='paused'
    assert not errors and not posted


def test_business_heartbeat_server_failure_waits_without_new_actions(runtime):
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed)
    reports=[]
    def tick(*args):runner.stop_event.set();raise ApiError('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE','safe',500)
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,_report_wait=reports.append)
    services.business_loop(runner,lambda _:None)
    assert reports==['HOOK_BACKEND_TEMPORARILY_UNAVAILABLE']
    assert binding.run_status=='paused' and not errors and not posted and not services.startup_check_state()['canStart']


def test_http_rejection_diagnostic_keeps_status_but_no_response_body(runtime,backend_peer):
    runner,services,binding,*_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed);runner.tick_once()
    backend_peer.status=403;backend_peer.body=b'{"code":"REJECTED","message":"private-unsafe-upstream"}'
    runner.tick_once()
    assert binding.run_status=='faulted' and services.connection.prepared is None
    with storage.db_connection() as db:
        row=db.execute("SELECT metadata FROM local_logs WHERE event='hook_connection_fault' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert json.loads(row[0])['backend_http_status']==403 and 'private-unsafe-upstream' not in row[0]


@pytest.mark.parametrize('status',[500,502,503,504,408,429])
@pytest.mark.parametrize('operation',['formal','observations'])
def test_all_hook_transports_wait_on_unavailable_without_retry(backend_peer,status,operation):
    from chejin_worker_client.hook_formal_client import HookFormalClient
    backend_peer.status=status;backend_peer.body=b'<html>secret-worker-token gateway unavailable</html>'
    client,credentials,_,_=values(backend_peer)
    with pytest.raises(ApiError) as exc:
        if operation=='formal':
            HookFormalClient(client.base_url).call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-work',{})
        else:
            claim=SimpleNamespace(envelope={'worker_id':credentials.worker_id,'client_instance_id':credentials.client_instance_id},payload_json='{}')
            client.store_observations(credentials,claim)
    assert exc.value.code=='HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'
    assert exc.value.status_code==status and exc.value.data is None
    assert 'secret-worker-token' not in str(exc.value)
    assert len(backend_peer.requests)==1


@pytest.mark.parametrize('status,body,code',[
    (200,b'<html>bad success</html>','HOOK_FORMAL_RESPONSE_INVALID'),
    (401,b'{"code":"AUTH_REJECTED"}','AUTH_REJECTED'),
    (403,b'{"code":"AUTH_REJECTED"}','AUTH_REJECTED'),
    (409,b'{"code":"HOOK_SEND_CONTEXT_STALE"}','HOOK_SEND_CONTEXT_STALE'),
    (302,b'{}','HOOK_BACKEND_REDIRECT_REJECTED'),
])
def test_formal_protocol_and_authorization_failures_remain_faults(backend_peer,status,body,code):
    from chejin_worker_client.hook_formal_client import HookFormalClient
    backend_peer.status=status;backend_peer.body=body
    client,credentials,_,_=values(backend_peer)
    with pytest.raises(ApiError) as exc:
        HookFormalClient(client.base_url).call(credentials,'POST',f'/workers/{credentials.worker_id}/wechat/native-work',{})
    assert exc.value.code==code and len(backend_peer.requests)==1


@pytest.mark.parametrize('run_status',['running','paused','faulted'])
def test_business_gateway_restart_recovers_without_changing_operator_intent(runtime,backend_peer,run_status):
    from chejin_worker_client.hook_formal_client import HookFormalClient
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed);runner.tick_once()
    original=services.connection.prepared
    callback(services.connection);services.projection.project_next(original.registration.scope)
    binding.run_status=run_status
    storage.begin_runtime_flow('retained-test-flow','task')
    backend_peer.status=502;backend_peer.body=b'<html>Bad Gateway</html>'
    client=HookFormalClient(f'http://127.0.0.1:{backend_peer.server_port}/api')
    reports=[];results=[]
    def report(code):
        reports.append(code)
        backend_peer.status=200;backend_peer.body=b'{"code":"OK","data":{"reconciled":true}}'
    def tick(*args):
        value=client.call(services._credentials,'POST',f'/workers/{binding.worker_id}/wechat/native-work',{})
        results.append(value);runner.stop_event.set()
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,_report_wait=report)
    services.business_loop(runner,lambda _:None)
    assert reports==['HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'] and results==[{'reconciled':True}]
    assert binding.run_status==run_status and not errors and not posted
    assert services.connection.prepared is original and projected_count()==1
    assert storage.load_runtime_control()['inflight_flow_id']=='retained-test-flow'
    with storage.db_connection() as db:
        assert db.execute("SELECT count(*) FROM local_logs WHERE event='hook_connection_fault'").fetchone()[0]==0


@pytest.mark.parametrize('status',[502,503])
def test_unavailable_send_begin_preserves_intent_and_never_replays_wechat(lab,tmp_path,backend_peer,status):
    from test_hook_native_sender import setup
    from chejin_worker_client.hook_formal_client import HookFormalClient
    server,gateway,process,scope,sender,permit,_,refresh,_=setup(lab,tmp_path)
    sender.client=HookFormalClient(f'http://127.0.0.1:{backend_peer.server_port}/api')
    backend_peer.status=status;backend_peer.body=b'<html>Bad Gateway</html>'
    with pytest.raises(ApiError) as exc:sender.execute(permit,process,scope,refresh)
    assert exc.value.code=='HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'
    files={str(p):p.read_bytes() for p in (tmp_path/'send-private').rglob('*.json')}
    assert any(p.endswith('intent.json') for p in files)
    assert not any(p.endswith('trigger.json') for p in files)
    assert not any(r[0]=='/SendTextMsg' for r in server.requests)
    backend_peer.status=200;backend_peer.body=b'{"code":"OK","data":{"may_call_hook":true,"duplicated":false}}'
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):sender.execute(permit,process,scope,refresh)
    assert len(backend_peer.requests)==1
    assert {str(p):p.read_bytes() for p in (tmp_path/'send-private').rglob('*.json')}==files
    assert not any(r[0]=='/SendTextMsg' for r in server.requests)


def test_committed_claim_lost_response_then_missing_permit_is_controlled_fault(runtime,backend_peer,tmp_path,monkeypatch,lab):
    from chejin_worker_client.hook_formal_client import HookFormalClient
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    import uuid
    runner,services,binding,_,errors,_,posted,_=runtime
    runner.binding=binding;services.attach(binding,runner._hook_persistence_failed);runner.tick_once()
    prepared=services.connection.prepared;scope=prepared.registration.scope
    task_id=str(uuid.uuid4());action_id=str(uuid.uuid4())
    storage.begin_runtime_flow(task_id,'chat_reply');binding.run_status='running'
    client=HookFormalClient(f'http://127.0.0.1:{backend_peer.server_port}/api')
    dispatcher=HookBusinessDispatcher(client,tmp_path/'lost-response',lambda *a:None,lambda *a:None,automatic_transcription_disabled=lambda:True)
    dispatcher.runtime=SimpleNamespace(credentials=services._credentials,scope=scope)
    original_file=dispatcher.private_root/'flows'/f'{task_id}.json'
    original_file.parent.mkdir(parents=True);original_file.write_text(json.dumps({'flow_id':task_id}))
    before={str(p):p.read_bytes() for p in dispatcher.private_root.rglob('*') if p.is_file()}
    committed=[]
    handle=backend_peer.RequestHandlerClass.do_POST
    def accept_claim_then_lose_response(handler):
        if handler.path.endswith('/claim'):committed.append(action_id)
        handle(handler)
    monkeypatch.setattr(backend_peer.RequestHandlerClass,'do_POST',accept_claim_then_lose_response)
    backend_peer.status=502;backend_peer.body=b'<html>Bad Gateway</html>'
    reports=[];ticks=[]
    def report(code):
        reports.append(code)
        assert binding.run_status=='running' and not errors
        backend_peer.status=200;backend_peer.body=b'{"code":"OK","data":{}}'
    def tick(*args):
        ticks.append(1)
        if len(ticks)==1:
            account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
            client.native_send_claim(services._credentials,account,task_id,action_id,1,'a'*64,flow_id=task_id)
        else:
            if len(ticks)==3:runner.stop_event.set()
            dispatcher._recover_send({'peer_wxid':'wxid_customer_A'}, {'reply_action_id':action_id,'task_id':task_id})
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,_report_wait=report)
    services.business_loop(runner,lambda _:None)
    assert len(ticks)==3 and reports==['HOOK_BACKEND_TEMPORARILY_UNAVAILABLE']
    assert committed==[action_id] and binding.run_status=='faulted' and errors
    assert not any(r[0]=='/SendTextMsg' for r in lab[0].requests)
    assert storage.load_runtime_control()['inflight_flow_id']==task_id
    assert {str(p):p.read_bytes() for p in dispatcher.private_root.rglob('*') if p.is_file()}==before
    assert not list(dispatcher.private_root.rglob('not-triggered.json'))
    paths=[path for path,*_ in backend_peer.requests]
    assert sum(path.endswith('/claim') for path in paths)==1
    assert not any(path.endswith(('/begin','/not-triggered','/settle')) for path in paths)
    with storage.db_connection() as db:
        codes=[r[0] for r in db.execute("SELECT error_code FROM local_logs WHERE event='hook_connection_fault'")]
    assert codes==['HOOK_RETAINED_SEND_PERMIT_MISSING']
