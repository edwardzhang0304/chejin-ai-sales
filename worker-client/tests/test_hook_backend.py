"""Protocol failures must not create an account scope or leak credentials."""
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

import pytest

from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_backend import (
    HookHeartbeatIdentity, HookHeartbeatState, HookIdentityClient, HookWorkerCredentials,
)


@pytest.fixture
def peer():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = self.server.body
            if (getattr(self.server, 'live_heartbeat_time', False)
                    and self.server.status == 200 and self.path.endswith('/heartbeat')):
                try:
                    data = json.loads(body)
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get('code') == 'OK' and isinstance(data.get('data'), dict):
                    data['data']['last_heartbeat_at'] = datetime.now(timezone.utc).isoformat()
                    body = json.dumps(data).encode()
            self.send_response(self.server.status)
            if self.server.status == 302:
                self.send_header('Location', '/credential-sink')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests, server.status, server.body = [], 200, b'{}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def values(peer, limit=65536):
    credentials = HookWorkerCredentials(str(uuid.uuid4()), 'secret-worker-token', 'instance-A')
    identity = HookHeartbeatIdentity('wxid_sales_A', 'process-1', 'deployment-1', 'a' * 64)
    state = HookHeartbeatState('idle', 'ready', 'logged_in', None, None, '{}')
    client = HookIdentityClient(f'http://127.0.0.1:{peer.server_port}/api', 2, limit)
    return client, credentials, identity, state


@pytest.mark.parametrize('status,body,code', [
    (302, b'{}', 'HOOK_BACKEND_REDIRECT_REJECTED'),
    (503, b'{"message":"secret-worker-token"}', 'HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'),
    (200, b'not json: secret-worker-token', 'HOOK_ACCOUNT_RESPONSE_INVALID'),
])
def test_failed_response_has_one_request_no_redirect_retry_or_raw_body(peer, status, body, code):
    peer.status, peer.body = status, body
    client, credentials, identity, state = values(peer)
    with pytest.raises(ApiError) as error:
        client.heartbeat(credentials, identity, state)
    assert error.value.code == code and credentials.worker_token not in str(error.value)
    assert len(peer.requests) == 1
    path, headers, body = peer.requests[0]
    assert path == f'/api/workers/{credentials.worker_id}/heartbeat'
    assert headers['X-Worker-Token'] == credentials.worker_token
    assert 'X-Inflight-Flow-Id' not in headers and 'X-Task-Lease-Fencing-Token' not in headers
    assert body['hook_context']['account_epoch'] is None


def test_response_capacity_is_enforced(peer):
    peer.body = b'x' * 1024
    client, credentials, identity, state = values(peer, limit=100)
    with pytest.raises(ApiError) as error:
        client.heartbeat(credentials, identity, state)
    assert error.value.code == 'HOOK_BACKEND_RESPONSE_TOO_LARGE'
    assert len(peer.requests) == 1


def test_success_response_for_another_instance_cannot_authorize_scope(peer):
    client, credentials, identity, state = values(peer)
    peer.body = json.dumps({'code': 'OK', 'data': {
        'id': credentials.worker_id, 'client_instance_id': 'other-instance', 'run_status': 'paused',
        'hook_context': {**identity.payload(), 'account_epoch': str(uuid.uuid4()), 'sales_id': str(uuid.uuid4()),
            'blocked': False, 'rebind_required': False, 'reason_code': None,
            'observed_self_wxid': identity.self_wxid}}}).encode()
    with pytest.raises(ApiError) as error:
        client.heartbeat(credentials, identity, state)
    assert error.value.code == 'HOOK_ACCOUNT_RESPONSE_INVALID'


def test_invalid_identity_fails_before_network(peer):
    _, credentials, identity, state = values(peer)
    with pytest.raises(ValueError):
        replace(identity, account_epoch='guessed-epoch')
    with pytest.raises(ValueError):
        replace(credentials, client_instance_id='bad\r\nHeader: injection')
    with pytest.raises(ValueError):
        replace(state, local_lock_summary_json='[]')
    assert peer.requests == []


@pytest.mark.parametrize('status,upstream,expected',[
    (400,'WORKER_DISABLED','HOOK_WORKER_DISABLED'),
    (400,'SOME_UNKNOWN_REASON','HOOK_ACCOUNT_REQUEST_REJECTED'),
    (401,'WORKER_DISABLED','HOOK_ACCOUNT_REQUEST_REJECTED'),
])
def test_only_explicit_disabled_response_gets_disabled_message(peer,status,upstream,expected):
    peer.status=status
    peer.body=json.dumps({'code':upstream,'message':'secret-worker-token arbitrary text'}).encode()
    client,credentials,identity,state=values(peer)
    with pytest.raises(ApiError) as error:client.heartbeat(credentials,identity,state)
    assert error.value.code==expected
    assert 'secret-worker-token' not in str(error.value)


@pytest.mark.parametrize('blocked,rebind,reason,identity_present,expected',[
    (True,False,'HOOK_SALES_DISABLED',True,'HOOK_SALES_DISABLED'),
    (True,True,'HOOK_ACCOUNT_CHANGED',True,'HOOK_ACCOUNT_CHANGED'),
    (True,False,'HOOK_SALES_BINDING_REQUIRED',True,'HOOK_SALES_BINDING_REQUIRED'),
    (True,False,'unrecognized-reason',True,'HOOK_ACCOUNT_NOT_CONFIRMED'),
    (False,False,'HOOK_SALES_DISABLED',False,'HOOK_ACCOUNT_NOT_CONFIRMED'),
    (False,False,None,True,None),
])
def test_receipt_scope_and_connection_share_confirmation_reason(blocked,rebind,reason,identity_present,expected):
    from chejin_worker_client.hook_backend import HookAccountReceipt
    receipt=HookAccountReceipt(str(uuid.uuid4()),'test-instance','wxid_test' if identity_present else None,
        str(uuid.uuid4()) if identity_present else None,str(uuid.uuid4()),'process-1',blocked,rebind,reason,'paused')
    assert receipt.blocking_code()==expected
    if expected:
        with pytest.raises(ValueError) as error:receipt.receive_scope(30001)
        assert error.value.code==expected
    else:
        assert receipt.receive_scope(30001).self_wxid=='wxid_test'


def test_clock_metadata_uses_real_response_boundaries_and_no_identity_leaks(peer, tmp_path):
    from chejin_worker_client.clock_diagnostics import ClockDiagnostics
    client, credentials, identity, state = values(peer)
    monitor = ClockDiagnostics(tmp_path)
    client = replace(client, clock_diagnostics=monitor)
    peer.live_heartbeat_time = True
    peer.body = json.dumps({'code':'OK','data':{
        'id':credentials.worker_id,'client_instance_id':credentials.client_instance_id,'run_status':'paused',
        'clock_reference':{'source':'untrusted','secret':'must-not-be-exported'},
        'hook_context':{**identity.payload(),'observed_self_wxid':identity.self_wxid,
            'self_wxid':identity.self_wxid,'account_epoch':str(uuid.uuid4()),'sales_id':str(uuid.uuid4()),
            'blocked':False,'rebind_required':False,'reason_code':None}}}).encode()
    receipt = client.heartbeat(credentials,identity,state)
    assert receipt.request_started_wall <= receipt.response_received_wall
    assert receipt.request_started_monotonic <= receipt.response_received_monotonic
    evidence = monitor.path.read_text()
    assert monitor.snapshot()['comparison_status'] == 'aligned'
    assert monitor.snapshot()['backend_reference']['status'] == 'unconfirmed'
    assert 'must-not-be-exported' not in evidence and credentials.worker_token not in evidence
