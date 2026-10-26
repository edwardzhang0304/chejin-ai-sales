"""Synthetic Windows sharing faults; no customer package or Windows credentials."""
import errno
from pathlib import Path
import threading
import uuid

import pytest

from chejin_worker_client import client_update as update, update_coordinator as coordinator_module
from chejin_worker_client.models import Binding
from test_client_update import _archive, _signed_release, _Session
from test_update_coordinator import FakeApi, FakeRunner, _wait


def denial(code=5):
    exc = PermissionError(errno.EACCES, "synthetic staging denial")
    exc.winerror = code
    return exc


def inject_promotion(monkeypatch, failures, code=5, event=None):
    original = update.os.replace
    attempts = []

    def replace(source, target):
        if Path(source).name == "staging.extracting":
            attempts.append((source, target))
            if len(attempts) <= failures:
                if event:
                    event.set()
                raise denial(code)
        return original(source, target)

    monkeypatch.setattr(update.os, "replace", replace)
    monkeypatch.setattr(update, "STAGING_RETRY_DELAYS", (0, 0, 0, 0))
    return attempts


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_denial_retries_same_directory_and_keeps_verification(tmp_path, monkeypatch, code):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    attempts = inject_promotion(monkeypatch, 2, code)
    progress = []
    result = update.prepare_release_package(release, request_root=tmp_path / "request",
        session=_Session(payload), trusted_keys=keys, on_prepare=progress.append)
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert Path(result["package_root"]).is_dir()
    assert result["package_manifest"]["version"] == release.latest_version
    assert progress[-1]["operation"] == "verify_package"
    assert [p["attempt"] for p in progress if "attempt" in p and not p.get("completed")] == [1, 2, 3]
    assert any(p.get("completed") and p["elapsed_ms"] >= 0 for p in progress)


def test_permanent_denial_is_bounded_preserves_only_archive_and_evidence(tmp_path, monkeypatch):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    attempts = inject_promotion(monkeypatch, 100)
    request = tmp_path / "request"
    control = request / "control"
    control.mkdir(parents=True)
    (control / "evidence.json").write_text("synthetic")
    with pytest.raises(update.ClientUpdateError) as error:
        update.prepare_release_package(release, request_root=request,
            session=_Session(payload), trusted_keys=keys)
    assert error.value.code == update.STAGING_PROMOTION_FAILED
    assert len(attempts) == error.value.data["attempt"] == 5
    assert error.value.data["prior_winerrors"] == [5] * 5
    assert error.value.data["elapsed_ms"] >= 0
    assert (request / "download/client-update.zip").read_bytes() == payload
    assert not (request / "staging.extracting").exists()
    assert not (request / "staging").exists()
    assert (control / "evidence.json").read_text() == "synthetic"


@pytest.mark.parametrize("mode", ["cancel", "non_retryable"])
def test_cancel_or_other_os_error_never_installs_or_retains_archive(tmp_path, monkeypatch, mode):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    event = threading.Event()
    attempts = inject_promotion(monkeypatch, 100, 5 if mode == "cancel" else 112,
                                event if mode == "cancel" else None)
    with pytest.raises(update.ClientUpdateError) as error:
        update.prepare_release_package(release, request_root=tmp_path,
            session=_Session(payload), trusted_keys=keys, cancel_event=event)
    assert len(attempts) == 1
    assert error.value.code == ("UPDATE_CANCELLED" if mode == "cancel" else "UPDATE_CHECK_FAILED")
    assert not (tmp_path / "download/client-update.zip").exists()


@pytest.mark.parametrize("tampered", [False, True])
def test_cached_archive_is_rehashed_before_reuse(tmp_path, tampered):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    archive = tmp_path / "download/client-update.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"x" * len(payload) if tampered else payload)
    session = _Session(payload)
    update.prepare_release_package(release, request_root=tmp_path, session=session, trusted_keys=keys)
    assert session.calls == int(tampered)


def test_retained_archive_does_not_bypass_signature(tmp_path):
    payload, manifest_sha = _archive()
    release, _ = _signed_release(payload, manifest_sha)
    archive = tmp_path / "download/client-update.zip"
    archive.parent.mkdir()
    archive.write_bytes(payload)
    session = _Session(payload)
    with pytest.raises(update.ClientUpdateError):
        update.prepare_release_package(release, request_root=tmp_path, session=session, trusted_keys={})
    assert session.calls == 0 and not (tmp_path / "staging").exists()


def test_coordinator_retry_uses_new_request_and_old_archive_once(tmp_path, monkeypatch):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    session = _Session(payload)
    real_prepare = update.prepare_release_package
    monkeypatch.setattr(coordinator_module, "prepare_release_package",
        lambda release, **kw: real_prepare(release, session=session, trusted_keys=keys, **kw))
    monkeypatch.setattr(coordinator_module, "set_update_new_work_gate", lambda *a, **kw: None)
    binding = Binding("synthetic", "synthetic-token", "synthetic", run_status="faulted")
    runner = FakeRunner(binding, [])
    runner.safe = True
    store = update.UpdateStateStore(tmp_path / "update")
    instance = coordinator_module.UpdateCoordinator(FakeApi(release), runner,
        binding_provider=lambda: binding, on_state=lambda _: None,
        request_normal_exit=lambda: pytest.fail("must not exit in preparation"),
        state_store=store, formal_package=True)
    installs = []
    monkeypatch.setattr(instance, "_start_install", lambda *args: installs.append(args))
    attempts = inject_promotion(monkeypatch, 5)
    assert instance.check_for_updates()
    _wait(instance)
    failed = store.load()
    first_root = store.root / "requests" / failed["update_request_id"]
    assert failed["result_code"] == update.STAGING_PROMOTION_FAILED
    assert len(failed["preparation_events"]) == 6
    assert not installs and binding.run_status == "faulted"
    assert (first_root / "download/client-update.zip").is_file()
    assert (first_root / "cleanup-ready.json").is_file()
    assert instance.check_for_updates()
    _wait(instance)
    assert len(installs) == 1 and session.calls == 1 and len(attempts) == 6
    assert store.load()["update_request_id"] != failed["update_request_id"]
    assert not (first_root / "download/client-update.zip").exists()
    assert binding.run_status == "faulted"


def test_retry_never_deletes_unexpected_staging_destination(tmp_path, monkeypatch):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    original = update.os.replace
    def occupy(source, target):
        if Path(source).name == "staging.extracting":
            Path(target).mkdir()
            (Path(target) / "unrelated.txt").write_text("keep")
            raise denial()
        return original(source, target)
    monkeypatch.setattr(update.os, "replace", occupy)
    with pytest.raises(update.ClientUpdateError):
        update.prepare_release_package(release, request_root=tmp_path,
            session=_Session(payload), trusted_keys=keys)
    assert (tmp_path / "staging/unrelated.txt").read_text() == "keep"


def test_cached_archive_links_are_not_followed(tmp_path):
    payload, manifest_sha = _archive()
    release, keys = _signed_release(payload, manifest_sha)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(payload)
    request = tmp_path / "request"
    (request / "download").mkdir(parents=True)
    (request / "download/client-update.zip").symlink_to(outside)
    with pytest.raises(update.ClientUpdateError, match="不安全"):
        update.prepare_release_package(release, request_root=request,
            session=_Session(payload), trusted_keys=keys)
    assert outside.read_bytes() == payload


@pytest.mark.parametrize("previous", ["../outside", "update-invalid"])
def test_cache_adoption_rejects_invalid_scope(tmp_path, previous):
    instance = object.__new__(coordinator_module.UpdateCoordinator)
    instance.store = update.UpdateStateStore(tmp_path)
    instance._adopt_cached_download({"cached_download_request_id": previous,
                                    "update_request_id": f"update-{uuid.uuid4()}"})
    assert not (tmp_path / "requests").exists()
