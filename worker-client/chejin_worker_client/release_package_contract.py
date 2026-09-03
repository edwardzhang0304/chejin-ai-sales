from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__
from .models import ClientRelease


UPDATE_SCHEMA_VERSION = 1
UPDATER_VERSION = "0.9.62"
UPDATE_CHANNEL = "gray"
UPDATE_PLATFORM = "windows-x64"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PACKAGE_MANIFEST_NAME = "update-package-manifest.json"


class ClientUpdateError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def parse_exact_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新版本格式不合法")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def canonical_utc_timestamp(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").strip().replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新发布时间格式不合法",
        ) from exc
    if parsed.tzinfo is None:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新发布时间必须包含时区",
        )
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def canonical_release_manifest(release: ClientRelease) -> bytes:
    payload = {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "version": release.latest_version,
        "channel": release.channel,
        "platform": release.platform,
        "artifact_size_bytes": release.artifact_size_bytes,
        "artifact_sha256": release.artifact_sha256,
        "git_commit": release.git_commit,
        "package_manifest_sha256": release.package_manifest_sha256,
        "published_at": canonical_utc_timestamp(release.published_at),
        "minimum_updater_version": release.minimum_updater_version,
        "rollback_safe": release.rollback_safe,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bundled_key_path() -> Path:
    configured = str(
        os.environ.get("CHEJIN_RELEASE_SIGNING_KEYS_PATH") or ""
    ).strip()
    if configured:
        return Path(configured)
    frozen_root = getattr(__import__("sys"), "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "release-signing-public-keys.json"
    return (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "release-signing-public-keys.json"
    )


def load_trusted_release_keys(
    path: Path | None = None,
) -> dict[str, Ed25519PublicKey]:
    key_path = path or _bundled_key_path()
    try:
        payload = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_SIGNATURE_INVALID",
            "客户端缺少有效的发布签名公钥",
            data={"key_path": str(key_path), "error_type": type(exc).__name__},
        ) from exc
    keys: dict[str, Ed25519PublicKey] = {}
    for item in payload.get("keys") if isinstance(payload, dict) else []:
        if (
            not isinstance(item, dict)
            or str(item.get("algorithm") or "").lower() != "ed25519"
        ):
            continue
        key_id = str(item.get("key_id") or "").strip()
        try:
            raw_key = base64.b64decode(
                str(item.get("public_key_base64") or ""),
                validate=True,
            )
            if len(raw_key) != 32 or not key_id:
                continue
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw_key)
        except (TypeError, ValueError):
            continue
    if not keys:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_SIGNATURE_INVALID",
            "客户端没有可信发布签名公钥",
        )
    return keys


def validate_release_contract(
    release: ClientRelease,
    *,
    current_version: str = __version__,
    require_download_url: bool = True,
) -> None:
    if not release.update_available:
        return
    current = parse_exact_version(current_version)
    target = parse_exact_version(release.latest_version)
    minimum = parse_exact_version(release.minimum_updater_version)
    if (
        target <= current
        or release.channel != UPDATE_CHANNEL
        or release.platform != UPDATE_PLATFORM
    ):
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新版本、渠道或平台不匹配",
        )
    if parse_exact_version(UPDATER_VERSION) < minimum:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "当前更新器版本过低")
    if not release.rollback_safe:
        raise ClientUpdateError(
            "UPDATE_MANUAL_UPGRADE_REQUIRED",
            "该版本需要人工升级",
        )
    if require_download_url:
        parsed_url = urlparse(str(release.artifact_url or ""))
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
        ):
            raise ClientUpdateError(
                "UPDATE_PACKAGE_INCOMPATIBLE",
                "更新地址不是可信 HTTPS 地址",
            )
    if int(release.artifact_size_bytes or 0) <= 0:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包大小不合法")
    if not SHA256_RE.fullmatch(str(release.artifact_sha256 or "").lower()):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包哈希不合法")
    if not GIT_COMMIT_RE.fullmatch(str(release.git_commit or "").lower()):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包提交标识不合法")
    if not SHA256_RE.fullmatch(
        str(release.package_manifest_sha256 or "").lower()
    ):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "包内清单哈希不合法")
    canonical_utc_timestamp(release.published_at)


def verify_release_signature(
    release: ClientRelease,
    *,
    trusted_keys: dict[str, Ed25519PublicKey] | None = None,
) -> None:
    keys = trusted_keys or load_trusted_release_keys()
    key_id = str(release.signature_key_id or "").strip()
    public_key = keys.get(key_id)
    if public_key is None:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_SIGNATURE_INVALID",
            "发布签名密钥不受客户端信任",
            data={"signature_key_id": key_id},
        )
    try:
        signature = base64.b64decode(
            str(release.manifest_signature or ""),
            validate=True,
        )
        public_key.verify(signature, canonical_release_manifest(release))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_SIGNATURE_INVALID",
            "发布清单数字签名校验失败",
            data={"signature_key_id": key_id},
        ) from exc


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_staged_package(
    release: ClientRelease,
    package_root: Path,
) -> dict[str, Any]:
    manifest_path = package_root / PACKAGE_MANIFEST_NAME
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新包内部清单不可读",
        ) from exc
    if hashlib.sha256(raw_manifest).hexdigest() != str(
        release.package_manifest_sha256 or ""
    ).lower():
        raise ClientUpdateError(
            "UPDATE_PACKAGE_HASH_MISMATCH",
            "包内清单 SHA-256 不一致",
        )
    expected = {
        "schema_version": 1,
        "version": release.latest_version,
        "platform": release.platform,
        "git_commit": str(release.git_commit or "").lower(),
        "rollback_safe": True,
    }
    for key, value in expected.items():
        actual = manifest.get(key) if isinstance(manifest, dict) else None
        if actual != value:
            raise ClientUpdateError(
                "UPDATE_PACKAGE_INCOMPATIBLE",
                "更新包内部清单与发布记录不一致",
                data={"field": key, "expected": value, "actual": actual},
            )
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新包内部清单缺少文件哈希",
        )
    expected_paths: set[str] = set()
    for relative, expected_hash in files.items():
        pure = PurePosixPath(str(relative or ""))
        if (
            not pure.parts
            or pure.is_absolute()
            or ".." in pure.parts
            or not SHA256_RE.fullmatch(str(expected_hash or "").lower())
        ):
            raise ClientUpdateError(
                "UPDATE_PACKAGE_INCOMPATIBLE",
                "更新包文件清单不合法",
            )
        target = package_root.joinpath(*pure.parts)
        try:
            target.resolve(strict=True).relative_to(
                package_root.resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise ClientUpdateError(
                "UPDATE_PACKAGE_INCOMPATIBLE",
                "更新包清单指向越界或缺失文件",
            ) from exc
        if not target.is_file() or hash_file(target) != str(expected_hash).lower():
            raise ClientUpdateError(
                "UPDATE_PACKAGE_HASH_MISMATCH",
                "更新包内部文件哈希不一致",
            )
        expected_paths.add(pure.as_posix())
    actual_paths = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file() and path.name != PACKAGE_MANIFEST_NAME
    }
    if actual_paths != expected_paths:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新包内部文件集合与清单不一致",
        )
    main_exe = package_root / "CheJinWorkerClient.exe"
    updater_exe = package_root / "CheJinUpdater.exe"
    if not main_exe.is_file() or not updater_exe.is_file():
        raise ClientUpdateError(
            "UPDATE_PACKAGE_INCOMPATIBLE",
            "更新包缺少客户端或独立更新器",
        )
    return dict(manifest)
