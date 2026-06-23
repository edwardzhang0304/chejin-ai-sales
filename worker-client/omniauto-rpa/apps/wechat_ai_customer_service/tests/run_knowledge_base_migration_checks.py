"""Checks for the classified knowledge-base structure and migration path."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "knowledge_base_checks"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_compiler import KnowledgeCompiler  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_registry import KnowledgeRegistry  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_schema_manager import KnowledgeSchemaManager  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import default_admin_knowledge_base_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime  # noqa: E402


def main() -> int:
    results = []
    try:
        for check in CHECKS:
            try:
                check()
                results.append({"name": check.__name__, "ok": True})
            except Exception as exc:
                results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
                break
        failures = [item for item in results if not item["ok"]]
        print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    finally:
        cleanup()


def check_default_categories_are_isolated() -> None:
    registry = KnowledgeRegistry()
    categories = registry.list_categories()
    ids = [item["id"] for item in categories]
    assert_equal(ids[:3], ["chats", "policies", "erp_exports"], "formal category order")
    assert_true("products" not in ids, "products should live in product_master, not formal knowledge registry")
    for category_id in ids:
        root = registry.category_root(category_id)
        assert_true((root / "schema.json").exists(), f"{category_id} schema should exist")
        assert_true((root / "resolver.json").exists(), f"{category_id} resolver should exist")
        assert_true((root / "items").is_dir(), f"{category_id} items dir should exist")
        assert_true(root.name == category_id or root.parent.name == "custom", f"{category_id} should have isolated root")


def check_schema_and_resolver_are_valid() -> None:
    registry = KnowledgeRegistry()
    manager = KnowledgeSchemaManager(registry)
    for category in registry.list_categories():
        category_id = category["id"]
        schema = manager.load_schema(category_id)
        resolver = manager.load_resolver(category_id)
        assert_equal(schema["category_id"], category_id, f"{category_id} schema category")
        assert_equal(resolver["category_id"], category_id, f"{category_id} resolver category")
        validation = manager.validate_schema(category_id, schema)
        assert_true(validation["ok"], f"{category_id} schema should validate: {validation}")


def check_custom_category_creation_and_item_storage() -> None:
    root = prepare_temp_knowledge_root()
    registry = KnowledgeRegistry(root=root)
    schema_manager = KnowledgeSchemaManager(registry)
    store = KnowledgeBaseStore(registry, schema_manager)
    category = registry.create_custom_category(
        "admin_check_custom",
        "测试自定义门类",
        "Only used by automated checks.",
        participates_in_reply=True,
    )
    assert_equal(category["path"], "custom/admin_check_custom", "custom path")
    category_root = registry.category_root("admin_check_custom")
    assert_true((category_root / "schema.json").exists(), "custom schema should exist")
    assert_true((category_root / "resolver.json").exists(), "custom resolver should exist")
    assert_true((category_root / "items").is_dir(), "custom items should exist")

    saved = store.save_item(
        "admin_check_custom",
        {
            "schema_version": 1,
            "category_id": "admin_check_custom",
            "id": "custom_item_1",
            "data": {"title": "测试条目", "content": "自定义门类内容"},
        },
    )
    assert_true(saved["ok"], f"custom item should save: {saved}")
    listed = store.list_items("admin_check_custom")
    assert_equal(len(listed), 1, "custom item count")
    assert_equal(listed[0]["data"]["title"], "测试条目", "custom item content")
    archived = store.archive_item("admin_check_custom", "custom_item_1")
    assert_true(archived["ok"], f"custom item should archive: {archived}")
    assert_equal(store.list_items("admin_check_custom"), [], "archived item should be hidden by default")


def check_migrated_content_counts() -> None:
    compiled = KnowledgeCompiler().compile()
    structured = compiled["product_knowledge"]
    styles = compiled["style_examples"]
    store = KnowledgeBaseStore()
    runtime = KnowledgeRuntime()
    product_count = len(structured.get("products", []) or [])
    faq_count = len(structured.get("faq", []) or [])
    style_count = len(styles.get("examples", []) or [])
    assert_equal(len(store.list_items("products")), product_count, "migrated product count")
    expected_faq_count = len([item for item in store.list_items("policies") if not str(item.get("id") or "").endswith("_details")])
    expected_faq_count += len(list(runtime.iter_all_product_scoped_items()))
    expected_style_count = len(runtime.list_items("chats")) + len(runtime.list_items("global_guidelines"))
    assert_equal(faq_count, expected_faq_count, "compiled policy and product-scoped FAQ count")
    assert_equal(style_count, expected_style_count, "compiled chat and global guideline count")
    fridge = store.get_item("products", "commercial_fridge_bx_200")
    assert_true(bool(fridge), "commercial fridge should migrate")
    assert_true("aliases" in fridge.get("data", {}), "migrated product should keep aliases")
    invoice = store.get_item("policies", "invoice")
    assert_true(bool(invoice), "invoice FAQ should migrate to policies")
    style = store.get_item("chats", "quote_detail_request")
    assert_true(bool(style), "style example should migrate to chats")


def prepare_temp_knowledge_root() -> Path:
    source = default_admin_knowledge_base_root()
    cleanup()
    shutil.copytree(source, TEST_ROOT)
    return TEST_ROOT


def cleanup() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


CHECKS = [
    check_default_categories_are_isolated,
    check_schema_and_resolver_are_valid,
    check_custom_category_creation_and_item_storage,
    check_migrated_content_counts,
]


if __name__ == "__main__":
    raise SystemExit(main())
