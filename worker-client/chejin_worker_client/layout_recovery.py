"""Finite retries for proven layout failures, independent of flow settlement."""
from typing import Any

LAYOUT_ERROR_CODES = frozenset({
    "WECHAT_UI_LAYOUT_UNRESOLVED", "WECHAT_UI_LAYOUT_STALE",
    "WECHAT_UI_COORDINATE_MAPPING_INVALID",
})
LAYOUT_RETRY_LIMIT = 3


def layout_error_code(payload: dict[str, Any]) -> str:
    """Read typed evidence, never infer a layout failure from arbitrary text."""
    target = payload.get("target_confirmation")
    source = target if isinstance(target, dict) else payload
    guard = source.get("guard")
    guard = guard if isinstance(guard, dict) else {}
    title = guard.get("conversation_type_evidence")
    title = title if isinstance(title, dict) else {}
    for evidence in (payload, source, guard, title):
        code = str(evidence.get("error_code") or "")
        if code in LAYOUT_ERROR_CODES:
            return code
    return ""


def record_result(state: dict[str, Any], *, scope: str, payload: dict[str, Any]) -> dict[str, Any]:
    counts = dict(state.get("counts") or {})
    code = layout_error_code(payload) if not payload.get("ok") else ""
    if code:
        counts[scope] = min(LAYOUT_RETRY_LIMIT, int(counts.get(scope) or 0) + 1)
    elif payload.get("ok"):
        counts.pop(scope, None)
    else:
        # A different failure is not evidence that the missing boundary was fixed.
        return state
    return {"counts": counts, "error_code": code or state.get("error_code", "")}


def recovery_view(state: dict[str, Any]) -> dict[str, Any]:
    attempts = max((int(v) for v in (state.get("counts") or {}).values()), default=0)
    blocked = attempts >= LAYOUT_RETRY_LIMIT
    message = (
        "3次无法确认微信界面位置，已暂停接单。请保持微信窗口完整可见，点击“开始接单”重试；仍失败请导出故障包。"
        if blocked else
        f"暂时无法确认微信界面位置，正在重新截图定位（第{attempts}次失败，最多3次）。"
        if attempts else ""
    )
    return {"blocked": blocked, "attempts": attempts, "message": message}
