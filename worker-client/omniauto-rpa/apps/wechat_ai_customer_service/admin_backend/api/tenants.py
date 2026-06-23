"""Tenant discovery and status APIs."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..auth_context import current_auth_context
from apps.wechat_ai_customer_service.auth import assert_allowed
from apps.wechat_ai_customer_service.knowledge_paths import (
    DEFAULT_TENANT_ID,
    LEGACY_KNOWLEDGE_BASE_ROOT,
    TENANTS_ROOT,
    active_tenant_id,
    normalize_tenant_id,
    tenant_knowledge_base_root,
    tenant_metadata_path,
    tenant_root,
)
from apps.wechat_ai_customer_service.industry_catalog import default_industry_for_tenant, normalize_industry_id


router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("")
def list_tenants(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    items = []
    for path in sorted(TENANTS_ROOT.iterdir()) if TENANTS_ROOT.exists() else []:
        if not path.is_dir():
            continue
        tenant_id = path.name
        if not context.user.has_tenant(tenant_id):
            continue
        items.append(tenant_summary(tenant_id))
    return {"ok": True, "active_tenant_id": context.tenant_id, "items": items}


@router.post("")
def create_tenant(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    assert_allowed(context, resource="settings", action="write", tenant_id=context.tenant_id)
    tenant_id = normalize_tenant_id(str(payload.get("tenant_id") or payload.get("id") or ""))
    if tenant_root(tenant_id).exists():
        raise HTTPException(status_code=409, detail=f"tenant already exists: {tenant_id}")
    industry_id = normalize_industry_id(payload.get("industry_id") or payload.get("industry") or default_industry_for_tenant(tenant_id))
    metadata = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "display_name": str(payload.get("display_name") or payload.get("name") or tenant_id),
        "industry_id": industry_id,
        "knowledge_base_root": "knowledge_bases",
        "product_item_knowledge_root": "product_item_knowledge",
        "created_at": now(),
        "sync": {"private_backup": {"enabled": False, "schedule": "manual"}},
    }
    root = tenant_root(tenant_id)
    (root / "knowledge_bases").mkdir(parents=True, exist_ok=True)
    (root / "product_item_knowledge").mkdir(parents=True, exist_ok=True)
    (root / "rag_sources").mkdir(parents=True, exist_ok=True)
    (root / "rag_experience").mkdir(parents=True, exist_ok=True)
    initialize_tenant_knowledge_base(tenant_id)
    write_json(tenant_metadata_path(tenant_id), metadata)
    return {"ok": True, "item": tenant_summary(tenant_id)}


@router.get("/{tenant_id}/status")
def tenant_status(request: Request, tenant_id: str) -> dict[str, Any]:
    context = current_auth_context(request)
    tenant_id = active_tenant_id(tenant_id)
    assert_allowed(context, resource="settings", action="read", tenant_id=tenant_id)
    return {"ok": True, "item": tenant_summary(tenant_id)}


def tenant_summary(tenant_id: str) -> dict[str, Any]:
    root = tenant_root(tenant_id)
    metadata = read_json(tenant_metadata_path(tenant_id), default={"tenant_id": tenant_id, "display_name": tenant_id})
    return {
        "tenant_id": tenant_id,
        "display_name": metadata.get("display_name") or metadata.get("name") or tenant_id,
        "industry_id": normalize_industry_id(
            metadata.get("industry_id") or metadata.get("industry") or default_industry_for_tenant(tenant_id)
        ),
        "root": str(root),
        "exists": root.exists(),
        "knowledge_base_exists": (root / "knowledge_bases").exists(),
        "rag_sources_exists": (root / "rag_sources").exists(),
        "sync": metadata.get("sync") if isinstance(metadata.get("sync"), dict) else {},
        "updated_at": file_mtime(tenant_metadata_path(tenant_id)),
    }


def initialize_tenant_knowledge_base(tenant_id: str) -> None:
    target = tenant_knowledge_base_root(tenant_id)
    source = tenant_knowledge_base_root(DEFAULT_TENANT_ID)
    if not (source / "registry.json").exists():
        source = LEGACY_KNOWLEDGE_BASE_ROOT
    registry = read_json(source / "registry.json", default={})
    if not registry:
        return
    write_json(target / "registry.json", registry)
    for category in registry.get("categories", []) or []:
        category_path = str(category.get("path") or category.get("id") or "").strip()
        if not category_path:
            continue
        source_category = source / category_path
        target_category = target / category_path
        target_items = target_category / "items"
        target_items.mkdir(parents=True, exist_ok=True)
        (target_items / ".gitkeep").write_text("\n", encoding="utf-8")
        for filename in ("schema.json", "resolver.json"):
            source_file = source_category / filename
            target_file = target_category / filename
            if source_file.exists() and not target_file.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target_file)


def read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
