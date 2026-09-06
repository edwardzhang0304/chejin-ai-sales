"""Real local HTTP failures and late replies; no production credentials."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from chejin_worker_client.api import WorkerApiClient, ApiError
from chejin_worker_client.models import Binding
from chejin_worker_client import vision_credentials as credentials

KEY = "FAKE-VISION-0967-FAILURE-SENTINEL"


@pytest.fixture
def endpoint():
    state = {"status": 200, "body": {"code": "OK", "data": {"worker_id": "a", "client_instance_id": "a-instance", "configured": True, "vision_api_key": KEY}}, "calls": 0, "pause": False}
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            state["calls"] += 1
            if state["pause"]:
                entered.set(); assert release.wait(5)
            self.send_response(state["status"])
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirected")
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield WorkerApiClient(f"http://127.0.0.1:{server.server_port}/api"), state, entered, release
    release.set(); server.shutdown(); server.server_close(); thread.join(3)
    credentials.clear_vision_credential()


@pytest.mark.parametrize("status", [302, 401, 404, 503, 500])
def test_errors_and_redirect_do_not_echo_body_or_forward_auth(endpoint, status):
    api, state, *_ = endpoint
    state.update(status=status, body={"code": KEY, "message": KEY, "data": KEY})
    with pytest.raises(ApiError) as caught:
        api.get_vision_credential(Binding("a", "test-token", "a-instance"))
    assert KEY not in str(caught.value) + repr(caught.value.data) + caught.value.code
    assert caught.value.status_code == status
    assert state["calls"] == 1


@pytest.mark.parametrize("data", [None, {"worker_id": "b", "client_instance_id": "a-instance", "configured": True, "vision_api_key": KEY}, {"worker_id": "a", "client_instance_id": "wrong", "configured": True, "vision_api_key": KEY}, {"worker_id": "a", "client_instance_id": "a-instance", "configured": False, "vision_api_key": KEY}])
def test_malformed_or_mismatched_response_is_rejected(endpoint, data):
    api, state, *_ = endpoint
    state["body"]["data"] = data
    with pytest.raises(ApiError) as caught:
        api.get_vision_credential(Binding("a", "test-token", "a-instance"))
    assert caught.value.code == "VISION_CREDENTIAL_RESPONSE_INVALID"
    assert KEY not in str(caught.value)


def test_runner_drops_response_after_rebinding_or_exit(endpoint):
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.rpa_bridge import RpaBridge
    api, state, entered, release = endpoint
    runner = TaskRunner(api, RpaBridge(), on_profile=lambda _: None, on_status=lambda _: None, on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None, on_error=lambda _: None)
    runner.binding = binding = Binding("a", "test-token", "a-instance")
    state["pause"] = True
    thread = threading.Thread(target=runner.start, args=(binding,)); thread.start()
    assert entered.wait(3)
    runner.binding = Binding("b", "other-token", "other-instance")
    runner.stop()  # shutdown invalidates any request generation in flight
    release.set(); thread.join(5)
    assert not thread.is_alive()
    assert credentials.resolve_vision_api_key() == ""
    assert runner.thread is None and runner.c2_thread is None
