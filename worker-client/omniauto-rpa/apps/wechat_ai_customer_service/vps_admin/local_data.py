"""Local data inspection helpers used by the VPS admin local simulator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import (
    SHARED_KNOWLEDGE_ROOT,
    active_tenant_id,
    default_admin_knowledge_base_root,
    tenant_knowledge_base_root,
    tenant_product_item_knowledge_root,
    tenant_rag_chunks_root,
    tenant_rag_index_root,
    tenant_rag_sources_root,
    tenant_review_candidates_root,
    tenant_runtime_root,
    tenant_root,
)
from apps.wechat_ai_customer_service.sync.manifest import stable_digest


def build_tenant_data_summary(tenant_id: str | None = None) -> dict[str, Any]:
    tenant = active_tenant_id(tenant_id)
    root = tenant_root(tenant)
    requested_knowledge_root = tenant_knowledge_base_root(tenant)
    knowledge_root = default_admin_knowledge_base_root(tenant)
    product_root = tenant_product_item_knowledge_root(tenant)
    rag_sources = tenant_rag_sources_root(tenant)
    rag_chunks = tenant_rag_chunks_root(tenant)
    rag_index = tenant_rag_index_root(tenant)
    runtime_root = tenant_runtime_root(tenant)
    raw_root = runtime_root / "raw_messages"
    uploads_index = runtime_root / "admin" / "uploads_index.json"
    candidates_root = tenant_review_candidates_root(tenant)
    formal_knowledge = knowledge_base_summary(knowledge_root)
    formal_knowledge["requested_root"] = str(requested_knowledge_root)
    formal_knowledge["effective_root"] = str(knowledge_root)
    formal_knowledge["used_default_fallback"] = knowledge_root.resolve() != requested_knowledge_root.resolve()
    return {
        "tenant_id": tenant,
        "root": str(root),
        "exists": root.exists(),
        "files": tree_summary(root),
        "formal_knowledge": formal_knowledge,
        "product_item_knowledge": {
            "root": str(product_root),
            "product_count": count_directories(product_root),
            "file_count": len(list_files(product_root)),
        },
        "rag": {
            "sources": tree_summary(rag_sources),
            "chunks": tree_summary(rag_chunks),
            "index": tree_summary(rag_index),
        },
        "raw_messages": {
            "root": str(raw_root),
            "message_count": len(read_json(raw_root / "messages.json", default=[])),
            "conversation_count": len(read_json(raw_root / "conversations.json", default=[])),
            "batch_count": len(read_json(raw_root / "batches.json", default=[])),
            "files": tree_summary(raw_root),
        },
        "review_candidates": {
            "root": str(candidates_root),
            "pending_count": count_json_files(candidates_root / "pending"),
            "accepted_count": count_json_files(candidates_root / "accepted"),
            "rejected_count": count_json_files(candidates_root / "rejected"),
            "files": tree_summary(candidates_root),
        },
        "uploads": {
            "root": str(uploads_index),
            "item_count": len(read_json(uploads_index, default=[])),
        },
    }


def build_shared_knowledge_snapshot() -> dict[str, Any]:
    registry = read_json(SHARED_KNOWLEDGE_ROOT / "registry.json", default={"categories": []})
    categories = []
    items = []
    for category in registry.get("categories", []) if isinstance(registry, dict) else []:
        category_id = str(category.get("id") or category.get("category_id") or "")
        category_root = SHARED_KNOWLEDGE_ROOT / str(category.get("path") or category_id)
        category_items = []
        for item_path in sorted((category_root / "items").glob("*.json")) if (category_root / "items").exists() else []:
            item = read_json(item_path, default={})
            data = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else {}
            summary = {
                "item_id": str(item.get("id") or item_path.stem),
                "category_id": category_id,
                "status": str(item.get("status") or ""),
                "title": str(data.get("title") or item.get("id") or item_path.stem),
                "path": str(item_path),
                "digest": stable_digest(json.dumps(item, ensure_ascii=False, sort_keys=True), 16),
                "payload": item,
            }
            category_items.append(summary)
            items.append(summary)
        categories.append(
            {
                "category_id": category_id,
                "name": str(category.get("name") or category_id),
                "kind": str(category.get("kind") or "shared"),
                "enabled": category.get("enabled", True) is not False,
                "item_count": len(category_items),
                "items": category_items,
            }
        )
    return {
        "root": str(SHARED_KNOWLEDGE_ROOT),
        "exists": SHARED_KNOWLEDGE_ROOT.exists(),
        "registry": registry,
        "categories": categories,
        "items": items,
        "file_summary": tree_summary(SHARED_KNOWLEDGE_ROOT),
        "structure": {
            "shared_root": str(SHARED_KNOWLEDGE_ROOT),
            "tenant_formal_root_example": str(tenant_knowledge_base_root("default")),
            "product_item_root_example": str(tenant_product_item_knowledge_root("default")),
            "separated": True,
            "explanation": "共享公共知识在 data/shared_knowledge；客户专业知识在 data/tenants/<tenant>/knowledge_bases；商品专属知识在 data/tenants/<tenant>/product_item_knowledge。",
        },
    }


def knowledge_base_summary(root: Path) -> dict[str, Any]:
    registry = read_json(root / "registry.json", default={"categories": []})
    categories = []
    total_items = 0
    for category in registry.get("categories", []) if isinstance(registry, dict) else []:
        category_id = str(category.get("id") or category.get("category_id") or "")
        category_root = root / str(category.get("path") or category_id)
        item_count = len(list((category_root / "items").glob("*.json"))) if (category_root / "items").exists() else 0
        total_items += item_count
        categories.append(
            {
                "category_id": category_id,
                "name": str(category.get("name") or category_id),
                "kind": str(category.get("kind") or "tenant"),
                "item_count": item_count,
            }
        )
    return {
        "root": str(root),
        "exists": root.exists(),
        "category_count": len(categories),
        "item_count": total_items,
        "categories": categories,
        "files": tree_summary(root),
    }


def tree_summary(root: Path) -> dict[str, Any]:
    files = list_files(root)
    return {
        "root": str(root),
        "exists": root.exists(),
        "file_count": len(files),
        "json_file_count": sum(1 for item in files if item.suffix.lower() == ".json"),
        "bytes": sum(item.stat().st_size for item in files if item.exists()),
    }


def list_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts)


def count_directories(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for item in root.iterdir() if item.is_dir())


def count_json_files(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for item in root.glob("*.json") if item.is_file())


def read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
