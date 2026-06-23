"""Durable human handoff case store."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


PROJECT_ROOT = Path(__file__).resolve().parents[4]
HANDOFF_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "handoff_cases.json"
ALLOWED_STATUSES = {"open", "acknowledged", "resolved", "ignored"}


class HandoffStore:
    def __init__(self, tenant_id: str | None = None, path: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.path = path or HANDOFF_PATH

    def create_case(self, item: dict[str, Any]) -> dict[str, Any]:
        now_text = now()
        message_ids = [str(value) for value in item.get("message_ids", []) or []]
        case_id = item.get("case_id") or self.build_case_id(item, message_ids, now_text)
        existing = self.get_case(str(case_id))
        if existing:
            existing["deduped"] = True
            return existing
        case = {
            "tenant_id": self.tenant_id,
            "case_id": case_id,
            "target": item.get("target") or "",
            "status": item.get("status") or "open",
            "priority": int(item.get("priority", 1) or 1),
            "reason": item.get("reason") or "",
            "message_ids": message_ids,
            "message_contents": [str(value) for value in item.get("message_contents", []) or []],
            "reply_text": item.get("reply_text") or "",
            "operator_alert": item.get("operator_alert", {}) if isinstance(item.get("operator_alert"), dict) else {},
            "product_context": item.get("product_context", {}) if isinstance(item.get("product_context"), dict) else {},
            "payload": item,
            "resolution": {},
            "created_at": item.get("created_at") or now_text,
            "updated_at": now_text,
            "resolved_at": None,
        }
        db = self.db()
        if db:
            return db.upsert_handoff_case(self.tenant_id, case)
        cases = [old for old in self.read_cases() if old.get("case_id") != case["case_id"]]
        cases.append(case)
        self.write_cases(cases)
        return case

    def build_case_id(self, item: dict[str, Any], message_ids: list[str], now_text: str) -> str:
        if message_ids:
            seed = f"{self.tenant_id}:{item.get('target')}:{message_ids}:{item.get('reason')}"
        else:
            seed = f"{self.tenant_id}:{item.get('target')}:{item.get('reason')}:{now_text}:{item.get('reply_text')}"
        return "handoff_" + stable_digest(seed, 20)

    def list_cases(self, *, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        db = self.db()
        if db:
            return db.list_handoff_cases(self.tenant_id, status=status, limit=limit)
        cases = [item for item in self.read_cases() if item.get("tenant_id") == self.tenant_id]
        if status and status != "all":
            cases = [item for item in cases if item.get("status") == status]
        cases.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return cases[: max(1, min(int(limit or 100), 500))]

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        db = self.db()
        if db:
            return db.get_handoff_case(self.tenant_id, case_id)
        for item in self.read_cases():
            if item.get("tenant_id") == self.tenant_id and item.get("case_id") == case_id:
                return item
        return None

    def update_status(self, case_id: str, status: str, resolution: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid handoff status: {status}")
        db = self.db()
        if db:
            return db.update_handoff_status(self.tenant_id, case_id, status, resolution or {})
        cases = self.read_cases()
        found: dict[str, Any] | None = None
        now_text = now()
        for item in cases:
            if item.get("tenant_id") == self.tenant_id and item.get("case_id") == case_id:
                item["status"] = status
                item["resolution"] = resolution or {}
                item["updated_at"] = now_text
                if status in {"resolved", "ignored"}:
                    item["resolved_at"] = now_text
                found = dict(item)
                break
        self.write_cases(cases)
        return found

    def summary(self) -> dict[str, Any]:
        db = self.db()
        if db:
            return db.handoff_summary(self.tenant_id)
        counts: dict[str, int] = {}
        for item in self.list_cases(status="all", limit=500):
            status = str(item.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {
            "total": sum(counts.values()),
            "open": counts.get("open", 0),
            "acknowledged": counts.get("acknowledged", 0),
            "resolved": counts.get("resolved", 0),
            "ignored": counts.get("ignored", 0),
            "by_status": counts,
        }

    def read_cases(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            return list(payload.get("cases", []) or [])
        if isinstance(payload, list):
            return payload
        return []

    def write_cases(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"cases": items}, ensure_ascii=False, indent=2), encoding="utf-8")

    def db(self):
        config = load_storage_config()
        store = get_postgres_store(tenant_id=self.tenant_id, config=config)
        if not store.availability().ok:
            return None
        store.initialize_schema()
        return store


def stable_digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
