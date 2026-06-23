"""Compile classified knowledge bases into compatibility artifacts."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.workflows.knowledge_runtime import (
    PRODUCT_SCOPED_KINDS,
    PRODUCT_SCOPED_SCHEMAS,
    KnowledgeRuntime,
)
from apps.wechat_ai_customer_service.product_master import product_master_category_record


APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPILED_ROOT = APP_ROOT / "data" / "compiled" / "structured_compat"


class KnowledgeCompiler:
    """Export the formal category knowledge source into legacy-shaped files."""

    def __init__(self, runtime: KnowledgeRuntime | None = None, output_root: Path | None = None) -> None:
        self.runtime = runtime or KnowledgeRuntime()
        self.output_root = output_root or DEFAULT_COMPILED_ROOT

    def compile(self) -> dict[str, Any]:
        products = [compile_product(item) for item in self.runtime.list_items("products")]
        policy_items = self.runtime.list_items("policies")
        chats = self.runtime.list_items("chats")
        product_scoped = [compile_product_scoped_faq(item) for _category_id, _schema, _resolver, item in self.runtime.iter_all_product_scoped_items()]
        global_guidelines = self.runtime.list_items("global_guidelines")
        product_knowledge = {
            "version": "compiled-from-knowledge-bases",
            "currency": "CNY",
            "products": products,
            "faq": [
                *[compile_faq(item) for item in policy_items if not str(item.get("id") or "").endswith("_details")],
                *[item for item in product_scoped if item],
            ],
        }
        for item in policy_items:
            item_id = str(item.get("id") or "")
            if item_id.endswith("_details"):
                key = item_id.removesuffix("_details")
                product_knowledge[key] = parse_policy_details(item)
        style_examples = {
            "schema_version": 1,
            "examples": [*[compile_chat_example(item) for item in chats], *[compile_global_guideline(item) for item in global_guidelines]],
        }
        manifest = compile_manifest(self.runtime)
        return {
            "manifest": manifest,
            "product_knowledge": product_knowledge,
            "style_examples": style_examples,
            "metadata": compile_metadata(self.runtime, product_knowledge, style_examples),
        }

    def compile_to_disk(self) -> dict[str, Any]:
        compiled = self.compile()
        self.output_root.mkdir(parents=True, exist_ok=True)
        write_json(self.output_root / "manifest.json", compiled["manifest"])
        write_json(self.output_root / "product_knowledge.example.json", compiled["product_knowledge"])
        write_json(self.output_root / "style_examples.json", compiled["style_examples"])
        write_json(self.output_root / "metadata.json", compiled["metadata"])
        return {
            "ok": True,
            "output_root": str(self.output_root),
            "files": {
                "manifest": str(self.output_root / "manifest.json"),
                "product_knowledge": str(self.output_root / "product_knowledge.example.json"),
                "style_examples": str(self.output_root / "style_examples.json"),
                "metadata": str(self.output_root / "metadata.json"),
            },
            "counts": compiled["metadata"]["counts"],
        }


def compile_product(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    templates = data.get("reply_templates", {}) or {}
    return compact_dict(
        {
            "id": item.get("id"),
            "name": data.get("name"),
            "category": data.get("category"),
            "aliases": data.get("aliases", []) or [],
            "spec": data.get("specs", ""),
            "price": data.get("price"),
            "unit": data.get("unit"),
            "stock": data.get("inventory"),
            "lead_time": first_line(data.get("shipping_policy")),
            "shipping": clean_multiline_text(data.get("shipping_policy")),
            "warranty": data.get("warranty_policy"),
            "discount_policy": templates.get("discount_policy", ""),
            "discount_tiers": data.get("price_tiers", []) or [],
            "notes": combine_text(templates.get("notes", ""), display_details(data.get("additional_details"))),
        }
    )


def compile_faq(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    runtime = item.get("runtime", {}) or {}
    data_has_auto_reply = "allow_auto_reply" in data
    data_has_handoff = "requires_handoff" in data
    explicit_auto_reply = data.get("allow_auto_reply") if data_has_auto_reply else runtime.get("allow_auto_reply", True)
    explicit_requires_handoff = data.get("requires_handoff") if data_has_handoff else runtime.get("requires_handoff", False)
    runtime_allows_auto_reply = runtime.get("allow_auto_reply", True) is not False or data_has_auto_reply
    auto_reply_allowed = bool(explicit_auto_reply is not False and runtime_allows_auto_reply)
    requires_handoff = bool(explicit_requires_handoff or not auto_reply_allowed)
    return compact_dict(
        {
            "intent": item.get("id") or data.get("policy_type"),
            "keywords": data.get("keywords", []) or [],
            "answer": data.get("answer", ""),
            "priority": 100 if requires_handoff else 50,
            "needs_handoff": requires_handoff,
            "auto_reply_allowed": auto_reply_allowed,
            "operator_alert": bool(data.get("operator_alert", False) or runtime.get("operator_alert", False) or not auto_reply_allowed),
            "reason": data.get("handoff_reason") or ("auto_reply_disabled" if not auto_reply_allowed else ""),
            "details": display_details(data.get("additional_details")),
        }
    )


def compile_product_scoped_faq(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    answer = data.get("answer") or data.get("content") or ""
    if not answer:
        return {}
    auto_reply_allowed = bool(data.get("allow_auto_reply", True) is not False)
    requires_handoff = bool(data.get("requires_handoff", False) or not auto_reply_allowed)
    return compact_dict(
        {
            "intent": item.get("id"),
            "keywords": [*list(data.get("keywords", []) or []), data.get("product_id"), data.get("title")],
            "answer": answer,
            "priority": 100 if requires_handoff else 70,
            "needs_handoff": requires_handoff,
            "auto_reply_allowed": auto_reply_allowed,
            "operator_alert": bool(data.get("operator_alert", False) or requires_handoff),
            "reason": data.get("handoff_reason", ""),
            "details": display_details(data.get("additional_details")),
        }
    )


def parse_policy_details(item: dict[str, Any]) -> Any:
    answer = (item.get("data", {}) or {}).get("answer", "")
    if isinstance(answer, str) and answer.strip().startswith("{"):
        try:
            return json.loads(answer)
        except json.JSONDecodeError:
            return answer
    return answer


def compile_chat_example(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    return compact_dict(
        {
            "id": item.get("id"),
            "message": data.get("service_reply", ""),
            "intent_tags": data.get("intent_tags", []) or [],
            "tone_tags": data.get("tone_tags", []) or [],
            "customer_message": data.get("customer_message", ""),
            "details": display_details(data.get("additional_details")),
        }
    )


def compile_global_guideline(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    return compact_dict(
        {
            "id": item.get("id"),
            "message": data.get("guideline_text") or data.get("service_reply") or "",
            "intent_tags": data.get("intent_tags", []) or [],
            "tone_tags": data.get("tone_tags", []) or [],
            "customer_message": data.get("title", ""),
            "details": display_details(data.get("additional_details")),
        }
    )


def compile_manifest(runtime: KnowledgeRuntime) -> dict[str, Any]:
    categories = runtime.list_categories(enabled_only=True)
    product_master_category = product_master_category_record()
    product_scoped_categories = [
        {
            "id": category_id,
            "path": f"product_item_knowledge/*/{kind}",
            "name": PRODUCT_SCOPED_SCHEMAS[category_id].get("display_name") or category_id,
            "participates_in_reply": True,
        }
        for kind, category_id in PRODUCT_SCOPED_KINDS.items()
    ]
    return {
        "schema_version": 1,
        "scope": "wechat_ai_customer_service",
        "source": "product_master_plus_formal_knowledge",
        "compiled_at": now(),
        "items": [
            {
                "id": product_master_category.get("id"),
                "path": product_master_category.get("path"),
                "summary": product_master_category.get("name"),
                "scope": product_master_category.get("scope"),
                "participates_in_reply": True,
            },
            *[
                {
                    "id": category.get("id"),
                    "path": category.get("path"),
                    "summary": category.get("name"),
                    "participates_in_reply": bool(category.get("participates_in_reply", False)),
                }
                for category in [*categories, *product_scoped_categories]
            ],
        ],
    }


def compile_metadata(runtime: KnowledgeRuntime, product_knowledge: dict[str, Any], style_examples: dict[str, Any]) -> dict[str, Any]:
    category_counts = {category["id"]: len(runtime.list_items(str(category["id"]))) for category in runtime.list_categories(enabled_only=True)}
    for category_id in PRODUCT_SCOPED_SCHEMAS:
        category_counts.setdefault(category_id, 0)
    for category_id, _schema, _resolver, _item in runtime.iter_all_product_scoped_items():
        category_counts[category_id] = category_counts.get(category_id, 0) + 1
    return {
        "schema_version": 1,
        "compiled_at": now(),
        "source_root": str(runtime.root),
        "counts": {
            "categories": len(category_counts),
            "category_items": category_counts,
            "products": len(product_knowledge.get("products", []) or []),
            "faq": len(product_knowledge.get("faq", []) or []),
            "style_examples": len(style_examples.get("examples", []) or []),
        },
        "note": "Compatibility export only. Product facts come from product_master; formal runtime source is shared_knowledge plus tenant knowledge_bases and product_item_knowledge.",
    }


def first_line(value: Any) -> str:
    text = clean_multiline_text(value)
    return text.splitlines()[0] if text else ""


def display_details(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    return "；".join(f"{key}: {inner}" for key, inner in value.items() if inner not in (None, "", [], {}))


def combine_text(*parts: Any) -> str:
    return clean_multiline_text("\n".join(str(part) for part in parts if part not in (None, "", [], {})))


def clean_multiline_text(value: Any) -> str:
    lines = []
    seen = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def write_json(path: Path, payload: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
