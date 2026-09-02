from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.errors import AppError
from app.main import app
from app.models.client_release import WorkerClientRelease
from app.services.client_release_service import register_signed_client_release
from app.services.client_release_service import store_client_release_artifact
from app.services.client_release_service import withdraw_client_release


ROOT = Path(__file__).resolve().parents[2]
SIGNER = ROOT / "worker-client" / "scripts" / "sign-client-release.py"
sys.path.insert(0, str(ROOT / "worker-client"))
from chejin_worker_client.client_update import (  # noqa: E402
    load_trusted_release_keys,
    verify_release_signature,
)
from chejin_worker_client.models import ClientRelease  # noqa: E402


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _signed_descriptor(tmp_path: Path) -> tuple[dict[str, object], Path]:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    archive = tmp_path / "CheJinWorkerClient-v0.9.60-gray-windows-x64.zip"
    manifest = tmp_path / "update-package-manifest.json"
    descriptor = tmp_path / "release.json"
    keys = tmp_path / "release-signing-public-keys.json"
    archive.write_bytes(b"formal-archive")
    manifest.write_text('{"version":"0.9.60"}', encoding="utf-8")
    keys.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "gray-test",
                        "algorithm": "ed25519",
                        "public_key_base64": base64.b64encode(public_raw).decode(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # PowerShell's round-trip format uses seven fractional digits.  The
    # signer, database transport and client verifier must canonicalize this
    # to one identical signed UTC representation.
    published_at = "2026-09-01T12:34:56.1234567+08:00"
    env = {
        **os.environ,
        "CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64": base64.b64encode(
            private_raw
        ).decode(),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(SIGNER),
            "--archive",
            str(archive),
            "--package-manifest",
            str(manifest),
            "--version",
            "0.9.60",
            "--git-commit",
            "b" * 40,
            "--artifact-storage-key",
            "gray/windows-x64/0.9.60.zip",
            "--published-at",
            published_at,
            "--key-id",
            "gray-test",
            "--output",
            str(descriptor),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert base64.b64encode(private_raw).decode() not in completed.stdout
    return json.loads(descriptor.read_text(encoding="utf-8")), keys


def test_real_signer_database_response_and_client_verifier_share_timestamp_canonicalization(
    tmp_path: Path,
) -> None:
    descriptor, keys_path = _signed_descriptor(tmp_path)
    assert descriptor["published_at"] == "2026-09-01T04:34:56.123456Z"
    settings = get_settings()
    previous_root = settings.client_release_artifact_root
    previous_base = settings.client_release_public_base_url
    settings.client_release_artifact_root = str(tmp_path / "artifact-store")
    settings.client_release_public_base_url = "https://testserver/api"
    try:
        with SessionLocal.begin() as db:
            release = register_signed_client_release(
                db,
                descriptor,
                public_keys_path=keys_path,
            )
            store_client_release_artifact(
                release,
                tmp_path / "CheJinWorkerClient-v0.9.60-gray-windows-x64.zip",
            )

        http = TestClient(app)
        response = http.get(
            "/api/client-releases/latest",
            params={
                "current_version": "0.9.59",
                "platform": "windows-x64",
                "channel": "gray",
            },
            headers={"X-Client-Instance-Id": "cross-boundary-client"},
        )
        assert response.status_code == 200
        client_release = ClientRelease.from_api(response.json()["data"])
        verify_release_signature(
            client_release,
            trusted_keys=load_trusted_release_keys(keys_path),
        )

        lease = urlsplit(str(client_release.artifact_url))
        downloaded = http.get(f"{lease.path}?{lease.query}")
        assert downloaded.status_code == 200
        assert downloaded.content == b"formal-archive"
    finally:
        settings.client_release_artifact_root = previous_root
        settings.client_release_public_base_url = previous_base


def test_real_signer_descriptor_registers_idempotently(tmp_path: Path) -> None:
    descriptor, keys = _signed_descriptor(tmp_path)
    with SessionLocal.begin() as db:
        first = register_signed_client_release(
            db, descriptor, public_keys_path=keys
        )
        first_id = first.id
    with SessionLocal.begin() as db:
        second = register_signed_client_release(
            db, descriptor, public_keys_path=keys
        )
        assert second.id == first_id
        assert db.query(WorkerClientRelease).count() == 1


def test_tampered_signed_field_is_rejected(tmp_path: Path) -> None:
    descriptor, keys = _signed_descriptor(tmp_path)
    descriptor["artifact_sha256"] = "f" * 64
    with SessionLocal.begin() as db:
        with pytest.raises(AppError) as error:
            register_signed_client_release(db, descriptor, public_keys_path=keys)
    assert error.value.code == "CLIENT_RELEASE_SIGNATURE_INVALID"


def test_same_version_cannot_be_repointed_to_another_storage_key(tmp_path: Path) -> None:
    descriptor, keys = _signed_descriptor(tmp_path)
    with SessionLocal.begin() as db:
        register_signed_client_release(db, descriptor, public_keys_path=keys)
    changed = {
        **descriptor,
        "artifact_storage_key": "gray/windows-x64/another.zip",
    }
    with SessionLocal.begin() as db:
        with pytest.raises(AppError) as error:
            register_signed_client_release(db, changed, public_keys_path=keys)
    assert error.value.code == "CLIENT_RELEASE_IMMUTABLE_CONFLICT"


def test_withdraw_preserves_signed_artifact_identity(tmp_path: Path) -> None:
    descriptor, keys = _signed_descriptor(tmp_path)
    with SessionLocal.begin() as db:
        release = register_signed_client_release(
            db, descriptor, public_keys_path=keys
        )
        original_identity = (
            release.id,
            release.artifact_storage_key,
            release.artifact_sha256,
            release.manifest_signature,
        )

    with SessionLocal.begin() as db:
        withdrawn = withdraw_client_release(db, version="0.9.60")
        assert withdrawn.status == "withdrawn"
        assert (
            withdrawn.id,
            withdrawn.artifact_storage_key,
            withdrawn.artifact_sha256,
            withdrawn.manifest_signature,
        ) == original_identity
