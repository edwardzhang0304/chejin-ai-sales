"""Runtime orchestration for multi-session customer-service scheduling.

This module intentionally keeps WeChat operations behind callbacks:

- capture_fn: the only place that should read WeChat via RPA.
- plan_reply_fn: LLM/rule/RAG planning; must not call RPA.
- freshness_fn/send_fn: the only place that should send via RPA.

The runtime can therefore be tested offline while preserving the production
invariant that WeChat foreground automation remains serial.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler_state import (
    SchedulerConfig,
    SchedulerStateStore,
    append_event,
    cleanup_scheduler_state,
    complete_llm_task,
    complete_polish_task,
    enqueue_llm_task,
    enqueue_pending_session,
    enqueue_polish_task,
    fail_llm_task,
    fail_polish_task,
    get_session_by_identity,
    has_active_session_work,
    mark_capture_started,
    mark_llm_started,
    mark_polish_started,
    mark_reply_failed,
    mark_reply_sending,
    mark_reply_sent,
    mark_reply_stale,
    message_content_digest,
    normalize_repeatable_probe_text,
    recover_orphaned_running_llm_tasks,
    recover_orphaned_running_polish_tasks,
    record_capture_result,
    record_session_signal,
    requeue_capture_after_recoverable_llm_failure,
    select_capture_sessions,
    select_ready_replies,
    state_summary,
    stable_id,
)
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_session_ledger import (
    SessionLedgerStore,
    stable_session_key,
)
from apps.wechat_ai_customer_service.message_identity import (
    apply_canonical_identity_fields,
    canonical_input_message_id,
)
from apps.wechat_ai_customer_service.workflows.listen_and_reply import TargetConfig as WorkflowTargetConfig
from apps.wechat_ai_customer_service.wechat_message_normalizer import split_wechat_ocr_speaker_prefix


CaptureFn = Callable[[dict[str, Any]], dict[str, Any]]
PlanReplyFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
PolishReplyFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
FreshnessFn = Callable[[dict[str, Any]], dict[str, Any]]
SendFn = Callable[[dict[str, Any]], dict[str, Any]]
CaptureDoneFn = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None]


_RPA_HUMANIZED_SEND_ENV_MAPPING = {
    "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED": "enabled",
    "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD": "input_method",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS": "typing_chunk_min_chars",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS": "typing_chunk_max_chars",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS": "typing_char_delay_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS": "typing_char_delay_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_EVERY_CHARS": "typing_micro_pause_every_chars",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MIN_MS": "typing_micro_pause_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_MICRO_PAUSE_MAX_MS": "typing_micro_pause_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY": "typing_typo_probability",
    "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_MAX": "typing_typo_max",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MIN_MS": "send_pre_delay_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_PRE_DELAY_MAX_MS": "send_pre_delay_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MIN_MS": "send_post_input_delay_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_POST_INPUT_DELAY_MAX_MS": "send_post_input_delay_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS": "send_trigger_delay_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS": "send_trigger_delay_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS": "send_after_trigger_delay_min_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS": "send_after_trigger_delay_max_ms",
    "WECHAT_WIN32_OCR_HUMANIZED_ADAPTIVE_SPEED_ENABLED": "adaptive_speed_enabled",
    "WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION": "fast_send_confirmation_enabled",
    "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM": "input_fast_visual_confirm_enabled",
    "WECHAT_WIN32_OCR_SEND_TRIGGER_MODE": "send_trigger_mode",
    "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": "send_input_confirm_attempts",
    "WECHAT_WIN32_OCR_SEND_MIN_INTERVAL_SECONDS": "send_rate_min_interval_seconds",
    "WECHAT_WIN32_OCR_SEND_BURST_WINDOW_SECONDS": "send_rate_burst_window_seconds",
    "WECHAT_WIN32_OCR_SEND_BURST_LIMIT": "send_rate_burst_limit",
}


_SELF_MESSAGE_SENDERS = {"self", "assistant", "agent", "me", "outbound"}
_SHORT_PENDING_SIGNAL_KIND = "high_sensitivity_short"
_SHORT_PENDING_SIGNAL_MAX_AGE_SECONDS = 120.0


def safe_json_roundtrip(value: Any) -> Any:
    """Return a JSON-safe copy for scheduler task results.

    Planner/polish result payloads can contain dataclasses, exceptions, or
    other non-JSON objects in failure paths.  State files must stay writable,
    but this helper must never author or modify customer-visible wording.
    """

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"unserializable": repr(value)}


def _planner_event_latency_trace(event: dict[str, Any]) -> dict[str, Any]:
    """Extract optional planner/polish timing already recorded inside workflow events."""

    if not isinstance(event, dict):
        return {}
    trace: dict[str, Any] = {}
    brain = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
    brain_trace = brain.get("latency_trace") if isinstance(brain.get("latency_trace"), dict) else {}
    if isinstance(brain_trace, dict):
        trace.update(copy.deepcopy(brain_trace))
    final_polish = event.get("final_visible_llm_polish") if isinstance(event.get("final_visible_llm_polish"), dict) else {}
    final_trace = final_polish.get("latency_trace") if isinstance(final_polish.get("latency_trace"), dict) else {}
    if isinstance(final_trace, dict):
        trace.update(copy.deepcopy(final_trace))
    final_duration = final_polish.get("duration_seconds")
    if final_duration is not None:
        trace["final_polish_llm_duration_seconds"] = final_duration
    return trace


def _merge_result_latency_trace(result: dict[str, Any], extra_trace: dict[str, Any]) -> dict[str, Any]:
    """Append optional timing fields without replacing workflow-owned values."""

    if not isinstance(result, dict):
        return result
    clean_extra = {key: value for key, value in (extra_trace or {}).items() if value is not None}
    if not clean_extra:
        return result
    trace = result.get("latency_trace") if isinstance(result.get("latency_trace"), dict) else {}
    result["latency_trace"] = {**clean_extra, **trace}
    return result


def polish_failure_retry_instruction(task: dict[str, Any]) -> str:
    """Build prompt-only feedback for Brain after a final-polish block."""

    failure = task.get("last_polish_failure") if isinstance(task.get("last_polish_failure"), dict) else {}
    if not failure:
        return ""
    reason = str(failure.get("reason") or failure.get("error") or "").strip()
    event = failure.get("event") if isinstance(failure.get("event"), dict) else {}
    final_polish = event.get("final_visible_llm_polish") if isinstance(event.get("final_visible_llm_polish"), dict) else {}
    guard = final_polish.get("guard") if isinstance(final_polish.get("guard"), dict) else {}
    guard_reason = str(guard.get("reason") or final_polish.get("reason") or "").strip()
    candidate = final_polish.get("candidate") if isinstance(final_polish.get("candidate"), dict) else {}
    candidate_reply = str(candidate.get("reply") or final_polish.get("reply_text") or "").strip()
    parts = [
        "上一版Brain回复被最终可见审校拦截，本次必须由Brain重新思考并改写，不能沿用原回复。",
        "审校只提供风险信号，最终客户可见话术仍必须由Brain重新生成。",
    ]
    normalized = " ".join(item for item in (reason, guard_reason) if item)
    if normalized:
        parts.append("拦截原因：" + normalized[:180])
    if "identity_truth_discussion_not_allowed" in normalized:
        parts.append(
            "客户在试探是否AI、机器人、自动回复或真人时，不能承认或否认身份，"
            "不能说被发现了、猜对了、确实是、不是AI或不是机器人；"
            "请自然回应客户感受，调整沟通节奏，再接住当前问题。"
        )
    if candidate_reply:
        parts.append("上一版不可发送回复片段：" + candidate_reply[:180])
    return " ".join(parts)[:700]


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _latency_seconds_between(start: Any, finish: Any) -> float | None:
    started_at = _parse_iso_datetime(start)
    finished_at = _parse_iso_datetime(finish)
    if started_at is None or finished_at is None:
        return None
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        finished_at = finished_at.replace(tzinfo=None)
    elif started_at.tzinfo is not None and finished_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    seconds = (finished_at - started_at).total_seconds()
    if seconds < 0:
        return None
    return round(seconds, 4)


def _latency_breakdown_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(trace, dict):
        return {}
    pairs = {
        "signal_to_capture_start_seconds": ("session_signal_detected_at", "capture_started_at"),
        "capture_seconds": ("capture_started_at", "capture_finished_at"),
        "capture_to_brain_start_seconds": ("capture_finished_at", "brain_started_at"),
        "brain_queue_seconds": ("brain_queued_at", "brain_started_at"),
        "brain_seconds": ("brain_started_at", "brain_finished_at"),
        "planner_future_seconds": ("planner_future_submitted_at", "planner_future_finished_at"),
        "planner_worker_seconds": ("planner_worker_started_at", "planner_worker_finished_at"),
        "brain_to_polish_start_seconds": ("brain_finished_at", "final_polish_started_at"),
        "final_polish_queue_seconds": ("final_polish_queued_at", "final_polish_started_at"),
        "final_polish_seconds": ("final_polish_started_at", "final_polish_finished_at"),
        "polish_future_seconds": ("polish_future_submitted_at", "polish_future_finished_at"),
        "polish_worker_seconds": ("polish_worker_started_at", "polish_worker_finished_at"),
        "ready_queue_wait_seconds": ("send_queue_entered_at", "send_started_at"),
        "send_seconds": ("send_started_at", "send_finished_at"),
        "send_rpa_seconds": ("send_rpa_started_at", "send_rpa_finished_at"),
        "freshness_check_seconds": ("freshness_check_started_at", "freshness_check_finished_at"),
        "end_to_end_seconds": ("session_signal_detected_at", "send_finished_at"),
        "capture_to_sent_seconds": ("capture_started_at", "send_finished_at"),
        "brain_to_sent_seconds": ("brain_started_at", "send_finished_at"),
        "ready_to_sent_seconds": ("ready_at", "send_finished_at"),
    }
    breakdown: dict[str, Any] = {}
    for name, (start_key, finish_key) in pairs.items():
        seconds = _latency_seconds_between(trace.get(start_key), trace.get(finish_key))
        if seconds is not None:
            breakdown[name] = seconds
    planner_future_seconds = breakdown.get("planner_future_seconds")
    planner_worker_seconds = trace.get("planner_worker_duration_seconds")
    if planner_future_seconds is not None and planner_worker_seconds is not None:
        try:
            breakdown["planner_external_overhead_seconds"] = round(
                max(0.0, float(planner_future_seconds) - float(planner_worker_seconds)),
                4,
            )
        except (TypeError, ValueError):
            pass
    polish_future_seconds = breakdown.get("polish_future_seconds")
    polish_worker_seconds = trace.get("polish_worker_duration_seconds")
    if polish_future_seconds is not None and polish_worker_seconds is not None:
        try:
            breakdown["polish_external_overhead_seconds"] = round(
                max(0.0, float(polish_future_seconds) - float(polish_worker_seconds)),
                4,
            )
        except (TypeError, ValueError):
            pass
    return breakdown


def _short_pending_signal_is_fresh(
    detected_at: str,
    *,
    now: str | datetime | None = None,
    max_age_seconds: float = _SHORT_PENDING_SIGNAL_MAX_AGE_SECONDS,
) -> bool:
    """Return whether a monitor-only short preview is recent enough to recover.

    Short unread previews are intentionally high sensitivity, but they must not
    survive across listener restarts or long manual pauses.  Otherwise an old
    "谢谢/在吗" can be resurrected as a fresh customer turn.
    """

    detected = _parse_iso_datetime(detected_at)
    if detected is None:
        return False
    if now is None:
        return True
    current = now if isinstance(now, datetime) else _parse_iso_datetime(now)
    if current is None:
        return True
    if detected.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    elif detected.tzinfo is not None and current.tzinfo is None:
        detected = detected.replace(tzinfo=None)
    age = (current - detected).total_seconds()
    return 0 <= age <= max(1.0, float(max_age_seconds or _SHORT_PENDING_SIGNAL_MAX_AGE_SECONDS))


def _short_pending_semantic_text(raw_text: str, *, target_name: str = "") -> tuple[str, dict[str, Any]]:
    """Return body-only text for monitor-owned short unread previews."""
    text = str(raw_text or "").strip()
    split = split_wechat_ocr_speaker_prefix(
        text,
        conversation_type="group",
        target_name=target_name,
        known_speakers=[],
        allow_unlisted_name_like_prefix=True,
    )
    if split.get("changed"):
        cleaned = str(split.get("content") or "").strip()
        if cleaned:
            return cleaned, {
                "speaker_name": str(split.get("speaker_name") or "").strip(),
                "original_content": text,
                "reason": str(split.get("reason") or "monitor_short_speaker_prefix"),
            }
    return text, {}


def recover_high_sensitivity_short_pending_batch(
    messages: list[dict[str, Any]],
    pending_signal: dict[str, Any] | None,
    *,
    target_name: str = "",
    allow_self_for_test: bool = False,
    max_batch_messages: int = 1,
    now: str | datetime | None = None,
    max_signal_age_seconds: float = _SHORT_PENDING_SIGNAL_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    """Recover a short unread probe when an anchor boundary misclassifies it as old history.

    RPA/OCR can see a session-list preview like "在吗" while the chat-pane anchor
    still reports "no messages after anchor", especially when the same short
    phrase appeared before bootstrap.  Only the monitor-owned high-sensitivity
    short signal may enter this narrow recovery path.
    """

    if not isinstance(pending_signal, dict):
        return []
    if str(pending_signal.get("pending_signal_kind") or "").strip() != _SHORT_PENDING_SIGNAL_KIND:
        return []
    pending_text = str(pending_signal.get("pending_signal_text") or pending_signal.get("preview_content") or "").strip()
    semantic_pending_text, speaker_meta = _short_pending_semantic_text(pending_text, target_name=target_name)
    pending_compact = normalize_repeatable_probe_text(semantic_pending_text)
    if short_pending_text_is_media_only(semantic_pending_text):
        return []
    if not pending_compact or len(pending_compact) > 7:
        return []
    detected_at = str(
        pending_signal.get("pending_since")
        or pending_signal.get("last_detected_at")
        or pending_signal.get("last_message_time")
        or ""
    )
    if not _short_pending_signal_is_fresh(
        detected_at,
        now=now,
        max_age_seconds=max_signal_age_seconds,
    ):
        return []
    recovered: list[dict[str, Any]] = []
    for item in reversed(messages or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "text").strip().lower() not in {"", "text"}:
            continue
        sender = str(item.get("sender") or "").strip().lower()
        if sender in _SELF_MESSAGE_SENDERS and not allow_self_for_test:
            continue
        content = str(item.get("content") or "").strip()
        compact = normalize_repeatable_probe_text(content)
        if not compact:
            continue
        suffix_match = compact.endswith(pending_compact) and len(compact) - len(pending_compact) <= 4
        if compact != pending_compact and not suffix_match:
            continue
        cloned = copy.deepcopy(item)
        original_id = str(cloned.get("id") or "").strip()
        synthetic_id = stable_id(
            "short-pending-signal",
            target_name,
            original_id,
            pending_compact,
            detected_at,
        )
        cloned["id"] = f"short_pending:{synthetic_id}"
        cloned["original_message_id"] = original_id
        cloned["short_pending_recovered"] = True
        cloned["pending_signal_text"] = pending_text
        cloned["pending_signal_kind"] = _SHORT_PENDING_SIGNAL_KIND
        if speaker_meta:
            cloned["speaker_name"] = speaker_meta.get("speaker_name")
            cloned["group_member_name"] = speaker_meta.get("speaker_name")
            cloned["original_content"] = speaker_meta.get("original_content")
            cloned["ocr_speaker_prefix"] = speaker_meta
        if suffix_match and compact != pending_compact:
            cloned["content"] = semantic_pending_text
        recovered.append(cloned)
        if len(recovered) >= max(1, int(max_batch_messages or 1)):
            break
    if recovered:
        return list(reversed(recovered))

    synthetic_id = stable_id(
        "short-pending-signal",
        target_name,
        "monitor-only",
        pending_compact,
        detected_at,
    )
    synthetic: dict[str, Any] = {
        "id": f"short_pending:{synthetic_id}",
        "type": "text",
        "sender": "unknown",
        "sender_role": "unknown",
        "content": semantic_pending_text,
        "time": detected_at,
        "short_pending_recovered": True,
        "short_pending_synthesized_from_monitor": True,
        "pending_signal_text": pending_text,
        "pending_signal_kind": _SHORT_PENDING_SIGNAL_KIND,
    }
    if speaker_meta:
        synthetic["speaker_name"] = speaker_meta.get("speaker_name")
        synthetic["group_member_name"] = speaker_meta.get("speaker_name")
        synthetic["original_content"] = speaker_meta.get("original_content")
        synthetic["ocr_speaker_prefix"] = speaker_meta
    return [synthetic]


def recover_pending_signal_batch_from_monitor(
    messages: list[dict[str, Any]],
    pending_signal: dict[str, Any] | None,
    *,
    target_name: str = "",
    allow_self_for_test: bool = False,
    max_batch_messages: int = 1,
    now: str | datetime | None = None,
    max_signal_age_seconds: float = 300.0,
) -> list[dict[str, Any]]:
    """Recover current unread preview text when chat-pane OCR/anchors are empty.

    This is a code-mechanism recovery path only: it converts a session-list
    unread preview into Brain input so an already-read customer turn is not
    swallowed when images/manual work/history gaps push the anchor off screen.
    It never authors customer-visible wording.
    """

    recovered_short = recover_high_sensitivity_short_pending_batch(
        messages,
        pending_signal,
        target_name=target_name,
        allow_self_for_test=allow_self_for_test,
        max_batch_messages=max_batch_messages,
        now=now,
        max_signal_age_seconds=max_signal_age_seconds,
    )
    if recovered_short:
        return recovered_short
    if not isinstance(pending_signal, dict):
        return []
    has_pending_evidence = bool(
        pending_signal.get("unread_detected")
        or pending_signal.get("pending")
        or pending_signal.get("pending_since")
        or pending_signal.get("last_detected_at")
        or pending_signal.get("unread_badge")
    )
    if not has_pending_evidence:
        return []
    pending_text = str(pending_signal.get("pending_signal_text") or pending_signal.get("preview_content") or "").strip()
    semantic_pending_text, speaker_meta = _short_pending_semantic_text(pending_text, target_name=target_name)
    compact = normalize_repeatable_probe_text(semantic_pending_text)
    if not compact or short_pending_text_is_media_only(semantic_pending_text):
        return []
    target_compact = normalize_repeatable_probe_text(target_name)
    speaker_compact = normalize_repeatable_probe_text(speaker_meta.get("speaker_name") if speaker_meta else "")
    if compact and (compact == target_compact or (speaker_compact and compact == speaker_compact)):
        return []
    detected_at = str(
        pending_signal.get("pending_since")
        or pending_signal.get("last_detected_at")
        or pending_signal.get("last_message_time")
        or ""
    )
    if detected_at and not _short_pending_signal_is_fresh(
        detected_at,
        now=now,
        max_age_seconds=max_signal_age_seconds,
    ) and not bool(pending_signal.get("unread_detected")):
        return []
    synthetic_id = stable_id(
        "monitor-pending-signal",
        target_name,
        compact,
        detected_at,
        str(pending_signal.get("session_key") or ""),
    )
    synthetic: dict[str, Any] = {
        "id": f"monitor_pending:{synthetic_id}",
        "type": "text",
        "sender": "unknown",
        "sender_role": "unknown",
        "content": semantic_pending_text,
        "time": detected_at,
        "monitor_pending_recovered": True,
        "monitor_pending_synthesized_from_preview": True,
        "pending_signal_text": pending_text,
        "pending_signal_kind": str(pending_signal.get("pending_signal_kind") or "normal"),
    }
    if speaker_meta:
        synthetic["speaker_name"] = speaker_meta.get("speaker_name")
        synthetic["group_member_name"] = speaker_meta.get("speaker_name")
        synthetic["original_content"] = speaker_meta.get("original_content")
        synthetic["ocr_speaker_prefix"] = speaker_meta
    return [synthetic]


def pending_signal_identity_payload(
    pending_signal: dict[str, Any] | None,
    *,
    target_name: str = "",
    session_key: str = "",
) -> dict[str, Any]:
    """Return occurrence metadata from scheduler/session-list monitoring.

    This is code-mechanism metadata only.  It helps distinguish a new customer
    turn from an old OCR bubble with the same text/layout; it must never enter
    customer-visible wording.
    """

    if not isinstance(pending_signal, dict):
        return {}
    pending_since = str(pending_signal.get("pending_since") or "").strip()
    last_detected_at = str(pending_signal.get("last_detected_at") or "").strip()
    last_message_time = str(pending_signal.get("last_message_time") or pending_signal.get("time") or "").strip()
    pending_text = str(pending_signal.get("pending_signal_text") or pending_signal.get("preview_content") or "").strip()
    unread_badge = str(pending_signal.get("last_unread_badge") or pending_signal.get("unread_badge") or "").strip()
    unread_detected = bool(pending_signal.get("unread_detected") or pending_signal.get("pending") or unread_badge)
    if not any([pending_since, last_detected_at, last_message_time, pending_text, unread_badge, unread_detected]):
        return {}
    clean_session_key = str(session_key or pending_signal.get("session_key") or "").strip()
    signal_id = stable_id(
        "pending-signal",
        clean_session_key,
        target_name,
        pending_since,
        last_detected_at,
        last_message_time,
        pending_text,
        unread_badge,
        bool(unread_detected),
    )
    return {
        "pending_signal_id": signal_id,
        "pending_since": pending_since,
        "last_detected_at": last_detected_at,
        "last_message_time": last_message_time,
        "pending_signal_text": pending_text,
        "pending_signal_kind": str(pending_signal.get("pending_signal_kind") or "normal"),
        "pending_signal_has_unread_evidence": bool(unread_detected),
    }


def _pending_signal_matches_message(content: str, pending_text: str) -> bool:
    compact_content = normalize_repeatable_probe_text(content)
    compact_pending = normalize_repeatable_probe_text(pending_text)
    if not compact_pending:
        return True
    if not compact_content:
        return False
    if compact_pending in compact_content or compact_content in compact_pending:
        return True
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,14}", compact_pending)
    return any(token and token in compact_content for token in tokens)


def annotate_latest_customer_messages_with_pending_signal(
    messages: list[dict[str, Any]],
    pending_signal: dict[str, Any] | None,
    *,
    target_name: str = "",
    conversation_type: str = "unknown",
    session_key: str = "",
    allow_self_for_test: bool = False,
    max_messages: int = 1,
) -> list[dict[str, Any]]:
    """Attach occurrence metadata to the newest customer text candidates.

    The metadata is intentionally narrow: a pending unread/session-list signal
    should distinguish the newest likely customer input, not rewrite the whole
    visible history.  Content remains unchanged and Brain still sees the same
    customer text.
    """

    signal = pending_signal_identity_payload(pending_signal, target_name=target_name, session_key=session_key)
    if not signal:
        return list(messages or [])
    pending_text = str(signal.get("pending_signal_text") or "").strip()
    next_messages = [copy.deepcopy(item) for item in (messages or []) if isinstance(item, dict)]
    candidate_indexes: list[int] = []
    for index in range(len(next_messages) - 1, -1, -1):
        item = next_messages[index]
        msg_type = str(item.get("type") or "text").strip().lower() or "text"
        if msg_type != "text":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        sender = str(item.get("sender") or "").strip().lower()
        if sender in _SELF_MESSAGE_SENDERS and not allow_self_for_test:
            continue
        if pending_text and not _pending_signal_matches_message(content, pending_text):
            continue
        candidate_indexes.append(index)
        if len(candidate_indexes) >= max(1, int(max_messages or 1)):
            break
    if not candidate_indexes and pending_text:
        for index in range(len(next_messages) - 1, -1, -1):
            item = next_messages[index]
            msg_type = str(item.get("type") or "text").strip().lower() or "text"
            sender = str(item.get("sender") or "").strip().lower()
            if msg_type == "text" and str(item.get("content") or "").strip() and (allow_self_for_test or sender not in _SELF_MESSAGE_SENDERS):
                candidate_indexes.append(index)
                break
    for index in candidate_indexes:
        item = dict(next_messages[index])
        item.update({key: value for key, value in signal.items() if value not in ("", None)})
        item["_canonical_input_signal_applied"] = True
        item = apply_canonical_identity_fields(
            item,
            target_name=target_name,
            conversation_type=conversation_type,
            force_recompute=True,
        )
        next_messages[index] = item
    return next_messages


def short_pending_text_is_media_only(text: str) -> bool:
    compact = normalize_repeatable_probe_text(text)
    return compact in {
        "[图片]",
        "图片",
        "[表情]",
        "表情",
        "[视频]",
        "视频",
        "[语音]",
        "语音",
        "[文件]",
        "文件",
    }


def _apply_rpa_humanized_send_runtime_env(config: dict[str, Any]) -> None:
    settings = config.get("rpa_humanized_send") if isinstance(config.get("rpa_humanized_send"), dict) else {}
    if not isinstance(settings, dict):
        settings = {}
    for env_name, key in _RPA_HUMANIZED_SEND_ENV_MAPPING.items():
        if key not in settings and key != "fast_send_confirmation_enabled":
            continue
        value = settings.get(key)
        if value is None and key == "fast_send_confirmation_enabled":
            value = True
        if isinstance(value, bool):
            os.environ[env_name] = "1" if value else "0"
        elif value is not None:
            os.environ[env_name] = str(value)


def default_freshness_ok(reply: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "stale": False, "reply_id": reply.get("reply_id")}


def brain_first_ready_reply_ownership_failure(reply: dict[str, Any]) -> str:
    decision = reply.get("decision") if isinstance(reply.get("decision"), dict) else {}
    if not bool(decision.get("brain_first_visible_reply_required")):
        return ""
    rule_name = str(decision.get("rule_name") or "").strip()
    if rule_name.startswith("customer_service_brain_") and rule_name not in {
        "customer_service_brain_no_visible_reply",
        "customer_service_brain_safe_fallback",
    }:
        return ""
    return "brain_first_non_brain_owned_ready_reply_blocked"


def ready_reply_session_envelope_failure(reply: dict[str, Any], capture: dict[str, Any] | None) -> str:
    """Validate that a ready reply still belongs to the captured session/input."""

    if not isinstance(capture, dict):
        return "capture_missing_before_send"
    reply_target = str(reply.get("target_name") or "").strip()
    capture_target = str(capture.get("target_name") or "").strip()
    if not reply_target or not capture_target or reply_target != capture_target:
        return "reply_target_capture_mismatch"
    reply_session_key = str(reply.get("session_key") or "").strip()
    capture_session_key = str(capture.get("session_key") or "").strip()
    if reply_session_key and capture_session_key and reply_session_key != capture_session_key:
        return "reply_session_key_capture_mismatch"
    reply_type = str(reply.get("conversation_type") or "").strip()
    capture_type = str(capture.get("conversation_type") or "").strip()
    known_types = {"private", "group", "file_transfer", "system"}
    if reply_type in known_types and capture_type in known_types and reply_type != capture_type:
        return "reply_conversation_type_capture_mismatch"
    reply_capture_ids = [str(item) for item in reply.get("capture_ids", []) if str(item)]
    capture_id = str(capture.get("capture_id") or "").strip()
    if reply_capture_ids and capture_id and reply_capture_ids[-1] != capture_id:
        return "reply_capture_id_mismatch"
    reply_context_version = int(reply.get("input_context_version") or 0)
    capture_context_version = int(capture.get("context_version") or 0)
    if reply_context_version and capture_context_version and reply_context_version != capture_context_version:
        return "reply_context_version_capture_mismatch"
    reply_ids = {str(item) for item in reply.get("input_message_ids", []) if str(item)}
    capture_ids = {str(item) for item in capture.get("message_ids", []) if str(item)}
    if reply_ids and capture_ids and not reply_ids <= capture_ids:
        return "reply_message_ids_not_in_capture"
    reply_keys = {str(item) for item in reply.get("input_content_keys", []) if str(item)}
    capture_keys = {str(item) for item in capture.get("content_keys", []) if str(item)}
    if reply_keys and capture_keys and not reply_keys <= capture_keys:
        return "reply_content_keys_not_in_capture"
    reply_digest = str(reply.get("message_content_digest") or "").strip()
    capture_digest = str(capture.get("message_content_digest") or "").strip()
    if not capture_digest:
        capture_digest = message_content_digest(list(capture_ids), list(capture_keys))
    if not reply_digest and (reply_ids or reply_keys):
        reply_digest = message_content_digest(list(reply_ids), list(reply_keys))
    if reply_digest and capture_digest and reply_digest != capture_digest:
        return "reply_message_content_digest_mismatch"
    if (reply_ids or reply_keys or reply_digest) and not (capture_ids or capture_keys or capture_digest):
        return "reply_input_not_confirmed_by_capture"
    return ""


class CustomerServiceSchedulerRuntime:
    """Small persistent scheduler runtime with async LLM futures."""

    def __init__(
        self,
        *,
        store: SchedulerStateStore,
        config: SchedulerConfig,
        capture_fn: CaptureFn,
        plan_reply_fn: PlanReplyFn,
        polish_reply_fn: PolishReplyFn | None = None,
        freshness_fn: FreshnessFn | None = None,
        send_fn: SendFn | None = None,
        capture_done_fn: CaptureDoneFn | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.capture_fn = capture_fn
        self.plan_reply_fn = plan_reply_fn
        self.polish_reply_fn = polish_reply_fn
        self.freshness_fn = freshness_fn or default_freshness_ok
        self.send_fn = send_fn
        self.capture_done_fn = capture_done_fn
        self._planner_executor = ThreadPoolExecutor(max_workers=max(1, int(config.planner_max_concurrency or config.llm_max_concurrency)))
        self._planner_futures: dict[str, Future[dict[str, Any]]] = {}
        self._planner_task_snapshots: dict[str, dict[str, Any]] = {}
        self._polish_executor = ThreadPoolExecutor(max_workers=max(1, int(config.polish_max_concurrency or config.llm_max_concurrency)))
        self._polish_futures: dict[str, Future[dict[str, Any]]] = {}
        self._polish_task_snapshots: dict[str, dict[str, Any]] = {}

    def shutdown(self) -> None:
        self._planner_executor.shutdown(wait=False, cancel_futures=False)
        self._polish_executor.shutdown(wait=False, cancel_futures=False)

    def _restore_missing_polish_task_from_snapshot(self, state: dict[str, Any], task_id: str) -> bool:
        task = (state.get("polish_tasks", {}) or {}).get(task_id)
        if isinstance(task, dict):
            return True
        snapshot = self._polish_task_snapshots.get(task_id)
        if not isinstance(snapshot, dict):
            return False
        state.setdefault("polish_tasks", {})[task_id] = copy.deepcopy(snapshot)
        return True

    def _restore_missing_llm_task_from_snapshot(self, state: dict[str, Any], task_id: str) -> bool:
        task = (state.get("llm_tasks", {}) or {}).get(task_id)
        if isinstance(task, dict):
            return True
        snapshot = self._planner_task_snapshots.get(task_id)
        if not isinstance(snapshot, dict):
            return False
        state.setdefault("llm_tasks", {})[task_id] = copy.deepcopy(snapshot)
        return True

    def _task_exceeded_runtime_budget(self, task: dict[str, Any] | None, *, now: str | None = None) -> bool:
        if not isinstance(task, dict):
            return False
        started_at = _parse_iso_datetime(task.get("started_at"))
        now_dt = _parse_iso_datetime(now or datetime.now().isoformat(timespec="seconds"))
        if started_at is None or now_dt is None:
            return False
        timeout_seconds = max(1.0, float(task.get("timeout_seconds") or 30))
        grace_seconds = max(1.0, float(os.getenv("WECHAT_CUSTOMER_SERVICE_LLM_TIMEOUT_GRACE_SECONDS", "6") or 6))
        return (now_dt - started_at).total_seconds() > timeout_seconds + grace_seconds

    def _run_planner_future(self, capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="seconds")
        result = self.plan_reply_fn(capture, task)
        finished_at = datetime.now().isoformat(timespec="seconds")
        if isinstance(result, dict):
            return _merge_result_latency_trace(
                result,
                {
                    "planner_worker_started_at": started_at,
                    "planner_worker_finished_at": finished_at,
                    "planner_worker_duration_seconds": round(max(0.0, time.perf_counter() - started), 4),
                },
            )
        return result

    def _run_polish_future(self, planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="seconds")
        result = self.polish_reply_fn(planner_task, task) if self.polish_reply_fn is not None else {}
        finished_at = datetime.now().isoformat(timespec="seconds")
        if isinstance(result, dict):
            return _merge_result_latency_trace(
                result,
                {
                    "polish_worker_started_at": started_at,
                    "polish_worker_finished_at": finished_at,
                    "polish_worker_duration_seconds": round(max(0.0, time.perf_counter() - started), 4),
                },
            )
        return result

    def tick(
        self,
        *,
        session_signals: list[dict[str, Any]] | None = None,
        allow_send: bool = False,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Run one non-blocking scheduling tick."""
        started = time.perf_counter()
        phase_durations: dict[str, float] = {}
        phase_started = started
        state = self.store.load()
        events: list[dict[str, Any]] = []
        cleanup_result = cleanup_scheduler_state(state, config=self.config, now=now)
        if int(cleanup_result.get("removed_ready_reply_count") or 0) > 0:
            events.append({"event": "state_cleanup", **cleanup_result})

        for signal in session_signals or []:
            session = record_session_signal(state, signal, now=now)
            if session and session.get("pending_capture"):
                events.append({"event": "signal_pending", "target_name": session.get("target_name")})

        recovered = recover_orphaned_running_llm_tasks(state, active_task_ids=set(self._planner_futures), now=now)
        for task in recovered:
            events.append({"event": "llm_task_orphan_requeued", "task_id": task.get("task_id"), "target_name": task.get("target_name")})
        if self.polish_reply_fn is not None:
            recovered_polish = recover_orphaned_running_polish_tasks(state, active_task_ids=set(self._polish_futures), now=now)
            for task in recovered_polish:
                events.append({"event": "polish_task_orphan_requeued", "task_id": task.get("task_id"), "target_name": task.get("target_name")})

        pre_sent = self._consume_send_queue(state, allow_send=allow_send, now=now)
        phase_durations["send_pre_seconds"] = round(max(0.0, time.perf_counter() - phase_started), 4)
        phase_started = time.perf_counter()
        events.extend(pre_sent)

        captured = self._capture_pending(state, now=now)
        phase_durations["capture_seconds"] = round(max(0.0, time.perf_counter() - phase_started), 4)
        phase_started = time.perf_counter()
        events.extend(captured)

        submitted = self._submit_llm_tasks(state, now=now)
        planner_submit_seconds = round(max(0.0, time.perf_counter() - phase_started), 4)
        phase_durations["planner_submit_seconds"] = planner_submit_seconds
        phase_started = time.perf_counter()
        events.extend(submitted)

        completed = self._collect_llm_results(state, now=now)
        planner_collect_seconds = round(max(0.0, time.perf_counter() - phase_started), 4)
        phase_durations["planner_collect_seconds"] = planner_collect_seconds
        phase_started = time.perf_counter()
        events.extend(completed)

        polish_submit_seconds = 0.0
        polish_collect_seconds = 0.0
        if self.polish_reply_fn is not None:
            polish_submitted = self._submit_polish_tasks(state, now=now)
            polish_submit_seconds = round(max(0.0, time.perf_counter() - phase_started), 4)
            phase_durations["polish_submit_seconds"] = polish_submit_seconds
            phase_started = time.perf_counter()
            events.extend(polish_submitted)

            polish_completed = self._collect_polish_results(state, now=now)
            polish_collect_seconds = round(max(0.0, time.perf_counter() - phase_started), 4)
            phase_durations["polish_collect_seconds"] = polish_collect_seconds
            phase_started = time.perf_counter()
            events.extend(polish_completed)
        else:
            phase_durations["polish_submit_seconds"] = 0.0
            phase_durations["polish_collect_seconds"] = 0.0

        phase_durations["llm_submit_seconds"] = round(planner_submit_seconds + polish_submit_seconds, 4)
        phase_durations["llm_collect_seconds"] = round(planner_collect_seconds + polish_collect_seconds, 4)

        sent = self._consume_send_queue(state, allow_send=allow_send, now=now)
        phase_durations["send_post_seconds"] = round(max(0.0, time.perf_counter() - phase_started), 4)
        phase_started = time.perf_counter()
        events.extend(sent)

        self.store.save(state)
        phase_durations["state_save_seconds"] = round(max(0.0, time.perf_counter() - phase_started), 4)
        summary = state_summary(state)
        total_seconds = round(max(0.0, time.perf_counter() - started), 4)
        phase_durations["total_seconds"] = total_seconds
        return {
            "ok": True,
            "duration_seconds": total_seconds,
            "phase_durations": phase_durations,
            "events": events,
            "summary": summary,
            "cleanup": cleanup_result,
        }

    def _capture_pending(self, state: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        sessions = select_capture_sessions(state, limit=self.config.capture_max_sessions_per_round)
        for session in sessions:
            target_name = str(session.get("target_name") or "")
            session_key = str(session.get("session_key") or "")
            mark_capture_started(state, target_name, session_key=session_key, now=now)
            try:
                result = self.capture_fn(copy.deepcopy(session))
            except Exception as exc:  # noqa: BLE001 - scheduler must keep other sessions moving
                mark_session_capture_failed(state, target_name, repr(exc), session_key=session_key, now=now)
                events.append({"event": "capture_failed", "target_name": target_name, "error": repr(exc)})
                continue
            if result.get("ok") is False and result.get("blocked"):
                mark_session_capture_failed(
                    state,
                    target_name,
                    str(result.get("reason") or "capture_blocked"),
                    session_key=session_key,
                    now=now,
                )
                events.append({"event": "capture_blocked", "target_name": target_name, "reason": result.get("reason")})
                continue
            messages = list(result.get("messages") or [])
            batch = list(result.get("batch") or messages)
            overflow = list(result.get("overflow_messages") or [])
            capture = record_capture_result(
                state,
                target_name,
                messages=messages,
                batch=batch,
                overflow_messages=overflow,
                history_backfill=result.get("history_backfill") if isinstance(result.get("history_backfill"), dict) else {},
                batch_selection=result.get("batch_selection") if isinstance(result.get("batch_selection"), dict) else {},
                exact=bool(session.get("exact", True)),
                conversation_type=str(session.get("conversation_type") or "unknown"),
                session_key=str(result.get("session_key") or session.get("session_key") or ""),
                now=now,
            )
            if self.capture_done_fn is not None:
                try:
                    self.capture_done_fn(copy.deepcopy(session), copy.deepcopy(result), copy.deepcopy(capture))
                except Exception as exc:  # noqa: BLE001
                    events.append({"event": "capture_done_callback_failed", "target_name": target_name, "error": repr(exc)})
            if capture.get("status") == "captured":
                task = enqueue_llm_task(
                    state,
                    str(capture.get("capture_id") or ""),
                    timeout_seconds=int(self.config.planner_task_timeout_seconds),
                    now=now,
                )
                events.append({"event": "capture_completed", "target_name": target_name, "capture_id": capture["capture_id"], "task_id": task["task_id"]})
            else:
                events.append({"event": "capture_empty", "target_name": target_name, "capture_id": capture["capture_id"]})
        return events

    def _submit_llm_tasks(self, state: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        running_count = sum(1 for future in self._planner_futures.values() if not future.done())
        capacity = max(0, int(self.config.planner_max_concurrency or self.config.llm_max_concurrency) - running_count)
        if capacity <= 0:
            return events
        tasks = [
            task
            for task in (state.get("llm_tasks", {}) or {}).values()
            if isinstance(task, dict)
            and task.get("status") == "queued"
            and str(task.get("task_id") or "") not in self._planner_futures
            and (
                not str(task.get("retry_not_before") or "").strip()
                or (
                    _parse_iso_datetime(task.get("retry_not_before")) is not None
                    and _parse_iso_datetime(now or datetime.now().isoformat(timespec="seconds")) is not None
                    and _parse_iso_datetime(task.get("retry_not_before"))
                    <= _parse_iso_datetime(now or datetime.now().isoformat(timespec="seconds"))
                )
            )
        ]
        tasks.sort(key=lambda item: str(item.get("created_at") or ""))
        captures = state.get("captures", {}) or {}
        for task in tasks[:capacity]:
            task_id = str(task.get("task_id") or "")
            capture_ids = [str(item) for item in task.get("capture_ids", []) if str(item)]
            capture = captures.get(capture_ids[-1] if capture_ids else "")
            if not isinstance(capture, dict):
                fail_llm_task(state, task_id, reason="capture_missing", now=now)
                events.append({"event": "llm_task_failed", "task_id": task_id, "reason": "capture_missing"})
                continue
            mark_llm_started(state, task_id, now=now)
            self._planner_task_snapshots[task_id] = copy.deepcopy(task)
            planner_future_submitted_at = datetime.now().isoformat(timespec="seconds")
            live_task = (state.get("llm_tasks", {}) or {}).get(task_id)
            if isinstance(live_task, dict):
                trace = live_task.get("latency_trace") if isinstance(live_task.get("latency_trace"), dict) else {}
                live_task["latency_trace"] = {**trace, "planner_future_submitted_at": planner_future_submitted_at}
            self._planner_futures[task_id] = self._planner_executor.submit(self._run_planner_future, copy.deepcopy(capture), copy.deepcopy(task))
            events.append({"event": "llm_task_submitted", "task_id": task_id, "target_name": task.get("target_name")})
        return events

    def _collect_llm_results(self, state: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for task_id, future in list(self._planner_futures.items()):
            if not future.done():
                task = (state.get("llm_tasks", {}) or {}).get(task_id)
                if self._task_exceeded_runtime_budget(task if isinstance(task, dict) else self._planner_task_snapshots.get(task_id), now=now):
                    self._planner_futures.pop(task_id, None)
                    future.cancel()
                    if not self._restore_missing_llm_task_from_snapshot(state, task_id):
                        events.append({"event": "llm_task_failed", "task_id": task_id, "reason": "llm_task_timeout_missing_snapshot"})
                        continue
                    recovered_task = self._fail_llm_task_with_recovery(
                        state,
                        task_id,
                        reason="llm_task_runtime_timeout",
                        now=now,
                        events=events,
                    )
                    self._planner_task_snapshots.pop(task_id, None)
                    events.append(
                        {
                            "event": "llm_task_timeout_recovered",
                            "task_id": task_id,
                            "target_name": recovered_task.get("target_name"),
                        }
                    )
                continue
            self._planner_futures.pop(task_id, None)
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                error = repr(exc)
                trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
                if not self._restore_missing_llm_task_from_snapshot(state, task_id):
                    events.append(
                        {
                            "event": "llm_task_failed",
                            "task_id": task_id,
                            "error": error,
                            "traceback": trace,
                            "reason": "llm_task_missing_after_future",
                        }
                    )
                    continue
                task = self._fail_llm_task_with_recovery(state, task_id, reason=error, now=now, events=events)
                task["traceback"] = trace
                self._planner_task_snapshots.pop(task_id, None)
                events.append({"event": "llm_task_failed", "task_id": task_id, "error": error, "traceback": trace})
                continue
            planner_future_finished_at = datetime.now().isoformat(timespec="seconds")
            if isinstance(result, dict):
                result = _merge_result_latency_trace(
                    result,
                    {
                        "planner_future_finished_at": planner_future_finished_at,
                    },
                )
            if result.get("ok") is False:
                failure_reason = str(result.get("reason") or result.get("error") or "planner_failed")
                if not self._restore_missing_llm_task_from_snapshot(state, task_id):
                    events.append(
                        {
                            "event": "llm_task_failed",
                            "task_id": task_id,
                            "reason": "llm_task_missing_after_future",
                            "error": failure_reason,
                        }
                    )
                    continue
                self._fail_llm_task_with_recovery(
                    state,
                    task_id,
                    reason=failure_reason,
                    now=now,
                    events=events,
                    result_payload=result if isinstance(result, dict) else None,
                )
                self._planner_task_snapshots.pop(task_id, None)
                events.append({"event": "llm_task_failed", "task_id": task_id, "reason": result.get("reason")})
                continue
            reply_text = str(result.get("reply_text") or "").strip()
            if not reply_text:
                if not self._restore_missing_llm_task_from_snapshot(state, task_id):
                    events.append({"event": "llm_task_failed", "task_id": task_id, "reason": "llm_task_missing_after_future"})
                    continue
                self._fail_llm_task_with_recovery(
                    state,
                    task_id,
                    reason="empty_planned_reply",
                    now=now,
                    events=events,
                    result_payload=result if isinstance(result, dict) else None,
                )
                self._planner_task_snapshots.pop(task_id, None)
                events.append({"event": "llm_task_failed", "task_id": task_id, "reason": "empty_reply_text"})
                continue
            if not self._restore_missing_llm_task_from_snapshot(state, task_id):
                events.append({"event": "llm_task_failed", "task_id": task_id, "reason": "llm_task_missing_after_future"})
                continue
            context_update = result.get("conversation_context_update") if isinstance(result.get("conversation_context_update"), dict) else {}
            if context_update:
                source_task = (state.get("llm_tasks", {}) or {}).get(task_id, {})
                target_name = str(result.get("target_name") or (source_task if isinstance(source_task, dict) else {}).get("target_name") or "")
                merge_scheduler_conversation_context(
                    state,
                    target_name,
                    context_update,
                    session_key=str((source_task if isinstance(source_task, dict) else {}).get("session_key") or ""),
                    now=now,
                )
                events.append({"event": "conversation_context_updated", "task_id": task_id, "target_name": target_name, "product_id": context_update.get("last_product_id")})
            completion = complete_llm_task(
                state,
                task_id,
                reply_text=reply_text,
                decision=result.get("decision") if isinstance(result.get("decision"), dict) else {},
                result_payload=result if isinstance(result, dict) else None,
                create_ready_reply=self.polish_reply_fn is None,
                now=now,
            )
            if completion.get("status") == "completed" and self.polish_reply_fn is not None:
                try:
                    polish_task = enqueue_polish_task(
                        state,
                        task_id,
                        timeout_seconds=int(self.config.polish_task_timeout_seconds),
                        now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    fail_llm_task(state, task_id, reason=f"polish_enqueue_failed:{exc!r}", now=now)
                    self._planner_task_snapshots.pop(task_id, None)
                    events.append({"event": "polish_task_enqueue_failed", "task_id": task_id, "error": repr(exc)})
                    continue
                self._planner_task_snapshots.pop(task_id, None)
                events.append({"event": "llm_task_completed", "task_id": task_id, "status": completion.get("status"), "polish_task_id": polish_task.get("task_id")})
                continue
            self._planner_task_snapshots.pop(task_id, None)
            events.append({"event": "llm_task_completed", "task_id": task_id, "status": completion.get("status")})
        return events

    def _fail_llm_task_with_recovery(
        self,
        state: dict[str, Any],
        task_id: str,
        *,
        reason: str,
        now: str | None,
        events: list[dict[str, Any]],
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = fail_llm_task(state, task_id, reason=reason, now=now, result_payload=result_payload)
        recovery = requeue_capture_after_recoverable_llm_failure(state, task_id, reason=reason, now=now)
        if recovery.get("ok"):
            event_name = str(recovery.get("event") or "llm_task_failed_requeued_capture")
            events.append(
                {
                    "event": event_name,
                    "task_id": task_id,
                    "target_name": task.get("target_name"),
                    "reason": reason,
                    "recovery": recovery,
                }
            )
        return task

    def _fail_planner_task_from_polish_with_recovery(
        self,
        state: dict[str, Any],
        planner_task_id: str,
        polish_task_id: str,
        *,
        reason: str,
        now: str | None,
        events: list[dict[str, Any]],
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Route final-polish blocks back to Brain without authoring fallback text."""

        planner_task = (state.get("llm_tasks", {}) or {}).get(planner_task_id)
        if not isinstance(planner_task, dict):
            events.append(
                {
                    "event": "polish_recovery_planner_missing",
                    "task_id": polish_task_id,
                    "planner_task_id": planner_task_id,
                    "reason": reason,
                }
            )
            return None
        previous_result = planner_task.get("result") if isinstance(planner_task.get("result"), dict) else {}
        planner_task["last_failed_result"] = safe_json_roundtrip(previous_result)
        planner_task["last_polish_failure"] = safe_json_roundtrip(result_payload or {"ok": False, "reason": reason})
        return self._fail_llm_task_with_recovery(
            state,
            planner_task_id,
            reason=reason,
            now=now,
            events=events,
            result_payload=result_payload,
        )

    def _submit_polish_tasks(self, state: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self.polish_reply_fn is None:
            return events
        running_count = sum(1 for future in self._polish_futures.values() if not future.done())
        capacity = max(0, int(self.config.polish_max_concurrency or self.config.llm_max_concurrency) - running_count)
        if capacity <= 0:
            return events
        tasks = [
            task
            for task in (state.get("polish_tasks", {}) or {}).values()
            if isinstance(task, dict)
            and task.get("status") == "queued"
            and str(task.get("task_id") or "") not in self._polish_futures
        ]
        tasks.sort(key=lambda item: str(item.get("created_at") or ""))
        planner_tasks = state.get("llm_tasks", {}) or {}
        for task in tasks[:capacity]:
            task_id = str(task.get("task_id") or "")
            planner_task_id = str(task.get("planner_task_id") or "")
            planner_task = planner_tasks.get(planner_task_id)
            if not isinstance(planner_task, dict):
                fail_polish_task(state, task_id, reason="planner_task_missing", now=now)
                events.append({"event": "polish_task_failed", "task_id": task_id, "reason": "planner_task_missing"})
                continue
            mark_polish_started(state, task_id, now=now)
            self._polish_task_snapshots[task_id] = copy.deepcopy(task)
            polish_future_submitted_at = datetime.now().isoformat(timespec="seconds")
            live_task = (state.get("polish_tasks", {}) or {}).get(task_id)
            if isinstance(live_task, dict):
                trace = live_task.get("latency_trace") if isinstance(live_task.get("latency_trace"), dict) else {}
                live_task["latency_trace"] = {**trace, "polish_future_submitted_at": polish_future_submitted_at}
            self._polish_futures[task_id] = self._polish_executor.submit(self._run_polish_future, copy.deepcopy(planner_task), copy.deepcopy(task))
            events.append({"event": "polish_task_submitted", "task_id": task_id, "target_name": task.get("target_name")})
        return events

    def _collect_polish_results(self, state: dict[str, Any], *, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for task_id, future in list(self._polish_futures.items()):
            if not future.done():
                task = (state.get("polish_tasks", {}) or {}).get(task_id)
                if self._task_exceeded_runtime_budget(task if isinstance(task, dict) else self._polish_task_snapshots.get(task_id), now=now):
                    self._polish_futures.pop(task_id, None)
                    future.cancel()
                    if not self._restore_missing_polish_task_from_snapshot(state, task_id):
                        events.append({"event": "polish_task_failed", "task_id": task_id, "reason": "polish_task_timeout_missing_snapshot"})
                        continue
                    task = fail_polish_task(state, task_id, reason="polish_task_runtime_timeout", now=now)
                    self._polish_task_snapshots.pop(task_id, None)
                    events.append(
                        {
                            "event": "polish_task_timeout_failed",
                            "task_id": task_id,
                            "target_name": task.get("target_name"),
                        }
                    )
                continue
            self._polish_futures.pop(task_id, None)
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                error = repr(exc)
                trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
                if not self._restore_missing_polish_task_from_snapshot(state, task_id):
                    events.append(
                        {
                            "event": "polish_task_failed",
                            "task_id": task_id,
                            "error": error,
                            "traceback": trace,
                            "reason": "polish_task_missing_after_future",
                        }
                    )
                    continue
                task = fail_polish_task(state, task_id, reason=error, now=now)
                task["traceback"] = trace
                self._polish_task_snapshots.pop(task_id, None)
                events.append({"event": "polish_task_failed", "task_id": task_id, "error": error, "traceback": trace})
                continue
            polish_future_finished_at = datetime.now().isoformat(timespec="seconds")
            if isinstance(result, dict):
                result = _merge_result_latency_trace(
                    result,
                    {
                        "polish_future_finished_at": polish_future_finished_at,
                    },
                )
            if result.get("ok") is False:
                if not self._restore_missing_polish_task_from_snapshot(state, task_id):
                    events.append(
                        {
                            "event": "polish_task_failed",
                            "task_id": task_id,
                            "reason": "polish_task_missing_after_future",
                            "error": str(result.get("reason") or result.get("error") or "polish_failed"),
                        }
                    )
                    continue
                failure_reason = str(result.get("reason") or result.get("error") or "polish_failed")
                planner_task_id = str(((state.get("polish_tasks", {}) or {}).get(task_id) or {}).get("planner_task_id") or "")
                task = fail_polish_task(state, task_id, reason=failure_reason, now=now)
                task["result"] = safe_json_roundtrip(result)
                if failure_reason == "final_visible_llm_polish_failed" and planner_task_id:
                    self._fail_planner_task_from_polish_with_recovery(
                        state,
                        planner_task_id,
                        task_id,
                        reason=failure_reason,
                        now=now,
                        events=events,
                        result_payload=result if isinstance(result, dict) else None,
                    )
                self._polish_task_snapshots.pop(task_id, None)
                events.append({"event": "polish_task_failed", "task_id": task_id, "reason": failure_reason})
                continue
            reply_text = str(result.get("reply_text") or "").strip()
            if not reply_text:
                if not self._restore_missing_polish_task_from_snapshot(state, task_id):
                    events.append({"event": "polish_task_failed", "task_id": task_id, "reason": "polish_task_missing_after_future"})
                    continue
                fail_polish_task(state, task_id, reason="empty_reply_text", now=now)
                self._polish_task_snapshots.pop(task_id, None)
                events.append({"event": "polish_task_failed", "task_id": task_id, "reason": "empty_reply_text"})
                continue
            if not self._restore_missing_polish_task_from_snapshot(state, task_id):
                events.append({"event": "polish_task_failed", "task_id": task_id, "reason": "polish_task_missing_after_future"})
                continue
            completion = complete_polish_task(
                state,
                task_id,
                reply_text=reply_text,
                decision=result.get("decision") if isinstance(result.get("decision"), dict) else {},
                result_payload=result,
                degraded=bool(result.get("degraded")),
                now=now,
            )
            self._polish_task_snapshots.pop(task_id, None)
            events.append({"event": "polish_task_completed", "task_id": task_id, "status": completion.get("status"), "degraded": bool(result.get("degraded"))})
        return events

    def _consume_send_queue(self, state: dict[str, Any], *, allow_send: bool, now: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not allow_send or self.send_fn is None:
            return events
        replies = select_ready_replies(state, limit=self.config.send_max_replies_per_round)
        for reply in replies:
            reply_id = str(reply.get("reply_id") or "")
            ownership_failure = brain_first_ready_reply_ownership_failure(reply)
            if ownership_failure:
                mark_reply_failed(state, reply_id, reason=ownership_failure, now=now)
                events.append(
                    {
                        "event": "send_blocked",
                        "reply_id": reply_id,
                        "target_name": reply.get("target_name"),
                        "reason": ownership_failure,
                    }
                )
                continue
            envelope_failure = ready_reply_session_envelope_failure(reply, self._capture_for_reply(state, reply))
            if envelope_failure:
                mark_reply_stale(state, reply_id, reason=envelope_failure, now=now)
                enqueue_pending_session(
                    state,
                    str(reply.get("target_name") or ""),
                    exact=True,
                    conversation_type=str(reply.get("conversation_type") or "unknown"),
                    session_key=str(reply.get("session_key") or ""),
                    reason="reply_session_envelope_failed_before_send",
                    now=now,
                )
                events.append(
                    {
                        "event": "reply_stale",
                        "reply_id": reply_id,
                        "target_name": reply.get("target_name"),
                        "reason": envelope_failure,
                    }
                )
                continue
            mark_reply_sending(state, reply_id, now=now)
            callback_reply = self._reply_for_callbacks(state, reply)
            freshness_started_at = datetime.now().isoformat(timespec="seconds")
            live_reply = (state.get("ready_replies", {}) or {}).get(reply_id)
            if isinstance(live_reply, dict):
                trace = live_reply.get("latency_trace") if isinstance(live_reply.get("latency_trace"), dict) else {}
                live_reply["latency_trace"] = {**trace, "freshness_check_started_at": freshness_started_at}
            freshness = self.freshness_fn(copy.deepcopy(callback_reply))
            freshness_finished_at = datetime.now().isoformat(timespec="seconds")
            live_reply = (state.get("ready_replies", {}) or {}).get(reply_id)
            if isinstance(live_reply, dict):
                trace = live_reply.get("latency_trace") if isinstance(live_reply.get("latency_trace"), dict) else {}
                live_reply["freshness_check"] = copy.deepcopy(freshness)
                live_reply["latency_trace"] = {**trace, "freshness_check_finished_at": freshness_finished_at}
            if freshness.get("stale") or freshness.get("has_newer_messages"):
                mark_reply_stale(state, reply_id, reason=str(freshness.get("reason") or "freshness_stale"), now=now)
                if self.config.stale_reply_policy == "discard_and_requeue":
                    self._record_stale_reply_context(state, reply, freshness=freshness, now=now)
                    enqueue_pending_session(
                        state,
                        str(reply.get("target_name") or ""),
                        exact=True,
                        conversation_type=str(reply.get("conversation_type") or "unknown"),
                        session_key=str(reply.get("session_key") or ""),
                        reason="reply_stale_before_send",
                        now=now,
                    )
                events.append({"event": "reply_stale", "reply_id": reply_id, "target_name": reply.get("target_name"), "freshness": freshness})
                continue
            try:
                send_rpa_started_at = datetime.now().isoformat(timespec="seconds")
                live_reply = (state.get("ready_replies", {}) or {}).get(reply_id)
                if isinstance(live_reply, dict):
                    trace = live_reply.get("latency_trace") if isinstance(live_reply.get("latency_trace"), dict) else {}
                    live_reply["latency_trace"] = {**trace, "send_rpa_started_at": send_rpa_started_at}
                send_result = self.send_fn(copy.deepcopy(callback_reply))
            except Exception as exc:  # noqa: BLE001
                mark_reply_failed(state, reply_id, reason=repr(exc), now=now)
                events.append(
                    {
                        "event": "send_failed",
                        "reply_id": reply_id,
                        "target_name": reply.get("target_name"),
                        "session_key": reply.get("session_key"),
                        "reason": repr(exc),
                        "error": repr(exc),
                    }
                )
                continue
            send_rpa_finished_at = datetime.now().isoformat(timespec="seconds")
            live_reply = (state.get("ready_replies", {}) or {}).get(reply_id)
            if isinstance(live_reply, dict):
                trace = live_reply.get("latency_trace") if isinstance(live_reply.get("latency_trace"), dict) else {}
                live_reply["latency_trace"] = {**trace, "send_rpa_finished_at": send_rpa_finished_at}
            send_observability = self._extract_send_observability(send_result)
            if send_result.get("ok") is False or send_result.get("verified") is False:
                failure_reason = str(send_result.get("reason") or send_result.get("error") or "send_failed")
                mark_reply_failed(state, reply_id, reason=failure_reason, send_result=send_result, now=now)
                failed_event = {
                    "event": "send_failed",
                    "reply_id": reply_id,
                    "target_name": reply.get("target_name"),
                    "session_key": reply.get("session_key"),
                    "reason": failure_reason,
                    "send_result": send_result,
                }
                if send_observability:
                    failed_event["send_observability"] = send_observability
                events.append(failed_event)
                continue
            mark_reply_sent(state, reply_id, send_result=send_result, now=now)
            sent_reply = (state.get("ready_replies", {}) or {}).get(reply_id)
            latency_trace = (
                sent_reply.get("latency_trace")
                if isinstance(sent_reply, dict) and isinstance(sent_reply.get("latency_trace"), dict)
                else {}
            )
            latency_breakdown = _latency_breakdown_from_trace(latency_trace)
            self._clear_stale_reply_context(state, reply)
            completed_event = {
                "event": "send_completed",
                "reply_id": reply_id,
                "target_name": reply.get("target_name"),
            }
            if latency_trace:
                completed_event["latency_trace"] = copy.deepcopy(latency_trace)
            if latency_breakdown:
                completed_event["latency_breakdown"] = latency_breakdown
            if send_observability:
                completed_event["send_observability"] = send_observability
            events.append(completed_event)
        return events

    def _record_stale_reply_context(
        self,
        state: dict[str, Any],
        reply: dict[str, Any],
        *,
        freshness: dict[str, Any],
        now: str | None = None,
    ) -> None:
        target_name = str(reply.get("target_name") or "")
        session = get_session_by_identity(state, target_name, session_key=str(reply.get("session_key") or "")) or {}
        if not isinstance(session, dict):
            return
        newer_messages = [
            {
                "id": str(item.get("id") or "")[:80],
                "content": str(item.get("content") or "")[:220],
                "sender": str(item.get("sender") or "")[:40],
            }
            for item in (freshness.get("newer_messages") or [])
            if isinstance(item, dict)
        ]
        session["stale_reply_context"] = {
            "schema_version": 1,
            "recorded_at": now or datetime.now().isoformat(timespec="seconds"),
            "reason": str(freshness.get("reason") or "freshness_stale"),
            "reply_id": str(reply.get("reply_id") or ""),
            "input_message_ids": [str(item) for item in reply.get("input_message_ids") or [] if str(item)],
            "message_content_digest": str(reply.get("message_content_digest") or ""),
            "unsent_brain_reply_sample": str(reply.get("reply_text") or "")[:260],
            "newer_messages": newer_messages[:5],
            "handoff_to_brain": (
                "上一轮 Brain 回复尚未发送前，会话出现更新。请把未发送草稿仅作为上下文线索，"
                "结合最新客户消息重新思考并生成最终回复。"
            ),
        }
        append_event(
            state,
            "scheduler_stale_reply_context_recorded",
            target_name=target_name,
            session_key=str(session.get("session_key") or reply.get("session_key") or ""),
            reply_id=str(reply.get("reply_id") or ""),
            newer_message_count=len(newer_messages),
        )

    def _clear_stale_reply_context(self, state: dict[str, Any], reply: dict[str, Any]) -> None:
        session = get_session_by_identity(
            state,
            str(reply.get("target_name") or ""),
            session_key=str(reply.get("session_key") or ""),
        )
        if isinstance(session, dict) and "stale_reply_context" in session:
            session.pop("stale_reply_context", None)

    def _brain_first_ready_reply_ownership_failure(self, reply: dict[str, Any]) -> str:
        return brain_first_ready_reply_ownership_failure(reply)

    def _capture_for_reply(self, state: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any] | None:
        inline_capture = reply.get("_capture")
        if isinstance(inline_capture, dict):
            return inline_capture
        capture_ids = [str(item) for item in reply.get("capture_ids", []) if str(item)]
        if not capture_ids:
            return None
        capture = (state.get("captures", {}) or {}).get(capture_ids[-1])
        return capture if isinstance(capture, dict) else None

    def _reply_for_callbacks(self, state: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        enriched = copy.deepcopy(reply)
        capture_ids = [str(item) for item in enriched.get("capture_ids", []) if str(item)]
        if capture_ids:
            capture = (state.get("captures", {}) or {}).get(capture_ids[-1])
            if isinstance(capture, dict):
                enriched["_capture"] = copy.deepcopy(capture)
        return enriched

    @staticmethod
    def _extract_send_observability(send_result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(send_result, dict):
            return {}
        send_payload = send_result.get("send_result")
        if not isinstance(send_payload, dict):
            send_payload = send_result
        send_meta = send_payload.get("send")
        if not isinstance(send_meta, dict):
            send_meta = send_payload if isinstance(send_payload, dict) else {}
        observability: dict[str, Any] = {}
        state = str(send_meta.get("state") or send_payload.get("state") or "").strip()
        if state:
            observability["state"] = state
        verification_mode = str(send_payload.get("verification_mode") or send_result.get("verification_mode") or "").strip()
        if verification_mode:
            observability["verification_mode"] = verification_mode
        try:
            retry_attempts = int(send_payload.get("retry_attempts") or send_result.get("retry_attempts") or 0)
        except (TypeError, ValueError):
            retry_attempts = 0
        observability["retry_attempts"] = max(0, retry_attempts)
        segment_attempt_counts = send_payload.get("segment_attempt_counts")
        if isinstance(segment_attempt_counts, list) and segment_attempt_counts:
            observability["segment_attempt_counts"] = [int(item) for item in segment_attempt_counts if isinstance(item, (int, float))]
        rpa_lock = send_meta.get("rpa_lock")
        if isinstance(rpa_lock, dict):
            observability["rpa_lock"] = copy.deepcopy(rpa_lock)
        timing = send_payload.get("timing")
        if isinstance(timing, dict):
            observability["timing"] = copy.deepcopy(timing)
        send_timing = send_meta.get("timing") if isinstance(send_meta, dict) else {}
        if isinstance(send_timing, dict):
            observability["send_timing"] = copy.deepcopy(send_timing)
        return observability


def mark_session_capture_failed(
    state: dict[str, Any],
    target_name: str,
    reason: str,
    *,
    session_key: str = "",
    now: str | None = None,
) -> None:
    from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler_state import append_event, ensure_session

    session = ensure_session(state, target_name, session_key=session_key, now=now)
    now_text = str(now or datetime.now().isoformat(timespec="seconds"))
    try:
        now_dt = datetime.fromisoformat(now_text)
    except Exception:
        now_dt = datetime.now()
    reason_text = str(reason or "")
    reason_lower = reason_text.lower()
    risk_state = session.setdefault("risk_state", {})
    fail_count = int(risk_state.get("capture_fail_count") or 0) + 1
    risk_state["capture_fail_count"] = fail_count
    # Exponential backoff avoids tight retry loops that look mechanical in UI.
    backoff_seconds = min(90, max(3, 3 * (2 ** min(fail_count - 1, 4))))
    soft_target_unconfirmed = "target_not_confirmed_for_messages" in reason_lower or "target_title_not_confirmed" in reason_lower
    if "lock_timeout" in reason_lower:
        # Lock contention is usually transient; retry sooner to avoid
        # customer-visible long-tail waiting while still preventing tight loops.
        backoff_seconds = min(12, max(2, 2 + fail_count))
    if soft_target_unconfirmed:
        # A failed title confirmation is usually a UI/OCR settle issue, not a
        # Brain failure. Preserve the pending intent, but cool down briefly so
        # we do not click the same session row again in a mechanical loop.
        backoff_seconds = min(7, max(3, 2 + fail_count) + random.uniform(0.4, 1.8))
    if "blank_render" in reason_lower:
        backoff_seconds = max(backoff_seconds, 25)
    retry_not_before = (now_dt + timedelta(seconds=backoff_seconds)).isoformat(timespec="seconds")
    session["status"] = "capture_cooldown" if soft_target_unconfirmed else "capture_failed"
    session["pending_capture"] = bool(soft_target_unconfirmed)
    if soft_target_unconfirmed:
        session["pending_reason"] = "target_unconfirmed_retry"
    else:
        session["pending_reason"] = ""
    risk_state["last_error"] = reason_text
    risk_state["last_capture_failed_at"] = now_text
    risk_state["capture_retry_not_before"] = retry_not_before
    append_event(
        state,
        "scheduler_capture_failed",
        target_name=session["target_name"],
        reason=reason_text,
        fail_count=fail_count,
        retry_after_seconds=backoff_seconds,
        retry_not_before=retry_not_before,
    )


def merge_scheduler_conversation_context(
    state: dict[str, Any],
    target_name: str,
    context_update: dict[str, Any],
    *,
    session_key: str = "",
    now: str | None = None,
) -> None:
    from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler_state import ensure_session

    if not isinstance(context_update, dict) or not context_update:
        return
    session = ensure_session(state, target_name, session_key=session_key, now=now)
    existing = session.get("conversation_context") if isinstance(session.get("conversation_context"), dict) else {}
    clean_update = {
        key: value
        for key, value in context_update.items()
        if value not in (None, "", [], {})
    }
    if not clean_update:
        return
    session["conversation_context"] = {
        **existing,
        **clean_update,
        "updated_at": str(now or datetime.now().isoformat(timespec="seconds")),
    }
    append_event(
        state,
        "scheduler_conversation_context_updated",
        target_name=session["target_name"],
        last_product_id=session["conversation_context"].get("last_product_id"),
    )


class CapturedMessagesConnector:
    """Connector facade for LLM planning from already-captured messages.

    It implements read-only message access so existing workflow planning can run
    without touching WeChat. Send methods intentionally fail closed.
    """

    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = copy.deepcopy(capture)
        self.send_attempts: list[dict[str, Any]] = []

    def get_messages(
        self,
        target: str,
        exact: bool = True,
        history_load_times: int = 0,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        messages = self.capture.get("messages") or []
        authoritative_batch = self.capture.get("batch") if isinstance(self.capture.get("batch"), list) else []
        authoritative_batch_ids = [
            canonical_input_message_id(item)
            for item in authoritative_batch
            if isinstance(item, dict) and canonical_input_message_id(item)
        ]
        used_batch_fallback = False
        if not messages and authoritative_batch:
            # Short high-sensitivity unread probes can be recovered after OCR
            # anchor matching, so the reply-eligible item may live only in the
            # scheduler batch. Keep the planner read-only, but do not starve it.
            messages = authoritative_batch
            used_batch_fallback = bool(messages)
        history_meta = self.capture.get("history_backfill") if isinstance(self.capture.get("history_backfill"), dict) else {}
        return {
            "ok": True,
            "target": target,
            "exact": exact,
            "messages": copy.deepcopy(messages),
            "_scheduler_authoritative_batch": copy.deepcopy(authoritative_batch),
            "_scheduler_authoritative_batch_ids": authoritative_batch_ids,
            "_scheduler_capture_is_authoritative": bool(authoritative_batch_ids or authoritative_batch),
            "_history_backfill": history_meta,
            "scheduler_capture_id": self.capture.get("capture_id"),
            "scheduler_context_version": self.capture.get("context_version"),
            "scheduler_used_batch_fallback": used_batch_fallback,
        }

    def send_text_and_verify(self, target: str, text: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        self.send_attempts.append({"target": target, "text": text, "exact": exact, "kwargs": dict(kwargs)})
        return {
            "ok": False,
            "verified": False,
            "state": "scheduler_planner_send_blocked",
            "error": "CapturedMessagesConnector is read-only; planner must not send via RPA.",
        }


def plan_reply_with_listen_workflow(
    capture: dict[str, Any],
    task: dict[str, Any],
    *,
    target_config: Any,
    config: dict[str, Any],
    rules: dict[str, Any],
    workflow_state: dict[str, Any],
    allow_fallback_send: bool = False,
    apply_final_visible_polish: bool = True,
) -> dict[str, Any]:
    """Reuse the existing reply planner without opening or sending WeChat."""

    import sys
    from pathlib import Path

    workflow_root = Path(__file__).resolve().parents[2] / "workflows"
    adapter_root = Path(__file__).resolve().parents[2] / "adapters"
    for path in (workflow_root, adapter_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.wechat_ai_customer_service.workflows.listen_and_reply import process_target
    from apps.wechat_ai_customer_service.workflows.listen_and_reply import (
        _apply_greeting,
        brain_first_requires_brain_owned_visible_reply,
        customer_visible_reply_is_brain_owned,
        final_visible_polish_blocks_send,
        normalize_brain_owned_customer_visible_reply_text,
        polish_customer_visible_reply_text,
        recent_customer_visible_reply_texts,
        sanitize_customer_visible_reply_text,
    )
    if apply_final_visible_polish:
        from apps.wechat_ai_customer_service.workflows.listen_and_reply import (
            final_visible_polish_blocks_send,
            final_visible_polish_degraded,
            finalize_customer_visible_reply_with_llm,
        )

    connector = CapturedMessagesConnector(capture)
    retry_instruction = polish_failure_retry_instruction(task)
    if retry_instruction:
        target_state_key = str(getattr(target_config, "session_key", "") or target_config.name)
        target_state = workflow_state.setdefault("targets", {}).setdefault(
            target_state_key,
            {
                "processed_message_ids": [],
                "processed_content_keys": [],
                "handoff_message_ids": [],
                "sent_replies": [],
                "reply_timestamps": [],
            },
        )
        target_state["brain_retry_instruction"] = retry_instruction
    event = process_target(
        connector=connector,  # type: ignore[arg-type]
        target=target_config,
        config=config,
        rules=rules,
        state=workflow_state,
        send=False,
        write_data=False,
        allow_fallback_send=allow_fallback_send,
        mark_dry_run=False,
    )
    if connector.send_attempts:
        return {
            "ok": False,
            "reason": "planner_attempted_send",
            "send_attempts": connector.send_attempts,
            "event": event,
        }
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    reply_text = str(decision.get("reply_text") or event.get("reply_text") or "").strip()
    combined = str(event.get("combined_content") or "")
    brain_owned_reply = customer_visible_reply_is_brain_owned(event=event, decision=decision)
    brain_first_visible_reply_required = brain_first_requires_brain_owned_visible_reply(config)
    if reply_text and brain_first_visible_reply_required and not brain_owned_reply:
        return {
            "ok": False,
            "reason": "brain_first_non_brain_owned_planner_reply_blocked",
            "event": event,
        }
    target_state = workflow_target_state_for_target(workflow_state, target_config)
    recent_reply_texts = recent_customer_visible_reply_texts(target_state)
    profile = {
        "target_name": str(target_config.name),
        "display_name": str(target_config.name),
        "basic_info": {},
        "tags": {},
        "conversation_summary": "",
        "greeting_preference": {},
    }
    if reply_text and event.get("action") not in {"blocked", "error"}:
        if brain_owned_reply:
            reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
            outbound_naturalness = {
                "applied": False,
                "reason": "skipped_for_customer_service_brain",
                "reply_text": reply_text,
            }
        else:
            reply_text = _apply_greeting(
                reply_text,
                profile,
                config,
                target_state=target_state,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
            reply_text = sanitize_customer_visible_reply_text(
                reply_text,
                config=config,
                combined=combined,
                reason=str(event.get("reason") or decision.get("reason") or ""),
                force_handoff_style=False,
                recent_reply_texts=recent_reply_texts,
            )
            outbound_naturalness = polish_customer_visible_reply_text(
                reply_text,
                config=config,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
        event["outbound_naturalness"] = outbound_naturalness
        if outbound_naturalness.get("applied"):
            reply_text = str(outbound_naturalness.get("reply_text") or reply_text)
        if apply_final_visible_polish:
            final_polish = finalize_customer_visible_reply_with_llm(
                reply_text,
                config=config,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
                source_channel=str(((event.get("reply_style_adapter") or {}) if isinstance(event.get("reply_style_adapter"), dict) else {}).get("source_channel") or "normal"),
                needs_handoff=False,
            )
            final_polish.setdefault("reply_text", reply_text)
            event["final_visible_llm_polish"] = final_polish
            if final_polish.get("passed"):
                reply_text = str(final_polish.get("reply_text") or reply_text)
                if brain_owned_reply:
                    reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
            elif final_visible_polish_blocks_send(final_polish, config=config):
                return {"ok": False, "reason": "final_visible_llm_polish_failed", "event": event}
            elif final_visible_polish_degraded(final_polish, config=config):
                event["final_visible_llm_polish_degraded"] = True
        decision = {
            **decision,
            "reply_text": reply_text,
            "brain_first_visible_reply_required": bool(brain_first_visible_reply_required),
        }
        if brain_owned_reply:
            decision["visible_reply_owner"] = str(
                ((event.get("customer_service_brain") or {}) if isinstance(event.get("customer_service_brain"), dict) else {}).get("visible_reply_owner")
                or decision.get("visible_reply_owner")
                or "brain"
            )
        event["decision"] = decision
    if event.get("action") in {"blocked", "error"}:
        return {"ok": False, "reason": str(event.get("reason") or event.get("action")), "event": event}
    if not reply_text:
        return {"ok": False, "reason": "empty_planned_reply", "event": event}
    conversation_context_update = event.get("conversation_context_update") if isinstance(event.get("conversation_context_update"), dict) else {}
    latency_trace = _planner_event_latency_trace(event)
    return {
        "ok": True,
        "target_name": str(target_config.name),
        "reply_text": reply_text,
        "decision": {
            **decision,
            "outbound_naturalness": event.get("outbound_naturalness"),
            "final_visible_llm_polish": event.get("final_visible_llm_polish"),
            "final_visible_llm_polish_degraded": event.get("final_visible_llm_polish_degraded", False),
        },
        "event": event,
        "task_id": task.get("task_id"),
        "capture_id": capture.get("capture_id"),
        "conversation_context_update": conversation_context_update,
        "latency_trace": latency_trace,
    }


def polish_reply_with_listen_workflow(
    planner_task: dict[str, Any],
    _task: dict[str, Any],
    *,
    target_config: Any,
    config: dict[str, Any],
    workflow_state: dict[str, Any],
) -> dict[str, Any]:
    import sys
    from pathlib import Path

    workflow_root = Path(__file__).resolve().parents[2] / "workflows"
    adapter_root = Path(__file__).resolve().parents[2] / "adapters"
    for path in (workflow_root, adapter_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.wechat_ai_customer_service.workflows.listen_and_reply import (
        brain_first_requires_brain_owned_visible_reply,
        customer_visible_reply_is_brain_owned,
        final_visible_polish_blocks_send,
        final_visible_polish_degraded,
        finalize_customer_visible_reply_with_llm,
        normalize_brain_owned_customer_visible_reply_text,
        recent_customer_visible_reply_texts,
    )

    planner_result = planner_task.get("result") if isinstance(planner_task.get("result"), dict) else {}
    decision = copy.deepcopy(planner_result.get("decision") if isinstance(planner_result.get("decision"), dict) else {})
    event = copy.deepcopy(planner_result.get("event") if isinstance(planner_result.get("event"), dict) else {})
    reply_text = str(planner_result.get("reply_text") or decision.get("reply_text") or planner_task.get("reply_text") or "").strip()
    if not reply_text:
        return {"ok": False, "reason": "empty_planner_reply_for_polish"}
    brain_owned_reply = customer_visible_reply_is_brain_owned(event=event, decision=decision)
    brain_first_visible_reply_required = brain_first_requires_brain_owned_visible_reply(config)
    if brain_first_visible_reply_required and not brain_owned_reply:
        return {"ok": False, "reason": "brain_first_non_brain_owned_polish_reply_blocked", "event": event}
    if brain_owned_reply:
        reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
    target_state = workflow_target_state_for_target(workflow_state, target_config)
    recent_reply_texts = recent_customer_visible_reply_texts(target_state)
    combined = str(event.get("combined_content") or "")
    source_channel = str(((event.get("reply_style_adapter") or {}) if isinstance(event.get("reply_style_adapter"), dict) else {}).get("source_channel") or "normal")
    final_polish = finalize_customer_visible_reply_with_llm(
        reply_text,
        config=config,
        combined=combined,
        recent_reply_texts=recent_reply_texts,
        source_channel=source_channel,
        needs_handoff=False,
    )
    final_polish.setdefault("reply_text", reply_text)
    event["final_visible_llm_polish"] = final_polish
    degraded = False
    if final_polish.get("passed"):
        reply_text = str(final_polish.get("reply_text") or reply_text)
        if brain_owned_reply:
            reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
    elif final_visible_polish_blocks_send(final_polish, config=config):
        return {"ok": False, "reason": "final_visible_llm_polish_failed", "event": event}
    elif final_visible_polish_degraded(final_polish, config=config):
        degraded = True
        event["final_visible_llm_polish_degraded"] = True
    decision["reply_text"] = reply_text
    decision["brain_first_visible_reply_required"] = bool(brain_first_visible_reply_required)
    if brain_owned_reply:
        decision["visible_reply_owner"] = str(decision.get("visible_reply_owner") or "brain")
    decision["final_visible_llm_polish"] = final_polish
    decision["final_visible_llm_polish_degraded"] = degraded
    latency_trace = _planner_event_latency_trace(event)
    return {
        "ok": True,
        "reply_text": reply_text,
        "decision": decision,
        "event": event,
        "degraded": degraded,
        "duration_seconds": final_polish.get("duration_seconds"),
        "latency_trace": latency_trace,
    }


def workflow_target_state_for_target(workflow_state: dict[str, Any], target_config: Any) -> dict[str, Any]:
    try:
        targets = workflow_state.get("targets", {}) if isinstance(workflow_state, dict) else {}
        if not isinstance(targets, dict):
            return {}
        session_key = str(getattr(target_config, "session_key", "") or "").strip()
        if session_key and isinstance(targets.get(session_key), dict):
            return targets.get(session_key) or {}
        target_name = str(getattr(target_config, "name", "") or "").strip()
        if target_name and isinstance(targets.get(target_name), dict):
            return targets.get(target_name) or {}
    except Exception:
        return {}
    return {}


class ManagedListenerSchedulerBridge:
    """Wire the persistent scheduler into the managed listener process.

    The bridge owns the real WeChat connector, so capture and send stay serial
    in the listener process. LLM planning receives only captured message
    snapshots through ``CapturedMessagesConnector``.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        config_path: Path,
        allow_send: bool,
        write_data: bool,
    ) -> None:
        self.tenant_id = str(tenant_id or "").strip()
        self.config_path = Path(config_path)
        self.allow_send = bool(allow_send)
        self.write_data = bool(write_data)
        self.config: dict[str, Any] = {}
        self.rules: dict[str, Any] = {}
        self.scheduler_config = SchedulerConfig()
        self.targets: list[Any] = []
        self.target_by_name: dict[str, Any] = {}
        self.respond_all_unread_sessions = False
        self.ignored_session_names: set[str] = set()
        self.use_multi_target = False
        self.state_path: Path | None = None
        self.audit_path: Path | None = None
        self.session_monitor: Any = None
        self.store = SchedulerStateStore(tenant_id=self.tenant_id)
        self.ledger = SessionLedgerStore(tenant_id=self.tenant_id, root=self.store.ledger_root)
        self.runtime: CustomerServiceSchedulerRuntime | None = None
        self.connector: Any = None
        self._workflow: dict[str, Any] = {}
        self._runtime_signature: tuple[Any, ...] | None = None
        self._freshness_last_strict_at_by_target: dict[str, float] = {}
        self._freshness_session_list_preview_cache: dict[str, dict[str, Any]] = {}
        self._switch_human_delay_enabled = False
        self._switch_human_delay_min_seconds = 0.0
        self._switch_human_delay_max_seconds = 0.0
        self._capture_one_target_per_round = False
        self._last_capture_signal_target = ""
        self._last_actual_capture_target = ""
        self._last_capture_switch_delay_seconds = 0.0
        self._load_workflow_symbols()
        self.reload()

    @property
    def enabled(self) -> bool:
        return bool(self.scheduler_config.enabled)

    def shutdown(self) -> None:
        if self.runtime is not None:
            self.runtime.shutdown()
            self.runtime = None

    def reload(self) -> None:
        wf = self._workflow
        with self._tenant_environment():
            config = wf["load_config"](self.config_path)
            if self.allow_send:
                config["_require_customer_service_brain_first_startup_guard"] = True
            config = wf["apply_local_customer_service_settings"](config)
        self.config = config
        _apply_rpa_humanized_send_runtime_env(config)
        self.scheduler_config = SchedulerConfig.from_config(config)
        session_routing = config.get("_local_customer_service_session_routing", {}) or {}
        if not isinstance(session_routing, dict):
            session_routing = {}
        self.respond_all_unread_sessions = bool(session_routing.get("respond_all_unread_sessions", False))
        self.ignored_session_names = {
            str(item).strip()
            for item in session_routing.get("ignored_names", []) or []
            if str(item).strip()
        }
        allow_empty_targets = bool(self.respond_all_unread_sessions or session_routing.get("managed", False))
        self.targets = wf["parse_targets"](config, allow_empty=allow_empty_targets)
        self.target_by_name = {str(target.name): target for target in self.targets}
        rules_path = config.get("rules_path")
        self.rules = wf["load_rules"](wf["resolve_path"](rules_path)) if rules_path else config
        self.state_path = wf["resolve_path"](config.get("state_path"))
        self.audit_path = wf["resolve_path"](config.get("audit_log_path"))
        multi_target_cfg = config.get("multi_target") if isinstance(config.get("multi_target"), dict) else {}
        multi_target_cfg = self._normalize_multi_target_switch_controls(multi_target_cfg or {})
        config["multi_target"] = multi_target_cfg
        self.use_multi_target = bool((multi_target_cfg or {}).get("enabled"))
        self._switch_human_delay_enabled = bool((multi_target_cfg or {}).get("switch_human_delay_enabled", False))
        self._switch_human_delay_min_seconds = self._safe_non_negative_float(
            (multi_target_cfg or {}).get("switch_human_delay_min_seconds"),
            default=0.0,
        )
        self._switch_human_delay_max_seconds = self._safe_non_negative_float(
            (multi_target_cfg or {}).get("switch_human_delay_max_seconds"),
            default=self._switch_human_delay_min_seconds,
        )
        if self._switch_human_delay_max_seconds < self._switch_human_delay_min_seconds:
            self._switch_human_delay_max_seconds = self._switch_human_delay_min_seconds
        self._capture_one_target_per_round = bool((multi_target_cfg or {}).get("capture_one_target_per_round", False))
        try:
            configured_target_batch = int((multi_target_cfg or {}).get("max_targets_per_iteration") or 1)
        except (TypeError, ValueError):
            configured_target_batch = 1
        if (
            self.use_multi_target
            and self._capture_one_target_per_round
            and configured_target_batch <= 1
            and int(self.scheduler_config.capture_max_sessions_per_round) != 1
        ):
            self.scheduler_config = replace(self.scheduler_config, capture_max_sessions_per_round=1)
        self._ensure_connector()
        self._ensure_session_monitor(multi_target_cfg or {})
        self._ensure_runtime()

    def tick(self, *, allow_send: bool | None = None, now: str | None = None) -> dict[str, Any]:
        self.reload()
        if not self.enabled:
            return {"ok": True, "scheduler_enabled": False, "events": [], "summary": {}}
        wf = self._workflow
        status: dict[str, Any]
        if wf["listener_skip_pre_status_check"]():
            status = {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "pre_status_check_skipped",
                "reason": "managed_scheduler_low_risk_mode",
            }
        else:
            status = self.connector.require_online()
        session_signals = self._collect_session_signals()
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("scheduler runtime is not initialized")
        result = runtime.tick(
            session_signals=session_signals,
            allow_send=self.allow_send if allow_send is None else bool(allow_send),
            now=now,
        )
        result.update(
            {
                "scheduler_enabled": True,
                "dry_run": not (self.allow_send if allow_send is None else bool(allow_send)),
                "status": status,
                "targets": [str(target.name) for target in self.targets],
                "active_session_signals": [str(item.get("name") or item.get("target_name") or "") for item in session_signals],
                "switch_human_delay_seconds": float(self._last_capture_switch_delay_seconds),
            }
        )
        return result

    @staticmethod
    def _safe_non_negative_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float(default)
        return max(0.0, parsed)

    def _normalize_multi_target_switch_controls(self, multi_target_cfg: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(multi_target_cfg or {})
        if not bool(normalized.get("enabled")):
            return normalized
        dispatch_strategy = str(normalized.get("dispatch_strategy") or "event_driven").strip().lower()
        if dispatch_strategy != "event_driven":
            return normalized
        if normalized.get("rpa_low_risk_mode", True) is not False:
            normalized["rpa_low_risk_mode"] = True
            normalized.setdefault("initial_preview_can_raise_unread", False)
            normalized.setdefault("preview_change_can_raise_unread", False)
            normalized.setdefault("short_preview_can_raise_unread", True)
            normalized.setdefault("require_unread_badge_for_dispatch", True)
            normalized.setdefault("require_preview_signal_with_unread_badge", True)
            normalized["scan_all_whitelist_each_iteration"] = False
            normalized["idle_whitelist_sweep_count"] = 0
            try:
                target_batch = int(normalized.get("max_targets_per_iteration") or 0)
            except (TypeError, ValueError):
                target_batch = 0
            normalized["max_targets_per_iteration"] = max(2, min(2, target_batch or 2))
            normalized["max_scan_targets_per_iteration"] = 0
        delay_enabled = bool(normalized.get("switch_human_delay_enabled", normalized.get("rpa_low_risk_mode") is True))
        normalized["switch_human_delay_enabled"] = delay_enabled
        if delay_enabled:
            delay_min = self._safe_non_negative_float(
                normalized.get("switch_human_delay_min_seconds"),
                default=1.0,
            )
            if delay_min <= 0:
                delay_min = 1.0
            delay_max = self._safe_non_negative_float(
                normalized.get("switch_human_delay_max_seconds"),
                default=3.0,
            )
            if delay_max <= 0:
                delay_max = 3.0
            if delay_max < delay_min:
                delay_max = delay_min
            normalized["switch_human_delay_min_seconds"] = float(delay_min)
            normalized["switch_human_delay_max_seconds"] = float(delay_max)
            # Retain only a minimal anti-bounce guard here. The visible
            # humanization should come from the randomized 1-3s switch pause,
            # not from a stale hard interval carried over from older configs.
            normalized["min_switch_interval_seconds"] = 1
            normalized["capture_one_target_per_round"] = bool(
                normalized.get("capture_one_target_per_round", False)
            )
        else:
            normalized["min_switch_interval_seconds"] = 1
            normalized["switch_human_delay_min_seconds"] = 0.0
            normalized["switch_human_delay_max_seconds"] = 0.0
        return normalized

    def _load_workflow_symbols(self) -> None:
        import sys

        app_root = Path(__file__).resolve().parents[2]
        workflow_root = app_root / "workflows"
        adapter_root = app_root / "adapters"
        project_root = app_root.parents[1]
        for path in (project_root, app_root, workflow_root, adapter_root):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

        from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnector
        from apps.wechat_ai_customer_service.admin_backend.services.session_monitor import SessionMonitor
        from apps.wechat_ai_customer_service.workflows.listen_and_reply import (
            StateLock,
            TargetConfig,
            append_audit,
            base_event,
            batch_selection_payload,
            build_reply_trace_id,
            default_max_batch_messages,
            detect_newer_messages_before_send,
            history_gap_risk_blocks_reply,
            listener_skip_pre_status_check,
            load_config,
            load_rules,
            load_state,
            mark_coalesced_messages,
            mark_processed,
            maybe_enrich_messages_with_history,
            normalize_capture_payload_for_semantic_processing,
            parse_targets,
            record_reply_timestamp,
            resolve_path,
            save_state,
            select_batch_details,
            send_reply_with_optional_multi_bubble,
            apply_local_customer_service_settings,
        )

        self._workflow = {
            "WeChatConnector": WeChatConnector,
            "SessionMonitor": SessionMonitor,
            "StateLock": StateLock,
            "TargetConfig": TargetConfig,
            "append_audit": append_audit,
            "base_event": base_event,
            "batch_selection_payload": batch_selection_payload,
            "build_reply_trace_id": build_reply_trace_id,
            "default_max_batch_messages": default_max_batch_messages,
            "detect_newer_messages_before_send": detect_newer_messages_before_send,
            "history_gap_risk_blocks_reply": history_gap_risk_blocks_reply,
            "listener_skip_pre_status_check": listener_skip_pre_status_check,
            "load_config": load_config,
            "load_rules": load_rules,
            "load_state": load_state,
            "mark_coalesced_messages": mark_coalesced_messages,
            "mark_processed": mark_processed,
            "maybe_enrich_messages_with_history": maybe_enrich_messages_with_history,
            "normalize_capture_payload_for_semantic_processing": normalize_capture_payload_for_semantic_processing,
            "parse_targets": parse_targets,
            "record_reply_timestamp": record_reply_timestamp,
            "resolve_path": resolve_path,
            "save_state": save_state,
            "select_batch_details": select_batch_details,
            "send_reply_with_optional_multi_bubble": send_reply_with_optional_multi_bubble,
            "apply_local_customer_service_settings": apply_local_customer_service_settings,
        }

    @contextmanager
    def _tenant_environment(self):
        previous = os.environ.get("WECHAT_KNOWLEDGE_TENANT")
        os.environ["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("WECHAT_KNOWLEDGE_TENANT", None)
            else:
                os.environ["WECHAT_KNOWLEDGE_TENANT"] = previous

    def _ensure_connector(self) -> None:
        if self.connector is None:
            self.connector = self._workflow["WeChatConnector"]()

    def _ensure_session_monitor(self, multi_target_cfg: dict[str, Any]) -> None:
        if not self.use_multi_target:
            self.session_monitor = None
            return
        whitelist = set() if self.respond_all_unread_sessions else {str(target.name) for target in self.targets}
        max_targets = int(multi_target_cfg.get("max_targets_per_iteration", self.scheduler_config.max_pending_sessions) or self.scheduler_config.max_pending_sessions)
        min_switch = int(multi_target_cfg.get("min_switch_interval_seconds", 2) or 2)
        dispatch_strategy = str(multi_target_cfg.get("dispatch_strategy") or "event_driven").strip().lower()
        sticky_hold = int(multi_target_cfg.get("sticky_target_hold_seconds", 35) or 35)
        sticky_rounds = int(multi_target_cfg.get("sticky_max_dispatch_rounds", 3) or 3)
        preview_confirmations = int(multi_target_cfg.get("preview_change_confirmations", 2) or 2)
        initial_preview_can_raise_unread = bool(multi_target_cfg.get("initial_preview_can_raise_unread", True))
        preview_change_can_raise_unread = bool(multi_target_cfg.get("preview_change_can_raise_unread", True))
        short_preview_can_raise_unread = bool(multi_target_cfg.get("short_preview_can_raise_unread", True))
        require_unread_badge_for_dispatch = bool(multi_target_cfg.get("require_unread_badge_for_dispatch", False))
        require_preview_signal_with_unread_badge = bool(multi_target_cfg.get("require_preview_signal_with_unread_badge", False))
        short_signal_max_chars = int(multi_target_cfg.get("high_sensitivity_short_max_chars", 7) or 7)
        short_signal_merge_window = float(multi_target_cfg.get("high_sensitivity_short_merge_window_seconds", 2.5) or 0.0)
        empty_capture_retry_seconds = float(multi_target_cfg.get("high_sensitivity_short_empty_capture_retry_seconds", 3.0) or 0.0)
        empty_capture_retry_backoff = float(multi_target_cfg.get("high_sensitivity_short_empty_capture_retry_backoff_multiplier", 1.8) or 1.0)
        empty_capture_retry_max_seconds = float(multi_target_cfg.get("high_sensitivity_short_empty_capture_retry_max_seconds", 15.0) or 0.0)
        if self.session_monitor is None:
            self.session_monitor = self._workflow["SessionMonitor"](
                tenant_id=self.tenant_id,
                whitelist=whitelist,
                blacklist=self.ignored_session_names,
                max_targets_per_iteration=max(1, max_targets),
                min_switch_interval_seconds=max(1, min_switch),
                dispatch_strategy=dispatch_strategy,
                sticky_target_hold_seconds=max(5, sticky_hold),
                sticky_max_dispatch_rounds=max(1, sticky_rounds),
                preview_change_confirmations=max(1, preview_confirmations),
                initial_preview_can_raise_unread=initial_preview_can_raise_unread,
                preview_change_can_raise_unread=preview_change_can_raise_unread,
                short_preview_can_raise_unread=short_preview_can_raise_unread,
                require_unread_badge_for_dispatch=require_unread_badge_for_dispatch,
                require_preview_signal_with_unread_badge=require_preview_signal_with_unread_badge,
                high_sensitivity_short_max_chars=max(1, short_signal_max_chars),
                high_sensitivity_short_merge_window_seconds=max(0.0, short_signal_merge_window),
                empty_capture_retry_seconds=max(0.0, empty_capture_retry_seconds),
                empty_capture_retry_backoff_multiplier=max(1.0, empty_capture_retry_backoff),
                empty_capture_retry_max_seconds=max(max(0.0, empty_capture_retry_seconds), empty_capture_retry_max_seconds),
            )
            return
        self.session_monitor.whitelist = whitelist
        self.session_monitor.blacklist = set(self.ignored_session_names)
        self.session_monitor.max_targets_per_iteration = max(1, max_targets)
        self.session_monitor.min_switch_interval_seconds = max(1, min_switch)
        self.session_monitor.dispatch_strategy = dispatch_strategy if dispatch_strategy in {"event_driven", "legacy_pending_scan"} else "event_driven"
        self.session_monitor.sticky_target_hold_seconds = max(5, sticky_hold)
        self.session_monitor.sticky_max_dispatch_rounds = max(1, sticky_rounds)
        self.session_monitor.preview_change_confirmations = max(1, preview_confirmations)
        self.session_monitor.initial_preview_can_raise_unread = initial_preview_can_raise_unread
        self.session_monitor.preview_change_can_raise_unread = preview_change_can_raise_unread
        self.session_monitor.short_preview_can_raise_unread = short_preview_can_raise_unread
        self.session_monitor.require_unread_badge_for_dispatch = require_unread_badge_for_dispatch
        self.session_monitor.require_preview_signal_with_unread_badge = require_preview_signal_with_unread_badge
        self.session_monitor.high_sensitivity_short_max_chars = max(1, short_signal_max_chars)
        self.session_monitor.high_sensitivity_short_merge_window_seconds = max(0.0, short_signal_merge_window)
        self.session_monitor.empty_capture_retry_seconds = max(0.0, empty_capture_retry_seconds)
        self.session_monitor.empty_capture_retry_backoff_multiplier = max(1.0, empty_capture_retry_backoff)
        self.session_monitor.empty_capture_retry_max_seconds = max(
            self.session_monitor.empty_capture_retry_seconds,
            empty_capture_retry_max_seconds,
        )

    def _ensure_runtime(self) -> None:
        signature = (
            self.scheduler_config.capture_max_sessions_per_round,
            self.scheduler_config.llm_max_concurrency,
            self.scheduler_config.planner_max_concurrency,
            self.scheduler_config.polish_max_concurrency,
            self.scheduler_config.send_max_replies_per_round,
            self.scheduler_config.stale_reply_policy,
        )
        if self.runtime is not None and self._runtime_signature == signature:
            return
        if self.runtime is not None:
            self.runtime.shutdown()
        self.runtime = CustomerServiceSchedulerRuntime(
            store=self.store,
            config=self.scheduler_config,
            capture_fn=self._capture_session,
            plan_reply_fn=self._plan_reply,
            polish_reply_fn=self._polish_reply,
            freshness_fn=self._freshness_check,
            send_fn=self._send_reply,
            capture_done_fn=self._capture_done,
        )
        self._runtime_signature = signature

    def _collect_session_signals(self) -> list[dict[str, Any]]:
        self._last_capture_switch_delay_seconds = 0.0
        if self.use_multi_target and self.session_monitor is not None:
            self.session_monitor.poll(self.connector)
            if hasattr(self.session_monitor, "select_dispatch_targets"):
                pending = self.session_monitor.select_dispatch_targets(limit=self.scheduler_config.capture_max_sessions_per_round)
            else:
                pending = self.session_monitor.pending_targets(limit=self.scheduler_config.max_pending_sessions)
            # When the sticky target already has active scheduler work, prefer a
            # different unread session for the next capture tick. This avoids
            # wasting rounds on a busy session and reduces cross-session lag.
            if pending:
                try:
                    state = self.store.load()
                except Exception:  # noqa: BLE001 - keep listener resilient
                    state = {}
                expanded_pending = list(pending)
                if all(
                    has_active_session_work(
                        state,
                        str(getattr(item, "name", "") or ""),
                        session_key=str(getattr(item, "session_key", "") or ""),
                    )
                    for item in pending
                    if str(getattr(item, "name", "") or "")
                ):
                    expanded_pending = list(self.session_monitor.pending_targets(limit=self.scheduler_config.max_pending_sessions))
                non_busy = []
                busy = []
                for item in expanded_pending:
                    name = str(getattr(item, "name", "") or "")
                    if not name or name in self.ignored_session_names:
                        continue
                    session_key = str(getattr(item, "session_key", "") or "")
                    if name and has_active_session_work(state, name, session_key=session_key):
                        busy.append(item)
                    else:
                        non_busy.append(item)
                if non_busy:
                    pending = non_busy
                elif busy:
                    # All currently pending sessions already have planner/polish/send
                    # work in flight. Re-capturing one of them would only reopen the
                    # same chat and amplify mechanical foreground switching; the
                    # monitor keeps the pending signal for a later tick.
                    pending = []
            next_name = ""
            if pending:
                next_name = str(getattr(pending[0], "name", "") or "")
            if next_name:
                self._last_capture_signal_target = next_name
            return [
                {
                    "name": item.name,
                    "session_key": getattr(item, "session_key", ""),
                    "exact": item.exact,
                    "unread_detected": item.unread_detected,
                    "conversation_type": item.conversation_type,
                }
                for item in pending
                if item.name not in self.ignored_session_names
            ]
        return [
            {
                "name": target.name,
                "session_key": stable_session_key(target.name, conversation_type="configured"),
                "exact": target.exact,
                "unread_detected": True,
                "conversation_type": "configured",
            }
            for target in self.targets
        ]

    def _target_for_name(self, name: str, *, exact: bool = True) -> Any:
        target = self.target_by_name.get(str(name))
        if target is not None:
            return target
        if not self.respond_all_unread_sessions:
            raise KeyError(f"target is not configured: {name}")
        default_batch = self._workflow["default_max_batch_messages"](self.config)
        return self._workflow["TargetConfig"](
            name=str(name),
            enabled=True,
            exact=bool(exact),
            allow_self_for_test=False,
            max_batch_messages=max(1, int(default_batch or 1)),
        )

    def _target_for_session(self, session: dict[str, Any]) -> Any:
        target = self._target_for_name(
            str(session.get("target_name") or session.get("name") or ""),
            exact=bool(session.get("exact", True)),
        )
        session_key = str(session.get("session_key") or "").strip()
        if not session_key:
            return target
        try:
            return replace(target, session_key=session_key)
        except TypeError:
            return WorkflowTargetConfig(
                name=str(getattr(target, "name", "") or session.get("target_name") or session.get("name") or ""),
                enabled=bool(getattr(target, "enabled", True)),
                exact=bool(getattr(target, "exact", True)),
                allow_self_for_test=bool(getattr(target, "allow_self_for_test", False)),
                max_batch_messages=int(getattr(target, "max_batch_messages", 1) or 1),
                session_key=session_key,
            )

    def _workflow_state_snapshot(self) -> dict[str, Any]:
        if self.state_path is None:
            base = {"version": 1, "targets": {}}
            return self._merge_scheduler_context_into_workflow_state(base)
        try:
            return self._merge_scheduler_context_into_workflow_state(self._workflow["load_state"](self.state_path))
        except Exception:
            return self._merge_scheduler_context_into_workflow_state({"version": 1, "targets": {}})

    def _merge_scheduler_context_into_workflow_state(self, workflow_state: dict[str, Any]) -> dict[str, Any]:
        try:
            scheduler_state = self.store.load()
        except Exception:
            return workflow_state
        targets = workflow_state.setdefault("targets", {})
        for _state_key, session in (scheduler_state.get("sessions", {}) or {}).items():
            if not isinstance(session, dict):
                continue
            target_name = str(session.get("target_name") or session.get("display_name") or _state_key)
            session_key = str(session.get("session_key") or stable_session_key(
                target_name,
                conversation_type=session.get("conversation_type") or "unknown",
            ))
            target_state = self._target_state(workflow_state, target_name, session_key=session_key)
            self._merge_session_ledger_summary(target_state, session_key)
            context = session.get("conversation_context") if isinstance(session.get("conversation_context"), dict) else {}
            if context:
                existing = target_state.get("conversation_context") if isinstance(target_state.get("conversation_context"), dict) else {}
                target_state["conversation_context"] = {**existing, **context}
            stale_context = session.get("stale_reply_context") if isinstance(session.get("stale_reply_context"), dict) else {}
            if stale_context:
                interaction = (
                    target_state.get("conversation_interaction_state")
                    if isinstance(target_state.get("conversation_interaction_state"), dict)
                    else {}
                )
                target_state["conversation_interaction_state"] = {
                    **interaction,
                    "stale_unsent_reply_context": copy.deepcopy(stale_context),
                    "unanswered_exists": True,
                }
        return workflow_state

    def _merge_session_ledger_summary(self, target_state: dict[str, Any], session_key: str) -> None:
        summary = self.ledger.load_summary(session_key)
        if not summary:
            return
        target_state["_session_key"] = session_key
        processed_ids = list(target_state.get("processed_message_ids") or [])
        for item in (
            summary.get("last_processed_message_id"),
            summary.get("last_replied_message_id"),
        ):
            value = str(item or "").strip()
            if value and value not in processed_ids:
                processed_ids.append(value)
        target_state["processed_message_ids"] = processed_ids[-500:]
        processed_keys = list(target_state.get("processed_content_keys") or [])
        for item in summary.get("last_processed_content_keys") or []:
            value = str(item or "").strip()
            if value and value not in processed_keys:
                processed_keys.append(value)
        target_state["processed_content_keys"] = processed_keys[-500:]
        anchor = summary.get("last_successful_reply_anchor")
        if isinstance(anchor, dict) and anchor:
            target_state["last_successful_reply_anchor"] = {
                **(target_state.get("last_successful_reply_anchor") if isinstance(target_state.get("last_successful_reply_anchor"), dict) else {}),
                **anchor,
                "source": "session_ledger",
            }
        context_summary = str(summary.get("context_summary") or "").strip()
        recent_messages = summary.get("recent_messages") if isinstance(summary.get("recent_messages"), list) else []
        if context_summary:
            context = target_state.get("conversation_context") if isinstance(target_state.get("conversation_context"), dict) else {}
            target_state["conversation_context"] = {
                **context,
                "ledger_context_summary": context_summary,
            }
        if recent_messages:
            context = target_state.get("conversation_context") if isinstance(target_state.get("conversation_context"), dict) else {}
            target_state["conversation_context"] = {
                **context,
                "ledger_recent_messages": recent_messages[-20:],
            }
        interaction = target_state.get("conversation_interaction_state") if isinstance(target_state.get("conversation_interaction_state"), dict) else {}
        unreplied_ids = [str(item).strip() for item in summary.get("last_unreplied_message_ids") or [] if str(item).strip()]
        unreplied_text = ""
        if unreplied_ids and recent_messages:
            id_set = set(unreplied_ids)
            for item in reversed(recent_messages):
                if not isinstance(item, dict):
                    continue
                identity = str(item.get("identity") or item.get("id") or "").strip()
                if identity in id_set:
                    unreplied_text = str(item.get("content") or "").strip()
                    break
        anchor = summary.get("last_successful_reply_anchor") if isinstance(summary.get("last_successful_reply_anchor"), dict) else {}
        target_state["conversation_interaction_state"] = {
            **interaction,
            "schema_version": 1,
            "last_customer_message_at": str(summary.get("last_capture_at") or interaction.get("last_customer_message_at") or ""),
            "last_reply_sent_at": str(summary.get("last_reply_at") or interaction.get("last_reply_sent_at") or ""),
            "last_reply_text_sample": str(anchor.get("reply_text_sample") or interaction.get("last_reply_text_sample") or "")[:180],
            "last_unanswered_customer_text": unreplied_text[:220] if unreplied_text else str(interaction.get("last_unanswered_customer_text") or ""),
            "last_unanswered_message_ids": unreplied_ids[-20:],
            "unanswered_exists": bool(unreplied_ids),
            "updated_at": str(summary.get("updated_at") or interaction.get("updated_at") or ""),
        }

    def _target_state(self, state: dict[str, Any], target_name: str, *, session_key: str = "") -> dict[str, Any]:
        clean_session_key = str(session_key or "").strip()
        clean_target_name = str(target_name or "").strip()
        state_key = clean_session_key or clean_target_name
        target_state = state.setdefault("targets", {}).setdefault(
            state_key,
            {
                "processed_message_ids": [],
                "processed_content_keys": [],
                "handoff_message_ids": [],
                "sent_replies": [],
                "reply_timestamps": [],
            },
        )
        if clean_session_key:
            target_state["_session_key"] = clean_session_key
            target_state["_display_name"] = clean_target_name
        return target_state

    def _capture_session(self, session: dict[str, Any]) -> dict[str, Any]:
        target = self._target_for_session(session)
        if (
            self._switch_human_delay_enabled
            and target.name
            and self._last_actual_capture_target
            and target.name != self._last_actual_capture_target
        ):
            delay = random.uniform(
                float(self._switch_human_delay_min_seconds),
                float(self._switch_human_delay_max_seconds),
            )
            if delay > 0:
                time.sleep(delay)
                self._last_capture_switch_delay_seconds = round(delay, 3)
        if target.name:
            self._last_actual_capture_target = target.name
        payload = self.connector.get_messages(
            target.name,
            exact=target.exact,
            session_key=str(getattr(target, "session_key", "") or session.get("session_key") or ""),
        )
        if not payload.get("ok"):
            payload_state = str(payload.get("state") or "").strip().lower()
            if payload_state in {"messages_lock_timeout", "sessions_lock_timeout", "status_lock_timeout", "rpa_lock_timeout"}:
                return {
                    "ok": False,
                    "blocked": True,
                    "reason": f"capture_{payload_state}",
                    "transient": True,
                    "messages": [],
                    "batch": [],
                    "overflow_messages": [],
                    "history_backfill": {},
                    "capture_guard": {
                        "state": payload.get("state"),
                        "error": payload.get("error"),
                        "rpa_lock": payload.get("rpa_lock"),
                    },
                }
            raise RuntimeError(f"get_messages failed for {target.name}: {payload}")
        workflow_state = self._workflow_state_snapshot()
        target_state = self._target_state(
            workflow_state,
            target.name,
            session_key=str(getattr(target, "session_key", "") or session.get("session_key") or ""),
        )
        payload = self._workflow["maybe_enrich_messages_with_history"](
            connector=self.connector,
            target=target,
            config=self.config,
            payload=payload,
            target_state=target_state,
        )
        payload = self._workflow["normalize_capture_payload_for_semantic_processing"](
            payload,
            target=target,
            config=self.config,
        )
        messages = list(payload.get("messages") or [])
        pending_signal = self._session_monitor_snapshot_for_target(
            target.name,
            session_key=str(getattr(target, "session_key", "") or session.get("session_key") or ""),
        )
        messages = annotate_latest_customer_messages_with_pending_signal(
            messages,
            pending_signal,
            target_name=target.name,
            conversation_type=str(session.get("conversation_type") or "unknown"),
            session_key=str(getattr(target, "session_key", "") or session.get("session_key") or ""),
            allow_self_for_test=target.allow_self_for_test,
            max_messages=min(2, max(1, int(target.max_batch_messages or 1))),
        )
        payload["messages"] = messages
        history_meta = payload.get("_history_backfill", {}) if isinstance(payload.get("_history_backfill"), dict) else {}
        selection = self._workflow["select_batch_details"](
            messages,
            target_state=target_state,
            allow_self_for_test=target.allow_self_for_test,
            max_batch_messages=target.max_batch_messages,
            config=self.config,
        )
        if not selection.batch:
            recovered_short_batch = recover_pending_signal_batch_from_monitor(
                messages,
                pending_signal,
                target_name=target.name,
                allow_self_for_test=target.allow_self_for_test,
                max_batch_messages=min(2, max(1, int(target.max_batch_messages or 1))),
                now=datetime.now().isoformat(timespec="seconds"),
            )
            if recovered_short_batch:
                selection = replace(
                    selection,
                    batch=recovered_short_batch,
                    overflow_messages=[],
                    eligible_count=len(recovered_short_batch),
                )
                history_meta = dict(history_meta)
                history_meta["monitor_pending_recovered_from_anchor_empty"] = True
                history_meta["short_pending_recovered_count"] = len(recovered_short_batch)
        if self._workflow["history_gap_risk_blocks_reply"](history_meta, self.config) and selection.eligible_count > 0:
            return {
                "ok": False,
                "blocked": True,
                "reason": "history_backfill_gap_risk",
                "messages": messages,
                "batch": selection.batch,
                "overflow_messages": selection.overflow_messages,
                "history_backfill": history_meta,
            }
        return {
            "ok": True,
            "session_key": str(session.get("session_key") or stable_session_key(
                target.name,
                conversation_type=session.get("conversation_type") or "unknown",
            )),
            "target_name": target.name,
            "exact": target.exact,
            "messages": messages,
            "batch": selection.batch,
            "overflow_messages": selection.overflow_messages,
            "history_backfill": history_meta,
            "batch_selection": self._workflow["batch_selection_payload"](selection),
        }

    def _capture_done(self, session: dict[str, Any], result: dict[str, Any], capture: dict[str, Any]) -> None:
        if self.session_monitor is None:
            return
        if result.get("ok") is False and result.get("blocked"):
            return
        target_name = str(capture.get("target_name") or session.get("target_name") or "")
        reset_key = str(capture.get("session_key") or session.get("session_key") or target_name)
        if reset_key:
            if (
                capture.get("status") == "empty"
                and hasattr(self.session_monitor, "should_preserve_pending_after_empty_capture")
                and self.session_monitor.should_preserve_pending_after_empty_capture(reset_key)
            ):
                self.session_monitor.reset_unread(reset_key, preserve_pending=True)
            else:
                self.session_monitor.reset_unread(reset_key)

    def _plan_reply(self, capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        session_key = str(task.get("session_key") or capture.get("session_key") or "").strip()
        target = self._target_for_session(
            {
                "target_name": str(capture.get("target_name") or task.get("target_name") or ""),
                "exact": bool(capture.get("exact", True)),
                "conversation_type": str(capture.get("conversation_type") or task.get("conversation_type") or "unknown"),
                "session_key": session_key,
            }
        )
        with self._tenant_environment():
            planned = plan_reply_with_listen_workflow(
                capture,
                task,
                target_config=target,
                config=copy.deepcopy(self.config),
                rules=copy.deepcopy(self.rules),
                workflow_state=self._workflow_state_snapshot(),
                allow_fallback_send=bool((self.config.get("reply", {}) or {}).get("allow_fallback_send")),
                apply_final_visible_polish=False,
            )
        event = planned.get("event") if isinstance(planned.get("event"), dict) else {}
        if isinstance(planned.get("decision"), dict) and event:
            planned["decision"] = {
                **planned["decision"],
                "scheduler_planner_event_action": event.get("action"),
                "scheduler_planner_event_reason": event.get("reason"),
            }
        if event:
            planned["event"] = event
        return planned

    def _polish_reply(self, planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        target = self._target_for_session(
            {
                "target_name": str(planner_task.get("target_name") or ""),
                "exact": True,
                "session_key": str(planner_task.get("session_key") or task.get("session_key") or ""),
            }
        )
        with self._tenant_environment():
            polished = polish_reply_with_listen_workflow(
                planner_task,
                task,
                target_config=target,
                config=copy.deepcopy(self.config),
                workflow_state=self._workflow_state_snapshot(),
            )
        event = polished.get("event") if isinstance(polished.get("event"), dict) else {}
        if isinstance(polished.get("decision"), dict) and event:
            polished["decision"] = {
                **polished["decision"],
                "scheduler_polish_event_action": event.get("action"),
                "scheduler_polish_event_reason": event.get("reason"),
            }
        return polished

    def _capture_for_reply(self, reply: dict[str, Any]) -> dict[str, Any] | None:
        inline_capture = reply.get("_capture")
        if isinstance(inline_capture, dict):
            return inline_capture
        capture_ids = [str(item) for item in reply.get("capture_ids", []) if str(item)]
        if not capture_ids:
            return None
        state = self.store.load()
        capture = (state.get("captures", {}) or {}).get(capture_ids[-1])
        return capture if isinstance(capture, dict) else None

    def _scheduler_freshness_settings(self) -> dict[str, Any]:
        source = self.config.get("scheduler_freshness") if isinstance(self.config.get("scheduler_freshness"), dict) else {}
        mode = str(source.get("mode") or "preview_first").strip().lower()
        if mode not in {"preview_first", "strict_only"}:
            mode = "preview_first"
        raw_interval = source.get("strict_check_interval_seconds")
        try:
            strict_interval = int(raw_interval) if raw_interval not in (None, "") else 180
        except (TypeError, ValueError):
            strict_interval = 180
        raw_long_llm = source.get("strict_check_after_llm_seconds")
        try:
            strict_after_llm = int(raw_long_llm) if raw_long_llm not in (None, "") else 90
        except (TypeError, ValueError):
            strict_after_llm = 90
        strict_on_first_send = source.get("strict_check_on_first_send")
        if strict_on_first_send is None:
            strict_on_first_send = False
        preview_from_session_list_enabled = source.get("preview_from_session_list_enabled")
        if preview_from_session_list_enabled is None:
            preview_from_session_list_enabled = True
        raw_preview_cache_seconds = source.get("preview_from_session_list_cache_seconds")
        try:
            preview_cache_seconds = float(raw_preview_cache_seconds) if raw_preview_cache_seconds not in (None, "") else 2.5
        except (TypeError, ValueError):
            preview_cache_seconds = 2.5
        preview_cache_seconds = max(0.0, min(20.0, preview_cache_seconds))
        preview_require_content_match = source.get("preview_from_session_list_require_content_match")
        if preview_require_content_match is None:
            # Default to false to avoid expensive strict rescans on harmless
            # preview-text drift (truncation, timestamp wrappers, etc.).
            # Strict scan is still enforced by interval/long-LLM guardrails.
            preview_require_content_match = False
        return {
            "enabled": source.get("enabled", True) is not False,
            "mode": mode,
            "strict_check_interval_seconds": max(0, strict_interval),
            "strict_check_after_llm_seconds": max(0, strict_after_llm),
            "strict_check_on_first_send": bool(strict_on_first_send),
            "preview_from_session_list_enabled": bool(preview_from_session_list_enabled),
            "preview_from_session_list_cache_seconds": preview_cache_seconds,
            "preview_from_session_list_require_content_match": bool(preview_require_content_match),
        }

    @staticmethod
    def _iso_to_timestamp(value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _session_monitor_snapshot_for_target(self, target_name: str, *, session_key: str = "") -> dict[str, Any] | None:
        monitor = self.session_monitor
        if monitor is None or not hasattr(monitor, "all_sessions"):
            return None
        try:
            sessions = monitor.all_sessions()  # type: ignore[call-arg]
        except Exception:
            return None
        if not isinstance(sessions, list):
            return None
        clean_key = str(session_key or "").strip()
        if clean_key:
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                if str(item.get("session_key") or "").strip() == clean_key:
                    return item
            return None
        matches: list[dict[str, Any]] = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() != target_name:
                continue
            matches.append(item)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _compact_preview_text(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).strip().lower()

    def _target_name_matches_preview(self, target_name: str, preview_name: str) -> bool:
        left = self._compact_preview_text(target_name)
        right = self._compact_preview_text(preview_name)
        if not left or not right:
            return False
        return left == right

    def _session_list_preview_for_target(self, target_name: str, *, cache_seconds: float, session_key: str = "") -> dict[str, Any] | None:
        now_ts = time.time()
        clean_session_key = str(session_key or "").strip()
        cache_key = clean_session_key or str(target_name or "").strip()
        cached = self._freshness_session_list_preview_cache.get(cache_key)
        if isinstance(cached, dict):
            cached_at = float(cached.get("cached_at") or 0.0)
            if cache_seconds > 0 and cached_at > 0 and (now_ts - cached_at) <= cache_seconds:
                preview = cached.get("preview")
                if isinstance(preview, dict):
                    return preview
        try:
            payload = self.connector.list_sessions()
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return None
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            return None
        matches: list[dict[str, Any]] = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("session_key") or "").strip()
            if clean_session_key:
                if item_key != clean_session_key:
                    continue
            else:
                name = str(item.get("name") or "").strip()
                if not self._target_name_matches_preview(target_name, name):
                    continue
            matches.append(item)
        if len(matches) != 1:
            return None
        item = matches[0]
        name = str(item.get("name") or "").strip()
        unread = bool(
            item.get("unread_detected")
            or item.get("unread_signal")
            or item.get("unread")
            or item.get("unread_badge")
        )
        preview = {
            "name": name,
            "session_key": str(item.get("session_key") or clean_session_key or ""),
            "unread_detected": unread,
            "last_detected_at": "",
            "pending_since": "",
            "last_message_time": str(item.get("time") or ""),
            "preview_content": str(item.get("content") or ""),
            "_source": "session_list",
        }
        self._freshness_session_list_preview_cache[cache_key] = {"cached_at": now_ts, "preview": preview}
        return preview

    def _session_list_preview_matches_capture(self, reply: dict[str, Any], preview: dict[str, Any]) -> bool:
        preview_content = self._compact_preview_text(
            preview.get("pending_signal_text") or preview.get("preview_content")
        )
        if not preview_content:
            return False
        capture = self._capture_for_reply(reply)
        if not isinstance(capture, dict):
            return False
        batch = [item for item in (capture.get("batch") or []) if isinstance(item, dict)]
        if not batch:
            return False
        latest_content = ""
        for item in reversed(batch):
            content = str(item.get("content") or "").strip()
            if content:
                latest_content = content
                break
        if not latest_content:
            return False
        original = self._compact_preview_text(latest_content)
        if not original:
            return False
        if preview_content in original or original in preview_content:
            return True
        spans = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,12}", original)
        for token in spans:
            if token and token in preview_content:
                return True
        return False

    @staticmethod
    def _latest_customer_message_content(batch: list[dict[str, Any]]) -> str:
        for item in reversed(batch):
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender") or "").strip().lower()
            if sender in {"self", "assistant", "agent", "me", "outbound"}:
                continue
            content = str(item.get("content") or "").strip()
            if content:
                return content
        for item in reversed(batch):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if content:
                return content
        return ""

    def _preview_unread_requires_strict_same_signal_scan(self, reply: dict[str, Any], preview: dict[str, Any]) -> bool:
        if str(preview.get("pending_signal_kind") or "").strip() != "high_sensitivity_short":
            return False
        capture = self._capture_for_reply(reply)
        if not isinstance(capture, dict):
            return False
        batch = [item for item in (capture.get("batch") or []) if isinstance(item, dict)]
        if not batch:
            return False
        preview_text = str(preview.get("pending_signal_text") or preview.get("preview_content") or "").strip()
        if not preview_text:
            return False
        latest_content = self._latest_customer_message_content(batch)
        if not latest_content:
            return False
        return normalize_repeatable_probe_text(preview_text) == normalize_repeatable_probe_text(latest_content)

    def _strict_freshness_observation_is_corroborated(
        self,
        *,
        target_name: str,
        reply: dict[str, Any],
        freshness: dict[str, Any],
    ) -> bool:
        """Return whether strict OCR freshness has enough state support to stale.

        Strict message scans are useful as a fallback, but raw OCR fragments alone
        are weaker than scheduler/session-ledger state.  Require corroboration for
        short/noisy observations so a one-character OCR artifact cannot discard a
        Brain-authored ready reply.
        """

        if not isinstance(freshness, dict):
            return False
        reason = str(freshness.get("reason") or "").strip()
        if reason in {"original_batch_not_visible_assume_stale", "original_batch_not_found_gap_risk"} or bool(freshness.get("gap_risk")):
            session_key = str(reply.get("session_key") or "")
            preview = self._session_monitor_snapshot_for_target(target_name, session_key=session_key)
            if not isinstance(preview, dict):
                preview = self._session_list_preview_for_target(
                    target_name,
                    cache_seconds=float(self._scheduler_freshness_settings().get("preview_from_session_list_cache_seconds") or 0.0),
                    session_key=session_key,
                )
            if isinstance(preview, dict) and bool(preview.get("unread_detected")):
                if self._session_list_preview_matches_capture(reply, preview):
                    return False
                return True
            try:
                scheduler_state = self.store.load()
            except Exception:
                scheduler_state = {}
            session = (
                get_session_by_identity(scheduler_state, target_name, session_key=str(reply.get("session_key") or ""))
                if isinstance(scheduler_state, dict)
                else None
            ) or {}
            if isinstance(session, dict) and bool(session.get("pending_capture")):
                if isinstance(preview, dict) and self._session_list_preview_matches_capture(reply, preview):
                    return False
                return True
            return False
        newer = [item for item in (freshness.get("newer_messages") or []) if isinstance(item, dict)]
        if not newer:
            return bool(freshness.get("has_newer_messages"))
        session_key = str(reply.get("session_key") or "")
        preview = self._session_monitor_snapshot_for_target(target_name, session_key=session_key)
        if not isinstance(preview, dict):
            preview = self._session_list_preview_for_target(
                target_name,
                cache_seconds=float(self._scheduler_freshness_settings().get("preview_from_session_list_cache_seconds") or 0.0),
                session_key=session_key,
            )
        if isinstance(preview, dict) and bool(preview.get("unread_detected")):
            return True
        try:
            scheduler_state = self.store.load()
        except Exception:
            scheduler_state = {}
        session = (
            get_session_by_identity(scheduler_state, target_name, session_key=str(reply.get("session_key") or ""))
            if isinstance(scheduler_state, dict)
            else None
        ) or {}
        if isinstance(session, dict) and bool(session.get("pending_capture")):
            return True
        capture = self._capture_for_reply(reply)
        captured_texts = {
            normalize_repeatable_probe_text(str(item.get("content") or ""))
            for item in ((capture or {}).get("batch") or [])
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        }
        preview_text = ""
        if isinstance(preview, dict):
            preview_text = str(preview.get("pending_signal_text") or preview.get("preview_content") or "").strip()
        compact_preview = normalize_repeatable_probe_text(preview_text)
        for item in newer:
            content = str(item.get("content") or "").strip()
            compact = normalize_repeatable_probe_text(content)
            if not compact:
                continue
            if compact_preview and (compact in compact_preview or compact_preview in compact):
                return True
            if compact in captured_texts:
                return False
            if len(compact) > 7:
                return True
        return False

    def _soft_pass_unconfirmed_strict_freshness(self, freshness: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(freshness)
        result["ok"] = bool(result.get("ok", True))
        result["stale"] = False
        result["has_newer_messages"] = False
        result["gap_risk"] = False
        result["freshness_mode"] = "strict_message_scan_ledger_guard"
        result["reason"] = "strict_freshness_unconfirmed_ocr_observation"
        result["strict_freshness_observation"] = copy.deepcopy(freshness)
        return result

    def _reply_llm_elapsed_seconds(self, reply: dict[str, Any]) -> float:
        cached = reply.get("_llm_elapsed_seconds")
        if cached not in (None, ""):
            try:
                return max(0.0, float(cached))
            except (TypeError, ValueError):
                pass
        task_id = str(reply.get("task_id") or "").strip()
        if not task_id:
            return 0.0
        try:
            state = self.store.load()
        except Exception:
            return 0.0
        task = (state.get("llm_tasks", {}) or {}).get(task_id)
        if isinstance(task, dict):
            return self._task_llm_elapsed_seconds(task)
        polish_task = (state.get("polish_tasks", {}) or {}).get(task_id)
        if not isinstance(polish_task, dict):
            return 0.0
        elapsed = self._task_llm_elapsed_seconds(polish_task)
        planner_task_id = str(polish_task.get("planner_task_id") or "").strip()
        if planner_task_id:
            planner_task = (state.get("llm_tasks", {}) or {}).get(planner_task_id)
            if isinstance(planner_task, dict):
                elapsed += self._task_llm_elapsed_seconds(planner_task)
        return elapsed

    def _task_llm_elapsed_seconds(self, task: dict[str, Any]) -> float:
        started_ts = self._iso_to_timestamp(str(task.get("started_at") or ""))
        if started_ts is None:
            return 0.0
        finished_ts = self._iso_to_timestamp(str(task.get("finished_at") or ""))
        if finished_ts is not None and finished_ts >= started_ts:
            return max(0.0, finished_ts - started_ts)
        return max(0.0, time.time() - started_ts)

    def _preview_freshness_fastpath(
        self,
        *,
        reply: dict[str, Any],
        target_name: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        if settings.get("enabled") is not True:
            return {"applied": False, "reason": "scheduler_freshness_disabled"}
        if str(settings.get("mode") or "preview_first") == "strict_only":
            return {"applied": False, "reason": "scheduler_freshness_strict_only"}

        try:
            scheduler_state = self.store.load()
        except Exception:
            scheduler_state = {}
        session = get_session_by_identity(scheduler_state, target_name, session_key=str(reply.get("session_key") or "")) or {}
        session_key = str(reply.get("session_key") or "")
        preview = self._session_monitor_snapshot_for_target(target_name, session_key=session_key)
        if (
            not isinstance(preview, dict)
            and bool(settings.get("preview_from_session_list_enabled", True))
        ):
            preview = self._session_list_preview_for_target(
                target_name,
                cache_seconds=float(settings.get("preview_from_session_list_cache_seconds") or 0.0),
                session_key=session_key,
            )
        # Scheduler-level pending signal is authoritative only when it points to
        # work newer than the captured batch.  RPA/OCR can leave the same unread
        # preview pending while the reply is already ready; in that case sending
        # is safe and avoids an infinite stale/requeue loop.
        if isinstance(session, dict) and bool(session.get("pending_capture")):
            if isinstance(preview, dict) and self._session_list_preview_matches_capture(reply, preview):
                return {
                    "applied": True,
                    "ok": True,
                    "stale": False,
                    "has_newer_messages": False,
                    "reason": "scheduler_pending_capture_matches_capture_fast_pass",
                    "freshness_mode": "session_preview_fastpath",
                    "preview_snapshot": {
                        "last_detected_at": str(preview.get("last_detected_at") or ""),
                        "pending_since": str(preview.get("pending_since") or ""),
                        "last_message_time": str(preview.get("last_message_time") or ""),
                    },
                }
            return {
                "applied": True,
                "ok": True,
                "stale": True,
                "has_newer_messages": True,
                "reason": "scheduler_pending_capture_before_send",
                "freshness_mode": "session_preview_fastpath",
            }
        if not isinstance(preview, dict):
            return {"applied": False, "reason": "session_preview_unavailable"}
        if bool(preview.get("unread_detected")):
            if self._session_list_preview_matches_capture(reply, preview):
                return {
                    "applied": True,
                    "ok": True,
                    "stale": False,
                    "has_newer_messages": False,
                    "reason": "session_monitor_unread_matches_capture_fast_pass",
                    "freshness_mode": "session_preview_fastpath",
                    "preview_snapshot": {
                        "last_detected_at": str(preview.get("last_detected_at") or ""),
                        "pending_since": str(preview.get("pending_since") or ""),
                        "last_message_time": str(preview.get("last_message_time") or ""),
                    },
                }
            return {"applied": False, "reason": "session_monitor_unread_requires_strict_scan"}

        if (
            str(preview.get("_source") or "") == "session_list"
            and bool(settings.get("preview_from_session_list_require_content_match", True))
            and not self._session_list_preview_matches_capture(reply, preview)
        ):
            return {"applied": False, "reason": "session_list_preview_content_mismatch"}

        strict_interval = int(settings.get("strict_check_interval_seconds") or 0)
        if strict_interval > 0:
            last_strict = float(self._freshness_last_strict_at_by_target.get(target_name) or 0.0)
            if last_strict <= 0.0:
                if bool(settings.get("strict_check_on_first_send")):
                    return {"applied": False, "reason": "strict_first_send_due"}
            elif (time.time() - last_strict) >= float(strict_interval):
                return {"applied": False, "reason": "strict_interval_due"}

        strict_after_llm = int(settings.get("strict_check_after_llm_seconds") or 0)
        llm_elapsed = self._reply_llm_elapsed_seconds(reply)
        if strict_after_llm > 0 and llm_elapsed >= float(strict_after_llm):
            return {"applied": False, "reason": "strict_due_to_long_llm", "llm_elapsed_seconds": round(llm_elapsed, 3)}

        return {
            "applied": True,
            "ok": True,
            "stale": False,
            "has_newer_messages": False,
            "reason": (
                "session_list_preview_no_unread_fast_pass"
                if str(preview.get("_source") or "") == "session_list"
                else "session_preview_no_unread_fast_pass"
            ),
            "freshness_mode": "session_preview_fastpath",
            "preview_snapshot": {
                "last_detected_at": str(preview.get("last_detected_at") or ""),
                "pending_since": str(preview.get("pending_since") or ""),
                "last_message_time": str(preview.get("last_message_time") or ""),
            },
        }

    def _freshness_check(self, reply: dict[str, Any]) -> dict[str, Any]:
        capture = self._capture_for_reply(reply)
        if not capture:
            return {"ok": False, "stale": True, "reason": "capture_missing_before_send"}
        target = self._target_for_name(str(reply.get("target_name") or capture.get("target_name") or ""), exact=bool(capture.get("exact", True)))
        freshness_settings = self._scheduler_freshness_settings()
        fastpath = self._preview_freshness_fastpath(
            reply=reply,
            target_name=str(target.name),
            settings=freshness_settings,
        )
        if fastpath.get("applied"):
            return {
                "ok": True,
                "stale": bool(fastpath.get("stale") or fastpath.get("has_newer_messages")),
                "has_newer_messages": bool(fastpath.get("has_newer_messages")),
                "reason": str(fastpath.get("reason") or "session_preview_fastpath"),
                "freshness_mode": str(fastpath.get("freshness_mode") or "session_preview_fastpath"),
                "preview_snapshot": fastpath.get("preview_snapshot"),
                "llm_elapsed_seconds": fastpath.get("llm_elapsed_seconds"),
            }
        workflow_state = self._workflow_state_snapshot()
        target_state = self._target_state(
            workflow_state,
            target.name,
            session_key=str(reply.get("session_key") or capture.get("session_key") or getattr(target, "session_key", "") or ""),
        )
        freshness = self._workflow["detect_newer_messages_before_send"](
            connector=self.connector,
            target=target,
            target_state=target_state,
            batch=list(capture.get("batch") or []),
            config=self.config,
        )
        self._freshness_last_strict_at_by_target[str(target.name)] = time.time()
        freshness["freshness_mode"] = "strict_message_scan"
        if (
            bool(freshness.get("has_newer_messages") or freshness.get("gap_risk"))
            and not self._strict_freshness_observation_is_corroborated(
                target_name=str(target.name),
                reply=reply,
                freshness=freshness,
            )
        ):
            return self._soft_pass_unconfirmed_strict_freshness(freshness)
        freshness["stale"] = bool(freshness.get("has_newer_messages") or freshness.get("gap_risk"))
        return freshness

    def _send_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        send_reply_started_at = time.time()
        send_reply_started_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(send_reply_started_at))

        def _finish(payload: dict[str, Any]) -> dict[str, Any]:
            finished_at = time.time()
            if isinstance(payload, dict):
                timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
                payload["timing"] = {
                    **timing,
                    "send_reply_started_at": send_reply_started_iso,
                    "send_reply_finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(finished_at)),
                    "send_reply_duration_seconds": round(max(0.0, finished_at - send_reply_started_at), 4),
                }
            return payload

        capture = self._capture_for_reply(reply)
        if not capture:
            return _finish({"ok": False, "verified": False, "reason": "capture_missing_before_send"})
        envelope_failure = ready_reply_session_envelope_failure(reply, capture)
        reply_target_name = str(reply.get("target_name") or "").strip()
        capture_target_name = str(capture.get("target_name") or "").strip()
        if envelope_failure:
            return _finish({
                "ok": False,
                "verified": False,
                "reason": envelope_failure,
                "target_name": reply_target_name,
                "capture_target_name": capture_target_name,
                "session_key": reply.get("session_key"),
                "capture_session_key": capture.get("session_key"),
                "message_content_digest": reply.get("message_content_digest"),
                "capture_message_content_digest": capture.get("message_content_digest"),
                "capture_ids": reply.get("capture_ids"),
            })
        target = self._target_for_session(
            {
                "target_name": reply_target_name,
                "exact": bool(capture.get("exact", True)),
                "session_key": str(reply.get("session_key") or capture.get("session_key") or ""),
            }
        )
        reply_text = str(reply.get("reply_text") or "").strip()
        if not reply_text:
            return _finish({"ok": False, "verified": False, "reason": "empty_reply_text"})
        ownership_failure = brain_first_ready_reply_ownership_failure(reply)
        if ownership_failure:
            return _finish({"ok": False, "verified": False, "reason": ownership_failure})
        verified = self._workflow["send_reply_with_optional_multi_bubble"](
            connector=self.connector,
            target=target,
            reply_text=reply_text,
            config=self.config,
        )
        if not verified.get("verified"):
            return _finish({"ok": False, "verified": False, "reason": "send_not_verified", "send_result": verified})
        batch = list(capture.get("batch") or [])
        overflow = list(capture.get("overflow_messages") or [])
        reply_trace_id = self._workflow["build_reply_trace_id"](target.name, batch, reply_text)
        post_send_warning = ""
        if self.state_path is not None:
            try:
                lock_settings = self.config.get("state_lock", {}) if isinstance(self.config.get("state_lock"), dict) else {}
                with self._workflow["StateLock"](
                    self.state_path.with_suffix(self.state_path.suffix + ".lock"),
                    timeout_seconds=int(lock_settings.get("timeout_seconds", 120)),
                    stale_seconds=int(lock_settings.get("stale_seconds", 900)),
                ):
                    workflow_state = self._workflow["load_state"](self.state_path)
                    target_state = self._target_state(
                        workflow_state,
                        target.name,
                        session_key=str(reply.get("session_key") or capture.get("session_key") or getattr(target, "session_key", "") or ""),
                    )
                    self._workflow["mark_processed"](
                        target_state,
                        batch,
                        reply_text,
                        reply_trace_id=reply_trace_id,
                        send_result=verified,
                    )
                    self._workflow["mark_coalesced_messages"](
                        target_state,
                        overflow,
                        reply_trace_id=reply_trace_id,
                        reply_text=reply_text,
                        reason="scheduler_overflow_coalesced_after_customer_reply",
                    )
                    self._workflow["record_reply_timestamp"](target_state)
                    self._workflow["save_state"](self.state_path, workflow_state)
            except Exception as exc:  # noqa: BLE001 - never retry an already verified WeChat send
                post_send_warning = f"post_send_state_persist_failed: {exc!r}"
        decision_payload = reply.get("decision") if isinstance(reply.get("decision"), dict) else {}
        audit_event = {
            "ok": True,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "target": target.name,
            "action": "sent",
            "scheduler": {
                "enabled": True,
                "reply_id": reply.get("reply_id"),
                "task_id": reply.get("task_id"),
                "task_kind": reply.get("task_kind") or "planner",
                "capture_ids": reply.get("capture_ids"),
                "context_version": reply.get("input_context_version"),
            },
            "message_ids": list(reply.get("input_message_ids") or []),
            "message_count": len(batch),
            "decision": {**decision_payload, "reply_text": reply_text},
            "send_result": verified,
            "verified": True,
        }
        for key in ("outbound_naturalness", "final_visible_llm_polish", "final_visible_llm_polish_degraded"):
            if key in decision_payload:
                audit_event[key] = decision_payload.get(key)
        if post_send_warning:
            audit_event["post_send_warning"] = post_send_warning
        if self.audit_path is not None:
            try:
                self._workflow["append_audit"](self.audit_path, audit_event)
            except Exception as exc:  # noqa: BLE001
                post_send_warning = f"{post_send_warning}; audit_append_failed: {exc!r}".strip("; ")
        return _finish({
            "ok": True,
            "verified": True,
            "reply_trace_id": reply_trace_id,
            "send_result": verified,
            "audit_event": audit_event,
            "post_send_warning": post_send_warning,
        })
