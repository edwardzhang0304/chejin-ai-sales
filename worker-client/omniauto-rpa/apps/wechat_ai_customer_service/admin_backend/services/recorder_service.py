"""AI smart recorder orchestration on top of the shared raw message store."""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .raw_message_learning_service import RawMessageLearningService
from .raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnector
from apps.wechat_ai_customer_service.knowledge_paths import tenant_runtime_root
from apps.wechat_ai_customer_service.wechat_message_normalizer import normalize_wechat_message_record
from apps.wechat_ai_customer_service.wechat_message_envelope import (
    apply_message_envelope_to_record,
    build_message_envelope,
    export_risk_flags,
)


DEFAULT_SETTINGS = {
    "enabled": True,
    "private_recording_enabled": True,
    "group_recording_enabled": True,
    "file_transfer_recording_enabled": True,
    "notify_on_collect": False,
    "auto_learn": False,
    "use_llm": True,
    "capture_interval_seconds": 5,
    "history_backfill_enabled": True,
    "history_backfill_load_times": 3,
    "history_backfill_max_load_times": 5,
    "capture_anchor_recent_limit": 80,
    "runtime_max_runtime_seconds": 0,
    "runtime_liveness_interval_seconds": 60,
    "runtime_liveness_stale_seconds": 240,
}
RECORDER_DISCOVERY_SOURCE_TYPE = "wechat_session_discovery"
RECORDER_TEST_SOURCE_TYPE = "live_recorder_test"
MIN_CONTENT_ONLY_ANCHOR_LENGTH = 20


class RecorderService:
    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.raw_store = RawMessageStore(tenant_id=tenant_id)
        self.learning = RawMessageLearningService(tenant_id=tenant_id)
        self.connector = WeChatConnector()
        self._last_idle_foreground_refresh_at = 0.0

    @property
    def settings_path(self) -> Path:
        return tenant_runtime_root(self.raw_store.tenant_id) / "recorder" / "settings.json"

    def settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **(payload if isinstance(payload, dict) else {})}

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        settings = {**self.settings(), **{key: value for key, value in patch.items() if key in DEFAULT_SETTINGS}}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings_path)
        return settings

    def summary(self) -> dict[str, Any]:
        raw = self.raw_store.summary()
        settings = self.settings()
        conversations = self._live_session_conversations(self.raw_store.list_conversations(status="all", limit=500))
        selected_groups = [item for item in conversations if item.get("conversation_type") == "group" and item.get("selected_by_user")]
        selected_private = [item for item in conversations if item.get("conversation_type") == "private" and item.get("selected_by_user")]
        selected_file_transfer = [item for item in conversations if item.get("conversation_type") == "file_transfer" and item.get("selected_by_user")]
        return {
            "settings": settings,
            "status": status_text(settings),
            "raw": raw,
            "selected_group_count": len(selected_groups),
            "selected_private_count": len(selected_private),
            "selected_file_transfer_count": len(selected_file_transfer),
            "selected_conversation_count": len(selected_groups) + len(selected_private) + len(selected_file_transfer),
            "selected_groups": selected_groups,
            "selected_private": selected_private,
            "selected_file_transfer": selected_file_transfer,
        }

    def discover_sessions(self) -> dict[str, Any]:
        # Discovery must reflect the currently logged-in WeChat window in real time,
        # so we force a fresh sidecar attachment instead of daemon cache reuse.
        payload = self.connector.list_sessions(fresh=True)
        sessions = payload.get("sessions", []) if payload.get("ok") else []
        existing_records = self.raw_store.list_conversations(status="all", limit=500)
        existing_by_name = {
            str(item.get("target_name") or "").strip(): item
            for item in existing_records
            if isinstance(item, dict) and str(item.get("target_name") or "").strip()
        }
        discovered_names: set[str] = set()
        items = []
        for session in sessions or []:
            if not isinstance(session, dict):
                continue
            conversation = normalize_session(session)
            target_name = str(conversation.get("target_name") or "").strip()
            if not target_name:
                continue
            discovered_names.add(target_name)
            existing = existing_by_name.get(target_name)
            if existing:
                conversation = {**conversation, **preserved_selection(existing), "status": "active"}
            items.append(self.raw_store.upsert_conversation(conversation))
        archived_items: list[dict[str, Any]] = []
        if payload.get("ok"):
            archived_items = self._archive_missing_discovered_conversations(
                existing_records=existing_records,
                discovered_names=discovered_names,
            )
        return {
            "ok": bool(payload.get("ok")),
            "items": items,
            "archived_items": archived_items,
            "archived_count": len(archived_items),
            "source": payload,
        }

    def ensure_conversation(self, record: dict[str, Any]) -> dict[str, Any]:
        target_name = str(record.get("target_name") or record.get("name") or record.get("display_name") or "").strip()
        if not target_name:
            raise ValueError("target_name is required")
        existing = self.find_conversation_by_name(target_name)
        payload = {**(existing or {}), **record, "target_name": target_name, "display_name": record.get("display_name") or target_name}
        return self.raw_store.upsert_conversation(payload)

    def list_conversations(self, *, conversation_type: str = "", status: str = "all") -> list[dict[str, Any]]:
        conversations = self.raw_store.list_conversations(conversation_type=conversation_type, status=status, limit=500)
        return self._live_session_conversations(conversations)

    def update_conversation(self, conversation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = next((item for item in self.raw_store.list_conversations(status="all", limit=500) if item.get("conversation_id") == conversation_id), None)
        if not current:
            raise FileNotFoundError(conversation_id)
        updated = {
            **current,
            "selected_by_user": bool(patch.get("selected_by_user", current.get("selected_by_user", False))),
            "conversation_type": normalized_patch_conversation_type(patch.get("conversation_type"), current.get("conversation_type")),
            "target_name": str(patch.get("target_name") or current.get("target_name") or ""),
            "display_name": str(patch.get("display_name") or current.get("display_name") or current.get("target_name") or ""),
            "status": str(patch.get("status") or current.get("status") or "active"),
            "notify_enabled": bool(patch.get("notify_enabled", current.get("notify_enabled", False))),
            "learning_enabled": patch.get("learning_enabled", current.get("learning_enabled", True)) is not False,
            "updated_at": now_iso(),
        }
        return self.raw_store.upsert_conversation(updated)

    def capture_selected_once(self, *, send_notifications: bool = False) -> dict[str, Any]:
        settings = self.settings()
        if settings.get("enabled", True) is False:
            return {
                "ok": True,
                "enabled": False,
                "message": "AI智能记录员总开关已关闭，本轮不采集。",
                "conversation_count": 0,
                "inserted_count": 0,
                "items": [],
            }
        conversations = [
            item
            for item in self.list_conversations(status="active")
            if item.get("selected_by_user") and conversation_enabled_for_capture(item, settings)
        ]
        if not conversations:
            idle_refresh = self._refresh_wechat_foreground_when_idle()
            return {
                "ok": True,
                "enabled": True,
                "message": "当前没有勾选监听会话，本轮不采集。",
                "conversation_count": 0,
                "inserted_count": 0,
                "items": [],
                "idle_foreground_refresh": idle_refresh,
            }
        results = []
        for conversation in conversations:
            result = self.capture_conversation(
                conversation,
                auto_learn=bool(settings.get("auto_learn", True)),
                use_llm=settings.get("use_llm", True) is not False,
                send_notification=bool(send_notifications and (settings.get("notify_on_collect") or conversation.get("notify_enabled"))),
                settings=settings,
            )
            results.append(result)
        return {
            "ok": True,
            "enabled": True,
            "conversation_count": len(conversations),
            "inserted_count": sum(int(item.get("inserted_count", 0) or 0) for item in results),
            "items": results,
        }

    def _refresh_wechat_foreground_when_idle(self) -> dict[str, Any]:
        enabled = env_flag("WECHAT_RECORDER_IDLE_FOREGROUND_REFRESH", default=True)
        if not enabled:
            return {"attempted": False, "enabled": False}
        interval_seconds = bounded_float(
            os.getenv("WECHAT_RECORDER_IDLE_FOREGROUND_REFRESH_INTERVAL_SECONDS"),
            default=6.0,
            minimum=2.0,
            maximum=60.0,
        )
        now = time.time()
        if now - float(self._last_idle_foreground_refresh_at or 0.0) < interval_seconds:
            return {
                "attempted": False,
                "enabled": True,
                "reason": "interval_not_reached",
                "interval_seconds": interval_seconds,
            }
        self._last_idle_foreground_refresh_at = now
        try:
            payload = self.connector.call_compat_sidecar(["sessions"], allow_failure=True)
        except Exception as exc:
            return {
                "attempted": True,
                "enabled": True,
                "ok": False,
                "error": repr(exc),
                "interval_seconds": interval_seconds,
            }
        return {
            "attempted": True,
            "enabled": True,
            "ok": bool(payload.get("ok")),
            "online": bool(payload.get("online")),
            "state": str(payload.get("state") or ""),
            "interval_seconds": interval_seconds,
        }

    def capture_conversation(
        self,
        conversation: dict[str, Any],
        *,
        auto_learn: bool,
        use_llm: bool,
        send_notification: bool,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_name = str(conversation.get("target_name") or conversation.get("display_name") or "")
        session_key = str(conversation.get("session_key") or conversation.get("session_id") or "")
        active_settings = settings if isinstance(settings, dict) else self.settings()
        payload = self.connector.get_messages(
            target_name,
            exact=conversation.get("exact", True) is not False,
            session_key=session_key,
        )
        if not payload.get("ok"):
            return {"ok": False, "conversation_id": conversation.get("conversation_id"), "messages": payload}
        payload = normalize_recorder_capture_payload(payload, conversation)
        recovered_payload = self._recover_capture_window(conversation, payload, settings=active_settings)
        result = self.raw_store.upsert_messages(
            conversation,
            [item for item in recovered_payload.get("messages", []) or [] if isinstance(item, dict)],
            source_module="smart_recorder",
            learning_enabled=bool(auto_learn) and conversation.get("learning_enabled", True) is not False,
            create_batch=True,
            batch_reason="recorder_capture",
        )
        result["capture_quality"] = summarize_capture_quality(recovered_payload.get("messages", []) or [])
        if recovered_payload.get("_capture_recovery"):
            result["capture_recovery"] = recovered_payload["_capture_recovery"]
        if auto_learn and result.get("batch"):
            result["learning"] = self.learning.process_batch(str(result["batch"].get("batch_id") or ""), use_llm=use_llm)
        if send_notification and result.get("inserted_count"):
            result["notification"] = self.connector.send_text(
                target_name,
                f"已自动记录 {result['inserted_count']} 条新消息，后续可在后台导出或复核。",
                exact=conversation.get("exact", True) is not False,
                session_key=session_key,
            )
        return result

    def _recover_capture_window(
        self,
        conversation: dict[str, Any],
        payload: dict[str, Any],
        *,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        visible_messages = [item for item in payload.get("messages", []) or [] if isinstance(item, dict)]
        metadata: dict[str, Any] = {
            "enabled": bool(settings.get("history_backfill_enabled", True)),
            "initial_message_count": len(visible_messages),
            "history_load_applied": False,
            "anchor_found": False,
            "gap_risk": False,
        }
        if not metadata["enabled"]:
            return {**payload, "_capture_recovery": {**metadata, "reason": "disabled"}}

        recent_limit = bounded_int(settings.get("capture_anchor_recent_limit"), default=80, minimum=1, maximum=300)
        anchors = self._recent_capture_anchor_keys(conversation, limit=recent_limit)
        metadata["anchor_count"] = len(anchors)
        if not anchors:
            return {**payload, "_capture_recovery": {**metadata, "reason": "first_capture_no_anchor"}}

        anchor_index = find_latest_anchor_index(visible_messages, anchors)
        if anchor_index >= 0:
            return {
                **payload,
                "_capture_recovery": {
                    **metadata,
                    "anchor_found": True,
                    "anchor_source": "initial_window",
                    "anchor_index": anchor_index,
                    "final_message_count": len(visible_messages),
                },
            }

        max_load_times = bounded_int(settings.get("history_backfill_max_load_times"), default=5, minimum=0, maximum=10)
        load_times = bounded_int(settings.get("history_backfill_load_times"), default=3, minimum=0, maximum=max_load_times)
        if load_times <= 0:
            return {
                **payload,
                "_capture_recovery": {
                    **metadata,
                    "gap_risk": True,
                    "reason": "anchor_missing_history_load_disabled",
                    "final_message_count": len(visible_messages),
                },
            }

        target_name = str(conversation.get("target_name") or conversation.get("display_name") or "")
        session_key = str(conversation.get("session_key") or conversation.get("session_id") or "")
        try:
            loaded_payload = self.connector.get_messages(
                target_name,
                exact=conversation.get("exact", True) is not False,
                history_load_times=load_times,
                session_key=session_key,
            )
        except Exception as exc:
            return {
                **payload,
                "_capture_recovery": {
                    **metadata,
                    "gap_risk": True,
                    "reason": "history_load_exception",
                    "error": repr(exc),
                    "history_load_times": load_times,
                    "final_message_count": len(visible_messages),
                },
            }

        if not loaded_payload.get("ok"):
            return {
                **payload,
                "_capture_recovery": {
                    **metadata,
                    "gap_risk": True,
                    "reason": "history_load_failed",
                    "history_load_times": load_times,
                    "history_load_result": loaded_payload,
                    "final_message_count": len(visible_messages),
                },
            }

        loaded_payload = normalize_recorder_capture_payload(loaded_payload, conversation)
        loaded_messages = [item for item in loaded_payload.get("messages", []) or [] if isinstance(item, dict)]
        merged_messages = merge_capture_message_windows(loaded_messages, visible_messages)
        recovered_anchor_index = find_latest_anchor_index(merged_messages, anchors)
        sidecar_history_load = loaded_payload.get("history_load") if isinstance(loaded_payload.get("history_load"), dict) else {}
        recovery_meta = {
            **metadata,
            "history_load_applied": True,
            "history_load_times": load_times,
            "history_load": sidecar_history_load,
            "loaded_message_count": len(loaded_messages),
            "final_message_count": len(merged_messages),
            "anchor_found": recovered_anchor_index >= 0,
            "anchor_source": "history_backfill" if recovered_anchor_index >= 0 else "",
            "anchor_index": recovered_anchor_index,
            "gap_risk": recovered_anchor_index < 0,
        }
        if recovered_anchor_index < 0:
            recovery_meta["reason"] = "anchor_missing_after_history_load"
        return {**loaded_payload, "messages": merged_messages, "_capture_recovery": recovery_meta}

    def _recent_capture_anchor_keys(self, conversation: dict[str, Any], *, limit: int) -> set[str]:
        conversation_id = str(conversation.get("conversation_id") or "").strip()
        if not conversation_id:
            return set()
        recent_messages = self.raw_store.list_messages_advanced(conversation_id=conversation_id, limit=limit)
        anchors: set[str] = set()
        for message in recent_messages:
            anchors.update(capture_anchor_keys(message))
            raw_payload = message.get("raw_payload") if isinstance(message.get("raw_payload"), dict) else {}
            anchors.update(capture_anchor_keys(raw_payload))
        return anchors

    def find_conversation_by_name(self, target_name: str) -> dict[str, Any] | None:
        for item in self.raw_store.list_conversations(status="all", limit=500):
            if str(item.get("target_name") or "") == target_name:
                return item
        return None

    def _archive_missing_discovered_conversations(
        self,
        *,
        existing_records: list[dict[str, Any]],
        discovered_names: set[str],
    ) -> list[dict[str, Any]]:
        archived_items: list[dict[str, Any]] = []
        if not existing_records:
            return archived_items
        snapshot_time = now_iso()
        for existing in existing_records:
            target_name = str(existing.get("target_name") or "").strip()
            if not target_name or target_name in discovered_names:
                continue
            if str(existing.get("status") or "active") != "active":
                continue
            source = existing.get("source") if isinstance(existing.get("source"), dict) else {}
            archived_payload = {
                **existing,
                "status": "ignored",
                "selected_by_user": False,
                "updated_at": snapshot_time,
                "source": {
                    **source,
                    "archived_by_discover": True,
                    "archived_reason": "missing_from_latest_discovery",
                    "archived_at": snapshot_time,
                },
            }
            archived_items.append(self.raw_store.upsert_conversation(archived_payload))
        return archived_items

    def _live_session_conversations(self, conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in conversations:
            if not isinstance(item, dict):
                continue
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            source_type = str(source.get("type") or "").strip().lower()
            if not is_supported_recorder_source_type(source_type):
                continue
            items.append(item)
        return items


def normalize_session(session: dict[str, Any]) -> dict[str, Any]:
    name = str(session.get("name") or session.get("title") or "").strip()
    return {
        "target_name": name,
        "display_name": name,
        "conversation_type": infer_conversation_type(name, session),
        "status": "active",
        "exact": True,
        "selected_by_user": False,
        "learning_enabled": True,
        "notify_enabled": False,
        "source": {"type": RECORDER_DISCOVERY_SOURCE_TYPE},
        "raw_payload": session,
    }


def normalize_recorder_capture_payload(payload: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    conversation_type = str(conversation.get("conversation_type") or "unknown")
    target_name = str(conversation.get("target_name") or conversation.get("display_name") or "")
    known_speakers = recorder_known_speaker_names(conversation)
    normalized_messages: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for item in payload.get("messages", []) or []:
        if not isinstance(item, dict):
            continue
        next_item = normalize_wechat_message_record(
            item,
            conversation_type=conversation_type,
            target_name=target_name,
            known_speakers=known_speakers,
            allow_unlisted_name_like_prefix=True,
        )
        envelope = build_message_envelope(
            next_item,
            source_adapter=str(next_item.get("source_adapter") or ""),
            conversation=conversation,
        )
        next_item = apply_message_envelope_to_record(next_item, envelope)
        if next_item.get("ocr_speaker_prefix") or next_item.get("quality_flags") or next_item.get("quoted_fragments"):
            changed.append(
                {
                    "id": str(next_item.get("id") or next_item.get("message_id") or ""),
                    "speaker_name": str(next_item.get("speaker_name") or ""),
                    "content_preview": str(next_item.get("content") or "")[:120],
                    "quality_flags": list(next_item.get("quality_flags") or []),
                    "risk_flags": export_risk_flags(envelope),
                }
            )
        normalized_messages.append(next_item)
    if not changed:
        return {**payload, "messages": normalized_messages}
    return {
        **payload,
        "messages": normalized_messages,
        "_content_normalization": {
            "changed_count": len(changed),
            "changed_messages": changed[:20],
            "conversation_type": conversation_type,
            "target_name": target_name,
        },
    }


def recorder_known_speaker_names(conversation: dict[str, Any]) -> list[str]:
    names = [
        str(conversation.get("target_name") or ""),
        str(conversation.get("display_name") or ""),
        str(conversation.get("group_name") or ""),
    ]
    raw_payload = conversation.get("raw_payload") if isinstance(conversation.get("raw_payload"), dict) else {}
    for key in ("name", "title", "display_name", "target_name", "group_name"):
        text = str((raw_payload or {}).get(key) or "").strip()
        if text:
            names.append(text)
    for key in ("members", "participants", "member_names"):
        for item in (raw_payload or {}).get(key, []) or []:
            if isinstance(item, dict):
                text = str(item.get("name") or item.get("display_name") or item.get("remark") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                names.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "", name).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def summarize_capture_quality(messages: list[dict[str, Any]]) -> dict[str, Any]:
    flags: dict[str, int] = {}
    quoted_count = 0
    risk_count = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        item_flags = [str(flag) for flag in (item.get("quality_flags") or []) if str(flag)]
        for flag in item_flags:
            flags[flag] = flags.get(flag, 0) + 1
        if item.get("quoted_fragments"):
            quoted_count += 1
        if export_risk_flags(build_message_envelope(item, source_adapter=str(item.get("source_adapter") or ""))):
            risk_count += 1
    return {
        "message_count": len([item for item in messages if isinstance(item, dict)]),
        "quality_flags": flags,
        "quoted_message_count": quoted_count,
        "risk_message_count": risk_count,
    }


def infer_conversation_type(name: str, session: dict[str, Any]) -> str:
    explicit = str(session.get("conversation_type") or session.get("type") or "").lower()
    if explicit in {"private", "group", "file_transfer", "system"}:
        return explicit
    if name in {"文件传输助手", "File Transfer"}:
        return "file_transfer"
    if re.search(r"(群|群聊|chatroom|room)", name, re.IGNORECASE):
        return "group"
    return "private"


def preserved_selection(existing: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": existing.get("conversation_id"),
        "conversation_type": existing.get("conversation_type") or "unknown",
        "status": existing.get("status") or "active",
        "selected_by_user": bool(existing.get("selected_by_user", False)),
        "learning_enabled": existing.get("learning_enabled", True) is not False,
        "notify_enabled": bool(existing.get("notify_enabled", False)),
    }


def is_supported_recorder_source_type(source_type: str) -> bool:
    source = str(source_type or "").strip().lower()
    if source == RECORDER_DISCOVERY_SOURCE_TYPE:
        return True
    if source == RECORDER_TEST_SOURCE_TYPE:
        return True
    if source.startswith("live_recorder_"):
        return True
    return False


def normalized_patch_conversation_type(value: Any, current: Any) -> str:
    text = str(value or current or "unknown").strip().lower()
    if text in {"private", "group", "file_transfer", "system", "unknown"}:
        return text
    return str(current or "unknown")


def conversation_enabled_for_capture(conversation: dict[str, Any], settings: dict[str, Any]) -> bool:
    if settings.get("enabled", True) is False:
        return False
    conversation_type = str(conversation.get("conversation_type") or "unknown")
    if conversation_type == "group":
        return settings.get("group_recording_enabled", True) is not False
    if conversation_type == "file_transfer":
        return settings.get("file_transfer_recording_enabled", True) is not False
    if conversation_type == "private":
        return settings.get("private_recording_enabled", True) is not False
    return False


def merge_capture_message_windows(*windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for window in windows:
        for item in window or []:
            if not isinstance(item, dict):
                continue
            key = primary_capture_message_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def find_latest_anchor_index(messages: list[dict[str, Any]], anchors: set[str]) -> int:
    if not messages or not anchors:
        return -1
    for index in range(len(messages) - 1, -1, -1):
        if capture_anchor_keys(messages[index]) & anchors:
            return index
    return -1


def primary_capture_message_key(message: dict[str, Any]) -> str:
    keys = capture_anchor_keys(message)
    for prefix in ("id:", "composite:", "semantic:", "content:"):
        for key in sorted(keys):
            if key.startswith(prefix):
                return key
    return "fallback:" + digest_anchor(
        json.dumps(
            {
                "id": message.get("id") or message.get("message_id") or "",
                "sender": message.get("sender") or message.get("sender_role") or "",
                "type": message.get("type") or message.get("content_type") or "",
                "time": message.get("time") or message.get("message_time") or "",
                "content": message.get("content") or message.get("text") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def capture_anchor_keys(message: dict[str, Any]) -> set[str]:
    message_id = str(message.get("id") or message.get("message_id") or "").strip()
    sender = str(message.get("sender") or message.get("sender_role") or "").strip()
    content_type = str(message.get("type") or message.get("content_type") or "").strip()
    message_time = str(message.get("time") or message.get("message_time") or "").strip()
    content = normalize_anchor_content(str(message.get("content") or message.get("text") or ""))
    keys: set[str] = set()
    if message_id:
        keys.add(f"id:{message_id}")
    if sender and content_type and message_time and content:
        keys.add("composite:" + digest_anchor("|".join([sender, content_type, message_time, content])))
    if sender and content_type and content and len(content) >= MIN_CONTENT_ONLY_ANCHOR_LENGTH:
        keys.add("semantic:" + digest_anchor("|".join([sender, content_type, content])))
    if len(content) >= MIN_CONTENT_ONLY_ANCHOR_LENGTH:
        keys.add("content:" + digest_anchor(content))
    return keys


def normalize_anchor_content(value: str) -> str:
    return re.sub(r"\s+", "\n", str(value or "").strip())


def digest_anchor(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), min(float(parsed), float(maximum)))


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def status_text(settings: dict[str, Any]) -> str:
    if settings.get("enabled", True) is False:
        return "已关闭，不会自动采集或整理聊天记录。"
    return "已开启，可按已选择会话自动采集聊天记录。"
