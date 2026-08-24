from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import CONFIG
from .c2_contract import c2_contract_v3


ACTION_JOURNAL_SCHEMA_VERSION = 5
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
    origin_read_run_id: str | None = None,
    pre_action_identity_sequence: list[dict[str, Any]] | None = None,
    pre_frame_id: str | None = None,
    canonical_action_id: str | None = None,
    reserved_worker_stable_id: str | None = None,
    prepare_evidence: dict[str, Any] | None = None,
) -> Path:
    target = Path(path)
    normalized_action_kind = str(action_kind or "").strip().lower()
    normalized_origin_read_run_id = str(origin_read_run_id or "").strip()
    if (
        normalized_action_kind in {"voice", "image"}
        and not normalized_origin_read_run_id
    ):
        raise ValueError("ACTION_JOURNAL_ORIGIN_READ_RUN_ID_MISSING")
    normalized_items: dict[str, dict[str, Any]] = {}
    for item in items:
        journal_item_id = str(item.get("journal_item_id") or "").strip()
        if not journal_item_id:
            continue
        if journal_item_id in normalized_items:
            raise ValueError("ACTION_JOURNAL_ITEM_ID_DUPLICATE")
        normalized_items[journal_item_id] = {
            "journal_item_id": journal_item_id,
            "action_local_id": str(
                item.get("action_local_id") or journal_item_id
            ).strip(),
            # A durable source key does not exist until the unique identity
            # commit gate succeeds.  The journal's dictionary key stays local
            # for its entire lifetime and is never renamed to a message key.
            "source_message_key": None,
            "origin_read_run_id": normalized_origin_read_run_id or None,
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
            replayable = json.loads(
                json.dumps(
                    replayable_observation,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            # Before the commit gate this snapshot is action evidence, not a
            # durable message.  Strip any caller-supplied source key so an
            # action-local identifier cannot be smuggled into recovery data.
            replayable.pop("source_message_key", None)
            replayable_source = replayable.get("source_message")
            if isinstance(replayable_source, dict):
                replayable_source.pop("source_message_key", None)
            normalized_items[journal_item_id]["replayable_observation"] = (
                replayable
            )
    if not normalized_items:
        raise ValueError("ACTION_JOURNAL_ITEMS_MISSING")
    now = _now_iso()
    action_id = str(canonical_action_id or "").strip()
    reserved_id = str(reserved_worker_stable_id or "").strip()
    pre_sequence = [
        json.loads(json.dumps(item, ensure_ascii=False))
        for item in (pre_action_identity_sequence or [])
        if isinstance(item, dict)
    ]
    if pre_sequence and not str(pre_frame_id or "").strip():
        raise ValueError("ACTION_JOURNAL_PRE_FRAME_ID_MISSING")
    if bool(action_id) != bool(reserved_id):
        raise ValueError("ACTION_JOURNAL_RESERVED_IDENTITY_INCOMPLETE")
    normalized_prepare_evidence = (
        json.loads(json.dumps(prepare_evidence, ensure_ascii=False))
        if isinstance(prepare_evidence, dict)
        else None
    )
    if normalized_action_kind == "voice":
        required_prepare_fields = {
            "pre_frame_id",
            "selected_pre_observation_id",
            "selected_action_token",
            "selected_target_fingerprint",
            "message_viewport_change_digest",
        }
        if not isinstance(normalized_prepare_evidence, dict) or any(
            not str(normalized_prepare_evidence.get(field) or "").strip()
            for field in required_prepare_fields
        ):
            raise ValueError("ACTION_JOURNAL_VOICE_PREPARE_EVIDENCE_MISSING")
        if (
            str(normalized_prepare_evidence["pre_frame_id"]).strip()
            != str(pre_frame_id or "").strip()
        ):
            raise ValueError("ACTION_JOURNAL_VOICE_PRE_FRAME_CONFLICT")
    _atomic_write(
        target,
        {
            "schema_version": ACTION_JOURNAL_SCHEMA_VERSION,
            "action_kind": normalized_action_kind,
            "transaction_id": str(transaction_id or "").strip(),
            "conversation_id": str(conversation_id or "").strip(),
            "origin_read_run_id": normalized_origin_read_run_id or None,
            "canonical_action_id": action_id or None,
            "reserved_worker_stable_id": reserved_id or None,
            "pre_frame_id": str(pre_frame_id or "").strip() or None,
            "pre_action_identity_sequence": pre_sequence,
            "prepare_evidence": normalized_prepare_evidence,
            "sequence_alignment_evidence": None,
            "action_phase": "not_attempted",
            "items": normalized_items,
            "created_at": now,
            "updated_at": now,
        },
    )
    return target


def record_action_sequence_alignment(
    path: str | Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Persist post-action alignment without rebuilding the pre-action state."""

    target = Path(path)
    payload = read_action_journal(target)
    if not payload:
        raise ValueError("ACTION_JOURNAL_NOT_FOUND")
    if not isinstance(payload.get("pre_action_identity_sequence"), list):
        raise ValueError("ACTION_JOURNAL_PRE_SEQUENCE_MISSING")
    status = str(evidence.get("alignment_status") or "").strip()
    if status not in {"unique", "ambiguous", "unresolved", "not_required"}:
        raise ValueError("ACTION_JOURNAL_ALIGNMENT_STATUS_INVALID")
    payload["sequence_alignment_evidence"] = json.loads(
        json.dumps(evidence, ensure_ascii=False)
    )
    payload["updated_at"] = _now_iso()
    _atomic_write(target, payload)
    return payload


def commit_action_journal_item_identity(
    path: str | Path,
    *,
    journal_item_id: str,
    source_message_key: str,
) -> dict[str, Any]:
    """Attach a durable identity without replacing the action-local key."""

    target = Path(path)
    payload = read_action_journal(target)
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    item_id = str(journal_item_id or "").strip()
    source_key = str(source_message_key or "").strip()
    if not item_id or not source_key or item_id not in items:
        raise ValueError("ACTION_JOURNAL_ITEM_IDENTITY_INVALID")
    item = dict(items[item_id])
    existing_source_key = str(item.get("source_message_key") or "").strip()
    if existing_source_key and existing_source_key != source_key:
        raise ValueError("ACTION_JOURNAL_ITEM_IDENTITY_CONFLICT")
    item["source_message_key"] = source_key
    item["identity_committed_at"] = _now_iso()
    item["updated_at"] = _now_iso()
    items[item_id] = item
    payload["items"] = items
    payload["committed_worker_stable_id"] = str(
        payload.get("reserved_worker_stable_id") or ""
    ).strip() or None
    payload["updated_at"] = _now_iso()
    _atomic_write(target, payload)
    return payload


def update_action_journal_item(
    path: str | Path,
    *,
    journal_item_id: str,
    action_phase: str | None = None,
    business_state: str | None = None,
    business_result_confirmed: bool | None = None,
    error_code: str | None = None,
    terminal_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(path)
    payload = read_action_journal(target)
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    item_id = str(journal_item_id or "").strip()
    item = dict(items.get(item_id) or {})
    if not item:
        raise ValueError("ACTION_JOURNAL_ITEM_NOT_FOUND")
    current_phase = str(item.get("action_phase") or "not_attempted")
    requested_phase = str(action_phase or current_phase).strip()
    if requested_phase not in _PHASE_RANK:
        raise ValueError("ACTION_JOURNAL_PHASE_INVALID")
    if _PHASE_RANK[requested_phase] < _PHASE_RANK.get(current_phase, 0):
        # A delayed writer must not keep the newer phase while overwriting
        # its business result, confirmation bit, error, or terminal evidence
        # with an older snapshot. Treat the whole regressive mutation as a
        # stale write, not just the action_phase field.
        return payload
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
    items[item_id] = item
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


def action_journal_item_has_formed_fact(item: dict[str, Any]) -> bool:
    """Return whether an item contains a result that must be settled."""

    phase = str(item.get("action_phase") or "not_attempted").strip()
    if phase == "cancelled_before_trigger":
        return False
    terminal_payload = item.get("terminal_payload")
    return bool(
        phase != "not_attempted"
        or str(item.get("business_state") or "").strip()
        in {"completed", "failed"}
        or str(item.get("error_code") or "").strip()
        or (
            isinstance(terminal_payload, dict)
            and terminal_payload
        )
        or item.get("business_result_confirmed") is True
    )


def action_journal_is_strictly_not_attempted(
    payload: dict[str, Any],
) -> bool:
    """True only for a pure UI intent with no persisted business fact."""

    items = payload.get("items")
    if (
        str(payload.get("action_phase") or "").strip()
        not in {"not_attempted", "cancelled_before_trigger"}
        or not isinstance(items, dict)
        or not items
    ):
        return False
    return all(
        isinstance(item, dict)
        and str(item.get("action_phase") or "").strip()
        in {"not_attempted", "cancelled_before_trigger"}
        and not action_journal_item_has_formed_fact(item)
        for item in items.values()
    )
