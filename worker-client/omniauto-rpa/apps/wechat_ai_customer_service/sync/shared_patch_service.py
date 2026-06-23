"""Shared-knowledge patch preview and application."""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import SHARED_KNOWLEDGE_ROOT, shared_patches_root


ALLOWED_OPS = {"upsert_json"}

SHARED_CATEGORY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "global_guidelines": {
        "registry": {
            "id": "global_guidelines",
            "name": "Global Assistant Guidelines",
            "kind": "global",
            "path": "global_guidelines",
            "enabled": True,
            "participates_in_reply": True,
            "participates_in_learning": False,
            "participates_in_diagnostics": True,
            "sort_order": 10,
        },
        "schema": {
            "schema_version": 1,
            "category_id": "global_guidelines",
            "display_name": "共享通用客服原则",
            "item_title_field": "title",
            "fields": [
                {"id": "title", "label": "标题", "type": "short_text", "required": True},
                {"id": "keywords", "label": "关键词", "type": "tags", "required": False},
                {"id": "guideline_text", "label": "规则内容", "type": "long_text", "required": True},
                {"id": "applies_to", "label": "适用场景", "type": "long_text", "required": False},
            ],
        },
        "resolver": {
            "schema_version": 1,
            "category_id": "global_guidelines",
            "match_fields": ["title", "keywords", "guideline_text", "applies_to"],
            "intent_fields": ["keywords"],
            "reply_fields": ["guideline_text", "applies_to"],
            "minimum_confidence": 0.34,
            "default_action": "shared_guideline_context",
        },
    },
    "reply_style": {
        "registry": {
            "id": "reply_style",
            "name": "共享回复口吻",
            "kind": "global",
            "path": "reply_style",
            "enabled": True,
            "participates_in_reply": True,
            "participates_in_learning": False,
            "participates_in_diagnostics": True,
            "sort_order": 75,
        },
        "schema": {
            "schema_version": 1,
            "category_id": "reply_style",
            "display_name": "共享回复口吻",
            "item_title_field": "title",
            "fields": [
                {"id": "title", "label": "标题", "type": "short_text", "required": True},
                {"id": "keywords", "label": "触发关键词", "type": "tags", "required": False},
                {"id": "guideline_text", "label": "表达方式", "type": "long_text", "required": True},
                {"id": "applies_to", "label": "适用场景", "type": "long_text", "required": False},
            ],
        },
        "resolver": {
            "schema_version": 1,
            "category_id": "reply_style",
            "match_fields": ["title", "keywords", "guideline_text", "applies_to"],
            "intent_fields": ["keywords"],
            "reply_fields": ["guideline_text", "applies_to"],
            "minimum_confidence": 0.34,
            "default_action": "shared_reply_style_context",
        },
    },
    "risk_control": {
        "registry": {
            "id": "risk_control",
            "name": "共享风险边界",
            "kind": "global",
            "path": "risk_control",
            "enabled": True,
            "participates_in_reply": True,
            "participates_in_learning": False,
            "participates_in_diagnostics": True,
            "sort_order": 25,
        },
        "schema": {
            "schema_version": 1,
            "category_id": "risk_control",
            "display_name": "共享风险边界",
            "item_title_field": "title",
            "fields": [
                {"id": "title", "label": "标题", "type": "short_text", "required": True},
                {"id": "keywords", "label": "风险关键词", "type": "tags", "required": False},
                {"id": "guideline_text", "label": "处理规则", "type": "long_text", "required": True},
                {"id": "applies_to", "label": "适用场景", "type": "long_text", "required": False},
                {"id": "handoff_reason", "label": "转人工原因", "type": "short_text", "required": False},
            ],
        },
        "resolver": {
            "schema_version": 1,
            "category_id": "risk_control",
            "match_fields": ["title", "keywords", "guideline_text", "applies_to", "handoff_reason"],
            "intent_fields": ["keywords"],
            "risk_fields": ["keywords", "guideline_text", "handoff_reason"],
            "reply_fields": ["guideline_text", "handoff_reason"],
            "minimum_confidence": 0.34,
            "default_action": "shared_risk_control",
        },
    },
}


class SharedPatchService:
    def __init__(self, *, root: Path | None = None, signing_secret: str | None = None) -> None:
        self.root = (root or SHARED_KNOWLEDGE_ROOT).resolve()
        self.signing_secret = os.getenv("WECHAT_SHARED_PATCH_SECRET", "") if signing_secret is None else signing_secret

    def preview(self, patch: dict[str, Any]) -> dict[str, Any]:
        operations = self.validate_patch(patch)
        return {
            "ok": True,
            "patch_id": str(patch.get("patch_id") or ""),
            "version": str(patch.get("version") or ""),
            "operation_count": len(operations),
            "operations": [
                {
                    "op": item["op"],
                    "path": item["path"],
                    "target_path": str(self.target_path(item["path"])),
                    "exists": self.target_path(item["path"]).exists(),
                }
                for item in operations
            ],
        }

    def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        operations = self.validate_patch(patch)
        applied = []
        for operation in operations:
            target = self.target_path(operation["path"])
            self.ensure_category_support(operation["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            write_json(target, operation.get("content", {}))
            applied.append({"op": operation["op"], "path": operation["path"], "target_path": str(target)})
        record = {
            "patch_id": str(patch.get("patch_id") or ""),
            "version": str(patch.get("version") or ""),
            "applied_at": now(),
            "operation_count": len(applied),
        }
        self.record_applied(record)
        return {"ok": True, "applied": applied, "record": record}

    def validate_patch(self, patch: dict[str, Any]) -> list[dict[str, Any]]:
        if int(patch.get("schema_version") or 0) != 1:
            raise ValueError("unsupported shared patch schema_version")
        if self.signing_secret and not self.verify_signature(patch):
            raise ValueError("shared patch signature verification failed")
        operations = patch.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ValueError("shared patch requires operations")
        normalized = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("shared patch operation must be an object")
            op = str(operation.get("op") or "")
            if op not in ALLOWED_OPS:
                raise ValueError(f"unsupported shared patch operation: {op}")
            target_path = self.target_path(str(operation.get("path") or ""))
            if target_path.suffix.lower() != ".json":
                raise ValueError("shared patch can only write JSON files")
            content = operation.get("content")
            if not isinstance(content, dict):
                raise ValueError("upsert_json operation requires object content")
            normalized.append({"op": op, "path": str(operation.get("path") or ""), "content": content})
        return normalized

    def target_path(self, relative_path: str) -> Path:
        clean = relative_path.replace("\\", "/").lstrip("/")
        if not clean or ".." in Path(clean).parts:
            raise ValueError(f"unsafe shared patch path: {relative_path}")
        target = (self.root / clean).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError(f"shared patch path escapes root: {relative_path}")
        return target

    def ensure_category_support(self, relative_path: str) -> None:
        category_id = relative_path.replace("\\", "/").lstrip("/").split("/", 1)[0]
        definition = SHARED_CATEGORY_DEFINITIONS.get(category_id)
        if not definition:
            return
        registry_path = self.root / "registry.json"
        registry = read_json(registry_path, default={"schema_version": 1, "scope": "wechat_ai_customer_service_shared", "categories": []})
        if not isinstance(registry, dict):
            registry = {"schema_version": 1, "scope": "wechat_ai_customer_service_shared", "categories": []}
        categories = registry.get("categories") if isinstance(registry.get("categories"), list) else []
        category_ids = {str(item.get("id") or item.get("category_id") or "") for item in categories if isinstance(item, dict)}
        if category_id not in category_ids:
            categories.append(dict(definition["registry"]))
            registry["categories"] = categories
            registry["updated_at"] = now()
            write_json(registry_path, registry)
        category_root = self.root / category_id
        category_root.mkdir(parents=True, exist_ok=True)
        for file_name in ("schema.json", "resolver.json"):
            path = category_root / file_name
            if not path.exists():
                write_json(path, definition[file_name.removesuffix(".json")])

    def verify_signature(self, patch: dict[str, Any]) -> bool:
        signature = str(patch.get("signature") or "")
        if not signature:
            return False
        unsigned = {key: value for key, value in patch.items() if key != "signature"}
        payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(self.signing_secret.encode("utf-8"), payload, sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def record_applied(self, record: dict[str, Any]) -> None:
        root = shared_patches_root()
        if self.root != SHARED_KNOWLEDGE_ROOT.resolve():
            root = self.root / "patches"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "applied_patches.json"
        records = read_json(path, default=[])
        if not isinstance(records, list):
            records = []
        records.append(record)
        write_json(path, records[-200:])


def read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
