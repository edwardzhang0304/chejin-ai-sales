"""Minimal guarded WeChat customer-service loop.

This is the first workflow layer above ``WeChatConnector``. It reads one target
conversation, selects the latest unprocessed incoming text message, generates a
rule-based reply, optionally sends it, verifies the send, and persists the
processed message id.

Defaults are intentionally conservative:
- target: File Transfer Assistant
- dry-run: enabled unless ``--send`` is passed
- only non-self messages are eligible unless ``--allow-self-for-test`` is passed
- fallback replies are not sent unless ``--allow-fallback-send`` is passed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS_ROOT = APP_ROOT / "adapters"
WORKFLOWS_ROOT = Path(__file__).resolve().parent
for path in (WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from wechat_connector import FILE_TRANSFER_ASSISTANT, ROOT, WeChatConnector, WeChatConnectorError


RULES_PATH = ROOT / "apps/wechat_ai_customer_service/configs/customer_service_rules.example.json"
STATE_PATH = ROOT / "runtime/apps/wechat_ai_customer_service/state/minimal_loop_state.json"
MAX_PROCESSED_IDS = 500
BOT_PREFIX = "[OmniAuto客服]"


@dataclass(frozen=True)
class ReplyDecision:
    reply_text: str
    rule_name: str | None
    matched: bool
    need_handoff: bool
    reason: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=FILE_TRANSFER_ASSISTANT)
    parser.add_argument("--session-key", default="", help="Optional internal session key for row-level RPA targeting.")
    parser.add_argument("--rules", type=Path, default=RULES_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--send", action="store_true", help="Actually send the reply.")
    parser.add_argument(
        "--allow-self-for-test",
        action="store_true",
        help="Treat self messages as incoming. Use only with File Transfer Assistant tests.",
    )
    parser.add_argument(
        "--allow-fallback-send",
        action="store_true",
        help="Allow sending the default reply when no keyword rule matched.",
    )
    parser.add_argument(
        "--mark-dry-run",
        action="store_true",
        help="Mark the selected message as processed even when --send is not used.",
    )
    parser.add_argument(
        "--reply-prefix",
        default=BOT_PREFIX + " ",
        help="Prefix added to generated replies. Set to empty string to disable.",
    )
    args = parser.parse_args()

    try:
        result = run_once(args)
    except Exception as exc:
        result = {"ok": False, "error": repr(exc)}

    print_json(result)
    return 0 if result.get("ok") else 1


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    if args.allow_self_for_test and args.target != FILE_TRANSFER_ASSISTANT:
        raise ValueError("--allow-self-for-test is only allowed for File Transfer Assistant")

    connector = WeChatConnector()
    status = connector.require_online()
    rules = load_rules(args.rules)
    state = load_state(args.state)

    messages_payload = connector.get_messages(args.target, exact=True, session_key=str(getattr(args, "session_key", "") or ""))
    if not messages_payload.get("ok"):
        return {"ok": False, "status": status, "messages": messages_payload}

    target_state = state.setdefault("targets", {}).setdefault(
        args.target,
        {"processed_message_ids": [], "sent_replies": []},
    )
    processed_ids = set(target_state.get("processed_message_ids", []))
    selected = select_message(
        messages_payload.get("messages", []) or [],
        processed_ids=processed_ids,
        allow_self_for_test=bool(args.allow_self_for_test),
    )
    if selected is None:
        return {
            "ok": True,
            "action": "skipped",
            "reason": "no eligible unprocessed text message",
            "status": status,
            "target": args.target,
            "dry_run": not args.send,
        }

    decision = decide_reply(str(selected.get("content") or ""), rules)
    reply_text = format_reply(decision.reply_text, args.reply_prefix)
    send_allowed = bool(args.send and (decision.matched or args.allow_fallback_send))
    decision_payload = decision.__dict__.copy()
    decision_payload["raw_reply_text"] = decision.reply_text
    decision_payload["reply_text"] = reply_text
    result: dict[str, Any] = {
        "ok": True,
        "action": "planned" if not send_allowed else "sent",
        "target": args.target,
        "dry_run": not args.send,
        "selected_message": selected,
        "decision": decision_payload,
    }

    if args.send and not send_allowed:
        result["action"] = "blocked"
        result["ok"] = True
        result["reason"] = "fallback reply blocked; pass --allow-fallback-send or add a rule"
        return result

    if send_allowed:
        verified = connector.send_text_and_verify(
            args.target,
            reply_text,
            exact=True,
            session_key=str(getattr(args, "session_key", "") or ""),
        )
        result["send_result"] = verified
        result["verified"] = bool(verified.get("verified"))
        if not result["verified"]:
            result["ok"] = False
            return result
        mark_processed(target_state, selected, reply_text)
        save_state(args.state, state)
    elif args.mark_dry_run:
        mark_processed(target_state, selected, reply_text)
        save_state(args.state, state)
        result["marked_processed"] = True

    return result


def load_rules(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "targets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def select_message(
    messages: list[dict[str, Any]],
    processed_ids: set[str],
    allow_self_for_test: bool,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        message_id = str(message.get("id") or "")
        content = str(message.get("content") or "").strip()
        sender = str(message.get("sender") or "")
        if not message_id or message_id in processed_ids:
            continue
        if message.get("type") != "text" or not content:
            continue
        if content.startswith(BOT_PREFIX):
            continue
        if sender == "self" and not allow_self_for_test:
            continue
        return message
    return None


def decide_reply(content: str, rules: dict[str, Any]) -> ReplyDecision:
    normalized = content.lower()
    matches = []
    for rule in rules.get("rules", []) or []:
        keywords = [str(item).lower() for item in rule.get("keywords", []) or [] if not is_non_business_tracking_keyword(str(item))]
        matched_keywords = [keyword for keyword in keywords if keyword and keyword in normalized]
        if not matched_keywords:
            continue
        priority = int(rule.get("priority", 0) or 0)
        matches.append((priority, len(matched_keywords), max(len(item) for item in matched_keywords), rule))
    if matches:
        _, _, _, rule = max(matches, key=lambda item: (item[0], item[1], item[2]))
        return ReplyDecision(
            reply_text=str(rule.get("reply") or ""),
            rule_name=str(rule.get("name") or ""),
            matched=True,
            need_handoff=False,
            reason="keyword_rule_matched",
        )
    return ReplyDecision(
        reply_text=str(rules.get("default_reply") or ""),
        rule_name=None,
        matched=False,
        need_handoff=True,
        reason="no_rule_matched",
    )


def is_non_business_tracking_keyword(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.fullmatch(r"(?:CHEJIN|LLMSYN|DBG|TEST|BATCH)_[A-Za-z0-9_:-]{4,}", text, flags=re.IGNORECASE))


def format_reply(reply_text: str, prefix: str) -> str:
    if not prefix:
        return reply_text
    if reply_text.startswith(prefix):
        return reply_text
    return prefix + reply_text


def mark_processed(target_state: dict[str, Any], message: dict[str, Any], reply_text: str) -> None:
    processed = list(target_state.get("processed_message_ids", []))
    message_id = str(message.get("id") or "")
    if message_id and message_id not in processed:
        processed.append(message_id)
    target_state["processed_message_ids"] = processed[-MAX_PROCESSED_IDS:]
    target_state.setdefault("sent_replies", []).append(
        {
            "message_id": message_id,
            "message_content": message.get("content"),
            "reply_text": reply_text,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


def print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
