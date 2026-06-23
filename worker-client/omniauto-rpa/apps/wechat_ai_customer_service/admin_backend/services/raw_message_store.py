"""Shared raw WeChat message persistence for customer-service and recorder flows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import (
    message_learning_exclusion_reason,
)
from apps.wechat_ai_customer_service.wechat_message_normalizer import normalize_wechat_message_record
from apps.wechat_ai_customer_service.wechat_message_envelope import (
    OCR_RPA_ADAPTERS,
    apply_message_envelope_to_record,
    build_message_envelope,
)


MAX_FILE_RECORDS = 10000
MAX_BATCH_RECORDS = 2000
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
WINDOWS_FILE_RETRY_DELAYS = (0.03, 0.08, 0.16, 0.32, 0.64)
OCR_FUZZY_DEDUPE_MIN_LENGTH = 24
OCR_FUZZY_DEDUPE_RATIO = 0.965
OCR_PARTIAL_DEDUPE_MIN_LENGTH = 12
OCR_PARTIAL_DEDUPE_WINDOW_SECONDS = 30 * 60


class RawMessageStore:
    def __init__(self, *, tenant_id: str | None = None, root: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.root = root or (tenant_runtime_root(self.tenant_id) / "raw_messages")

    @property
    def conversations_path(self) -> Path:
        return self.root / "conversations.json"

    @property
    def messages_path(self) -> Path:
        return self.root / "messages.json"

    @property
    def batches_path(self) -> Path:
        return self.root / "batches.json"

    def upsert_conversation(self, record: dict[str, Any]) -> dict[str, Any]:
        conversation = normalize_conversation(record, tenant_id=self.tenant_id)
        db = postgres_store()
        config = load_storage_config()
        if db:
            db.upsert_raw_conversation(self.tenant_id, conversation)
            if not config.mirror_files:
                return conversation
        conversations = self._read_json(self.conversations_path, [])
        by_id = {str(item.get("conversation_id") or ""): item for item in conversations if isinstance(item, dict)}
        existing = by_id.get(conversation["conversation_id"], {})
        merged = {**existing, **conversation}
        by_id[conversation["conversation_id"]] = merged
        self._write_json(self.conversations_path, sorted(by_id.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True))
        return merged

    def list_conversations(self, *, conversation_type: str = "", status: str = "all", limit: int = 200) -> list[dict[str, Any]]:
        db = postgres_store()
        if db:
            items = db.list_raw_conversations(self.tenant_id, conversation_type=conversation_type, status=status)
            if items:
                return items[: max(1, min(int(limit or 200), 500))]
        records = [item for item in self._read_json(self.conversations_path, []) if isinstance(item, dict)]
        if conversation_type:
            records = [item for item in records if str(item.get("conversation_type") or "") == conversation_type]
        if status and status != "all":
            records = [item for item in records if str(item.get("status") or "active") == status]
        records.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 200), 500))]

    def upsert_messages(
        self,
        conversation: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        source_module: str,
        learning_enabled: bool = True,
        create_batch: bool = True,
        batch_reason: str = "message_observed",
    ) -> dict[str, Any]:
        normalized_conversation = self.upsert_conversation(conversation)
        existing = self._message_map()
        inserted: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        updated_records = dict(existing)
        db = postgres_store()
        config = load_storage_config()
        for raw in messages:
            message = normalize_message(
                raw,
                conversation=normalized_conversation,
                tenant_id=self.tenant_id,
                source_module=source_module,
                learning_enabled=learning_enabled,
            )
            db_existing = db.get_raw_message_by_dedupe(self.tenant_id, message["dedupe_key"]) if db else None
            previous = db_existing or updated_records.get(message["dedupe_key"])
            if not previous:
                previous = find_ocr_near_duplicate(updated_records.values(), message)
            if previous:
                message = merge_message(previous, message, source_module=source_module)
                duplicates.append(message)
            else:
                inserted.append(message)
            if db:
                db.upsert_raw_message(self.tenant_id, message)
            updated_records[message["dedupe_key"]] = message
        if not db or config.mirror_files:
            records = sorted(updated_records.values(), key=lambda item: str(item.get("observed_at") or ""), reverse=True)
            self._write_json(self.messages_path, records[:MAX_FILE_RECORDS])
        batch = None
        learnable_inserted = [item for item in inserted if item.get("learning_enabled") is True]
        if create_batch and learnable_inserted:
            batch = self.create_batch(
                conversation_id=normalized_conversation["conversation_id"],
                message_ids=[str(item.get("raw_message_id") or "") for item in learnable_inserted],
                reason=batch_reason,
                source_module=source_module,
            )
        return {
            "ok": True,
            "conversation": normalized_conversation,
            "inserted_count": len(inserted),
            "duplicate_count": len(duplicates),
            "message_ids": [item["raw_message_id"] for item in inserted],
            "duplicate_message_ids": [item["raw_message_id"] for item in duplicates],
            "batch": batch,
        }

    def list_messages(self, *, conversation_id: str = "", query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.list_messages_advanced(conversation_id=conversation_id, query=query, limit=limit)

    def list_messages_advanced(
        self,
        *,
        conversation_id: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        start_time: str = "",
        end_time: str = "",
        sender: str = "",
        content_type: str = "",
        conversation_type: str = "",
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        db = postgres_store()
        if db:
            return db.list_raw_messages(
                self.tenant_id,
                conversation_id=conversation_id,
                query=query,
                limit=limit,
                offset=offset,
                start_time=start_time,
                end_time=end_time,
                sender=sender,
                content_type=content_type,
                conversation_type=conversation_type,
                keywords=[item for item in (keywords or []) if str(item).strip()],
            )
        records = [item for item in self._read_json(self.messages_path, []) if isinstance(item, dict)]
        if conversation_id:
            records = [item for item in records if str(item.get("conversation_id") or "") == conversation_id]
        if conversation_type:
            records = [item for item in records if str(item.get("conversation_type") or "") == conversation_type]
        if sender:
            lowered_sender = sender.lower().strip()
            records = [item for item in records if lowered_sender in str(item.get("sender") or "").lower()]
        if content_type:
            records = [item for item in records if str(item.get("content_type") or "").lower() == content_type.lower().strip()]
        if query:
            lowered = query.lower()
            records = [item for item in records if lowered in str(item.get("content") or "").lower()]
        terms = [str(item).strip().lower() for item in (keywords or []) if str(item).strip()]
        if terms:
            records = [
                item
                for item in records
                if all(term in str(item.get("content") or "").lower() for term in terms)
            ]
        if start_time or end_time:
            parsed_start = parse_time_text(start_time)
            parsed_end = parse_time_text(end_time)
            if parsed_start or parsed_end:
                filtered: list[dict[str, Any]] = []
                for item in records:
                    parsed_message_time = parse_time_text(message_time_text(item))
                    if parsed_message_time is None:
                        continue
                    if parsed_start and parsed_message_time < parsed_start:
                        continue
                    if parsed_end and parsed_message_time > parsed_end:
                        continue
                    filtered.append(item)
                records = filtered
            else:
                # Backward-compatible fallback for unexpected formats.
                if start_time:
                    records = [item for item in records if message_time_text(item) >= start_time]
                if end_time:
                    records = [item for item in records if message_time_text(item) <= end_time]
        records.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)
        clean_offset = max(0, int(offset or 0))
        clean_limit = max(1, min(int(limit or 100), 10000))
        return records[clean_offset : clean_offset + clean_limit]

    def create_batch(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        reason: str,
        source_module: str,
    ) -> dict[str, Any]:
        clean_ids = [str(item) for item in message_ids if str(item)]
        created_at = now()
        batch = {
            "batch_id": "raw_batch_" + stable_digest(f"{self.tenant_id}:{conversation_id}:{reason}:{created_at}:{clean_ids}", 20),
            "tenant_id": self.tenant_id,
            "conversation_id": conversation_id,
            "message_ids": clean_ids,
            "reason": reason,
            "source_module": source_module,
            "status": "pending",
            "created_at": created_at,
        }
        db = postgres_store()
        config = load_storage_config()
        if db:
            db.upsert_raw_message_batch(self.tenant_id, batch)
            if not config.mirror_files:
                return batch
        records = [item for item in self._read_json(self.batches_path, []) if isinstance(item, dict)]
        records = [item for item in records if item.get("batch_id") != batch["batch_id"]]
        records.insert(0, batch)
        self._write_json(self.batches_path, records[:MAX_BATCH_RECORDS])
        return batch

    def list_batches(self, *, conversation_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        db = postgres_store()
        if db:
            return db.list_raw_message_batches(self.tenant_id, conversation_id=conversation_id, limit=limit)
        records = [item for item in self._read_json(self.batches_path, []) if isinstance(item, dict)]
        if conversation_id:
            records = [item for item in records if str(item.get("conversation_id") or "") == conversation_id]
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 100), 500))]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for batch in self.list_batches(limit=500):
            if str(batch.get("batch_id") or "") == batch_id:
                return batch
        return None

    def update_batch(self, batch_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_batch(batch_id)
        if not existing:
            raise FileNotFoundError(batch_id)
        updated = {**existing, **patch, "updated_at": now()}
        db = postgres_store()
        config = load_storage_config()
        if db:
            db.upsert_raw_message_batch(self.tenant_id, updated)
            if not config.mirror_files:
                return updated
        records = [item for item in self._read_json(self.batches_path, []) if isinstance(item, dict)]
        replaced = False
        for index, item in enumerate(records):
            if str(item.get("batch_id") or "") == batch_id:
                records[index] = updated
                replaced = True
                break
        if not replaced:
            records.insert(0, updated)
        self._write_json(self.batches_path, records[:MAX_BATCH_RECORDS])
        return updated

    def summary(self) -> dict[str, Any]:
        conversations = self.list_conversations(limit=500)
        messages = self.list_messages(limit=500)
        batches = self.list_batches(limit=500)
        return {
            "conversation_count": len(conversations),
            "message_count": len(messages),
            "batch_count": len(batches),
            "group_count": len([item for item in conversations if item.get("conversation_type") == "group"]),
            "private_count": len([item for item in conversations if item.get("conversation_type") == "private"]),
            "pending_batch_count": len([item for item in batches if item.get("status") == "pending"]),
        }

    def _message_map(self) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("dedupe_key") or ""): item
            for item in self._read_json(self.messages_path, [])
            if isinstance(item, dict) and item.get("dedupe_key")
        }

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        last_error: OSError | None = None
        for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
            try:
                os.replace(temp, path)
                return
            except OSError as exc:
                last_error = exc
                if index >= len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                    break
                time.sleep(delay)
        if last_error is not None:
            raise last_error


def normalize_conversation(record: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    target_name = str(record.get("target_name") or record.get("name") or record.get("display_name") or "").strip()
    conversation_type = normalize_conversation_type(str(record.get("conversation_type") or record.get("type") or "unknown"))
    seed = str(record.get("conversation_id") or f"{conversation_type}:{target_name or record}")
    conversation_id = safe_id(str(record.get("conversation_id") or "conv_" + stable_digest(f"{tenant_id}:{seed}", 18)))
    timestamp = now()
    return {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "conversation_type": conversation_type,
        "target_name": target_name,
        "display_name": str(record.get("display_name") or target_name or conversation_id),
        "group_name": str(record.get("group_name") or (target_name if conversation_type == "group" else "")),
        "status": str(record.get("status") or "active"),
        "exact": record.get("exact", True) is not False,
        "record_self": bool(record.get("record_self", False)),
        "learning_enabled": record.get("learning_enabled", True) is not False,
        "notify_enabled": bool(record.get("notify_enabled", False)),
        "selected_by_user": bool(record.get("selected_by_user", False)),
        "created_at": str(record.get("created_at") or timestamp),
        "updated_at": timestamp,
        "source": record.get("source") if isinstance(record.get("source"), dict) else {},
        "raw_payload": record.get("raw_payload") if "raw_payload" in record else record,
    }


def normalize_message(
    record: dict[str, Any],
    *,
    conversation: dict[str, Any],
    tenant_id: str,
    source_module: str,
    learning_enabled: bool,
) -> dict[str, Any]:
    original_record = record
    raw_source_adapter = str(record.get("source_adapter") or "wxauto4")
    has_ocr_payload = bool(record.get("ocr_items") or record.get("content_raw_ocr") or record.get("raw_ocr_text"))
    is_ocr_record = raw_source_adapter in OCR_RPA_ADAPTERS or has_ocr_payload
    timestamp = now()
    ocr_screen_time_text = ""
    if is_ocr_record:
        ocr_screen_time_text = str(record.get("screen_time_text") or record.get("time") or record.get("message_time") or "").strip()
    if raw_source_adapter in OCR_RPA_ADAPTERS or has_ocr_payload:
        record = normalize_wechat_message_record(
            record,
            conversation_type=str(conversation.get("conversation_type") or "unknown"),
            target_name=str(conversation.get("target_name") or ""),
        )
    else:
        record = dict(record)
    if is_ocr_record and ocr_screen_time_text and not str(record.get("screen_time_text") or "").strip():
        record["screen_time_text"] = ocr_screen_time_text
    envelope = build_message_envelope(
        record,
        source_adapter=str(record.get("source_adapter") or ""),
        conversation=conversation,
        captured_at=timestamp if is_ocr_record else None,
    )
    record = apply_message_envelope_to_record(record, envelope)
    content = str(record.get("content") or record.get("text") or "")
    message_id = str(record.get("id") or record.get("message_id") or "")
    sender = str(record.get("sender") or "")
    source_adapter = str(record.get("source_adapter") or "wxauto4")
    captured_at = str(record.get("captured_at") or envelope.get("captured_at") or "")
    if source_adapter in OCR_RPA_ADAPTERS:
        message_time = captured_at
    else:
        message_time = str(record.get("time") or record.get("message_time") or captured_at or "")
    content_type = str(record.get("type") or record.get("content_type") or "text")
    sender_role = normalize_sender_role(record, sender=sender)
    if conversation.get("conversation_type") == "group" and record.get("speaker_name") and sender_role == "unknown":
        sender_role = "group_member"
    content_fingerprint = normalized_content_fingerprint(content)
    explicit_dedupe_key = str(record.get("dedupe_key") or "").strip()
    bubble_id = str(record.get("bubble_id") or envelope.get("bubble_id") or "").strip()
    if explicit_dedupe_key:
        dedupe_seed = explicit_dedupe_key
    elif bubble_id:
        dedupe_seed = "|".join([conversation["conversation_id"], sender, content_type, "bubble", bubble_id])
    elif content_fingerprint:
        dedupe_seed = "|".join([conversation["conversation_id"], sender, content_type, content_fingerprint, message_time])
    else:
        dedupe_seed = message_id or "|".join([conversation["conversation_id"], sender, content_type, message_time])
    dedupe_key = stable_digest(f"{tenant_id}:{conversation['conversation_id']}:{dedupe_seed}", 32)
    raw_message_id = "raw_msg_" + dedupe_key[:20]
    observed_at = captured_at if source_adapter in OCR_RPA_ADAPTERS and captured_at else str(record.get("observed_at") or timestamp)
    requested_learning = bool(learning_enabled)
    exclusion_reason = message_learning_exclusion_reason(
        record,
        conversation=conversation,
        source_module=source_module,
    )
    final_learning_enabled = requested_learning and not exclusion_reason
    return {
        "tenant_id": tenant_id,
        "raw_message_id": raw_message_id,
        "conversation_id": conversation["conversation_id"],
        "conversation_type": conversation.get("conversation_type") or "unknown",
        "target_name": conversation.get("target_name") or "",
        "group_name": conversation.get("group_name") or "",
        "message_id": message_id,
        "bubble_id": bubble_id,
        "sender": sender,
        "sender_role": sender_role,
        "group_member_name": str(record.get("group_member_name") or record.get("speaker_name") or (sender if conversation.get("conversation_type") == "group" else "")),
        "speaker_name": str(record.get("speaker_name") or ""),
        "original_content": str(record.get("original_content") or ""),
        "ocr_speaker_prefix": record.get("ocr_speaker_prefix") if isinstance(record.get("ocr_speaker_prefix"), dict) else {},
        "content_type": content_type,
        "content": content,
        "content_body": content,
        "content_clean": content,
        "content_raw_ocr": str(record.get("content_raw_ocr") or ""),
        "quoted_fragments": record.get("quoted_fragments") if isinstance(record.get("quoted_fragments"), list) else [],
        "excluded_fragments": record.get("excluded_fragments") if isinstance(record.get("excluded_fragments"), list) else [],
        "quality_flags": record.get("quality_flags") if isinstance(record.get("quality_flags"), list) else [],
        "captured_at": captured_at,
        "screen_time_text": str(record.get("screen_time_text") or ""),
        "bubble_rect": record.get("bubble_rect") if isinstance(record.get("bubble_rect"), dict) else {},
        "ocr_items": record.get("ocr_items") if isinstance(record.get("ocr_items"), list) else [],
        "message_time": message_time,
        "message_fingerprint": content_fingerprint,
        "observed_at": observed_at,
        "updated_at": timestamp,
        "source_modules": [source_module],
        "source_adapter": source_adapter,
        "learning_enabled": final_learning_enabled,
        "excluded_reason": str(record.get("excluded_reason") or exclusion_reason or ""),
        "dedupe_key": dedupe_key,
        "raw_payload": {
            **record,
            "message_envelope": envelope,
            "_original_raw_payload": original_record,
        },
    }


def merge_message(existing: dict[str, Any], incoming: dict[str, Any], *, source_module: str) -> dict[str, Any]:
    merged = dict(existing)
    modules = [str(item) for item in merged.get("source_modules", []) if str(item)]
    if source_module not in modules:
        modules.append(source_module)
    merged["source_modules"] = modules
    merged["updated_at"] = now()
    merged["learning_enabled"] = bool(merged.get("learning_enabled", True)) and bool(incoming.get("learning_enabled", True))
    existing_key = fuzzy_ocr_content_key(str(merged.get("content") or ""))
    incoming_key = fuzzy_ocr_content_key(str(incoming.get("content") or ""))
    exact_ocr_content_repeat = bool(existing_key and incoming_key and existing_key == incoming_key)
    incoming_more_complete = bool(
        existing_key
        and incoming_key
        and len(incoming_key) > len(existing_key) + 4
        and existing_key in incoming_key
    )
    if (
        incoming_more_complete
    ):
        merged["content"] = str(incoming.get("content") or merged.get("content") or "")
        merged["message_fingerprint"] = str(incoming.get("message_fingerprint") or merged.get("message_fingerprint") or "")
        merged["raw_payload"] = incoming.get("raw_payload") if isinstance(incoming.get("raw_payload"), dict) else merged.get("raw_payload", {})
        if incoming.get("message_id"):
            merged["message_id"] = str(incoming.get("message_id") or "")
    if incoming.get("excluded_reason") and not merged["learning_enabled"]:
        merged["excluded_reason"] = str(incoming.get("excluded_reason") or merged.get("excluded_reason") or "")
    for key in (
        "content_body",
        "content_clean",
        "content_raw_ocr",
        "captured_at",
        "message_time",
        "observed_at",
        "screen_time_text",
        "bubble_id",
    ):
        if incoming.get(key):
            if key in {"captured_at", "message_time", "observed_at", "screen_time_text", "bubble_id"} and merged.get(key) and not incoming_more_complete:
                continue
            merged[key] = incoming.get(key)
    for key in ("quoted_fragments", "excluded_fragments", "quality_flags", "ocr_items"):
        if incoming.get(key):
            merged[key] = incoming.get(key)
    if incoming.get("bubble_rect"):
        merged["bubble_rect"] = incoming.get("bubble_rect")
    return merged


def find_ocr_near_duplicate(existing_records: Any, incoming: dict[str, Any]) -> dict[str, Any] | None:
    """Merge highly similar OCR captures that differ by tiny recognition noise."""
    incoming_modules = {str(item) for item in incoming.get("source_modules", []) if str(item)}
    if "smart_recorder" not in incoming_modules and str(incoming.get("source_adapter") or "") != "win32_ocr":
        return None
    incoming_key = fuzzy_ocr_content_key(str(incoming.get("content") or ""))
    if len(incoming_key) < OCR_PARTIAL_DEDUPE_MIN_LENGTH:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for candidate in existing_records:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("conversation_id") or "") != str(incoming.get("conversation_id") or ""):
            continue
        if str(candidate.get("sender") or "") != str(incoming.get("sender") or ""):
            continue
        if str(candidate.get("content_type") or "") != str(incoming.get("content_type") or ""):
            continue
        candidate_modules = {str(item) for item in candidate.get("source_modules", []) if str(item)}
        if "smart_recorder" not in candidate_modules and str(candidate.get("source_adapter") or "") != "win32_ocr":
            continue
        candidate_key = fuzzy_ocr_content_key(str(candidate.get("content") or ""))
        if len(candidate_key) < OCR_PARTIAL_DEDUPE_MIN_LENGTH:
            continue
        if incoming_key == candidate_key and (
            ocr_message_identity_same(incoming, candidate)
            or ocr_source_times_same(incoming, candidate)
            or ocr_observed_times_very_close(incoming, candidate)
        ):
            if best is None or 2.0 > best[0]:
                best = (2.0, candidate)
            continue
        partial_score = ocr_partial_duplicate_score(incoming_key, candidate_key)
        if partial_score and ocr_observed_times_close(incoming, candidate):
            if best is None or partial_score > best[0]:
                best = (partial_score, candidate)
            continue
        if len(candidate_key) < OCR_FUZZY_DEDUPE_MIN_LENGTH:
            continue
        max_length = max(len(incoming_key), len(candidate_key))
        if abs(len(incoming_key) - len(candidate_key)) > max(4, int(max_length * 0.08)):
            continue
        ratio = SequenceMatcher(None, incoming_key, candidate_key).ratio()
        if ratio >= OCR_FUZZY_DEDUPE_RATIO and (best is None or ratio > best[0]):
            best = (ratio, candidate)
    return best[1] if best else None


def fuzzy_ocr_content_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def ocr_partial_duplicate_score(left: str, right: str) -> float:
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < OCR_PARTIAL_DEDUPE_MIN_LENGTH or not longer or shorter not in longer:
        return 0.0
    if len(shorter) == len(longer):
        return 0.0
    unique_signal = re.search(r"[A-Za-z][A-Za-z0-9_.-]{5,}", shorter) or re.search(
        r"\d+(?:\.\d+)?(?:元|ML|G|MG|UL|μL)|\d+(?:\.\d+)?\s*[xX*×]\s*\d+",
        shorter,
        re.I,
    )
    if not unique_signal and len(shorter) < OCR_FUZZY_DEDUPE_MIN_LENGTH:
        return 0.0
    return 1.0 + (len(shorter) / max(1, len(longer)))


def ocr_observed_times_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_time = parse_time_text(str(left.get("observed_at") or left.get("message_time") or ""))
    right_time = parse_time_text(str(right.get("observed_at") or right.get("message_time") or ""))
    if left_time is None or right_time is None:
        return True
    return abs((left_time - right_time).total_seconds()) <= OCR_PARTIAL_DEDUPE_WINDOW_SECONDS


def ocr_observed_times_very_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_time = parse_time_text(str(left.get("observed_at") or left.get("message_time") or ""))
    right_time = parse_time_text(str(right.get("observed_at") or right.get("message_time") or ""))
    if left_time is None or right_time is None:
        return False
    return abs((left_time - right_time).total_seconds()) <= 45


def ocr_message_identity_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in ("message_id", "bubble_id"):
        left_value = str(left.get(key) or "").strip()
        right_value = str(right.get(key) or "").strip()
        if left_value and right_value and left_value == right_value:
            return True
    return False


def ocr_source_times_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_text = ocr_source_time_text(left)
    right_text = ocr_source_time_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_time = parse_time_text(left_text)
    right_time = parse_time_text(right_text)
    if left_time is None or right_time is None:
        return False
    return abs((left_time - right_time).total_seconds()) <= 1


def ocr_source_time_text(record: dict[str, Any]) -> str:
    direct = str(record.get("screen_time_text") or "").strip()
    if direct:
        return direct
    raw_payload = record.get("raw_payload") if isinstance(record.get("raw_payload"), dict) else {}
    original = raw_payload.get("_original_raw_payload") if isinstance(raw_payload.get("_original_raw_payload"), dict) else {}
    for key in ("screen_time_text", "time", "message_time", "captured_at"):
        value = str(original.get(key) or "").strip()
        if value:
            return value
    return str(record.get("message_time") or "").strip()


def normalize_conversation_type(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"private", "group", "file_transfer", "system", "unknown"}:
        return text
    if text in {"chatroom", "room"}:
        return "group"
    return "unknown"


def normalize_sender_role(record: dict[str, Any], *, sender: str) -> str:
    role = str(record.get("sender_role") or "").strip().lower()
    if role in {"self", "contact", "group_member", "bot", "system", "unknown"}:
        return role
    if sender == "self" or record.get("is_self"):
        return "self"
    return "unknown"


def safe_id(value: str) -> str:
    text = SAFE_ID_RE.sub("_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text or not re.match(r"^[A-Za-z0-9]", text):
        text = "id_" + stable_digest(value, 12)
    return text[:120]


def stable_digest(value: Any, length: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def normalized_content_fingerprint(value: str) -> str:
    text = re.sub(r"\s+", "\n", str(value or "").strip())
    return stable_digest(text, 32) if text else ""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def message_time_text(message: dict[str, Any]) -> str:
    source_adapter = str(message.get("source_adapter") or "").strip().lower()
    if source_adapter in OCR_RPA_ADAPTERS:
        return str(message.get("captured_at") or message.get("observed_at") or message.get("message_time") or "")
    return str(message.get("message_time") or message.get("time") or message.get("observed_at") or message.get("captured_at") or "")


def parse_time_text(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    candidates = [normalized]
    if len(normalized) == 10:
        candidates.extend([normalized + " 00:00:00", normalized + "T00:00:00"])
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def postgres_store():
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(config=config)
    return store if store.available() else None
