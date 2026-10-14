"""Real loopback HTTP between the Worker identity client and backend routes."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import socket
import sys
import threading
import time

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).parents[2] / "worker-client"))
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_backend import (
    HookHeartbeatIdentity, HookHeartbeatState, HookIdentityClient, HookWorkerCredentials,
)
from app.core.database import Base, engine
from app.main import app
from test_hook_account_http import client, worker


@pytest.fixture
def identity_client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield HookIdentityClient(f"http://127.0.0.1:{port}/api", 5, 65536)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_worker_receives_server_epoch_and_change_stays_blocked_until_original_reset(identity_client):
    w = worker()
    credentials = HookWorkerCredentials(w['id'], w['worker_token'], w['instance'])
    identity = HookHeartbeatIdentity('wxid_sales_A', 'process-1', 'deployment-1', 'a' * 64)
    state = HookHeartbeatState('idle', 'ready', 'logged_in', None, None, '{}')
    receipt = identity_client.heartbeat(credentials, identity, state)
    assert receipt.run_status == 'paused' and not receipt.blocked
    scope = receipt.receive_scope(30001)
    assert scope.account_epoch == receipt.account_epoch and scope.self_wxid == identity.self_wxid
    assert credentials.worker_token not in repr(credentials)
    with pytest.raises(FrozenInstanceError):
        credentials.client_instance_id = 'other-device'
    identity = replace(identity, account_epoch=receipt.account_epoch)
    assert identity_client.heartbeat(credentials, identity, state).account_epoch == receipt.account_epoch
    changed = identity_client.heartbeat(credentials, replace(identity, self_wxid='wxid_sales_B'), state)
    assert changed.blocked and changed.rebind_required and changed.run_status == 'faulted'
    with pytest.raises(ValueError, match='HOOK_ACCOUNT_NOT_CONFIRMED'):
        changed.receive_scope(30001)
    switched_back = identity_client.heartbeat(credentials, identity, state)
    assert switched_back.blocked and switched_back.rebind_required
    reset = client.post(f"/api/workers/{w['id']}/reset-binding", json={}).json()['data']
    with pytest.raises(ApiError) as error:
        identity_client.heartbeat(credentials, identity, state)
    assert error.value.status_code == 401
    new_credentials = replace(credentials, worker_token=reset['worker_token'], client_instance_id='new-instance')
    bound = client.post(f"/api/workers/{w['id']}/client-bind", json={
        'worker_token': new_credentials.worker_token, 'client_instance_id': new_credentials.client_instance_id})
    assert bound.status_code == 200
    new_identity = replace(identity, self_wxid='wxid_sales_B', account_epoch=None)
    new_receipt = identity_client.heartbeat(new_credentials, new_identity, state)
    assert not new_receipt.blocked and new_receipt.account_epoch != receipt.account_epoch
    # The old request/scope remains immutable; it is never relabelled as B.
    assert scope.self_wxid == 'wxid_sales_A' and identity.account_epoch == receipt.account_epoch


def test_wrong_instance_is_rejected_and_heartbeat_keeps_original_task_and_lock_state(identity_client):
    w = worker()
    credentials = HookWorkerCredentials(w['id'], w['worker_token'], w['instance'])
    identity = HookHeartbeatIdentity('wxid_sales_A', 'process-1', 'deployment-1', 'a' * 64)
    state = HookHeartbeatState('running', 'ready', 'logged_in', 'existing-task', 'filling_form',
        '{"action_lock":"held_by_c1"}')
    receipt = identity_client.heartbeat(credentials, identity, state)
    assert not receipt.blocked
    with pytest.raises(ApiError) as error:
        identity_client.heartbeat(replace(credentials, client_instance_id='another-device'), identity, state)
    assert error.value.status_code == 401
    from app.core.database import SessionLocal
    from app.models.worker import Worker
    with SessionLocal() as db:
        row = db.get(Worker, w['id'])
        assert row.client_instance_id == credentials.client_instance_id
        assert row.current_task == 'existing-task' and row.current_step == 'filling_form'
        assert row.local_lock_summary == {'action_lock': 'held_by_c1'}
