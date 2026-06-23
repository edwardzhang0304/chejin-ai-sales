"""Read-only knowledge APIs for the admin console."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from ..auth_context import current_auth_context
from ..services.formal_review_state import (
    acknowledge_item,
    enrich_knowledge_item,
    mark_item_new,
    sort_knowledge_items_for_review,
)
from ..services.knowledge_base_store import KnowledgeBaseStore, product_scoped_category_records
from ..services.knowledge_compiler import KnowledgeCompiler
from ..services.knowledge_registry import KnowledgeRegistry
from ..services.knowledge_schema_manager import KnowledgeSchemaManager
from ..services.knowledge_store import KnowledgeStore
from ..services.shared_public_sync import queue_shared_public_scan
from apps.wechat_ai_customer_service.product_master import PRODUCT_MASTER_CATEGORY_ID, product_master_category_record


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def compile_runtime_knowledge() -> None:
    KnowledgeCompiler().compile_to_disk()


def knowledge_store() -> KnowledgeStore:
    return KnowledgeStore()


def knowledge_components() -> tuple[KnowledgeRegistry, KnowledgeSchemaManager, KnowledgeBaseStore]:
    registry = KnowledgeRegistry()
    schema_manager = KnowledgeSchemaManager(registry)
    base_store = KnowledgeBaseStore(registry, schema_manager)
    return registry, schema_manager, base_store


@router.get("/overview")
def overview() -> dict[str, Any]:
    return knowledge_store().overview()


@router.get("/categories")
def categories() -> dict[str, Any]:
    registry, schema_manager, base_store = knowledge_components()
    items = []
    for category in registry.list_categories(enabled_only=True):
        category_id = str(category.get("id") or "")
        if category_id == PRODUCT_MASTER_CATEGORY_ID:
            continue
        items.append(
            {
                **category,
                "schema": schema_manager.load_schema(category_id),
                "resolver": schema_manager.load_resolver(category_id),
                "item_count": len(base_store.list_items(category_id)),
            }
        )
    product_category = product_master_category_record()
    items.append(
        {
            **product_category,
            "schema": schema_manager.load_schema(PRODUCT_MASTER_CATEGORY_ID),
            "resolver": schema_manager.load_resolver(PRODUCT_MASTER_CATEGORY_ID),
            "item_count": len(base_store.list_items(PRODUCT_MASTER_CATEGORY_ID)),
        }
    )
    for category in product_scoped_category_records():
        category_id = str(category.get("id") or "")
        items.append(
            {
                **category,
                "schema": schema_manager.load_schema(category_id),
                "resolver": schema_manager.load_resolver(category_id),
                "item_count": len(base_store.list_items(category_id)),
            }
        )
    return {"ok": True, "items": items}


@router.post("/categories")
def create_category(payload: dict[str, Any]) -> dict[str, Any]:
    registry, _, _ = knowledge_components()
    try:
        category = registry.create_custom_category(
            category_id=str(payload.get("id") or payload.get("category_id") or ""),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            participates_in_reply=bool(payload.get("participates_in_reply", False)),
            participates_in_learning=bool(payload.get("participates_in_learning", True)),
            participates_in_diagnostics=bool(payload.get("participates_in_diagnostics", True)),
            fields=payload.get("fields") if isinstance(payload.get("fields"), list) else None,
        )
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": category}


@router.get("/categories/{category_id}")
def category_detail(category_id: str) -> dict[str, Any]:
    registry, schema_manager, base_store = knowledge_components()
    try:
        category = product_master_category_record() if category_id == PRODUCT_MASTER_CATEGORY_ID else None
        if not category:
            category = next((item for item in product_scoped_category_records() if item.get("id") == category_id), None)
        if not category:
            category = registry.require_category(category_id)
        return {
            "ok": True,
            "item": {
                **category,
                "schema": schema_manager.load_schema(category_id),
                "resolver": schema_manager.load_resolver(category_id),
                "item_count": len(base_store.list_items(category_id)),
            },
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"category not found: {category_id}") from exc


@router.get("/categories/{category_id}/items")
def category_items(
    category_id: str,
    include_archived: bool = False,
    limit: int = Query(0, ge=0, le=1000),
    offset: int = Query(0, ge=0),
    query: str = "",
) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    try:
        items = sort_knowledge_items_for_review(base_store.list_items(category_id, include_archived=include_archived))
        unfiltered_total = len(items)
        normalized_query = str(query or "").strip().lower()
        if normalized_query:
            items = [item for item in items if normalized_query in knowledge_item_search_text(item).lower()]
        total = len(items)
        if limit:
            page = items[offset : offset + limit]
        else:
            page = items
        return {
            "ok": True,
            "items": [enrich_knowledge_item(item) for item in page],
            "total": total,
            "unfiltered_total": unfiltered_total,
            "offset": offset,
            "limit": limit,
            "has_more": bool(limit and offset + len(page) < total),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"category not found: {category_id}") from exc


def knowledge_item_search_text(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return " ".join(
        [
            str(item.get("id") or ""),
            str(item.get("category_id") or ""),
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ]
    )


@router.get("/categories/{category_id}/items/{item_id}")
def category_item_detail(category_id: str, item_id: str) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    try:
        item = base_store.get_item(category_id, item_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"item not found: {category_id}/{item_id}") from exc
    if not item:
        raise HTTPException(status_code=404, detail=f"item not found: {category_id}/{item_id}")
    return {"ok": True, "item": enrich_knowledge_item(item)}


@router.post("/categories/{category_id}/items")
def create_category_item(category_id: str, payload: dict[str, Any], request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    item = normalize_item_payload(category_id, payload)
    item = mark_item_new(item, {"source_module": "manual", "target_category": category_id, "item_id": item.get("id")})
    result = base_store.save_item(category_id, item)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    compile_runtime_knowledge()
    context = current_auth_context(request)
    queue_shared_public_scan(
        background_tasks,
        tenant_id=context.tenant_id,
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        category_id=category_id,
    )
    return result


@router.put("/categories/{category_id}/items/{item_id}")
def update_category_item(category_id: str, item_id: str, payload: dict[str, Any], request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    existing = base_store.get_item(category_id, item_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"item not found: {category_id}/{item_id}")
    item = normalize_item_payload(category_id, {**existing, **payload, "id": item_id})
    result = base_store.save_item(category_id, item)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    compile_runtime_knowledge()
    context = current_auth_context(request)
    queue_shared_public_scan(
        background_tasks,
        tenant_id=context.tenant_id,
        token=str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip(),
        category_id=category_id,
    )
    return result


@router.post("/categories/{category_id}/items/{item_id}/acknowledge")
def acknowledge_category_item(category_id: str, item_id: str) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    existing = base_store.get_item(category_id, item_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"item not found: {category_id}/{item_id}")
    result = base_store.save_item(category_id, acknowledge_item(existing))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    compile_runtime_knowledge()
    result["item"] = enrich_knowledge_item(result.get("item") or {})
    return result


@router.delete("/categories/{category_id}/items/{item_id}")
def archive_category_item(category_id: str, item_id: str) -> dict[str, Any]:
    _, _, base_store = knowledge_components()
    result = base_store.archive_item(category_id, item_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    compile_runtime_knowledge()
    return result


@router.get("/products")
def products() -> dict[str, Any]:
    return {"ok": True, "items": knowledge_store().products()}


@router.get("/products/{product_id}")
def product_detail(product_id: str) -> dict[str, Any]:
    return {"ok": True, "item": knowledge_store().product(product_id)}


@router.get("/faqs")
def faqs() -> dict[str, Any]:
    return {"ok": True, "items": knowledge_store().faqs()}


@router.get("/policies")
def policies() -> dict[str, Any]:
    return {"ok": True, "items": knowledge_store().policies()}


@router.get("/styles")
def styles() -> dict[str, Any]:
    return {"ok": True, "items": knowledge_store().styles()}


@router.get("/persona")
def persona() -> dict[str, Any]:
    return {"ok": True, "item": knowledge_store().persona()}


@router.get("/raw-json")
def raw_json(file: str = Query(..., pattern="^(manifest|product_knowledge|style_examples)$")) -> dict[str, Any]:
    return {"ok": True, "file": file, "content": knowledge_store().raw_json(file)}


def normalize_item_payload(category_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    item = {
        "schema_version": int(payload.get("schema_version") or 1),
        "category_id": category_id,
        "id": str(payload.get("id") or ""),
        "status": str(payload.get("status") or "active"),
        "source": payload.get("source") if isinstance(payload.get("source"), dict) else {"type": "admin_form"},
        "data": data,
        "runtime": payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }
    if isinstance(payload.get("review_state"), dict):
        item["review_state"] = payload["review_state"]
    return item
