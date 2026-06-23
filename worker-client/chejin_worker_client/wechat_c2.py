from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import WechatReadTarget
from .storage import save_c2_state


REMARK_CODE_RE = re.compile(r"(?<![A-Za-z0-9])CJ[-A-Z0-9]{4,24}(?![A-Za-z0-9])", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_digest(payload: Any, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def extract_remark_codes(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        for match in REMARK_CODE_RE.findall(str(value or "")):
            code = match.upper()
            if code not in found:
                found.append(code)
    return found[:10]


def row_fingerprint(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:255]
    if value:
        return stable_digest(value, length=24)
    return stable_digest(fallback, length=24)


def sidecar_run_id(payload: dict[str, Any], prefix: str) -> str:
    for key in ("sidecar_run_id", "run_id", "trace_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:128]
    return f"{prefix}-{stable_digest(payload, length=20)}"


def build_scan_result_payload(sidecar_payload: dict[str, Any]) -> dict[str, Any]:
    started_at = now_iso()
    sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
    mapped: list[dict[str, Any]] = []
    for index, item in enumerate(sessions):
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("name") or item.get("title") or item.get("display_name") or "").strip()
        if not display_name:
            continue
        rpa_session_key = str(item.get("session_key") or "").strip() or f"session:{stable_digest([display_name, index], length=20)}"
        preview = str(item.get("content") or item.get("preview") or item.get("last_message_preview") or "")
        fingerprint = row_fingerprint(item.get("row_fingerprint"), fallback=f"{display_name}:{rpa_session_key}:{index}")
        mapped.append(
            {
                "rpa_session_key": rpa_session_key,
                "display_name": display_name[:255],
                "remark_code_candidates": extract_remark_codes(display_name, preview),
                "row_fingerprint": fingerprint,
                "unread_hint": bool(item.get("unread_signal") or item.get("unread") or item.get("unread_badge")),
                "last_message_preview": preview[:1000] or None,
                "ocr_confidence": item.get("ocr_confidence"),
            }
        )
    payload = {
        "scan_id": f"scan-{uuid.uuid4()}",
        "sidecar_run_id": sidecar_run_id(sidecar_payload, "sessions"),
        "wechat_account_hint": str(((sidecar_payload.get("window_probe") or {}).get("title") if isinstance(sidecar_payload.get("window_probe"), dict) else "") or "")[:128] or None,
        "started_at": started_at,
        "finished_at": now_iso(),
        "sessions": mapped,
        "evidence": {
            "screenshot": sidecar_payload.get("screenshot_path"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "ocr_items_count": sidecar_payload.get("ocr_items_count"),
        },
        "scan_failed": not bool(sidecar_payload.get("ok")),
        "error_code": None if sidecar_payload.get("ok") else str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "SESSION_SCAN_FAILED"),
    }
    save_c2_state("last_scan", {"scan_id": payload["scan_id"], "sidecar_run_id": payload["sidecar_run_id"], "session_count": len(mapped), "finished_at": payload["finished_at"]})
    return payload


def sender_role_hint(message: dict[str, Any]) -> str:
    value = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if value in {"self", "sales", "sales_candidate"}:
        return "self"
    if value in {"customer", "contact"}:
        return "customer"
    if value == "system":
        return "system"
    return "unknown"


def message_type(message: dict[str, Any]) -> str:
    value = str(message.get("type") or message.get("message_type") or "").strip().lower()
    if value in {"text", "image", "system"}:
        return value
    if message.get("image_local_path"):
        return "image"
    return "text"


def message_dedupe_key(target: WechatReadTarget, message: dict[str, Any], index: int) -> str:
    for key in ("dedupe_key", "id", "message_id"):
        value = str(message.get(key) or "").strip()
        if value:
            return f"{target.conversation_id}:{value}"[:255]
    content = str(message.get("content") or message.get("content_raw_ocr") or "")
    base = {
        "conversation_id": target.conversation_id,
        "sender": sender_role_hint(message),
        "type": message_type(message),
        "content": content,
        "time": message.get("time") or message.get("occurred_at") or "",
        "rect": message.get("bubble_rect") or {},
        "index": index,
    }
    return f"{target.conversation_id}:{stable_digest(base, length=32)}"[:255]


def build_message_ingest_payload(target: WechatReadTarget, sidecar_payload: dict[str, Any]) -> dict[str, Any]:
    messages = sidecar_payload.get("messages") if isinstance(sidecar_payload.get("messages"), list) else []
    mapped: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        image_local_path = str(item.get("image_local_path") or "").strip() or None
        if not content and not image_local_path:
            continue
        mapped.append(
            {
                "dedupe_key": message_dedupe_key(target, item, index),
                "sender_role_hint": sender_role_hint(item),
                "message_type": message_type(item),
                "content": content or None,
                "image_local_path": image_local_path,
                "occurred_at": item.get("occurred_at") or None,
                "ocr_confidence": item.get("ocr_confidence"),
                "raw_payload": item,
            }
        )
    finished_at = now_iso()
    payload = {
        "read_run_id": f"read-{uuid.uuid4()}",
        "conversation_id": target.conversation_id,
        "rpa_session_key": target.rpa_session_key,
        "messages": mapped,
        "evidence": {
            "screenshot": sidecar_payload.get("screenshot_path"),
            "adapter": sidecar_payload.get("adapter"),
            "state": sidecar_payload.get("state"),
            "ocr_items_count": sidecar_payload.get("ocr_items_count"),
            "finished_at": finished_at,
        },
    }
    save_c2_state(
        "last_message_read",
        {
            "read_run_id": payload["read_run_id"],
            "conversation_id": target.conversation_id,
            "rpa_session_key": target.rpa_session_key,
            "message_count": len(mapped),
            "finished_at": finished_at,
        },
    )
    return payload
