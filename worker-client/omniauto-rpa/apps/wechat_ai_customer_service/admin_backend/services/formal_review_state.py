"""Review markers for newly added formal knowledge items."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def mark_item_new(item: dict[str, Any], source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an item marked as newly added until the operator acknowledges it."""
    result = dict(item)
    review_state = dict(result.get("review_state") if isinstance(result.get("review_state"), dict) else {})
    now = now_iso()
    review_state.update(
        {
            "is_new": True,
            "new_reason": "newly_added_formal_knowledge",
            "marked_at": review_state.get("marked_at") or now,
            "updated_at": now,
            "read_at": "",
            "read_by": "",
            "source": source or review_state.get("source") or {},
        }
    )
    result["review_state"] = review_state
    return result


def acknowledge_item(item: dict[str, Any], *, actor: str = "admin") -> dict[str, Any]:
    result = dict(item)
    review_state = dict(result.get("review_state") if isinstance(result.get("review_state"), dict) else {})
    now = now_iso()
    review_state.update(
        {
            "is_new": False,
            "read_at": now,
            "read_by": actor,
            "updated_at": now,
        }
    )
    result["review_state"] = review_state
    return result


def enrich_knowledge_item(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    review_state = result.get("review_state") if isinstance(result.get("review_state"), dict) else {}
    source = review_state.get("source") if isinstance(review_state.get("source"), dict) else {}
    source_module = str(source.get("source_module") or "")
    badges: list[dict[str, str]] = []
    if review_state.get("is_new"):
        unread_label = "NEW" if source_module == "rag_overlap" else "新加入"
        badges.append({"key": "new_unread", "label": unread_label, "tone": "danger"})
    if source_module == "candidate":
        badges.append({"key": "candidate_promoted", "label": "候选晋升", "tone": "info"})
    if source_module == "manual":
        badges.append({"key": "manual_added", "label": "手动新增", "tone": "muted"})
    result["display_badges"] = dedupe_badges(badges)
    return result


def sort_knowledge_items_for_review(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=knowledge_review_sort_key)


def knowledge_review_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    review_state = item.get("review_state") if isinstance(item.get("review_state"), dict) else {}
    is_new = bool(review_state.get("is_new"))
    unread_rank = 0 if is_new else 1
    if is_new:
        timestamp = (
            review_state.get("marked_at")
            or review_state.get("updated_at")
            or item.get("updated_at")
            or item.get("created_at")
            or ""
        )
    else:
        timestamp = (
            review_state.get("read_at")
            or review_state.get("updated_at")
            or review_state.get("marked_at")
            or item.get("updated_at")
            or item.get("created_at")
            or ""
        )
    return (unread_rank, -timestamp_value(timestamp), str(item.get("id") or ""))


def timestamp_value(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def dedupe_badges(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        key = str(item.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
