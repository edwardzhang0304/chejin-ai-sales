"""Download progress uses verified release size and the existing UI state path."""
from pathlib import Path

import pytest
import requests

from chejin_worker_client import client_update, update_coordinator
from chejin_worker_client.models import Binding
from test_client_update import _archive, _signed_release, _Response
from test_update_coordinator import FakeApi, FakeRunner, _wait


class ChunkedResponse(_Response):
    def iter_content(self, chunk_size):
        # Deliberately omit Content-Length, include empty network chunks.
        for offset in range(0, len(self.payload), 3):
            yield b""
            yield self.payload[offset:offset + 3]


class Session:
    def __init__(self, payload, failure=None):
        self.payload = payload
        self.failure = failure
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.failure == "expired" and self.calls == 1:
            return _Response(b"", status_code=410)
        return ChunkedResponse(self.payload)


def test_stream_progress_is_monotonic_bounded_and_verified(tmp_path):
    payload, manifest_sha = _archive()
    release, _ = _signed_release(payload, manifest_sha)
    target = tmp_path / "client.zip"
    progress = []

    def observe(percent):
        progress.append(percent)
        if percent == 100:
            assert target.read_bytes() == payload
        else:
            assert not target.exists()

    client_update.download_release_archive(release, target, session=Session(payload), on_progress=observe)
    assert progress[0] == 0 and progress[-1] == 100
    assert progress == sorted(set(progress))
    assert any(0 < value < 99 for value in progress)
    assert len(progress) <= 101


@pytest.mark.parametrize("failure", ["short", "oversize", "hash", "network"])
def test_failed_download_never_reports_complete_and_retry_resets(tmp_path, failure):
    payload, manifest_sha = _archive()
    release, _ = _signed_release(payload, manifest_sha)
    bad = {"short": payload[:-30], "oversize": payload + b"extra", "hash": b"x" * len(payload)}
    session = Session(bad.get(failure, payload))
    if failure == "network":
        class InterruptedResponse(ChunkedResponse):
            def iter_content(self, chunk_size):
                yield payload[:30]
                raise requests.Timeout("synthetic timeout")
        session.get = lambda *a, **kw: InterruptedResponse(payload)
    progress = []
    target = tmp_path / "client.zip"
    with pytest.raises(client_update.ClientUpdateError):
        client_update.download_release_archive(release, target, session=session, on_progress=progress.append)
    assert 100 not in progress
    assert not target.exists() and not target.with_suffix(".zip.partial").exists()
    failed_count = len(progress)
    client_update.download_release_archive(release, target, session=Session(payload), on_progress=progress.append)
    assert progress[failed_count] == 0 and progress[-1] == 100


@pytest.mark.parametrize("expired", [False, True])
def test_real_prepare_progress_reaches_ui_and_persisted_state(tmp_path, monkeypatch, expired):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    session = Session(payload, "expired" if expired else None)
    real_prepare = client_update.prepare_release_package
    monkeypatch.setattr(update_coordinator, "prepare_release_package",
                        lambda value, **kw: real_prepare(value, session=session, trusted_keys=keys, **kw))
    monkeypatch.setattr(update_coordinator, "set_update_new_work_gate", lambda *a, **kw: None)
    binding = Binding("synthetic-worker", "synthetic-token", "synthetic-instance", run_status="paused")
    runner = FakeRunner(binding, [])
    runner.safe = True
    states = []
    persisted = []
    store = client_update.UpdateStateStore(tmp_path / "update")

    def observe(state):
        states.append(state)
        if state["state"] == "downloading":
            persisted.append(store.load()["download_percent"])

    coordinator = update_coordinator.UpdateCoordinator(
        FakeApi(release), runner, binding_provider=lambda: binding,
        on_state=observe, request_normal_exit=lambda: None,
        state_store=store, formal_package=True,
    )
    installs = []
    monkeypatch.setattr(coordinator, "_start_install", lambda *args: installs.append(args))
    assert coordinator.check_for_updates()
    _wait(coordinator, timeout=10)
    percentages = [s["download_percent"] for s in states if s["state"] == "downloading"]
    assert persisted == percentages
    assert all(s["status_text"].endswith(f'正在下载 {s["download_percent"]}%')
               for s in states if s["state"] == "downloading" and not s.get("prepare_progress"))
    assert any(s.get("prepare_progress", {}).get("operation") == "verify_package" for s in states)
    assert percentages[0] == 0 and percentages[-1] == 100
    assert any(0 < p < 99 for p in percentages)
    assert percentages == sorted(percentages)  # preparation keeps verified download at 100%
    assert session.calls == (2 if expired else 1)
    assert len(installs) == 1
    assert states[-1]["state"] == "waiting_for_safe_boundary"
    assert "%" not in states[-1]["status_text"]
    assert Path(installs[0][0]["package_root"]).is_dir()


@pytest.mark.parametrize("percent", [None, -1, 101, "50", True])
def test_legacy_or_invalid_progress_does_not_invent_percentage(percent):
    assert client_update.update_status_text({"state": "downloading", "download_percent": percent}) == "发现新版本V，正在下载"
