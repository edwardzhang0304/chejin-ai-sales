"""Client-side triggers for shared public knowledge candidate sync."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_context
from apps.wechat_ai_customer_service.sync import VpsLocalSyncService


UNIVERSAL_SYNC_CATEGORIES = {"policies", "chats", "custom"}


def should_trigger_shared_public_scan(category_id: str) -> bool:
    return str(category_id or "").strip() in UNIVERSAL_SYNC_CATEGORIES


def queue_shared_public_scan(background_tasks: Any, *, tenant_id: str, token: str, category_id: str) -> None:
    if not should_trigger_shared_public_scan(category_id):
        return
    if background_tasks is None:
        run_shared_public_scan(tenant_id=tenant_id, token=token)
        return
    background_tasks.add_task(run_shared_public_scan, tenant_id=tenant_id, token=token)


def run_shared_public_scan(*, tenant_id: str, token: str = "") -> dict[str, Any]:
    tenant = active_tenant_id(tenant_id)
    with tenant_context(tenant):
        try:
            return VpsLocalSyncService().upload_formal_knowledge_candidates(
                token=token,
                tenant_id=tenant,
                use_llm=True,
                limit=30,
                only_unscanned=True,
            )
        except Exception as exc:
            return {"ok": False, "tenant_id": tenant, "error": str(exc)}
