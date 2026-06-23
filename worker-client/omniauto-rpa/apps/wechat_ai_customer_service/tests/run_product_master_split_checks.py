"""Focused checks for the product-master/formal-knowledge split."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_compiler import KnowledgeCompiler  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_schema_manager import KnowledgeSchemaManager  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.source_authority_policy import (  # noqa: E402
    evaluate_candidate_source_authority,
    evaluate_experience_source_authority,
)
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_knowledge_base_root, tenant_product_master_root, tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.product_master import (  # noqa: E402
    PRODUCT_MASTER_CATEGORY_ID,
    ProductMasterStore,
    product_master_category_record,
)
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime  # noqa: E402


TENANT_ID = "product_split_iso_20260516"


def main() -> int:
    cleanup()
    try:
        with tenant_context(TENANT_ID):
            setup_legacy_product_category()
            results = [
                check_read_only_product_master_does_not_rewrite_manifest(),
                check_migration_copies_legacy_product_master(),
                check_store_writes_new_products_to_product_master_only(),
                check_runtime_reads_product_master_but_excludes_formal_reply_iteration(),
                check_schema_and_category_contract(),
                check_compiler_uses_product_master(),
                check_rag_and_candidate_product_master_guards(),
            ]
        payload = {"ok": all(item["ok"] for item in results), "count": len(results), "results": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    finally:
        cleanup()


def setup_legacy_product_category() -> None:
    root = tenant_knowledge_base_root(TENANT_ID)
    product_root = root / "products"
    (product_root / "items").mkdir(parents=True, exist_ok=True)
    registry = {
        "schema_version": 1,
        "scope": "wechat_ai_customer_service",
        "categories": [
            {
                "id": "products",
                "name": "商品资料",
                "kind": "builtin",
                "path": "products",
                "enabled": True,
                "participates_in_reply": True,
                "participates_in_learning": True,
                "participates_in_diagnostics": True,
                "sort_order": 10,
            },
            {
                "id": "chats",
                "name": "聊天记录与话术",
                "kind": "builtin",
                "path": "chats",
                "enabled": True,
                "participates_in_reply": True,
                "participates_in_learning": True,
                "participates_in_diagnostics": True,
                "sort_order": 20,
            },
        ],
    }
    write_json(root / "registry.json", registry)
    write_json(product_root / "schema.json", ProductMasterStore(tenant_id=TENANT_ID).load_schema())
    write_json(product_root / "resolver.json", ProductMasterStore(tenant_id=TENANT_ID).load_resolver())
    write_json(product_root / "items" / "legacy_car_001.json", product_item("legacy_car_001", "Legacy Camry", "LEG-CAMRY"))
    chat_root = root / "chats"
    (chat_root / "items").mkdir(parents=True, exist_ok=True)
    write_json(
        chat_root / "schema.json",
        {
            "schema_version": 1,
            "category_id": "chats",
            "display_name": "聊天",
            "item_title_field": "customer_message",
            "fields": [
                {"id": "customer_message", "label": "客户问题", "type": "long_text", "required": True},
                {"id": "service_reply", "label": "客服回复", "type": "long_text", "required": True},
            ],
        },
    )
    write_json(
        chat_root / "resolver.json",
        {
            "schema_version": 1,
            "category_id": "chats",
            "match_fields": ["customer_message", "service_reply"],
            "intent_fields": [],
            "risk_fields": [],
            "reply_fields": ["service_reply"],
            "minimum_confidence": 0.3,
            "default_action": "answer_from_evidence",
        },
    )
    write_json(
        chat_root / "items" / "chat_probe.json",
        {
            "schema_version": 1,
            "category_id": "chats",
            "id": "chat_probe",
            "status": "active",
            "data": {"customer_message": "预算十万", "service_reply": "先看车况透明的。"},
        },
    )


def check_read_only_product_master_does_not_rewrite_manifest() -> dict[str, Any]:
    store = ProductMasterStore(tenant_id=TENANT_ID)
    manifest_path = tenant_product_master_root(TENANT_ID) / "manifest.json"
    before = manifest_path.read_text(encoding="utf-8")
    store.load_schema()
    store.load_resolver()
    store.list_items(include_archived=True)
    after = manifest_path.read_text(encoding="utf-8")
    assert_equal(after, before, "read-only product master calls must not refresh manifest timestamps")
    return {"name": "read_only_product_master_does_not_rewrite_manifest", "ok": True}


def check_migration_copies_legacy_product_master() -> dict[str, Any]:
    store = ProductMasterStore(tenant_id=TENANT_ID)
    result = store.migrate_from_legacy()
    product_path = tenant_product_master_root(TENANT_ID) / "items" / "legacy_car_001.json"
    assert_true(product_path.exists(), "migrated product should exist in product_master")
    assert_true((tenant_knowledge_base_root(TENANT_ID) / "products" / "items" / "legacy_car_001.json").exists(), "legacy product should remain as rollback evidence")
    assert_true("legacy_car_001" in result.get("copied", []), "migration should report copied product")
    return {"name": "migration_copies_legacy_product_master", "ok": True}


def check_store_writes_new_products_to_product_master_only() -> dict[str, Any]:
    store = KnowledgeBaseStore()
    item = product_item("new_master_only", "New Master Product", "NMP-001")
    result = store.save_item("products", item)
    assert_true(result.get("ok"), f"save product failed: {result}")
    assert_true((tenant_product_master_root(TENANT_ID) / "items" / "new_master_only.json").exists(), "new product should be written to product_master")
    assert_true(not (tenant_knowledge_base_root(TENANT_ID) / "products" / "items" / "new_master_only.json").exists(), "new product must not be written to legacy formal products")
    listed_ids = {str(item.get("id") or "") for item in store.list_items("products", include_archived=True)}
    assert_true({"legacy_car_001", "new_master_only"}.issubset(listed_ids), "store should list product_master items")
    return {"name": "store_writes_new_products_to_product_master_only", "ok": True}


def check_runtime_reads_product_master_but_excludes_formal_reply_iteration() -> dict[str, Any]:
    runtime = KnowledgeRuntime(tenant_id=TENANT_ID)
    product_ids = {str(item.get("id") or "") for item in runtime.list_items("products", include_unacknowledged=True)}
    assert_true("new_master_only" in product_ids, "runtime should read product master")
    reply_categories = {str(category.get("id") or "") for category, _schema, _resolver, _item in runtime.iter_reply_items()}
    assert_true("products" not in reply_categories, "product master must not be iterated as generic formal reply knowledge")
    assert_true("chats" in reply_categories, "formal chat category should still participate in reply iteration")
    return {"name": "runtime_reads_product_master_but_excludes_formal_reply_iteration", "ok": True}


def check_schema_and_category_contract() -> dict[str, Any]:
    category = product_master_category_record()
    assert_equal(category.get("scope"), "product_master", "product category should expose product_master scope")
    assert_true(category.get("participates_in_learning") is False, "product master should not participate in generic learning")
    schema = KnowledgeSchemaManager().load_schema("products")
    assert_equal(schema.get("category_id"), "products", "product schema category")
    assert_true(bool(schema.get("fields")), "product schema should expose fields")
    return {"name": "schema_and_category_contract", "ok": True}


def check_compiler_uses_product_master() -> dict[str, Any]:
    compiled = KnowledgeCompiler(runtime=KnowledgeRuntime(tenant_id=TENANT_ID)).compile()
    ids = {str(item.get("id") or "") for item in compiled.get("product_knowledge", {}).get("products", [])}
    assert_true("new_master_only" in ids, "compiled compatibility product knowledge should include product_master products")
    manifest_items = compiled.get("manifest", {}).get("items", [])
    product_manifest = next((item for item in manifest_items if item.get("id") == "products"), {})
    assert_equal(product_manifest.get("scope"), "product_master", "compiled manifest should label product scope")
    return {"name": "compiler_uses_product_master", "ok": True}


def check_rag_and_candidate_product_master_guards() -> dict[str, Any]:
    candidate = {
        "source": {"type": "rag_experience"},
        "proposal": {
            "formal_patch": {
                "target_category": "products",
                "item": product_item("blocked_candidate_product", "Blocked", "BLOCKED"),
            }
        },
    }
    candidate_decision = evaluate_candidate_source_authority(candidate)
    assert_true(not candidate_decision.get("allowed"), "candidate to product master should be blocked")
    assert_equal(candidate_decision.get("reason"), "product_master_manual_intake_only", "candidate block reason")
    experience_decision = evaluate_experience_source_authority(
        {
            "source_type": "raw_wechat_private",
            "summary": "客户说这台车9.8万还有库存",
            "reply_text": "可以看",
        },
        "products",
    )
    assert_true(not experience_decision.get("allowed"), "RAG promotion to product master should be blocked")
    assert_equal(experience_decision.get("reason"), "rag_product_master_promotion_disabled", "RAG block reason")
    return {"name": "rag_and_candidate_product_master_guards", "ok": True}


def product_item(item_id: str, name: str, sku: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category_id": "products",
        "id": item_id,
        "status": "active",
        "source": {"type": "test_fixture", "test": "product_master_split"},
        "data": {
            "name": name,
            "sku": sku,
            "category": "二手车/测试",
            "aliases": [name, sku],
            "specs": "测试规格",
            "price": 9.8,
            "unit": "台",
            "inventory": 1,
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
        "metadata": {"created_at": datetime.now().isoformat(timespec="seconds"), "updated_at": datetime.now().isoformat(timespec="seconds")},
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cleanup() -> None:
    root = tenant_root(TENANT_ID).resolve()
    expected_parent = (PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "data" / "tenants").resolve()
    if root.exists() and expected_parent in root.parents and root.name == TENANT_ID:
        shutil.rmtree(root)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
