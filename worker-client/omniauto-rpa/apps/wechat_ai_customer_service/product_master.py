"""Tenant product-master storage.

Product master data is the authoritative source for product facts such as
price, inventory, SKU, specs, and availability. It is intentionally separate
from formal business knowledge so RAG/candidate promotion cannot mutate product
facts through the generic knowledge path.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import (
    LEGACY_KNOWLEDGE_BASE_ROOT,
    active_tenant_id,
    tenant_knowledge_base_root,
    tenant_product_master_root,
)
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


PRODUCT_MASTER_CATEGORY_ID = "products"
PRODUCT_MASTER_DB_LAYER = "product_master"
LEGACY_PRODUCT_DB_LAYER = "tenant"
SAFE_PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


DEFAULT_PRODUCT_MASTER_SCHEMA: dict[str, Any] = {
    "schema_version": 1,
    "category_id": PRODUCT_MASTER_CATEGORY_ID,
    "display_name": "商品主数据",
    "description": "商品事实权威源：商品、车型、价格、库存、规格、物流、售后和禁用承诺。",
    "item_title_field": "name",
    "item_subtitle_field": "sku",
    "fields": [
        {"id": "name", "label": "商品名称", "type": "short_text", "required": True, "searchable": True, "form_order": 10},
        {"id": "sku", "label": "型号/SKU", "type": "short_text", "required": False, "searchable": True, "form_order": 20},
        {"id": "category", "label": "商品类目", "type": "short_text", "required": False, "searchable": True, "form_order": 30},
        {"id": "aliases", "label": "客户常用叫法", "type": "tags", "required": False, "searchable": True, "form_order": 40},
        {"id": "specs", "label": "规格参数", "type": "long_text", "required": False, "searchable": True, "form_order": 50},
        {"id": "price", "label": "基础价格", "type": "money", "required": False, "searchable": False, "form_order": 60},
        {"id": "unit", "label": "计价单位", "type": "short_text", "required": False, "searchable": False, "form_order": 70},
        {
            "id": "price_tiers",
            "label": "阶梯价格",
            "type": "table",
            "required": False,
            "form_order": 80,
            "columns": [
                {"id": "min_quantity", "label": "起订量", "type": "number"},
                {"id": "unit_price", "label": "单价", "type": "money"},
            ],
        },
        {"id": "inventory", "label": "库存", "type": "number", "required": False, "form_order": 90},
        {"id": "shipping_policy", "label": "发货/物流", "type": "long_text", "required": False, "searchable": True, "form_order": 100},
        {"id": "warranty_policy", "label": "售后/保修", "type": "long_text", "required": False, "searchable": True, "form_order": 110},
        {"id": "reply_templates", "label": "标准回复模板", "type": "object", "required": False, "form_order": 120},
        {"id": "risk_rules", "label": "风险与禁用承诺", "type": "tags", "required": False, "searchable": True, "form_order": 130},
        {"id": "additional_details", "label": "补充信息", "type": "object", "required": False, "searchable": True, "form_order": 140},
    ],
    "validation": {
        "unique_fields": ["id"],
        "unique_tag_fields": ["aliases"],
        "required_for_auto_reply": ["name"],
    },
}

DEFAULT_PRODUCT_MASTER_RESOLVER: dict[str, Any] = {
    "schema_version": 1,
    "category_id": PRODUCT_MASTER_CATEGORY_ID,
    "match_fields": ["name", "sku", "category", "aliases", "specs", "additional_details"],
    "intent_fields": ["reply_templates", "risk_rules", "shipping_policy", "warranty_policy", "additional_details"],
    "risk_fields": ["risk_rules"],
    "reply_fields": [
        "name",
        "sku",
        "category",
        "price",
        "unit",
        "price_tiers",
        "inventory",
        "shipping_policy",
        "warranty_policy",
        "reply_templates",
        "additional_details",
    ],
    "minimum_confidence": 0.45,
    "default_action": "answer_from_product_master",
}


def product_master_category_record() -> dict[str, Any]:
    return {
        "id": PRODUCT_MASTER_CATEGORY_ID,
        "name": "商品主数据",
        "kind": "product_master",
        "path": "product_master",
        "enabled": True,
        "participates_in_reply": True,
        "participates_in_learning": False,
        "participates_in_diagnostics": True,
        "scope": "product_master",
        "authority": "manual_product_master_only",
        "sort_order": 10,
    }


class ProductMasterStore:
    """Read/write facade for product master data with legacy read fallback."""

    def __init__(self, *, tenant_id: str | None = None, root: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.root = (root or tenant_product_master_root(self.tenant_id)).resolve()
        self.items_dir = self.root / "items"

    @property
    def schema_path(self) -> Path:
        return self.root / "schema.json"

    @property
    def resolver_path(self) -> Path:
        return self.root / "resolver.json"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def ensure_structure(self) -> None:
        self.items_dir.mkdir(parents=True, exist_ok=True)
        changed = False
        if not self.schema_path.exists():
            write_json(self.schema_path, self._legacy_schema() or DEFAULT_PRODUCT_MASTER_SCHEMA)
            changed = True
        if not self.resolver_path.exists():
            write_json(self.resolver_path, self._legacy_resolver() or DEFAULT_PRODUCT_MASTER_RESOLVER)
            changed = True
        if changed or self._manifest_needs_refresh():
            self.write_manifest()

    def load_schema(self) -> dict[str, Any]:
        self.ensure_structure()
        return read_json(self.schema_path, default=DEFAULT_PRODUCT_MASTER_SCHEMA)

    def load_resolver(self) -> dict[str, Any]:
        self.ensure_structure()
        return read_json(self.resolver_path, default=DEFAULT_PRODUCT_MASTER_RESOLVER)

    def list_items(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        db_items = self._list_db_items(include_archived=include_archived)
        if db_items:
            return db_items
        self.ensure_structure()
        items = self._list_file_items(self.items_dir, include_archived=include_archived)
        if items:
            return items
        return self._legacy_items(include_archived=include_archived)

    def get_item(self, product_id: str, *, include_archived: bool = False) -> dict[str, Any] | None:
        validate_product_id(product_id)
        for item in self.list_items(include_archived=include_archived):
            if str(item.get("id") or "") == product_id:
                if not include_archived and str(item.get("status") or "active") == "archived":
                    return None
                return item
        return None

    def save_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_product_item(item)
        validation = validate_product_item(normalized, self.load_schema())
        if not validation["ok"]:
            return validation

        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            db.upsert_knowledge_item(self.tenant_id, PRODUCT_MASTER_DB_LAYER, PRODUCT_MASTER_CATEGORY_ID, normalized)
            if not config.mirror_files:
                return {"ok": True, "item": normalized}

        self.ensure_structure()
        path = self.item_path(str(normalized.get("id") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, normalized)
        self.write_manifest()
        return {"ok": True, "item": normalized}

    def archive_item(self, product_id: str) -> dict[str, Any]:
        item = self.get_item(product_id, include_archived=True)
        if not item:
            return {"ok": False, "message": f"item not found: {PRODUCT_MASTER_CATEGORY_ID}/{product_id}"}
        item["status"] = "archived"
        item.setdefault("metadata", {})["updated_at"] = now()
        return self.save_item(item)

    def migrate_from_legacy(self, *, overwrite: bool = False) -> dict[str, Any]:
        self.ensure_structure()
        copied = []
        skipped = []
        for item in self._legacy_items(include_archived=True):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            target = self.item_path(item_id)
            if target.exists() and not overwrite:
                skipped.append(item_id)
                continue
            item = normalize_product_item(item)
            item.setdefault("source", {})["legacy_migrated_from"] = "knowledge_bases/products"
            result = self.save_item(item)
            if result.get("ok"):
                copied.append(item_id)
            else:
                skipped.append(item_id)
        self.write_manifest(extra={"legacy_migrated_count": len(copied), "legacy_skipped_count": len(skipped)})
        return {"ok": True, "copied": copied, "skipped": skipped, "count": len(copied)}

    def item_path(self, product_id: str) -> Path:
        validate_product_id(product_id)
        root = self.items_dir.resolve()
        path = (root / f"{product_id}.json").resolve()
        if root not in path.parents:
            raise ValueError(f"product path escapes product master root: {product_id}")
        return path

    def write_manifest(self, extra: dict[str, Any] | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "authority": "product_master",
            "category_id": PRODUCT_MASTER_CATEGORY_ID,
            "tenant_id": self.tenant_id,
            "updated_at": now(),
            "items_path": "items",
            "compatibility": {
                "legacy_read_fallback": "knowledge_bases/products",
                "new_writes_to_legacy": False,
            },
        }
        if extra:
            payload.update(extra)
        write_json(self.manifest_path, payload)

    def _manifest_needs_refresh(self) -> bool:
        payload = read_json(self.manifest_path, default=None)
        if not isinstance(payload, dict):
            return True
        compatibility = payload.get("compatibility") if isinstance(payload.get("compatibility"), dict) else {}
        expected = {
            "schema_version": 1,
            "authority": "product_master",
            "category_id": PRODUCT_MASTER_CATEGORY_ID,
            "tenant_id": self.tenant_id,
            "items_path": "items",
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                return True
        return (
            compatibility.get("legacy_read_fallback") != "knowledge_bases/products"
            or compatibility.get("new_writes_to_legacy") is not False
        )

    def _list_db_items(self, *, include_archived: bool) -> list[dict[str, Any]]:
        db = postgres_store(self.tenant_id)
        if not db:
            return []
        items = db.list_knowledge_items(
            self.tenant_id,
            layer=PRODUCT_MASTER_DB_LAYER,
            category_id=PRODUCT_MASTER_CATEGORY_ID,
            include_archived=include_archived,
        )
        if items:
            return items
        return db.list_knowledge_items(
            self.tenant_id,
            layer=LEGACY_PRODUCT_DB_LAYER,
            category_id=PRODUCT_MASTER_CATEGORY_ID,
            include_archived=include_archived,
        )

    def _legacy_root_candidates(self) -> list[Path]:
        tenant_root = tenant_knowledge_base_root(self.tenant_id) / PRODUCT_MASTER_CATEGORY_ID
        candidates = [tenant_root]
        if self.tenant_id == "default":
            candidates.append(LEGACY_KNOWLEDGE_BASE_ROOT / PRODUCT_MASTER_CATEGORY_ID)
        return candidates

    def _legacy_schema(self) -> dict[str, Any] | None:
        for root in self._legacy_root_candidates():
            payload = read_json(root / "schema.json", default=None)
            if isinstance(payload, dict):
                return payload
        return None

    def _legacy_resolver(self) -> dict[str, Any] | None:
        for root in self._legacy_root_candidates():
            payload = read_json(root / "resolver.json", default=None)
            if isinstance(payload, dict):
                return payload
        return None

    def _legacy_items(self, *, include_archived: bool) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in self._legacy_root_candidates():
            for item in self._list_file_items(root / "items", include_archived=include_archived):
                item_id = str(item.get("id") or "")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    items.append(item)
        return items

    def _list_file_items(self, root: Path, *, include_archived: bool) -> list[dict[str, Any]]:
        if not root.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            payload = read_json(path, default=None)
            if not isinstance(payload, dict):
                continue
            if not include_archived and str(payload.get("status") or "active") == "archived":
                continue
            payload["category_id"] = PRODUCT_MASTER_CATEGORY_ID
            items.append(payload)
        return items


def normalize_product_item(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["category_id"] = PRODUCT_MASTER_CATEGORY_ID
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


def validate_product_item(item: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    product_id = str(item.get("id") or "")
    if not product_id:
        problems.append("item id is required")
    elif not SAFE_PRODUCT_ID_RE.fullmatch(product_id):
        problems.append(f"unsafe product id: {product_id}")
    if item.get("category_id") != PRODUCT_MASTER_CATEGORY_ID:
        problems.append("item category_id must be products")
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    fields = {field["id"]: field for field in schema.get("fields", []) or [] if isinstance(field, dict) and field.get("id")}
    for field_id, field in fields.items():
        if field.get("required") and data.get(field_id) in (None, "", [], {}):
            problems.append(f"required field is missing: {field_id}")
    return {"ok": not problems, "problems": problems}


def validate_product_id(product_id: str) -> None:
    if not SAFE_PRODUCT_ID_RE.fullmatch(str(product_id or "")):
        raise ValueError(f"unsafe product id: {product_id}")


def read_json(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    last_error: PermissionError | None = None
    for attempt in range(6):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.08 * (attempt + 1))
    try:
        temp_path.unlink(missing_ok=True)
    finally:
        if last_error:
            raise last_error


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def postgres_store(tenant_id: str):
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(tenant_id=tenant_id, config=config)
    return store if store.available() else None
