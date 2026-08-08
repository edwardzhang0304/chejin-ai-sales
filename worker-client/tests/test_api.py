from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-worker-api-test-"),
)
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError, WorkerApiClient
from chejin_worker_client.models import Binding


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


if __name__ == "__main__":
    unittest.main()
