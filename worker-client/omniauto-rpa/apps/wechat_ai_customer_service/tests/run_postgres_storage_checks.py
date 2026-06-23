"""Static and optional integration checks for PostgreSQL storage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.storage.config import load_storage_config, validate_schema_name  # noqa: E402
from apps.wechat_ai_customer_service.storage.postgres_store import SCHEMA_SQL_PATH, PostgresJsonStore, search_text  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402


Check = Callable[[], None]


def check_schema_contains_required_tables() -> None:
    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    required = [
        "tenants",
        "knowledge_categories",
        "knowledge_items",
        "review_candidates",
        "uploads",
        "audit_events",
        "version_snapshots",
        "rag_sources",
        "rag_chunks",
        "rag_index_entries",
        "rag_experiences",
        "app_kv",
    ]
    missing = [name for name in required if f".{name}" not in sql]
    assert not missing, f"missing tables in schema: {missing}"


def check_schema_name_validation() -> None:
    validate_schema_name("wechat_ai_customer_service")
    validate_schema_name("tenant_01")
    try:
        validate_schema_name("bad-schema;drop")
    except ValueError:
        return
    raise AssertionError("unsafe schema name should fail")


def check_config_defaults_to_json() -> None:
    old_backend = os.environ.pop("WECHAT_STORAGE_BACKEND", None)
    old_dsn = os.environ.pop("WECHAT_POSTGRES_DSN", None)
    old_mirror = os.environ.pop("WECHAT_POSTGRES_MIRROR_FILES", None)
    try:
        config = load_storage_config()
        assert config.backend == "json"
        assert not config.use_postgres
        assert config.mirror_files is False
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)
        restore_env("WECHAT_POSTGRES_MIRROR_FILES", old_mirror)


def check_postgres_defaults_to_mirror_files() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_mirror = os.environ.pop("WECHAT_POSTGRES_MIRROR_FILES", None)
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    try:
        config = load_storage_config()
        assert config.backend == "postgres"
        assert config.mirror_files is True
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_MIRROR_FILES", old_mirror)


def check_postgres_mirror_files_can_be_disabled() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_mirror = os.environ.get("WECHAT_POSTGRES_MIRROR_FILES")
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    os.environ["WECHAT_POSTGRES_MIRROR_FILES"] = "0"
    try:
        config = load_storage_config()
        assert config.backend == "postgres"
        assert config.mirror_files is False
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_MIRROR_FILES", old_mirror)


def check_search_text_preserves_business_fields() -> None:
    text = search_text(
        {
            "id": "fl-920",
            "category_id": "products",
            "data": {
                "name": "智能指纹门锁 FL-920",
                "sku": "FL-920",
                "aliases": ["民宿门锁"],
                "additional_details": {"安装前": "确认门厚"},
            },
        }
    )
    assert "FL-920" in text
    assert "民宿门锁" in text
    assert "确认门厚" in text


def check_postgres_status_without_dsn_is_clear() -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.pop("WECHAT_POSTGRES_DSN", None)
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    try:
        availability = PostgresJsonStore().availability()
        assert not availability.ok
        assert "DSN" in availability.reason or "DATABASE_URL" in availability.reason
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)


def check_optional_postgres_roundtrip(dsn: str) -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.get("WECHAT_POSTGRES_DSN")
    old_schema = os.environ.get("WECHAT_POSTGRES_SCHEMA")
    schema = "wechat_ai_customer_service_test_" + next(tempfile._get_candidate_names()).replace("-", "_")[:12]
    os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
    os.environ["WECHAT_POSTGRES_DSN"] = dsn
    os.environ["WECHAT_POSTGRES_SCHEMA"] = schema
    try:
        store = PostgresJsonStore(tenant_id="pg_check")
        store.initialize_schema()
        store.upsert_tenant({"tenant_id": "pg_check", "display_name": "Postgres Check"})
        store.upsert_category("pg_check", "tenant", {"id": "products", "name": "商品资料", "enabled": True, "sort_order": 10})
        item = {"id": "demo", "category_id": "products", "status": "active", "data": {"name": "演示商品", "sku": "DEMO"}}
        store.upsert_knowledge_item("pg_check", "tenant", "products", item)
        loaded = store.get_knowledge_item("pg_check", layer="tenant", category_id="products", item_id="demo")
        assert loaded and loaded["data"]["sku"] == "DEMO"
        store.upsert_rag_source({"tenant_id": "pg_check", "source_id": "source_demo", "source_type": "manual", "status": "active"})
        store.replace_rag_chunks("source_demo", [{"tenant_id": "pg_check", "chunk_id": "chunk_demo", "source_id": "source_demo", "text": "演示 RAG chunk", "status": "active"}])
        assert store.counts("pg_check")["rag_chunks"] == 1
    finally:
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)
        restore_env("WECHAT_POSTGRES_SCHEMA", old_schema)


def check_optional_knowledge_store_mirrors_json(dsn: str) -> None:
    old_backend = os.environ.get("WECHAT_STORAGE_BACKEND")
    old_dsn = os.environ.get("WECHAT_POSTGRES_DSN")
    old_schema = os.environ.get("WECHAT_POSTGRES_SCHEMA")
    old_tenant = os.environ.get("WECHAT_KNOWLEDGE_TENANT")
    old_mirror = os.environ.pop("WECHAT_POSTGRES_MIRROR_FILES", None)
    tenant_id = "mirror_check_" + next(tempfile._get_candidate_names()).replace("-", "_")[:8]
    schema = "wechat_ai_customer_service_mirror_" + next(tempfile._get_candidate_names()).replace("-", "_")[:8]
    tenant_root = APP_ROOT / "data" / "tenants" / tenant_id
    kb_root = tenant_root / "knowledge_bases"
    category_root = kb_root / "products"
    item_id = "mirror_demo"
    try:
        os.environ["WECHAT_STORAGE_BACKEND"] = "postgres"
        os.environ["WECHAT_POSTGRES_DSN"] = dsn
        os.environ["WECHAT_POSTGRES_SCHEMA"] = schema
        os.environ["WECHAT_KNOWLEDGE_TENANT"] = tenant_id
        category_root.mkdir(parents=True, exist_ok=True)
        (category_root / "items").mkdir(parents=True, exist_ok=True)
        write_json(
            kb_root / "registry.json",
            {
                "schema_version": 1,
                "categories": [
                    {
                        "id": "products",
                        "name": "Products",
                        "path": "products",
                        "enabled": True,
                        "sort_order": 10,
                    }
                ],
            },
        )
        write_json(
            category_root / "schema.json",
            {
                "schema_version": 1,
                "category_id": "products",
                "display_name": "Products",
                "item_title_field": "name",
                "fields": [
                    {"id": "name", "label": "Name", "type": "short_text", "required": True},
                    {"id": "sku", "label": "SKU", "type": "short_text", "required": False},
                ],
            },
        )
        write_json(
            category_root / "resolver.json",
            {
                "schema_version": 1,
                "category_id": "products",
                "match_fields": ["name", "sku"],
                "reply_fields": ["name"],
            },
        )
        store = PostgresJsonStore(tenant_id=tenant_id)
        store.initialize_schema()
        item = {
            "id": item_id,
            "category_id": "products",
            "status": "active",
            "data": {"name": "Mirror Demo Product", "sku": "MIRROR-1"},
        }
        saved = KnowledgeBaseStore().save_item("products", item)
        assert saved.get("ok"), saved
        json_path = category_root / "items" / f"{item_id}.json"
        assert json_path.exists(), "PostgreSQL writes should mirror to JSON files by default"
        loaded_db = store.get_knowledge_item(tenant_id, layer="tenant", category_id="products", item_id=item_id)
        assert loaded_db and loaded_db["data"]["sku"] == "MIRROR-1"
        loaded_file = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded_file["data"]["sku"] == "MIRROR-1"
    finally:
        if tenant_root.exists():
            shutil.rmtree(tenant_root)
        try:
            PostgresJsonStore(tenant_id=tenant_id).execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        except Exception:
            pass
        restore_env("WECHAT_STORAGE_BACKEND", old_backend)
        restore_env("WECHAT_POSTGRES_DSN", old_dsn)
        restore_env("WECHAT_POSTGRES_SCHEMA", old_schema)
        restore_env("WECHAT_KNOWLEDGE_TENANT", old_tenant)
        restore_env("WECHAT_POSTGRES_MIRROR_FILES", old_mirror)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_checks(checks: list[tuple[str, Check]]) -> dict:
    results = []
    failures = []
    for name, check in checks:
        try:
            check()
            results.append({"name": name, "ok": True})
        except Exception as exc:
            results.append({"name": name, "ok": False, "error": str(exc)})
            failures.append({"name": name, "error": str(exc)})
    return {"ok": not failures, "count": len(checks), "failures": failures, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.getenv("WECHAT_POSTGRES_DSN") or "")
    args = parser.parse_args()
    checks: list[tuple[str, Check]] = [
        ("check_schema_contains_required_tables", check_schema_contains_required_tables),
        ("check_schema_name_validation", check_schema_name_validation),
        ("check_config_defaults_to_json", check_config_defaults_to_json),
        ("check_postgres_defaults_to_mirror_files", check_postgres_defaults_to_mirror_files),
        ("check_postgres_mirror_files_can_be_disabled", check_postgres_mirror_files_can_be_disabled),
        ("check_search_text_preserves_business_fields", check_search_text_preserves_business_fields),
        ("check_postgres_status_without_dsn_is_clear", check_postgres_status_without_dsn_is_clear),
    ]
    if args.dsn:
        checks.append(("check_optional_postgres_roundtrip", lambda: check_optional_postgres_roundtrip(args.dsn)))
        checks.append(("check_optional_knowledge_store_mirrors_json", lambda: check_optional_knowledge_store_mirrors_json(args.dsn)))
    report = run_checks(checks)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
