"""Tenant-local settings for the WeChat customer-service workbench."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import tenant_runtime_root


DEFAULT_SETTINGS = {
    "enabled": False,
    "reply_mode": "manual_assist",
    "record_messages": True,
    "auto_learn": True,
    "use_llm": True,
    "rag_enabled": True,
    "data_capture_enabled": True,
    "handoff_enabled": True,
    "operator_alert_enabled": True,
    "identity_guard_enabled": True,
    "style_adapter_enabled": True,
    "final_visible_llm_polish_enabled": True,
    "customer_service_brain_mode": "brain_first",
    "respond_all_unread_sessions": False,
    "session_targets_managed": False,
    "session_targets": [],
}

REPLY_MODES = {
    "record_only": "只记录不回复",
    "manual_assist": "只给建议，人工发送",
    "guarded_auto": "谨慎自动回复",
    "full_auto": "全自动回复",
}

CUSTOMER_SERVICE_BRAIN_FIRST_MODE = "brain_first"
CUSTOMER_SERVICE_BRAIN_MODES = {
    CUSTOMER_SERVICE_BRAIN_FIRST_MODE: "客服大脑优先：由大模型先思考再回复",
}
LEGACY_CUSTOMER_SERVICE_BRAIN_MODES = {"off", "shadow", "hybrid_shadow"}


def normalize_customer_service_brain_mode(value: Any) -> str:
    """Collapse hidden legacy modes to the only customer-service runtime mode."""
    _ = value
    return CUSTOMER_SERVICE_BRAIN_FIRST_MODE


class CustomerServiceSettings:
    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.tenant_id = tenant_id

    @property
    def settings_path(self) -> Path:
        return tenant_runtime_root(self.tenant_id) / "customer_service" / "settings.json"

    def get(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.settings_path.exists():
            try:
                raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = raw
            except json.JSONDecodeError:
                payload = {}
        settings = {**DEFAULT_SETTINGS, **{key: value for key, value in payload.items() if key in DEFAULT_SETTINGS}}
        if settings["reply_mode"] not in REPLY_MODES:
            settings["reply_mode"] = DEFAULT_SETTINGS["reply_mode"]
        settings["identity_guard_enabled"] = bool(settings.get("identity_guard_enabled", True))
        settings["style_adapter_enabled"] = bool(settings.get("style_adapter_enabled", True))
        settings["final_visible_llm_polish_enabled"] = bool(settings.get("final_visible_llm_polish_enabled", True))
        settings["customer_service_brain_mode"] = normalize_customer_service_brain_mode(
            settings.get("customer_service_brain_mode")
        )
        settings["respond_all_unread_sessions"] = bool(settings.get("respond_all_unread_sessions", False))
        settings["session_targets_managed"] = bool(settings.get("session_targets_managed", False))
        settings["session_targets"] = normalize_session_targets(settings.get("session_targets"))
        return settings

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {key: value for key, value in (patch or {}).items() if key in DEFAULT_SETTINGS}
        if "identity_guard_enabled" in allowed:
            allowed["identity_guard_enabled"] = bool(allowed.get("identity_guard_enabled", True))
        if "style_adapter_enabled" in allowed:
            allowed["style_adapter_enabled"] = bool(allowed.get("style_adapter_enabled", True))
        if "final_visible_llm_polish_enabled" in allowed:
            allowed["final_visible_llm_polish_enabled"] = bool(allowed.get("final_visible_llm_polish_enabled", True))
        if "customer_service_brain_mode" in allowed:
            allowed["customer_service_brain_mode"] = normalize_customer_service_brain_mode(
                allowed.get("customer_service_brain_mode")
            )
        if "respond_all_unread_sessions" in allowed:
            allowed["respond_all_unread_sessions"] = bool(allowed.get("respond_all_unread_sessions", False))
        if "session_targets_managed" in allowed:
            allowed["session_targets_managed"] = bool(allowed.get("session_targets_managed", False))
        if "session_targets" in allowed:
            allowed["session_targets"] = normalize_session_targets(allowed.get("session_targets"))
        settings = {**self.get(), **allowed}
        if settings["reply_mode"] not in REPLY_MODES:
            settings["reply_mode"] = DEFAULT_SETTINGS["reply_mode"]
        settings["identity_guard_enabled"] = bool(settings.get("identity_guard_enabled", True))
        settings["style_adapter_enabled"] = bool(settings.get("style_adapter_enabled", True))
        settings["final_visible_llm_polish_enabled"] = bool(settings.get("final_visible_llm_polish_enabled", True))
        settings["customer_service_brain_mode"] = normalize_customer_service_brain_mode(
            settings.get("customer_service_brain_mode")
        )
        settings["respond_all_unread_sessions"] = bool(settings.get("respond_all_unread_sessions", False))
        settings["session_targets_managed"] = bool(settings.get("session_targets_managed", False))
        settings["session_targets"] = normalize_session_targets(settings.get("session_targets"))
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings_path)
        return settings

    def summary(self) -> dict[str, Any]:
        settings = self.get()
        targets = self.list_session_targets()
        target_enabled = [item for item in targets if item.get("enabled")]
        return {
            "settings": settings,
            "reply_modes": [{"id": key, "label": label} for key, label in REPLY_MODES.items()],
            "customer_service_brain_modes": [
                {"id": key, "label": label} for key, label in CUSTOMER_SERVICE_BRAIN_MODES.items()
            ],
            "legacy_customer_service_brain_modes_hidden": True,
            "status": self.status_text(settings),
            "session_targets": targets,
            "session_target_counts": {
                "total": len(targets),
                "enabled": len(target_enabled),
                "ignored": max(0, len(targets) - len(target_enabled)),
            },
        }

    def list_session_targets(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        items = normalize_session_targets(self.get().get("session_targets"))
        if include_archived:
            return items
        return [item for item in items if not bool(item.get("archived", False))]

    def merge_discovered_sessions(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        settings = self.get()
        existing = normalize_session_targets(settings.get("session_targets"))
        by_name = {str(item.get("name") or ""): dict(item) for item in existing}
        respond_all_unread = bool(settings.get("respond_all_unread_sessions", False))
        discovered_count = 0
        added_count = 0
        archived_count = 0
        warnings: list[str] = []
        discovered_names: set[str] = set()
        tick = now_iso()
        for raw in sessions or []:
            if not isinstance(raw, dict):
                continue
            name = normalize_session_name(raw.get("name") or raw.get("title"))
            if not name:
                continue
            discovered_names.add(name)
            discovered_count += 1
            current = by_name.get(name)
            if current is None:
                kind = infer_conversation_type(name, raw)
                default_enabled = respond_all_unread and kind not in {"file_transfer", "system", "group"}
                current = {
                    "name": name,
                    "display_name": name,
                    "enabled": default_enabled,
                    "exact": True,
                    "conversation_type": kind,
                    "source": "discovered",
                    "updated_at": tick,
                    "archived": False,
                    "archived_at": "",
                    "archived_reason": "",
                    "last_discovered_at": tick,
                }
                by_name[name] = current
                added_count += 1
                continue
            current["display_name"] = normalize_session_name(current.get("display_name") or name) or name
            current["conversation_type"] = normalized_conversation_type(
                raw.get("conversation_type")
                or raw.get("type")
                or current.get("conversation_type")
                or infer_conversation_type(name, raw)
            )
            current["archived"] = False
            current["archived_at"] = ""
            current["archived_reason"] = ""
            current["last_discovered_at"] = tick
            current["updated_at"] = tick
            by_name[name] = normalize_session_target(current)
        for name, current in by_name.items():
            if not name or name in discovered_names:
                continue
            if bool(current.get("archived", False)):
                continue
            current["enabled"] = False
            current["archived"] = True
            current["archived_at"] = tick
            current["archived_reason"] = "missing_from_latest_discovery"
            current["updated_at"] = tick
            by_name[name] = normalize_session_target(current)
            archived_count += 1
        merged = sort_session_targets(by_name.values())
        updated = self.save(
            {
                "session_targets_managed": True,
                "session_targets": merged,
            }
        )
        latest_targets = normalize_session_targets(updated.get("session_targets"))
        active_targets = [item for item in latest_targets if not bool(item.get("archived", False))]
        return {
            "settings": updated,
            "items": active_targets,
            "all_items": latest_targets,
            "added_count": added_count,
            "discovered_count": discovered_count,
            "archived_count": archived_count,
            "warnings": warnings,
        }

    def update_session_target(self, session_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        settings = self.get()
        items = normalize_session_targets(settings.get("session_targets"))
        by_name = {str(item.get("name") or ""): dict(item) for item in items}
        name = normalize_session_name(session_name)
        if not name:
            raise ValueError("session name is required")
        current = by_name.get(name) or {
            "name": name,
            "display_name": name,
            "enabled": bool(settings.get("respond_all_unread_sessions", False)),
            "exact": True,
            "conversation_type": normalized_conversation_type(str((patch or {}).get("conversation_type") or "unknown")),
            "source": "manual",
            "updated_at": now_iso(),
            "archived": False,
            "archived_at": "",
            "archived_reason": "",
        }
        warnings: list[str] = []
        if "enabled" in (patch or {}):
            current["enabled"] = bool((patch or {}).get("enabled"))
        if "exact" in (patch or {}):
            current["exact"] = bool((patch or {}).get("exact"))
        if "conversation_type" in (patch or {}):
            current["conversation_type"] = normalized_conversation_type((patch or {}).get("conversation_type"))
        if "display_name" in (patch or {}):
            display_name = normalize_session_name((patch or {}).get("display_name"))
            if display_name:
                current["display_name"] = display_name
        if bool(current.get("enabled", False)):
            current["archived"] = False
            current["archived_at"] = ""
            current["archived_reason"] = ""
        current["source"] = str((patch or {}).get("source") or current.get("source") or "manual")
        current["updated_at"] = now_iso()
        by_name[name] = normalize_session_target(current)
        updated = self.save(
            {
                "session_targets_managed": True,
                "session_targets": sort_session_targets(by_name.values()),
            }
        )
        latest_targets = normalize_session_targets(updated.get("session_targets"))
        active_targets = [entry for entry in latest_targets if not bool(entry.get("archived", False))]
        item = next((entry for entry in latest_targets if str(entry.get("name") or "") == name), normalize_session_target(current))
        return {
            "settings": updated,
            "item": item,
            "items": active_targets,
            "all_items": latest_targets,
            "warnings": warnings,
        }

    @staticmethod
    def status_text(settings: dict[str, Any]) -> str:
        if not settings.get("enabled"):
            return "已关闭，不会自动回复客户。"
        mode = str(settings.get("reply_mode") or "")
        if mode == "record_only":
            return "只记录消息，不自动回复。"
        if mode == "manual_assist":
            return "会生成回复建议，等待人工发送。"
        if mode == "guarded_auto":
            return "只在命中可靠知识时谨慎自动回复。"
        if mode == "full_auto":
            return "会按知识库自动回复，风险问题仍转人工。"
        return "已启用。"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_session_name(value: Any) -> str:
    text = str(value or "").strip()
    return strip_session_time_suffix(text)


def strip_session_time_suffix(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    patterns = (
        r"(?:今天|昨天|前天)?\d{1,2}:\d{2}$",
        r"(?:星期|周)[一二三四五六日天]$",
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}$",
        r"\d{1,2}[/-]\d{1,2}$",
    )
    stripped = text
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            updated = re.sub(pattern, "", stripped).strip()
            if updated != stripped:
                stripped = updated
                changed = True
    return stripped or text


def normalized_conversation_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"private", "group", "file_transfer", "system", "unknown"}:
        return text
    return "unknown"


def infer_conversation_type(name: str, session: dict[str, Any] | None = None) -> str:
    payload = session if isinstance(session, dict) else {}
    explicit = str(payload.get("conversation_type") or payload.get("type") or "").strip().lower()
    if explicit in {"private", "group", "file_transfer", "system"}:
        return explicit
    if name in {"文件传输助手", "File Transfer"}:
        return "file_transfer"
    if re.search(r"(群|群聊|chatroom|room)", name, re.IGNORECASE):
        return "group"
    if re.search(r"(微信团队|系统消息|订阅号|服务通知)", name, re.IGNORECASE):
        return "system"
    return "private"


def normalize_session_target(raw: dict[str, Any]) -> dict[str, Any]:
    name = normalize_session_name(raw.get("name") or raw.get("target_name") or raw.get("display_name"))
    if not name:
        return {}
    display_name = normalize_session_name(raw.get("display_name") or name) or name
    enabled = bool(raw.get("enabled", False))
    exact = bool(raw.get("exact", True))
    archived = bool(raw.get("archived", False))
    conversation_type = normalized_conversation_type(
        raw.get("conversation_type") or infer_conversation_type(name, raw if isinstance(raw, dict) else {})
    )
    source = str(raw.get("source") or "manual")
    updated_at = str(raw.get("updated_at") or now_iso())
    archived_at = str(raw.get("archived_at") or "")
    archived_reason = str(raw.get("archived_reason") or "")
    last_discovered_at = str(raw.get("last_discovered_at") or "")
    if enabled:
        archived = False
    if not archived:
        archived_at = ""
        archived_reason = ""
    item = {
        "name": name,
        "display_name": display_name,
        "enabled": enabled,
        "exact": exact,
        "archived": archived,
        "archived_at": archived_at,
        "archived_reason": archived_reason,
        "last_discovered_at": last_discovered_at,
        "conversation_type": conversation_type,
        "source": source,
        "updated_at": updated_at,
    }
    if "max_batch_messages" in raw:
        try:
            item["max_batch_messages"] = max(1, int(raw.get("max_batch_messages") or 0))
        except (TypeError, ValueError):
            pass
    return item


def normalize_session_targets(raw_items: Any) -> list[dict[str, Any]]:
    items = raw_items if isinstance(raw_items, list) else []
    by_name: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_session_target(raw)
        name = str(normalized.get("name") or "")
        if not name:
            continue
        by_name[name] = normalized
    return sort_session_targets(by_name.values())


def sort_session_targets(items: Any) -> list[dict[str, Any]]:
    normalized = [normalize_session_target(item) for item in items if isinstance(item, dict)]
    cleaned = [item for item in normalized if item]
    return sorted(
        cleaned,
        key=lambda item: (
            1 if item.get("archived", False) else 0,
            0 if item.get("enabled") else 1,
            str(item.get("conversation_type") or ""),
            str(item.get("display_name") or item.get("name") or ""),
        ),
    )
