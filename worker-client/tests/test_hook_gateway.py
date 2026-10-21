"""Real loopback HTTP and action leases; process facts are controlled fixtures.

These tests do not prove Windows inspection or a real DLL's behavior.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import struct
import threading

import pytest

from chejin_worker_client import storage, ui_lock
from chejin_worker_client.hook_config import HookDeployment
from chejin_worker_client.hook_gateway import HookGateway, HookLocalError
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_process import HookProcessSnapshot, HookProcessError, pe_machine, inspect_windows_process
from chejin_worker_client.hook_receiver import create_receiver


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv('CHEJIN_WORKER_HOME', str(tmp_path))
    from chejin_worker_client import config
    importlib.reload(config)
    importlib.reload(ui_lock)
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            server.requests.append((self.path, body, dict(self.headers)))
            server.lock_observations.append(ui_lock.lock_summary())
            if self.path == '/IsLoginStatus':
                payload = {'onlinestatus': server.login_status, 'selfwxid': server.wxid}
            elif self.path == '/GetDBHandleAndPath' and hasattr(server, 'catalog_entries'):
                payload = {'data': server.catalog_entries}
            elif self.path == '/QueryDB' and hasattr(server, 'query_response'):
                payload = server.query_response(body)
            elif self.path == '/Voice2Text' and hasattr(server, 'voice_response'):
                payload = server.voice_response
            elif self.path == '/ConfigureMsgReciveFullURL':
                payload = {'code': 0, **body, **server.configure_changes}
            else:
                payload = {}
            if server.on_request:
                server.on_request(self.path)
            raw = server.raw_override if server.raw_override is not None else json.dumps(payload).encode()
            self.send_response(server.status)
            if server.status == 302:
                self.send_header('Location', '/unexpected-redirect')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (OSError, ConnectionError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests, server.lock_observations = [], []
    server.status, server.wxid, server.login_status = 200, 'wxid_sales_A', '3'
    server.configure_changes, server.raw_override, server.on_request = {}, None, None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receiver = receiver_thread = None
    try:
        # Bind the actual receiver before deriving configuration; no port guessing.
        probe_scope = HookReceiveScope(server.wxid, 'epoch-1', 'placeholder', server.server_port)
        receiver = create_receiver(port=0, inbox=NativeInbox(max_body_bytes=4096, max_inbox_bytes=65536),
            scope=probe_scope, request_timeout_seconds=1, on_persistence_failure=lambda: None)
        deployment = HookDeployment('deployment-1', r'C:\Work WeChat\WeChat.exe', 'a' * 64,
            server.server_port, receiver.server_port)
        process = HookProcessSnapshot(123, 1791543000.25, deployment.wechat_exe, '3.9.10.16',
            0x14C, 0x14C, 'a' * 64, True, deployment.launch_arguments, (123,), 1, False)
        scope = replace(probe_scope, process_generation=process.generation)
        receiver.server_close()
        receiver = create_receiver(port=deployment.receiver_port,
            inbox=NativeInbox(max_body_bytes=4096, max_inbox_bytes=65536), scope=scope,
            request_timeout_seconds=1, on_persistence_failure=lambda: None)
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_thread.start()
        server.current_process = process
        gateway = HookGateway(deployment, timeout_seconds=1, max_response_bytes=4096,
            lock_timeout_seconds=.1, inspector=lambda _: server.current_process)
        yield server, gateway, process, scope, receiver, receiver_thread
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        if receiver_thread:
            receiver.shutdown(); receiver.server_close(); receiver_thread.join(timeout=3)
            assert not receiver_thread.is_alive()
        assert not thread.is_alive()


def test_login_and_registration_use_existing_action_lock_and_keep_history_gate(lab):
    server, gateway, process, scope, receiver, thread = lab
    assert gateway.login_status(process).self_wxid == scope.self_wxid
    result = gateway.register_callback(process, scope, receiver, thread)
    assert result.scope == scope and result.history_required and not result.business_ready
    assert result.callback_url == gateway.deployment.callback_url
    assert [r[0] for r in server.requests] == ['/IsLoginStatus', '/IsLoginStatus',
        '/ConfigureMsgReciveFullURL', '/IsLoginStatus']
    assert all(x['locked'] and x['operation_type'] == 'hook_control' for x in server.lock_observations)
    assert not ui_lock.lock_summary()['locked']
    assert all('Authorization' not in r[2] and 'X-Worker-Token' not in r[2] for r in server.requests)


def test_c1_lease_prevents_local_hook_request(lab):
    server, gateway, process, *_ = lab
    lease = ui_lock.acquire_ui_lock(operation_type='add_friend', owner='original-C1', timeout_seconds=.1)
    try:
        with pytest.raises(ui_lock.UiLockError): gateway.login_status(process)
        assert server.requests == [] and ui_lock.lock_summary()['operation_type'] == 'add_friend'
    finally:
        lease.release()


def test_process_restart_requires_new_scope_and_no_focus_check(lab):
    server, gateway, process, scope, receiver, thread = lab
    assert replace(process).generation == process.generation
    replacement = replace(process, started_at=process.started_at + 1)
    assert replacement.generation != process.generation
    server.current_process = replacement
    with pytest.raises(HookLocalError, match='GENERATION_CHANGED'): gateway.login_status(process)
    assert server.requests == []
    assert gateway.login_status(replacement).logged_in
    with pytest.raises(HookLocalError, match='RECEIVER_NOT_READY'):
        gateway.register_callback(replacement, scope, receiver, thread)
    # Passive identity reads do not require focus or an expanded window.
    server.current_process = replace(replacement, minimized=True)
    assert not server.current_process.window_available_for_actions
    assert gateway.login_status(server.current_process).logged_in


def test_process_change_during_request_does_not_return_identity(lab):
    server, gateway, process, *_ = lab
    server.on_request = lambda _: setattr(server, 'current_process', replace(process, started_at=process.started_at+1))
    with pytest.raises(HookLocalError, match='GENERATION_CHANGED'): gateway.login_status(process)
    assert len(server.requests) == 1 and not ui_lock.lock_summary()['locked']


@pytest.mark.parametrize('change,code', [
    ({'exe': r'C:\Other\WeChat.exe'}, 'PATH_MISMATCH'),
    ({'file_version': '4.0.0.1'}, 'VERSION_MISMATCH'),
    ({'exe_machine': 0x8664}, 'VERSION_MISMATCH'),
    ({'dll_machine': 0x8664}, 'VERSION_MISMATCH'),
    ({'dll_sha256': 'b'*64}, 'HASH_OR_LOAD'),
    ({'dll_loaded': False}, 'HASH_OR_LOAD'),
    ({'listener_owners': (456,)}, 'PORT_OWNER'),
    ({'listener_owners': (123,None)}, 'PORT_OWNER'),
    ({'command_arguments': ('StartPort=1',)}, 'ARGUMENTS'),
])
def test_unverified_instance_cannot_make_http_requests(lab,change,code):
    server,gateway,process,*_ = lab
    with pytest.raises(HookProcessError,match=code): gateway.login_status(replace(process,**change))
    assert server.requests == []


@pytest.mark.parametrize('raw,status,code', [
    (b'{"onlinestatus":"3","selfwxid":"wxid_sales_A","selfwxid":"wxid_sales_B"}',200,'RESPONSE_INVALID'),
    (b'{"onlinestatus":3,"selfwxid":"wxid_sales_A"}',200,'LOGIN_RESPONSE'),
    (b'{"onlinestatus":[],"selfwxid":"wxid_sales_A"}',200,'LOGIN_RESPONSE'),
    (b'{"onlinestatus":"3"}',200,'IDENTITY_MISSING'),
    (b'x'*4097,200,'TOO_LARGE'),
    (b'{}',302,'HTTP_RESPONSE'),
])
def test_bad_response_is_bounded_not_retried_or_redirected(lab,raw,status,code):
    server,gateway,process,*_ = lab
    server.raw_override,server.status=raw,status
    with pytest.raises(HookLocalError,match=code): gateway.login_status(process)
    assert len(server.requests)==1 and not ui_lock.lock_summary()['locked']


@pytest.mark.parametrize('change',[{'code':True},{'code':-1},{'RecvType':'1'},
    {'CallBackURL':'http://remote.example/receiveChatBotMsg'}])
def test_registration_requires_exact_configuration_echo(lab,change):
    server,gateway,process,scope,receiver,thread=lab
    server.configure_changes=change
    with pytest.raises(HookLocalError) as error: gateway.register_callback(process,scope,receiver,thread)
    assert error.value.outcome_unknown
    assert [x[0] for x in server.requests].count('/ConfigureMsgReciveFullURL')==1


def test_wrong_account_or_dead_receiver_cannot_register(lab):
    server,gateway,process,scope,receiver,thread=lab
    server.wxid='wxid_sales_B'
    with pytest.raises(HookLocalError,match='ACCOUNT_MISMATCH'):
        gateway.register_callback(process,scope,receiver,thread)
    assert [x[0] for x in server.requests]==['/IsLoginStatus']
    receiver.shutdown();thread.join(timeout=2)
    with pytest.raises(HookLocalError,match='RECEIVER_NOT_READY'):
        gateway.register_callback(process,scope,receiver,thread)
    assert len(server.requests)==1


def test_logout_is_an_observation_not_a_new_account(lab):
    server,gateway,process,*_=lab
    server.login_status='0'
    status=gateway.login_status(process)
    assert not status.logged_in and status.self_wxid is None


def test_pe_machine_and_non_windows_inspection_fail_closed(tmp_path,lab):
    data=bytearray(70);data[:2]=b'MZ';struct.pack_into('<I',data,60,64)
    data[64:68]=b'PE\x00\x00';struct.pack_into('<H',data,68,0x14C)
    path=tmp_path/'component.exe';path.write_bytes(data)
    assert pe_machine(path)==0x14C
    path.write_bytes(b'not a component')
    with pytest.raises(HookProcessError):pe_machine(path)
    import sys
    if sys.platform!='win32':
        with pytest.raises(HookProcessError,match='WINDOWS_REQUIRED'):inspect_windows_process(lab[1].deployment)


@pytest.mark.parametrize('status,wxid,code', [
    (3,'wxid_synthetic','HOOK_LOGIN_RESPONSE_INVALID'),
    ('3','12345678901','HOOK_LOGIN_IDENTITY_MISSING'),
    ('3',' wxid_synthetic ','HOOK_LOGIN_IDENTITY_MISSING'),
])
def test_login_rejection_exposes_safe_shape_without_identity(lab,status,wxid,code):
    server,gateway,process,*_=lab
    server.login_status=status;server.wxid=wxid
    with pytest.raises(HookLocalError) as caught:gateway.login_status(process)
    error=caught.value
    assert str(error)==code and error.operation=='IsLoginStatus'
    assert error.diagnostic_details['status_is_string']==isinstance(status,str)
    assert error.diagnostic_details['account_length']==len(wxid)
    assert wxid not in json.dumps(error.diagnostic_details)
