"""Local-first backup package builder for shared and tenant data."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import (
    SHARED_KNOWLEDGE_ROOT,
    TENANTS_ROOT,
    active_tenant_id,
    default_admin_knowledge_base_root,
    runtime_app_root,
    tenant_runtime_root,
    tenant_runtime_backups_root,
    tenant_root,
)

from .manifest import file_entry, stable_digest


DERIVED_DIRS = {"rag_chunks", "rag_index", "rag_cache"}
RUNTIME_SKIP_DIRS = {"backups"}
SKIP_SUFFIXES = {".tmp", ".lock"}


class BackupService:
    def __init__(self, *, output_root: Path | None = None) -> None:
        self.output_root = output_root

    def build_backup(
        self,
        *,
        scope: str = "tenant",
        tenant_id: str | None = None,
        include_derived: bool = False,
        include_runtime: bool = False,
    ) -> dict[str, Any]:
        scope = normalize_scope(scope)
        tenant = active_tenant_id(tenant_id)
        backup_id = "backup_" + stable_digest(f"{scope}:{tenant}:{now()}", 20)
        output_root = self.output_root or tenant_runtime_backups_root(tenant)
        output_root.mkdir(parents=True, exist_ok=True)
        package_path = output_root / f"{backup_id}.zip"

        files: list[dict[str, Any]] = []
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as package:
            roots = self.roots_for_scope(scope, tenant, include_runtime=include_runtime)
            for label, root in roots:
                for path in iter_backup_files(root, include_derived=include_derived):
                    relative = path.relative_to(root).as_posix()
                    archive_path = f"payload/{label}/{relative}"
                    package.write(path, archive_path)
                    files.append(file_entry(path, relative_path=archive_path).to_dict())
            manifest = {
                "schema_version": 1,
                "backup_id": backup_id,
                "scope": scope,
                "tenant_id": tenant,
                "created_at": now(),
                "include_derived": include_derived,
                "include_runtime": include_runtime,
                "file_count": len(files),
                "files": sorted(files, key=lambda item: item["path"]),
            }
            package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return {
            "ok": True,
            "backup_id": backup_id,
            "scope": scope,
            "tenant_id": tenant,
            "package_path": str(package_path),
            "bytes": package_path.stat().st_size,
            "manifest": manifest,
        }

    def roots_for_scope(self, scope: str, tenant_id: str, *, include_runtime: bool = False) -> list[tuple[str, Path]]:
        if scope == "shared":
            roots = [("shared_knowledge", SHARED_KNOWLEDGE_ROOT)]
            if include_runtime:
                roots.append(("runtime/shared", runtime_app_root() / "shared_knowledge"))
            return roots
        if scope == "tenant":
            roots = [(f"tenants/{tenant_id}", tenant_root(tenant_id))]
            fallback_root = default_admin_knowledge_base_root(tenant_id)
            tenant_scope_root = tenant_root(tenant_id).resolve()
            if fallback_root.exists() and not path_is_within(fallback_root.resolve(), tenant_scope_root):
                roots.append(("knowledge_bases", fallback_root))
            if include_runtime:
                roots.append((f"runtime/tenants/{tenant_id}", tenant_runtime_root(tenant_id)))
            return roots
        if scope == "all":
            roots = [("shared_knowledge", SHARED_KNOWLEDGE_ROOT)]
            if TENANTS_ROOT.exists():
                for path in sorted(item for item in TENANTS_ROOT.iterdir() if item.is_dir()):
                    roots.append((f"tenants/{path.name}", path))
            fallback_root = default_admin_knowledge_base_root()
            existing_roots = {path.resolve() for _, path in roots}
            if fallback_root.exists() and fallback_root.resolve() not in existing_roots:
                roots.append(("knowledge_bases", fallback_root))
            if include_runtime and runtime_app_root().exists():
                roots.append(("runtime", runtime_app_root()))
            return roots
        raise ValueError(f"unsupported backup scope: {scope}")


def path_is_within(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
        return True
    except ValueError:
        return False


def iter_backup_files(root: Path, *, include_derived: bool) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix in SKIP_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        if any(part in RUNTIME_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not include_derived and any(part in DERIVED_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def normalize_scope(scope: str) -> str:
    value = str(scope or "tenant").strip().lower()
    if value not in {"tenant", "shared", "all"}:
        raise ValueError(f"unsupported backup scope: {scope}")
    return value


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
