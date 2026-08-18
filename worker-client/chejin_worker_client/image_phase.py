from __future__ import annotations

from typing import Any


IMAGE_PHASE_COUNTER_KEYS = (
    "discovered",
    "completed",
    "failed",
    "cached",
    "authorization_revoked",
    "removed_from_final_screen",
)

IMAGE_PHASE_STATE_SOURCE_FIELDS = {
    "completed": "completed_source_keys",
    "failed": "failed_source_keys",
    "cached": "cached_source_keys",
}


def new_image_phase_result() -> dict[str, Any]:
    result: dict[str, Any] = {
        key: 0 for key in IMAGE_PHASE_COUNTER_KEYS
    }
    result.update(
        {
            "new_action_source_keys": [],
            "terminal_source_keys": [],
            "removed_source_keys": [],
            "refresh_source_keys": [],
            "ui_frame_invalidated_source_keys": [],
            **{
                field: []
                for field in IMAGE_PHASE_STATE_SOURCE_FIELDS.values()
            },
            "new_action_count": 0,
            "ui_frame_invalidated": False,
            "requires_final_refresh": False,
        }
    )
    return result


def _clean_keys(values: Any) -> set[str]:
    return {
        str(value).strip()
        for value in (values or [])
        if str(value or "").strip()
    }


def finalize_image_phase_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    action_keys = _clean_keys(result.get("new_action_source_keys"))
    terminal_keys = _clean_keys(result.get("terminal_source_keys"))
    removed_keys = _clean_keys(result.get("removed_source_keys"))
    invalidated_keys = _clean_keys(
        result.get("ui_frame_invalidated_source_keys")
    )
    refresh_keys = (
        _clean_keys(result.get("refresh_source_keys"))
        | action_keys
        | removed_keys
        | invalidated_keys
    )

    result["new_action_source_keys"] = sorted(action_keys)
    result["terminal_source_keys"] = sorted(terminal_keys)
    result["removed_source_keys"] = sorted(removed_keys)
    result["refresh_source_keys"] = sorted(refresh_keys)
    result["ui_frame_invalidated_source_keys"] = sorted(invalidated_keys)
    for state, field in IMAGE_PHASE_STATE_SOURCE_FIELDS.items():
        state_keys = _clean_keys(result.get(field))
        result[field] = sorted(state_keys)
        result[state] = len(state_keys)
    result["removed_from_final_screen"] = len(removed_keys)
    result["new_action_count"] = len(action_keys)
    result["ui_frame_invalidated"] = bool(invalidated_keys)
    result["requires_final_refresh"] = bool(refresh_keys)
    return result


def mark_image_action(
    result: dict[str, Any],
    source_message_key: str,
) -> None:
    result.setdefault("new_action_source_keys", []).append(
        str(source_message_key or "").strip()
    )
    finalize_image_phase_result(result)


def mark_image_ui_frame_invalidated(
    result: dict[str, Any],
    source_message_key: str,
) -> None:
    key = str(source_message_key or "").strip()
    if not key:
        return
    result.setdefault("ui_frame_invalidated_source_keys", []).append(key)
    result.setdefault("refresh_source_keys", []).append(key)
    finalize_image_phase_result(result)


def mark_image_terminal(
    result: dict[str, Any],
    source_message_key: str,
    *,
    terminal_state: str,
    cached: bool = False,
) -> None:
    key = str(source_message_key or "").strip()
    state = str(terminal_state or "").strip().lower()
    if not key or state not in {"completed", "failed"}:
        raise ValueError("C2_IMAGE_TERMINAL_STATE_INVALID")
    result.setdefault("terminal_source_keys", []).append(key)
    result.setdefault(
        IMAGE_PHASE_STATE_SOURCE_FIELDS[state],
        [],
    ).append(key)
    if cached:
        result.setdefault("cached_source_keys", []).append(key)
    finalize_image_phase_result(result)


def mark_image_removed_from_final_screen(
    result: dict[str, Any],
    source_message_key: str,
) -> None:
    key = str(source_message_key or "").strip()
    if not key:
        return
    result.setdefault("removed_source_keys", []).append(key)
    result.setdefault("refresh_source_keys", []).append(key)
    finalize_image_phase_result(result)


def merge_image_phase_results(
    target: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    for key in ("discovered", "authorization_revoked"):
        target[key] = max(
            int(target.get(key) or 0),
            int(incoming.get(key) or 0),
        )
    for state, field in IMAGE_PHASE_STATE_SOURCE_FIELDS.items():
        target.setdefault(field, []).extend(incoming.get(field) or [])
    target.setdefault("new_action_source_keys", []).extend(
        incoming.get("new_action_source_keys") or []
    )
    target.setdefault("terminal_source_keys", []).extend(
        incoming.get("terminal_source_keys") or []
    )
    target.setdefault("removed_source_keys", []).extend(
        incoming.get("removed_source_keys") or []
    )
    target.setdefault("refresh_source_keys", []).extend(
        incoming.get("refresh_source_keys") or []
    )
    target.setdefault("ui_frame_invalidated_source_keys", []).extend(
        incoming.get("ui_frame_invalidated_source_keys") or []
    )
    return finalize_image_phase_result(target)
