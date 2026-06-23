"""Session identity and lightweight local ledger for RPA customer service.

The ledger is the local source of truth for conversation state: session binding,
captured inputs, pending reply anchors, sent reply anchors, and compact context.
It is not an authority source for product facts or formal policy. Brain First
still owns customer-visible reply strategy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root
from apps.wechat_ai_customer_service.message_identity import (
    canonical_input_message_id,
    canonical_visual_message_id,
)

MAX_LEDGER_RECENT_MESSAGES = 80
MAX_LEDGER_EVENT_MESSAGES = 30
MAX_LEDGER_CONTEXT_LINES = 14
MAX_LEDGER_MESSAGE_CHARS = 600


def utcnow_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_display_name(value: Any) -> str:
    return str(value or "").strip()


def normalize_conversation_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"private", "group", "file_transfer", "system"}:
        return text
    return "unknown"


def stable_hash(*parts: Any, length: int = 20) -> str:
    seed = json.dumps([str(item) for item in parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[: max(8, int(length or 20))]


def stable_session_key(
    display_name: Any,
    *,
    conversation_type: Any = "unknown",
    row_fingerprint: dict[str, Any] | None = None,
    explicit_key: Any = "",
) -> str:
    """Return a stable internal key that is not merely the display name.

    When an explicit key comes from the sidecar/session binding layer, keep it.
    Otherwise use type + display name.  Row fingerprint is deliberately used only
    when it includes a duplicate discriminator, because row order alone can move
    as conversations receive messages.
    """

    explicit = str(explicit_key or "").strip()
    if explicit:
        return explicit
    name = normalize_display_name(display_name)
    ctype = normalize_conversation_type(conversation_type)
    fingerprint = row_fingerprint if isinstance(row_fingerprint, dict) else {}
    duplicate_key = str(fingerprint.get("duplicate_discriminator") or "").strip()
    return "wx:rpa:v1:" + stable_hash(ctype, name, duplicate_key)


def row_fingerprint_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    direct = payload.get("row_fingerprint")
    if isinstance(direct, dict):
        return dict(direct)
    result: dict[str, Any] = {}
    for key in ("center_y", "left", "right", "top", "bottom"):
        if key in payload:
            result[key] = payload.get(key)
    badge_meta = payload.get("unread_badge_meta")
    if isinstance(badge_meta, dict):
        bbox = badge_meta.get("bbox") or badge_meta.get("bounds")
        if bbox:
            result["last_unread_badge_bbox"] = bbox
    preview = str(payload.get("content") or payload.get("preview") or "").strip()
    if preview:
        result["last_preview_digest"] = stable_hash(preview, length=16)
    return result


def session_key_from_payload(payload: dict[str, Any] | None, *, fallback_name: Any = "") -> str:
    payload = payload if isinstance(payload, dict) else {}
    name = payload.get("name") or payload.get("title") or payload.get("target_name") or fallback_name
    return stable_session_key(
        name,
        conversation_type=payload.get("conversation_type") or payload.get("type") or "unknown",
        row_fingerprint=row_fingerprint_from_payload(payload),
        explicit_key=payload.get("session_key"),
    )


def safe_filename(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text.strip("._") or "session"


def ledger_message_content_key(message: dict[str, Any]) -> str:
    sender = str(message.get("sender") or "")
    content = " ".join(str(message.get("content") or "").split())
    msg_type = str(message.get("type") or "")
    if not content:
        return ""
    return stable_hash(sender, msg_type, content, length=24)


def sanitize_ledger_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    content = " ".join(str(message.get("content") or "").split()).strip()
    msg_type = str(message.get("type") or "text").strip() or "text"
    if not content and msg_type == "text":
        return None
    if len(content) > MAX_LEDGER_MESSAGE_CHARS:
        content = content[:MAX_LEDGER_MESSAGE_CHARS].rstrip() + "..."
    sender = str(message.get("sender") or message.get("role") or "").strip()
    legacy_message_id = str(message.get("legacy_message_id") or message.get("id") or message.get("message_id") or "").strip()
    canonical_id = canonical_input_message_id(message)
    visual_id = canonical_visual_message_id(message)
    message_id = canonical_id or legacy_message_id
    time_value = str(message.get("time") or message.get("created_at") or "").strip()
    identity = canonical_id or message_id or stable_hash(sender, msg_type, content, time_value, length=24)
    content_key = ledger_message_content_key({"sender": sender, "type": msg_type, "content": content})
    return {
        "id": message_id,
        "legacy_message_id": legacy_message_id,
        "canonical_input_id": canonical_id,
        "canonical_visual_id": visual_id,
        "identity": identity,
        "sender": sender,
        "type": msg_type,
        "content": content,
        "time": time_value,
        "content_key": content_key,
    }


def merge_recent_messages(
    existing: list[dict[str, Any]] | None,
    additions: list[dict[str, Any]] | None,
    *,
    limit: int = MAX_LEDGER_RECENT_MESSAGES,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw in list(existing or []) + list(additions or []):
        message = sanitize_ledger_message(raw)
        if not message:
            continue
        identity = str(message.get("identity") or message.get("id") or "").strip()
        if not identity:
            identity = stable_hash(message.get("sender"), message.get("type"), message.get("content"), message.get("time"), length=24)
            message["identity"] = identity
        if identity in seen:
            merged[seen[identity]] = message
            continue
        seen[identity] = len(merged)
        merged.append(message)
    return merged[-max(1, int(limit or MAX_LEDGER_RECENT_MESSAGES)) :]


def build_context_summary(messages: list[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for message in list(messages or [])[-MAX_LEDGER_CONTEXT_LINES:]:
        item = sanitize_ledger_message(message)
        if not item:
            continue
        sender = str(item.get("sender") or "").lower()
        if sender in {"customer", "user", "client"}:
            label = "客户"
        elif sender in {"self", "assistant", "service", "bot"}:
            label = "客服"
        else:
            label = "对话"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{label}: {content}")
    return "\n".join(lines[-MAX_LEDGER_CONTEXT_LINES:])


class SessionLedgerStore:
    """Append-only per-session ledger plus compact summary files."""

    def __init__(self, *, tenant_id: str | None = None, root: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.root = root or (tenant_runtime_root(self.tenant_id) / "customer_service" / "session_ledgers")

    def session_dir(self, session_key: str) -> Path:
        return self.root / safe_filename(session_key)

    def events_path(self, session_key: str) -> Path:
        return self.session_dir(session_key) / "events.jsonl"

    def summary_path(self, session_key: str) -> Path:
        return self.session_dir(session_key) / "summary.json"

    def load_summary(self, session_key: str) -> dict[str, Any]:
        path = self.summary_path(session_key)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_summary(self, session_key: str, summary: dict[str, Any]) -> None:
        path = self.summary_path(session_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(summary)
        payload["session_key"] = session_key
        payload["updated_at"] = utcnow_iso()
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def append_event(self, session_key: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": "ledger_" + stable_hash(session_key, event_type, utcnow_iso(), payload, length=24),
            "event_type": event_type,
            "session_key": session_key,
            "created_at": utcnow_iso(),
            **dict(payload),
        }
        path = self.events_path(session_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def record_capture(
        self,
        *,
        session_key: str,
        target_name: str,
        conversation_type: str,
        capture_id: str,
        messages: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        history_backfill: dict[str, Any],
        context_version: int,
    ) -> None:
        if not session_key:
            return
        sanitized_messages = [item for item in (sanitize_ledger_message(raw) for raw in messages) if item]
        sanitized_batch = [item for item in (sanitize_ledger_message(raw) for raw in batch) if item]
        message_ids = [
            str(item.get("identity") or item.get("canonical_input_id") or item.get("id") or "")
            for item in sanitized_batch
            if str(item.get("identity") or item.get("canonical_input_id") or item.get("id") or "")
        ]
        self.append_event(
            session_key,
            "capture_recorded",
            {
                "target_name": target_name,
                "conversation_type": conversation_type,
                "capture_id": capture_id,
                "context_version": context_version,
                "message_ids": message_ids,
                "message_count": len(messages),
                "batch_count": len(batch),
                "batch_messages": sanitized_batch[-MAX_LEDGER_EVENT_MESSAGES:],
                "history_continuity": str(history_backfill.get("history_continuity") or ""),
                "history_backfill": history_backfill,
            },
        )
        summary = self.load_summary(session_key)
        recent_messages = merge_recent_messages(
            summary.get("recent_messages") if isinstance(summary.get("recent_messages"), list) else [],
            sanitized_messages,
        )
        summary.update(
            {
                "display_name": target_name,
                "target_name": target_name,
                "conversation_type": conversation_type,
                "last_capture_id": capture_id,
                "last_capture_at": utcnow_iso(),
                "last_captured_message_id": message_ids[-1] if message_ids else summary.get("last_captured_message_id", ""),
                "last_captured_content_keys": [
                    str(item.get("content_key") or "")
                    for item in sanitized_batch
                    if str(item.get("content_key") or "")
                ][-20:],
                "last_unreplied_capture_id": capture_id if sanitized_batch else summary.get("last_unreplied_capture_id", ""),
                "last_unreplied_message_ids": message_ids[-20:] if sanitized_batch else summary.get("last_unreplied_message_ids", []),
                "last_unreplied_content_keys": [
                    str(item.get("content_key") or "")
                    for item in sanitized_batch
                    if str(item.get("content_key") or "")
                ][-20:] if sanitized_batch else summary.get("last_unreplied_content_keys", []),
                "recent_messages": recent_messages,
                "context_summary": build_context_summary(recent_messages),
                "last_history_continuity": str(history_backfill.get("history_continuity") or ""),
                "context_version": int(context_version or 0),
            }
        )
        self.save_summary(session_key, summary)

    def record_reply_sent(
        self,
        *,
        session_key: str,
        target_name: str,
        reply_id: str,
        input_message_ids: list[str],
        input_content_keys: list[str] | None = None,
        reply_text: str,
        send_result: dict[str, Any] | None = None,
    ) -> None:
        if not session_key:
            return
        reply_message = sanitize_ledger_message(
            {
                "id": reply_id,
                "sender": "assistant",
                "type": "text",
                "content": reply_text,
                "time": utcnow_iso(),
            }
        )
        self.append_event(
            session_key,
            "reply_sent",
            {
                "target_name": target_name,
                "reply_id": reply_id,
                "input_message_ids": list(input_message_ids or []),
                "input_content_keys": list(input_content_keys or []),
                "reply_message": reply_message or {},
                "reply_digest": stable_hash(reply_text, length=24) if reply_text else "",
                "send_ok": bool((send_result or {}).get("ok", True)),
            },
        )
        summary = self.load_summary(session_key)
        recent_messages = merge_recent_messages(
            summary.get("recent_messages") if isinstance(summary.get("recent_messages"), list) else [],
            [reply_message] if reply_message else [],
        )
        summary.update(
            {
                "display_name": target_name,
                "target_name": target_name,
                "last_reply_at": utcnow_iso(),
                "last_processed_message_id": (input_message_ids or [""])[-1],
                "last_processed_content_keys": list(input_content_keys or [])[-20:],
                "last_replied_message_id": (input_message_ids or [""])[-1],
                "last_successful_reply_digest": stable_hash(reply_text, length=24) if reply_text else "",
                "last_reply_id": reply_id,
                "last_unreplied_capture_id": "",
                "last_unreplied_message_ids": [],
                "last_unreplied_content_keys": [],
                "recent_messages": recent_messages,
                "context_summary": build_context_summary(recent_messages),
                "last_successful_reply_anchor": {
                    "message_ids": list(input_message_ids or [])[-20:],
                    "message_content_keys": list(input_content_keys or [])[-20:],
                    "reply_content_key": stable_hash(reply_text, length=24) if reply_text else "",
                    "reply_text_sample": str(reply_text or "").strip()[:160],
                    "processed_at": utcnow_iso(),
                    "send_verified": bool((send_result or {}).get("verified", (send_result or {}).get("ok", True))),
                },
            }
        )
        self.save_summary(session_key, summary)
