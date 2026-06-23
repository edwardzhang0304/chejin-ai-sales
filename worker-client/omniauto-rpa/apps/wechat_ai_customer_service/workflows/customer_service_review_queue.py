"""Manual review queue for the guarded WeChat customer-service workflow.

This utility is intentionally read-only against WeChat. It reads the workflow
state and audit log, then lists items that should be checked by a human before
expanding the bot beyond test conversations.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from listen_and_reply import CONFIG_PATH, load_config, load_state, resolve_path


DEFAULT_LIMIT = 50
REVIEW_AUDIT_ACTIONS = {"blocked", "handoff", "error"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--json", action="store_true", help="Print the queue as JSON.")
    parser.add_argument("--export-json", type=Path, help="Write the queue to a JSON file.")
    parser.add_argument("--export-excel", type=Path, help="Write the queue to an Excel workbook.")
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="Include completed pending customer-data records.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    try:
        queue = build_review_queue(
            config_path=args.config,
            include_resolved=bool(args.include_resolved),
            limit=max(1, args.limit),
        )
        if args.export_json:
            write_json(args.export_json, queue)
            queue["export_json"] = str(args.export_json.resolve())
        if args.export_excel:
            write_excel(args.export_excel, queue)
            queue["export_excel"] = str(args.export_excel.resolve())
    except Exception as exc:
        print_json({"ok": False, "error": repr(exc)})
        return 1

    if args.json:
        print_json(queue)
    else:
        print_human(queue)
    return 0


def build_review_queue(config_path: Path, include_resolved: bool, limit: int) -> dict[str, Any]:
    config = load_config(config_path)
    state_path = resolve_path(config.get("state_path"))
    audit_path = resolve_path(config.get("audit_log_path"))
    state = load_state(state_path)

    items = []
    targets = state.get("targets", {}) or {}
    unresolved_handoff_ids: set[str] = set()
    for target_name, target_state in targets.items():
        unresolved_handoff_ids.update(str(value) for value in target_state.get("handoff_message_ids", []) or [])
        for handoff in target_state.get("handoff_events", []) or []:
            if handoff.get("status") == "resolved":
                continue
            unresolved_handoff_ids.update(str(value) for value in handoff.get("message_ids", []) or [])
        items.extend(customer_data_items(target_name, target_state, include_resolved))
        items.extend(handoff_items(target_name, target_state))

    items.extend(audit_attention_items(audit_path, unresolved_handoff_ids=unresolved_handoff_ids))
    items.sort(key=lambda item: parse_sort_time(item.get("created_at")), reverse=True)
    limited_items = items[:limit]

    counts = {
        "pending_customer_data": sum(1 for item in items if item["kind"] == "pending_customer_data"),
        "open_pending_customer_data": sum(
            1
            for item in items
            if item["kind"] == "pending_customer_data" and item.get("status") == "waiting_for_fields"
        ),
        "handoff": sum(1 for item in items if item["kind"] == "handoff"),
        "audit_attention": sum(1 for item in items if item["kind"] == "audit_attention"),
        "total": len(items),
        "shown": len(limited_items),
    }
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config_path": str(config_path.resolve()),
        "state_path": str(state_path),
        "audit_log_path": str(audit_path),
        "include_resolved": include_resolved,
        "limit": limit,
        "counts": counts,
        "items": limited_items,
    }


def customer_data_items(
    target_name: str,
    target_state: dict[str, Any],
    include_resolved: bool,
) -> list[dict[str, Any]]:
    result = []
    for item in target_state.get("pending_customer_data", []) or []:
        status = str(item.get("status") or "unknown")
        if status != "waiting_for_fields" and not include_resolved:
            continue
        is_completed = status == "completed"
        write_result = item.get("write_result", {}) or {}
        fields = write_result.get("fields") or item.get("fields", {})
        missing_labels = [] if is_completed else [str(value) for value in item.get("missing_required_labels", []) or []]
        message_ids = item.get("completed_message_ids") if is_completed else item.get("message_ids")
        result.append(
            {
                "kind": "pending_customer_data",
                "status": status,
                "priority": 1 if not is_completed else 3,
                "target": target_name,
                "created_at": item.get("completed_at") if is_completed else item.get("updated_at") or item.get("created_at"),
                "title": f"客户资料待补充：{'、'.join(missing_labels) or '未知字段'}"
                if not is_completed
                else "客户资料已完成",
                "missing_required_fields": [] if is_completed else item.get("missing_required_fields", []),
                "missing_required_labels": missing_labels,
                "message_ids": message_ids or [],
                "fields": fields,
                "raw_text": item.get("raw_text", ""),
                "source": "state.pending_customer_data",
            }
        )
    return result


def handoff_items(target_name: str, target_state: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in target_state.get("handoff_events", []) or []:
        if item.get("status") == "resolved":
            continue
        result.append(
            {
                "kind": "handoff",
                "status": "open",
                "priority": 1,
                "target": target_name,
                "created_at": item.get("created_at"),
                "title": "需要人工接管",
                "reason": item.get("reason"),
                "message_ids": item.get("message_ids", []),
                "message_contents": item.get("message_contents", []),
                "operator_alert": item.get("operator_alert"),
                "raw_text": "\n".join(str(value) for value in item.get("message_contents", []) or []),
                "source": "state.handoff_events",
            }
        )
    return result


def audit_attention_items(audit_path: Path, unresolved_handoff_ids: set[str] | None = None) -> list[dict[str, Any]]:
    if not audit_path.exists():
        return []

    events = []
    sent_message_ids: set[str] = set()
    processed_message_ids: set[str] = set()
    terminal_actions = {"sent", "captured", "outbound_sent", "bootstrapped"}
    with audit_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            event["_line_number"] = line_number
            events.append(event)
            action = str(event.get("action") or "")
            if action in terminal_actions:
                message_ids = [str(value) for value in event.get("message_ids", []) or []]
                if action == "sent":
                    sent_message_ids.update(message_ids)
                processed_message_ids.update(message_ids)

    result = []
    for event in events:
        action = str(event.get("action") or "")
        if action not in REVIEW_AUDIT_ACTIONS and event.get("ok", True):
            continue
        message_ids = [str(value) for value in event.get("message_ids", []) or []]
        if action == "blocked" and message_ids and all(message_id in sent_message_ids for message_id in message_ids):
            continue
        if action == "blocked" and message_ids and all(message_id in processed_message_ids for message_id in message_ids):
            continue
        if action == "handoff" and message_ids and unresolved_handoff_ids is not None:
            if all(message_id not in unresolved_handoff_ids for message_id in message_ids):
                continue
        result.append(
            {
                "kind": "audit_attention",
                "status": "open",
                "priority": 2 if event.get("ok", True) else 1,
                "target": event.get("target"),
                "created_at": event.get("created_at"),
                "title": f"审计事件：{action or 'unknown'}",
                "reason": event.get("reason") or event.get("error"),
                "message_ids": event.get("message_ids", []),
                "raw_text": event.get("combined_content", ""),
                "action": action,
                "source": f"audit.jsonl:{event.get('_line_number')}",
            }
        )
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_excel(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "人工复核队列"
    headers = [
        "created_at",
        "target",
        "kind",
        "status",
        "priority",
        "title",
        "reason",
        "missing_required_labels",
        "message_ids",
        "fields_json",
        "raw_text",
        "source",
    ]
    sheet.append(headers)
    for item in payload.get("items", []) or []:
        sheet.append(
            [
                item.get("created_at", ""),
                item.get("target", ""),
                item.get("kind", ""),
                item.get("status", ""),
                item.get("priority", ""),
                item.get("title", ""),
                item.get("reason", ""),
                "、".join(str(value) for value in item.get("missing_required_labels", []) or []),
                ",".join(str(value) for value in item.get("message_ids", []) or []),
                json.dumps(item.get("fields", {}), ensure_ascii=False),
                item.get("raw_text", ""),
                item.get("source", ""),
            ]
        )
    workbook.save(path)


def print_human(payload: dict[str, Any]) -> None:
    counts = payload.get("counts", {})
    lines = [
        "WeChat customer-service review queue",
        f"state: {payload.get('state_path')}",
        f"audit: {payload.get('audit_log_path')}",
        (
            "counts: "
            f"open_pending={counts.get('open_pending_customer_data', 0)}, "
            f"handoff={counts.get('handoff', 0)}, "
            f"audit_attention={counts.get('audit_attention', 0)}, "
            f"shown={counts.get('shown', 0)}/{counts.get('total', 0)}"
        ),
        "",
    ]
    items = payload.get("items", []) or []
    if not items:
        lines.append("No review items.")
    for index, item in enumerate(items, start=1):
        lines.append(
            f"{index}. [{item.get('kind')}] {item.get('target')} "
            f"{item.get('status')} p{item.get('priority')} {item.get('created_at')}"
        )
        lines.append(f"   {item.get('title')}")
        reason = item.get("reason")
        if reason:
            lines.append(f"   reason: {reason}")
        missing = item.get("missing_required_labels") or []
        if missing:
            lines.append(f"   missing: {'、'.join(str(value) for value in missing)}")
        fields = item.get("fields") or {}
        if fields:
            lines.append(
                "   fields: "
                + ", ".join(f"{key}={value}" for key, value in fields.items())
            )
        message_ids = item.get("message_ids") or []
        if message_ids:
            lines.append(f"   message_ids: {', '.join(str(value) for value in message_ids)}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def parse_sort_time(value: Any) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return datetime.min


if __name__ == "__main__":
    raise SystemExit(main())
