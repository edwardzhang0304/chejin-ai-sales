"""Version snapshots for formal knowledge files."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from apps.wechat_ai_customer_service.knowledge_paths import (
    SHARED_KNOWLEDGE_ROOT,
    TENANTS_ROOT,
    active_tenant_id,
    default_admin_knowledge_base_root,
)
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config
from apps.wechat_ai_customer_service.sync import BackupService
from apps.wechat_ai_customer_service.exports.readable_export import build_customer_readable_workbook


APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT.parents[1]
STRUCTURED_ROOT = APP_ROOT / "data" / "structured"
KNOWLEDGE_BASE_ROOT = default_admin_knowledge_base_root()
VERSIONS_ROOT = APP_ROOT / "data" / "versions"
VERSION_RETENTION_LIMIT = 20


class VersionStore:
    def create_snapshot(self, reason: str, metadata: dict[str, Any] | None = None, *, prune: bool = True) -> dict[str, Any]:
        version_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        version_root = VERSIONS_ROOT / version_id
        structured_target = version_root / "structured"
        knowledge_target = version_root / "knowledge_bases"
        shared_target = version_root / "shared_knowledge"
        tenants_target = version_root / "tenants"
        version_root.mkdir(parents=True, exist_ok=False)
        if STRUCTURED_ROOT.exists():
            shutil.copytree(STRUCTURED_ROOT, structured_target)
        if KNOWLEDGE_BASE_ROOT.exists():
            shutil.copytree(KNOWLEDGE_BASE_ROOT, knowledge_target)
        if SHARED_KNOWLEDGE_ROOT.exists():
            shutil.copytree(SHARED_KNOWLEDGE_ROOT, shared_target)
        if TENANTS_ROOT.exists():
            shutil.copytree(TENANTS_ROOT, tenants_target)
        payload = {
            "version_id": version_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "metadata": metadata or {},
            "structured_path": str(structured_target),
            "knowledge_base_path": str(knowledge_target),
            "shared_knowledge_path": str(shared_target),
            "tenants_path": str(tenants_target),
        }
        payload.update(build_download_bundle(version_id, version_root))
        (version_root / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        db = postgres_store()
        if db:
            db.upsert_version(active_tenant_id(), payload)
        append_audit("version_created", {"version_id": version_id, "reason": reason})
        if prune:
            self.prune_versions()
        return payload

    def list_versions(self) -> list[dict[str, Any]]:
        self.prune_versions()
        db = postgres_store()
        if db:
            versions = db.list_versions(active_tenant_id())
            if versions:
                return versions[:VERSION_RETENTION_LIMIT]
        if not VERSIONS_ROOT.exists():
            return []
        return [item["metadata"] for item in self._file_versions()[:VERSION_RETENTION_LIMIT]]

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        db = postgres_store()
        if db:
            version = db.get_version(active_tenant_id(), version_id)
            if version:
                return version
        metadata_path = VERSIONS_ROOT / version_id / "metadata.json"
        if not metadata_path.exists():
            return None
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def download_path(self, version_id: str) -> Path:
        metadata = self.get_version(version_id)
        if not metadata:
            raise FileNotFoundError(version_id)
        package_path = Path(str(metadata.get("download_package_path") or ""))
        if not package_path.exists() or not package_path.is_file():
            version_root = VERSIONS_ROOT / version_id
            metadata.update(build_download_bundle(version_id, version_root))
            (version_root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            package_path = Path(str(metadata.get("download_package_path") or ""))
        if not package_path.exists() or not package_path.is_file():
            raise FileNotFoundError(version_id)
        return package_path

    def rollback(self, version_id: str) -> dict[str, Any]:
        version_root = VERSIONS_ROOT / version_id
        structured_source = version_root / "structured"
        knowledge_source = version_root / "knowledge_bases"
        shared_source = version_root / "shared_knowledge"
        tenants_source = version_root / "tenants"
        if not structured_source.exists() and not knowledge_source.exists() and not shared_source.exists() and not tenants_source.exists():
            return {"ok": False, "message": f"version not found: {version_id}"}
        rollback_snapshot = self.create_snapshot("before rollback", {"rollback_to": version_id}, prune=False)
        if structured_source.exists():
            STRUCTURED_ROOT.mkdir(parents=True, exist_ok=True)
            for source_file in structured_source.glob("*.json"):
                target = STRUCTURED_ROOT / source_file.name
                shutil.copy2(source_file, target)
        if shared_source.exists():
            replace_tree(shared_source, SHARED_KNOWLEDGE_ROOT)
        if tenants_source.exists():
            preserved_learning_packs = snapshot_learning_pack_artifacts()
            replace_tree(tenants_source, TENANTS_ROOT)
            restore_learning_pack_artifacts(preserved_learning_packs)
        if knowledge_source.exists():
            replace_tree(knowledge_source, KNOWLEDGE_BASE_ROOT)
        refresh_postgres_formal_knowledge()
        append_audit("rollback_applied", {"version_id": version_id, "rollback_snapshot": rollback_snapshot["version_id"]})
        self.prune_versions()
        return {"ok": True, "message": "rollback applied", "version_id": version_id, "backup": rollback_snapshot}

    def prune_versions(self, limit: int = VERSION_RETENTION_LIMIT) -> dict[str, Any]:
        limit = max(1, int(limit))
        removed_ids: list[str] = []

        db = postgres_store()
        if db:
            db_versions = db.list_versions(active_tenant_id())
            if db_versions:
                for item in db_versions[limit:]:
                    version_id = str(item.get("version_id") or "")
                    if not version_id:
                        continue
                    db.delete_version(active_tenant_id(), version_id)
                    removed_ids.append(version_id)
                removed_set = set(removed_ids)
                for item in self._file_versions():
                    version_id = str(item["metadata"].get("version_id") or item["path"].name)
                    if version_id in removed_set:
                        remove_version_directory(item["path"])
                if removed_ids:
                    append_audit("versions_pruned", {"limit": limit, "removed_version_ids": removed_ids})
                return {"ok": True, "limit": limit, "removed": removed_ids}

        for item in self._file_versions()[limit:]:
            version_id = str(item["metadata"].get("version_id") or item["path"].name)
            remove_version_directory(item["path"])
            if version_id:
                removed_ids.append(version_id)

        if removed_ids:
            append_audit("versions_pruned", {"limit": limit, "removed_version_ids": removed_ids})
        return {"ok": True, "limit": limit, "removed": removed_ids}

    def _file_versions(self) -> list[dict[str, Any]]:
        if not VERSIONS_ROOT.exists():
            return []
        versions: list[dict[str, Any]] = []
        for path in VERSIONS_ROOT.iterdir():
            if not path.is_dir():
                continue
            metadata_path = path / "metadata.json"
            if not metadata_path.exists():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            version_id = str(metadata.get("version_id") or path.name)
            created_at = str(metadata.get("created_at") or version_id)
            versions.append({"path": path, "metadata": metadata, "sort_key": (created_at, version_id)})
        return sorted(versions, key=lambda item: item["sort_key"], reverse=True)


def remove_version_directory(path: Path) -> None:
    target = path.resolve()
    root = VERSIONS_ROOT.resolve()
    if root not in target.parents or not target.exists():
        return
    shutil.rmtree(target)


def replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        for child in target.iterdir():
            if child.is_dir():
                remove_tree_best_effort(child)
            else:
                unlink_best_effort(child)
    else:
        target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def remove_tree_best_effort(path: Path) -> None:
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        pass
    make_writable(path)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        return


def unlink_best_effort(path: Path) -> None:
    try:
        path.unlink()
        return
    except PermissionError:
        pass
    make_writable(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def make_writable(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                os.chmod(child, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                continue


def snapshot_learning_pack_artifacts() -> Path | None:
    if not TENANTS_ROOT.exists():
        return None
    has_any = False
    preserved_root = Path(tempfile.mkdtemp(prefix="wechat_preserve_learning_packs_"))
    for tenant_dir in TENANTS_ROOT.iterdir():
        if not tenant_dir.is_dir():
            continue
        learning_pack_root = tenant_dir / "learning_packs"
        if not learning_pack_root.exists():
            continue
        target = preserved_root / tenant_dir.name / "learning_packs"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(learning_pack_root, target, dirs_exist_ok=True)
        has_any = True
    if has_any:
        return preserved_root
    shutil.rmtree(preserved_root, ignore_errors=True)
    return None


def restore_learning_pack_artifacts(preserved_root: Path | None) -> None:
    if not preserved_root or not preserved_root.exists():
        return
    try:
        for tenant_dir in preserved_root.iterdir():
            if not tenant_dir.is_dir():
                continue
            source = tenant_dir / "learning_packs"
            if not source.exists():
                continue
            target = TENANTS_ROOT / tenant_dir.name / "learning_packs"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
    finally:
        shutil.rmtree(preserved_root, ignore_errors=True)


def refresh_postgres_formal_knowledge() -> None:
    db = postgres_store()
    if not db:
        return
    tenant_id = active_tenant_id()
    from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import product_scoped_category_records
    from apps.wechat_ai_customer_service.product_master import PRODUCT_MASTER_DB_LAYER, product_master_category_record
    from apps.wechat_ai_customer_service.workflows.migrate_file_storage_to_postgres import collect_file_storage

    plan = collect_file_storage(tenant_id)
    db.execute(f"DELETE FROM {db.schema}.knowledge_items WHERE tenant_id = %s", [tenant_id])
    db.execute(f"DELETE FROM {db.schema}.knowledge_categories WHERE tenant_id = %s", [tenant_id])
    db.upsert_tenant(plan["tenant"])
    for category in plan["shared_categories"]:
        db.upsert_category(tenant_id, "shared", category)
    for category in plan["tenant_categories"]:
        db.upsert_category(tenant_id, "tenant", category)
    db.upsert_category(tenant_id, PRODUCT_MASTER_DB_LAYER, product_master_category_record())
    for category in product_scoped_category_records():
        db.upsert_category(tenant_id, "tenant_product", category)
    for item in plan["knowledge_items"]:
        db.upsert_knowledge_item(
            tenant_id,
            item["layer"],
            item["category_id"],
            item["payload"],
            product_id=item.get("product_id", ""),
        )


def build_download_bundle(version_id: str, version_root: Path) -> dict[str, Any]:
    tenant_id = active_tenant_id()
    bundle_root = version_root / "download"
    bundle_root.mkdir(parents=True, exist_ok=True)
    backup = BackupService(output_root=bundle_root).build_backup(
        scope="tenant",
        tenant_id=tenant_id,
        include_derived=True,
        include_runtime=True,
    )
    raw_package = Path(str(backup.get("package_path") or ""))
    readable_record = {
        "package_id": f"local_{version_id}",
        "account_username": tenant_id,
        "tenant_id": tenant_id,
        "backup_id": backup.get("backup_id"),
        "package_path": str(raw_package),
        "bytes": backup.get("bytes"),
        "manifest": backup.get("manifest"),
    }
    readable_path = build_customer_readable_workbook(readable_record, raw_package, output_root=bundle_root / "readable")
    bundle_path = bundle_root / f"{version_id}_complete_backup.zip"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(raw_package, "原始数据备份.zip")
        archive.write(readable_path, "可读知识表.xlsx")
        archive.writestr("备份说明.json", json.dumps(readable_record, ensure_ascii=False, indent=2))
    return {
        "raw_backup_package_path": str(raw_package),
        "readable_export_path": str(readable_path),
        "download_package_path": str(bundle_path),
        "download_package_bytes": bundle_path.stat().st_size,
    }


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
