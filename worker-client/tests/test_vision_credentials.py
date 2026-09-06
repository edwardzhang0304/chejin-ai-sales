"""Memory credential boundary; all keys are artificial test sentinels."""
import json
import os
import sys
import threading
from unittest.mock import patch

import pytest

from chejin_worker_client import vision_credentials as credentials
from chejin_worker_client.api import WorkerApiClient, ApiError
from chejin_worker_client.models import Binding

KEY = "FAKE-VISION-0967-MEMORY-SENTINEL"


@pytest.fixture(autouse=True)
def reset_memory():
    credentials.clear_vision_credential()
    yield
    credentials.clear_vision_credential()


@pytest.mark.parametrize("kind", ["official", "debug_uat_locked", "development"])
def test_no_key_fallback_from_old_resource_or_environment(tmp_path, monkeypatch, kind):
    legacy = tmp_path / "vision-runtime.json"
    legacy.write_text(json.dumps({"schema_version": 1, "vision_api_key": KEY}))
    monkeypatch.setenv("CHEJIN_BUILD_KIND", kind)
    monkeypatch.setenv("CHEJIN_VISION_CREDENTIAL_PATH", str(legacy))
    monkeypatch.setenv(credentials.VISION_API_KEY_ENV, KEY)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert credentials.resolve_vision_api_key() == ""
    assert credentials.vision_credential_status()["credential_source"] == "worker_backend"


def test_flow_snapshot_survives_refresh_failure_and_next_flow_has_no_key(monkeypatch):
    monkeypatch.delenv(credentials.VISION_API_KEY_ENV, raising=False)
    credentials.complete_credential_refresh(credentials.begin_credential_refresh(), KEY)
    with credentials.vision_credential_snapshot():
        credentials.complete_credential_refresh(credentials.begin_credential_refresh(), failure_reason="VISION_CREDENTIAL_NETWORK_FAILED")
        assert credentials.resolve_vision_api_key() == KEY
        with credentials.vision_credential_snapshot():
            assert credentials.resolve_vision_api_key() == KEY
    assert credentials.resolve_vision_api_key() == ""
    assert credentials.VISION_API_KEY_ENV not in os.environ


def test_late_response_after_rebind_or_shutdown_is_discarded():
    first = credentials.begin_credential_refresh()
    second = credentials.begin_credential_refresh()
    assert credentials.complete_credential_refresh(second, KEY + "-NEW")
    assert not credentials.complete_credential_refresh(first, KEY)
    credentials.clear_vision_credential()
    assert not credentials.complete_credential_refresh(second, KEY)
    assert credentials.resolve_vision_api_key() == ""


def test_status_and_provider_environment_only_send_key_to_child(monkeypatch):
    monkeypatch.setenv("CHEJIN_BUILD_KIND", "official")
    credentials.complete_credential_refresh(credentials.begin_credential_refresh(), KEY)
    status = credentials.vision_credential_status()
    assert status["configured"] and status["configuration_locked"]
    assert KEY not in json.dumps(status)
    base = {"SAFE": "value", credentials.VISION_API_KEY_ENV: "old"}
    child = credentials.vision_provider_environment(base)
    assert child[credentials.VISION_API_KEY_ENV] == KEY
    assert base[credentials.VISION_API_KEY_ENV] == "old"
    with patch.object(credentials, "_run_vision_provider_probe_request", return_value={"ok": True, "status": 200, "response_text": KEY}):
        probe = credentials.probe_official_vision_provider()
    assert probe["ok"] and KEY not in json.dumps(probe)


@pytest.mark.parametrize("url", ["http://example.com/api", "https://user:password@example.com/api", "https://example.com/api?key=x"])
def test_insecure_credential_url_never_sends_request(url):
    client = WorkerApiClient(url)
    with patch.object(client.session, "get") as get:
        with pytest.raises(ApiError, match="HTTPS"):
            client.get_vision_credential(Binding("worker", "token", "instance"))
        get.assert_not_called()


def test_missing_key_does_not_block_unbound_preflight(monkeypatch):
    from chejin_worker_client.preflight import vision_credential_check, has_blocking_failures
    monkeypatch.setenv("CHEJIN_BUILD_KIND", "official")
    check = vision_credential_check()
    assert check.ok is False
    assert not has_blocking_failures([check])
    assert check.detail["configured"] is False


def test_task_lease_completion_keeps_credential_for_next_flow():
    from types import SimpleNamespace
    from chejin_worker_client.task_runner import TaskLeaseGuard
    credentials.complete_credential_refresh(credentials.begin_credential_refresh(), KEY)
    guard = TaskLeaseGuard(api=WorkerApiClient("http://127.0.0.1:1/api"), binding=Binding("a", "test-token", "instance"), task=SimpleNamespace(id="task"), current_step=lambda: None)
    guard.stop()
    assert credentials.resolve_vision_api_key() == KEY
