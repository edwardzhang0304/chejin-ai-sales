"""Customer profile APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..services.customer_profile_store import CustomerProfileStore
from ..services.raw_message_store import RawMessageStore


router = APIRouter(prefix="/api/customers", tags=["customers"])


@router.get("")
def list_customers(
    query: str = Query(default=""),
    tag_key: str = Query(default=""),
    tag_value: str = Query(default=""),
    status: str = Query(default="all"),
    sort: str = Query(default="updated_at"),
    limit: int = Query(default=200),
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    items = store.list_profiles(
        query=query,
        tag_key=tag_key,
        tag_value=tag_value,
        status=status,
        sort=sort,
        limit=limit,
    )
    return {"ok": True, "items": items, "count": len(items)}


@router.get("/{profile_id}")
def get_customer(profile_id: str, tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    item = store.get_profile(profile_id=profile_id)
    if not item:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "item": item}


@router.put("/{profile_id}")
def update_customer(
    profile_id: str,
    payload: dict[str, Any],
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    existing = store.get_profile(profile_id=profile_id)
    if not existing:
        return {"ok": False, "error": "not_found"}
    update = {"profile_id": profile_id}
    if "display_name" in payload:
        update["display_name"] = payload["display_name"]
    if "tags" in payload:
        update["tags"] = payload["tags"]
    if "basic_info" in payload:
        update["basic_info"] = payload["basic_info"]
    if "conversation_summary" in payload:
        update["conversation_summary"] = payload["conversation_summary"]
    updated = store.upsert_profile(update)
    return {"ok": True, "item": updated}


@router.get("/{profile_id}/messages")
def get_customer_messages(
    profile_id: str,
    limit: int = Query(default=100),
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    profile = store.get_profile(profile_id=profile_id)
    if not profile:
        return {"ok": False, "error": "not_found"}
    conversation_id = profile_id
    messages = RawMessageStore(tenant_id=tenant_id).list_messages(
        conversation_id=conversation_id,
        limit=limit,
    )
    return {"ok": True, "items": messages, "count": len(messages)}


@router.post("/{profile_id}/tags")
def add_tag(
    profile_id: str,
    payload: dict[str, Any],
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    existing = store.get_profile(profile_id=profile_id)
    if not existing:
        return {"ok": False, "error": "not_found"}
    key = str(payload.get("key") or "").strip()
    value = payload.get("value")
    if not key:
        return {"ok": False, "error": "missing_key"}
    tags = dict(existing.get("tags") or {})
    tags[key] = value
    updated = store.upsert_profile({"profile_id": profile_id, "tags": tags})
    return {"ok": True, "item": updated}


@router.delete("/{profile_id}/tags/{key}")
def remove_tag(
    profile_id: str,
    key: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    store = CustomerProfileStore(tenant_id=tenant_id)
    existing = store.get_profile(profile_id=profile_id)
    if not existing:
        return {"ok": False, "error": "not_found"}
    tags = dict(existing.get("tags") or {})
    if key in tags:
        del tags[key]
    updated = store.upsert_profile({"profile_id": profile_id, "tags": tags})
    return {"ok": True, "item": updated}
