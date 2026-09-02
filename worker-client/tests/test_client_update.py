from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest
import requests

import chejin_worker_client.client_update as client_update_module
import chejin_worker_client.release_package_contract as release_contract_module
from chejin_worker_client.client_update import (
    ClientUpdateError,
    UpdateStateStore,
    canonical_release_manifest,
    download_release_archive,
    extract_verified_archive,
    prepare_release_package,
    update_status_text,
)
from chejin_worker_client.models import ClientRelease
from chejin_worker_client.api import WorkerApiClient


def test_worker_and_updater_share_one_release_package_contract() -> None:
    assert (
        client_update_module.ClientUpdateError
        is release_contract_module.ClientUpdateError
    )
    assert (
        client_update_module.validate_release_contract
        is release_contract_module.validate_release_contract
    )
    assert (
        client_update_module.verify_staged_package
        is release_contract_module.verify_staged_package
    )


class _Response:
    def __init__(self, payload: bytes, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]


class _Session:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(self.payload)


class _FailingSession:
    def get(self, *_args, **_kwargs):
        raise requests.Timeout("simulated download timeout")


def _archive() -> tuple[bytes, str]:
    client_bytes = b"old-client-test"
    updater_bytes = b"updater-test"
    manifest = {
        "schema_version": 1,
        "version": "0.9.60",
        "platform": "windows-x64",
        "git_commit": "b" * 40,
        "rollback_safe": True,
        "files": {
            "CheJinUpdater.exe": hashlib.sha256(updater_bytes).hexdigest(),
            "CheJinWorkerClient.exe": hashlib.sha256(client_bytes).hexdigest(),
        },
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CheJinWorkerClient/update-package-manifest.json", manifest_bytes)
        archive.writestr("CheJinWorkerClient/CheJinWorkerClient.exe", client_bytes)
        archive.writestr("CheJinWorkerClient/CheJinUpdater.exe", updater_bytes)
    return buffer.getvalue(), hashlib.sha256(manifest_bytes).hexdigest()


def _signed_release(payload: bytes, manifest_sha: str):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    release = ClientRelease(
        update_available=True,
        latest_version="0.9.60",
        channel="gray",
        platform="windows-x64",
        artifact_url="https://download.example.test/release.zip?expires=1",
        artifact_size_bytes=len(payload),
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        manifest_signature=None,
        signature_key_id="test-key",
        git_commit="b" * 40,
        package_manifest_sha256=manifest_sha,
        published_at="2026-09-01T00:00:00+00:00",
        release_notes="test",
        minimum_updater_version="0.9.59",
        rollback_safe=True,
    )
    signature = private_key.sign(canonical_release_manifest(release))
    release = ClientRelease(
        **{
            **release.__dict__,
            "manifest_signature": base64.b64encode(signature).decode("ascii"),
        }
    )
    return release, {"test-key": public_key}


def test_full_release_verification_download_and_safe_extraction(tmp_path: Path) -> None:
    payload, manifest_sha = _archive()
    release, trusted_keys = _signed_release(payload, manifest_sha)
    session = _Session(payload)

    result = prepare_release_package(
        release,
        request_root=tmp_path / "request",
        session=session,
        trusted_keys=trusted_keys,
    )

    assert session.calls == 1
    package_root = Path(result["package_root"])
    assert package_root.name == "CheJinWorkerClient"
    assert (package_root / "CheJinWorkerClient.exe").read_bytes() == b"old-client-test"
    assert result["package_manifest"]["version"] == "0.9.60"


@pytest.mark.parametrize(
    "member_name",
    ["../escape.txt", "/absolute.txt", "C:/device.txt", "CheJinWorkerClient/../../escape.txt"],
)
def test_archive_path_escape_is_rejected(tmp_path: Path, member_name: str) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member_name, b"bad")
    with pytest.raises(ClientUpdateError) as raised:
        extract_verified_archive(archive_path, tmp_path / "stage")
    assert raised.value.code == "UPDATE_PACKAGE_INCOMPATIBLE"
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.parametrize(
    "member_name",
    [
        "CheJinWorkerClient/CON",
        "CheJinWorkerClient/NUL.txt",
        "CheJinWorkerClient/file.txt:secret",
        "CheJinWorkerClient/trailing. ",
    ],
)
def test_archive_windows_device_or_stream_name_is_rejected(
    tmp_path: Path,
    member_name: str,
) -> None:
    archive_path = tmp_path / "bad-windows-name.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member_name, b"bad")
    with pytest.raises(ClientUpdateError) as raised:
        extract_verified_archive(archive_path, tmp_path / "stage")
    assert raised.value.code == "UPDATE_PACKAGE_INCOMPATIBLE"


def test_archive_case_insensitive_duplicate_path_is_rejected(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "duplicate-windows-path.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("CheJinWorkerClient/file.txt", b"first")
        archive.writestr("CheJinWorkerClient/FILE.TXT", b"second")
    with pytest.raises(ClientUpdateError) as raised:
        extract_verified_archive(archive_path, tmp_path / "stage")
    assert raised.value.code == "UPDATE_PACKAGE_INCOMPATIBLE"


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFCHR])
def test_archive_symbolic_link_or_device_entry_is_rejected(
    tmp_path: Path,
    file_type: int,
) -> None:
    archive_path = tmp_path / "special-entry.zip"
    special = zipfile.ZipInfo("CheJinWorkerClient/special")
    special.create_system = 3
    special.external_attr = (file_type | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(special, b"target")
    with pytest.raises(ClientUpdateError) as raised:
        extract_verified_archive(archive_path, tmp_path / "stage")
    assert raised.value.code == "UPDATE_PACKAGE_INCOMPATIBLE"


def test_signature_and_hash_tampering_fail_before_install(tmp_path: Path) -> None:
    payload, manifest_sha = _archive()
    release, trusted_keys = _signed_release(payload, manifest_sha)
    bad_signature = ClientRelease(**{**release.__dict__, "manifest_signature": base64.b64encode(b"x" * 64).decode("ascii")})
    with pytest.raises(ClientUpdateError) as signature_error:
        prepare_release_package(
            bad_signature,
            request_root=tmp_path / "signature",
            session=_Session(payload),
            trusted_keys=trusted_keys,
        )
    assert signature_error.value.code == "UPDATE_PACKAGE_SIGNATURE_INVALID"

    bad_payload = payload[:-1] + bytes([payload[-1] ^ 1])
    with pytest.raises(ClientUpdateError) as hash_error:
        prepare_release_package(
            release,
            request_root=tmp_path / "hash",
            session=_Session(bad_payload),
            trusted_keys=trusted_keys,
        )
    assert hash_error.value.code == "UPDATE_PACKAGE_HASH_MISMATCH"


def test_download_timeout_and_short_body_leave_no_partial_archive(
    tmp_path: Path,
) -> None:
    payload, manifest_sha = _archive()
    release, _trusted_keys = _signed_release(payload, manifest_sha)
    destination = tmp_path / "download" / "client.zip"

    with pytest.raises(ClientUpdateError) as timeout_error:
        download_release_archive(
            release,
            destination,
            session=_FailingSession(),
        )
    assert timeout_error.value.code == "UPDATE_DOWNLOAD_FAILED"
    assert not destination.exists()
    assert not destination.with_suffix(".zip.partial").exists()

    with pytest.raises(ClientUpdateError) as short_error:
        download_release_archive(
            release,
            destination,
            session=_Session(payload[:-1]),
        )
    assert short_error.value.code == "UPDATE_DOWNLOAD_FAILED"
    assert not destination.exists()
    assert not destination.with_suffix(".zip.partial").exists()


def test_expired_download_lease_has_a_dedicated_requery_result(
    tmp_path: Path,
) -> None:
    payload, manifest_sha = _archive()
    release, _trusted_keys = _signed_release(payload, manifest_sha)

    class ExpiredSession:
        def get(self, *_args, **_kwargs):
            return _Response(b"", status_code=410)

    with pytest.raises(ClientUpdateError) as raised:
        download_release_archive(
            release,
            tmp_path / "expired.zip",
            session=ExpiredSession(),
        )
    assert raised.value.code == "UPDATE_DOWNLOAD_URL_EXPIRED"
    assert not (tmp_path / "expired.zip.partial").exists()


def test_invalid_internal_inventory_is_removed_before_install(tmp_path: Path) -> None:
    payload, manifest_sha = _archive()
    source = io.BytesIO(payload)
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        rebuilt, "w", compression=zipfile.ZIP_DEFLATED
    ) as changed:
        for member in original.infolist():
            changed.writestr(member, original.read(member))
        changed.writestr("CheJinWorkerClient/unlisted.exe", b"not-authorized")
    changed_payload = rebuilt.getvalue()
    release, trusted_keys = _signed_release(changed_payload, manifest_sha)
    request_root = tmp_path / "invalid-inventory"

    with pytest.raises(ClientUpdateError) as raised:
        prepare_release_package(
            release,
            request_root=request_root,
            session=_Session(changed_payload),
            trusted_keys=trusted_keys,
        )

    assert raised.value.code == "UPDATE_PACKAGE_INCOMPATIBLE"
    assert not (request_root / "staging").exists()
    assert not (request_root / "download" / "client-update.zip").exists()


def test_update_request_is_single_flight_and_status_text_is_fixed(tmp_path: Path) -> None:
    store = UpdateStateStore(tmp_path)
    first = store.begin(pre_update_run_status="running", client_instance_id="client-a")
    assert first["state"] == "checking"
    with pytest.raises(ClientUpdateError) as raised:
        store.begin(pre_update_run_status="running", client_instance_id="client-a")
    assert raised.value.code == "UPDATE_ALREADY_IN_PROGRESS"
    assert update_status_text({"state": "downloading", "target_version": "0.9.60"}) == "发现新版本V0.9.60，正在下载"
    assert update_status_text({"state": "waiting_for_safe_boundary"}) == "更新包已下载，正在等待当前任务安全结束"
    assert update_status_text(
        {
            "state": "waiting_for_safe_boundary",
            "waiting_reason_text": "正在等待后端确认客户端已停止接单",
        }
    ) == "更新包已下载，正在等待后端确认客户端已停止接单"


def test_unbound_client_uses_the_public_release_route_without_worker_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = WorkerApiClient("https://api.example.test/api")
    calls: list[dict] = []

    def request(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {
            "update_available": False,
            "latest_version": "0.9.59",
            "channel": "gray",
            "platform": "windows-x64",
            "minimum_updater_version": "0.9.59",
            "rollback_safe": True,
        }

    monkeypatch.setattr(client, "_request", request)
    release = client.latest_client_release(
        current_version="0.9.59",
        client_instance_id="unbound-instance",
    )

    assert release.update_available is False
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"].startswith("/client-releases/latest?")
    assert calls[0]["extra_headers"] == {
        "X-Client-Instance-Id": "unbound-instance"
    }
    assert "binding" not in calls[0]
