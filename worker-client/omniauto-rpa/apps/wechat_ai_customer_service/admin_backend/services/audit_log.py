"""Append-only audit logging for admin actions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


PROJECT_ROOT = Path(__file__).resolve().parents[4]
AUDIT_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "audit.jsonl"


def append_audit(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        **(payload or {}),
    }
    db = postgres_store()
    config = load_storage_config()
    if db:
        db.append_audit(active_tenant_id(), action, event)
        if not config.mirror_files:
            return event
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
