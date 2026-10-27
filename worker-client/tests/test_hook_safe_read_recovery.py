"""Real loopback transport, original ledgers/connection/UI; no real WeChat."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import json
import socket
import threading

import pytest

from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_worker_services import runtime
from test_hook_startup_order import configure
from test_hook_connection import callback
from test_hook_history import history_lab
from chejin_worker_client import storage, ui_lock, hook_transport, hook_read_retry as retry
from chejin_worker_client.hook_active_calls import reserve, finish, unresolved
from chejin_worker_client.hook_gateway import HookGateway, HookLocalError
from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot, SnapshotMismatch


@pytest.fixture
def refused(lab, monkeypatch):
    # A real local port with no listening socket: connect cannot send HTTP.
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]  # Keep bound but not listening, avoid port reuse.
    original=hook_transport.connection
    calls=[];clock=[100.]
    def connection(_port, timeout):
        calls.append(_port)
        return original(port,timeout)
    monkeypatch.setattr(hook_transport,'connection',connection)
    monkeypatch.setattr(retry,'time',SimpleNamespace(time=lambda:clock[0]))
    yield clock,calls,original
    sock.close()


def attempt(gateway,process,code):
    with pytest.raises(retry.ReadRetryWait,match=code):
        gateway.login_status(process)
    assert not ui_lock.lock_summary()['locked']
    assert not unresolved(process.generation)


def test_real_refusal_two_retries_durable_budget_and_explicit_reset(lab,refused):
    server,gateway,process,*_=lab;clock,calls,_=refused
    attempt(gateway,process,'RETRY_WAIT')
    assert retry.pending()['failures']==1 and retry.pending()['next_at']==101
    for _ in range(4):attempt(gateway,process,'RETRY_WAIT')
    assert len(calls)==1 and server.requests==[]
    clock[0]=101
    # New object with same durable intent cannot reset the episode.
    new=HookGateway(gateway.deployment,timeout_seconds=1,max_response_bytes=4096,
        lock_timeout_seconds=.1,inspector=gateway.inspector)
    attempt(new,process,'RETRY_WAIT')
    assert retry.pending()['failures']==2 and retry.pending()['next_at']==106
    clock[0]=105.99;attempt(new,process,'RETRY_WAIT');assert len(calls)==2
    clock[0]=106;attempt(new,process,'RETRY_EXHAUSTED')
    for _ in range(4):attempt(gateway,process,'RETRY_EXHAUSTED')
    assert len(calls)==3 and retry.pending()['failures']==3
    with storage.db_connection() as conn:
        events=[json.loads(r['evidence']) for r in conn.execute('SELECT * FROM hook_transport_events')]
    assert len(events)==3 and all(e['delivery']=='not_sent' and not e['request_started'] for e in events)
    assert retry.request_new_round()
    attempt(new,process,'RETRY_WAIT')
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_read_retry').fetchone()[0]==2


@pytest.mark.parametrize('cause',['restart','pause','update','new_process'])
def test_intent_change_cancels_durably_without_http(lab,refused,cause):
    server,gateway,process,*_=lab;clock,calls,_=refused
    attempt(gateway,process,'RETRY_WAIT');clock[0]=999
    if cause in {'restart','pause'}:gateway.retry_intent=lambda:cause
    if cause=='update':gateway.retry_allowed=lambda:False
    if cause=='new_process':
        process=replace(process,started_at=process.started_at+1)
        server.current_process=process
    attempt(gateway,process,'RETRY_CANCELLED')
    gateway.retry_intent=lambda:'standalone';gateway.retry_allowed=lambda:True
    attempt(gateway,process,'RETRY_CANCELLED')
    assert len(calls)==1 and retry.pending()['state']=='cancelled'


def test_failed_retry_journal_cannot_release_call_barrier(lab,refused,monkeypatch):
    _,gateway,process,*_=lab
    monkeypatch.setattr(retry,'failed',Mock(side_effect=OSError('fixture disk full')))
    with pytest.raises(OSError):gateway.login_status(process)
    assert unresolved(process.generation)
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)


def test_restart_before_call_finish_keeps_budget_and_barrier(lab,refused,monkeypatch):
    _,gateway,process,*_=lab
    monkeypatch.setattr('chejin_worker_client.hook_active_calls.finish',Mock(side_effect=OSError('fixture crash')))
    with pytest.raises(OSError):gateway.login_status(process)
    assert retry.pending()['failures']==1 and unresolved(process.generation)
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)


@pytest.mark.parametrize('reply',[
    b'',
    b'HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n{}',
    b'HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: /elsewhere\r\n\r\n',
    b'HTTP/1.1 200 OK\r\nContent-Length: 4097\r\n\r\n'+b'x'*4097,
])
def test_received_request_dropped_truncated_or_redirected_is_never_retryable(lab,monkeypatch,reply):
    _,gateway,process,*_=lab
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    observed=[]
    def serve():
        conn,_=listener.accept()
        with conn:
            observed.append(conn.recv(65536))
            if reply:conn.sendall(reply)
    thread=threading.Thread(target=serve);thread.start()
    original=hook_transport.connection
    monkeypatch.setattr(hook_transport,'connection',lambda _,timeout:original(listener.getsockname()[1],timeout))
    try:
        with pytest.raises(HookLocalError):gateway.login_status(process)
        thread.join(2)
        assert not thread.is_alive() and observed and retry.pending() is None
        assert unresolved(process.generation)
        with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)
    finally:
        listener.close()


def test_non_read_control_never_gets_retry_budget(lab,refused):
    _,gateway,process,scope,receiver,thread=lab
    with pytest.raises(HookLocalError,match='TRANSPORT_FAILED'):
        gateway.register_callback(process,scope,receiver,thread)
    assert retry.pending() is None  # No retry of whole callback/side-effect operation.


def test_old_generation_requires_death_proof_and_preserves_unknown(lab):
    server,gateway,old,*_=lab
    reserve(old.generation,'original-send','send_text');finish(old.generation,'original-send',False)
    with storage.db_connection() as conn:
        original=dict(conn.execute('SELECT * FROM hook_active_calls').fetchone())
    new=replace(old,started_at=old.started_at+1);server.current_process=new
    gateway.generation_exited=lambda _:False
    with pytest.raises(ValueError,match='PREVIOUS_PROCESS_EXIT_UNCONFIRMED') as failure:
        gateway.login_status(new)
    assert failure.value.code=='HOOK_PREVIOUS_PROCESS_EXIT_UNCONFIRMED'
    from chejin_worker_client import startup_check_state
    assert startup_check_state.failed(failure.value.code)['title']=='原微信调用尚未安全结束'
    assert not server.requests
    seen=[]
    gateway.generation_exited=lambda generation:seen.append(generation) or True
    assert gateway.login_status(new).self_wxid=='wxid_sales_A'
    assert seen==[old.generation] and unresolved(old.generation)
    with storage.db_connection() as conn:
        assert dict(conn.execute('SELECT * FROM hook_active_calls WHERE action_id=?',('original-send',)).fetchone())==original
        proof=dict(conn.execute('SELECT * FROM hook_process_recovery').fetchone())
        assert proof['old_generation']==old.generation and proof['new_generation']==new.generation
    assert all(r[0]=='/IsLoginStatus' for r in server.requests)
    # Worker-only restart with original live generation cannot use new proof.
    server.current_process=old
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(old)


def test_retry_evidence_survives_update_and_deletion_is_detected(lab,refused):
    _,gateway,process,*_=lab;attempt(gateway,process,'RETRY_WAIT')
    saved=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='synthetic')
    assert saved['hook_recovery_v1']['hook_read_retry']
    assert_protected_update_snapshot(saved,data_dir=storage.APP_DIR,digest_key='synthetic')
    with storage.db_connection() as conn:
        conn.execute('DELETE FROM hook_read_retry');conn.commit()
    with pytest.raises(SnapshotMismatch):
        assert_protected_update_snapshot(saved,data_dir=storage.APP_DIR,digest_key='synthetic')


def test_connection_retry_preserves_receiver_and_gui_recheck_never_starts(runtime,lab,monkeypatch):
    runner,services,binding,_,rest=configure(runtime,monkeypatch)
    services.tick(runner)
    original=services.connection.prepared
    gateway=services.connection.gateway;real=hook_transport.connection
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    clock=[100.]
    monkeypatch.setattr(retry,'time',SimpleNamespace(time=lambda:clock[0]))
    monkeypatch.setattr(hook_transport,'connection',lambda _,t:real(sock.getsockname()[1],t))
    try:
        for t in (100,101,106):clock[0]=t;services.tick(runner)
        assert services.startup_check_state()['canRecheck']
        assert services.connection.prepared is original
        callback(services.connection)
        from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
        with _headless_web_ui_module() as module:
            window=WebUiBindingBehaviorTest._window(module,binding)
            window.runner=runner;window._publish=Mock()
            monkeypatch.setattr(module,'_log_rows',lambda:[])
            monkeypatch.setattr(module,'latest_incident',lambda:None)
            states=[window._state()]
            window.set_accepting(True)
            states.append(window._state())
        assert services._recheck_requested.is_set() and binding.run_status=='paused'
        monkeypatch.setattr(hook_transport,'connection',real)
        services.tick(runner);services.tick(runner)
        assert retry.pending() is None and services.startup_check_state()['canStart']
        assert binding.run_status=='paused' and 'running' not in rest[3]
        import os
        from pathlib import Path
        target=os.environ.get('CHEJIN_RECHECK_UI_STATES')
        if target:Path(target).write_text(json.dumps(states,ensure_ascii=False))
        with storage.db_connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1
            assert conn.execute("SELECT COUNT(*) FROM hook_read_retry WHERE state='operator_recheck'").fetchone()[0]==1
    finally:sock.close()


@pytest.mark.parametrize('failed_route',['IsLoginStatus','GetDBHandleAndPath','QueryDB'])
def test_history_retry_uses_new_capture_and_preserves_scope_checks(history_lab,monkeypatch,failed_route):
    from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
    server,gateway,process,scope,delivery,_,_=history_lab
    real=hook_transport.connection;post=hook_transport.post
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    captures=[];clock=[100.]
    monkeypatch.setattr(retry,'time',SimpleNamespace(time=lambda:clock[0]))
    synchronizer=HookHistorySynchronizer(gateway,captures.append,lambda:(0,0))
    try:
        def route_fault(conn,route,payload,limit):
            if route==failed_route:
                bad=real(sock.getsockname()[1],1)
                return post(bad,route,payload,limit)
            return post(conn,route,payload,limit)
        monkeypatch.setattr(hook_transport,'post',route_fault)
        with pytest.raises(retry.ReadRetryWait):synchronizer.capture(process,scope,delivery,'0')
        assert not captures and not unresolved(process.generation)
        assert retry.pending()['route']==failed_route
        assert not ui_lock.lock_summary()['locked']
        clock[0]=101
        monkeypatch.setattr(hook_transport,'post',post)
        if failed_route!='IsLoginStatus':
            gateway.login_status(process)
            assert retry.pending()['route']==failed_route  # Heartbeat cannot reset a failed query budget.
        synchronizer.capture(process,scope,delivery,'0')
        assert captures and retry.pending() is None
        server.wxid='wxid_wrong_account'
        with pytest.raises(HookLocalError,match='ACCOUNT_MISMATCH'):
            synchronizer.capture(process,scope,delivery,'0')
        assert not any(path=='/SendTextMsg' for path,*_ in server.requests)
    finally:sock.close()


def test_changed_page_is_settled_only_by_full_same_customer_history(history_lab,monkeypatch):
    from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
    server,gateway,process,scope,delivery,_,db=history_lab
    real=hook_transport.connection;post=hook_transport.post
    sock=socket.socket();sock.bind(('127.0.0.1',0));clock=[100.]
    monkeypatch.setattr(retry,'time',SimpleNamespace(time=lambda:clock[0]))
    def fail_page(conn,route,payload,limit):
        if route=='QueryDB' and 'ORDER BY MSG.localId ASC' in payload['sql']:
            return post(real(sock.getsockname()[1],1),route,payload,limit)
        return post(conn,route,payload,limit)
    synchronizer=HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0))
    try:
        monkeypatch.setattr(hook_transport,'post',fail_page)
        with pytest.raises(retry.ReadRetryWait):synchronizer.capture(process,scope,delivery,'0')
        event=retry.pending()['event_id'];assert retry.pending()['scope_key']
        db.execute("INSERT INTO MSG VALUES(5,'5005',1,0,1791543700,5,5,5,?,'synthetic new message')",(delivery.peer_wxid,))
        clock[0]=101
        with pytest.raises(retry.ReadRetryWait):synchronizer.capture(process,scope,delivery,'0')
        assert retry.pending()['event_id']==event and retry.pending()['failures']==2
        # Successful schema/login/catalog and changed page bounds did not reset it.
        clock[0]=106;monkeypatch.setattr(hook_transport,'post',post)
        synchronizer.capture(process,scope,delivery,'0')
        assert retry.pending() is None
    finally:sock.close()


def test_retired_customer_query_requires_explicit_new_round(history_lab,monkeypatch):
    _,gateway,process,scope,delivery,_,_=history_lab
    subject=retry.history_subject(delivery,'0')
    with pytest.raises(retry.ReadRetryWait):
        retry.failed(process.generation,'standalone','QueryDB',subject,
            scope_key=retry.fingerprint('QueryDB',subject))
    retry.cancel_retired_history(process.generation,[subject])
    assert retry.pending()['state']=='waiting'
    retry.cancel_retired_history(process.generation,[])
    assert retry.pending()['state']=='cancelled'
    assert retry.pending()['cancel_reason']=='history_authorization_changed'


def test_real_desktop_collector_exports_transport_budget_and_exit_proof(lab,refused,tmp_path):
    import zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    server,gateway,process,*_=lab
    attempt(gateway,process,'RETRY_WAIT')
    from chejin_worker_client.hook_active_calls import verify_previous_generations
    reserve('old-generation','old-send','send_text');finish('old-generation','old-send',False)
    verify_previous_generations(process.generation,lambda old:old=='old-generation')
    before=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='synthetic')
    output=export_bundle(storage.APP_DIR,tmp_path/'evidence.zip')
    with zipfile.ZipFile(output) as z:
        rows=lambda table:json.loads(z.read('state/'+table+'.json'))['rows']
        assert rows('hook_read_retry')[0]['failures']==1
        assert rows('hook_transport_events')[0]['action_id']
        assert rows('hook_process_recovery')[0]['death_proof']=='os_process_generation_absent'
        assert any(r['action_id']=='old-send' and r['state']=='uncertain' for r in rows('hook_active_calls'))
        assert not any('.sqlite' in n for n in z.namelist())
    assert_protected_update_snapshot(before,data_dir=storage.APP_DIR,digest_key='synthetic')


def test_retention_keeps_open_retry_and_unknown_call_evidence(lab,refused):
    _,gateway,process,*_=lab
    attempt(gateway,process,'RETRY_WAIT')
    with storage.db_connection() as conn:
        retry.retire_completed_evidence(conn,'9999');conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM hook_transport_events').fetchone()[0]==1
    reserve('old','unknown','send_text');finish('old','unknown',False)
    retry.record_transport('old','unknown','SendTextMsg',{'delivery':'unknown'})
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_read_retry SET state='cancelled'");conn.commit()
    retry.request_new_round()
    with storage.db_connection() as conn:
        retry.retire_completed_evidence(conn,'9999');conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM hook_read_retry').fetchone()[0]==0
        rows=conn.execute('SELECT action_id FROM hook_transport_events').fetchall()
        assert [r[0] for r in rows]==['unknown']


def test_current_retry_evidence_precedes_resolved_refusals(lab,refused,monkeypatch,tmp_path):
    import zipfile
    from chejin_worker_client import diagnostic_bundle
    _,gateway,process,*_=lab;clock,_,real=refused
    failing=hook_transport.connection
    for _ in range(3):
        monkeypatch.setattr(hook_transport,'connection',failing)
        attempt(gateway,process,'RETRY_WAIT');clock[0]+=1
        monkeypatch.setattr(hook_transport,'connection',real)
        assert gateway.login_status(process).logged_in and retry.pending() is None
    monkeypatch.setattr(hook_transport,'connection',failing)
    attempt(gateway,process,'RETRY_WAIT')
    first=retry.pending()['first_transport_event_id']
    clock[0]+=1;attempt(gateway,process,'RETRY_WAIT')
    clock[0]+=5;attempt(gateway,process,'RETRY_EXHAUSTED')
    before=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='synthetic')
    monkeypatch.setattr(diagnostic_bundle,'MAX_ROWS',2)
    output=diagnostic_bundle.export_bundle(storage.APP_DIR,tmp_path/'bounded.zip')
    with zipfile.ZipFile(output) as archive:
        data=json.loads(archive.read('state/hook_transport_events.json'))
        assert data['truncated'] and len(data['rows'])==2
        assert data['rows'][0]['event_id']==first
        assert all(json.loads(row['evidence'])['delivery']=='not_sent' for row in data['rows'])
        manifest=archive.read('export.json').decode()
        assert 'critical_rows_limit' in manifest
        assert 'synthetic reply' not in manifest
    assert_protected_update_snapshot(before,data_dir=storage.APP_DIR,digest_key='synthetic')


def test_missing_transport_proof_cannot_authorize_another_read(lab,refused):
    _,gateway,process,*_=lab;clock,calls,_=refused
    attempt(gateway,process,'RETRY_WAIT');clock[0]+=1
    with storage.db_connection() as conn:
        conn.execute('DELETE FROM hook_transport_events');conn.commit()
    with pytest.raises(RuntimeError,match='RETRY_EVIDENCE_UNCONFIRMED'):
        gateway.login_status(process)
    assert len(calls)==1 and retry.pending()['failures']==1
