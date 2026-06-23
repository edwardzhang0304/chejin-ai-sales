"""Create the three-layer knowledge layout for the WeChat service app."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.knowledge_paths import (
    LEGACY_KNOWLEDGE_BASE_ROOT,
    SHARED_KNOWLEDGE_ROOT,
    tenant_knowledge_base_root,
    tenant_product_item_knowledge_root,
    tenant_root,
)


TENANT_ID = "default"
PRODUCT_SCOPED_POLICY_MAP = {
    "door-lock-installation": ("fl-920", "rules"),
    "after-sales-ap88-noise": ("ap-88", "rules"),
}


def main() -> int:
    result = migrate()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def migrate() -> dict[str, Any]:
    created: list[str] = []
    warnings: list[str] = []
    tenant_kb_root = tenant_knowledge_base_root(TENANT_ID)
    if not (tenant_kb_root / "registry.json").exists():
        if not LEGACY_KNOWLEDGE_BASE_ROOT.exists():
            raise FileNotFoundError(str(LEGACY_KNOWLEDGE_BASE_ROOT))
        tenant_kb_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(LEGACY_KNOWLEDGE_BASE_ROOT, tenant_kb_root)
        created.append(str(tenant_kb_root))

    tenant_meta = tenant_root(TENANT_ID) / "tenant.json"
    if not tenant_meta.exists():
        write_json(
            tenant_meta,
            {
                "schema_version": 1,
                "tenant_id": TENANT_ID,
                "display_name": "Default Demo Tenant",
                "knowledge_base_root": "knowledge_bases",
                "product_item_knowledge_root": "product_item_knowledge",
                "created_at": now(),
            },
        )
        created.append(str(tenant_meta))

    ensure_shared_global_guidelines(tenant_kb_root, created)
    ensure_product_scoped_dirs(tenant_kb_root, created)
    migrate_product_scoped_policies(tenant_kb_root, created, warnings)
    validate_json_tree(SHARED_KNOWLEDGE_ROOT)
    validate_json_tree(tenant_root(TENANT_ID))
    return {
        "ok": True,
        "tenant_id": TENANT_ID,
        "shared_root": str(SHARED_KNOWLEDGE_ROOT),
        "tenant_root": str(tenant_root(TENANT_ID)),
        "created_or_updated": created,
        "warnings": warnings,
    }


def ensure_shared_global_guidelines(tenant_kb_root: Path, created: list[str]) -> None:
    category_root = SHARED_KNOWLEDGE_ROOT / "global_guidelines"
    items_root = category_root / "items"
    items_root.mkdir(parents=True, exist_ok=True)
    write_json(
        SHARED_KNOWLEDGE_ROOT / "registry.json",
        {
            "schema_version": 1,
            "scope": "wechat_ai_customer_service_shared",
            "updated_at": now(),
            "categories": [
                {
                    "id": "global_guidelines",
                    "name": "Global Assistant Guidelines",
                    "kind": "global",
                    "path": "global_guidelines",
                    "enabled": True,
                    "participates_in_reply": True,
                    "participates_in_learning": False,
                    "participates_in_diagnostics": True,
                    "sort_order": 10,
                }
            ],
        },
    )
    write_json(
        category_root / "schema.json",
        {
            "schema_version": 1,
            "category_id": "global_guidelines",
            "display_name": "全局客服准则",
            "description": "所有微信 AI 客服/助理共享的表达风格和安全边界。",
            "item_title_field": "title",
            "item_subtitle_field": "priority",
            "fields": [
                {"id": "title", "label": "准则名称", "type": "short_text", "required": True, "searchable": True, "form_order": 10},
                {"id": "guideline_text", "label": "准则内容", "type": "long_text", "required": True, "searchable": True, "form_order": 20},
                {"id": "intent_tags", "label": "适用意图", "type": "tags", "required": False, "searchable": True, "form_order": 30},
                {"id": "tone_tags", "label": "语气标签", "type": "tags", "required": False, "searchable": True, "form_order": 40},
                {"id": "priority", "label": "优先级", "type": "number", "required": False, "default": 50, "form_order": 50},
                {"id": "always_include", "label": "默认纳入上下文", "type": "boolean", "required": False, "default": False, "form_order": 60},
                {"id": "additional_details", "label": "补充信息", "type": "object", "required": False, "searchable": True, "form_order": 70},
            ],
        },
    )
    write_json(
        category_root / "resolver.json",
        {
            "schema_version": 1,
            "category_id": "global_guidelines",
            "match_fields": ["title", "guideline_text", "intent_tags", "tone_tags", "additional_details"],
            "intent_fields": ["intent_tags"],
            "risk_fields": [],
            "reply_fields": ["guideline_text", "intent_tags", "tone_tags", "additional_details"],
            "minimum_confidence": 0.35,
            "default_action": "global_style_guideline",
        },
    )
    source = tenant_kb_root / "chats" / "items" / "chat_style_guidelines.json"
    guideline_text = (
        "回复要自然，先接住客户使用场景，再问关键参数；只按知识库里的明确事实回答，"
        "对价格、库存、发货、售后、最低价、赔偿、账期等不确定或越权内容不能凭空承诺，"
        "需要请示或人工确认时要简短说明并转人工。"
    )
    details: dict[str, Any] = {"source": "three_layer_migration"}
    if source.exists():
        source_item = read_json(source)
        source_data = source_item.get("data", {}) or {}
        guideline_text = str(source_data.get("service_reply") or guideline_text)
        details["migrated_from"] = "tenant.chats.chat_style_guidelines"
        source_item["status"] = "archived"
        source_item.setdefault("metadata", {})["archived_by"] = "three_layer_migration"
        source_item.setdefault("metadata", {})["archived_at"] = now()
        write_json(source, source_item)
    write_json(
        items_root / "customer_service_style_guidelines.json",
        {
            "schema_version": 1,
            "category_id": "global_guidelines",
            "id": "customer_service_style_guidelines",
            "status": "active",
            "source": {"type": "three_layer_migration"},
            "data": {
                "title": "客服通用表达与边界原则",
                "guideline_text": guideline_text,
                "intent_tags": ["greeting", "small_talk", "scene_product", "quote", "discount", "handoff"],
                "tone_tags": ["natural", "truthful", "safe"],
                "priority": 100,
                "always_include": True,
                "additional_details": details,
            },
            "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
            "metadata": {"updated_at": now(), "updated_by": "three_layer_migration"},
        },
    )
    created.append(str(items_root / "customer_service_style_guidelines.json"))


def ensure_product_scoped_dirs(tenant_kb_root: Path, created: list[str]) -> None:
    products_root = tenant_kb_root / "products" / "items"
    if not products_root.exists():
        return
    for path in products_root.glob("*.json"):
        if path.name == ".gitkeep":
            continue
        product_id = path.stem
        for kind in ("faq", "rules", "explanations"):
            target = tenant_product_item_knowledge_root(TENANT_ID) / product_id / kind
            target.mkdir(parents=True, exist_ok=True)
            gitkeep = target / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text("\n", encoding="utf-8")
                created.append(str(gitkeep))


def migrate_product_scoped_policies(tenant_kb_root: Path, created: list[str], warnings: list[str]) -> None:
    policies_root = tenant_kb_root / "policies" / "items"
    for policy_id, (product_id, kind) in PRODUCT_SCOPED_POLICY_MAP.items():
        source = policies_root / f"{policy_id}.json"
        if not source.exists():
            warnings.append(f"missing product-scoped source policy: {policy_id}")
            continue
        item = read_json(source)
        data = dict(item.get("data", {}) or {})
        data["product_id"] = product_id
        target_item = {
            **item,
            "category_id": f"product_{kind}",
            "id": policy_id,
            "status": "active",
            "source": {**(item.get("source") if isinstance(item.get("source"), dict) else {}), "type": "three_layer_product_scope_migration"},
            "data": data,
        }
        target = tenant_product_item_knowledge_root(TENANT_ID) / product_id / kind / f"{policy_id}.json"
        write_json(target, target_item)
        created.append(str(target))
        item["status"] = "archived"
        item.setdefault("metadata", {})["archived_by"] = "three_layer_product_scope_migration"
        item.setdefault("metadata", {})["archived_at"] = now()
        write_json(source, item)


def validate_json_tree(root: Path) -> None:
    for path in root.rglob("*.json"):
        read_json(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
