from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


RETENTION_MARKER_NAME = ".chejin-retention.json"
FLOW_DIR_NAME_RE = re.compile(r"^\d{8}_\d{6}(?:_|$)")


@dataclass(frozen=True)
class ArtifactCleanupResult:
    deleted_directories: int = 0
    deleted_files: int = 0
    released_bytes: int = 0
    retained_bytes: int = 0


@dataclass(frozen=True)
class _FlowDirectory:
    path: Path
    modified_at: datetime
    size_bytes: int
    file_count: int
    critical: bool


def _safe_root(app_dir: Path, artifacts_root: Path | None = None) -> Path:
    expected = (app_dir / "artifacts").resolve()
    resolved = (artifacts_root or expected).resolve()
    if resolved != expected:
        raise ValueError("ARTIFACT_CLEANUP_ROOT_OUTSIDE_WORKER_HOME")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _flow_directories(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    c2_root = root / "wechat_c2"
    for category in ("sessions", "messages", "voice"):
        category_root = c2_root / category
        if not category_root.is_dir():
            continue
        candidates.update(
            child
            for child in category_root.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    tasks_root = root / "tasks"
    if tasks_root.is_dir():
        for child in tasks_root.rglob("*"):
            if (
                child.is_dir()
                and not child.is_symlink()
                and (
                    FLOW_DIR_NAME_RE.match(child.name)
                    or (child / RETENTION_MARKER_NAME).is_file()
                )
            ):
                candidates.add(child)
    return sorted(candidates)


def _directory_stats(path: Path) -> tuple[int, int, datetime]:
    size_bytes = 0
    file_count = 0
    latest_timestamp = path.stat().st_mtime
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        stat = item.stat()
        size_bytes += int(stat.st_size)
        file_count += 1
        latest_timestamp = max(latest_timestamp, stat.st_mtime)
    return (
        size_bytes,
        file_count,
        datetime.fromtimestamp(latest_timestamp, tz=timezone.utc),
    )


def _is_critical(path: Path) -> bool:
    marker = path / RETENTION_MARKER_NAME
    if not marker.is_file():
        return True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return str(payload.get("retention_class") or "").strip().lower() != "success"


def record_artifact_outcome(artifact_dir: Path | None, result: dict) -> bool:
    if artifact_dir is None or not artifact_dir.is_dir():
        return True
    error_code = str(result.get("error_code") or "").strip()
    send_result = str(result.get("send_result") or "").strip().lower()
    critical = (
        result.get("ok") is not True
        or bool(error_code)
        or send_result in {"unknown", "failed"}
    )
    marker = {
        "version": 1,
        "retention_class": "critical" if critical else "success",
        "error_code": error_code or None,
        "send_result": send_result or None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        (artifact_dir / RETENTION_MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        try:
            from .storage import append_log

            append_log(
                "WARN",
                "artifact_retention_marker_failed",
                "证据留存标记写入失败，业务动作结果保持不变。",
                error_code=type(exc).__name__,
                metadata={
                    "artifact_dir": str(artifact_dir),
                    "retention_class": marker["retention_class"],
                },
            )
        except Exception:
            pass
        return False
    return True


def cleanup_artifacts(
    *,
    app_dir: Path,
    artifacts_root: Path | None = None,
    success_retention_days: int = 7,
    critical_retention_days: int = 30,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
    protected_paths: Iterable[Path] = (),
    now: datetime | None = None,
) -> ArtifactCleanupResult:
    root = _safe_root(app_dir, artifacts_root)
    if not root.exists():
        return ArtifactCleanupResult()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    protected = {
        path.resolve()
        for path in protected_paths
        if _is_relative_to(path.resolve(), root)
    }
    flows: list[_FlowDirectory] = []
    for path in _flow_directories(root):
        resolved = path.resolve()
        if not _is_relative_to(resolved, root):
            continue
        if any(resolved == item or item.is_relative_to(resolved) for item in protected):
            continue
        size_bytes, file_count, modified_at = _directory_stats(resolved)
        flows.append(
            _FlowDirectory(
                path=resolved,
                modified_at=modified_at,
                size_bytes=size_bytes,
                file_count=file_count,
                critical=_is_critical(resolved),
            )
        )

    deleted: set[Path] = set()
    released_bytes = 0
    deleted_files = 0

    def remove(flow: _FlowDirectory) -> None:
        nonlocal released_bytes, deleted_files
        if flow.path in deleted:
            return
        shutil.rmtree(flow.path)
        deleted.add(flow.path)
        released_bytes += flow.size_bytes
        deleted_files += flow.file_count

    for flow in sorted(flows, key=lambda item: item.modified_at):
        retention_days = (
            max(0, int(critical_retention_days))
            if flow.critical
            else max(0, int(success_retention_days))
        )
        if current - flow.modified_at >= timedelta(days=retention_days):
            remove(flow)

    remaining = [flow for flow in flows if flow.path not in deleted]
    retained_bytes = sum(flow.size_bytes for flow in remaining)
    capacity = max(0, int(max_bytes))
    if retained_bytes > capacity:
        eviction_order = sorted(
            remaining,
            key=lambda item: (item.critical, item.modified_at),
        )
        for flow in eviction_order:
            if retained_bytes <= capacity:
                break
            remove(flow)
            retained_bytes -= flow.size_bytes

    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    return ArtifactCleanupResult(
        deleted_directories=len(deleted),
        deleted_files=deleted_files,
        released_bytes=released_bytes,
        retained_bytes=max(0, retained_bytes),
    )
