from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import secrets
import shutil
from urllib.parse import urlencode
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError
from app.core.config import get_settings
from app.models.client_release import (
    WorkerClientRelease,
    WorkerClientReleaseDownloadLease,
    WorkerClientReleaseQueryThrottle,
)


VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_PLATFORM = "windows-x64"
SUPPORTED_CHANNEL = "gray"
RELEASE_DESCRIPTOR_FIELDS = (
    "channel",
    "platform",
    "version",
    "status",
    "artifact_storage_key",
    "artifact_size_bytes",
    "artifact_sha256",
    "manifest_signature",
    "signature_key_id",
    "git_commit",
    "package_manifest_sha256",
    "published_at",
    "release_notes",
    "minimum_updater_version",
    "rollback_safe",
)


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _serialized_release_datetime(value: datetime) -> str:
    return (
        _aware_utc(value)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _release_query_throttle_row(
    db: Session,
    *,
    scope: str,
    value: str,
) -> WorkerClientReleaseQueryThrottle:
    key_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
    row = db.scalar(
        select(WorkerClientReleaseQueryThrottle)
        .where(
            WorkerClientReleaseQueryThrottle.scope == scope,
            WorkerClientReleaseQueryThrottle.key_hash == key_hash,
        )
        .with_for_update()
    )
    if row is not None:
        return row
    candidate = WorkerClientReleaseQueryThrottle(
        scope=scope,
        key_hash=key_hash,
    )
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush()
        return candidate
    except IntegrityError:
        row = db.scalar(
            select(WorkerClientReleaseQueryThrottle)
            .where(
                WorkerClientReleaseQueryThrottle.scope == scope,
                WorkerClientReleaseQueryThrottle.key_hash == key_hash,
            )
            .with_for_update()
        )
        if row is None:
            raise
        return row


def record_client_release_query(
    db: Session,
    *,
    ip_address: str,
    client_instance_id: str | None,
) -> None:
    """Rate-limit the public route without storing raw IP or instance IDs."""

    settings = get_settings()
    now = datetime.now(timezone.utc)
    retention_seconds = max(
        300,
        int(settings.client_release_rate_window_seconds) * 2,
    )
    db.execute(
        delete(WorkerClientReleaseQueryThrottle).where(
            WorkerClientReleaseQueryThrottle.updated_at
            < now - timedelta(seconds=retention_seconds)
        )
    )
    dimensions = [("ip", str(ip_address or "unknown")[:256])]
    clean_instance = str(client_instance_id or "").strip()
    if clean_instance:
        dimensions.append(("instance", clean_instance[:128]))
    for scope, value in dimensions:
        row = _release_query_throttle_row(db, scope=scope, value=value)
        window_start = _aware_utc(row.window_started_at)
        if window_start + timedelta(
            seconds=settings.client_release_rate_window_seconds
        ) <= now:
            row.window_started_at = now
            row.request_count = 0
        row.request_count += 1
        row.updated_at = now
        maximum = (
            settings.client_release_ip_max_requests
            if scope == "ip"
            else settings.client_release_instance_max_requests
        )
        if row.request_count > maximum:
            retry_after = max(
                1,
                int(
                    (
                        window_start
                        + timedelta(
                            seconds=settings.client_release_rate_window_seconds
                        )
                        - now
                    ).total_seconds()
                )
                + 1,
            )
            raise AppError(
                "CLIENT_RELEASE_RATE_LIMITED",
                "检查更新过于频繁，请稍后再试",
                429,
                data={"retry_after_seconds": retry_after},
            )


def parse_exact_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise AppError("CLIENT_RELEASE_VERSION_INVALID", "客户端版本格式不合法", 400)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _valid_https_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _clean_artifact_storage_key(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or pure.is_absolute()
        or ".." in pure.parts
        or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in pure.parts)
    ):
        raise AppError(
            "CLIENT_RELEASE_DESCRIPTOR_INVALID",
            "更新包存储定位键不合法",
            400,
        )
    return pure.as_posix()


def _artifact_path(storage_key: str) -> Path:
    root = Path(get_settings().client_release_artifact_root).resolve(strict=False)
    target = root.joinpath(*PurePosixPath(storage_key).parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_UNAVAILABLE",
            "更新包存储位置越界",
            503,
        ) from exc
    return target


def _parse_release_datetime(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(
            "CLIENT_RELEASE_DESCRIPTOR_INVALID",
            f"{field} 必须是带时区的 ISO-8601 时间",
            400,
        ) from exc
    if parsed.tzinfo is None:
        raise AppError(
            "CLIENT_RELEASE_DESCRIPTOR_INVALID",
            f"{field} 必须带时区",
            400,
        )
    return parsed.astimezone(timezone.utc)


def _canonical_release_manifest(payload: dict[str, object]) -> bytes:
    published_at = _parse_release_datetime(
        payload.get("published_at"),
        field="published_at",
    )
    signed = {
        "schema_version": 1,
        "version": payload["version"],
        "channel": payload["channel"],
        "platform": payload["platform"],
        "artifact_size_bytes": payload["artifact_size_bytes"],
        "artifact_sha256": payload["artifact_sha256"],
        "git_commit": payload["git_commit"],
        "package_manifest_sha256": payload["package_manifest_sha256"],
        "published_at": published_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "minimum_updater_version": payload["minimum_updater_version"],
        "rollback_safe": payload["rollback_safe"],
    }
    return json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_release_public_keys(path: Path) -> dict[str, Ed25519PublicKey]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(
            "CLIENT_RELEASE_SIGNING_KEYS_INVALID",
            "发布签名公钥文件不可读",
            400,
        ) from exc
    keys: dict[str, Ed25519PublicKey] = {}
    for item in payload.get("keys") if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or str(item.get("algorithm") or "").lower() != "ed25519":
            continue
        key_id = str(item.get("key_id") or "").strip()
        try:
            raw = base64.b64decode(
                str(item.get("public_key_base64") or ""), validate=True
            )
            if not key_id or len(raw) != 32:
                continue
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw)
        except (TypeError, ValueError):
            continue
    if not keys:
        raise AppError(
            "CLIENT_RELEASE_SIGNING_KEYS_INVALID",
            "发布签名公钥文件没有有效密钥",
            400,
        )
    return keys


def validate_signed_release_descriptor(
    raw: dict[str, object],
    *,
    public_keys_path: Path,
) -> dict[str, object]:
    """Validate the exact signer output before it becomes release authority."""

    if int(raw.get("schema_version") or 0) != 1:
        raise AppError("CLIENT_RELEASE_DESCRIPTOR_INVALID", "发布描述版本不兼容", 400)
    missing = [field for field in RELEASE_DESCRIPTOR_FIELDS if field not in raw]
    if missing:
        raise AppError(
            "CLIENT_RELEASE_DESCRIPTOR_INVALID",
            "发布描述缺少字段",
            400,
            data={"missing_fields": missing},
        )
    channel = str(raw.get("channel") or "").strip().lower()
    platform = str(raw.get("platform") or "").strip().lower()
    version = str(raw.get("version") or "").strip()
    status = str(raw.get("status") or "").strip().lower()
    minimum = str(raw.get("minimum_updater_version") or "").strip()
    parse_exact_version(version)
    parse_exact_version(minimum)
    if channel != SUPPORTED_CHANNEL or platform != SUPPORTED_PLATFORM:
        raise AppError("CLIENT_RELEASE_DESCRIPTOR_INVALID", "发布渠道或平台不合法", 400)
    if status != "published":
        raise AppError("CLIENT_RELEASE_DESCRIPTOR_INVALID", "签名描述只允许登记 published 版本", 400)
    published_at = _parse_release_datetime(raw.get("published_at"), field="published_at")
    storage_key = _clean_artifact_storage_key(raw.get("artifact_storage_key"))
    try:
        artifact_size = int(raw.get("artifact_size_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise AppError("CLIENT_RELEASE_DESCRIPTOR_INVALID", "更新包大小不合法", 400) from exc
    artifact_sha = str(raw.get("artifact_sha256") or "").lower()
    package_sha = str(raw.get("package_manifest_sha256") or "").lower()
    git_commit = str(raw.get("git_commit") or "").lower()
    if (
        artifact_size <= 0
        or not SHA256_RE.fullmatch(artifact_sha)
        or not SHA256_RE.fullmatch(package_sha)
        or not GIT_COMMIT_RE.fullmatch(git_commit)
        or raw.get("rollback_safe") is not True
    ):
        raise AppError("CLIENT_RELEASE_DESCRIPTOR_INVALID", "发布哈希、提交或回滚属性不合法", 400)
    keys = _load_release_public_keys(public_keys_path)
    key_id = str(raw.get("signature_key_id") or "").strip()
    public_key = keys.get(key_id)
    if public_key is None:
        raise AppError("CLIENT_RELEASE_SIGNATURE_INVALID", "发布签名密钥不受信任", 400)
    normalized: dict[str, object] = {
        **raw,
        "channel": channel,
        "platform": platform,
        "version": version,
        "status": status,
        "artifact_storage_key": storage_key,
        "artifact_size_bytes": artifact_size,
        "artifact_sha256": artifact_sha,
        "git_commit": git_commit,
        "package_manifest_sha256": package_sha,
        "published_at": published_at,
        "minimum_updater_version": minimum,
        "release_notes": str(raw.get("release_notes") or ""),
    }
    try:
        signature = base64.b64decode(
            str(raw.get("manifest_signature") or ""), validate=True
        )
        public_key.verify(signature, _canonical_release_manifest(normalized))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise AppError("CLIENT_RELEASE_SIGNATURE_INVALID", "发布描述签名无效", 400) from exc
    return normalized


def store_client_release_artifact(
    release: WorkerClientRelease,
    source_path: Path,
) -> Path:
    """Install one immutable artifact after descriptor verification.

    This is a publication operation, not an update query. It never rewrites a
    different file at the same storage key.
    """

    try:
        source = source_path.resolve(strict=True)
    except OSError as exc:
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_INVALID",
            "发布包文件不存在",
            400,
        ) from exc
    if not source.is_file():
        raise AppError("CLIENT_RELEASE_ARTIFACT_INVALID", "发布包不是普通文件", 400)
    expected_size = int(release.artifact_size_bytes or 0)
    expected_sha = str(release.artifact_sha256 or "").lower()
    if source.stat().st_size != expected_size or _hash_file(source) != expected_sha:
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_INVALID",
            "发布包与签名描述的大小或 SHA-256 不一致",
            400,
        )
    storage_key = _clean_artifact_storage_key(release.artifact_storage_key)
    target = _artifact_path(storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if (
            target.is_file()
            and target.stat().st_size == expected_size
            and _hash_file(target) == expected_sha
        ):
            return target
        raise AppError(
            "CLIENT_RELEASE_IMMUTABLE_CONFLICT",
            "更新包存储定位键已被其他内容占用",
            409,
        )
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != expected_size or _hash_file(temporary) != expected_sha:
            raise AppError(
                "CLIENT_RELEASE_ARTIFACT_INVALID",
                "发布包复制后校验失败",
                400,
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def register_signed_client_release(
    db: Session,
    raw: dict[str, object],
    *,
    public_keys_path: Path,
) -> WorkerClientRelease:
    """Idempotently register one immutable signed release descriptor."""

    payload = validate_signed_release_descriptor(
        raw, public_keys_path=public_keys_path
    )
    existing = db.scalar(
        select(WorkerClientRelease).where(
            WorkerClientRelease.channel == payload["channel"],
            WorkerClientRelease.platform == payload["platform"],
            WorkerClientRelease.version == payload["version"],
        )
    )
    model_values = {field: payload[field] for field in RELEASE_DESCRIPTOR_FIELDS}
    if existing is not None:
        for field, expected in model_values.items():
            actual = getattr(existing, field)
            if isinstance(actual, datetime) and isinstance(expected, datetime):
                actual_value = actual.replace(tzinfo=actual.tzinfo or timezone.utc).astimezone(timezone.utc)
                expected_value = expected.astimezone(timezone.utc)
                equal = actual_value == expected_value
            else:
                equal = actual == expected
            if not equal:
                raise AppError(
                    "CLIENT_RELEASE_IMMUTABLE_CONFLICT",
                    "同版本发布记录已存在且内容不同",
                    409,
                    data={"field": field},
                )
        return existing
    release = WorkerClientRelease(**model_values)
    db.add(release)
    db.flush()
    return release


def withdraw_client_release(
    db: Session,
    *,
    version: str,
    channel: str = SUPPORTED_CHANNEL,
    platform: str = SUPPORTED_PLATFORM,
) -> WorkerClientRelease:
    """Withdraw one release without rewriting its signed artifact identity."""

    clean_version = str(version or "").strip()
    parse_exact_version(clean_version)
    release = db.scalar(
        select(WorkerClientRelease).where(
            WorkerClientRelease.channel == channel,
            WorkerClientRelease.platform == platform,
            WorkerClientRelease.version == clean_version,
        )
    )
    if release is None:
        raise AppError("CLIENT_RELEASE_NOT_FOUND", "客户端发布记录不存在", 404)
    release.status = "withdrawn"
    db.flush()
    return release


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue_download_lease(
    db: Session,
    *,
    release: WorkerClientRelease,
    requested_current_version: str,
    client_instance_id: str | None,
    requester_ip: str,
) -> str:
    settings = get_settings()
    base_url = str(settings.client_release_public_base_url or "").rstrip("/")
    if not _valid_https_url(base_url):
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_UNAVAILABLE",
            "客户端下载入口没有配置为可信 HTTPS 地址",
            503,
        )
    now = datetime.now(timezone.utc)
    retention_before = now - timedelta(
        days=int(settings.client_release_download_lease_retention_days)
    )
    db.execute(
        delete(WorkerClientReleaseDownloadLease).where(
            WorkerClientReleaseDownloadLease.expires_at < retention_before
        )
    )
    raw_token = secrets.token_urlsafe(32)
    lease = WorkerClientReleaseDownloadLease(
        release_id=release.id,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        requested_current_version=requested_current_version,
        client_instance_hash=(
            hashlib.sha256(client_instance_id.encode("utf-8")).hexdigest()
            if client_instance_id
            else None
        ),
        requester_ip_hash=(
            hashlib.sha256(str(requester_ip or "unknown").encode("utf-8")).hexdigest()
        ),
        issued_at=now,
        expires_at=now
        + timedelta(seconds=int(settings.client_release_download_lease_seconds)),
    )
    db.add(lease)
    db.flush()
    query = urlencode({"token": raw_token})
    return f"{base_url}/client-releases/artifacts/{lease.id}?{query}"


def _published_release_payload(
    db: Session,
    release: WorkerClientRelease,
    *,
    requested_current_version: str,
    client_instance_id: str | None,
    requester_ip: str,
) -> dict[str, object]:
    storage_key = str(release.artifact_storage_key or "")
    try:
        artifact_path = _artifact_path(_clean_artifact_storage_key(storage_key))
    except AppError:
        artifact_path = Path()
    fields_valid = bool(
        storage_key
        and artifact_path.is_file()
        and artifact_path.stat().st_size == int(release.artifact_size_bytes or 0)
        and int(release.artifact_size_bytes or 0) > 0
        and SHA256_RE.fullmatch(str(release.artifact_sha256 or "").lower())
        and str(release.manifest_signature or "").strip()
        and str(release.signature_key_id or "").strip()
        and GIT_COMMIT_RE.fullmatch(str(release.git_commit or "").lower())
        and SHA256_RE.fullmatch(str(release.package_manifest_sha256 or "").lower())
        and release.published_at is not None
        and release.rollback_safe is True
    )
    if not fields_valid:
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_UNAVAILABLE",
            "最新客户端发布记录尚不可安全下载",
            503,
        )
    return {
        "artifact_url": _issue_download_lease(
            db,
            release=release,
            requested_current_version=requested_current_version,
            client_instance_id=client_instance_id,
            requester_ip=requester_ip,
        ),
        "artifact_size_bytes": int(release.artifact_size_bytes or 0),
        "artifact_sha256": str(release.artifact_sha256 or "").lower(),
        "manifest_signature": str(release.manifest_signature or ""),
        "signature_key_id": str(release.signature_key_id or ""),
        "git_commit": str(release.git_commit or "").lower(),
        "package_manifest_sha256": str(release.package_manifest_sha256 or "").lower(),
        "published_at": _serialized_release_datetime(release.published_at),
        "release_notes": str(release.release_notes or ""),
        "minimum_updater_version": str(release.minimum_updater_version or "0.9.59"),
        "rollback_safe": True,
    }


def latest_client_release(
    db: Session,
    *,
    current_version: str,
    platform: str,
    channel: str,
    client_instance_id: str | None = None,
    requester_ip: str = "unknown",
) -> dict[str, object]:
    current = parse_exact_version(current_version)
    platform_value = str(platform or "").strip().lower()
    channel_value = str(channel or "").strip().lower()
    if platform_value != SUPPORTED_PLATFORM:
        raise AppError("CLIENT_RELEASE_PLATFORM_UNSUPPORTED", "客户端平台不受支持", 400)
    if channel_value != SUPPORTED_CHANNEL:
        raise AppError("CLIENT_RELEASE_CHANNEL_UNSUPPORTED", "客户端更新渠道不受支持", 400)

    releases = list(
        db.scalars(
            select(WorkerClientRelease).where(
                WorkerClientRelease.platform == platform_value,
                WorkerClientRelease.channel == channel_value,
                WorkerClientRelease.status == "published",
                WorkerClientRelease.rollback_safe.is_(True),
            )
        ).all()
    )
    valid_versions: list[tuple[tuple[int, int, int], WorkerClientRelease]] = []
    for release in releases:
        try:
            parsed = parse_exact_version(release.version)
            parse_exact_version(release.minimum_updater_version)
        except AppError:
            continue
        valid_versions.append((parsed, release))
    if not valid_versions:
        return {
            "update_available": False,
            "latest_version": current_version,
            "channel": channel_value,
            "platform": platform_value,
            "minimum_updater_version": "0.9.59",
            "rollback_safe": True,
            "client_ahead_of_channel": False,
        }

    latest_tuple, latest = max(valid_versions, key=lambda item: item[0])
    base = {
        "latest_version": latest.version,
        "channel": channel_value,
        "platform": platform_value,
        "published_at": _serialized_release_datetime(latest.published_at),
        "release_notes": str(latest.release_notes or ""),
        "minimum_updater_version": str(latest.minimum_updater_version),
        "rollback_safe": bool(latest.rollback_safe),
    }
    if current >= latest_tuple:
        return {
            **base,
            "update_available": False,
            "client_ahead_of_channel": current > latest_tuple,
        }
    return {
        **base,
        **_published_release_payload(
            db,
            latest,
            requested_current_version=current_version,
            client_instance_id=client_instance_id,
            requester_ip=requester_ip,
        ),
        "update_available": True,
        "client_ahead_of_channel": False,
    }


def resolve_client_release_download(
    db: Session,
    *,
    lease_id: str,
    raw_token: str,
) -> tuple[WorkerClientRelease, Path]:
    """Resolve one live lease without changing release identity or lease row."""

    token_hash = hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()
    lease = db.scalar(
        select(WorkerClientReleaseDownloadLease).where(
            WorkerClientReleaseDownloadLease.id == lease_id,
            WorkerClientReleaseDownloadLease.token_hash == token_hash,
        )
    )
    now = datetime.now(timezone.utc)
    if lease is None or _aware_utc(lease.expires_at) <= now:
        raise AppError(
            "CLIENT_RELEASE_DOWNLOAD_LEASE_EXPIRED",
            "更新包下载地址已过期，请重新检查更新",
            410,
        )
    release = db.get(WorkerClientRelease, lease.release_id)
    if release is None or release.status != "published" or release.rollback_safe is not True:
        raise AppError(
            "CLIENT_RELEASE_WITHDRAWN",
            "该客户端版本已撤回，禁止继续下载",
            410,
        )
    storage_key = _clean_artifact_storage_key(release.artifact_storage_key)
    path = _artifact_path(storage_key)
    if (
        not path.is_file()
        or path.stat().st_size != int(release.artifact_size_bytes or 0)
    ):
        raise AppError(
            "CLIENT_RELEASE_ARTIFACT_UNAVAILABLE",
            "客户端更新包暂不可用",
            503,
        )
    return release, path
