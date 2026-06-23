"""Structured knowledge file access for the Web admin console."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .knowledge_base_store import KnowledgeBaseStore, product_scoped_category_records
from .knowledge_compiler import KnowledgeCompiler
from .raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.knowledge_paths import (
    SHARED_KNOWLEDGE_ROOT,
    TENANTS_ROOT,
    default_admin_knowledge_base_root,
    tenant_product_item_knowledge_root,
    tenant_product_master_root,
    tenant_raw_inbox_root,
    tenant_review_candidates_root,
)
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime


APP_ROOT = Path(__file__).resolve().parents[2]
STRUCTURED_ROOT = APP_ROOT / "data" / "structured"
KNOWLEDGE_BASE_ROOT = default_admin_knowledge_base_root()
PROMPTS_ROOT = APP_ROOT / "prompts"


class KnowledgeStore:
    def __init__(self, app_root: Path | None = None) -> None:
        self.app_root = app_root or APP_ROOT
        self.structured_root = self.app_root / "data" / "structured"
        self.knowledge_base_root = default_admin_knowledge_base_root()
        self.shared_knowledge_root = SHARED_KNOWLEDGE_ROOT
        self.tenants_root = TENANTS_ROOT
        self.product_master_root = tenant_product_master_root()
        self.product_item_knowledge_root = tenant_product_item_knowledge_root()
        self.review_root = tenant_review_candidates_root()
        self.raw_inbox_root = tenant_raw_inbox_root()
        self.prompts_root = self.app_root / "prompts"
        self.runtime = KnowledgeRuntime()
        self.compiler = KnowledgeCompiler(runtime=self.runtime)
        self._compiled_cache: dict[str, Any] | None = None

    @property
    def manifest_path(self) -> Path:
        return self.structured_root / "manifest.json"

    @property
    def product_knowledge_path(self) -> Path:
        return self.structured_root / "product_knowledge.example.json"

    @property
    def style_examples_path(self) -> Path:
        return self.structured_root / "style_examples.json"

    def overview(self) -> dict[str, Any]:
        categories = self.runtime.list_categories(enabled_only=True)
        base_store = KnowledgeBaseStore()
        category_counts: dict[str, int] = {}
        for category_id in ("products", "chats", "policies", "erp_exports"):
            try:
                category_counts[category_id] = len(base_store.list_items(category_id, include_archived=False))
            except FileNotFoundError:
                category_counts[category_id] = 0
        product_scoped_counts: dict[str, int] = {}
        for category in product_scoped_category_records():
            category_id = str(category.get("id") or "")
            if not category_id:
                continue
            try:
                product_scoped_counts[category_id] = len(base_store.list_items(category_id, include_archived=False))
            except FileNotFoundError:
                product_scoped_counts[category_id] = 0
        product_master_count = category_counts.get("products", 0)
        chat_count = category_counts.get("chats", 0)
        policy_count = category_counts.get("policies", 0)
        erp_count = category_counts.get("erp_exports", 0)
        product_scoped_total = sum(product_scoped_counts.values())
        formal_knowledge_total = chat_count + policy_count + erp_count + product_scoped_total
        knowledge_total = product_master_count + formal_knowledge_total
        reply_eligible_product_count = len(self.runtime.list_items("products"))
        product_pending_review = max(0, product_master_count - reply_eligible_product_count)
        pending_count = count_visible_pending_candidates(self.review_root / "pending")
        approved_count = count_json_files(self.review_root / "approved")
        rejected_count = count_json_files(self.review_root / "rejected")
        raw_file_count = count_supported_files(self.raw_inbox_root)
        raw_message_summary = RawMessageStore().summary()
        new_knowledge_count = self.count_new_knowledge_items()
        return {
            "ok": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "version": "knowledge-center-overview-v2",
            "scope": "wechat_ai_customer_service",
            "counts": {
                "categories": len(categories),
                "knowledge_total": knowledge_total,
                "formal_knowledge_total": formal_knowledge_total,
                "product_master": product_master_count,
                "reply_eligible_products": reply_eligible_product_count,
                "product_pending_review": product_pending_review,
                # Backward-compatible keys retained for older checks and clients.
                "products": product_master_count,
                "faqs": product_scoped_counts.get("product_faq", 0),
                "policies": policy_count,
                "style_examples": chat_count,
                "chats": chat_count,
                "erp_exports": erp_count,
                "product_scoped_knowledge": product_scoped_total,
                "product_faq": product_scoped_counts.get("product_faq", 0),
                "product_rules": product_scoped_counts.get("product_rules", 0),
                "product_explanations": product_scoped_counts.get("product_explanations", 0),
                "pending_candidates": pending_count,
                "approved_candidates": approved_count,
                "rejected_candidates": rejected_count,
                "raw_files": raw_file_count,
                "raw_conversations": raw_message_summary.get("conversation_count", 0),
                "raw_messages": raw_message_summary.get("message_count", 0),
                "raw_message_batches": raw_message_summary.get("batch_count", 0),
                "new_knowledge": new_knowledge_count,
            },
            "paths": {
                "knowledge_base_root": str(self.knowledge_base_root),
                "product_master_root": str(self.product_master_root),
                "shared_knowledge_root": str(self.shared_knowledge_root),
                "tenants_root": str(self.tenants_root),
                "product_item_knowledge_root": str(self.product_item_knowledge_root),
                "structured_root": str(self.structured_root),
                "raw_inbox_root": str(self.raw_inbox_root),
                "review_root": str(self.review_root),
            },
            "updated_at": {
                "knowledge_bases": newest_mtime(self.knowledge_base_root),
                "product_master": newest_mtime(self.product_master_root),
                "shared_knowledge": newest_mtime(self.shared_knowledge_root),
                "product_item_knowledge": newest_mtime(self.product_item_knowledge_root),
                "product_knowledge": file_mtime(self.product_knowledge_path),
                "style_examples": file_mtime(self.style_examples_path),
                "manifest": file_mtime(self.manifest_path),
            },
        }

    def count_new_knowledge_items(self) -> int:
        base_store = KnowledgeBaseStore()
        category_ids = [str(item.get("id") or "") for item in self.runtime.list_categories(enabled_only=True)]
        category_ids.extend(str(item.get("id") or "") for item in product_scoped_category_records())
        count = 0
        for category_id in sorted(set(item for item in category_ids if item)):
            try:
                items = base_store.list_items(category_id, include_archived=False)
            except FileNotFoundError:
                continue
            for item in items:
                review_state = item.get("review_state") if isinstance(item.get("review_state"), dict) else {}
                if review_state.get("is_new"):
                    count += 1
        return count

    def manifest(self) -> dict[str, Any]:
        return self._compiled()["manifest"]

    def product_knowledge(self) -> dict[str, Any]:
        return self._compiled()["product_knowledge"]

    def style_data(self) -> dict[str, Any]:
        return self._compiled()["style_examples"]

    def _compiled(self) -> dict[str, Any]:
        # Compile once per request-scoped store instance to avoid repeated full scans.
        if self._compiled_cache is None:
            self._compiled_cache = self.compiler.compile()
        return self._compiled_cache

    def products(self) -> list[dict[str, Any]]:
        return list(self.product_knowledge().get("products", []) or [])

    def product(self, product_id: str) -> dict[str, Any] | None:
        for item in self.products():
            if str(item.get("id") or "") == product_id:
                return item
        return None

    def faqs(self) -> list[dict[str, Any]]:
        return list(self.product_knowledge().get("faq", []) or [])

    def policies(self) -> dict[str, Any]:
        knowledge = self.product_knowledge()
        excluded = {"version", "currency", "products", "faq"}
        return {key: value for key, value in knowledge.items() if key not in excluded}

    def styles(self) -> list[dict[str, Any]]:
        return list(self.style_data().get("examples", []) or [])

    def persona(self) -> dict[str, Any]:
        prompt_files = {}
        for path in sorted(self.prompts_root.glob("*.md")):
            prompt_files[path.stem] = path.read_text(encoding="utf-8")
        return {"prompt_files": prompt_files}

    def raw_json(self, file_key: str) -> dict[str, Any]:
        path = {
            "manifest": self.manifest_path,
            "product_knowledge": self.product_knowledge_path,
            "style_examples": self.style_examples_path,
        }[file_key]
        return load_json(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def newest_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    latest = path.stat().st_mtime
    for item in path.rglob("*"):
        if item.is_file():
            latest = max(latest, item.stat().st_mtime)
    return datetime.fromtimestamp(latest).isoformat(timespec="seconds")


def count_json_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.glob("*.json") if item.is_file())


def count_visible_pending_candidates(path: Path) -> int:
    return count_json_files(path)


def count_supported_files(path: Path) -> int:
    if not path.exists():
        return 0
    suffixes = {".txt", ".md", ".json", ".csv", ".xlsx", ".docx", ".pdf"}
    return sum(1 for item in path.rglob("*") if item.is_file() and item.suffix.lower() in suffixes)
