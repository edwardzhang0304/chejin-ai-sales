from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-worker-api-test-"),
)
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError, WorkerApiClient
from chejin_worker_client.models import Binding, ReplySendClaim, Task


def _lease_case():
    client = WorkerApiClient("http://unused/api")
    binding = Binding("worker-1", "secret", "client-1", run_status="running")
    claim = ReplySendClaim.from_api({"task_id": "task-1", "reply_action_id": "reply-1"})
    client._remember_task_lease(Task("task-1", "chat_reply", "running", lease_fencing_token=7))
    return client, binding, claim


def _ack(claim, *, duplicated=False):
    # Duplicate receipt responses intentionally have no task object.
    return {"duplicated": duplicated, "ack": {"task_id": claim.task_id,
        "reply_action_id": claim.reply_action_id, "send_result": "sent"}}


def _send_ack(client, binding, claim):
    return client.sent_ack(binding, claim, send_result="sent", action_phase="confirmed", reply_text_hash=None)


@pytest.mark.parametrize("duplicated", [False, True])
@pytest.mark.parametrize("send_result", ["sent", "failed", "unknown"])
def test_confirmed_ack_releases_only_its_task_lease(monkeypatch, duplicated, send_result):
    client, binding, claim = _lease_case()
    client._remember_task_lease(Task("other", "chat_reply", "running", lease_fencing_token=8))
    response = _ack(claim, duplicated=duplicated)
    response["ack"]["send_result"] = send_result
    monkeypatch.setattr(client, "_request", lambda *a, **k: response)
    assert _send_ack(client, binding, claim) == response
    assert client.task_lease_fencing_tokens == {"other": 8}


@pytest.mark.parametrize("failure", ["timeout", "conflict", "missing_ack", "wrong_task", "wrong_reply"])
def test_unconfirmed_ack_keeps_lease(monkeypatch, failure):
    client, binding, claim = _lease_case()
    def request(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("offline")
        if failure == "conflict":
            raise ApiError("CONFLICT", "conflict", 409)
        response = _ack(claim)
        if failure == "missing_ack":
            return {}
        response["ack"]["task_id" if failure == "wrong_task" else "reply_action_id"] = "unrelated"
        return response
    monkeypatch.setattr(client, "_request", request)
    if failure in {"timeout", "conflict"}:
        with pytest.raises((TimeoutError, ApiError)):
            _send_ack(client, binding, claim)
    else:
        _send_ack(client, binding, claim)
    assert client.task_lease_fencing_tokens == {"task-1": 7}


@pytest.mark.parametrize("new_generation", [False, True])
def test_late_renewal_cannot_resurrect_settled_or_overwrite_new_lease(monkeypatch, new_generation):
    client, binding, claim = _lease_case()
    started, release = Event(), Event()
    def request(method, path, **kwargs):
        if path.endswith("/renew"):
            started.set()
            assert release.wait(5)
            return {"id": claim.task_id, "status": "running", "lease_fencing_token": 7}
        return _ack(claim)
    monkeypatch.setattr(client, "_request", request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.renew_task_lease, binding, claim.task_id, current_step="sending")
        try:
            assert started.wait(5)
            _send_ack(client, binding, claim)
            if new_generation:
                client._remember_task_lease(Task(claim.task_id, "chat_reply", "running", lease_fencing_token=8))
        finally:
            release.set()
        future.result(timeout=5)
    assert client.task_lease_fencing_tokens == ({"task-1": 8} if new_generation else {})


def test_old_ack_response_cannot_clear_a_new_generation(monkeypatch):
    client, binding, claim = _lease_case()
    def request(*args, **kwargs):
        client._remember_task_lease(Task(claim.task_id, "chat_reply", "running", lease_fencing_token=8))
        return _ack(claim)
    monkeypatch.setattr(client, "_request", request)
    _send_ack(client, binding, claim)
    assert client.task_lease_fencing_tokens == {"task-1": 8}


@pytest.mark.parametrize("method", ["complete_invite_sent", "complete_already_friend", "fail_task"])
@pytest.mark.parametrize("new_generation", [False, True])
def test_other_terminal_endpoints_release_only_confirmed_generation(monkeypatch, method, new_generation):
    client, binding, claim = _lease_case()
    def request(*args, **kwargs):
        if new_generation:
            client._remember_task_lease(Task(claim.task_id, "add_friend", "running", lease_fencing_token=8))
        return {"id": claim.task_id, "status": "completed"}
    monkeypatch.setattr(client, "_request", request)
    args = ("TEST_FAILURE", "test", "failed") if method == "fail_task" else ()
    getattr(client, method)(binding, claim.task_id, *args)
    assert client.task_lease_fencing_tokens == ({"task-1": 8} if new_generation else {})


def test_normal_renewal_preserves_live_lease(monkeypatch):
    client, binding, claim = _lease_case()
    monkeypatch.setattr(client, "_request", lambda *a, **k: {
        "id": claim.task_id, "status": "running", "lease_fencing_token": 7,
        "lease_expires_at": "2026-09-04T16:00:00Z"})
    renewed = client.renew_task_lease(binding, claim.task_id, current_step="sending")
    assert renewed.lease_expires_at == "2026-09-04T16:00:00Z"
    assert client.task_lease_fencing_tokens == {claim.task_id: 7}


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "code": "OK",
            "data": {
                "allowed": True,
                "authorization_scope": "batch_continuation",
            },
        }


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Response()


class _ErrorResponse:
    status_code = 409
    text = ""

    @staticmethod
    def json():
        return {
            "code": "MESSAGE_AUTHORIZATION_REVISION_EXPIRED",
            "message": "expired",
            "trace_id": "trace-error-1",
            "data": {
                "retryable": False,
                "recovery_action": "refresh_and_rebuild",
            },
        }


class _ErrorSession:
    def request(self, method, url, **kwargs):
        return _ErrorResponse()


class WorkerApiClientTest(unittest.TestCase):
    def test_heartbeat_does_not_send_local_run_status(self):
        client = WorkerApiClient("http://127.0.0.1:8000/api")
        session = _Session()
        client.session = session
        binding = Binding(
            worker_id="worker-1",
            worker_token="worker-token",
            client_instance_id="client-1",
            run_status="running",
        )

        client.heartbeat(
            binding,
            running_status="idle",
            current_task=None,
            rpa_component_status="ready",
            wechat_status="logged_in",
        )

        payload = session.calls[0]["json"]
        self.assertNotIn("run_status", payload)
        self.assertEqual(payload["running_status"], "idle")

    def test_continuation_token_is_sent_in_header_not_url(self):
        client = WorkerApiClient("http://127.0.0.1:8000/api")
        session = _Session()
        client.session = session
        binding = Binding(
            worker_id="worker-1",
            worker_token="worker-token",
            client_instance_id="client-1",
            run_status="running",
        )

        result = client.get_wechat_read_authorization(
            binding,
            "conversation-1",
            continuation_batch_id="batch-1",
            continuation_token="secret-continuation-token",
        )

        self.assertTrue(result["allowed"])
        call = session.calls[0]
        self.assertIn("continuation_batch_id=batch-1", call["url"])
        self.assertNotIn("secret-continuation-token", call["url"])
        self.assertEqual(
            call["headers"]["X-C2-Continuation-Token"],
            "secret-continuation-token",
        )

    def test_api_error_preserves_backend_recovery_action(self):
        client = WorkerApiClient("http://127.0.0.1:8000/api")
        client.session = _ErrorSession()

        with self.assertRaises(ApiError) as raised:
            client._request("POST", "/workers/worker-1/wechat/messages/ingest")

        self.assertEqual(
            raised.exception.recovery_action,
            "refresh_and_rebuild",
        )
        self.assertEqual(raised.exception.trace_id, "trace-error-1")

    def test_legacy_media_settlement_uses_exact_flow_header_and_payload(self):
        client = WorkerApiClient("http://127.0.0.1:8000/api")
        session = _Session()
        client.session = session
        binding = Binding(
            worker_id="worker-legacy",
            worker_token="worker-token",
            client_instance_id="client-legacy",
            run_status="running",
        )

        client.settle_legacy_media_recovery(
            binding,
            flow_id="read-legacy",
            legacy_record_digest="a" * 64,
            resolution="legacy_owner_unknown_incident",
            conversation_id=None,
            record_summary={"ledger_count": 2},
        )

        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(
            call["url"].endswith(
                "/workers/worker-legacy/legacy-media-recovery/settle"
            )
        )
        self.assertEqual(
            call["headers"]["X-Inflight-Flow-Id"],
            "read-legacy",
        )
        self.assertEqual(
            call["json"],
            {
                "flow_id": "read-legacy",
                "legacy_record_digest": "a" * 64,
                "resolution": "legacy_owner_unknown_incident",
                "conversation_id": None,
                "record_summary": {"ledger_count": 2},
            },
        )


if __name__ == "__main__":
    unittest.main()
