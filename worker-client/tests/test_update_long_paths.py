from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PosixPath
import shutil
import tempfile
import zipfile

import pytest

from chejin_worker_client import client_update, release_package_contract
from chejin_worker_client.update_filesystem import _extended_windows_path, update_filesystem_path
from test_client_update import _Session, _signed_release, TARGET_TEST_VERSION


LONG_MEMBER = (
    "_internal/omniauto-rpa/apps/wechat_ai_customer_service/admin_backend/services/"
    "customer_service_scheduler_state.py"
)


@pytest.fixture
def tmp_path():
    # Keep the control/download filenames below MAX_PATH so the negative
    # control fails at the same *package member* as the customer's incident.
    with tempfile.TemporaryDirectory(prefix="cjup-", dir=None if os.name == "nt" else "/tmp") as name:
        yield Path(name)


@pytest.mark.parametrize("plain,expected", [
    (r"C:\Users\Administrator\AppData\Local\file.py", r"\\?\C:\Users\Administrator\AppData\Local\file.py"),
    ("C:/目录/a/../文件.py", "\\\\?\\C:\\目录\\文件.py"),
    (r"\\server\share\目录\file.py", r"\\?\UNC\server\share\目录\file.py"),
    (r"\\?\C:\existing\file.py", r"\\?\C:\existing\file.py"),
    (r"\\?\UNC\server\share\file.py", r"\\?\UNC\server\share\file.py"),
    ("C:\\" + "deep\\" * 80 + "file.py", "\\\\?\\C:\\" + "deep\\" * 80 + "file.py"),
])
def test_extended_windows_path_preserves_drive_unc_unicode_and_existing_prefix(plain, expected):
    assert _extended_windows_path(plain) == expected


@pytest.mark.parametrize("plain", [r"relative\file", r"C:relative", r"\root_relative", r"\\.\PhysicalDrive0"])
def test_extended_windows_path_rejects_non_filesystem_roots(plain):
    with pytest.raises(ValueError):
        _extended_windows_path(plain)


class _ExtendedTestPath(PosixPath):
    """POSIX filesystem boundary model, NOT Windows/EXE acceptance.

    On macOS a long ordinary path succeeds, so it cannot expose a missing
    Windows adapter. This tagged Path models an extended path at the I/O seam;
    actual file reads, writes, hashes, ZIPs, renames and cleanup still execute.
    The separate Windows case below uses the production adapter unmodified.
    """


@pytest.fixture
def legacy_path_boundary(tmp_path, monkeypatch):
    if os.name == "nt":
        yield {"environment": "native_windows"}
        return
    from chejin_worker_client import chejin_updater, update_coordinator
    for module in (client_update, release_package_contract, chejin_updater, update_coordinator):
        monkeypatch.setattr(module, "update_filesystem_path", lambda p: _ExtendedTestPath(p))
    real_open = io.open

    def guarded_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and str(path).startswith(str(tmp_path)):
            if len(str(path)) >= 260 and not isinstance(path, _ExtendedTestPath):
                raise FileNotFoundError(2, "Legacy Windows path boundary model", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(io, "open", guarded_open)
    real_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        if not isinstance(path, _ExtendedTestPath) and any(len(str(p)) >= 260 for p in Path(path).rglob("*")):
            raise FileNotFoundError(2, "Legacy recursive deletion boundary model", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", guarded_rmtree)
    yield {"environment": "posix_real_files_with_modeled_legacy_path_boundary"}


def _package(*, extra_member=None, corrupt=False):
    files = {"CheJinWorkerClient.exe": b"test client", "CheJinUpdater.exe": b"test updater",
             LONG_MEMBER: b"protected long-file contents", "_internal/车辆资料/说明.txt": b"unicode path"}
    manifest = {"schema_version": 1, "version": TARGET_TEST_VERSION, "platform": "windows-x64",
                "git_commit": "b" * 40, "rollback_safe": True,
                "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CheJinWorkerClient\\", b"")
        archive.writestr("CheJinWorkerClient\\_internal\\", b"")
        archive.writestr("CheJinWorkerClient\\update-package-manifest.json", raw)
        for name, value in files.items():
            archive.writestr("CheJinWorkerClient\\" + name.replace("/", "\\"),
                             b"tampered" if corrupt and name == LONG_MEMBER else value)
        if extra_member:
            archive.writestr(extra_member, b"unauthorized")
    payload = buffer.getvalue()
    release, keys = _signed_release(payload, hashlib.sha256(raw).hexdigest())
    return payload, release, keys


def _request(tmp_path):
    # User directory plus the actual request/staging/package member structure.
    root = tmp_path / "Users" / "Administrator" / "AppData" / "Local" / "CheJinWorkerUpdate"
    request = root / "requests" / "update-68c884bc-baf4-48a2-a5ec-c87ffa3f86f7"
    assert len(str(request / "staging.extracting" / "CheJinWorkerClient" / LONG_MEMBER)) >= 260
    return request


def test_long_path_download_signature_extract_and_inventory(tmp_path, legacy_path_boundary):
    payload, release, keys = _package()
    request = _request(tmp_path)
    result = client_update.prepare_release_package(release, request_root=request,
                                                  session=_Session(payload), trusted_keys=keys)
    root = Path(result["package_root"])
    assert root == request / "staging" / "CheJinWorkerClient"
    assert not str(root).startswith("\\\\?\\")  # The persisted plan keeps logical paths.
    assert release_package_contract.hash_file(root / LONG_MEMBER) == hashlib.sha256(b"protected long-file contents").hexdigest()
    # Updater's second verification starts from a freshly deserialized path.
    assert release_package_contract.verify_staged_package(release, Path(str(root))) == result["package_manifest"]
    assert not (request / "staging.extracting").exists()


@pytest.mark.parametrize("member,corrupt,code", [
    (None, True, "UPDATE_PACKAGE_HASH_MISMATCH"),
    ("CheJinWorkerClient/unlisted.exe", False, "UPDATE_PACKAGE_INCOMPATIBLE"),
    ("CheJinWorkerClient/../../escape.txt", False, "UPDATE_PACKAGE_INCOMPATIBLE"),
    ("CheJinWorkerClient/CON.txt", False, "UPDATE_PACKAGE_INCOMPATIBLE"),
])
def test_long_path_tamper_and_escape_rejected_with_cleanup(tmp_path, legacy_path_boundary, member, corrupt, code):
    request = _request(tmp_path)
    sentinel = tmp_path / "business-data.txt"
    sentinel.write_text("keep")
    control = request / "control"
    control.mkdir(parents=True)
    (control / "evidence.json").write_text("{}")
    payload, release, keys = _package(extra_member=member, corrupt=corrupt)
    with pytest.raises(client_update.ClientUpdateError) as raised:
        client_update.prepare_release_package(release, request_root=request, session=_Session(payload), trusted_keys=keys)
    assert raised.value.code == code
    assert not (request / "staging").exists()
    assert not (request / "staging.extracting").exists()
    assert not (request / "download" / "client-update.zip").exists()
    assert sentinel.read_text() == "keep"
    assert (control / "evidence.json").read_text() == "{}"


def test_disabling_extraction_adapter_reproduces_failure_at_long_file(tmp_path, legacy_path_boundary, monkeypatch):
    if os.name == "nt":
        pytest.skip("The native legacy-limit ablation is an explicit separate gate")
    monkeypatch.setattr(client_update, "update_filesystem_path", lambda path: path)
    payload, release, keys = _package()
    with pytest.raises(client_update.ClientUpdateError) as raised:
        client_update.prepare_release_package(release, request_root=_request(tmp_path), session=_Session(payload), trusted_keys=keys)
    assert raised.value.code == "UPDATE_CHECK_FAILED"
    assert raised.value.data["phase"] == "extract_archive"
    assert raised.value.data["archive_member"].endswith("customer_service_scheduler_state.py")
    assert raised.value.data["filesystem_path_length"] >= 260


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("CHEJIN_TEST_NATIVE_LEGACY_PATH_LIMIT") != "1",
    reason="Explicit native Windows legacy-limit gate; not covered by portable tests",
)
def test_native_windows_legacy_limit_and_fixed_extraction(tmp_path, monkeypatch):
    # This gate MUST use an interpreter without long-path opt-in (or a Windows
    # environment with LongPathsEnabled disabled). Never call native success
    # a regression proof if the unchanged/ordinary-path control also succeeds.
    probe = _request(tmp_path) / "probe" / LONG_MEMBER
    update_filesystem_path(probe.parent).mkdir(parents=True)
    try:
        probe.write_bytes(b"plain path control")
    except OSError:
        pass
    else:
        pytest.fail("Native gate requires active legacy path limits; plain long path unexpectedly succeeded")
    payload, release, keys = _package()
    original_adapter = client_update.update_filesystem_path
    monkeypatch.setattr(client_update, "update_filesystem_path", lambda path: path)
    with pytest.raises(client_update.ClientUpdateError) as raised:
        client_update.prepare_release_package(release, request_root=_request(tmp_path), session=_Session(payload), trusted_keys=keys)
    assert raised.value.data["phase"] == "extract_archive"
    monkeypatch.setattr(client_update, "update_filesystem_path", original_adapter)
    result = client_update.prepare_release_package(release, request_root=_request(tmp_path), session=_Session(payload), trusted_keys=keys)
    assert release_package_contract.verify_staged_package(release, Path(result["package_root"])) == result["package_manifest"]
    shutil.rmtree(update_filesystem_path(_request(tmp_path)))
    assert not _request(tmp_path).exists()


@pytest.mark.parametrize("initial_status", ["running", "paused"])
def test_coordinator_records_actual_extraction_failure_without_install_or_data_loss(
    tmp_path, legacy_path_boundary, monkeypatch, initial_status,
):
    from chejin_worker_client import update_coordinator as coordinator
    from chejin_worker_client.models import Binding
    from test_update_coordinator import FakeApi, FakeRunner, _wait

    payload, release, keys = _package()
    binding = Binding("test-worker", "test-token", "test-instance", run_status=initial_status)
    events = []
    runner = FakeRunner(binding, events)
    gates = []
    logs = []
    monkeypatch.setattr(coordinator, "set_update_new_work_gate", lambda blocked, **kw: gates.append(blocked))
    monkeypatch.setattr(coordinator, "_safe_update_log", lambda *a, **kw: logs.append(kw))
    monkeypatch.setattr(coordinator, "prepare_release_package", lambda value, *, request_root:
                        client_update.prepare_release_package(value, request_root=request_root, session=_Session(payload), trusted_keys=keys))
    real_open = io.open

    def fail_long_file(path, *args, **kwargs):
        if str(path).endswith("customer_service_scheduler_state.py") and args and "w" in args[0]:
            raise OSError(5, "injected file write failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(io, "open", fail_long_file)
    instance = coordinator.UpdateCoordinator(
        FakeApi(release), runner, binding_provider=lambda: binding,
        on_state=lambda state: None, request_normal_exit=lambda: events.append("exit"),
        state_store=client_update.UpdateStateStore(_request(tmp_path).parent.parent), formal_package=True,
    )
    assert instance.check_for_updates()
    _wait(instance)
    state = instance.store.load()
    assert state["state"] == "failed" and state["install_started"] is False
    assert state["result_code"] == "UPDATE_CHECK_FAILED"
    assert state["prepare_diagnostic"]["archive_member"].endswith("customer_service_scheduler_state.py")
    assert state["prepare_diagnostic"]["operation"] == "extract_member"
    assert state["prepare_diagnostic"]["errno"] == 5
    assert any(log.get("metadata", {}).get("prepare_diagnostic") == state["prepare_diagnostic"] for log in logs)
    assert "exit" not in events and gates == [True, False]
    assert runner.statuses == (["paused", "running"] if initial_status == "running" else [])


@pytest.mark.skipif(os.name == "nt", reason="Uses existing POSIX process probes, not formal Windows EXEs")
@pytest.mark.parametrize("healthy", [True, False])
def test_real_updater_deep_file_verification_switch_rollback_and_retired_cleanup(
    tmp_path, legacy_path_boundary, monkeypatch, healthy,
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from chejin_worker_client import chejin_updater as updater
    from chejin_worker_client.models import ClientRelease
    from test_chejin_updater import _prepare_plan, HEALTHY_WORKER, FAILED_WORKER

    plan_path, token, current, previous = _prepare_plan(tmp_path, monkeypatch, new_worker=HEALTHY_WORKER if healthy else FAILED_WORKER)
    plan = json.loads(plan_path.read_text())
    staged = Path(plan["staged_program_dir"])
    # A genuinely deep nested payload and retired version must both be handled.
    member = "nested/" * 22 + LONG_MEMBER
    leaf = updater.update_filesystem_path(staged / member)
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"new deep payload")
    assert len(str(leaf)) > 260
    manifest_path = staged / "update-package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][member] = hashlib.sha256(b"new deep payload").hexdigest()
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path.write_bytes(raw)
    plan["release"]["package_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    key = Ed25519PrivateKey.generate()
    plan["release"]["manifest_signature"] = base64.b64encode(key.sign(client_update.canonical_release_manifest(ClientRelease(**plan["release"])))).decode()
    monkeypatch.setattr(updater, "load_trusted_release_keys", lambda: {"test-key": key.public_key()})
    plan_path.write_text(json.dumps(plan))
    retired_leaf = updater.update_filesystem_path(previous / member)
    retired_leaf.parent.mkdir(parents=True)
    retired_leaf.write_bytes(b"retired version")
    assert updater.run_update(plan_path, token) == (0 if healthy else 1)
    result = json.loads((plan_path.parent / "update-result.json").read_text())
    assert result["result_code"] == ("UPDATE_SUCCEEDED" if healthy else "UPDATE_ROLLED_BACK")
    assert not list(previous.parent.glob("*.retired-*"))
    payload_root = current if healthy else Path(plan["failed_program_dir"])
    assert release_package_contract.hash_file(payload_root / member) == hashlib.sha256(b"new deep payload").hexdigest()
    assert (current / "worker.py").read_text() == HEALTHY_WORKER


def test_coordinator_cleanup_uses_extended_paths_and_preserves_control(tmp_path, legacy_path_boundary):
    from chejin_worker_client import update_coordinator as coordinator
    from test_update_coordinator import FakeApi, FakeRunner
    payload, release, keys = _package()
    request = _request(tmp_path)
    result = client_update.prepare_release_package(release, request_root=request, session=_Session(payload), trusted_keys=keys)
    control = request / "control"
    control.mkdir()
    (control / "update-result.json").write_text("{}")
    instance = coordinator.UpdateCoordinator(
        FakeApi(release), FakeRunner(None, []), binding_provider=lambda: None,
        on_state=lambda state: None, request_normal_exit=lambda: None,
        state_store=client_update.UpdateStateStore(request.parent.parent), formal_package=True,
    )
    instance._cleanup_request_payload(request.name)
    assert not Path(result["package_root"]).exists()
    assert not Path(result["archive_path"]).exists()
    assert (control / "update-result.json").read_text() == "{}"
