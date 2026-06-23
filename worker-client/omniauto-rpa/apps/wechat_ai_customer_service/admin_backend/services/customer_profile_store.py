"""Per-customer profile persistence with dual-backend (JSON file + Postgres JSONB)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


MAX_PROFILE_RECORDS = 5000
MAX_CONVERSATION_RECORDS = 5000
TRANSIENT_WRITE_WINERRORS = {5, 32, 33}
TRANSIENT_WRITE_ERRNOS = {13, 16, 32}
_PROFILE_STORE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stable_digest(value: Any, length: int = 16) -> str:
    import hashlib
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _safe_id(value: str) -> str:
    import re
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text or not re.match(r"^[A-Za-z0-9]", text):
        text = "id_" + _stable_digest(value, 12)
    return text[:120]


def _profile_id_for(target_name: str, tenant_id: str) -> str:
    return _safe_id(f"cust_{tenant_id}_{target_name}")


def _file_lock(path: Path) -> threading.RLock:
    return _PROFILE_STORE_LOCK


def _is_transient_file_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror in TRANSIENT_WRITE_WINERRORS:
        return True
    errno_value = getattr(exc, "errno", None)
    return errno_value in TRANSIENT_WRITE_ERRNOS


class CustomerProfileStore:
    """Dual-backend store for customer profiles and conversation summaries."""

    def __init__(self, *, tenant_id: str | None = None, root: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.root = root or (tenant_runtime_root(self.tenant_id) / "customer_profiles")

    @property
    def profiles_path(self) -> Path:
        return self.root / "profiles.json"

    @property
    def conversations_path(self) -> Path:
        return self.root / "conversations.json"

    # ── Profile CRUD ──────────────────────────────────────────────────────

    def upsert_profile(self, record: dict[str, Any]) -> dict[str, Any]:
        profile = _normalize_profile(record, tenant_id=self.tenant_id)
        db = _postgres_store()
        config = load_storage_config()
        if db:
            db.upsert_customer_profile(self.tenant_id, profile)
            if not config.mirror_files:
                return profile
        with _file_lock(self.profiles_path):
            profiles = self._read_json(self.profiles_path, [])
            by_id = {str(item.get("profile_id") or ""): item for item in profiles if isinstance(item, dict)}
            existing = by_id.get(profile["profile_id"], {})
            merged = {**existing, **profile, "updated_at": _now()}
            by_id[profile["profile_id"]] = merged
            self._write_json(
                self.profiles_path,
                sorted(by_id.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True),
            )
        return merged

    def get_profile(self, *, target_name: str = "", profile_id: str = "") -> dict[str, Any] | None:
        if not profile_id and target_name:
            profile_id = _profile_id_for(target_name, self.tenant_id)
        if not profile_id:
            return None
        db = _postgres_store()
        if db:
            item = db.get_customer_profile(self.tenant_id, profile_id)
            if item:
                return item
        for item in self._read_json(self.profiles_path, []):
            if isinstance(item, dict) and str(item.get("profile_id") or "") == profile_id:
                return item
        return None

    def get_or_create(self, *, target_name: str, display_name: str = "") -> dict[str, Any]:
        profile_id = _profile_id_for(target_name, self.tenant_id)
        existing = self.get_profile(profile_id=profile_id)
        if existing:
            return existing
        return self.upsert_profile({
            "profile_id": profile_id,
            "target_name": target_name,
            "display_name": display_name or target_name,
        })

    def list_profiles(
        self,
        *,
        query: str = "",
        tag_key: str = "",
        tag_value: str = "",
        status: str = "all",
        sort: str = "updated_at",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        db = _postgres_store()
        if db:
            items = db.list_customer_profiles(
                self.tenant_id,
                query=query,
                tag_key=tag_key,
                tag_value=tag_value,
                status=status,
                sort=sort,
                limit=limit,
            )
            if items is not None:
                return items
        records = [item for item in self._read_json(self.profiles_path, []) if isinstance(item, dict)]
        if query:
            lowered = query.lower()
            records = [
                item for item in records
                if lowered in str(item.get("display_name") or "").lower()
                or lowered in str(item.get("target_name") or "").lower()
            ]
        if status and status != "all":
            records = [item for item in records if str(item.get("status") or "active") == status]
        if tag_key:
            records = [
                item for item in records
                if tag_key in (item.get("tags") or {})
                and (not tag_value or str((item.get("tags") or {}).get(tag_key) or "") == tag_value)
            ]
        reverse = sort in ("updated_at", "last_contact_at", "total_messages", "created_at")
        records.sort(
            key=lambda item: str(item.get(sort) or item.get("updated_at") or ""),
            reverse=reverse,
        )
        return records[: max(1, min(int(limit or 200), 500))]

    def delete_profile(self, *, profile_id: str = "", target_name: str = "") -> bool:
        if not profile_id and target_name:
            profile_id = _profile_id_for(target_name, self.tenant_id)
        if not profile_id:
            return False
        db = _postgres_store()
        config = load_storage_config()
        deleted = False
        if db:
            deleted = db.delete_customer_profile(self.tenant_id, profile_id)
            if not config.mirror_files:
                return deleted
        with _file_lock(self.profiles_path):
            profiles = [item for item in self._read_json(self.profiles_path, []) if isinstance(item, dict)]
            new_profiles = [item for item in profiles if str(item.get("profile_id") or "") != profile_id]
            if len(new_profiles) < len(profiles):
                self._write_json(self.profiles_path, new_profiles)
                deleted = True
        return deleted

    def increment_message_stats(self, *, target_name: str, is_reply: bool = False) -> dict[str, Any] | None:
        db = _postgres_store()
        config = load_storage_config()
        if db and not config.mirror_files:
            profile = self.get_or_create(target_name=target_name)
            basic = dict(profile.get("basic_info") or {})
            basic["total_messages"] = int(basic.get("total_messages", 0) or 0) + 1
            if is_reply:
                basic["total_replies"] = int(basic.get("total_replies", 0) or 0) + 1
            basic["last_contact_at"] = _now()
            return self.upsert_profile({
                "profile_id": profile["profile_id"],
                "target_name": target_name,
                "basic_info": basic,
            })

        profile_id = _profile_id_for(target_name, self.tenant_id)
        with _file_lock(self.profiles_path):
            profiles = self._read_json(self.profiles_path, [])
            by_id = {str(item.get("profile_id") or ""): item for item in profiles if isinstance(item, dict)}
            existing = by_id.get(profile_id, {})
            profile = {
                **existing,
                "profile_id": profile_id,
                "target_name": target_name,
                "display_name": str(existing.get("display_name") or target_name),
            }
            basic = dict(profile.get("basic_info") or {})
            basic["total_messages"] = int(basic.get("total_messages", 0) or 0) + 1
            if is_reply:
                basic["total_replies"] = int(basic.get("total_replies", 0) or 0) + 1
            basic["last_contact_at"] = _now()
            merged = _normalize_profile({**profile, "basic_info": basic}, tenant_id=self.tenant_id)
            merged["updated_at"] = _now()
            by_id[profile_id] = merged
            self._write_json(
                self.profiles_path,
                sorted(by_id.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True),
            )
        if db:
            db.upsert_customer_profile(self.tenant_id, merged)
        return merged

    # ── Conversation summary CRUD ─────────────────────────────────────────

    def upsert_conversation_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        conv = _normalize_conversation(record, tenant_id=self.tenant_id)
        db = _postgres_store()
        config = load_storage_config()
        if db:
            db.upsert_customer_conversation(self.tenant_id, conv)
            if not config.mirror_files:
                return conv
        with _file_lock(self.conversations_path):
            records = self._read_json(self.conversations_path, [])
            by_id = {str(item.get("conversation_id") or ""): item for item in records if isinstance(item, dict)}
            existing = by_id.get(conv["conversation_id"], {})
            merged = {**existing, **conv, "updated_at": _now()}
            by_id[conv["conversation_id"]] = merged
            self._write_json(
                self.conversations_path,
                sorted(by_id.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True),
            )
        return merged

    def get_conversation_summary(self, *, conversation_id: str = "", target_name: str = "") -> dict[str, Any] | None:
        if not conversation_id and target_name:
            conversation_id = _profile_id_for(target_name, self.tenant_id)
        if not conversation_id:
            return None
        db = _postgres_store()
        if db:
            item = db.get_customer_conversation_summary(self.tenant_id, conversation_id)
            if item:
                return item
        for item in self._read_json(self.conversations_path, []):
            if isinstance(item, dict) and str(item.get("conversation_id") or "") == conversation_id:
                return item
        return None

    def list_conversation_summaries(self, *, profile_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        db = _postgres_store()
        if db:
            items = db.list_customer_conversations(self.tenant_id, profile_id=profile_id, limit=limit)
            if items is not None:
                return items
        records = [item for item in self._read_json(self.conversations_path, []) if isinstance(item, dict)]
        if profile_id:
            records = [item for item in records if str(item.get("profile_id") or "") == profile_id]
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 100), 500))]

    # ── Helpers ───────────────────────────────────────────────────────────

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        for attempt in range(5):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                if not _is_transient_file_error(exc) or attempt >= 4:
                    return default
                time.sleep(0.03 * (attempt + 1))
            except json.JSONDecodeError:
                if attempt >= 4:
                    return default
                time.sleep(0.03 * (attempt + 1))
        return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        last_error: OSError | None = None
        for attempt in range(10):
            temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
            try:
                temp.write_text(text, encoding="utf-8")
                os.replace(temp, path)
                return
            except OSError as exc:
                last_error = exc
                try:
                    temp.unlink()
                except OSError:
                    pass
                if not _is_transient_file_error(exc) or attempt >= 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error


def _normalize_profile(record: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    target_name = str(record.get("target_name") or record.get("name") or "").strip()
    profile_id = str(record.get("profile_id") or _profile_id_for(target_name, tenant_id)).strip()
    timestamp = _now()
    existing_basic = record.get("basic_info") if isinstance(record.get("basic_info"), dict) else {}
    return {
        "profile_id": profile_id,
        "tenant_id": tenant_id,
        "target_name": target_name,
        "display_name": str(record.get("display_name") or target_name or profile_id).strip(),
        "status": str(record.get("status") or "active"),
        "basic_info": {
            "inferred_name": str(existing_basic.get("inferred_name") or "").strip(),
            "gender": str(existing_basic.get("gender") or "").strip(),
            "gender_confidence": float(existing_basic.get("gender_confidence") or 0.0),
            "region": str(existing_basic.get("region") or "").strip(),
            "first_contact_at": str(existing_basic.get("first_contact_at") or timestamp),
            "last_contact_at": str(existing_basic.get("last_contact_at") or timestamp),
            "total_messages": int(existing_basic.get("total_messages", 0) or 0),
            "total_replies": int(existing_basic.get("total_replies", 0) or 0),
            "total_handoffs": int(existing_basic.get("total_handoffs", 0) or 0),
        },
        "tags": dict(record.get("tags") or {}),
        "conversation_summary": str(record.get("conversation_summary") or "").strip(),
        "greeting_preference": {
            "formality": str(record.get("greeting_preference", {}).get("formality") or "casual"),
            "last_greeting_type": str(record.get("greeting_preference", {}).get("last_greeting_type") or ""),
        } if isinstance(record.get("greeting_preference"), dict) else {
            "formality": "casual",
            "last_greeting_type": "",
        },
        "created_at": str(record.get("created_at") or timestamp),
        "updated_at": str(record.get("updated_at") or timestamp),
        "source": record.get("source") if isinstance(record.get("source"), dict) else {},
    }


def _normalize_conversation(record: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    conversation_id = str(record.get("conversation_id") or record.get("profile_id") or "").strip()
    if not conversation_id:
        raise ValueError("conversation_id is required")
    timestamp = _now()
    return {
        "conversation_id": conversation_id,
        "tenant_id": tenant_id,
        "profile_id": str(record.get("profile_id") or conversation_id).strip(),
        "target_name": str(record.get("target_name") or "").strip(),
        "summary": str(record.get("summary") or record.get("conversation_summary") or "").strip(),
        "last_message_at": str(record.get("last_message_at") or timestamp),
        "message_count": int(record.get("message_count", 0) or 0),
        "reply_count": int(record.get("reply_count", 0) or 0),
        "payload": record.get("payload") if isinstance(record.get("payload"), dict) else {},
        "created_at": str(record.get("created_at") or timestamp),
        "updated_at": str(record.get("updated_at") or timestamp),
    }


def _postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
