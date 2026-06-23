"""Helpers for routing real service chat samples into learning layers.

Real chat is valuable evidence for style and repeatable handling patterns, but
it is not authoritative formal knowledge until a human rewrites and approves it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.workflows.rag_experience_store import (
    normalize_space,
    now,
    score_record_quality,
    stable_digest,
    truncate,
)
from apps.wechat_ai_customer_service.workflows.style_memory_store import clean_style_reply, looks_like_product_master_data


REAL_CHAT_SOURCE_TYPES = {
    "cleaned_real_chat_pack",
    "real_chat",
    "real_chat_style",
    "wechat_raw_message",
    "raw_wechat_private",
    "raw_wechat_group",
    "raw_wechat_file_transfer",
}
REAL_CHAT_BATCH_MARKERS = ("realchat", "real_chat", "实盘聊天", "真实聊天", "微信聊天")
REAL_CHAT_ID_PREFIXES = ("chejin_real_",)


def formal_chat_item_is_real_chat(item: dict[str, Any], *, path: Path | None = None) -> bool:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    source_values = {
        str(source.get("type") or ""),
        str(source.get("original_type") or ""),
        str(source.get("source_type") or ""),
        str(source.get("candidate_source_type") or ""),
        str(details.get("source_type") or ""),
    }
    if any(value in REAL_CHAT_SOURCE_TYPES for value in source_values):
        return True
    item_id = str(item.get("id") or (path.stem if path else ""))
    if any(item_id.startswith(prefix) for prefix in REAL_CHAT_ID_PREFIXES):
        return True
    marker_text = " ".join(
        str(value or "")
        for value in (
            source.get("batch_token"),
            details.get("batch_token"),
            details.get("source_hint_id"),
            details.get("cleaning_kind"),
        )
    ).lower()
    return any(marker.lower() in marker_text for marker in REAL_CHAT_BATCH_MARKERS)


def formal_chat_item_to_experience(
    item: dict[str, Any],
    *,
    tenant_id: str,
    migration_id: str,
    source_file: str = "",
) -> dict[str, Any] | None:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    customer_message = normalize_space(str(data.get("customer_message") or item.get("customer_message") or ""))
    service_reply = normalize_space(str(data.get("service_reply") or item.get("service_reply") or ""))
    if not customer_message or not service_reply:
        return None

    item_id = str(item.get("id") or "")
    fingerprint = stable_digest(f"{tenant_id}|{item_id}|{customer_message}|{service_reply}", 20)
    experience_id = "rag_exp_realchat_" + fingerprint
    now_text = now()
    requires_handoff = bool(runtime.get("requires_handoff")) or str(runtime.get("risk_level") or "") == "high"
    hit_count = max(1, int(_coerce_int(details.get("hit_count"), 1)))
    scenario = str(details.get("scenario") or "")
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    hit_score = 0.66 if not requires_handoff else 0.24
    source_text = truncate(f"实盘客服话术样本：客户：{customer_message} 客服：{service_reply}", 800)
    record = {
        "experience_id": experience_id,
        "tenant_id": tenant_id,
        "status": "active",
        "source": "real_chat_style",
        "source_type": str(source.get("type") or "cleaned_real_chat_pack"),
        "source_path": source_file,
        "category": "chats",
        "formal_knowledge_policy": "experience_only_not_formal_knowledge",
        "promotion_policy": "manual_candidate_review_only",
        "summary": truncate(f"实盘话术样本：{scenario or '未分类'}；客户={customer_message}；客服={service_reply}", 160),
        "question": customer_message,
        "reply_text": service_reply,
        "target": "",
        "message_ids": [],
        "intent": ",".join(str(tag) for tag in data.get("intent_tags", []) or [] if str(tag)),
        "recommended_action": "handoff_required" if requires_handoff else "reply_style_reference",
        "safety": {"must_handoff": requires_handoff, "source_runtime": runtime},
        "rag_hit": {
            "chunk_id": "realchat_" + fingerprint,
            "source_id": item_id,
            "score": hit_score,
            "category": "chats",
            "source_type": "cleaned_real_chat_pack",
            "product_id": "",
            "text": source_text,
            "risk_terms": [],
        },
        "usage": {
            "reply_count": hit_count,
            "last_used_at": now_text,
        },
        "experience_review": {
            "status": "auto_kept",
            "auto_kept_at": now_text,
            "auto_keep_reason": "migrated_real_chat_sample_to_rag_first_learning_layer",
            "migration_id": migration_id,
        },
        "reviewed_by_user": False,
        "review_state": {
            "is_new": False,
            "new_reason": "migrated_real_chat_sample",
            "marked_at": now_text,
            "updated_at": now_text,
            "read_at": now_text,
            "read_by": "system",
        },
        "source_dialogue": {
            "customer_message": customer_message,
            "service_reply": service_reply,
            "scenario": scenario,
            "intent_tags": [str(tag) for tag in data.get("intent_tags", []) or [] if str(tag)],
            "tone_tags": [str(tag) for tag in data.get("tone_tags", []) or [] if str(tag)],
        },
        "migration": {
            "source_migration_id": migration_id,
            "from_layer": "formal_knowledge.chats",
            "from_item_id": item_id,
            "source_file": source_file,
        },
        "created_at": now_text,
        "updated_at": now_text,
    }
    record["quality"] = score_record_quality(record)
    return record


def formal_chat_item_to_style_example(
    item: dict[str, Any],
    *,
    tenant_id: str,
    migration_id: str,
    source_file: str = "",
) -> dict[str, Any] | None:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    customer_message = normalize_space(str(data.get("customer_message") or item.get("customer_message") or ""))
    service_reply = clean_style_reply(str(data.get("service_reply") or item.get("service_reply") or ""))
    if not customer_message or not service_reply:
        return None
    if looks_like_product_master_data(service_reply):
        return None
    item_id = str(item.get("id") or "")
    fingerprint = stable_digest(f"{tenant_id}|style|{item_id}|{customer_message}|{service_reply}", 20)
    hit_count = max(1, int(_coerce_int(details.get("hit_count"), 1)))
    return {
        "id": "style_realchat_" + fingerprint,
        "tenant_id": tenant_id,
        "source": "style_memory",
        "source_type": "cleaned_real_chat_pack",
        "source_id": item_id,
        "source_path": source_file,
        "customer_message": truncate(customer_message, 240),
        "service_reply": truncate(service_reply, 320),
        "scenario": str(details.get("scenario") or ""),
        "intent_tags": [str(tag) for tag in data.get("intent_tags", []) or [] if str(tag)],
        "tone_tags": [str(tag) for tag in data.get("tone_tags", []) or [] if str(tag)],
        "quality_score": min(0.96, 0.48 + min(hit_count, 10) * 0.035),
        "status": "active",
        "migration": {
            "source_migration_id": migration_id,
            "from_layer": "formal_knowledge.chats",
            "from_item_id": item_id,
        },
        "created_at": now(),
    }


def merge_records_by_id(existing: list[dict[str, Any]], additions: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing:
        if isinstance(item, dict):
            key = str(item.get(id_key) or "")
            if key:
                merged[key] = item
    for item in additions:
        key = str(item.get(id_key) or "")
        if key:
            merged[key] = item
    return list(merged.values())


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
