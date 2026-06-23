"""Live-test safety guard for the WeChat customer-service listener.

This guard is intentionally opt-in through ``live_safety_guard.enabled``.
Production configurations that do not set the flag keep their existing
behavior, while risky live smoke tests can fail closed before any RPA action.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import Any


FILE_TRANSFER_ASSISTANT_NAME = "".join(chr(c) for c in [0x6587, 0x4EF6, 0x4F20, 0x8F93, 0x52A9, 0x624B])


class CustomerServiceLiveSafetyError(RuntimeError):
    """Raised when an opt-in live-safety guard rejects a listener config."""

    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        reasons = ", ".join(str(item) for item in summary.get("fail_reasons", []) or []) or "live_safety_guard_failed"
        super().__init__(f"live safety guard failed: {reasons}")


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _name(value: Any) -> str:
    return str(value or "").strip()


def _name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in value:
        name = _name(raw)
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def _guard_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    guard = cfg.get("live_safety_guard")
    if not isinstance(guard, dict) or not _truthy(guard.get("enabled"), default=False):
        return {}
    return guard


def live_safety_guard_enabled(config: dict[str, Any] | None) -> bool:
    return bool(_guard_settings(config))


def _enabled_config_targets(config: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw in (config or {}).get("targets", []) or []:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        name = _name(raw.get("name") or raw.get("target_name"))
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def _settings_targets(settings: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(settings, dict):
        return []
    return [dict(item) for item in settings.get("session_targets", []) or [] if isinstance(item, dict)]


def _enabled_settings_targets(settings: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw in _settings_targets(settings):
        if not bool(raw.get("enabled", False)) or bool(raw.get("archived", False)):
            continue
        name = _name(raw.get("name") or raw.get("target_name") or raw.get("display_name"))
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def _all_known_target_names(config: dict[str, Any] | None, settings: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in _enabled_config_targets(config):
        if name not in seen:
            names.append(name)
            seen.add(name)
    for raw in _settings_targets(settings):
        name = _name(raw.get("name") or raw.get("target_name") or raw.get("display_name"))
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _conversation_type_by_name(settings: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in _settings_targets(settings):
        name = _name(raw.get("name") or raw.get("target_name") or raw.get("display_name"))
        if not name:
            continue
        result[name] = _name(raw.get("conversation_type")).lower()
    return result


def evaluate_customer_service_live_safety_guard(
    config: dict[str, Any] | None,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed summary for an opt-in live-safety guard."""

    guard = _guard_settings(config)
    if not guard:
        return {"enabled": False, "ok": True, "fail_reasons": []}

    allowed_targets = _name_list(guard.get("allowed_targets") or guard.get("targets"))
    allowed_set = set(allowed_targets)
    managed = bool((settings or {}).get("session_targets_managed", False))
    config_enabled = _enabled_config_targets(config)
    settings_enabled = _enabled_settings_targets(settings)
    effective_enabled = settings_enabled if managed else config_enabled
    known_names = _all_known_target_names(config, settings)
    conversation_types = _conversation_type_by_name(settings)
    respond_all = bool((settings or {}).get("respond_all_unread_sessions", False))
    routing = (config or {}).get("_local_customer_service_session_routing")
    if isinstance(routing, dict):
        respond_all = respond_all or bool(routing.get("respond_all_unread_sessions", False))

    fail_reasons: list[str] = []
    disallowed_enabled = [name for name in effective_enabled if name not in allowed_set]
    missing_required = [name for name in allowed_targets if name not in set(effective_enabled)]
    group_targets = [
        name
        for name in effective_enabled
        if name in allowed_set and conversation_types.get(name) == "group"
    ]

    if not allowed_targets:
        fail_reasons.append("allowed_targets_missing")
    if disallowed_enabled:
        fail_reasons.append("disallowed_enabled_targets")
    if _truthy(guard.get("require_exact_targets"), default=True) and missing_required:
        fail_reasons.append("required_allowed_targets_not_enabled")
    if _truthy(guard.get("disable_respond_all_unread_sessions"), default=True) and respond_all:
        fail_reasons.append("respond_all_unread_sessions_enabled")
    if _truthy(guard.get("reject_group_targets"), default=True) and group_targets:
        fail_reasons.append("group_target_enabled")

    return {
        "enabled": True,
        "ok": not fail_reasons,
        "fail_reasons": fail_reasons,
        "allowed_targets": allowed_targets,
        "effective_enabled_targets": effective_enabled,
        "config_enabled_targets": config_enabled,
        "settings_enabled_targets": settings_enabled,
        "known_targets": known_names,
        "disallowed_enabled_targets": disallowed_enabled,
        "missing_required_targets": missing_required,
        "group_targets": group_targets,
        "respond_all_unread_sessions": respond_all,
        "managed_session_targets": managed,
    }


def assert_customer_service_live_safety_guard(
    config: dict[str, Any] | None,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = evaluate_customer_service_live_safety_guard(config, settings=settings)
    if summary.get("enabled") and not summary.get("ok"):
        raise CustomerServiceLiveSafetyError(summary)
    return summary


def _parse_iso_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except (OSError, ValueError):
        return 0.0


def evaluate_customer_service_recent_bootstrap_guard(
    config: dict[str, Any] | None,
    *,
    state: dict[str, Any] | None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    guard = _guard_settings(config)
    if not guard or not _truthy(guard.get("require_recent_bootstrap"), default=False):
        return {"enabled": False, "ok": True, "fail_reasons": []}

    allowed_targets = _name_list(guard.get("allowed_targets") or guard.get("targets"))
    max_age_seconds = int(guard.get("bootstrap_max_age_seconds") or 900)
    allow_pending_visible = _truthy(guard.get("allow_pending_visible_bootstrap"), default=True)
    tick = float(now_ts if now_ts is not None else time.time())
    targets_state = (state or {}).get("targets") if isinstance((state or {}).get("targets"), dict) else {}
    missing: list[str] = []
    stale: list[str] = []
    bootstrapped: dict[str, str] = {}
    pending_visible: dict[str, str] = {}
    for target in allowed_targets:
        target_state = targets_state.get(target) if isinstance(targets_state, dict) else {}
        pending_at = ""
        pending_ts = 0.0
        if allow_pending_visible and isinstance(target_state, dict):
            pending = target_state.get("bootstrap_pending_visible")
            if isinstance(pending, dict):
                pending_at = str(pending.get("created_at") or "")
                pending_ts = _parse_iso_timestamp(pending_at)
                if pending_ts and max_age_seconds > 0 and tick - pending_ts > max_age_seconds:
                    pending_at = ""
                    pending_ts = 0.0
                if pending_ts:
                    pending_visible[target] = pending_at
        events = target_state.get("bootstrap_events", []) if isinstance(target_state, dict) else []
        latest_at = ""
        latest_ts = 0.0
        for raw in events or []:
            if not isinstance(raw, dict):
                continue
            created_at = str(raw.get("created_at") or "")
            created_ts = _parse_iso_timestamp(created_at)
            if created_ts >= latest_ts:
                latest_ts = created_ts
                latest_at = created_at
        if not latest_ts:
            if pending_ts:
                continue
            missing.append(target)
            continue
        bootstrapped[target] = latest_at
        if max_age_seconds > 0 and tick - latest_ts > max_age_seconds:
            if pending_ts and pending_ts >= latest_ts:
                continue
            stale.append(target)

    fail_reasons: list[str] = []
    if missing:
        fail_reasons.append("recent_bootstrap_missing")
    if stale:
        fail_reasons.append("recent_bootstrap_stale")
    return {
        "enabled": True,
        "ok": not fail_reasons,
        "fail_reasons": fail_reasons,
        "allowed_targets": allowed_targets,
        "max_age_seconds": max_age_seconds,
        "allow_pending_visible_bootstrap": allow_pending_visible,
        "bootstrapped_targets": bootstrapped,
        "pending_visible_targets": pending_visible,
        "pending_visible_message": "当前不在可见会话列表里，待收到新消息时会自动识别" if pending_visible else "",
        "missing_targets": missing,
        "stale_targets": stale,
    }


def assert_customer_service_recent_bootstrap_guard(
    config: dict[str, Any] | None,
    *,
    state: dict[str, Any] | None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    summary = evaluate_customer_service_recent_bootstrap_guard(config, state=state, now_ts=now_ts)
    if summary.get("enabled") and not summary.get("ok"):
        raise CustomerServiceLiveSafetyError(summary)
    return summary


def apply_customer_service_live_safety_guard(
    config: dict[str, Any],
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply low-risk RPA runtime constraints after local settings are merged."""

    merged = copy.deepcopy(config)
    guard = _guard_settings(merged)
    if not guard:
        return merged

    summary = assert_customer_service_live_safety_guard(merged, settings=settings)
    allowed_targets = summary.get("allowed_targets", []) or []
    allowed_set = set(allowed_targets)

    by_name: dict[str, dict[str, Any]] = {}
    for raw in merged.get("targets", []) or []:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        name = _name(raw.get("name") or raw.get("target_name"))
        if name in allowed_set and name not in by_name:
            target = copy.deepcopy(raw)
            target["name"] = name
            target["enabled"] = True
            target["exact"] = bool(target.get("exact", True))
            if name != FILE_TRANSFER_ASSISTANT_NAME:
                target["allow_self_for_test"] = False
            else:
                target["allow_self_for_test"] = True
                try:
                    self_test_batch = int(target.get("max_batch_messages") or 0)
                except (TypeError, ValueError):
                    self_test_batch = 0
                target["max_batch_messages"] = max(self_test_batch, 8)
            by_name[name] = target
    merged["targets"] = [by_name[name] for name in allowed_targets if name in by_name]

    routing = dict(merged.get("_local_customer_service_session_routing", {}) or {})
    routing["respond_all_unread_sessions"] = False
    routing["enabled_names"] = list(allowed_targets)
    ignored = {
        _name(item)
        for item in routing.get("ignored_names", []) or []
        if _name(item)
    }
    ignored.update(name for name in summary.get("known_targets", []) or [] if name not in allowed_set)
    if FILE_TRANSFER_ASSISTANT_NAME not in allowed_set:
        ignored.add(FILE_TRANSFER_ASSISTANT_NAME)
    routing["ignored_names"] = sorted(ignored)
    merged["_local_customer_service_session_routing"] = routing

    if _truthy(guard.get("low_risk_single_target_scan"), default=True):
        allowed_target_count = len(allowed_targets)
        multi_target = dict(merged.get("multi_target", {}) or {})
        if allowed_target_count <= 1:
            multi_target.update(
                {
                    "enabled": False,
                    "rpa_low_risk_mode": True,
                    "scan_all_whitelist_each_iteration": False,
                    "max_scan_targets_per_iteration": 0,
                    "idle_whitelist_sweep_count": 0,
                    "max_targets_per_iteration": 1,
                    "dispatch_strategy": "event_driven",
                    "sticky_target_hold_seconds": max(int(multi_target.get("sticky_target_hold_seconds") or 0), 35),
                    "preview_change_confirmations": max(int(multi_target.get("preview_change_confirmations") or 0), 1),
                    "initial_preview_can_raise_unread": False,
                    "preview_change_can_raise_unread": False,
                    "short_preview_can_raise_unread": True,
                    "require_unread_badge_for_dispatch": True,
                    "require_preview_signal_with_unread_badge": True,
                    "change_warmup_enabled": False,
                    "change_warmup_min_seconds": 0.0,
                    "change_warmup_max_seconds": 0.0,
                }
            )
            # Single-target mode does not need a long hard switch gate. Keep the
            # baseline aligned with multi-session mode so old persisted configs
            # cannot carry a stale 25s interval back into later runs.
            multi_target["min_switch_interval_seconds"] = 1
        else:
            # In multi-session mode, prefer unread-driven dispatch only.
            # Avoid full-whitelist scans that create visible mechanical
            # cross-chat hopping under RPA.
            multi_target.update(
                {
                    "enabled": True,
                    "rpa_low_risk_mode": True,
                    "scan_all_whitelist_each_iteration": False,
                    "max_scan_targets_per_iteration": 0,
                    "idle_whitelist_sweep_count": 0,
                    "max_targets_per_iteration": 2,
                    "dispatch_strategy": "event_driven",
                    "sticky_target_hold_seconds": max(int(multi_target.get("sticky_target_hold_seconds") or 0), 30),
                    "preview_change_confirmations": max(int(multi_target.get("preview_change_confirmations") or 0), 2),
                    "initial_preview_can_raise_unread": False,
                    "preview_change_can_raise_unread": False,
                    "short_preview_can_raise_unread": True,
                    "require_unread_badge_for_dispatch": True,
                    "require_preview_signal_with_unread_badge": True,
                    "change_warmup_enabled": False,
                    "change_warmup_min_seconds": 0.0,
                    "change_warmup_max_seconds": 0.0,
                    "switch_human_delay_enabled": True,
                    "switch_human_delay_min_seconds": 1.0,
                    "switch_human_delay_max_seconds": 3.0,
                    "capture_one_target_per_round": False,
                }
            )
            # No hard long switch interval: unread-driven dispatch plus a short
            # humanized switch delay is safer and keeps response latency stable.
            multi_target["min_switch_interval_seconds"] = 1
        merged["multi_target"] = multi_target

    scheduler = dict(merged.get("concurrency_scheduler", {}) or {})
    if _truthy(guard.get("backend_state_scheduler_enabled"), default=True):
        if scheduler.get("enabled") is not False:
            def bounded_scheduler_int(name: str, default: int, minimum: int, maximum: int) -> int:
                try:
                    value = int(scheduler.get(name) or default)
                except (TypeError, ValueError):
                    value = default
                return min(max(value, minimum), maximum)

            scheduler.update(
                {
                    "enabled": True,
                    "capture_max_sessions_per_round": bounded_scheduler_int("capture_max_sessions_per_round", 3, 1, 5),
                    "llm_max_concurrency": bounded_scheduler_int("llm_max_concurrency", 4, 1, 8),
                    "planner_max_concurrency": bounded_scheduler_int(
                        "planner_max_concurrency",
                        bounded_scheduler_int("llm_max_concurrency", 4, 1, 8),
                        1,
                        8,
                    ),
                    "polish_max_concurrency": bounded_scheduler_int(
                        "polish_max_concurrency",
                        bounded_scheduler_int("llm_max_concurrency", 4, 1, 8),
                        1,
                        8,
                    ),
                    "send_max_replies_per_round": bounded_scheduler_int("send_max_replies_per_round", 1, 1, 2),
                    "same_session_single_inflight": scheduler.get("same_session_single_inflight", True) is not False,
                    "stale_reply_policy": str(scheduler.get("stale_reply_policy") or "discard_and_requeue"),
                    "pending_session_ttl_seconds": bounded_scheduler_int("pending_session_ttl_seconds", 1800, 300, 7200),
                    "reply_ready_ttl_seconds": bounded_scheduler_int("reply_ready_ttl_seconds", 900, 120, 3600),
                    "max_pending_sessions": bounded_scheduler_int("max_pending_sessions", 30, 5, 100),
                    "max_pending_messages_per_session": bounded_scheduler_int(
                        "max_pending_messages_per_session",
                        80,
                        10,
                        200,
                    ),
                }
            )
            merged["concurrency_scheduler"] = scheduler

    poll = dict(merged.get("poll", {}) or {})
    # Keep live customer-service response snappy while avoiding clock-like loops.
    # The managed listener samples a fresh value inside this 3-5s window.
    poll_interval = int(poll.get("interval_seconds") or 3)
    poll["interval_seconds"] = min(max(poll_interval, 3), 5)
    poll["interval_min_seconds"] = 3
    poll["interval_max_seconds"] = 5
    merged["poll"] = poll

    transport_risk = dict(merged.get("transport_risk_guard", {}) or {})
    loop_jitter = float(transport_risk.get("loop_jitter_seconds") or 0.8)
    transport_risk["loop_jitter_seconds"] = min(max(loop_jitter, 0.8), 1.8)
    probe_interval = int(transport_risk.get("passive_logout_probe_interval_seconds") or 45)
    transport_risk["passive_logout_probe_interval_seconds"] = min(max(probe_interval, 30), 60)
    merged["transport_risk_guard"] = transport_risk

    rpa_send = dict(merged.get("rpa_humanized_send", {}) or {})
    rpa_send.setdefault("enabled", True)
    rpa_send.setdefault("input_method", "clipboard_chunks")
    rpa_send["adaptive_speed_enabled"] = True
    rpa_send["typing_typo_probability"] = 0.0
    rpa_send["typing_typo_max"] = 0
    rpa_send["typing_chunk_min_chars"] = max(int(rpa_send.get("typing_chunk_min_chars") or 0), 2)
    rpa_send["typing_chunk_max_chars"] = min(max(int(rpa_send.get("typing_chunk_max_chars") or 5), 4), 7)
    rpa_send["typing_char_delay_min_ms"] = max(int(rpa_send.get("typing_char_delay_min_ms") or 0), 45)
    rpa_send["typing_char_delay_max_ms"] = max(int(rpa_send.get("typing_char_delay_max_ms") or 0), 145)
    rpa_send["typing_micro_pause_every_chars"] = max(int(rpa_send.get("typing_micro_pause_every_chars") or 0), 18)
    rpa_send["typing_micro_pause_min_ms"] = max(int(rpa_send.get("typing_micro_pause_min_ms") or 0), 180)
    rpa_send["typing_micro_pause_max_ms"] = max(int(rpa_send.get("typing_micro_pause_max_ms") or 0), 480)
    rpa_send["send_pre_delay_min_ms"] = max(int(rpa_send.get("send_pre_delay_min_ms") or 0), 250)
    rpa_send["send_pre_delay_max_ms"] = max(int(rpa_send.get("send_pre_delay_max_ms") or 0), 900)
    # Keep a human-like post-input pause, but avoid an overly conservative floor
    # that drags response latency in normal low-risk scenarios.
    rpa_send["send_post_input_delay_min_ms"] = max(int(rpa_send.get("send_post_input_delay_min_ms") or 0), 320)
    rpa_send["send_post_input_delay_max_ms"] = max(int(rpa_send.get("send_post_input_delay_max_ms") or 0), 900)
    rpa_send["input_fast_visual_confirm_enabled"] = True
    rpa_send["send_trigger_mode"] = "enter_only"
    rpa_send["send_input_confirm_attempts"] = 1
    rpa_send["send_rate_min_interval_seconds"] = 0
    rpa_send["send_rate_burst_window_seconds"] = max(int(rpa_send.get("send_rate_burst_window_seconds") or 0), 600)
    rpa_send["send_rate_burst_limit"] = max(int(rpa_send.get("send_rate_burst_limit") or 0), 20)
    merged["rpa_humanized_send"] = rpa_send

    for section_name, max_chars in (
        ("rag_response", 150),
        ("reply_style_adapter", 150),
        ("llm_reply_synthesis", 150),
        ("final_visible_llm_polish", 150),
    ):
        section = dict(merged.get(section_name, {}) or {})
        if section:
            current = int(section.get("max_reply_chars") or max_chars)
            section["max_reply_chars"] = min(current, max_chars)
            merged[section_name] = section

    realtime = dict(merged.get("realtime_reply", {}) or {})
    if realtime:
        realtime["max_completion_tokens"] = min(int(realtime.get("max_completion_tokens") or 180), 180)
        realtime["max_history_messages"] = min(int(realtime.get("max_history_messages") or 4), 4)
        realtime["history_char_budget"] = min(int(realtime.get("history_char_budget") or 800), 800)
        realtime["business_local_style_foreground_llm_enabled"] = _truthy(
            realtime.get("business_local_style_foreground_llm_enabled"),
            default=True,
        )
        merged["realtime_reply"] = realtime

    reply_safety = dict(merged.get("rpa_reply_safety", {}) or {})
    reply_safety.setdefault("enabled", True)
    reply_safety["max_auto_reply_chars"] = min(int(reply_safety.get("max_auto_reply_chars") or 150), 150)
    reply_safety["defer_standalone_greeting"] = _truthy(
        reply_safety.get("defer_standalone_greeting"),
        default=_truthy(guard.get("defer_standalone_greeting"), default=False),
    )
    reply_safety["rate_limit_notice_customer"] = True
    merged["rpa_reply_safety"] = reply_safety

    if _truthy(guard.get("disable_history_backfill"), default=True):
        history_backfill = dict(merged.get("history_backfill", {}) or {})
        mode = str(history_backfill.get("mode") or "").strip().lower()
        allow_anchor = _truthy(guard.get("allow_anchor_backfill_when_needed"), default=True)
        if mode == "anchor_until_found" and allow_anchor:
            history_backfill.update(
                {
                    "enabled": True,
                    "mode": "anchor_until_found",
                    "load_times": 0,
                    "max_load_times": 0,
                    "freshness_load_times": 0,
                    "trigger_when_anchor_missing": True,
                    "block_on_anchor_not_found": True,
                    "overflow_batch_on_anchor_missing": True,
                    "restore_to_latest": True,
                    "max_scroll_steps": min(int(history_backfill.get("max_scroll_steps") or 4), 6),
                    "max_duration_seconds": min(int(history_backfill.get("max_duration_seconds") or 10), 15),
                    "max_snapshots": min(int(history_backfill.get("max_snapshots") or 6), 8),
                }
            )
        else:
            history_backfill.update(
                {
                    "enabled": False,
                    "load_times": 0,
                    "max_load_times": 0,
                    "freshness_load_times": 0,
                }
            )
        merged["history_backfill"] = history_backfill

    rate_limits = dict(merged.get("rate_limits", {}) or {})
    if _truthy(guard.get("customer_experience_first"), default=True):
        rate_limits["min_seconds_between_replies"] = min(int(rate_limits.get("min_seconds_between_replies") or 0), 3)
        rate_limits["max_replies_per_10_minutes"] = max(int(rate_limits.get("max_replies_per_10_minutes") or 0), 20)
        rate_limits["max_replies_per_hour"] = max(int(rate_limits.get("max_replies_per_hour") or 0), 100)
        rate_limits["notice_customer"] = True
        merged["rate_limits"] = rate_limits
    elif _truthy(guard.get("low_frequency_reply_limits"), default=False):
        rate_limits["min_seconds_between_replies"] = max(int(rate_limits.get("min_seconds_between_replies") or 0), 90)
        rate_limits["max_replies_per_10_minutes"] = min(int(rate_limits.get("max_replies_per_10_minutes") or 999), 2)
        rate_limits["max_replies_per_hour"] = min(int(rate_limits.get("max_replies_per_hour") or 999), 10)
        rate_limits["notice_customer"] = False
        merged["rate_limits"] = rate_limits

    merged["_live_safety_guard"] = summary
    return merged
