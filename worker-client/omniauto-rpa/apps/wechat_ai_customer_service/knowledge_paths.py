"""Path helpers for layered WeChat customer-service knowledge."""

from __future__ import annotations

import os
import re
import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = APP_ROOT / "data"
LEGACY_KNOWLEDGE_BASE_ROOT = DATA_ROOT / "knowledge_bases"
SHARED_KNOWLEDGE_ROOT = DATA_ROOT / "shared_knowledge"
TENANTS_ROOT = DATA_ROOT / "tenants"
DEFAULT_TENANT_ID = "default"
SAFE_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACTIVE_TENANT: ContextVar[str | None] = ContextVar("wechat_active_tenant_id", default=None)


def active_tenant_id(value: str | None = None) -> str:
    tenant_id = (value or _ACTIVE_TENANT.get() or os.getenv("WECHAT_KNOWLEDGE_TENANT") or DEFAULT_TENANT_ID).strip()
    return normalize_tenant_id(tenant_id)


def normalize_tenant_id(value: str | None) -> str:
    tenant_id = (value or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID
    if not SAFE_TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError(f"unsafe tenant id: {tenant_id}")
    return tenant_id


def set_active_tenant_id(tenant_id: str | None) -> Token[str | None]:
    return _ACTIVE_TENANT.set(active_tenant_id(tenant_id))


def reset_active_tenant_id(token: Token[str | None]) -> None:
    _ACTIVE_TENANT.reset(token)


@contextmanager
def tenant_context(tenant_id: str | None) -> Iterator[str]:
    token = set_active_tenant_id(tenant_id)
    try:
        yield active_tenant_id()
    finally:
        reset_active_tenant_id(token)


def tenant_root(tenant_id: str | None = None) -> Path:
    return TENANTS_ROOT / active_tenant_id(tenant_id)


def tenant_metadata_path(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id) / "tenant.json"


def read_tenant_metadata(tenant_id: str | None = None) -> dict:
    path = tenant_metadata_path(tenant_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def tenant_industry_hint(tenant_id: str | None = None) -> str:
    metadata = read_tenant_metadata(tenant_id)
    for key in ("industry_id", "industry"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    sync_meta = metadata.get("sync") if isinstance(metadata.get("sync"), dict) else {}
    shared_meta = sync_meta.get("shared_knowledge") if isinstance(sync_meta.get("shared_knowledge"), dict) else {}
    for key in ("industry_id", "industry"):
        value = str(shared_meta.get(key) or "").strip()
        if value:
            return value
    return ""


def tenant_knowledge_base_root(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id) / "knowledge_bases"


def tenant_product_item_knowledge_root(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id) / "product_item_knowledge"


def tenant_product_master_root(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id) / "product_master"


def tenant_rag_root(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id)


def tenant_rag_sources_root(tenant_id: str | None = None) -> Path:
    return tenant_rag_root(tenant_id) / "rag_sources"


def tenant_rag_chunks_root(tenant_id: str | None = None) -> Path:
    return tenant_rag_root(tenant_id) / "rag_chunks"


def tenant_rag_index_root(tenant_id: str | None = None) -> Path:
    return tenant_rag_root(tenant_id) / "rag_index"


def tenant_rag_cache_root(tenant_id: str | None = None) -> Path:
    return tenant_rag_root(tenant_id) / "rag_cache"


def tenant_review_candidates_root(tenant_id: str | None = None) -> Path:
    tenant_id = active_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        return DATA_ROOT / "review_candidates"
    return tenant_root(tenant_id) / "review_candidates"


def tenant_raw_inbox_root(tenant_id: str | None = None) -> Path:
    tenant_id = active_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        return DATA_ROOT / "raw_inbox"
    return tenant_root(tenant_id) / "raw_inbox"


def tenant_sync_root(tenant_id: str | None = None) -> Path:
    return tenant_root(tenant_id) / "sync"


def shared_proposals_root() -> Path:
    return SHARED_KNOWLEDGE_ROOT / "proposals"


def shared_patches_root() -> Path:
    return SHARED_KNOWLEDGE_ROOT / "patches"


def runtime_app_root() -> Path:
    return APP_ROOT.parents[1] / "runtime" / "apps" / "wechat_ai_customer_service"


def shared_runtime_cache_root() -> Path:
    return runtime_app_root() / "cache" / "shared_knowledge"


def shared_runtime_snapshot_path() -> Path:
    return shared_runtime_cache_root() / "snapshot.json"


def shared_runtime_cache_valid(now: datetime | None = None, tenant_id: str | None = None) -> bool:
    snapshot_path = shared_runtime_snapshot_path()
    if not snapshot_path.exists():
        return False
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("source") or "") != "cloud_official_shared_library":
        return False
    expected_tenant = active_tenant_id(tenant_id) if tenant_id else ""
    snapshot_tenant = str(payload.get("tenant_id") or "").strip()
    if expected_tenant and snapshot_tenant and active_tenant_id(snapshot_tenant) != expected_tenant:
        return False
    expires_at = parse_cloud_time(str(payload.get("expires_at") or ""))
    if expires_at is None:
        cache_policy = payload.get("cache_policy") if isinstance(payload.get("cache_policy"), dict) else {}
        expires_at = parse_cloud_time(str(cache_policy.get("expires_at") or ""))
    if expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires_at > current


def parse_cloud_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def tenant_runtime_root(tenant_id: str | None = None) -> Path:
    return runtime_app_root() / "tenants" / active_tenant_id(tenant_id)


def tenant_runtime_admin_root(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "admin"


def tenant_admin_upload_index_path(tenant_id: str | None = None) -> Path:
    tenant_id = active_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        return runtime_app_root() / "admin" / "uploads_index.json"
    return tenant_runtime_admin_root(tenant_id) / "uploads_index.json"


def tenant_admin_jobs_root(tenant_id: str | None = None) -> Path:
    tenant_id = active_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT_ID:
        return runtime_app_root() / "admin" / "jobs"
    return tenant_runtime_admin_root(tenant_id) / "jobs"


def tenant_runtime_state_root(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "state"


def tenant_runtime_logs_root(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "logs"


def tenant_runtime_backups_root(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "backups"


def default_admin_knowledge_base_root(tenant_id: str | None = None) -> Path:
    root = tenant_knowledge_base_root(tenant_id)
    if (root / "registry.json").exists():
        return root
    return LEGACY_KNOWLEDGE_BASE_ROOT


def runtime_knowledge_roots(tenant_id: str | None = None) -> list[Path]:
    roots: list[Path] = []
    tenant_root_path = tenant_knowledge_base_root(tenant_id)
    if (tenant_root_path / "registry.json").exists():
        roots.append(tenant_root_path)
    elif (LEGACY_KNOWLEDGE_BASE_ROOT / "registry.json").exists():
        roots.append(LEGACY_KNOWLEDGE_BASE_ROOT)
    shared_cache_root = shared_runtime_cache_root()
    if (shared_cache_root / "registry.json").exists() and shared_runtime_cache_valid(tenant_id=tenant_id):
        roots.append(shared_cache_root)
    elif (SHARED_KNOWLEDGE_ROOT / "registry.json").exists():
        roots.append(SHARED_KNOWLEDGE_ROOT)
    return roots
