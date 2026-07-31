from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import CONFIG
from .c2_contract import c2_contract_v3


ACTION_JOURNAL_SCHEMA_VERSION = 1
ACTION_PHASES = tuple(
    str(value)
    for value in (c2_contract_v3().get("action_phases") or [])
)
if not ACTION_PHASES:
    raise RuntimeError("C2 action_phases contract is empty")
_PHASE_RANK = {value: index for index, value in enumerate(ACTION_PHASES)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_token(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:24]


def action_journal_path(action_kind: str, transaction_id: str) -> Path:
    action = str(action_kind or "").strip().lower()
    identity = str(transaction_id or "").strip()
    if action not in {"send", "voice", "image", "add_friend"} or not identity:
        raise ValueError("ACTION_JOURNAL_IDENTITY_INVALID")
    return (
        CONFIG.app_dir
        / "transactions"
        / "actions"
        / action
        / f"{_safe_token(identity)}.json"
    )


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_action_journal(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def initialize_action_journal(
    path: str | Path,
    *,
    action_kind: str,
    transaction_id: str,
    conversation_id: str,
    items: Iterable[dict[str, Any]],
) -> Path:
    target = Path(path)
    normalized_items: dict[str, dict[str, Any]] = {}
    for item in items:
        source_key = str(item.get("source_message_key") or "").strip()
        if not source_key:
            continue
        normalized_items[source_key] = {
            "source_message_key": source_key,
            "physical_anchor_keys": sorted(
                {
                    str(value).strip()
                    for value in (item.get("physical_anchor_keys") or [])
                    if str(value).strip()
                }
            ),
            "action_phase": "not_attempted",
            "business_state": None,
            "business_result_confirmed": False,
            "error_code": None,
            "terminal_payload": None,
            "updated_at": _now_iso(),
        }
        replayable_observation = item.get("replayable_observation")
        if isinstance(replayable_observation, dict):
            normalized_items[source_key]["replayable_observation"] = (
                json.loads(
                    json.dumps(
                        replayable_observation,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            )
    if not normalized_items:
        raise ValueError("ACTION_JOURNAL_ITEMS_MISSING")
    now = _now_iso()
    _atomic_write(
        target,
        {
            "schema_version": ACTION_JOURNAL_SCHEMA_VERSION,
            "action_kind": str(action_kind or "").strip().lower(),
            "transaction_id": str(transaction_id or "").strip(),
            "conversation_id": str(conversation_id or "").strip(),
            "action_phase": "not_attempted",
            "items": normalized_items,
            "created_at": now,
            "updated_at": now,
        },
    )
    return target


def update_action_journal_item(
    path: str | Path,
    *,
    source_message_key: str,
    action_phase: str | None = None,
    business_state: str | None = None,
    business_result_confirmed: bool | None = None,
    error_code: str | None = None,
    terminal_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(path)
    payload = read_action_journal(target)
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    source_key = str(source_message_key or "").strip()
    item = dict(items.get(source_key) or {})
    if not item:
        raise ValueError("ACTION_JOURNAL_ITEM_NOT_FOUND")
    current_phase = str(item.get("action_phase") or "not_attempted")
    requested_phase = str(action_phase or current_phase).strip()
    if requested_phase not in _PHASE_RANK:
        raise ValueError("ACTION_JOURNAL_PHASE_INVALID")
    if _PHASE_RANK[requested_phase] < _PHASE_RANK.get(current_phase, 0):
        requested_phase = current_phase
    item["action_phase"] = requested_phase
    if business_state is not None:
        item["business_state"] = str(business_state or "").strip() or None
    if business_result_confirmed is not None:
        item["business_result_confirmed"] = bool(
            business_result_confirmed
        )
    if error_code is not None:
        item["error_code"] = str(error_code or "").strip() or None
    if terminal_payload is not None:
        item["terminal_payload"] = dict(terminal_payload)
    item["updated_at"] = _now_iso()
    items[source_key] = item
    payload["items"] = items
    payload["action_phase"] = max(
        (
            str(value.get("action_phase") or "not_attempted")
            for value in items.values()
            if isinstance(value, dict)
        ),
        key=lambda value: _PHASE_RANK.get(value, 0),
        default="not_attempted",
    )
    payload["updated_at"] = _now_iso()
    _atomic_write(target, payload)
    return payload


def list_action_journals(
    *,
    conversation_id: str | None = None,
    action_kinds: Iterable[str] = ("voice", "image"),
) -> list[tuple[Path, dict[str, Any]]]:
    root = CONFIG.app_dir / "transactions" / "actions"
    results: list[tuple[Path, dict[str, Any]]] = []
    normalized_conversation = str(conversation_id or "").strip()
    for action_kind in action_kinds:
        folder = root / str(action_kind or "").strip().lower()
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            payload = read_action_journal(path)
            if not payload:
                continue
            if (
                normalized_conversation
                and str(payload.get("conversation_id") or "")
                != normalized_conversation
            ):
                continue
            results.append((path, payload))
    return results


def remove_action_journal(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return


def action_journal_phase(path: str | Path) -> str:
    payload = read_action_journal(path)
    phase = str(payload.get("action_phase") or "").strip()
    if phase in ACTION_PHASES:
        return phase
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    return max(
        (
            str(item.get("action_phase") or "not_attempted")
            for item in items.values()
            if isinstance(item, dict)
        ),
        key=lambda value: _PHASE_RANK.get(value, 0),
        default="not_attempted",
    )
