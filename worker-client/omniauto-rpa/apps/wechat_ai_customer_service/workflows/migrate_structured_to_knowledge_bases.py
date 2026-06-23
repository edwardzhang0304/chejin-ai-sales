"""Migrate legacy structured knowledge JSON into category-isolated knowledge bases."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
STRUCTURED_ROOT = APP_ROOT / "data" / "structured"
KNOWLEDGE_BASE_ROOT = APP_ROOT / "data" / "knowledge_bases"
BACKUPS_ROOT = APP_ROOT / "data" / "backups"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_registry import KnowledgeRegistry  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_schema_manager import KnowledgeSchemaManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite existing migrated items")
    args = parser.parse_args()

    result = migrate(apply=args.apply, force=args.force)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def migrate(apply: bool, force: bool = False) -> dict[str, Any]:
    product_knowledge = load_json(STRUCTURED_ROOT / "product_knowledge.example.json")
    style_examples = load_json(STRUCTURED_ROOT / "style_examples.json")
    manifest = load_json(STRUCTURED_ROOT / "manifest.json")
    items = [
        *build_product_items(product_knowledge),
        *build_policy_items(product_knowledge),
        *build_chat_items(style_examples),
    ]
    report: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "force": force,
        "source": {
            "product_knowledge": str(STRUCTURED_ROOT / "product_knowledge.example.json"),
            "style_examples": str(STRUCTURED_ROOT / "style_examples.json"),
            "manifest": str(STRUCTURED_ROOT / "manifest.json"),
            "manifest_scope": manifest.get("scope"),
        },
        "counts": {
            "products": sum(1 for item in items if item["category_id"] == "products"),
            "policies": sum(1 for item in items if item["category_id"] == "policies"),
            "chats": sum(1 for item in items if item["category_id"] == "chats"),
            "total": len(items),
            "written": 0,
            "unchanged": 0,
            "conflicts": 0,
        },
        "items": [{"category_id": item["category_id"], "id": item["id"], "title": item["data"].get("name") or item["data"].get("title") or item["id"]} for item in items],
        "backup": None,
        "conflicts": [],
    }
    if not apply:
        return report

    report["backup"] = create_backup()
    registry = KnowledgeRegistry()
    schema_manager = KnowledgeSchemaManager(registry)
    store = KnowledgeBaseStore(registry, schema_manager)
    for item in items:
        status = write_migrated_item(store, item, force=force)
        if status == "written":
            report["counts"]["written"] += 1
        elif status == "unchanged":
            report["counts"]["unchanged"] += 1
        else:
            report["counts"]["conflicts"] += 1
            report["conflicts"].append(status)
    report["ok"] = report["counts"]["conflicts"] == 0
    return report


def build_product_items(product_knowledge: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for product in product_knowledge.get("products", []) or []:
        item_id = safe_id(str(product.get("id") or product.get("name") or "product"))
        data = {
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
        }
        items.append(make_item("products", item_id, data, "structured/product_knowledge.example.json", allow_auto_reply=True))
    return items


def build_policy_items(product_knowledge: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for key, value in product_knowledge.items():
        if key in {"version", "currency", "products", "faq"}:
            continue
        if not isinstance(value, dict):
            continue
        item_id = safe_id(f"{key}_details")
        data = {
            "title": policy_title(key),
            "policy_type": policy_type_from_key(key),
            "keywords": policy_keywords_from_key(key),
            "answer": json.dumps(value, ensure_ascii=False, indent=2),
            "allow_auto_reply": True,
            "requires_handoff": key in {"after_sales_policy"} and "manual_required" in value,
            "handoff_reason": "manual_required" if key in {"after_sales_policy"} else "",
            "operator_alert": key in {"after_sales_policy"},
            "risk_level": "warning" if key in {"after_sales_policy", "payment_policy"} else "normal",
        }
        items.append(make_item("policies", item_id, data, "structured/product_knowledge.example.json", allow_auto_reply=True))

    for faq in product_knowledge.get("faq", []) or []:
        item_id = safe_id(str(faq.get("intent") or "faq"))
        requires_handoff = bool(faq.get("needs_handoff") or faq.get("operator_alert"))
        data = {
            "title": str(faq.get("intent") or item_id),
            "policy_type": policy_type_from_key(str(faq.get("intent") or "")),
            "keywords": faq.get("keywords", []) or [],
            "answer": faq.get("answer", ""),
            "allow_auto_reply": not requires_handoff,
            "requires_handoff": requires_handoff,
            "handoff_reason": faq.get("reason", ""),
            "operator_alert": bool(faq.get("operator_alert")),
            "risk_level": "high" if requires_handoff else "normal",
        }
        items.append(make_item("policies", item_id, data, "structured/product_knowledge.example.json", allow_auto_reply=not requires_handoff, requires_handoff=requires_handoff))
    return items


def build_chat_items(style_examples: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for example in style_examples.get("examples", []) or []:
        item_id = safe_id(str(example.get("id") or "style"))
        tags = example.get("intent_tags", []) or []
        data = {
            "customer_message": "",
            "service_reply": example.get("message", ""),
            "intent_tags": tags,
            "tone_tags": [],
            "linked_categories": [],
            "linked_item_ids": [],
            "usable_as_template": True,
        }
        items.append(
            make_item(
                "chats",
                item_id,
                data,
                "structured/style_examples.json",
                allow_auto_reply=True,
                requires_handoff="handoff" in tags,
            )
        )
    return items


def make_item(
    category_id: str,
    item_id: str,
    data: dict[str, Any],
    source_path: str,
    allow_auto_reply: bool = True,
    requires_handoff: bool = False,
) -> dict[str, Any]:
    timestamp = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "category_id": category_id,
        "id": item_id,
        "status": "active",
        "source": {"type": "migration", "path": source_path},
        "data": data,
        "runtime": {
            "allow_auto_reply": allow_auto_reply,
            "requires_handoff": requires_handoff,
            "risk_level": "high" if requires_handoff else "normal",
        },
        "metadata": {
            "created_at": timestamp,
            "updated_at": timestamp,
            "created_by": "migration",
            "updated_by": "migration",
        },
    }


def write_migrated_item(store: KnowledgeBaseStore, item: dict[str, Any], force: bool) -> str:
    existing = store.get_item(item["category_id"], item["id"])
    if existing and not force:
        if comparable_item(existing) == comparable_item(item):
            return "unchanged"
        return f"{item['category_id']}/{item['id']}"
    result = store.save_item(item["category_id"], item)
    if not result.get("ok"):
        return f"{item['category_id']}/{item['id']}: {result}"
    return "written"


def comparable_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "category_id": item.get("category_id"),
        "id": item.get("id"),
        "status": item.get("status"),
        "source": item.get("source"),
        "data": item.get("data"),
        "runtime": item.get("runtime"),
    }


def create_backup() -> dict[str, Any]:
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)
    backup_id = "migration_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = BACKUPS_ROOT / backup_id
    suffix = 1
    while backup_root.exists():
        suffix += 1
        backup_root = BACKUPS_ROOT / f"{backup_id}_{suffix}"
    backup_root.mkdir(parents=True)
    if STRUCTURED_ROOT.exists():
        shutil.copytree(STRUCTURED_ROOT, backup_root / "structured")
    review_root = APP_ROOT / "data" / "review_candidates"
    if review_root.exists():
        shutil.copytree(review_root, backup_root / "review_candidates")
    if KNOWLEDGE_BASE_ROOT.exists():
        shutil.copytree(KNOWLEDGE_BASE_ROOT, backup_root / "knowledge_bases_before")
    metadata = {
        "backup_id": backup_root.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": "before structured-to-knowledge-bases migration",
        "structured_path": str(STRUCTURED_ROOT),
        "knowledge_base_path": str(KNOWLEDGE_BASE_ROOT),
    }
    (backup_root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def policy_title(key: str) -> str:
    titles = {
        "company_profile": "公司信息",
        "invoice_policy": "开票政策",
        "payment_policy": "付款政策",
        "logistics_policy": "物流政策",
        "after_sales_policy": "售后政策",
    }
    return titles.get(key, key)


def policy_type_from_key(key: str) -> str:
    lowered = key.lower()
    if "company" in lowered:
        return "company"
    if "invoice" in lowered:
        return "invoice"
    if "payment" in lowered or "bank" in lowered or "credit" in lowered:
        return "payment"
    if "logistics" in lowered or "shipping" in lowered:
        return "logistics"
    if "after_sales" in lowered or "warranty" in lowered:
        return "after_sales"
    if "discount" in lowered:
        return "discount"
    if "sample" in lowered:
        return "sample"
    if "installation" in lowered:
        return "installation"
    if "contract" in lowered:
        return "contract"
    if "manual" in lowered:
        return "manual_required"
    return "other"


def policy_keywords_from_key(key: str) -> list[str]:
    return [part for part in key.replace("_policy", "").split("_") if part]


if __name__ == "__main__":
    raise SystemExit(main())
