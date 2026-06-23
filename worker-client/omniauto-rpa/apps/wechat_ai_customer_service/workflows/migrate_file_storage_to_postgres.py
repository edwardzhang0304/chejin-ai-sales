"""Migrate existing JSON/file storage into PostgreSQL.

The migration is idempotent and keeps all source files in place. Without a DSN
or in --dry-run mode it only reports what would be imported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import product_scoped_category_records  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    SHARED_KNOWLEDGE_ROOT,
    TENANTS_ROOT,
    active_tenant_id,
    tenant_knowledge_base_root,
    tenant_product_item_knowledge_root,
    tenant_rag_chunks_root,
    tenant_rag_index_root,
    tenant_rag_sources_root,
    tenant_root,
)
from apps.wechat_ai_customer_service.product_master import (  # noqa: E402
    PRODUCT_MASTER_CATEGORY_ID,
    PRODUCT_MASTER_DB_LAYER,
    ProductMasterStore,
    product_master_category_record,
)
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import PRODUCT_SCOPED_KINDS  # noqa: E402


APP_DATA_ROOT = APP_ROOT / "data"
RUNTIME_ADMIN_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin"
PRODUCT_KIND_TO_CATEGORY = {kind: category for kind, category in PRODUCT_SCOPED_KINDS.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WeChat AI customer-service file storage to PostgreSQL.")
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-init", action="store_true")
    args = parser.parse_args()

    tenant_id = active_tenant_id(args.tenant_id or None)
    plan = collect_file_storage(tenant_id)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "tenant_id": tenant_id, "counts": plan["counts"]}, ensure_ascii=True, indent=2))
        return 0

    config = load_storage_config()
    store = get_postgres_store(tenant_id=tenant_id, config=config)
    availability = store.availability()
    if not availability.ok:
        print(json.dumps({"ok": False, "message": availability.reason, "counts": plan["counts"]}, ensure_ascii=True, indent=2))
        return 2
    if not args.skip_init:
        store.initialize_schema()

    store.upsert_tenant(plan["tenant"])
    for category in plan["shared_categories"]:
        store.upsert_category(tenant_id, "shared", category)
    for category in plan["tenant_categories"]:
        store.upsert_category(tenant_id, "tenant", category)
    store.upsert_category(tenant_id, PRODUCT_MASTER_DB_LAYER, product_master_category_record())
    for category in product_scoped_category_records():
        store.upsert_category(tenant_id, "tenant_product", category)
    for item in plan["knowledge_items"]:
        store.upsert_knowledge_item(
            tenant_id,
            item["layer"],
            item["category_id"],
            item["payload"],
            product_id=item.get("product_id", ""),
        )
    for source in plan["rag_sources"]:
        store.upsert_rag_source(source)
    for source_id, chunks in plan["rag_chunks_by_source"].items():
        store.replace_rag_chunks(source_id, chunks)
    if plan["rag_index_entries"]:
        store.replace_rag_index(tenant_id, plan["rag_index_entries"])
    for experience in plan["rag_experiences"]:
        store.upsert_rag_experience(experience)
    for status, candidates in plan["candidates_by_status"].items():
        for candidate in candidates:
            store.upsert_candidate(tenant_id, candidate, status=status)
    for upload in plan["uploads"]:
        store.upsert_upload(tenant_id, upload)
    for version in plan["versions"]:
        store.upsert_version(tenant_id, version)
    for namespace, key, payload in plan["kv_items"]:
        store.set_kv(tenant_id, namespace, key, payload)

    db_counts = store.counts(tenant_id)
    parity = build_parity_report(plan, db_counts)
    print(
        json.dumps(
            {"ok": parity["ok"], "tenant_id": tenant_id, "counts": plan["counts"], "db_counts": db_counts, "parity": parity},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def collect_file_storage(tenant_id: str) -> dict[str, Any]:
    tenant_meta = read_json(tenant_root(tenant_id) / "tenant.json", default={"tenant_id": tenant_id, "display_name": tenant_id})
    shared_registry = read_json(SHARED_KNOWLEDGE_ROOT / "registry.json", default={"categories": []})
    tenant_registry = read_json(tenant_knowledge_base_root(tenant_id) / "registry.json", default={"categories": []})
    shared_categories = list(shared_registry.get("categories", []) or [])
    tenant_categories = list(tenant_registry.get("categories", []) or [])
    knowledge_items = []
    knowledge_items.extend(collect_category_items(SHARED_KNOWLEDGE_ROOT, shared_categories, layer="shared"))
    knowledge_items.extend(collect_category_items(tenant_knowledge_base_root(tenant_id), tenant_categories, layer="tenant"))
    knowledge_items.extend(collect_product_master_items(tenant_id))
    knowledge_items.extend(collect_product_scoped_items(tenant_id))
    rag_sources = read_json(tenant_rag_sources_root(tenant_id) / "sources.json", default=[])
    rag_chunks_by_source = collect_rag_chunks(tenant_id)
    rag_index_entries = list((read_json(tenant_rag_index_root(tenant_id) / "index.json", default={}).get("entries", []) or []))
    rag_experiences = read_json(tenant_root(tenant_id) / "rag_experience" / "experiences.json", default=[])
    candidates_by_status = {
        status: collect_json_files(APP_DATA_ROOT / "review_candidates" / status)
        for status in ("pending", "approved", "rejected")
    }
    uploads = read_json(RUNTIME_ADMIN_ROOT / "uploads_index.json", default=[])
    versions = collect_versions(APP_DATA_ROOT / "versions")
    kv_items = collect_kv_items()
    return {
        "tenant": tenant_meta,
        "shared_categories": shared_categories,
        "tenant_categories": tenant_categories,
        "knowledge_items": knowledge_items,
        "rag_sources": rag_sources,
        "rag_chunks_by_source": rag_chunks_by_source,
        "rag_index_entries": rag_index_entries,
        "rag_experiences": rag_experiences,
        "candidates_by_status": candidates_by_status,
        "uploads": uploads,
        "versions": versions,
        "kv_items": kv_items,
        "counts": {
            "shared_categories": len(shared_categories),
            "tenant_categories": len(tenant_categories),
            "knowledge_items": len(knowledge_items),
            "rag_sources": len(rag_sources),
            "rag_chunks": sum(len(items) for items in rag_chunks_by_source.values()),
            "rag_index_entries": len(rag_index_entries),
            "rag_experiences": len(rag_experiences),
            "candidates": sum(len(items) for items in candidates_by_status.values()),
            "uploads": len(uploads),
            "versions": len(versions),
            "kv_items": len(kv_items),
        },
    }


def build_parity_report(plan: dict[str, Any], db_counts: dict[str, int]) -> dict[str, Any]:
    candidate_ids = [
        str(candidate.get("candidate_id") or candidate.get("id") or "")
        for candidates in (plan.get("candidates_by_status") or {}).values()
        for candidate in candidates
        if str(candidate.get("candidate_id") or candidate.get("id") or "")
    ]
    unique_candidate_ids = sorted(set(candidate_ids))
    duplicate_candidate_count = max(0, len(candidate_ids) - len(unique_candidate_ids))
    expected = {
        "knowledge_categories": len(plan.get("shared_categories", []) or [])
        + len(plan.get("tenant_categories", []) or [])
        + len(product_scoped_category_records()),
        "knowledge_items": int((plan.get("counts") or {}).get("knowledge_items", 0) or 0),
        "review_candidates": len(unique_candidate_ids),
        "uploads": int((plan.get("counts") or {}).get("uploads", 0) or 0),
        "version_snapshots": int((plan.get("counts") or {}).get("versions", 0) or 0),
        "rag_sources": int((plan.get("counts") or {}).get("rag_sources", 0) or 0),
        "rag_chunks": int((plan.get("counts") or {}).get("rag_chunks", 0) or 0),
        "rag_index_entries": int((plan.get("counts") or {}).get("rag_index_entries", 0) or 0),
        "rag_experiences": int((plan.get("counts") or {}).get("rag_experiences", 0) or 0),
        "app_kv": int((plan.get("counts") or {}).get("kv_items", 0) or 0),
    }
    differences = []
    for key, expected_count in expected.items():
        actual_count = int(db_counts.get(key, 0) or 0)
        if actual_count != expected_count:
            differences.append({"name": key, "expected": expected_count, "actual": actual_count})
    return {
        "ok": not differences,
        "expected": expected,
        "differences": differences,
        "dedupe": {
            "review_candidate_file_total": len(candidate_ids),
            "review_candidate_unique_ids": len(unique_candidate_ids),
            "review_candidate_merged_duplicates": duplicate_candidate_count,
            "note": "File-side candidates are deduplicated by candidate_id when imported into PostgreSQL.",
        },
    }


def collect_category_items(root: Path, categories: list[dict[str, Any]], *, layer: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for category in categories:
        category_id = str(category.get("id") or "")
        if not category_id:
            continue
        if category_id == PRODUCT_MASTER_CATEGORY_ID:
            continue
        category_path = root / str(category.get("path") or category_id)
        for item in collect_json_files(category_path / "items"):
            items.append({"layer": layer, "category_id": category_id, "product_id": "", "payload": item})
    return items


def collect_product_master_items(tenant_id: str) -> list[dict[str, Any]]:
    store = ProductMasterStore(tenant_id=tenant_id)
    return [
        {
            "layer": PRODUCT_MASTER_DB_LAYER,
            "category_id": PRODUCT_MASTER_CATEGORY_ID,
            "product_id": "",
            "payload": item,
        }
        for item in store.list_items(include_archived=True)
    ]


def collect_product_scoped_items(tenant_id: str) -> list[dict[str, Any]]:
    root = tenant_product_item_knowledge_root(tenant_id)
    result: list[dict[str, Any]] = []
    if not root.exists():
        return result
    for product_root in sorted(path for path in root.iterdir() if path.is_dir()):
        product_id = product_root.name
        for kind_root in sorted(path for path in product_root.iterdir() if path.is_dir()):
            category_id = PRODUCT_KIND_TO_CATEGORY.get(kind_root.name)
            if not category_id:
                continue
            for item in collect_json_files(kind_root):
                item.setdefault("category_id", category_id)
                item.setdefault("data", {})
                item["data"].setdefault("product_id", product_id)
                result.append({"layer": "tenant_product", "category_id": category_id, "product_id": product_id, "payload": item})
    return result


def collect_rag_chunks(tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    root = tenant_rag_chunks_root(tenant_id)
    chunks_by_source: dict[str, list[dict[str, Any]]] = {}
    if not root.exists():
        return chunks_by_source
    for path in sorted(root.glob("source_*.json")):
        payload = read_json(path, default={})
        source_id = str((payload.get("source") or {}).get("source_id") or path.stem)
        chunks_by_source[source_id] = list(payload.get("chunks", []) or [])
    return chunks_by_source


def collect_versions(root: Path) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    if not root.exists():
        return versions
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        metadata = read_json(path / "metadata.json", default=None)
        if isinstance(metadata, dict):
            versions.append(metadata)
    return versions


def collect_kv_items() -> list[tuple[str, str, Any]]:
    result: list[tuple[str, str, Any]] = []
    ignored = read_json(RUNTIME_ADMIN_ROOT / "diagnostic_ignores.json", default=None)
    if isinstance(ignored, dict):
        result.append(("diagnostics", "ignored_issues", ignored))
    return result


def collect_json_files(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        payload = read_json(path, default=None)
        if isinstance(payload, dict):
            items.append(payload)
    return items


def read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
