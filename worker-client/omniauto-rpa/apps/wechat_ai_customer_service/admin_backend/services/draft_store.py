"""Runtime draft storage and safe application."""

from __future__ import annotations

import difflib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from .diagnostics_service import DiagnosticsService
from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_compiler import KnowledgeCompiler
from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager
from .version_store import VersionStore


APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT.parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin"
DRAFTS_ROOT = RUNTIME_ROOT / "drafts"
STRUCTURED_ROOT = APP_ROOT / "data" / "structured"

TARGET_FILES = {
    "manifest": STRUCTURED_ROOT / "manifest.json",
    "product_knowledge": STRUCTURED_ROOT / "product_knowledge.example.json",
    "style_examples": STRUCTURED_ROOT / "style_examples.json",
}


class DraftStore:
    def __init__(self) -> None:
        self.diagnostics = DiagnosticsService()
        self.versions = VersionStore()
        self.registry = KnowledgeRegistry()
        self.schema_manager = KnowledgeSchemaManager(self.registry)
        self.base_store = KnowledgeBaseStore(self.registry, self.schema_manager)
        self.compiler = KnowledgeCompiler()

    def create_draft(self, target_file: str, content: Any, summary: str) -> dict[str, Any]:
        target_path = self.target_path(target_file)
        if content is None:
            content = json.loads(target_path.read_text(encoding="utf-8"))
        draft_id = "draft_" + uuid.uuid4().hex[:12]
        draft = {
            "draft_id": draft_id,
            "target_file": target_file,
            "summary": summary,
            "content": content,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "draft",
        }
        self.write_draft(draft)
        append_audit("draft_created", {"draft_id": draft_id, "target_file": target_file, "summary": summary})
        return {"ok": True, "draft": draft}

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        path = self.draft_path(draft_id)
        if not path.exists():
            raise FileNotFoundError(draft_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def update_draft(self, draft_id: str, content: Any, summary: Any = None) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        if content is not None:
            draft["content"] = content
        if summary is not None:
            draft["summary"] = str(summary)
        draft["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.write_draft(draft)
        append_audit("draft_updated", {"draft_id": draft_id, "target_file": draft.get("target_file")})
        return {"ok": True, "draft": draft}

    def diff(self, draft_id: str) -> list[str]:
        draft = self.get_draft(draft_id)
        target_file = str(draft.get("target_file") or "")
        target_path = self.target_path(target_file)
        original = json.dumps(self.current_target_content(target_file), ensure_ascii=False, indent=2).splitlines()
        updated = json.dumps(draft.get("content"), ensure_ascii=False, indent=2).splitlines()
        return list(difflib.unified_diff(original, updated, fromfile=str(target_path.name), tofile=f"{draft_id}.json", lineterm=""))

    def validate_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        return self.diagnostics.validate_target_content(str(draft.get("target_file") or ""), draft.get("content"))

    def apply_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        target_file = str(draft.get("target_file") or "")
        validation = self.validate_draft(draft_id)
        if not validation.get("ok"):
            return {"ok": False, "message": "validation failed", "validation": validation}
        snapshot = self.versions.create_snapshot("before draft apply", {"draft_id": draft_id, "target_file": target_file})
        self.apply_target_content(target_file, draft.get("content"))
        draft["status"] = "applied"
        draft["applied_at"] = datetime.now().isoformat(timespec="seconds")
        draft["version_snapshot"] = snapshot
        self.write_draft(draft)
        append_audit("knowledge_applied", {"draft_id": draft_id, "target_file": target_file, "version_id": snapshot["version_id"]})
        return {"ok": True, "message": "draft applied", "draft": draft, "snapshot": snapshot}

    def delete_draft(self, draft_id: str) -> dict[str, Any]:
        path = self.draft_path(draft_id)
        if path.exists():
            path.unlink()
        append_audit("draft_deleted", {"draft_id": draft_id})
        return {"ok": True, "message": "draft deleted"}

    def write_draft(self, draft: dict[str, Any]) -> None:
        DRAFTS_ROOT.mkdir(parents=True, exist_ok=True)
        self.draft_path(str(draft["draft_id"])).write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    def draft_path(self, draft_id: str) -> Path:
        return DRAFTS_ROOT / f"{draft_id}.json"

    def target_path(self, target_file: str) -> Path:
        if target_file not in TARGET_FILES:
            raise ValueError(f"unsupported target_file: {target_file}")
        return TARGET_FILES[target_file]

    def current_target_content(self, target_file: str) -> Any:
        compiled = self.compiler.compile()
        if target_file == "manifest":
            return compiled["manifest"]
        if target_file == "product_knowledge":
            return compiled["product_knowledge"]
        if target_file == "style_examples":
            return compiled["style_examples"]
        return json.loads(self.target_path(target_file).read_text(encoding="utf-8"))

    def apply_target_content(self, target_file: str, content: Any) -> None:
        if target_file == "style_examples":
            self.apply_style_examples(content)
            return
        if target_file == "product_knowledge":
            self.apply_product_knowledge(content)
            return
        if target_file == "manifest":
            self.atomic_write_json(self.target_path(target_file), content)
            return
        raise ValueError(f"unsupported target_file: {target_file}")

    def apply_style_examples(self, content: Any) -> None:
        examples = (content or {}).get("examples", []) or []
        active_ids = set()
        for example in examples:
            item_id = safe_id(str(example.get("id") or "style"))
            active_ids.add(item_id)
            result = self.base_store.save_item(
                "chats",
                {
                    "schema_version": 1,
                    "category_id": "chats",
                    "id": item_id,
                    "status": "active",
                    "source": {"type": "admin_draft", "legacy_target": "style_examples"},
                    "data": {
                        "customer_message": example.get("customer_message", ""),
                        "service_reply": example.get("message") or example.get("service_reply", ""),
                        "intent_tags": example.get("intent_tags", []) or [],
                        "tone_tags": example.get("tone_tags", []) or [],
                        "linked_categories": example.get("linked_categories", []) or [],
                        "linked_item_ids": example.get("linked_item_ids", []) or [],
                        "usable_as_template": example.get("usable_as_template", True),
                    },
                    "runtime": {
                        "allow_auto_reply": "handoff" not in (example.get("intent_tags", []) or []),
                        "requires_handoff": "handoff" in (example.get("intent_tags", []) or []),
                        "risk_level": "high" if "handoff" in (example.get("intent_tags", []) or []) else "normal",
                    },
                },
            )
            if not result.get("ok"):
                raise ValueError(f"style item validation failed: {result}")
        for existing in self.base_store.list_items("chats"):
            if str(existing.get("id") or "") not in active_ids:
                self.base_store.archive_item("chats", str(existing.get("id") or ""))

    def apply_product_knowledge(self, content: Any) -> None:
        # Legacy full-file product edits are kept for compatibility. The form API
        # uses category item writes directly; this path handles old JSON drafts.
        for product in (content or {}).get("products", []) or []:
            item_id = safe_id(str(product.get("id") or product.get("name") or "product"))
            result = self.base_store.save_item(
                "products",
                {
                    "schema_version": 1,
                    "category_id": "products",
                    "id": item_id,
                    "status": "active",
                    "source": {"type": "admin_draft", "legacy_target": "product_knowledge"},
                    "data": {
                        "name": product.get("name", ""),
                        "sku": item_id,
                        "category": product.get("category", ""),
                        "aliases": product.get("aliases", []) or [],
                        "specs": product.get("spec", ""),
                        "price": product.get("price"),
                        "unit": product.get("unit", ""),
                        "price_tiers": product.get("discount_tiers", []) or [],
                        "inventory": product.get("stock"),
                        "shipping_policy": combine_text(product.get("lead_time"), product.get("shipping")),
                        "warranty_policy": product.get("warranty", ""),
                        "reply_templates": {
                            "discount_policy": product.get("discount_policy", ""),
                            "notes": product.get("notes", ""),
                        },
                        "risk_rules": [],
                    },
                },
            )
            if not result.get("ok"):
                raise ValueError(f"product item validation failed: {result}")

    def atomic_write_json(self, path: Path, content: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)


def safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    normalized = normalized.strip("._-").lower()
    if not normalized:
        normalized = "item"
    if not re.match(r"^[A-Za-z0-9]", normalized):
        normalized = "item_" + normalized
    return normalized[:120]


def combine_text(*parts: Any) -> str:
    return "\n".join(str(part) for part in parts if part)
