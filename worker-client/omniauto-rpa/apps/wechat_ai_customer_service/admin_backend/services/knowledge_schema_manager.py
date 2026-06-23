"""Schema and resolver access for classified knowledge categories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .knowledge_registry import KnowledgeRegistry
from apps.wechat_ai_customer_service.product_master import PRODUCT_MASTER_CATEGORY_ID, ProductMasterStore
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import (
    PRODUCT_SCOPED_RESOLVERS,
    PRODUCT_SCOPED_SCHEMAS,
)


class KnowledgeSchemaManager:
    def __init__(self, registry: KnowledgeRegistry | None = None) -> None:
        self.registry = registry or KnowledgeRegistry()
        self.product_master = ProductMasterStore()

    def load_schema(self, category_id: str) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID:
            return self.product_master.load_schema()
        if category_id in PRODUCT_SCOPED_SCHEMAS:
            return dict(PRODUCT_SCOPED_SCHEMAS[category_id])
        path = self.registry.category_root(category_id) / "schema.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def load_resolver(self, category_id: str) -> dict[str, Any]:
        if category_id == PRODUCT_MASTER_CATEGORY_ID:
            return self.product_master.load_resolver()
        if category_id in PRODUCT_SCOPED_RESOLVERS:
            return dict(PRODUCT_SCOPED_RESOLVERS[category_id])
        path = self.registry.category_root(category_id) / "resolver.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def save_schema(self, category_id: str, schema: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_schema(category_id, schema)
        if not validation["ok"]:
            return validation
        if category_id == PRODUCT_MASTER_CATEGORY_ID:
            self.product_master.ensure_structure()
            self.product_master.schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"ok": True, "schema": schema}
        path = self.registry.category_root(category_id) / "schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "schema": schema}

    def validate_schema(self, category_id: str, schema: dict[str, Any]) -> dict[str, Any]:
        problems = []
        if schema.get("category_id") != category_id:
            problems.append(f"schema category_id must be {category_id}")
        fields = schema.get("fields", []) or []
        if not isinstance(fields, list) or not fields:
            problems.append("schema fields must be a non-empty list")
        seen = set()
        for field in fields:
            field_id = str(field.get("id") or "")
            if not field_id:
                problems.append("field id is required")
                continue
            if field_id in seen:
                problems.append(f"duplicate field id: {field_id}")
            seen.add(field_id)
            if not field.get("label"):
                problems.append(f"field label is required: {field_id}")
            if not field.get("type"):
                problems.append(f"field type is required: {field_id}")
        resolver = self.load_resolver(category_id) if category_id in PRODUCT_SCOPED_RESOLVERS or (self.registry.category_root(category_id) / "resolver.json").exists() else {}
        for key in ("match_fields", "intent_fields", "risk_fields", "reply_fields"):
            for field_id in resolver.get(key, []) or []:
                if field_id not in seen:
                    problems.append(f"resolver {key} references missing field: {field_id}")
        return {"ok": not problems, "problems": problems}


def schema_path(category_root: Path) -> Path:
    return category_root / "schema.json"
