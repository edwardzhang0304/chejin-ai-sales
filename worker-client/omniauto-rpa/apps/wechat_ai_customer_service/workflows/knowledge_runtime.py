"""Runtime access to the category-isolated knowledge bases.

This module is intentionally independent from the admin API layer. The WeChat
workflow can use it directly without depending on FastAPI routes or admin-only
models, while still reading the same formal knowledge source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - supports package imports and direct workflow scripts.
    from apps.wechat_ai_customer_service.knowledge_paths import (
        LEGACY_KNOWLEDGE_BASE_ROOT,
        SHARED_KNOWLEDGE_ROOT,
        active_tenant_id,
        default_admin_knowledge_base_root,
        runtime_knowledge_roots,
        shared_runtime_cache_root,
        tenant_product_item_knowledge_root,
    )
    from apps.wechat_ai_customer_service.product_master import (
        PRODUCT_MASTER_CATEGORY_ID,
        ProductMasterStore,
        product_master_category_record,
    )
    from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config
except ImportError:  # pragma: no cover
    APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
    LEGACY_KNOWLEDGE_BASE_ROOT = APP_PACKAGE_ROOT / "data" / "knowledge_bases"
    SHARED_KNOWLEDGE_ROOT = APP_PACKAGE_ROOT / "data" / "shared_knowledge"
    shared_runtime_cache_root = lambda: APP_PACKAGE_ROOT.parents[1] / "runtime" / "apps" / "wechat_ai_customer_service" / "cache" / "shared_knowledge"
    active_tenant_id = lambda tenant_id=None: tenant_id or "default"
    default_admin_knowledge_base_root = lambda tenant_id=None: LEGACY_KNOWLEDGE_BASE_ROOT
    runtime_knowledge_roots = lambda tenant_id=None: [LEGACY_KNOWLEDGE_BASE_ROOT]
    tenant_product_item_knowledge_root = lambda tenant_id=None: APP_PACKAGE_ROOT / "data" / "tenants" / "default" / "product_item_knowledge"
    PRODUCT_MASTER_CATEGORY_ID = "products"
    ProductMasterStore = None
    product_master_category_record = lambda: {"id": "products", "name": "商品主数据", "scope": "product_master"}
    get_postgres_store = None
    load_storage_config = None

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_BASE_ROOT = default_admin_knowledge_base_root()

PRODUCT_SCOPED_KINDS = {
    "faq": "product_faq",
    "rules": "product_rules",
    "explanations": "product_explanations",
}

PRODUCT_SCOPED_SCHEMAS: dict[str, dict[str, Any]] = {
    "product_faq": {
        "schema_version": 1,
        "category_id": "product_faq",
        "display_name": "商品专属问答",
        "item_title_field": "title",
        "item_subtitle_field": "product_id",
        "fields": [
            {"id": "product_id", "label": "商品 ID", "type": "short_text", "required": True},
            {"id": "title", "label": "标题", "type": "short_text", "required": True},
            {"id": "keywords", "label": "触发关键词", "type": "tags", "required": False},
            {"id": "question", "label": "客户问题", "type": "long_text", "required": False},
            {"id": "answer", "label": "标准回复", "type": "long_text", "required": True},
            {"id": "additional_details", "label": "补充信息", "type": "object", "required": False},
        ],
    },
    "product_rules": {
        "schema_version": 1,
        "category_id": "product_rules",
        "display_name": "商品专属规则",
        "item_title_field": "title",
        "item_subtitle_field": "product_id",
        "fields": [
            {"id": "product_id", "label": "商品 ID", "type": "short_text", "required": True},
            {"id": "title", "label": "规则名称", "type": "short_text", "required": True},
            {"id": "keywords", "label": "触发关键词", "type": "tags", "required": False},
            {"id": "answer", "label": "标准回复", "type": "long_text", "required": True},
            {"id": "allow_auto_reply", "label": "允许自动回复", "type": "boolean", "required": False},
            {"id": "requires_handoff", "label": "必须转人工", "type": "boolean", "required": False},
            {"id": "handoff_reason", "label": "转人工原因", "type": "short_text", "required": False},
            {"id": "additional_details", "label": "补充信息", "type": "object", "required": False},
        ],
    },
    "product_explanations": {
        "schema_version": 1,
        "category_id": "product_explanations",
        "display_name": "商品专属解释",
        "item_title_field": "title",
        "item_subtitle_field": "product_id",
        "fields": [
            {"id": "product_id", "label": "商品 ID", "type": "short_text", "required": True},
            {"id": "title", "label": "说明主题", "type": "short_text", "required": True},
            {"id": "keywords", "label": "触发关键词", "type": "tags", "required": False},
            {"id": "content", "label": "说明内容", "type": "long_text", "required": True},
            {"id": "additional_details", "label": "补充信息", "type": "object", "required": False},
        ],
    },
}

PRODUCT_SCOPED_RESOLVERS: dict[str, dict[str, Any]] = {
    category_id: {
        "schema_version": 1,
        "category_id": category_id,
        "match_fields": ["title", "keywords", "question", "answer", "content", "additional_details"],
        "intent_fields": ["keywords"],
        "risk_fields": ["keywords", "answer", "content"],
        "reply_fields": ["answer", "content", "additional_details"],
        "minimum_confidence": 0.35,
        "default_action": "product_scoped_context",
    }
    for category_id in PRODUCT_SCOPED_SCHEMAS
}


class KnowledgeRuntime:
    """Read-only runtime facade for classified knowledge."""

    def __init__(self, root: Path | None = None, *, tenant_id: str | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.single_root_mode = root is not None
        self.roots = [root.resolve()] if root else [item.resolve() for item in runtime_knowledge_roots(tenant_id)]
        if not self.roots:
            self.roots = [LEGACY_KNOWLEDGE_BASE_ROOT.resolve()]
        self.root = (root or default_admin_knowledge_base_root(tenant_id)).resolve()
        self.registry_path = self.root / "registry.json"
        self.product_item_root = tenant_product_item_knowledge_root(tenant_id).resolve() if not root else (root.parent / "product_item_knowledge").resolve()
        has_legacy_products = bool(root and (root / "products").exists())
        self.product_master = ProductMasterStore(tenant_id=self.tenant_id) if ProductMasterStore is not None and not has_legacy_products else None

    def load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            raise FileNotFoundError(str(self.registry_path))
        return read_json(self.registry_path)

    def load_registry_from_root(self, root: Path) -> dict[str, Any]:
        path = root / "registry.json"
        if not path.exists():
            return {"categories": []}
        return read_json(path)

    def list_categories(
        self,
        *,
        enabled_only: bool = True,
        reply_only: bool = False,
    ) -> list[dict[str, Any]]:
        categories = []
        seen: set[str] = set()
        for root in self.roots:
            for item in self.load_registry_from_root(root).get("categories", []) or []:
                category_id = str(item.get("id") or "")
                if category_id == PRODUCT_MASTER_CATEGORY_ID:
                    continue
                if not category_id or category_id in seen:
                    continue
                seen.add(category_id)
                categories.append(item)
        if enabled_only:
            categories = [item for item in categories if item.get("enabled", True)]
        if reply_only:
            categories = [item for item in categories if item.get("participates_in_reply", False)]
        return sorted(categories, key=lambda item: (int(item.get("sort_order", 999)), str(item.get("id") or "")))

    def get_category(self, category_id: str) -> dict[str, Any] | None:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            return product_master_category_record()
        for root in self.roots:
            for category in self.load_registry_from_root(root).get("categories", []) or []:
                if category.get("id") == category_id:
                    return category
        return None

    def require_category(self, category_id: str) -> dict[str, Any]:
        category = self.get_category(category_id)
        if not category:
            raise FileNotFoundError(f"category not found: {category_id}")
        return category

    def category_root(self, category_id: str) -> Path:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            return self.product_master.root
        for root in self.roots:
            category = self.get_category_from_root(root, category_id)
            if not category:
                continue
            path = (root / str(category.get("path") or category_id)).resolve()
            if root not in path.parents and path != root:
                raise ValueError(f"category path escapes knowledge base root: {category_id}")
            return path
        raise FileNotFoundError(f"category not found: {category_id}")

    def get_category_from_root(self, root: Path, category_id: str) -> dict[str, Any] | None:
        for category in self.load_registry_from_root(root).get("categories", []) or []:
            if category.get("id") == category_id:
                return category
        return None

    def category_root_from_root(self, root: Path, category_id: str) -> Path:
        category = self.get_category_from_root(root, category_id)
        if not category:
            raise FileNotFoundError(f"category not found: {category_id}")
        path = (root / str(category.get("path") or category_id)).resolve()
        if root not in path.parents and path != root:
            raise ValueError(f"category path escapes knowledge base root: {category_id}")
        return path

    def load_schema(self, category_id: str) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            return self.product_master.load_schema()
        return read_json(self.category_root(category_id) / "schema.json")

    def load_resolver(self, category_id: str) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            return self.product_master.load_resolver()
        return read_json(self.category_root(category_id) / "resolver.json")

    def items_root(self, category_id: str) -> Path:
        return self.category_root(category_id) / "items"

    def list_items(
        self,
        category_id: str,
        *,
        include_archived: bool = False,
        include_unacknowledged: bool = False,
    ) -> list[dict[str, Any]]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            return filter_runtime_items(
                [
                    mark_layer(item, "product_master")
                    for item in self.product_master.list_items(include_archived=include_archived)
                ],
                include_archived=include_archived,
                include_unacknowledged=include_unacknowledged,
            )
        db = postgres_store(self.tenant_id)
        if db and not self.single_root_mode:
            layer_items: list[dict[str, Any]] = []
            if category_id in PRODUCT_SCOPED_SCHEMAS:
                layer_items.extend(
                    [
                        mark_layer(item, "tenant_product")
                        for item in db.list_knowledge_items(self.tenant_id, layer="tenant_product", category_id=category_id, include_archived=include_archived)
                    ]
                )
            else:
                layer_items.extend(
                    [
                        mark_layer(item, "tenant")
                        for item in db.list_knowledge_items(self.tenant_id, layer="tenant", category_id=category_id, include_archived=include_archived)
                    ]
                )
                layer_items.extend(
                    [
                        mark_layer(item, "shared")
                        for item in db.list_knowledge_items(self.tenant_id, layer="shared", category_id=category_id, include_archived=include_archived)
                    ]
                )
            if layer_items:
                return filter_runtime_items(
                    layer_items,
                    include_archived=include_archived,
                    include_unacknowledged=include_unacknowledged,
                )
        if category_id in PRODUCT_SCOPED_SCHEMAS:
            return self.list_file_product_scoped_items(
                category_id,
                include_archived=include_archived,
                include_unacknowledged=include_unacknowledged,
            )
        items = []
        for root in self.roots:
            category = self.get_category_from_root(root, category_id)
            if not category:
                continue
            items_root = self.category_root_from_root(root, category_id) / "items"
            if not items_root.exists():
                continue
            for path in sorted(items_root.glob("*.json")):
                item = read_json_optional(path)
                if item is None:
                    continue
                if not include_archived and item.get("status") == "archived":
                    continue
                items.append(mark_layer(item, layer_for_root(root)))
        return filter_runtime_items(
            items,
            include_archived=include_archived,
            include_unacknowledged=include_unacknowledged,
        )

    def get_item(
        self,
        category_id: str,
        item_id: str,
        *,
        include_unacknowledged: bool = False,
    ) -> dict[str, Any] | None:
        if category_id == PRODUCT_MASTER_CATEGORY_ID and self.product_master is not None:
            item = self.product_master.get_item(item_id, include_archived=True)
            if not item:
                return None
            marked = mark_layer(item, "product_master")
            return marked if item_is_runtime_usable(marked, include_unacknowledged=include_unacknowledged) else None
        db = postgres_store(self.tenant_id)
        if db and not self.single_root_mode:
            if category_id in PRODUCT_SCOPED_SCHEMAS:
                for item in db.list_knowledge_items(self.tenant_id, layer="tenant_product", category_id=category_id, include_archived=True):
                    if str(item.get("id") or "") == item_id:
                        marked = mark_layer(item, "tenant_product")
                        return marked if item_is_runtime_usable(marked, include_unacknowledged=include_unacknowledged) else None
            else:
                for layer in ("tenant", "shared"):
                    item = db.get_knowledge_item(self.tenant_id, layer=layer, category_id=category_id, item_id=item_id)
                    if item:
                        marked = mark_layer(item, layer)
                        return marked if item_is_runtime_usable(marked, include_unacknowledged=include_unacknowledged) else None
        if category_id in PRODUCT_SCOPED_SCHEMAS:
            for _kind, _schema, _resolver, item in self.iter_all_product_scoped_items():
                if str(item.get("id") or "") == item_id and item_is_runtime_usable(item, include_unacknowledged=include_unacknowledged):
                    return item
            return None
        for root in self.roots:
            if not self.get_category_from_root(root, category_id):
                continue
            items_root = (self.category_root_from_root(root, category_id) / "items").resolve()
            path = (items_root / f"{item_id}.json").resolve()
            if items_root not in path.parents:
                raise ValueError(f"item path escapes category root: {item_id}")
            if not path.exists():
                continue
            item = read_json_optional(path)
            if item is None:
                continue
            marked = mark_layer(item, layer_for_root(root))
            return marked if item_is_runtime_usable(marked, include_unacknowledged=include_unacknowledged) else None
        return None

    def iter_reply_items(self) -> Iterable[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]:
        db = postgres_store(self.tenant_id)
        if db and not self.single_root_mode:
            for category in self.list_categories(reply_only=True):
                category_id = str(category.get("id") or "")
                try:
                    schema = self.load_schema(category_id)
                    resolver = self.load_resolver(category_id)
                except FileNotFoundError:
                    continue
                for item in self.list_items(category_id, include_unacknowledged=False):
                    yield category, schema, resolver, item
            return
        for root in self.roots:
            for category in self.list_categories_from_root(root, reply_only=True):
                category_id = str(category.get("id") or "")
                schema = read_json(self.category_root_from_root(root, category_id) / "schema.json")
                resolver = read_json(self.category_root_from_root(root, category_id) / "resolver.json")
                items_root = self.category_root_from_root(root, category_id) / "items"
                if not items_root.exists():
                    continue
                for path in sorted(items_root.glob("*.json")):
                    item = read_json_optional(path)
                    if item is None:
                        continue
                    marked = mark_layer(item, layer_for_root(root))
                    if not item_is_runtime_usable(marked, include_unacknowledged=False):
                        continue
                    yield category, schema, resolver, marked

    def list_categories_from_root(self, root: Path, *, reply_only: bool = False) -> list[dict[str, Any]]:
        categories = [
            item
            for item in self.load_registry_from_root(root).get("categories", []) or []
            if item.get("enabled", True) and str(item.get("id") or "") != PRODUCT_MASTER_CATEGORY_ID
        ]
        if reply_only:
            categories = [item for item in categories if item.get("participates_in_reply", False)]
        return sorted(categories, key=lambda item: (int(item.get("sort_order", 999)), str(item.get("id") or "")))

    def iter_product_scoped_items(
        self,
        product_ids: Iterable[str],
    ) -> Iterable[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]:
        allowed_product_ids = self.runtime_usable_product_ids()
        db = postgres_store(self.tenant_id)
        if db and not self.single_root_mode:
            for product_id in sorted({str(item) for item in product_ids if str(item)}):
                if product_id not in allowed_product_ids:
                    continue
                for kind, category_id in PRODUCT_SCOPED_KINDS.items():
                    category = {
                        "id": category_id,
                        "name": PRODUCT_SCOPED_SCHEMAS[category_id]["display_name"],
                        "kind": "product_scoped",
                        "path": f"product_item_knowledge/{product_id}/{kind}",
                        "enabled": True,
                        "participates_in_reply": True,
                        "sort_order": 70,
                    }
                    schema = PRODUCT_SCOPED_SCHEMAS[category_id]
                    resolver = PRODUCT_SCOPED_RESOLVERS[category_id]
                    for item in db.list_knowledge_items(
                        self.tenant_id,
                        layer="tenant_product",
                        category_id=category_id,
                        product_id=product_id,
                    ):
                        marked = mark_layer(item, "tenant_product")
                        if item_is_runtime_usable(marked, include_unacknowledged=False):
                            yield category, schema, resolver, marked
            return
        for product_id in sorted({str(item) for item in product_ids if str(item)}):
            if product_id not in allowed_product_ids:
                continue
            product_root = (self.product_item_root / product_id).resolve()
            if self.product_item_root not in product_root.parents and product_root != self.product_item_root:
                continue
            for kind, category_id in PRODUCT_SCOPED_KINDS.items():
                kind_root = product_root / kind
                if not kind_root.exists():
                    continue
                category = {
                    "id": category_id,
                    "name": PRODUCT_SCOPED_SCHEMAS[category_id]["display_name"],
                    "kind": "product_scoped",
                    "path": str(kind_root),
                    "enabled": True,
                    "participates_in_reply": True,
                    "sort_order": 70,
                }
                schema = PRODUCT_SCOPED_SCHEMAS[category_id]
                resolver = PRODUCT_SCOPED_RESOLVERS[category_id]
                for path in sorted(kind_root.glob("*.json")):
                    item = read_json_optional(path)
                    if item is None:
                        continue
                    item.setdefault("category_id", category_id)
                    item.setdefault("data", {})
                    item["data"].setdefault("product_id", product_id)
                    marked = mark_layer(item, "tenant_product")
                    if not item_is_runtime_usable(marked, include_unacknowledged=False):
                        continue
                    yield category, schema, resolver, marked

    def list_file_product_scoped_items(
        self,
        category_id: str,
        *,
        include_archived: bool = False,
        include_unacknowledged: bool = False,
    ) -> list[dict[str, Any]]:
        if category_id not in PRODUCT_SCOPED_SCHEMAS or not self.product_item_root.exists():
            return []
        kind = ""
        for candidate_kind, candidate_category_id in PRODUCT_SCOPED_KINDS.items():
            if candidate_category_id == category_id:
                kind = candidate_kind
                break
        if not kind:
            return []
        items: list[dict[str, Any]] = []
        for product_root in sorted(path for path in self.product_item_root.iterdir() if path.is_dir()):
            kind_root = product_root / kind
            if not kind_root.exists():
                continue
            for path in sorted(kind_root.glob("*.json")):
                item = read_json_optional(path)
                if item is None:
                    continue
                if not include_archived and item.get("status") == "archived":
                    continue
                item.setdefault("category_id", category_id)
                item.setdefault("data", {})
                item["data"].setdefault("product_id", product_root.name)
                items.append(mark_layer(item, "tenant_product"))
        items = filter_runtime_items(
            items,
            include_archived=include_archived,
            include_unacknowledged=include_unacknowledged,
        )
        if include_unacknowledged:
            return items
        allowed_product_ids = self.runtime_usable_product_ids()
        return [item for item in items if str((item.get("data") or {}).get("product_id") or "") in allowed_product_ids]

    def iter_all_product_scoped_items(self) -> Iterable[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
        db = postgres_store(self.tenant_id)
        if db and not self.single_root_mode:
            allowed_product_ids = self.runtime_usable_product_ids()
            for category_id in PRODUCT_SCOPED_SCHEMAS:
                for item in db.list_knowledge_items(self.tenant_id, layer="tenant_product", category_id=category_id):
                    marked = mark_layer(item, "tenant_product")
                    if not item_is_runtime_usable(marked, include_unacknowledged=False):
                        continue
                    product_id = str((marked.get("data") or {}).get("product_id") or "")
                    if product_id and product_id not in allowed_product_ids:
                        continue
                    yield category_id, PRODUCT_SCOPED_SCHEMAS[category_id], PRODUCT_SCOPED_RESOLVERS[category_id], marked
            return
        if not self.product_item_root.exists():
            return
        product_ids = [path.name for path in self.product_item_root.iterdir() if path.is_dir()]
        for category, schema, resolver, item in self.iter_product_scoped_items(product_ids):
            yield str(category.get("id") or ""), schema, resolver, item

    def runtime_usable_product_ids(self) -> set[str]:
        return {str(item.get("id") or "") for item in self.list_items("products", include_unacknowledged=False) if str(item.get("id") or "")}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_optional(path: Path) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def layer_for_root(root: Path) -> str:
    try:
        resolved = root.resolve()
        if resolved in {SHARED_KNOWLEDGE_ROOT.resolve(), shared_runtime_cache_root().resolve()}:
            return "shared"
    except OSError:
        pass
    return "tenant"


def mark_layer(item: dict[str, Any], layer: str) -> dict[str, Any]:
    marked = dict(item)
    marked["_knowledge_layer"] = layer
    metadata = marked.get("metadata") if isinstance(marked.get("metadata"), dict) else {}
    marked["metadata"] = {**metadata, "knowledge_layer": layer}
    return marked


def filter_runtime_items(
    items: list[dict[str, Any]],
    *,
    include_archived: bool = False,
    include_unacknowledged: bool = False,
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if item_is_runtime_usable(
            item,
            include_archived=include_archived,
            include_unacknowledged=include_unacknowledged,
        )
    ]


def item_is_runtime_usable(
    item: dict[str, Any],
    *,
    include_archived: bool = False,
    include_unacknowledged: bool = False,
) -> bool:
    if not include_archived and str(item.get("status") or "active") == "archived":
        return False
    if include_unacknowledged:
        return True
    review_state = item.get("review_state") if isinstance(item.get("review_state"), dict) else {}
    return not bool(review_state.get("is_new"))


def postgres_store(tenant_id: str):
    if get_postgres_store is None or load_storage_config is None:
        return None
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(tenant_id=tenant_id, config=config)
    return store if store.available() else None
