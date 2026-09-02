from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, engine
from app.core.config import get_settings
from app.main import app
from app.models.base import utcnow
from app.models.client_release import WorkerClientRelease
from app.models.client_release import WorkerClientReleaseDownloadLease
from app.models.client_release import WorkerClientReleaseQueryThrottle


client = TestClient(app)
ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "chejin-client-release-api-tests"


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    shutil.rmtree(ARTIFACT_ROOT, ignore_errors=True)
    ARTIFACT_ROOT.mkdir(parents=True)
    settings = get_settings()
    settings.client_release_artifact_root = str(ARTIFACT_ROOT)
    settings.client_release_public_base_url = "https://testserver/api"


def _release(
    version: str,
    *,
    status: str = "published",
    rollback_safe: bool = True,
) -> None:
    artifact = f"formal-package-{version}".encode("utf-8")
    storage_key = f"gray/windows-x64/{version}.zip"
    artifact_path = ARTIFACT_ROOT / storage_key
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact)
    with SessionLocal() as db:
        db.add(
            WorkerClientRelease(
                channel="gray",
                platform="windows-x64",
                version=version,
                status=status,
                artifact_storage_key=storage_key,
                artifact_size_bytes=len(artifact),
                artifact_sha256=hashlib.sha256(artifact).hexdigest(),
                manifest_signature="ed25519-signature",
                signature_key_id="gray-2026-01",
                git_commit="b" * 40,
                package_manifest_sha256="c" * 64,
                published_at=utcnow(),
                release_notes=f"release {version}",
                minimum_updater_version="0.9.59",
                rollback_safe=rollback_safe,
            )
        )
        db.commit()


def _latest(current_version: str):
    return client.get(
        "/api/client-releases/latest",
        params={
            "current_version": current_version,
            "platform": "windows-x64",
            "channel": "gray",
        },
        headers={"X-Client-Instance-Id": "fresh-unbound-client"},
    )


def test_unbound_client_can_discover_the_only_published_newer_release() -> None:
    _release("0.9.59")
    response = _latest("0.9.58")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["update_available"] is True
    assert payload["latest_version"] == "0.9.59"
    assert payload["artifact_url"].startswith("https://")
    assert payload["artifact_sha256"] == hashlib.sha256(
        b"formal-package-0.9.59"
    ).hexdigest()
    assert payload["signature_key_id"] == "gray-2026-01"
    assert payload["git_commit"] == "b" * 40


def test_equal_and_ahead_versions_never_receive_a_download_url() -> None:
    _release("0.9.59")
    equal = _latest("0.9.59").json()["data"]
    ahead = _latest("0.9.60").json()["data"]
    assert equal["update_available"] is False
    assert equal["artifact_url"] is None
    assert equal["client_ahead_of_channel"] is False
    assert ahead["update_available"] is False
    assert ahead["artifact_url"] is None
    assert ahead["client_ahead_of_channel"] is True


def test_withdrawn_draft_wrong_platform_and_rollback_unsafe_are_not_installable() -> None:
    _release("0.9.60", status="withdrawn")
    _release("0.9.61", status="draft")
    _release("0.9.62", rollback_safe=False)
    response = _latest("0.9.59")
    assert response.status_code == 200
    assert response.json()["data"]["update_available"] is False


def test_invalid_version_and_missing_artifact_fail_closed() -> None:
    invalid = _latest("V0.9.58")
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "CLIENT_RELEASE_VERSION_INVALID"

    _release("0.9.59")
    (ARTIFACT_ROOT / "gray/windows-x64/0.9.59.zip").unlink()
    missing = _latest("0.9.58")
    assert missing.status_code == 503
    assert missing.json()["code"] == "CLIENT_RELEASE_ARTIFACT_UNAVAILABLE"


def test_same_release_gets_distinct_append_only_short_lived_download_leases() -> None:
    _release("0.9.59")
    first = _latest("0.9.58").json()["data"]
    second = _latest("0.9.58").json()["data"]
    assert first["artifact_url"] != second["artifact_url"]
    for field in (
        "latest_version",
        "artifact_size_bytes",
        "artifact_sha256",
        "manifest_signature",
        "signature_key_id",
        "git_commit",
        "package_manifest_sha256",
    ):
        assert first[field] == second[field]
    with SessionLocal() as db:
        release = db.query(WorkerClientRelease).one()
        assert release.artifact_storage_key == "gray/windows-x64/0.9.59.zip"
        assert db.query(WorkerClientReleaseDownloadLease).count() == 2


def test_expired_lease_can_be_renewed_but_withdrawn_release_cannot() -> None:
    _release("0.9.59")
    first_url = _latest("0.9.58").json()["data"]["artifact_url"]
    first_parts = urlsplit(first_url)
    with SessionLocal.begin() as db:
        lease = db.query(WorkerClientReleaseDownloadLease).one()
        lease.expires_at = utcnow() - timedelta(seconds=1)

    expired = client.get(f"{first_parts.path}?{first_parts.query}")
    assert expired.status_code == 410
    assert expired.json()["code"] == "CLIENT_RELEASE_DOWNLOAD_LEASE_EXPIRED"

    renewed_url = _latest("0.9.58").json()["data"]["artifact_url"]
    assert renewed_url != first_url
    renewed_parts = urlsplit(renewed_url)
    downloaded = client.get(f"{renewed_parts.path}?{renewed_parts.query}")
    assert downloaded.status_code == 200
    assert downloaded.content == b"formal-package-0.9.59"

    with SessionLocal.begin() as db:
        db.query(WorkerClientRelease).one().status = "withdrawn"
    withdrawn_query = _latest("0.9.58")
    assert withdrawn_query.status_code == 200
    assert withdrawn_query.json()["data"]["update_available"] is False
    withdrawn_download = client.get(f"{renewed_parts.path}?{renewed_parts.query}")
    assert withdrawn_download.status_code == 410
    assert withdrawn_download.json()["code"] == "CLIENT_RELEASE_WITHDRAWN"


def test_wrong_platform_or_channel_never_returns_an_artifact() -> None:
    for platform, channel, expected_code in (
        ("windows-arm64", "gray", "CLIENT_RELEASE_PLATFORM_UNSUPPORTED"),
        ("windows-x64", "stable", "CLIENT_RELEASE_CHANNEL_UNSUPPORTED"),
    ):
        response = client.get(
            "/api/client-releases/latest",
            params={
                "current_version": "0.9.59",
                "platform": platform,
                "channel": channel,
            },
            headers={"X-Client-Instance-Id": "wrong-target-client"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == expected_code
        assert "artifact_url" not in response.json().get("data", {})


def test_public_release_query_is_rate_limited_by_client_instance() -> None:
    settings = get_settings()
    previous_instance_limit = settings.client_release_instance_max_requests
    previous_ip_limit = settings.client_release_ip_max_requests
    settings.client_release_instance_max_requests = 2
    settings.client_release_ip_max_requests = 100
    try:
        assert _latest("0.9.59").status_code == 200
        assert _latest("0.9.59").status_code == 200
        blocked = _latest("0.9.59")
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "CLIENT_RELEASE_RATE_LIMITED"
        assert blocked.json()["data"]["retry_after_seconds"] >= 1
        assert _latest("0.9.59").status_code == 429
    finally:
        settings.client_release_instance_max_requests = previous_instance_limit
        settings.client_release_ip_max_requests = previous_ip_limit


def test_public_release_query_prunes_expired_hashed_rate_rows() -> None:
    with SessionLocal.begin() as db:
        db.add(
            WorkerClientReleaseQueryThrottle(
                scope="instance",
                key_hash="f" * 64,
                window_started_at=utcnow() - timedelta(hours=1),
                request_count=1,
                updated_at=utcnow() - timedelta(hours=1),
            )
        )

    assert _latest("0.9.59").status_code == 200
    with SessionLocal() as db:
        assert (
            db.query(WorkerClientReleaseQueryThrottle)
            .filter(WorkerClientReleaseQueryThrottle.key_hash == "f" * 64)
            .count()
            == 0
        )
