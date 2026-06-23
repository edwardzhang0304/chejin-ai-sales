"""Item-level storage for classified knowledge bases."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, default_admin_knowledge_base_root, tenant_product_item_knowledge_root
from apps.wechat_ai_customer_service.product_master import (
    PRODUCT_MASTER_CATEGORY_ID,
    ProductMasterStore,
    normalize_product_item,
    validate_product_item,
)
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import (
    PRODUCT_SCOPED_KINDS,
    PRODUCT_SCOPED_RESOLVERS,
    PRODUCT_SCOPED_SCHEMAS,
)


SAFE_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PRODUCT_SCOPED_CATEGORY_TO_KIND = {category_id: kind for kind, category_id in PRODUCT_SCOPED_KINDS.items()}


class KnowledgeBaseStore:
    def __init__(
        self,
        registry: KnowledgeRegistry | None = None,
        schema_manager: KnowledgeSchemaManager | None = None,
    ) -> None:
        self.registry = registry or KnowledgeRegistry()
        self.schema_manager = schema_manager or KnowledgeSchemaManager(self.registry)
        self.default_root_mode = self.registry.root.resolve() == default_admin_knowledge_base_root().resolve()
        self.product_master = ProductMasterStore()

    def list_items(self, category_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return self.product_master.list_items(include_archived=include_archived)
        db = postgres_store() if self.default_root_mode else None
        if db:
            layer = "tenant_product" if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND else "tenant"
            items = db.list_knowledge_items(active_tenant_id(), layer=layer, category_id=category_id, include_archived=include_archived)
            if items:
                return items
        if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
            return self.list_product_scoped_items(category_id, include_archived=include_archived)
        items_root = self.items_root(category_id)
        if not items_root.exists():
            return []
        items = []
        for path in sorted(items_root.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            if not include_archived and item.get("status") == "archived":
                continue
            items.append(item)
        return items

    def get_item(self, category_id: str, item_id: str) -> dict[str, Any] | None:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return self.product_master.get_item(item_id, include_archived=False)
        db = postgres_store() if self.default_root_mode else None
        if db:
            if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
                for item in db.list_knowledge_items(active_tenant_id(), layer="tenant_product", category_id=category_id, include_archived=True):
                    if str(item.get("id") or "") == item_id:
                        return None if item.get("status") == "archived" else item
            else:
                item = db.get_knowledge_item(active_tenant_id(), layer="tenant", category_id=category_id, item_id=item_id)
                if item:
                    return None if item.get("status") == "archived" else item
        if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
            return self.get_product_scoped_item(category_id, item_id)
        path = self.item_path(category_id, item_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_item(self, category_id: str, item: dict[str, Any]) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return self.product_master.save_item(item)
        item_id = str(item.get("id") or "")
        validate_item_id(item_id)
        validation = self.validate_item(category_id, item)
        if not validation["ok"]:
            return validation
        normalized = normalize_item(category_id, item)
        db = postgres_store() if self.default_root_mode else None
        config = load_storage_config()
        if db:
            layer = "tenant_product" if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND else "tenant"
            product_id = ""
            if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
                product_id = str((normalized.get("data") or {}).get("product_id") or "")
            db.upsert_knowledge_item(active_tenant_id(), layer, category_id, normalized, product_id=product_id)
            if not config.mirror_files:
                return {"ok": True, "item": normalized}
        path = self.product_scoped_item_path(category_id, normalized) if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND else self.item_path(category_id, item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, normalized)
        return {"ok": True, "item": normalized}

    def archive_item(self, category_id: str, item_id: str) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return self.product_master.archive_item(item_id)
        item = self.get_item(category_id, item_id)
        if not item:
            return {"ok": False, "message": f"item not found: {category_id}/{item_id}"}
        item["status"] = "archived"
        item.setdefault("metadata", {})["updated_at"] = now()
        return self.save_item(category_id, item)

    def validate_item(self, category_id: str, item: dict[str, Any]) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return validate_product_item(normalize_product_item(item), self.product_master.load_schema())
        problems = []
        if item.get("category_id") != category_id:
            problems.append(f"item category_id must be {category_id}")
        item_id = str(item.get("id") or "")
        if not item_id:
            problems.append("item id is required")
        elif not SAFE_ITEM_ID_RE.fullmatch(item_id):
            problems.append(f"unsafe item id: {item_id}")
        schema = self.schema_manager.load_schema(category_id)
        fields = {field["id"]: field for field in schema.get("fields", []) or []}
        data = item.get("data", {}) or {}
        for field_id, field in fields.items():
            if field.get("required") and is_empty(data.get(field_id)):
                problems.append(f"required field is missing: {field_id}")
        if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
            product_id = str(data.get("product_id") or "")
            if not product_id:
                problems.append("required field is missing: product_id")
            elif not SAFE_ITEM_ID_RE.fullmatch(product_id):
                problems.append(f"unsafe product id: {product_id}")
        return {"ok": not problems, "problems": problems}

    def items_root(self, category_id: str) -> Path:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.default_root_mode:
            return self.product_master.items_dir
        if category_id in PRODUCT_SCOPED_CATEGORY_TO_KIND:
            return tenant_product_item_knowledge_root()
        return self.registry.category_root(category_id) / "items"

    def item_path(self, category_id: str, item_id: str) -> Path:
        validate_item_id(item_id)
        path = self.items_root(category_id) / f"{item_id}.json"
        resolved = path.resolve()
        root = self.items_root(category_id).resolve()
        if root not in resolved.parents:
            raise ValueError(f"item path escapes category root: {item_id}")
        return resolved

    def list_product_scoped_items(self, category_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        kind = PRODUCT_SCOPED_CATEGORY_TO_KIND[category_id]
        root = tenant_product_item_knowledge_root()
        if not root.exists():
            return []
        items: list[dict[str, Any]] = []
        for product_root in sorted(path for path in root.iterdir() if path.is_dir()):
            items_root = product_root / kind
            if not items_root.exists():
                continue
            for path in sorted(items_root.glob("*.json")):
                item = json.loads(path.read_text(encoding="utf-8"))
                if not include_archived and item.get("status") == "archived":
                    continue
                item.setdefault("category_id", category_id)
                item.setdefault("data", {})
                item["data"].setdefault("product_id", product_root.name)
                items.append(item)
        return items

    def get_product_scoped_item(self, category_id: str, item_id: str) -> dict[str, Any] | None:
        validate_item_id(item_id)
        for item in self.list_product_scoped_items(category_id, include_archived=True):
            if str(item.get("id") or "") == item_id:
                return None if item.get("status") == "archived" else item
        return None

    def product_scoped_item_path(self, category_id: str, item: dict[str, Any]) -> Path:
        kind = PRODUCT_SCOPED_CATEGORY_TO_KIND[category_id]
        item_id = str(item.get("id") or "")
        validate_item_id(item_id)
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        product_id = str(data.get("product_id") or "")
        validate_item_id(product_id)
        root = tenant_product_item_knowledge_root().resolve()
        path = (root / product_id / kind / f"{item_id}.json").resolve()
        if root not in path.parents:
            raise ValueError(f"product-scoped item path escapes root: {product_id}/{item_id}")
        return path


def product_scoped_category_records() -> list[dict[str, Any]]:
    records = []
    for category_id, schema in PRODUCT_SCOPED_SCHEMAS.items():
        records.append(
            {
                "id": category_id,
                "name": schema.get("display_name") or category_id,
                "description": "Stored under each product folder and only used when that product is in context.",
                "path": f"product_item_knowledge/*/{PRODUCT_SCOPED_CATEGORY_TO_KIND[category_id]}",
                "enabled": True,
                "participates_in_reply": True,
                "participates_in_learning": True,
                "participates_in_diagnostics": True,
                "scope": "tenant_product",
                "sort_order": 70,
            }
        )
    return records


def validate_item_id(item_id: str) -> None:
    if not SAFE_ITEM_ID_RE.fullmatch(item_id):
        raise ValueError(f"unsafe item id: {item_id}")


def normalize_item(category_id: str, item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["category_id"] = category_id
    result.setdefault("schema_version", 1)
    result.setdefault("status", "active")
    result.setdefault("source", {"type": "manual"})
    result.setdefault("data", {})
    result.setdefault("runtime", {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"})
    metadata = result.setdefault("metadata", {})
    metadata.setdefault("created_at", now())
    metadata["updated_at"] = now()
    metadata.setdefault("created_by", "admin")
    metadata.setdefault("updated_by", "admin")
    return result


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path: Path, content: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
