from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
from typing import Any, Callable
from urllib.parse import urlparse
import uuid
import zipfile

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__
from .config import CONFIG
from .models import ClientRelease
from .storage import utc_now_iso


UPDATE_SCHEMA_VERSION = 1
UPDATER_VERSION = "0.9.59"
UPDATE_CHANNEL = "gray"
UPDATE_PLATFORM = "windows-x64"
UPDATE_STATES = {
    "idle",
    "checking",
    "downloading",
    "waiting_for_safe_boundary",
    "installing",
    "restarting",
    "verifying",
    "succeeded",
    "failed",
    "rolling_back",
    "rolled_back",
    "rollback_failed",
}
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
PACKAGE_ROOT_NAME = "CheJinWorkerClient"
PACKAGE_MANIFEST_NAME = "update-package-manifest.json"
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_COMPONENT_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


class ClientUpdateError(RuntimeError):
    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
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
    configured = str(os.environ.get("CHEJIN_RELEASE_SIGNING_KEYS_PATH") or "").strip()
    if configured:
        return Path(configured)
    frozen_root = getattr(__import__("sys"), "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "release-signing-public-keys.json"
    return Path(__file__).resolve().parents[1] / "packaging" / "release-signing-public-keys.json"


def load_trusted_release_keys(path: Path | None = None) -> dict[str, Ed25519PublicKey]:
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
        if not isinstance(item, dict) or str(item.get("algorithm") or "").lower() != "ed25519":
            continue
        key_id = str(item.get("key_id") or "").strip()
        try:
            raw_key = base64.b64decode(str(item.get("public_key_base64") or ""), validate=True)
            if len(raw_key) != 32 or not key_id:
                continue
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw_key)
        except (TypeError, ValueError):
            continue
    if not keys:
        raise ClientUpdateError("UPDATE_PACKAGE_SIGNATURE_INVALID", "客户端没有可信发布签名公钥")
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
    if target <= current or release.channel != UPDATE_CHANNEL or release.platform != UPDATE_PLATFORM:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新版本、渠道或平台不匹配")
    if parse_exact_version(UPDATER_VERSION) < minimum:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "当前更新器版本过低")
    if not release.rollback_safe:
        raise ClientUpdateError("UPDATE_MANUAL_UPGRADE_REQUIRED", "该版本需要人工升级")
    if require_download_url:
        parsed_url = urlparse(str(release.artifact_url or ""))
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新地址不是可信 HTTPS 地址")
    if int(release.artifact_size_bytes or 0) <= 0:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包大小不合法")
    if not SHA256_RE.fullmatch(str(release.artifact_sha256 or "").lower()):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包哈希不合法")
    if not GIT_COMMIT_RE.fullmatch(str(release.git_commit or "").lower()):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包提交标识不合法")
    if not SHA256_RE.fullmatch(str(release.package_manifest_sha256 or "").lower()):
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
        signature = base64.b64decode(str(release.manifest_signature or ""), validate=True)
        public_key.verify(signature, canonical_release_manifest(release))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ClientUpdateError(
            "UPDATE_PACKAGE_SIGNATURE_INVALID",
            "发布清单数字签名校验失败",
            data={"signature_key_id": key_id},
        ) from exc


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def update_root() -> Path:
    configured = str(os.environ.get("CHEJIN_UPDATE_STAGING_ROOT") or "").strip()
    if configured:
        return Path(configured)
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "CheJinWorkerUpdate"
    return CONFIG.app_dir.parent / "CheJinWorkerUpdate"


def is_formal_update_runtime() -> bool:
    """Only the official frozen EXE package may replace its program directory."""

    if not bool(getattr(__import__("sys"), "frozen", False)):
        return False
    roots = [
        Path(str(getattr(__import__("sys"), "_MEIPASS", "") or "")),
        Path(__import__("sys").executable).resolve(strict=False).parent,
    ]
    for root in roots:
        if not str(root):
            continue
        try:
            payload = json.loads(
                (root / "runtime-build-identity.json").read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload.get("formal_release") is True and str(
                payload.get("build_kind") or ""
            ).strip().lower() == "official"
    return False


class UpdateStateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or update_root()
        self.state_path = self.root / "update-state.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"schema_version": UPDATE_SCHEMA_VERSION, "state": "idle"}
            except (OSError, json.JSONDecodeError) as exc:
                raise ClientUpdateError(
                    "UPDATE_STATE_INVALID",
                    "本地更新状态不可读",
                    data={"error_type": type(exc).__name__},
                ) from exc
            if not isinstance(payload, dict) or payload.get("state") not in UPDATE_STATES:
                raise ClientUpdateError("UPDATE_STATE_INVALID", "本地更新状态格式不合法")
            return payload

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = str(payload.get("state") or "")
        if state not in UPDATE_STATES:
            raise ClientUpdateError("UPDATE_STATE_INVALID", "更新状态不合法")
        saved = {**payload, "schema_version": UPDATE_SCHEMA_VERSION, "updated_at": utc_now_iso()}
        with self._lock:
            _atomic_json_write(self.state_path, saved)
        return saved

    def begin(self, *, pre_update_run_status: str, client_instance_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.load()
            if current.get("state") not in {"idle", "succeeded", "failed", "rolled_back"}:
                raise ClientUpdateError("UPDATE_ALREADY_IN_PROGRESS", "已有更新任务正在执行")
            return self.save(
                {
                    "state": "checking",
                    "update_request_id": f"update-{uuid.uuid4()}",
                    "client_instance_id": client_instance_id,
                    "pre_update_run_status": pre_update_run_status,
                    "operator_pause_after_request": False,
                    "fault_after_request": False,
                    "created_at": utc_now_iso(),
                }
            )


def download_release_archive(
    release: ClientRelease,
    destination: Path,
    *,
    session: requests.Session | None = None,
    timeout_seconds: float = 60.0,
) -> Path:
    expected_size = int(release.artifact_size_bytes or 0)
    expected_sha = str(release.artifact_sha256 or "").lower()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    digest = hashlib.sha256()
    received = 0
    client = session or requests.Session()
    try:
        with client.get(str(release.artifact_url), stream=True, timeout=timeout_seconds) as response:
            if int(getattr(response, "status_code", 0) or 0) in {401, 403, 410}:
                raise ClientUpdateError(
                    "UPDATE_DOWNLOAD_URL_EXPIRED",
                    "更新包临时下载地址已过期",
                )
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > expected_size:
                        raise ClientUpdateError("UPDATE_DOWNLOAD_FAILED", "下载内容超过发布记录大小")
                    digest.update(chunk)
                    handle.write(chunk)
        if received != expected_size:
            raise ClientUpdateError("UPDATE_DOWNLOAD_FAILED", "下载内容长度与发布记录不一致")
        if digest.hexdigest() != expected_sha:
            raise ClientUpdateError("UPDATE_PACKAGE_HASH_MISMATCH", "更新包 SHA-256 校验失败")
        os.replace(temporary, destination)
        return destination
    except ClientUpdateError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, requests.RequestException) as exc:
        temporary.unlink(missing_ok=True)
        raise ClientUpdateError(
            "UPDATE_DOWNLOAD_FAILED",
            "更新包下载失败",
            data={"error_type": type(exc).__name__},
        ) from exc


def _validated_member_path(member: zipfile.ZipInfo, destination: Path) -> Path:
    name = member.filename.replace("\\", "/")
    pure = PurePosixPath(name)
    if not name or name.startswith(("/", "\\")) or pure.is_absolute() or ".." in pure.parts:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包包含越界路径")
    if pure.parts and re.match(r"^[A-Za-z]:", pure.parts[0]):
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包包含绝对路径")
    for part in pure.parts:
        trimmed = part.rstrip(" .")
        reserved_base = trimmed.split(".", 1)[0].upper()
        if (
            not trimmed
            or trimmed != part
            or WINDOWS_FORBIDDEN_COMPONENT_RE.search(part)
            or reserved_base in WINDOWS_RESERVED_NAMES
        ):
            raise ClientUpdateError(
                "UPDATE_PACKAGE_INCOMPATIBLE",
                "更新包包含 Windows 非法或设备文件名",
            )
    mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包包含符号链接或设备文件")
    target = destination.joinpath(*pure.parts)
    try:
        target.resolve(strict=False).relative_to(destination.resolve(strict=False))
    except ValueError as exc:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包路径越出 staging 目录") from exc
    return target


def extract_verified_archive(archive_path: Path, staging_root: Path) -> Path:
    temporary = staging_root.with_name(staging_root.name + ".extracting")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包文件数量超过限制")
            total = sum(max(0, int(item.file_size)) for item in members)
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包解压后体积超过限制")
            seen_windows_paths: set[str] = set()
            for member in members:
                target = _validated_member_path(member, temporary)
                windows_key = "/".join(
                    part.casefold()
                    for part in PurePosixPath(
                        member.filename.replace("\\", "/")
                    ).parts
                )
                if windows_key in seen_windows_paths:
                    raise ClientUpdateError(
                        "UPDATE_PACKAGE_INCOMPATIBLE",
                        "更新包包含 Windows 重复路径",
                    )
                seen_windows_paths.add(windows_key)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        package_root = temporary / PACKAGE_ROOT_NAME
        if not package_root.is_dir():
            raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包缺少唯一程序根目录")
        top_level = [item.name for item in temporary.iterdir()]
        if top_level != [PACKAGE_ROOT_NAME]:
            raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包包含额外顶层内容")
        if staging_root.exists():
            shutil.rmtree(staging_root)
        os.replace(temporary, staging_root)
        return staging_root / PACKAGE_ROOT_NAME
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_staged_package(release: ClientRelease, package_root: Path) -> dict[str, Any]:
    manifest_path = package_root / PACKAGE_MANIFEST_NAME
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包内部清单不可读") from exc
    if hashlib.sha256(raw_manifest).hexdigest() != str(release.package_manifest_sha256 or "").lower():
        raise ClientUpdateError("UPDATE_PACKAGE_HASH_MISMATCH", "包内清单 SHA-256 不一致")
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
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包内部清单缺少文件哈希")
    expected_paths: set[str] = set()
    for relative, expected_hash in files.items():
        pure = PurePosixPath(str(relative or ""))
        if (
            not pure.parts
            or pure.is_absolute()
            or ".." in pure.parts
            or not SHA256_RE.fullmatch(str(expected_hash or "").lower())
        ):
            raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包文件清单不合法")
        target = package_root.joinpath(*pure.parts)
        try:
            target.resolve(strict=True).relative_to(package_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包清单指向越界或缺失文件") from exc
        if not target.is_file() or hash_file(target) != str(expected_hash).lower():
            raise ClientUpdateError("UPDATE_PACKAGE_HASH_MISMATCH", "更新包内部文件哈希不一致")
        expected_paths.add(pure.as_posix())
    actual_paths = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file() and path.name != PACKAGE_MANIFEST_NAME
    }
    if actual_paths != expected_paths:
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包内部文件集合与清单不一致")
    main_exe = package_root / "CheJinWorkerClient.exe"
    updater_exe = package_root / "CheJinUpdater.exe"
    if not main_exe.is_file() or not updater_exe.is_file():
        raise ClientUpdateError("UPDATE_PACKAGE_INCOMPATIBLE", "更新包缺少客户端或独立更新器")
    return dict(manifest)


def prepare_release_package(
    release: ClientRelease,
    *,
    request_root: Path,
    session: requests.Session | None = None,
    trusted_keys: dict[str, Ed25519PublicKey] | None = None,
) -> dict[str, Any]:
    validate_release_contract(release)
    verify_release_signature(release, trusted_keys=trusted_keys)
    archive_path = request_root / "download" / "client-update.zip"
    download_release_archive(release, archive_path, session=session)
    staging_root = request_root / "staging"
    try:
        package_root = extract_verified_archive(archive_path, staging_root)
        package_manifest = verify_staged_package(release, package_root)
    except Exception:
        # A hash-valid archive can still fail its internal identity/inventory
        # contract.  It is not executable evidence, so remove only this
        # request's bounded staging and downloaded archive before failing.
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        raise
    return {
        "archive_path": str(archive_path),
        "package_root": str(package_root),
        "package_manifest": package_manifest,
    }


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_status_text(state: dict[str, Any]) -> str:
    code = str(state.get("result_code") or "")
    current_state = str(state.get("state") or "idle")
    target = str(state.get("target_version") or "")
    if code == "UPDATE_ALREADY_LATEST":
        return "当前已是最新版本"
    if current_state == "checking":
        return "正在检查更新"
    if current_state == "downloading":
        return f"发现新版本V{target}，正在下载"
    if current_state == "waiting_for_safe_boundary":
        reason = str(state.get("waiting_reason_text") or "").strip()
        return (
            f"更新包已下载，{reason}"
            if reason
            else "更新包已下载，正在等待当前任务安全结束"
        )
    if current_state in {"installing", "restarting", "verifying"}:
        return "正在安装并重启客户端"
    if current_state == "succeeded":
        return f"已更新到V{target or __version__}"
    if current_state == "rolled_back":
        return f"更新失败，已恢复到V{state.get('previous_version') or __version__}"
    if current_state in {"failed", "rollback_failed"}:
        return "检查更新失败，请重试" if not state.get("install_started") else "更新失败，请查看本机更新日志"
    return ""
