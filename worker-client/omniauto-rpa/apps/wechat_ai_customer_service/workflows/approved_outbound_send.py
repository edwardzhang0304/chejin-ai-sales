"""Approved outbound sender for WeChat customer-service test contacts.

This is the first guarded building block for scheduled outreach. It does not
select contacts by itself: the target must already be enabled in the workflow
config, and the command is dry-run unless ``--send`` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS_ROOT = APP_ROOT / "adapters"
WORKFLOWS_ROOT = Path(__file__).resolve().parent
for path in (WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from customer_service_review_queue import build_review_queue
from listen_and_reply import (
    CONFIG_PATH,
    StateLock,
    append_audit,
    check_rate_limit,
    load_config,
    load_state,
    parse_targets,
    record_reply_timestamp,
    resolve_path,
    save_state,
)
from wechat_connector import WeChatConnector


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--target", required=True)
    parser.add_argument("--session-key", default="", help="Optional internal session key for row-level RPA targeting.")
    parser.add_argument("--text", required=True)
    parser.add_argument("--send", action="store_true", help="Actually send the outbound message.")
    parser.add_argument("--reason", default="manual_test")
    parser.add_argument("--allow-prefixless", action="store_true")
    parser.add_argument("--ignore-review-queue", action="store_true")
    parser.add_argument("--ignore-rate-limit", action="store_true")
    args = parser.parse_args()

    try:
        result = run(args)
    except Exception as exc:
        result = {"ok": False, "error": repr(exc)}
    print_json(result)
    return 0 if result.get("ok") else 1


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    target = find_target(config, args.target)
    reply_prefix = str(config.get("reply", {}).get("prefix") or "")
    if reply_prefix and not args.allow_prefixless and not args.text.startswith(reply_prefix):
        raise ValueError(f"Outbound text must start with configured prefix: {reply_prefix}")

    queue = build_review_queue(args.config, include_resolved=False, limit=100)
    queue_counts = queue.get("counts", {})
    if not args.ignore_review_queue and (
        queue_counts.get("open_pending_customer_data", 0)
        or queue_counts.get("handoff", 0)
        or queue_counts.get("audit_attention", 0)
    ):
        raise ValueError("Review queue is not clean; inspect it before outbound send.")

    state_path = resolve_path(config.get("state_path"))
    audit_path = resolve_path(config.get("audit_log_path"))
    lock_settings = config.get("state_lock", {}) or {}
    with StateLock(
        state_path.with_suffix(state_path.suffix + ".lock"),
        timeout_seconds=int(lock_settings.get("timeout_seconds", 120)),
        stale_seconds=int(lock_settings.get("stale_seconds", 900)),
    ):
        state = load_state(state_path)
        target_state = state.setdefault("targets", {}).setdefault(
            target.name,
            {
                "processed_message_ids": [],
                "handoff_message_ids": [],
                "sent_replies": [],
                "reply_timestamps": [],
            },
        )
        rate_check = check_rate_limit(target_state, config)
        event = {
            "ok": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "target": target.name,
            "action": "outbound_planned",
            "dry_run": not args.send,
            "reason": args.reason,
            "text": args.text,
            "rate_limit": rate_check,
            "review_queue_counts": queue_counts,
        }
        if not args.ignore_rate_limit and not rate_check["allowed"]:
            event["ok"] = False
            event["action"] = "outbound_blocked"
            event["blocked_reason"] = rate_check["reason"]
            append_audit(audit_path, event)
            return event

        if not args.send:
            append_audit(audit_path, event)
            return event

        connector = WeChatConnector()
        send_result = connector.send_text_and_verify(
            target.name,
            args.text,
            exact=target.exact,
            session_key=str(args.session_key or getattr(target, "session_key", "") or ""),
        )
        event["send_result"] = send_result
        event["verified"] = bool(send_result.get("verified"))
        if not event["verified"]:
            event["ok"] = False
            event["action"] = "outbound_error"
            append_audit(audit_path, event)
            return event

        record_reply_timestamp(target_state)
        target_state.setdefault("outbound_sends", []).append(
            {
                "text": args.text,
                "reason": args.reason,
                "sent_at": datetime.now().isoformat(timespec="seconds"),
                "verified": True,
            }
        )
        event["action"] = "outbound_sent"
        append_audit(audit_path, event)
        save_state(state_path, state)
        return event


def find_target(config: dict[str, Any], target_name: str) -> Any:
    for target in parse_targets(config):
        if target.name == target_name:
            return target
    raise ValueError("Target is not enabled in config; add it to the whitelist first.")


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
