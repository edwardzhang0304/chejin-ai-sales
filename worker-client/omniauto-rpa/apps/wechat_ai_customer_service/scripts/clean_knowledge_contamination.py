"""Quarantine learnable data that came from tests, self replies, or raw product facts.

This script is intentionally conservative: it does not erase audit logs or raw
messages. It disables their learning/retrieval flags, quarantines RAG chunks
that should not be directly searchable, and rebuilds the RAG index.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import (  # noqa: E402
    BLOCKED_RAG_CATEGORIES,
    BLOCKED_RAG_SOURCE_TYPES,
    message_learning_exclusion_reason,
    rag_chunk_exclusion_reason,
    text_has_model_reply_marker,
    text_has_test_marker,
    transcript_learning_exclusion_reason,
)
from apps.wechat_ai_customer_service.knowledge_paths import tenant_root, tenant_runtime_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="chejin")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = run_cleanup(args.tenant, apply=args.apply)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


def run_cleanup(tenant_id: str, *, apply: bool) -> dict[str, Any]:
    started = datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact_root = (
        PROJECT_ROOT
        / "runtime"
        / "apps"
        / "wechat_ai_customer_service"
        / "test_artifacts"
        / "knowledge_contamination_cleanup"
        / f"{tenant_id}_{started}"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": True,
        "tenant_id": tenant_id,
        "apply": apply,
        "artifact_root": str(artifact_root),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "actions": {},
    }

    tenant_data = tenant_root(tenant_id)
    runtime_root = tenant_runtime_root(tenant_id)
    backup_dir = artifact_root / "backups"

    report["actions"]["rag"] = quarantine_rag_sources_and_chunks(tenant_data, backup_dir=backup_dir, apply=apply)
    report["actions"]["raw_messages"] = sanitize_raw_messages(runtime_root, backup_dir=backup_dir, apply=apply)
    report["actions"]["customer_service_settings"] = disable_customer_service_auto_learning(
        runtime_root,
        backup_dir=backup_dir,
        apply=apply,
    )
    report["actions"]["customer_service_state"] = sanitize_customer_service_state(
        runtime_root,
        backup_dir=backup_dir,
        apply=apply,
    )
    report["actions"]["rag_experiences"] = quarantine_rag_experience_records(
        tenant_data,
        backup_dir=backup_dir,
        apply=apply,
    )
    report["actions"]["formal_knowledge_seed_metadata"] = sanitize_formal_seed_metadata(
        tenant_data,
        backup_dir=backup_dir,
        apply=apply,
    )
    if apply:
        report["actions"]["rag_index"] = RagService(tenant_id=tenant_id).rebuild_index()
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(artifact_root / "report.json", report)
    return report


def quarantine_rag_sources_and_chunks(tenant_data: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    rag_sources_path = tenant_data / "rag_sources" / "sources.json"
    rag_chunks_root = tenant_data / "rag_chunks"
    result = {
        "source_count": 0,
        "blocked_source_count": 0,
        "chunk_file_count": 0,
        "quarantined_chunk_count": 0,
        "blocked_source_ids": [],
    }
    sources = read_json(rag_sources_path, default=[])
    if isinstance(sources, list):
        result["source_count"] = len(sources)
    blocked_source_ids: set[str] = set()
    changed_sources = False
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        reason = source_exclusion_reason(source)
        if not reason:
            continue
        source_id = str(source.get("source_id") or "")
        if source_id:
            blocked_source_ids.add(source_id)
        if str(source.get("status") or "active") != "quarantined":
            source["status"] = "quarantined"
            source["quarantine_reason"] = reason
            source["quarantined_at"] = datetime.now().isoformat(timespec="seconds")
            changed_sources = True
    result["blocked_source_count"] = len(blocked_source_ids)
    result["blocked_source_ids"] = sorted(blocked_source_ids)[:200]
    if apply and changed_sources:
        backup_file(rag_sources_path, backup_dir)
        write_json(rag_sources_path, sources)

    changed_chunk_paths: list[Path] = []
    if rag_chunks_root.exists():
        for path in sorted(rag_chunks_root.glob("source_*.json")):
            payload = read_json(path, default={})
            if not isinstance(payload, dict):
                continue
            result["chunk_file_count"] += 1
            source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
            source_id = str(source.get("source_id") or "")
            source_reason = source_exclusion_reason(source) if source else ""
            file_changed = False
            for chunk in payload.get("chunks", []) or []:
                if not isinstance(chunk, dict):
                    continue
                reason = source_reason or rag_chunk_exclusion_reason(chunk)
                if not reason and source_id in blocked_source_ids:
                    reason = "source_quarantined"
                if not reason:
                    continue
                if str(chunk.get("status") or "active") != "quarantined":
                    chunk["status"] = "quarantined"
                    chunk["quarantine_reason"] = reason
                    chunk["quarantined_at"] = datetime.now().isoformat(timespec="seconds")
                    result["quarantined_chunk_count"] += 1
                    file_changed = True
            if file_changed:
                if isinstance(payload.get("source"), dict):
                    payload["source"]["status"] = "quarantined"
                    payload["source"].setdefault("quarantine_reason", source_reason or "chunk_quarantined")
                changed_chunk_paths.append(path)
                if apply:
                    backup_file(path, backup_dir)
                    write_json(path, payload)
    result["changed_chunk_files"] = len(changed_chunk_paths)
    return result


def sanitize_raw_messages(runtime_root: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    raw_root = runtime_root / "raw_messages"
    messages_path = raw_root / "messages.json"
    batches_path = raw_root / "batches.json"
    messages = read_json(messages_path, default=[])
    batches = read_json(batches_path, default=[])
    result = {
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "disabled_learning_count": 0,
        "batch_count": len(batches) if isinstance(batches, list) else 0,
        "skipped_batch_count": 0,
    }
    message_by_id: dict[str, dict[str, Any]] = {}
    changed_messages = False
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        message_by_id[str(message.get("raw_message_id") or "")] = message
        reason = message_learning_exclusion_reason(
            message,
            conversation=message,
            source_module="customer_service" if "customer_service" in (message.get("source_modules") or []) else "",
        )
        if reason and message.get("learning_enabled") is not False:
            message["learning_enabled"] = False
            message["excluded_reason"] = reason
            message["learning_disabled_at"] = datetime.now().isoformat(timespec="seconds")
            result["disabled_learning_count"] += 1
            changed_messages = True
        elif reason and not message.get("excluded_reason"):
            message["excluded_reason"] = reason
            changed_messages = True
    if apply and changed_messages:
        backup_file(messages_path, backup_dir)
        write_json(messages_path, messages)

    changed_batches = False
    for batch in batches if isinstance(batches, list) else []:
        if not isinstance(batch, dict):
            continue
        batch_messages = [message_by_id.get(str(raw_id)) for raw_id in batch.get("message_ids", []) or []]
        batch_messages = [item for item in batch_messages if isinstance(item, dict)]
        reason = batch_exclusion_reason(batch, batch_messages)
        if reason and str(batch.get("status") or "") not in {"skipped", "archived"}:
            batch["previous_status_before_quarantine"] = str(batch.get("status") or "")
            batch["status"] = "skipped"
            batch["skipped_reason"] = reason
            batch["quarantined_at"] = datetime.now().isoformat(timespec="seconds")
            result["skipped_batch_count"] += 1
            changed_batches = True
    if apply and changed_batches:
        backup_file(batches_path, backup_dir)
        write_json(batches_path, batches)
    return result


def disable_customer_service_auto_learning(runtime_root: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    customer_service_root = runtime_root / "customer_service"
    changed = []
    for path in (customer_service_root / "listener_config.json", customer_service_root / "settings.json"):
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            continue
        before = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if path.name == "listener_config.json":
            raw = payload.setdefault("raw_messages", {})
            if isinstance(raw, dict):
                raw["learning_enabled"] = False
                raw["auto_learn"] = False
                raw["allow_customer_service_learning"] = False
        else:
            payload["auto_learn"] = False
        after = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if after != before:
            changed.append(str(path))
            if apply:
                backup_file(path, backup_dir)
                write_json(path, payload)
    return {"changed_files": changed, "changed_count": len(changed)}


def sanitize_customer_service_state(runtime_root: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    """Remove stale/test reply context without touching raw audit evidence.

    The listener keeps `sent_replies` and handoff events as runtime context. Old
    live-test replies should not remain available as context even if retrieval
    guards are working, so we trim only context-bearing fields and keep processed
    message IDs intact to avoid re-answering historical WeChat rows.
    """

    state_root = runtime_root / "state"
    result = {
        "state_file_count": 0,
        "changed_file_count": 0,
        "removed_context_item_count": 0,
        "cleared_cache_count": 0,
    }
    if not state_root.exists():
        return result
    now = datetime.now()
    context_lists = {
        "sent_replies",
        "operator_alerts",
        "handoff_events",
        "pending_customer_data",
        "bootstrap_events",
    }
    for path in sorted(state_root.glob("*.json")):
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            continue
        result["state_file_count"] += 1
        before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        targets = payload.get("targets")
        if not isinstance(targets, dict):
            targets = {"": payload}
        for state in targets.values():
            if not isinstance(state, dict):
                continue
            for key in context_lists:
                values = state.get(key)
                if not isinstance(values, list):
                    continue
                kept = []
                for item in values:
                    reason = state_context_exclusion_reason(item, now=now)
                    if reason:
                        result["removed_context_item_count"] += 1
                    else:
                        kept.append(item)
                state[key] = kept
            timestamps = state.get("reply_timestamps")
            if isinstance(timestamps, list):
                kept_timestamps = []
                for item in timestamps:
                    if timestamp_is_stale(str(item or ""), now=now, max_age=timedelta(hours=6)):
                        result["removed_context_item_count"] += 1
                    else:
                        kept_timestamps.append(item)
                state["reply_timestamps"] = kept_timestamps
            cache = state.get("_intent_router_cache")
            if isinstance(cache, dict) and state_context_exclusion_reason(cache, now=now):
                state["_intent_router_cache"] = {}
                result["cleared_cache_count"] += 1
        after = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if after != before:
            result["changed_file_count"] += 1
            if apply:
                backup_file(path, backup_dir)
                write_json(path, payload)
    return result


def quarantine_rag_experience_records(tenant_data: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    path = tenant_data / "rag_experience" / "experiences.json"
    records = read_json(path, default=[])
    result = {"record_count": len(records) if isinstance(records, list) else 0, "discarded_count": 0}
    changed = False
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        reason = experience_exclusion_reason(record)
        if not reason:
            continue
        if str(record.get("status") or "active") != "discarded":
            record["status"] = "discarded"
            record["discard_reason"] = reason
            record["discarded_at"] = datetime.now().isoformat(timespec="seconds")
            result["discarded_count"] += 1
            changed = True
        if isinstance(record.get("rag_ingest"), dict):
            record["rag_ingest"]["deactivated_by_contamination_guard"] = True
            record["rag_ingest"]["deactivation_reason"] = reason
            changed = True
    if apply and changed:
        backup_file(path, backup_dir)
        write_json(path, records)
    return result


def sanitize_formal_seed_metadata(tenant_data: Path, *, backup_dir: Path, apply: bool) -> dict[str, Any]:
    root = tenant_data / "knowledge_bases"
    result = {
        "item_count": 0,
        "normalized_source_count": 0,
        "rewritten_text_count": 0,
        "changed_file_count": 0,
        "remaining_test_marker_count": 0,
    }
    for path in root.glob("*/items/*.json"):
        payload = read_json(path, default={})
        if not isinstance(payload, dict):
            continue
        result["item_count"] += 1
        before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        if str(source.get("type") or "") == "test_fixture":
            source["type"] = "manual_seed"
            source.pop("batch_token", None)
            source["normalized_by"] = "knowledge_contamination_guard"
            source["normalized_at"] = datetime.now().isoformat(timespec="seconds")
            result["normalized_source_count"] += 1
        review_source = (
            payload.get("review_state", {}).get("source")
            if isinstance(payload.get("review_state"), dict)
            else None
        )
        if isinstance(review_source, dict) and "test" in str(review_source.get("source_module") or "").lower():
            review_source["source_module"] = "manual_seed"
        rewritten = rewrite_formal_seed_text(payload)
        result["rewritten_text_count"] += rewritten
        after = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if after != before:
            result["changed_file_count"] += 1
            if apply:
                backup_file(path, backup_dir)
                write_json(path, payload)
        if text_has_test_marker(json.dumps(payload, ensure_ascii=False)):
            result["remaining_test_marker_count"] += 1
    return result


def rewrite_formal_seed_text(value: Any) -> int:
    replacements = {
        "抖音直播或新加微信线索可以由 AI 先问": "抖音直播或新加微信线索可以先问",
        "客户明确要求试驾、到店、订金或事故赔付承诺时，AI只能记录并转人工确认。": "客户明确要求试驾、到店、订金或事故赔付承诺时，客服只能先记录需求，并请负责人核实确认。",
        "客户咨询置换时，AI 可以先收集": "客户咨询置换时，客服可以先收集",
        "AI 可以": "客服可以",
        "AI可以": "客服可以",
        "AI 只能": "客服只能",
        "AI只能": "客服只能",
        "转人工确认": "请负责人核实确认",
    }
    changed = 0
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str):
                new_item = item
                for old, new in replacements.items():
                    new_item = new_item.replace(old, new)
                if new_item != item:
                    value[key] = new_item
                    changed += 1
            elif isinstance(item, (dict, list)):
                changed += rewrite_formal_seed_text(item)
    elif isinstance(value, list):
        for index, item in enumerate(list(value)):
            if isinstance(item, str):
                new_item = item
                for old, new in replacements.items():
                    new_item = new_item.replace(old, new)
                if new_item != item:
                    value[index] = new_item
                    changed += 1
            elif isinstance(item, (dict, list)):
                changed += rewrite_formal_seed_text(item)
    return changed


def source_exclusion_reason(source: dict[str, Any]) -> str:
    source_type = str(source.get("source_type") or "")
    category = str(source.get("category") or "")
    source_path = str(source.get("source_path") or "")
    raw = json.dumps(source, ensure_ascii=False)
    if source_type in BLOCKED_RAG_SOURCE_TYPES:
        return "raw_wechat_source_not_directly_retrievable"
    if category in BLOCKED_RAG_CATEGORIES:
        return "product_master_not_rag_retrievable"
    if "raw_messages" in source_path.replace("\\", "/"):
        return "raw_message_path_not_directly_retrievable"
    if category == "chats" and "raw_inbox/chats" in source_path.replace("\\", "/"):
        return "chat_upload_requires_reviewed_experience"
    if text_has_test_marker(raw) or text_has_model_reply_marker(raw):
        return "source_metadata_contains_test_or_model_marker"
    return ""


def batch_exclusion_reason(batch: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    reasons = [
        message_learning_exclusion_reason(
            item,
            conversation=item,
            source_module=str(batch.get("source_module") or ""),
        )
        for item in messages
    ]
    if reasons and all(reasons):
        return "all_messages_unlearnable:" + ",".join(sorted(set(reasons))[:4])
    transcript_reason = transcript_learning_exclusion_reason("\n".join(str(item.get("content") or "") for item in messages))
    return transcript_reason


def experience_exclusion_reason(record: dict[str, Any]) -> str:
    source_type = str(record.get("source_type") or "")
    category = str(record.get("category") or "")
    raw = json.dumps(record, ensure_ascii=False)
    if source_type.startswith("raw_wechat_"):
        return "raw_wechat_experience_requires_manual_keep_before_retrieval"
    if category in BLOCKED_RAG_CATEGORIES:
        return "product_master_not_rag_experience"
    if text_has_test_marker(raw) or text_has_model_reply_marker(raw):
        return "experience_contains_test_or_model_marker"
    return ""


def state_context_exclusion_reason(item: Any, *, now: datetime) -> str:
    raw = json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item
    if text_has_test_marker(raw):
        return "runtime_context_contains_test_marker"
    if text_has_model_reply_marker(raw):
        return "runtime_context_contains_model_reply"
    for key in ("processed_at", "created_at", "updated_at", "completed_at"):
        if isinstance(item, dict) and timestamp_is_stale(str(item.get(key) or ""), now=now, max_age=timedelta(hours=6)):
            return "runtime_context_stale"
    return ""


def timestamp_is_stale(value: str, *, now: datetime, max_age: timedelta) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return now - parsed > max_age


def read_json(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def backup_file(path: Path, backup_dir: Path) -> None:
    if not path.exists():
        return
    relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


if __name__ == "__main__":
    raise SystemExit(main())
