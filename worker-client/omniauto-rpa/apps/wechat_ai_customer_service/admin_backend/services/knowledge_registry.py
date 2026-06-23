"""Category registry for the classified knowledge base."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, default_admin_knowledge_base_root
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


APP_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_BASE_ROOT = default_admin_knowledge_base_root()
REGISTRY_PATH = KNOWLEDGE_BASE_ROOT / "registry.json"
SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
RESERVED_CATEGORY_IDS = {"products", "chats", "policies", "erp_exports", "custom"}


class KnowledgeRegistry:
    def __init__(self, root: Path | None = None) -> None:
        default_root = default_admin_knowledge_base_root()
        self.root = root or default_root
        self.registry_path = self.root / "registry.json"
        self.default_root_mode = self.root.resolve() == default_root.resolve()

    def load(self) -> dict[str, Any]:
        db = postgres_store() if self.default_root_mode else None
        if db:
            categories = db.list_categories(active_tenant_id(), layer="tenant", enabled_only=False)
            categories = [category for category in categories if self.category_files_available(category)]
            if categories:
                return {"schema_version": 1, "categories": categories}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def save(self, registry: dict[str, Any]) -> None:
        registry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        db = postgres_store() if self.default_root_mode else None
        config = load_storage_config()
        if db:
            for category in registry.get("categories", []) or []:
                db.upsert_category(active_tenant_id(), "tenant", category)
            if not config.mirror_files:
                return
        self.registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def list_categories(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        categories = list(self.load().get("categories", []) or [])
        if enabled_only:
            categories = [item for item in categories if item.get("enabled", True)]
        return sorted(categories, key=lambda item: (int(item.get("sort_order", 999)), str(item.get("id") or "")))

    def get_category(self, category_id: str) -> dict[str, Any] | None:
        for category in self.load().get("categories", []) or []:
            if category.get("id") == category_id:
                return category
        return None

    def require_category(self, category_id: str) -> dict[str, Any]:
        category = self.get_category(category_id)
        if not category:
            raise FileNotFoundError(f"category not found: {category_id}")
        return category

    def category_root(self, category_id: str) -> Path:
        category = self.require_category(category_id)
        path = self.root / str(category.get("path") or category_id)
        resolved = path.resolve()
        root = self.root.resolve()
        if root not in resolved.parents and resolved != root:
            raise ValueError(f"category path escapes knowledge base root: {category_id}")
        return resolved

    def category_files_available(self, category: dict[str, Any]) -> bool:
        category_id = str(category.get("id") or "")
        if not category_id:
            return False
        path = self.root / str(category.get("path") or category_id)
        return (path / "schema.json").exists() and (path / "resolver.json").exists()

    def create_custom_category(
        self,
        category_id: str,
        name: str,
        description: str = "",
        participates_in_reply: bool = False,
        participates_in_learning: bool = True,
        participates_in_diagnostics: bool = True,
        fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        validate_category_id(category_id)
        if category_id in RESERVED_CATEGORY_IDS:
            raise ValueError(f"reserved category id: {category_id}")
        if self.get_category(category_id):
            raise FileExistsError(f"category already exists: {category_id}")

        registry = self.load()
        category = {
            "id": category_id,
            "name": name,
            "kind": "custom",
            "path": f"custom/{category_id}",
            "enabled": True,
            "participates_in_reply": participates_in_reply,
            "participates_in_learning": participates_in_learning,
            "participates_in_diagnostics": participates_in_diagnostics,
            "sort_order": next_sort_order(registry.get("categories", []) or []),
        }
        category_root = self.root / category["path"]
        (category_root / "items").mkdir(parents=True, exist_ok=False)
        (category_root / "items" / ".gitkeep").write_text("\n", encoding="utf-8")
        write_default_custom_schema(category_root, category_id, name, description, fields or [])
        write_default_custom_resolver(category_root, category_id, participates_in_reply)
        registry.setdefault("categories", []).append(category)
        self.save(registry)
        return category


def validate_category_id(category_id: str) -> None:
    if not SAFE_ID_RE.fullmatch(category_id):
        raise ValueError("category_id must start with a letter and contain only lowercase letters, digits, underscores, or hyphens")


def next_sort_order(categories: list[dict[str, Any]]) -> int:
    if not categories:
        return 100
    return max(int(item.get("sort_order", 0)) for item in categories) + 10


def write_default_custom_schema(
    category_root: Path,
    category_id: str,
    name: str,
    description: str,
    fields: list[dict[str, Any]],
) -> None:
    normalized_fields = fields or [
        {"id": "title", "label": "标题", "type": "short_text", "required": True, "searchable": True, "form_order": 10},
        {"id": "content", "label": "内容", "type": "long_text", "required": False, "searchable": True, "form_order": 20},
    ]
    schema = {
        "schema_version": 1,
        "category_id": category_id,
        "display_name": name,
        "description": description,
        "item_title_field": normalized_fields[0]["id"],
        "item_subtitle_field": normalized_fields[1]["id"] if len(normalized_fields) > 1 else normalized_fields[0]["id"],
        "fields": normalized_fields,
        "validation": {"unique_fields": ["id"], "required_for_auto_reply": []},
    }
    (category_root / "schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_default_custom_resolver(category_root: Path, category_id: str, participates_in_reply: bool) -> None:
    resolver = {
        "schema_version": 1,
        "category_id": category_id,
        "match_fields": ["title", "content"] if participates_in_reply else [],
        "intent_fields": [],
        "risk_fields": [],
        "reply_fields": ["title", "content"] if participates_in_reply else [],
        "minimum_confidence": 0.5,
        "default_action": "custom_context" if participates_in_reply else "admin_only",
    }
    (category_root / "resolver.json").write_text(json.dumps(resolver, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
