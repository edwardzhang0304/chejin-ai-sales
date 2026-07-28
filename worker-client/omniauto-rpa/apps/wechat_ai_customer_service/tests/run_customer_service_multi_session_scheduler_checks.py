"""Offline checks for customer-service multi-session scheduling primitives."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler_state import (  # noqa: E402
    SchedulerConfig,
    SchedulerStateStore,
    cleanup_scheduler_state,
    complete_llm_task,
    complete_polish_task,
    enqueue_llm_task,
    enqueue_polish_task,
    enqueue_pending_session,
    mark_capture_started,
    mark_llm_started,
    mark_polish_started,
    mark_reply_sending,
    mark_reply_sent,
    message_identity,
    record_capture_result,
    record_session_signal,
    select_capture_sessions,
    select_ready_replies,
    state_summary,
)
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings import CustomerServiceSettings  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler import (  # noqa: E402
    CapturedMessagesConnector,
    CustomerServiceSchedulerRuntime,
    ManagedListenerSchedulerBridge,
    mark_session_capture_failed,
    merge_scheduler_conversation_context,
    plan_reply_with_listen_workflow,
    polish_reply_with_listen_workflow,
    ready_reply_session_envelope_failure,
)
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_session_ledger import SessionLedgerStore  # noqa: E402
import apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler as scheduler_module  # noqa: E402
import apps.wechat_ai_customer_service.admin_backend.services.customer_service_scheduler_state as scheduler_state_module  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.customer_profile_store import CustomerProfileStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.session_monitor import SessionMonitor, SessionState  # noqa: E402
from apps.wechat_ai_customer_service.customer_service_live_safety import apply_customer_service_live_safety_guard  # noqa: E402
from apps.wechat_ai_customer_service.workflows.llm_intent_router import route_intent  # noqa: E402
from apps.wechat_ai_customer_service.scripts.run_customer_service_listener import (  # noqa: E402
    load_concurrency_scheduler_enabled,
    load_managed_poll_interval_settings,
    load_rpa_humanized_send_settings,
    scheduler_bridge_has_active_work,
    summarize_scheduler_tick_activity,
)
from listen_and_reply import (  # noqa: E402
    TargetConfig,
    customer_service_anchor_payload,
    load_config,
    load_rules,
    maybe_enrich_messages_with_history,
    select_batch_details,
    select_scheduler_authoritative_batch_details,
)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def empty_state() -> dict[str, Any]:
    return {
        "version": 2,
        "tenant_id": f"unit_{uuid.uuid4().hex[:12]}",
        "sessions": {},
        "captures": {},
        "llm_tasks": {},
        "polish_tasks": {},
        "ready_replies": {},
        "send_sequence": 0,
        "events": [],
    }


def session_by_name(state: dict[str, Any], name: str) -> dict[str, Any]:
    sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
    matches = [
        session
        for session in (sessions or {}).values()
        if isinstance(session, dict)
        and str(session.get("target_name") or session.get("display_name") or "").strip() == name
    ]
    if len(matches) != 1:
        raise KeyError(name)
    return matches[0]


def target_state_by_name(state: dict[str, Any], name: str) -> dict[str, Any]:
    targets = state.get("targets", {}) if isinstance(state, dict) else {}
    direct = (targets or {}).get(name)
    if isinstance(direct, dict):
        return direct
    matches = [
        target_state
        for target_state in (targets or {}).values()
        if isinstance(target_state, dict)
        and str(target_state.get("_display_name") or "").strip() == name
    ]
    if len(matches) != 1:
        raise KeyError(name)
    return matches[0]


def enable_brain_first_test_settings(tenant_id: str) -> Path:
    settings_store = CustomerServiceSettings(tenant_id=tenant_id)
    settings_store.save(
        {
            "use_llm": True,
            "final_visible_llm_polish_enabled": True,
            "customer_service_brain_mode": "brain_first",
        }
    )
    return settings_store.settings_path


class FakeSessionConnector:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self.sessions = sessions

    def list_sessions(self) -> dict[str, Any]:
        return {"ok": True, "sessions": self.sessions}


class FakeBridgeConnector:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.messages = {
            "customer_a": [
                {
                    "id": "bridge-a-1",
                    "type": "text",
                    "sender": "customer",
                    "content": "这台车还能优惠吗",
                    "time": "2026-05-25T10:00:00",
                }
            ]
        }

    def list_sessions(self) -> dict[str, Any]:
        return {
            "ok": True,
            "sessions": [{"name": "customer_a", "content": "这台车还能优惠吗", "time": "10:00", "conversation_type": "private"}],
        }

    def get_messages(self, target: str, exact: bool = True, history_load_times: int = 0, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "target": target, "exact": exact, "messages": list(self.messages.get(target, []))}

    def send_text_and_verify(
        self,
        target: str,
        text: str,
        exact: bool = True,
        *,
        skip_send_rate_guard: bool = False,
        session_key: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.sent.append({"target": target, "text": text, "exact": exact, "session_key": session_key})
        return {"ok": True, "verified": True, "adapter": "win32_ocr", "state": "sent"}


class FakePreviewSessionMonitor:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions

    def all_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)


def message(target: str, index: int, content: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{target}-m-{index}",
        "type": "text",
        "sender": "customer",
        "content": content or f"{target} 问题 {index}",
        "time": f"2026-05-25T10:{index:02d}:00",
    }


def check_pending_sessions_survive_round_limit() -> None:
    state = empty_state()
    for index in range(10):
        enqueue_pending_session(state, f"客户{index}", reason="unit_burst", now=f"2026-05-25T10:{index:02d}:00")
    first_round = select_capture_sessions(state, limit=3)
    assert_equal(len(first_round), 3, "round should select only the configured capture limit")
    for item in first_round:
        mark_capture_started(state, item["target_name"], now="2026-05-25T10:30:00")
    remaining = select_capture_sessions(state, limit=20)
    remaining_names = {item["target_name"] for item in remaining}
    assert_equal(len(remaining_names), 7, "unselected pending sessions must remain pending")
    assert_true("客户3" in remaining_names and "客户9" in remaining_names, "later pending sessions should survive truncation")


def check_no_change_signal_does_not_clear_pending() -> None:
    state = empty_state()
    record_session_signal(
        state,
        {
            "name": "客户A",
            "content": "第一条",
            "time": "10:00",
            "unread_detected": True,
            "unread_badge": "visual_red_dot",
            "conversation_type": "private",
        },
        now="2026-05-25T10:00:00",
    )
    assert_true(bool(session_by_name(state, "客户A").get("pending_capture")), "changed signal should enqueue pending")
    record_session_signal(
        state,
        {"name": "客户A", "content": "第一条", "time": "10:00", "conversation_type": "private"},
        now="2026-05-25T10:01:00",
    )
    assert_true(bool(session_by_name(state, "客户A").get("pending_capture")), "unchanged signal must not clear pending")


def check_unread_signal_without_preview_enters_pending() -> None:
    state = empty_state()
    record_session_signal(
        state,
        {"name": "客户无预览", "unread_detected": True, "conversation_type": "private"},
        now="2026-05-25T10:02:00",
    )
    session = session_by_name(state, "客户无预览")
    assert_true(bool(session.get("pending_capture")), "unread badge/signal without preview must enqueue pending")
    assert_equal(session.get("status"), "capture_pending", "unread-only signal should be capture pending")


def check_context_version_marks_old_llm_task_stale() -> None:
    state = empty_state()
    capture1 = record_capture_result(
        state,
        "客户A",
        messages=[message("A", 1)],
        batch=[message("A", 1)],
        now="2026-05-25T10:00:00",
    )
    task1 = enqueue_llm_task(state, capture1["capture_id"], now="2026-05-25T10:00:01")
    mark_llm_started(state, task1["task_id"], now="2026-05-25T10:00:02")
    capture2 = record_capture_result(
        state,
        "客户A",
        messages=[message("A", 1), message("A", 2)],
        batch=[message("A", 2)],
        now="2026-05-25T10:00:03",
    )
    assert_equal(capture2["context_version"], 2, "second capture should advance context version")
    result = complete_llm_task(
        state,
        task1["task_id"],
        reply_text="旧回复",
        now="2026-05-25T10:00:04",
    )
    assert_equal(result["status"], "stale", "old LLM task must become stale after newer context")
    assert_equal(state_summary(state)["reply_ready"], 0, "stale task must not create ready reply")


def check_duplicate_active_capture_does_not_stale_llm_task() -> None:
    state = empty_state()
    first_message = message("A", 1)
    capture1 = record_capture_result(
        state,
        "客户A",
        messages=[first_message],
        batch=[first_message],
        now="2026-05-25T10:00:00",
    )
    task1 = enqueue_llm_task(state, capture1["capture_id"], now="2026-05-25T10:00:01")
    mark_llm_started(state, task1["task_id"], now="2026-05-25T10:00:02")
    duplicate_capture = record_capture_result(
        state,
        "客户A",
        messages=[first_message],
        batch=[first_message],
        now="2026-05-25T10:00:03",
    )
    assert_equal(duplicate_capture["status"], "empty", "same active input should not create a new capture")
    assert_equal(duplicate_capture["context_version"], 1, "same active input must not advance context version")
    completed = complete_llm_task(
        state,
        task1["task_id"],
        reply_text="当前回复",
        now="2026-05-25T10:00:04",
    )
    assert_equal(completed["status"], "completed", "duplicate active capture must not stale the original LLM task")
    assert_equal(state_summary(state)["reply_ready"], 1, "original LLM task should still create one ready reply")


def check_ready_reply_fifo_and_same_session_latest_only() -> None:
    state = empty_state()
    capture_a = record_capture_result(state, "客户A", messages=[message("A", 1)], batch=[message("A", 1)], now="2026-05-25T10:00:00")
    capture_b = record_capture_result(state, "客户B", messages=[message("B", 1)], batch=[message("B", 1)], now="2026-05-25T10:00:01")
    task_a = enqueue_llm_task(state, capture_a["capture_id"], now="2026-05-25T10:00:02")
    task_b = enqueue_llm_task(state, capture_b["capture_id"], now="2026-05-25T10:00:03")
    mark_llm_started(state, task_a["task_id"], now="2026-05-25T10:00:04")
    mark_llm_started(state, task_b["task_id"], now="2026-05-25T10:00:05")
    complete_llm_task(state, task_b["task_id"], reply_text="B先完成", now="2026-05-25T10:00:06")
    complete_llm_task(state, task_a["task_id"], reply_text="A后完成", now="2026-05-25T10:00:07")
    selected = select_ready_replies(state, limit=2)
    assert_equal([item["target_name"] for item in selected], ["客户B", "客户A"], "ready replies should be FIFO by ready_at")

    mark_reply_sent(state, selected[0]["reply_id"], now="2026-05-25T10:00:08")
    capture_b2 = record_capture_result(state, "客户B", messages=[message("B", 2)], batch=[message("B", 2)], now="2026-05-25T10:00:09")
    task_b2 = enqueue_llm_task(state, capture_b2["capture_id"], now="2026-05-25T10:00:10")
    mark_llm_started(state, task_b2["task_id"], now="2026-05-25T10:00:11")
    complete_llm_task(state, task_b2["task_id"], reply_text="B新版", now="2026-05-25T10:00:12")
    selected_after_new_context = select_ready_replies(state, limit=5)
    b_replies = [item for item in selected_after_new_context if item["target_name"] == "客户B"]
    assert_equal(len(b_replies), 1, "same target should expose only the latest ready reply")
    assert_equal(b_replies[0]["reply_text"], "B新版", "latest context reply should win")


def check_customer_profile_store_concurrent_json_writes() -> None:
    with tempfile.TemporaryDirectory(prefix="profile_store_concurrent_") as tmp:
        root = Path(tmp) / "customer_profiles"
        tenant_id = "scheduler_profile_concurrent"

        def worker(index: int) -> dict[str, Any] | None:
            store = CustomerProfileStore(tenant_id=tenant_id, root=root)
            return store.increment_message_stats(target_name=f"客户{index % 3}", is_reply=index % 2 == 0)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker, index) for index in range(40)]
            results = [future.result() for future in as_completed(futures)]

        assert_equal(len(results), 40, "all concurrent profile writes should complete")
        assert_true(root.joinpath("profiles.json").exists(), "profiles file should be written")
        profiles = json.loads(root.joinpath("profiles.json").read_text(encoding="utf-8"))
        assert_true(isinstance(profiles, list), "profiles JSON should stay valid after concurrent writes")
        names = {str(item.get("target_name") or "") for item in profiles if isinstance(item, dict)}
        assert_true({"客户0", "客户1", "客户2"} <= names, "all concurrent targets should be present")


def check_session_monitor_keeps_overflow_pending(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_overflow_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(state_path=state_path, max_targets_per_iteration=3)
    sessions = [
        {"name": f"客户{idx}", "content": f"新消息{idx}", "time": f"10:{idx:02d}", "conversation_type": "private"}
        for idx in range(6)
    ]
    first = monitor.poll(FakeSessionConnector(sessions))
    assert_equal(len(first), 3, "monitor should return only max targets per iteration")
    pending_after_first = monitor.pending_targets()
    assert_equal(len(pending_after_first), 6, "monitor should expose every pending session, not only the visible round limit")
    for item in first:
        monitor.reset_unread(item.name)
    second = monitor.poll(FakeSessionConnector(sessions))
    second_names = {item.name for item in second}
    assert_equal(len(second_names), 3, "unreturned active sessions should remain pending for next poll")
    assert_true({"客户3", "客户4", "客户5"}.issubset(second_names), "overflow active sessions should be returned next")


def check_session_monitor_empty_preview_does_not_clear_pending(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_empty_preview_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(state_path=state_path, max_targets_per_iteration=3)
    first = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "刚发的新消息", "time": "10:00", "conversation_type": "private"}
        ])
    )
    assert_equal([item.name for item in first], ["客户A"], "initial signal should mark active")
    second = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "", "time": "", "conversation_type": "private"}
        ])
    )
    assert_equal([item.name for item in second], ["客户A"], "empty preview without reset must not clear pending")


def check_session_monitor_visual_unread_badge_retriggers_after_reset(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_visual_badge_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(state_path=state_path, max_targets_per_iteration=3)
    initial = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "", "time": "", "unread_badge": "", "conversation_type": "private"},
            {"name": "客户B", "content": "", "time": "", "unread_badge": "", "conversation_type": "private"},
        ])
    )
    assert_equal(initial, [], "empty previews without badges should stay idle")
    red = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "", "time": "", "unread_badge": "visual_red_dot", "conversation_type": "private"},
            {"name": "客户B", "content": "", "time": "", "unread_badge": "visual_red_dot", "conversation_type": "private"},
        ])
    )
    assert_equal([item.name for item in red], ["客户A", "客户B"], "visual red badges should activate every unread session")
    for item in red:
        monitor.reset_unread(item.name)
    cleared = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "", "time": "", "unread_badge": "", "conversation_type": "private"},
            {"name": "客户B", "content": "", "time": "", "unread_badge": "", "conversation_type": "private"},
        ])
    )
    assert_equal(cleared, [], "cleared visual badges should not stay pending after reset")
    red_again = monitor.poll(
        FakeSessionConnector([
            {"name": "客户A", "content": "", "time": "", "unread_badge": "visual_red_dot", "conversation_type": "private"},
            {"name": "客户B", "content": "", "time": "", "unread_badge": "", "conversation_type": "private"},
        ])
    )
    assert_equal([item.name for item in red_again], ["客户A"], "a new visual badge after reset should retrigger capture")


def check_passive_probe_defers_when_monitor_has_unread_signal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monitor = SessionMonitor(
            state_path=root / "session_monitor_probe_defer.json",
            max_targets_per_iteration=2,
            initial_preview_can_raise_unread=False,
            preview_change_can_raise_unread=False,
            short_preview_can_raise_unread=True,
        )
        connector = FakeSessionConnector([
            {"name": "客户A", "content": "在吗", "time": "13:01", "unread_badge": "visual_red_dot", "conversation_type": "private"}
        ])
        store = SchedulerStateStore(tenant_id="unit_probe_defer", path=root / "scheduler_state.json")
        bridge = SimpleNamespace(enabled=True, store=store, session_monitor=monitor, connector=connector)
        assert_true(
            scheduler_bridge_has_active_work(bridge),
            "passive probe should defer when low-disturbance monitor can see an unread short signal",
        )


def check_session_monitor_high_sensitivity_short_signal_waits_merge_window(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_short_merge_window_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        high_sensitivity_short_merge_window_seconds=0.4,
    )
    monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "在吗", "time": "10:00", "conversation_type": "private"}]
        )
    )
    assert_equal(monitor.pending_targets(), [], "short greeting should wait a brief merge window before dispatch")
    time.sleep(0.45)
    assert_equal([item.name for item in monitor.pending_targets()], ["客户A"], "short greeting should become dispatchable after merge window")


def check_session_monitor_preserves_high_sensitivity_pending_after_empty_capture(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_short_empty_capture_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        high_sensitivity_short_merge_window_seconds=0.0,
        empty_capture_retry_seconds=0.0,
    )
    monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "地址", "time": "10:00", "conversation_type": "private"}]
        )
    )
    assert_equal([item.name for item in monitor.pending_targets()], ["客户A"], "short business phrase should enter pending queue")
    assert_true(monitor.should_preserve_pending_after_empty_capture("客户A"), "short high-sensitivity signal should request empty-capture retry preservation")
    monitor.reset_unread("客户A", preserve_pending=True, retry_after_seconds=0.0)
    assert_equal([item.name for item in monitor.pending_targets()], ["客户A"], "empty capture must not silently consume high-sensitivity short signal")
    monitor.reset_unread("客户A")
    assert_equal(monitor.pending_targets(), [], "normal reset should still clear preserved short signal")


def check_session_monitor_preserves_normal_pending_after_empty_capture_briefly(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_normal_empty_capture_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        require_unread_badge_for_dispatch=True,
        require_preview_signal_with_unread_badge=True,
        empty_capture_retry_seconds=0.0,
    )
    monitor.poll(
        FakeSessionConnector(
            [
                {
                    "name": "新数据测试",
                    "session_key": "wx:rpa:v1:normal-empty-retry",
                    "content": "有没有至少2.0T的？预算高点能接受",
                    "time": "23:00",
                    "unread_badge": "visual_red_dot",
                    "conversation_type": "group",
                }
            ]
        )
    )
    assert_equal(
        [item.session_key for item in monitor.pending_targets()],
        ["wx:rpa:v1:normal-empty-retry"],
        "normal unread preview should enter pending queue when badge+preview agree",
    )
    assert_true(
        monitor.should_preserve_pending_after_empty_capture("wx:rpa:v1:normal-empty-retry"),
        "ordinary unread preview should survive an empty chat-pane capture instead of being consumed",
    )
    monitor.reset_unread("wx:rpa:v1:normal-empty-retry", preserve_pending=True, retry_after_seconds=0.0)
    assert_equal(
        [item.session_key for item in monitor.pending_targets()],
        ["wx:rpa:v1:normal-empty-retry"],
        "first empty capture should keep ordinary unread pending for retry/recovery",
    )
    assert_true(
        monitor.should_preserve_pending_after_empty_capture("wx:rpa:v1:normal-empty-retry"),
        "second empty capture is still allowed so transient OCR blanking can recover",
    )
    monitor.reset_unread("wx:rpa:v1:normal-empty-retry", preserve_pending=True, retry_after_seconds=0.0)
    assert_true(
        not monitor.should_preserve_pending_after_empty_capture("wx:rpa:v1:normal-empty-retry"),
        "ordinary empty-capture preservation must be bounded to avoid infinite foreground loops",
    )


def check_session_monitor_low_disturbance_ignores_normal_preview_without_badge(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_low_disturbance_preview_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        initial_preview_can_raise_unread=False,
        preview_change_can_raise_unread=False,
        short_preview_can_raise_unread=True,
    )
    initial = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "昨天聊到的预算和车型方向", "time": "10:00", "conversation_type": "private"}]
        )
    )
    assert_equal(initial, [], "ordinary first-seen preview should become baseline, not unread work")
    changed = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "列表预览轻微变化但没有角标", "time": "10:01", "conversation_type": "private"}]
        )
    )
    assert_equal(changed, [], "ordinary preview drift without badge should not trigger foreground switching")
    badge = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "真正新消息", "time": "10:02", "unread_badge": "visual_red_dot", "conversation_type": "private"}]
        )
    )
    assert_equal([item.name for item in badge], ["客户A"], "visual unread badge must still trigger capture")


def check_session_monitor_low_disturbance_keeps_short_preview_signal(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_low_disturbance_short_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        initial_preview_can_raise_unread=False,
        preview_change_can_raise_unread=False,
        short_preview_can_raise_unread=True,
        high_sensitivity_short_merge_window_seconds=0.0,
    )
    short_signal = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "在吗", "time": "10:00", "conversation_type": "private"}]
        )
    )
    assert_equal([item.name for item in short_signal], ["客户A"], "short high-sensitivity preview should remain dispatchable")


def check_session_monitor_low_risk_requires_badge_and_preview_signal(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_badge_preview_gate_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=3,
        initial_preview_can_raise_unread=False,
        preview_change_can_raise_unread=False,
        short_preview_can_raise_unread=True,
        require_unread_badge_for_dispatch=True,
        require_preview_signal_with_unread_badge=True,
        high_sensitivity_short_merge_window_seconds=0.0,
    )
    no_badge = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户A", "content": "在吗", "time": "10:00", "conversation_type": "private"}]
        )
    )
    assert_equal(no_badge, [], "short preview without visual unread badge must not switch sessions")
    red_without_preview = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户B", "content": "", "time": "", "unread_badge": "visual_red_dot", "conversation_type": "private"}]
        )
    )
    assert_equal(red_without_preview, [], "visual badge without a preview/time signal should wait for a concrete message signal")
    red_with_preview = monitor.poll(
        FakeSessionConnector(
            [{"name": "客户C", "content": "晚上好", "time": "10:01", "unread_badge": "visual_red_dot", "conversation_type": "private"}]
        )
    )
    assert_equal([item.name for item in red_with_preview], ["客户C"], "badge plus preview signal should dispatch")


def check_session_monitor_event_driven_dispatch_keeps_sticky_target(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_dispatch_sticky_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=5,
        min_switch_interval_seconds=30,
        dispatch_strategy="event_driven",
        sticky_target_hold_seconds=60,
        preview_change_confirmations=2,
    )
    monitor.poll(
        FakeSessionConnector(
            [
                {"name": "客户A", "content": "A 新消息", "time": "10:00", "conversation_type": "private"},
                {"name": "客户B", "content": "B 新消息", "time": "10:00", "conversation_type": "private"},
            ]
        )
    )
    first = monitor.select_dispatch_targets(limit=2)
    assert_true(bool(first), "event-driven dispatch should choose one pending target")
    second = monitor.select_dispatch_targets(limit=2)
    assert_equal(
        [item.name for item in second],
        [item.name for item in first],
        "sticky window should keep dispatching the same target briefly",
    )
    monitor.reset_unread(first[0].name)
    third = monitor.select_dispatch_targets(limit=2)
    assert_equal(len(third), 1, "after clearing sticky target, remaining pending session should be dispatched")
    assert_true(
        third[0].name in {"客户A", "客户B"} and third[0].name != first[0].name,
        "dispatch should switch only after current sticky target is handled",
    )


def check_session_monitor_event_driven_dispatch_rotates_under_hot_target(tmp_dir: Path | None = None) -> None:
    state_path = (tmp_dir or (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts")) / "session_monitor_dispatch_rotate_unit.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    monitor = SessionMonitor(
        state_path=state_path,
        max_targets_per_iteration=5,
        min_switch_interval_seconds=30,
        dispatch_strategy="event_driven",
        sticky_target_hold_seconds=90,
        sticky_max_dispatch_rounds=2,
        preview_change_confirmations=1,
    )
    monitor.poll(
        FakeSessionConnector(
            [
                {"name": "客户A", "content": "A 新消息", "time": "10:00", "conversation_type": "private"},
                {"name": "客户B", "content": "B 新消息", "time": "10:00", "conversation_type": "private"},
            ]
        )
    )
    first = monitor.select_dispatch_targets(limit=1)
    second = monitor.select_dispatch_targets(limit=1)
    third = monitor.select_dispatch_targets(limit=1)
    first_name = first[0].name if first else ""
    second_name = second[0].name if second else ""
    third_name = third[0].name if third else ""
    assert_true(bool(first_name), "first dispatch should select one pending target")
    assert_equal(second_name, first_name, "sticky dispatch should keep same target in early rounds")
    assert_true(
        third_name and third_name != first_name,
        "hot sticky target should rotate after max sticky rounds to avoid starving others",
    )


def check_capture_failed_backoff_blocks_immediate_requeue() -> None:
    state = empty_state()
    now_text = datetime.now().isoformat(timespec="seconds")
    record_session_signal(
        state,
        {"name": "客户A", "unread_detected": True, "conversation_type": "private"},
        now=now_text,
    )
    session = session_by_name(state, "客户A")
    assert_true(bool(session.get("pending_capture")), "initial unread should enqueue capture")
    mark_session_capture_failed(
        state,
        "客户A",
        "target_not_confirmed_for_messages",
        now=now_text,
    )
    session = session_by_name(state, "客户A")
    retry_not_before = str((session.get("risk_state") or {}).get("capture_retry_not_before") or "")
    assert_true(bool(retry_not_before), "target confirmation failure should write retry cooldown")
    assert_true(bool(session.get("pending_capture")), "target confirmation failure should preserve pending intent")
    assert_true(
        not select_capture_sessions(state, limit=1),
        "target confirmation cooldown should block mechanical immediate recapture",
    )
    (session.setdefault("risk_state", {}))["capture_retry_not_before"] = "2999-01-01T00:00:00"
    record_session_signal(
        state,
        {"name": "客户A", "unread_detected": True, "conversation_type": "private"},
        now=now_text,
    )
    session = session_by_name(state, "客户A")
    assert_true(bool(session.get("pending_capture")), "cooldown should not swallow the pending message")
    assert_true(
        not select_capture_sessions(state, limit=1),
        "cooldown window should still block mechanical immediate requeue",
    )
    (session.setdefault("risk_state", {}))["capture_retry_not_before"] = "2000-01-01T00:00:00"
    record_session_signal(
        state,
        {"name": "客户A", "unread_detected": True, "conversation_type": "private"},
        now=now_text,
    )
    session = session_by_name(state, "客户A")
    assert_true(bool(session.get("pending_capture")), "after cooldown, capture should be allowed again")
    assert_true(
        bool(select_capture_sessions(state, limit=1)),
        "expired target confirmation cooldown should allow capture retry",
    )


def check_runtime_tick_does_not_wait_for_slow_llm() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        messages_by_target = {
            "客户A": [message("A", 1)],
            "客户B": [message("B", 1)],
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            target = str(session.get("target_name") or "")
            return {"messages": messages_by_target[target], "batch": messages_by_target[target]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            if capture.get("target_name") == "客户A":
                time.sleep(0.25)
            else:
                time.sleep(0.02)
            return {"ok": True, "reply_text": f"回复 {capture.get('target_name')}", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=2, llm_max_concurrency=2, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            started = time.time()
            result = runtime.tick(
                session_signals=[
                    {"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"},
                    {"name": "客户B", "content": "B新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"},
                ],
                allow_send=False,
                now="2026-05-25T10:00:00",
            )
            duration = time.time() - started
            assert_true(duration < 0.15, f"tick should submit LLM tasks without waiting for slow worker, got {duration:.3f}s")
            assert_equal(result["summary"]["llm_running"], 2, "both LLM tasks should be running after first tick")
            phase = result.get("phase_durations")
            assert_true(isinstance(phase, dict), f"tick should expose phase_durations: {result}")
            for key in (
                "send_pre_seconds",
                "capture_seconds",
                "llm_submit_seconds",
                "llm_collect_seconds",
                "send_post_seconds",
                "state_save_seconds",
                "total_seconds",
            ):
                assert_true(key in phase, f"phase duration key missing: {key} -> {phase}")
            time.sleep(0.06)
            second = runtime.tick(allow_send=False, now="2026-05-25T10:00:01")
            assert_true(second["summary"]["reply_ready"] >= 1, "fast LLM task should become ready while slow task may still run")
            time.sleep(0.25)
            third = runtime.tick(allow_send=False, now="2026-05-25T10:00:02")
            assert_equal(third["summary"]["reply_ready"], 2, "both LLM tasks should eventually be ready")
        finally:
            runtime.shutdown()


def check_runtime_retries_same_capture_for_monitor_only_short_pending_after_brain_no_visible_reply() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_short_requeue", path=path)
        synthetic = {
            "id": "short_pending:unit-boss",
            "type": "text",
            "sender": "unknown",
            "sender_role": "unknown",
            "content": "老板",
            "short_pending_recovered": True,
            "short_pending_synthesized_from_monitor": True,
            "pending_signal_kind": "high_sensitivity_short",
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:short-requeue",
                "messages": [],
                "batch": [copy.deepcopy(synthetic)],
                "history_backfill": {"short_pending_recovered_from_anchor_empty": True},
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=lambda _capture, _task: {"ok": False, "reason": "customer_service_brain_no_visible_reply"},
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "许聪",
                        "content": "老板",
                        "time": "00:35",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "private",
                        "session_key": "wx:rpa:v1:short-requeue",
                    }
                ],
                now="2026-06-08T00:36:00",
            )
            runtime.tick(now="2026-06-08T00:36:02")
            time.sleep(0.03)
            result = runtime.tick(now="2026-06-08T00:36:03")
            events = result.get("events") or []
            assert_true(
                any(item.get("event") == "llm_task_failed_requeued_planner" for item in events),
                f"monitor-only short pending Brain failure should retry the same capture in the background: {events}",
            )
            session = session_by_name(store.load(), "许聪")
            assert_true(not bool(session.get("pending_capture")), f"session must not reopen WeChat after short Brain failure: {session}")
            assert_equal(session.get("status"), "llm_queued", "recoverable short preview failure should stay queued for Brain retry")
            queued_task = next(
                task
                for task in (store.load().get("llm_tasks") or {}).values()
                if isinstance(task, dict) and task.get("status") == "queued"
            )
            assert_true(int(queued_task.get("recoverable_retry_count") or 0) >= 1, f"retry count should be tracked: {queued_task}")
        finally:
            runtime.shutdown()


def check_runtime_retries_same_capture_for_real_ocr_short_probe_after_brain_no_visible_reply() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_ocr_short_requeue", path=path)
        ocr_short = {
            "id": "win32_ocr:stable-short-layout",
            "type": "text",
            "sender": "许聪",
            "sender_role": "customer",
            "content": "好的，谢谢",
            "time": "2026-06-08T13:12:40",
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:ocr-short-requeue",
                "messages": [copy.deepcopy(ocr_short)],
                "batch": [copy.deepcopy(ocr_short)],
                "history_backfill": {"anchor_found": True},
            }

        def planner(_capture: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "reason": "customer_service_brain_no_visible_reply",
                "event": {
                    "target": "新数据测试",
                    "action": "blocked",
                    "reason": "customer_service_brain_no_visible_reply",
                    "customer_service_brain": {
                        "rule_name": "customer_service_brain_no_visible_reply",
                        "reason": "brain_guard_rejected",
                        "guard": {"allowed": False, "reason": "brain_guard_rejected"},
                    },
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "许聪",
                        "content": "好的，谢谢",
                        "time": "2026-06-08T13:12:40",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "private",
                        "session_key": "wx:rpa:v1:ocr-short-requeue",
                    }
                ],
                now="2026-06-08T13:12:41",
            )
            runtime.tick(now="2026-06-08T13:12:42")
            events: list[dict[str, Any]] = []
            for _ in range(20):
                time.sleep(0.03)
                result = runtime.tick(now="2026-06-08T13:12:43")
                events = result.get("events") or []
                if any(item.get("event") == "llm_task_failed_requeued_planner" for item in events):
                    break
            assert_true(
                not any(item.get("event") == "llm_task_failed_requeued_capture" for item in events),
                f"real OCR short Brain failure must not trigger RPA recapture loop: {events}",
            )
            assert_true(
                any(item.get("event") == "llm_task_failed_requeued_planner" for item in events),
                f"real OCR short Brain failure should retry the same captured message through Brain: {events}",
            )
            session = session_by_name(store.load(), "许聪")
            assert_true(not bool(session.get("pending_capture")), f"real OCR failure must not re-open the chat pane: {session}")
            assert_equal(session.get("status"), "llm_queued", "real OCR Brain failure should keep the same capture queued for Brain")
            queued_task = next(
                task
                for task in (store.load().get("llm_tasks") or {}).values()
                if isinstance(task, dict) and task.get("status") == "queued"
            )
            assert_true(
                int(queued_task.get("recoverable_retry_count") or 0) >= 1,
                f"same-capture Brain retry count should be tracked: {queued_task}",
            )
            assert_true(str(queued_task.get("retry_not_before") or ""), f"same-capture retry should be paced: {queued_task}")
        finally:
            runtime.shutdown()


def check_runtime_retries_same_capture_for_full_customer_capture_after_brain_no_visible_reply() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_full_capture_requeue", path=path)
        business_message = {
            "id": "win32_ocr:business-recommendation",
            "type": "text",
            "sender": "许聪",
            "sender_role": "customer",
            "content": "我老婆说想换个电车，你有没有差不多价格的，合适的能推荐",
            "time": "2026-06-08T14:52:34",
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:business-requeue",
                "messages": [copy.deepcopy(business_message)],
                "batch": [copy.deepcopy(business_message)],
                "history_backfill": {"anchor_found": True},
            }

        def planner(_capture: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "reason": "customer_service_brain_no_visible_reply",
                "event": {
                    "target": "新数据测试",
                    "action": "blocked",
                    "reason": "customer_service_brain_no_visible_reply",
                    "customer_service_brain": {
                        "rule_name": "customer_service_brain_no_visible_reply",
                        "reason": "brain_guard_rejected",
                        "guard": {"allowed": False, "reason": "brain_guard_rejected"},
                    },
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "新数据测试",
                        "content": "许聪：我老婆说想换个电车，你有没有差不多价格的，合适的能推荐",
                        "time": "2026-06-08T14:52:34",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "group",
                        "session_key": "wx:rpa:v1:business-requeue",
                    }
                ],
                now="2026-06-08T14:52:35",
            )
            runtime.tick(now="2026-06-08T14:52:36")
            events: list[dict[str, Any]] = []
            for _ in range(20):
                time.sleep(0.03)
                result = runtime.tick(now="2026-06-08T14:52:37")
                events = result.get("events") or []
                if any(item.get("event") == "llm_task_failed_requeued_planner" for item in events):
                    break
            requeued = [
                item
                for item in events
                if item.get("event") == "llm_task_failed_requeued_capture"
                and (item.get("recovery") or {}).get("reason") == "full_customer_capture_recapture"
            ]
            assert_true(not requeued, f"normal business Brain failure must not requeue RPA capture repeatedly: {events}")
            assert_true(
                any(item.get("event") == "llm_task_failed_requeued_planner" for item in events),
                f"normal business Brain failure should retry the same durable capture through Brain: {events}",
            )
            session = session_by_name(store.load(), "新数据测试")
            assert_true(not bool(session.get("pending_capture")), f"business session should not keep switching chats after Brain failure: {session}")
            assert_equal(session.get("status"), "llm_queued", "business Brain failure should stay queued for same-capture Brain retry")
            failed_task = next(
                task
                for task in (store.load().get("llm_tasks") or {}).values()
                if isinstance(task, dict) and task.get("status") == "queued"
            )
            stored_result = failed_task.get("last_failed_result") if isinstance(failed_task.get("last_failed_result"), dict) else {}
            stored_event = stored_result.get("event") if isinstance(stored_result.get("event"), dict) else {}
            stored_brain = stored_event.get("customer_service_brain") if isinstance(stored_event.get("customer_service_brain"), dict) else {}
            assert_equal(stored_result.get("reason"), "customer_service_brain_no_visible_reply", "failed planner result should keep reason")
            assert_equal(stored_brain.get("reason"), "brain_guard_rejected", "failed planner result should keep Brain/guard audit payload")
        finally:
            runtime.shutdown()


def check_runtime_exhausted_brain_no_visible_reply_preserves_internal_handoff() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_brain_no_visible_exhausted", path=path)
        business_message = {
            "id": "win32_ocr:business-no-visible-exhausted",
            "type": "text",
            "sender": "许聪",
            "sender_role": "customer",
            "content": "有没有至少2.0T的？预算高点能接受",
            "time": "2026-06-11T23:00:57",
        }

        def capture_fn(_session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:no-visible-exhausted",
                "messages": [copy.deepcopy(business_message)],
                "batch": [copy.deepcopy(business_message)],
                "history_backfill": {"history_continuity": "overflow_unanchored", "overflow_batch": True, "gap_risk": False},
            }

        def planner(_capture: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "reason": "customer_service_brain_no_visible_reply",
                "event": {"customer_service_brain": {"reason": "unit_no_visible_reply"}},
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "新数据测试",
                        "content": "许聪：有没有至少2.0T的？预算高点能接受",
                        "time": "2026-06-11T23:00:57",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "group",
                        "session_key": "wx:rpa:v1:no-visible-exhausted",
                    }
                ],
                now="2026-06-11T23:01:00",
            )
            for index in range(20):
                time.sleep(0.03)
                runtime.tick(now=f"2026-06-11T23:01:{index + 1:02d}")
                state = store.load()
                session = state.get("sessions", {}).get("wx:rpa:v1:no-visible-exhausted", {})
                if session.get("status") == "internal_handoff_pending":
                    break
            state = store.load()
            session = state.get("sessions", {}).get("wx:rpa:v1:no-visible-exhausted", {})
            assert_equal(
                session.get("status"),
                "internal_handoff_pending",
                "exhausted Brain no-visible reply must preserve the unreplied turn for internal handoff",
            )
            assert_equal(session.get("pending_message_count"), 1, "exhaustion must not clear pending message count")
            assert_true(not bool(session.get("pending_capture")), "exhaustion must not reopen foreground RPA capture loops")
            assert_equal(
                (session.get("risk_state") or {}).get("handoff_reason"),
                "llm_recovery_exhausted_without_visible_reply",
                "exhaustion should be auditable as internal handoff, not silent idle",
            )
        finally:
            runtime.shutdown()


def check_runtime_retries_same_capture_for_brain_schema_failure() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_schema_failure_requeue", path=path)
        customer_message = {
            "id": "win32_ocr:schema-failure",
            "type": "text",
            "sender": "许聪",
            "sender_role": "customer",
            "content": "在吗",
            "time": "2026-06-11T09:12:34",
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:schema-failure-requeue",
                "messages": [copy.deepcopy(customer_message)],
                "batch": [copy.deepcopy(customer_message)],
                "history_backfill": {"anchor_found": True},
            }

        def planner(_capture: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "reason": "brain_response_json_repair_failed",
                "event": {
                    "customer_service_brain": {
                        "rule_name": "customer_service_brain_no_visible_reply",
                        "reason": "brain_response_json_repair_failed",
                        "no_visible_reply": {
                            "class": "schema_parse_failed",
                            "stage": "brain_llm",
                            "same_capture_retry": True,
                        },
                    }
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "许聪",
                        "content": "在吗",
                        "time": "2026-06-11T09:12:34",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "private",
                        "session_key": "wx:rpa:v1:schema-failure-requeue",
                    }
                ],
                now="2026-06-11T09:12:35",
            )
            runtime.tick(now="2026-06-11T09:12:36")
            events: list[dict[str, Any]] = []
            for _ in range(20):
                time.sleep(0.03)
                result = runtime.tick(now="2026-06-11T09:12:37")
                events = result.get("events") or []
                if any(item.get("event") == "llm_task_failed_requeued_planner" for item in events):
                    break
            assert_true(
                any(item.get("event") == "llm_task_failed_requeued_planner" for item in events),
                f"Brain schema parse failure should retry the same durable capture: {events}",
            )
            session = session_by_name(store.load(), "许聪")
            assert_true(not bool(session.get("pending_capture")), f"schema failure must not reopen WeChat RPA capture: {session}")
            assert_equal(session.get("status"), "llm_queued", "schema failure should stay queued for Brain retry")
            queued_task = next(
                task
                for task in (store.load().get("llm_tasks") or {}).values()
                if isinstance(task, dict) and task.get("status") == "queued"
            )
            assert_equal(queued_task.get("error"), None, "requeued task error should be cleared")
            assert_true(int(queued_task.get("recoverable_retry_count") or 0) >= 1, f"retry count should be tracked: {queued_task}")
        finally:
            runtime.shutdown()


def check_runtime_same_capture_retry_can_recover_without_rpa_recapture() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_same_capture_retry_success", path=path)
        customer_message = {
            "id": "win32_ocr:evening-hello",
            "type": "text",
            "sender": "许聪",
            "sender_role": "customer",
            "content": "晚上好",
            "time": "2026-06-11T00:01:15",
        }
        capture_calls: list[dict[str, Any]] = []
        planner_calls: list[dict[str, Any]] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            capture_calls.append(copy.deepcopy(session))
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:same-capture-retry",
                "messages": [copy.deepcopy(customer_message)],
                "batch": [copy.deepcopy(customer_message)],
                "history_backfill": {"anchor_found": True},
            }

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            planner_calls.append({"capture_id": capture.get("capture_id"), "retry_count": task.get("recoverable_retry_count")})
            if len(planner_calls) == 1:
                return {
                    "ok": False,
                    "reason": "customer_service_brain_no_visible_reply",
                    "event": {
                        "customer_service_brain": {
                            "rule_name": "customer_service_brain_no_visible_reply",
                            "reason": "brain_quality_verification_failed",
                        }
                    },
                }
            return {
                "ok": True,
                "reply_text": "晚上好，您说。",
                "decision": {
                    "rule_name": "customer_service_brain_reply",
                    "brain_first_visible_reply_required": True,
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "许聪",
                        "content": "晚上好",
                        "time": "2026-06-11T00:01:15",
                        "unread_badge": "visual_red_dot",
                        "unread_detected": True,
                        "conversation_type": "private",
                        "session_key": "wx:rpa:v1:same-capture-retry",
                    }
                ],
                now="2026-06-11T00:01:16",
            )
            for _ in range(20):
                time.sleep(0.03)
                runtime.tick(now="2026-06-11T00:01:18")
                state = store.load()
                if any(isinstance(reply, dict) and reply.get("status") == "ready" for reply in (state.get("ready_replies") or {}).values()):
                    break
            state = store.load()
            ready = [
                reply
                for reply in (state.get("ready_replies") or {}).values()
                if isinstance(reply, dict) and reply.get("status") == "ready"
            ]
            assert_equal(len(capture_calls), 1, "same-capture retry must not perform a second RPA capture")
            assert_true(len(planner_calls) >= 2, f"Brain should be retried on the same capture: {planner_calls}")
            assert_true(bool(ready), f"same-capture retry should eventually produce a ready reply: {state}")
            assert_equal(ready[-1].get("reply_text"), "晚上好，您说。", "ready reply should be authored by Brain retry")
        finally:
            runtime.shutdown()


def check_scheduler_cleanup_clears_session_ready_refs_without_losing_recent_audit() -> None:
    state = empty_state()
    enqueue_pending_session(state, "客户A", now="2026-06-06T10:00:00")
    capture = record_capture_result(
        state,
        "客户A",
        messages=[message("A", 1)],
        batch=[message("A", 1)],
        now="2026-06-06T10:00:01",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-06T10:00:02")
    complete_llm_task(state, task["task_id"], reply_text="收到，我帮您看。", decision={"rule_name": "unit"}, now="2026-06-06T10:00:03")
    reply_id = next(iter(state["ready_replies"]))
    mark_reply_sent(state, reply_id, send_result={"ok": True, "verified": True}, now="2026-06-06T10:00:04")
    assert_true(reply_id in session_by_name(state, "客户A").get("ready_reply_ids", []), "precondition should keep old session ref")
    cleanup_scheduler_state(state, config=SchedulerConfig(enabled=True), now="2026-06-06T10:00:05")
    assert_true(reply_id in state["ready_replies"], "recent sent reply should remain for audit and summary")
    assert_true(reply_id not in session_by_name(state, "客户A").get("ready_reply_ids", []), "session ready refs should keep only live ready/sending ids")


def check_scheduler_cleanup_preserves_stale_recoverable_llm_pending_messages() -> None:
    state = empty_state()
    business = enqueue_pending_session(
        state,
        "新数据测试",
        conversation_type="group",
        session_key="wx:rpa:v1:business-stale-recoverable",
        reason="recoverable_llm_failure",
        now="2026-06-10T12:53:11",
    )
    business["pending_message_count"] = 1
    business["oldest_unreplied_at"] = "2026-06-10T12:53:07"
    business["pending_recapture_kind"] = "full_customer_capture"
    monitor = enqueue_pending_session(
        state,
        "许聪",
        conversation_type="private",
        session_key="wx:rpa:v1:monitor-short-recoverable",
        reason="recoverable_llm_failure",
        now="2026-06-10T12:53:11",
    )
    monitor["pending_message_count"] = 1
    monitor["oldest_unreplied_at"] = "2026-06-10T12:53:07"
    monitor["pending_recapture_kind"] = "monitor_only_short_pending"
    result = cleanup_scheduler_state(state, config=SchedulerConfig(enabled=True), now="2026-06-10T12:54:00")
    assert_equal(result.get("cleared_stale_recoverable_recaptures"), 2, "cleanup should clear stale recoverable LLM recapture gates")
    business_after = session_by_name(state, "新数据测试")
    monitor_after = session_by_name(state, "许聪")
    assert_true(not business_after.get("pending_capture"), "recoverable LLM failure must not drive stale full RPA recapture")
    assert_equal(
        business_after.get("status"),
        "internal_handoff_pending",
        "cleared full recapture gate must preserve unreplied message instead of swallowing it",
    )
    assert_equal(business_after.get("pending_message_count"), 1, "business pending count should be preserved for internal handoff/audit")
    assert_equal(
        (business_after.get("risk_state") or {}).get("handoff_reason"),
        "legacy_recoverable_llm_failure_pending_preserved",
        "cleanup should surface preserved legacy recoverable failure as internal handoff",
    )
    assert_true(not monitor_after.get("pending_capture"), "recoverable LLM failure must not drive monitor-only RPA recapture")
    assert_equal(
        monitor_after.get("pending_recapture_kind"),
        "",
        "cleanup should erase the old monitor-only recapture path; Brain retries happen on the durable capture instead",
    )
    assert_equal(monitor_after.get("status"), "internal_handoff_pending", "monitor pending count should also be preserved")


def check_select_capture_sessions_preserves_recoverable_llm_pending_messages() -> None:
    state = empty_state()
    pending = enqueue_pending_session(
        state,
        "新数据测试",
        conversation_type="group",
        session_key="wx:rpa:v1:recoverable-gate-preserve",
        reason="recoverable_llm_failure",
        now="2026-06-11T23:10:00",
    )
    pending["pending_message_count"] = 1
    pending["oldest_unreplied_at"] = "2026-06-11T23:09:58"
    pending["pending_recapture_kind"] = "full_customer_capture"
    selected = select_capture_sessions(state, limit=1)
    assert_equal(selected, [], "recoverable LLM gate must not reopen foreground RPA capture")
    after = state["sessions"]["wx:rpa:v1:recoverable-gate-preserve"]
    assert_equal(after.get("status"), "internal_handoff_pending", "recoverable gate must not settle idle with unreplied content")
    assert_equal(after.get("pending_message_count"), 1, "recoverable gate must preserve pending message count")
    assert_equal(
        (after.get("risk_state") or {}).get("handoff_reason"),
        "recoverable_llm_failure_pending_preserved",
        "recoverable gate should produce auditable internal handoff state",
    )


def check_runtime_latency_trace_flows_through_reply_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "reply_text": "收到，我帮您看。",
                "decision": {"rule_name": "unit"},
                "latency_trace": {
                    "brain_llm_duration_seconds": 0.25,
                    "semantic_review_duration_seconds": 0.01,
                },
            }

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "verified": True, "send_result": {"send": {"state": "sent"}}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            send_fn=sender,
        )
        try:
            runtime.tick(session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}], allow_send=False, now="2026-06-06T10:00:00")
            time.sleep(0.03)
            runtime.tick(allow_send=False, now="2026-06-06T10:00:01")
            runtime.tick(allow_send=True, now="2026-06-06T10:00:02")
            state = store.load()
            reply = next(iter((state.get("ready_replies") or {}).values()))
            trace = reply.get("latency_trace") if isinstance(reply.get("latency_trace"), dict) else {}
            for key in (
                "unread_detected_at",
                "capture_started_at",
                "capture_finished_at",
                "brain_queued_at",
                "brain_started_at",
                "brain_finished_at",
                "ready_at",
                "send_started_at",
                "freshness_check_started_at",
                "freshness_check_finished_at",
                "send_rpa_started_at",
                "send_rpa_finished_at",
                "send_finished_at",
                "brain_llm_duration_seconds",
                "semantic_review_duration_seconds",
            ):
                assert_true(bool(trace.get(key)), f"latency trace missing {key}: {trace}")
            summary = state_summary(state)
            assert_true("pending_age_seconds_max" in summary, "summary should expose pending age")
            assert_true("oldest_ready_age_seconds" in summary, "summary should expose ready age")
        finally:
            runtime.shutdown()


def check_polish_latency_trace_is_inherited_by_ready_reply() -> None:
    state = empty_state()
    enqueue_pending_session(state, "客户A", now="2026-06-06T10:00:00")
    capture = record_capture_result(
        state,
        "客户A",
        messages=[message("A", 1)],
        batch=[message("A", 1)],
        now="2026-06-06T10:00:01",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-06T10:00:02")
    complete_llm_task(
        state,
        task["task_id"],
        reply_text="收到，我帮您看。",
        decision={"rule_name": "unit"},
        result_payload={"latency_trace": {"brain_llm_duration_seconds": 1.2}},
        create_ready_reply=False,
        now="2026-06-06T10:00:03",
    )
    polish_task = enqueue_polish_task(state, task["task_id"], now="2026-06-06T10:00:04")
    mark_polish_started(state, polish_task["task_id"], now="2026-06-06T10:00:05")
    completion = complete_polish_task(
        state,
        polish_task["task_id"],
        reply_text="收到，我帮您看。",
        decision={"rule_name": "unit"},
        result_payload={
            "duration_seconds": 1.7,
            "latency_trace": {"final_polish_llm_duration_seconds": 1.4},
        },
        now="2026-06-06T10:00:06",
    )
    trace = (completion.get("reply") or {}).get("latency_trace") or {}
    assert_true(trace.get("brain_llm_duration_seconds") == 1.2, f"Brain trace should flow through polish: {trace}")
    assert_true(trace.get("final_polish_duration_seconds") == 1.7, f"polish duration should be recorded: {trace}")
    assert_true(trace.get("final_polish_llm_duration_seconds") == 1.4, f"polish LLM trace should be recorded: {trace}")


def check_runtime_future_latency_trace_exposes_external_overhead() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_future_latency", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1, content="你好")], "batch": [message("A", 1, content="你好")]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.02)
            return {
                "ok": True,
                "reply_text": "你好，在的。",
                "decision": {"rule_name": "unit_future_latency"},
                "duration_seconds": 0.02,
                "latency_trace": {"brain_llm_duration_seconds": 0.01},
            }

        def polish(planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.02)
            return {
                "ok": True,
                "reply_text": str(planner_task.get("result", {}).get("reply_text") or "你好，在的。"),
                "decision": {"rule_name": "unit_future_latency_polish"},
                "duration_seconds": 0.02,
                "latency_trace": {"final_polish_llm_duration_seconds": 0.01},
            }

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "verified": True, "send_result": {"send": {"state": "sent"}}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
            send_fn=sender,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "你好", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-06T10:00:00",
            )
            time.sleep(0.04)
            runtime.tick(allow_send=False, now="2026-06-06T10:00:01")
            time.sleep(0.04)
            runtime.tick(allow_send=True, now="2026-06-06T10:00:02")
            state = store.load()
            reply = next(iter((state.get("ready_replies") or {}).values()))
            trace = reply.get("latency_trace") if isinstance(reply.get("latency_trace"), dict) else {}
            for key in (
                "planner_future_submitted_at",
                "planner_future_finished_at",
                "planner_worker_started_at",
                "planner_worker_finished_at",
                "planner_worker_duration_seconds",
                "polish_future_submitted_at",
                "polish_future_finished_at",
                "polish_worker_started_at",
                "polish_worker_finished_at",
                "polish_worker_duration_seconds",
            ):
                assert_true(trace.get(key) is not None, f"future latency trace missing {key}: {trace}")
            assert_true(float(trace.get("planner_worker_duration_seconds") or 0.0) >= 0.02, f"planner worker duration should include callback wall time: {trace}")
            assert_true(float(trace.get("polish_worker_duration_seconds") or 0.0) >= 0.02, f"polish worker duration should include callback wall time: {trace}")
            breakdown = scheduler_module._latency_breakdown_from_trace(trace)
            assert_true("planner_future_seconds" in breakdown, f"planner future breakdown missing: {breakdown}")
            assert_true("planner_worker_seconds" in breakdown, f"planner worker breakdown missing: {breakdown}")
            assert_true("polish_future_seconds" in breakdown, f"polish future breakdown missing: {breakdown}")
            assert_true("polish_worker_seconds" in breakdown, f"polish worker breakdown missing: {breakdown}")
            assert_true(float(breakdown.get("planner_external_overhead_seconds", 0.0)) >= 0.0, f"planner overhead should be nonnegative: {breakdown}")
            assert_true(float(breakdown.get("polish_external_overhead_seconds", 0.0)) >= 0.0, f"polish overhead should be nonnegative: {breakdown}")
        finally:
            runtime.shutdown()


def check_planner_event_internal_latency_trace_is_extracted() -> None:
    event = {
        "customer_service_brain": {
            "latency_trace": {
                "evidence_pack_started_at": "2026-06-06T10:00:01",
                "evidence_pack_finished_at": "2026-06-06T10:00:02",
                "evidence_pack_duration_seconds": 1.0,
                "brain_llm_duration_seconds": 2.5,
                "semantic_review_duration_seconds": 0.2,
            }
        },
        "final_visible_llm_polish": {
            "duration_seconds": 0.7,
            "latency_trace": {
                "final_visible_llm_started_at": "2026-06-06T10:00:03",
                "final_visible_llm_finished_at": "2026-06-06T10:00:04",
            },
        },
    }
    trace = scheduler_module._planner_event_latency_trace(event)
    assert_equal(trace.get("evidence_pack_duration_seconds"), 1.0, "Brain evidence timing should be extracted")
    assert_equal(trace.get("brain_llm_duration_seconds"), 2.5, "Brain LLM timing should be extracted")
    assert_equal(trace.get("semantic_review_duration_seconds"), 0.2, "semantic review timing should be extracted")
    assert_equal(trace.get("final_polish_llm_duration_seconds"), 0.7, "final polish duration should be extracted")
    assert_equal(trace.get("final_visible_llm_started_at"), "2026-06-06T10:00:03", "final polish trace should be preserved")


def check_scheduler_fast_followup_treats_unread_and_capture_as_urgent() -> None:
    signal_result = {
        "scheduler_enabled": True,
        "summary": {"pending_sessions": 1, "llm_running": 0, "reply_ready": 0, "reply_sent": 0},
        "events": [{"event": "signal_pending", "target_name": "客户A"}],
    }
    capture_result = {
        "scheduler_enabled": True,
        "summary": {"pending_sessions": 0, "llm_running": 1, "reply_ready": 0, "reply_sent": 0},
        "events": [{"event": "capture_completed", "target_name": "客户A"}],
    }
    assert_true(summarize_scheduler_tick_activity(signal_result)["urgent_followup"], "unread signal should trigger fast follow-up")
    assert_true(summarize_scheduler_tick_activity(capture_result)["urgent_followup"], "capture completion should trigger fast follow-up")


def check_runtime_repeated_unread_signal_does_not_stale_same_batch() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        first_message = message("A", 1)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [first_message], "batch": [first_message]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.08)
            return {"ok": True, "reply_text": "稳定回复", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(session_signals=[{"name": "客户A", "unread_detected": True}], allow_send=False, now="2026-05-25T10:00:00")
            runtime.tick(session_signals=[{"name": "客户A", "unread_detected": True}], allow_send=False, now="2026-05-25T10:00:01")
            time.sleep(0.1)
            result = runtime.tick(session_signals=[{"name": "客户A", "unread_detected": True}], allow_send=False, now="2026-05-25T10:00:02")
            assert_equal(result["summary"]["reply_ready"], 1, "repeated unread-only signal should leave one ready reply")
            assert_equal(result["summary"].get("reply_stale", 0), 0, "same batch must not become stale from repeated unread-only polling")
        finally:
            runtime.shutdown()


def check_runtime_send_runner_stales_before_send() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        sent: list[str] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "旧回复", "decision": {"rule_name": "unit"}}

        def freshness(reply: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "stale": True, "reason": "newer_message_arrived_during_reply_build"}

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            sent.append(str(reply.get("reply_text") or ""))
            return {"ok": True, "verified": True}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            freshness_fn=freshness,
            send_fn=sender,
        )
        try:
            runtime.tick(session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}], allow_send=False, now="2026-05-25T10:00:00")
            time.sleep(0.03)
            runtime.tick(allow_send=False, now="2026-05-25T10:00:01")
            result = runtime.tick(allow_send=True, now="2026-05-25T10:00:02")
            assert_equal(sent, [], "stale freshness result must block send")
            assert_equal(result["summary"]["reply_stale"], 1, "reply should be marked stale")
            events = result.get("events") or []
            recaptured = any(item.get("event") == "capture_completed" and item.get("target_name") == "客户A" for item in events)
            assert_true(
                result["summary"]["pending_sessions"] == 1 or recaptured,
                "stale reply should requeue the target or recapture it in the same optimized tick",
            )
        finally:
            runtime.shutdown()


def check_runtime_stale_reply_context_is_preserved_for_brain_repair() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1, content="第一条")], "batch": [message("A", 1, content="第一条")]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "旧回复草稿", "decision": {"rule_name": "unit"}}

        def freshness(reply: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "stale": True,
                "has_newer_messages": True,
                "reason": "newer_message_arrived_during_reply_build",
                "newer_messages": [{"id": "A-m-2", "sender": "customer", "content": "第二条追问"}],
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            freshness_fn=freshness,
            send_fn=lambda reply: {"ok": True, "verified": True},
        )
        try:
            runtime.tick(session_signals=[{"name": "客户A", "content": "第一条", "time": "10:00", "unread_detected": True}], allow_send=False, now="2026-05-25T10:00:00")
            time.sleep(0.03)
            runtime.tick(allow_send=False, now="2026-05-25T10:00:01")
            result = runtime.tick(allow_send=True, now="2026-05-25T10:00:02")
            state = store.load()
            session = session_by_name(state, "客户A")
            stale_context = session.get("stale_reply_context") if isinstance(session.get("stale_reply_context"), dict) else {}
            assert_true(bool(stale_context), f"stale reply context should be preserved for Brain repair: {state}")
            assert_equal(stale_context.get("unsent_brain_reply_sample"), "旧回复草稿", "unsent Brain draft should be retained as context only")
            assert_true(
                bool(session.get("pending_capture")) or str(session.get("status") or "") in {"captured", "llm_queued", "llm_running"},
                f"stale reply should keep the target in active recovery flow: {session}",
            )
            assert_true(
                any(item.get("event") == "scheduler_stale_reply_context_recorded" for item in state.get("events") or []),
                f"stale context should be auditable: {result}",
            )
        finally:
            runtime.shutdown()


def check_runtime_times_out_running_llm_task_without_swallowing_message() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        state = store.load()
        capture = record_capture_result(
            state,
            "客户A",
            messages=[message("A", 1, content="你好")],
            batch=[message("A", 1, content="你好")],
            now="2026-05-25T10:00:00",
        )
        task = enqueue_llm_task(state, capture["capture_id"], timeout_seconds=1, now="2026-05-25T10:00:01")
        mark_llm_started(state, task["task_id"], now="2026-05-25T10:00:02")
        store.save(state)

        def planner(capture_payload: dict[str, Any], task_payload: dict[str, Any]) -> dict[str, Any]:
            time.sleep(1.0)
            return {"ok": True, "reply_text": "迟到回复", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=lambda session: {"messages": [message("A", 1, content="你好")], "batch": [message("A", 1, content="你好")]},
            plan_reply_fn=planner,
        )
        try:
            runtime._planner_task_snapshots[task["task_id"]] = copy.deepcopy(task)
            runtime._planner_futures[task["task_id"]] = runtime._planner_executor.submit(planner, copy.deepcopy(capture), copy.deepcopy(task))
            result = runtime.tick(allow_send=False, now="2026-05-25T10:00:10")
            state = store.load()
            session = session_by_name(state, "客户A")
            assert_true(
                any(item.get("event") == "llm_task_timeout_recovered" for item in result.get("events") or []),
                f"running LLM timeout should be recovered: {result}",
            )
            assert_equal(session.get("status"), "llm_queued", "timed-out task should keep the same capture queued for Brain retry")
            assert_true(
                any(item.get("status") == "queued" for item in (state.get("llm_tasks") or {}).values() if isinstance(item, dict)),
                "message must remain in queued Brain work after timeout",
            )
        finally:
            runtime.shutdown()


def check_scheduler_config_derives_planner_timeout_from_brain_budget() -> None:
    config = SchedulerConfig.from_config(
        {
            "concurrency_scheduler": {"enabled": True},
            "customer_service_brain": {
                "timeout_seconds": 35,
                "large_prompt_timeout_seconds": 60,
                "fallback_timeout_seconds": 45,
                "quality_repair_timeout_seconds": 12,
            },
            "final_visible_llm_polish": {"timeout_seconds": 6},
        }
    )
    assert_true(
        int(config.planner_task_timeout_seconds) >= 82,
        f"planner task timeout should cover Brain main + repair budget, not default to 30s: {config}",
    )
    assert_true(
        int(config.polish_task_timeout_seconds) >= 16,
        f"polish task timeout should cover final-polish request budget plus scheduler overhead: {config}",
    )


def check_runtime_brain_budget_prevents_premature_scheduler_timeout() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_brain_budget", path=path)
        config = SchedulerConfig.from_config(
            {
                "concurrency_scheduler": {"enabled": True, "llm_max_concurrency": 1},
                "customer_service_brain": {
                    "timeout_seconds": 35,
                    "large_prompt_timeout_seconds": 60,
                    "fallback_timeout_seconds": 45,
                    "quality_repair_timeout_seconds": 12,
                },
            }
        )

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1, content="现在买车有什么优惠政策吗")], "batch": [message("A", 1, content="现在买车有什么优惠政策吗")]}

        def planner(capture_payload: dict[str, Any], task_payload: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.2)
            return {"ok": True, "reply_text": "优惠要看具体车型和成交方案，我先按现有政策帮您核一下。", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=config,
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[
                    {
                        "name": "客户A",
                        "content": "现在买车有什么优惠政策吗",
                        "time": "10:00",
                        "unread_detected": True,
                        "unread_badge": "visual_red_dot",
                    }
                ],
                allow_send=False,
                now="2026-06-11T10:00:00",
            )
            state = store.load()
            queued = next(task for task in (state.get("llm_tasks") or {}).values() if isinstance(task, dict))
            assert_true(
                int(queued.get("timeout_seconds") or 0) >= 82,
                f"queued planner task should inherit Brain-derived timeout: {queued}",
            )
            runtime.tick(allow_send=False, now="2026-06-11T10:00:40")
            mid_state = store.load()
            task = next(task for task in (mid_state.get("llm_tasks") or {}).values() if isinstance(task, dict))
            assert_true(
                task.get("status") in {"running", "completed"},
                f"40s scheduler tick must not prematurely kill a Brain task with 82s budget: {task}",
            )
            time.sleep(0.25)
            final = runtime.tick(allow_send=False, now="2026-06-11T10:00:41")
            assert_true(
                final["summary"].get("reply_ready", 0) >= 1,
                f"planner should eventually produce ready reply after longer scheduler budget: {final}",
            )
        finally:
            runtime.shutdown()


def check_preview_change_during_active_work_defers_capture_without_hopping() -> None:
    state = empty_state()
    first = message("A", 1, content="第一条消息")
    second = message("A", 2, content="第二条追问")
    record_session_signal(
        state,
        {
            "name": "客户A",
            "content": first["content"],
            "time": "10:00",
            "unread_detected": True,
            "unread_badge": "visual_red_dot",
            "conversation_type": "private",
        },
        now="2026-06-11T10:00:00",
    )
    mark_capture_started(state, "客户A", now="2026-06-11T10:00:01")
    capture = record_capture_result(
        state,
        "客户A",
        messages=[first],
        batch=[first],
        now="2026-06-11T10:00:02",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-11T10:00:03")
    mark_llm_started(state, task["task_id"], now="2026-06-11T10:00:04")

    record_session_signal(
        state,
        {"name": "客户A", "content": second["content"], "time": "10:01", "conversation_type": "private"},
        now="2026-06-11T10:00:05",
    )
    session = session_by_name(state, "客户A")
    assert_equal(
        session.get("pending_reason"),
        "session_signal_preview_changed_during_active_work",
        "preview-only follow-up during Brain work should be deferred, not discarded",
    )
    assert_equal(
        select_capture_sessions(state, limit=1),
        [],
        "deferred preview-only follow-up must not trigger foreground RPA hopping while Brain is active",
    )
    complete_llm_task(
        state,
        task["task_id"],
        reply_text="先回复第一条",
        decision={"rule_name": "unit"},
        now="2026-06-11T10:00:06",
    )
    assert_equal(
        select_capture_sessions(state, limit=1),
        [],
        "deferred follow-up should wait until the in-flight ready reply is sent",
    )
    reply = select_ready_replies(state, limit=1)[0]
    mark_reply_sending(state, str(reply.get("reply_id") or ""), now="2026-06-11T10:00:07")
    mark_reply_sent(
        state,
        str(reply.get("reply_id") or ""),
        send_result={"ok": True, "verified": True},
        now="2026-06-11T10:00:08",
    )
    next_capture = select_capture_sessions(state, limit=1)
    assert_equal(
        [item.get("target_name") for item in next_capture],
        ["客户A"],
        "deferred follow-up should become dispatchable after the previous reply is sent",
    )


def check_preview_change_without_unread_evidence_is_baseline_only_when_idle() -> None:
    state = empty_state()
    record_session_signal(
        state,
        {
            "name": "客户A",
            "content": "旧预览",
            "time": "10:00",
            "conversation_type": "private",
        },
        now="2026-06-11T10:00:00",
    )
    record_session_signal(
        state,
        {
            "name": "客户A",
            "content": "新预览但没有红点",
            "time": "10:01",
            "conversation_type": "private",
        },
        now="2026-06-11T10:01:00",
    )
    session = session_by_name(state, "客户A")
    assert_true(not bool(session.get("pending_capture")), f"idle preview drift without unread evidence should not enqueue capture: {session}")
    assert_equal(
        select_capture_sessions(state, limit=1),
        [],
        "idle badge-less preview drift must not drive foreground RPA switching",
    )


def check_reply_sent_preserves_followup_pending_signal() -> None:
    state = empty_state()
    first = message("A", 1, content="第一条消息")
    second = message("A", 2, content="第二条追问")
    record_session_signal(
        state,
        {
            "name": "客户A",
            "content": first["content"],
            "time": "10:00",
            "unread_detected": True,
            "unread_badge": "visual_red_dot",
            "conversation_type": "private",
        },
        now="2026-05-25T10:00:00",
    )
    mark_capture_started(state, "客户A", now="2026-05-25T10:00:01")
    capture = record_capture_result(
        state,
        "客户A",
        messages=[first],
        batch=[first],
        now="2026-05-25T10:00:02",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-05-25T10:00:03")
    mark_llm_started(state, task["task_id"], now="2026-05-25T10:00:04")

    record_session_signal(
        state,
        {"name": "客户A", "content": second["content"], "time": "10:01", "conversation_type": "private"},
        now="2026-05-25T10:00:05",
    )
    completion = complete_llm_task(
        state,
        task["task_id"],
        reply_text="先回复第一条",
        decision={"rule_name": "unit"},
        now="2026-05-25T10:00:06",
    )
    reply = completion.get("reply") if isinstance(completion.get("reply"), dict) else {}
    reply_id = str(reply.get("reply_id") or "")
    assert_true(bool(reply_id), "completion should generate one ready reply")
    mark_reply_sending(state, reply_id, now="2026-05-25T10:00:07")
    mark_reply_sent(state, reply_id, send_result={"ok": True, "verified": True}, now="2026-05-25T10:00:08")

    session = session_by_name(state, "客户A")
    assert_true(bool(session.get("pending_capture")), "follow-up signal must survive first send completion")
    next_capture = select_capture_sessions(state, limit=1)
    assert_equal([item.get("target_name") for item in next_capture], ["客户A"], "follow-up should stay queued for next capture")


def check_runtime_same_tick_fast_llm_send_has_capture_snapshot() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        sent: list[str] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "快速回复", "decision": {"rule_name": "unit"}}

        def freshness(reply: dict[str, Any]) -> dict[str, Any]:
            assert_true(isinstance(reply.get("_capture"), dict), "same-tick freshness callback should receive capture snapshot")
            return {"ok": True, "stale": False}

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            assert_true(isinstance(reply.get("_capture"), dict), "same-tick sender callback should receive capture snapshot")
            sent.append(str(reply.get("reply_text") or ""))
            return {"ok": True, "verified": True}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            freshness_fn=freshness,
            send_fn=sender,
        )
        try:
            runtime.tick(session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}], allow_send=True, now="2026-05-25T10:00:00")
            time.sleep(0.03)
            runtime.tick(allow_send=True, now="2026-05-25T10:00:01")
            assert_equal(sent, ["快速回复"], "fast same-tick/next-tick reply should send once")
        finally:
            runtime.shutdown()


def check_runtime_send_runner_fifo() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        sent: list[str] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            target = str(session.get("target_name") or "")
            prefix = "A" if target == "客户A" else "B"
            return {"messages": [message(prefix, 1)], "batch": [message(prefix, 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": f"回复{capture.get('target_name')}", "decision": {"rule_name": "unit"}}

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            sent.append(str(reply.get("target_name") or ""))
            return {"ok": True, "verified": True}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=2, llm_max_concurrency=2, send_max_replies_per_round=2),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            send_fn=sender,
        )
        try:
            runtime.tick(
                session_signals=[
                    {"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"},
                    {"name": "客户B", "content": "B新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"},
                ],
                allow_send=False,
                now="2026-05-25T10:00:00",
            )
            time.sleep(0.05)
            runtime.tick(allow_send=False, now="2026-05-25T10:00:01")
            runtime.tick(allow_send=True, now="2026-05-25T10:00:02")
            assert_equal(sent, ["客户A", "客户B"], "send runner should consume ready replies in FIFO order")
        finally:
            runtime.shutdown()


def check_runtime_send_event_includes_observability() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "可观测回复", "decision": {"rule_name": "unit"}}

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "verified": True,
                "send_result": {
                    "ok": True,
                    "verified": True,
                    "state": "sent",
                    "retry_attempts": 1,
                    "verification_mode": "verify_each_segment",
                    "segment_attempt_counts": [1, 2],
                    "send": {"state": "sent", "rpa_lock": {"action": "send", "waited_seconds": 0.44, "attempts": 3}},
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            send_fn=sender,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-05-25T10:00:00",
            )
            time.sleep(0.03)
            result = runtime.tick(allow_send=True, now="2026-05-25T10:00:01")
            events = [item for item in result.get("events") or [] if item.get("event") == "send_completed"]
            assert_true(events, f"send_completed event should exist: {result}")
            observability = events[0].get("send_observability")
            assert_true(isinstance(observability, dict), f"send event should include observability payload: {events[0]}")
            assert_equal(observability.get("retry_attempts"), 1, "retry attempts should surface in send event")
            assert_equal(observability.get("verification_mode"), "verify_each_segment", "verification mode should surface in send event")
            lock_meta = observability.get("rpa_lock") if isinstance(observability.get("rpa_lock"), dict) else {}
            assert_true(float(lock_meta.get("waited_seconds") or 0.0) > 0.0, f"lock wait should surface in send event: {observability}")
        finally:
            runtime.shutdown()


def check_runtime_prioritizes_ready_send_before_new_capture() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        state = store.load()
        capture_ready = record_capture_result(
            state,
            "客户已完成",
            messages=[message("R", 1)],
            batch=[message("R", 1)],
            now="2026-05-25T10:00:00",
        )
        task_ready = enqueue_llm_task(state, capture_ready["capture_id"], now="2026-05-25T10:00:01")
        mark_llm_started(state, task_ready["task_id"], now="2026-05-25T10:00:02")
        complete_llm_task(
            state,
            task_ready["task_id"],
            reply_text="已生成回复",
            decision={"rule_name": "unit"},
            now="2026-05-25T10:00:03",
        )
        enqueue_pending_session(state, "客户新消息", reason="unit_pending", now="2026-05-25T10:00:04")
        store.save(state)
        action_order: list[str] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            action_order.append(f"capture:{session.get('target_name')}")
            return {"messages": [message("N", 1)], "batch": [message("N", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "新回复", "decision": {"rule_name": "unit"}}

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            action_order.append(f"send:{reply.get('target_name')}")
            return {"ok": True, "verified": True}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            send_fn=sender,
        )
        try:
            result = runtime.tick(allow_send=True, now="2026-05-25T10:00:05")
            assert_equal(action_order[:2], ["send:客户已完成", "capture:客户新消息"], "ready replies should be sent before starting new RPA capture")
            assert_true(result["summary"]["reply_sent"] >= 1, "pre-existing ready reply should be marked sent before any new capture")
        finally:
            runtime.shutdown()


def check_runtime_recovers_orphaned_running_llm_task_after_restart() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        state = store.load()
        capture = record_capture_result(
            state,
            "客户A",
            messages=[message("A", 1)],
            batch=[message("A", 1)],
            now="2026-05-25T10:00:00",
        )
        task = enqueue_llm_task(state, capture["capture_id"], now="2026-05-25T10:00:01")
        mark_llm_started(state, task["task_id"], now="2026-05-25T10:00:02")
        store.save(state)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 2)], "batch": [message("A", 2)]}

        def planner(capture_payload: dict[str, Any], task_payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "重启后恢复回复", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            first = runtime.tick(allow_send=False, now="2026-05-25T10:00:03")
            assert_true(
                any(item.get("event") == "llm_task_orphan_requeued" for item in first.get("events") or []),
                f"orphaned running task should be requeued: {first}",
            )
            time.sleep(0.03)
            second = runtime.tick(allow_send=False, now="2026-05-25T10:00:04")
            assert_equal(second["summary"]["reply_ready"], 1, "recovered LLM task should complete into ready reply")
        finally:
            runtime.shutdown()


def check_runtime_restores_missing_llm_task_from_in_memory_snapshot() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            msg = message("A", 1, content="你好")
            return {"messages": [msg], "batch": [msg]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.05)
            return {"ok": True, "reply_text": "您好，在的。", "decision": {"rule_name": "unit"}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, llm_max_concurrency=1, planner_max_concurrency=1, send_max_replies_per_round=1),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "你好", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:15:00",
            )
            state = store.load()
            state["llm_tasks"] = {}
            store.save(state)
            time.sleep(0.08)
            result = runtime.tick(allow_send=False, now="2026-06-02T18:15:01")
            assert_equal(
                result["summary"].get("reply_ready", 0),
                1,
                "missing persisted llm task should be restored from in-memory snapshot",
            )
            restored = store.load()
            assert_true(bool(restored.get("ready_replies") or {}), "restored llm completion should still enqueue ready reply")
        finally:
            runtime.shutdown()


def check_runtime_recovers_orphaned_running_polish_task_after_restart() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        state = store.load()
        capture = record_capture_result(
            state,
            "客户A",
            messages=[message("A", 1)],
            batch=[message("A", 1)],
            now="2026-06-02T18:10:00",
        )
        task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-02T18:10:01")
        mark_llm_started(state, task["task_id"], now="2026-06-02T18:10:02")
        complete_llm_task(
            state,
            task["task_id"],
            reply_text="待润色草稿",
            decision={"rule_name": "unit"},
            create_ready_reply=False,
            now="2026-06-02T18:10:03",
        )
        polish_task = enqueue_polish_task(state, task["task_id"], now="2026-06-02T18:10:04")
        mark_polish_started(state, polish_task["task_id"], now="2026-06-02T18:10:05")
        store.save(state)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 2)], "batch": [message("A", 2)]}

        def planner(_capture_payload: dict[str, Any], _task_payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "不会被调用", "decision": {"rule_name": "unit"}}

        def polish(_planner_task: dict[str, Any], _task_payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "重启后恢复润色回复", "decision": {"rule_name": "unit", "polished": True}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
        )
        try:
            first = runtime.tick(allow_send=False, now="2026-06-02T18:10:06")
            assert_true(
                any(item.get("event") == "polish_task_orphan_requeued" for item in first.get("events") or []),
                f"orphaned running polish task should be requeued: {first}",
            )
            time.sleep(0.03)
            second = runtime.tick(allow_send=False, now="2026-06-02T18:10:07")
            assert_equal(second["summary"]["reply_ready"], 1, "recovered polish task should complete into ready reply")
        finally:
            runtime.shutdown()


def check_runtime_restores_missing_polish_task_from_in_memory_snapshot() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            msg = message("A", 1, content="在吗？")
            return {"messages": [msg], "batch": [msg]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.05)
            return {"ok": True, "reply_text": "在的，您说。", "decision": {"rule_name": "unit"}}

        def polish(planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.05)
            return {"ok": True, "reply_text": "在的，您说。", "decision": {"rule_name": "unit", "polished": True}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "在吗？", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:20:00",
            )
            time.sleep(0.08)
            runtime.tick(allow_send=False, now="2026-06-02T18:20:01")
            state = store.load()
            state["polish_tasks"] = {}
            store.save(state)
            time.sleep(0.08)
            result = runtime.tick(allow_send=False, now="2026-06-02T18:20:02")
            assert_equal(
                result["summary"].get("reply_ready", 0),
                1,
                "missing persisted polish task should be restored from in-memory snapshot",
            )
            restored = store.load()
            assert_true(bool(restored.get("ready_replies") or {}), "restored polish completion should still enqueue ready reply")
        finally:
            runtime.shutdown()


def check_runtime_degraded_polish_reply_still_sends() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        sent: list[dict[str, Any]] = []

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": f"草稿 {capture.get('target_name')}", "decision": {"rule_name": "unit"}}

        def polish(planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            result = planner_task.get("result") if isinstance(planner_task.get("result"), dict) else {}
            draft = str(result.get("reply_text") or "")
            return {
                "ok": True,
                "reply_text": draft,
                "decision": {"rule_name": "unit", "used_safe_draft": True},
                "degraded": True,
            }

        def sender(reply: dict[str, Any]) -> dict[str, Any]:
            sent.append(copy.deepcopy(reply))
            return {"ok": True, "verified": True}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
            freshness_fn=lambda reply: {"ok": True, "stale": False},
            send_fn=sender,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:12:00",
            )
            result = None
            for index, now_text in enumerate(
                [
                    "2026-06-02T18:12:01",
                    "2026-06-02T18:12:02",
                    "2026-06-02T18:12:03",
                    "2026-06-02T18:12:04",
                ],
                start=1,
            ):
                time.sleep(0.03)
                result = runtime.tick(allow_send=index >= 2, now=now_text)
                if sent:
                    break
            assert_true(isinstance(result, dict), "runtime should return a result dict during degraded-polish send loop")
            assert_equal(len(sent), 1, "degraded polish reply should still be sendable")
            assert_equal(str(sent[0].get("task_kind") or ""), "polish", "sent reply should originate from polish task when dual-pool mode is enabled")
            assert_true(bool(((sent[0].get("decision") or {}) if isinstance(sent[0].get("decision"), dict) else {}).get("used_safe_draft")), f"degraded polish metadata should survive send path: {sent[0]}")
            assert_equal(result["summary"]["reply_sent"], 1, "degraded polish reply should reach sent state")
        finally:
            runtime.shutdown()


def check_runtime_polish_failure_result_is_json_safe() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message("A", 1)], "batch": [message("A", 1)]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "reply_text": "Brain草稿", "decision": {"rule_name": "customer_service_brain_reply", "visible_reply_owner": "brain"}}

        def polish(_planner_task: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {"ok": False, "reason": "unit_polish_failed", "error_object": RuntimeError("boom")}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
        )
        try:
            initial = runtime.tick(
                session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:30:00",
            )
            result = initial
            events: list[dict[str, Any]] = [item for item in (initial.get("events") or []) if isinstance(item, dict)]
            for offset in range(1, 8):
                time.sleep(0.05)
                result = runtime.tick(allow_send=False, now=f"2026-06-02T18:30:0{offset}")
                events.extend(item for item in (result.get("events") or []) if isinstance(item, dict))
                if any(item.get("event") == "polish_task_failed" and item.get("reason") == "unit_polish_failed" for item in events):
                    break
            assert_true(
                any(item.get("event") == "polish_task_failed" and item.get("reason") == "unit_polish_failed" for item in events),
                f"polish failure should be recorded without crashing scheduler: {events}",
            )
            state = store.load()
            failed = [item for item in (state.get("polish_tasks") or {}).values() if item.get("status") == "failed"]
            assert_equal(len(failed), 1, f"failed polish task should persist: {state}")
            assert_equal((failed[0].get("result") or {}).get("reason"), "unit_polish_failed", "failure result should be JSON-safe persisted")
            assert_true(isinstance((failed[0].get("result") or {}).get("error_object"), str), "non-JSON values should be stringified")
        finally:
            runtime.shutdown()


def check_runtime_final_polish_block_requeues_brain_with_feedback() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit_final_polish_requeue", path=path)
        customer_message = {
            "id": "identity-probe-1",
            "type": "text",
            "sender": "customer",
            "content": "你是不是机器人在回我？怎么感觉每句都往车上绕。",
            "time": "2026-06-12T14:20:00",
        }

        def capture_fn(_session: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "session_key": "wx:rpa:v1:identity-polish-requeue",
                "messages": [copy.deepcopy(customer_message)],
                "batch": [copy.deepcopy(customer_message)],
                "history_backfill": {"anchor_found": True},
            }

        def planner(_capture: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "reply_text": "哈哈被您发现了，我确实聊得太急了。",
                "decision": {
                    "rule_name": "customer_service_brain_reply",
                    "visible_reply_owner": "brain",
                },
                "event": {
                    "customer_service_brain": {
                        "visible_reply_owner": "brain",
                        "reply_text": "哈哈被您发现了，我确实聊得太急了。",
                    }
                },
            }

        def polish(_planner_task: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "reason": "final_visible_llm_polish_failed",
                "event": {
                    "final_visible_llm_polish": {
                        "passed": False,
                        "reason": "identity_truth_discussion_not_allowed",
                        "candidate": {"reply": "哈哈被您发现了，我确实聊得太急了。"},
                        "guard": {"allowed": False, "reason": "identity_truth_discussion_not_allowed"},
                    }
                },
            }

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
        )
        try:
            events: list[dict[str, Any]] = []
            initial = runtime.tick(
                session_signals=[
                    {
                        "name": "新数据测试",
                        "content": "你是不是机器人在回我？怎么感觉每句都往车上绕。",
                        "time": "2026-06-12T14:20:00",
                        "unread_detected": True,
                        "unread_badge": "visual_red_dot",
                        "conversation_type": "private",
                        "session_key": "wx:rpa:v1:identity-polish-requeue",
                    }
                ],
                allow_send=False,
                now="2026-06-12T14:20:01",
            )
            events.extend(item for item in (initial.get("events") or []) if isinstance(item, dict))
            for offset in range(1, 12):
                time.sleep(0.05)
                result = runtime.tick(allow_send=False, now=f"2026-06-12T14:20:{offset + 1:02d}")
                events.extend(item for item in (result.get("events") or []) if isinstance(item, dict))
                if any(item.get("event") == "llm_task_failed_requeued_planner" for item in events):
                    break
            assert_true(
                any(item.get("event") == "llm_task_failed_requeued_planner" for item in events),
                f"final polish block must requeue the same Brain task instead of swallowing the message: {events}",
            )
            state = store.load()
            session = state.get("sessions", {}).get("wx:rpa:v1:identity-polish-requeue", {})
            assert_equal(session.get("status"), "llm_queued", f"session should wait for Brain retry: {session}")
            queued = [
                task
                for task in (state.get("llm_tasks") or {}).values()
                if isinstance(task, dict) and task.get("status") == "queued"
            ]
            assert_equal(len(queued), 1, f"planner task should be requeued once: {state}")
            retry_instruction = scheduler_module.polish_failure_retry_instruction(queued[0])
            assert_true(
                "不能承认或否认身份" in retry_instruction,
                f"Brain retry should carry final-polish identity feedback: {retry_instruction}",
            )
            assert_equal((queued[0].get("last_polish_failure") or {}).get("reason"), "final_visible_llm_polish_failed", "polish failure audit should be retained")
        finally:
            runtime.shutdown()


def check_captured_messages_connector_accepts_history_kwargs() -> None:
    capture = {
        "capture_id": "capture-history-kwargs",
        "target_name": "客户A",
        "context_version": 1,
        "messages": [message("A", 1)],
        "history_backfill": {"enabled": True, "mode": "anchor_until_found", "gap_risk": False},
    }
    connector = CapturedMessagesConnector(capture)
    payload = connector.get_messages(
        "客户A",
        exact=True,
        history_load_times=0,
        history_mode="anchor_until_found",
        anchor_content_keys=["unit"],
        max_scroll_steps=3,
    )
    assert_true(payload.get("ok") is True, f"captured connector should ignore RPA history kwargs safely: {payload}")
    assert_equal(payload.get("scheduler_capture_id"), "capture-history-kwargs", "capture id should be preserved")


def check_captured_messages_connector_uses_batch_when_messages_empty() -> None:
    capture = {
        "capture_id": "capture-short-pending-batch",
        "target_name": "客户A",
        "context_version": 2,
        "messages": [],
        "batch": [
            {
                "id": "short_pending:客户A:unit",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "content": "在吗",
                "short_pending_recovered": True,
                "short_pending_synthesized_from_monitor": True,
            }
        ],
        "history_backfill": {
            "enabled": True,
            "reason": "visible_anchor_found_no_scroll",
            "short_pending_recovered_from_anchor_empty": True,
        },
    }
    payload = CapturedMessagesConnector(capture).get_messages("客户A", exact=True)
    assert_true(payload.get("ok") is True, f"captured connector should return ok: {payload}")
    assert_true(payload.get("scheduler_used_batch_fallback") is True, f"empty messages should fall back to batch: {payload}")
    assert_equal(payload.get("messages", [{}])[0].get("content"), "在吗", "short pending batch content must reach the planner")
    assert_equal(
        payload.get("_history_backfill", {}).get("short_pending_recovered_from_anchor_empty"),
        True,
        "history recovery metadata should be preserved for audit",
    )


def check_scheduler_capture_filters_self_only_normal_customer_session() -> None:
    state = empty_state()
    self_message = {
        "id": "self-visible-1",
        "type": "text",
        "sender": "self",
        "content": "这是之前客服自己发出的内容",
        "time": "2026-06-07T14:52:00",
    }
    capture = record_capture_result(
        state,
        "许聪",
        messages=[self_message],
        batch=[self_message],
        batch_selection={"message_ids": ["self-visible-1"], "eligible_count": 1},
        conversation_type="private",
        session_key="wx:rpa:v1:normal-self-filter",
        now="2026-06-07T14:52:01",
    )
    assert_equal(capture.get("status"), "empty", "normal customer self-only capture must not become reply task")
    assert_equal(capture.get("batch"), [], "normal customer reply batch must filter self-authored bubbles")
    assert_equal(capture.get("message_ids"), [], "self-authored bubbles must not become input anchors")
    assert_equal(capture.get("reply_input_message_count"), 0, "reply input count should be zero after filtering")
    assert_equal(capture.get("filtered_non_customer_message_count"), 1, "filtered self bubble should be observable")
    assert_equal(
        capture.get("batch_selection", {}).get("message_ids"),
        ["self-visible-1"],
        "capture should retain selector audit metadata even when state-layer filtering rejects the batch",
    )
    session = session_by_name(state, "许聪")
    assert_equal(session.get("status"), "idle", "self-only capture should leave session idle")
    assert_equal(int(session.get("context_version") or 0), 0, "self-only capture must not advance context version")
    assert_equal(state.get("llm_tasks"), {}, "self-only capture must not enqueue LLM work")


def check_scheduler_capture_allows_new_occurrence_of_same_short_probe() -> None:
    state = empty_state()
    old_message = {
        "id": "win32_ocr:same-short-probe",
        "type": "text",
        "sender": "unknown",
        "content": "在不",
        "time": "2026-06-12T11:16:56",
    }
    first = record_capture_result(
        state,
        "许聪",
        messages=[old_message],
        batch=[old_message],
        conversation_type="private",
        session_key="wx:rpa:v1:repeatable-short",
        now="2026-06-12T11:17:00",
    )
    task = enqueue_llm_task(state, str(first.get("capture_id") or ""), now="2026-06-12T11:17:01")
    complete_llm_task(
        state,
        str(task.get("task_id") or ""),
        reply_text="在的，您说。",
        result_payload={"ok": True, "reply_text": "在的，您说。"},
        now="2026-06-12T11:17:02",
    )
    reply_id = next(iter(state.get("ready_replies") or {}))
    mark_reply_sent(state, reply_id, send_result={"ok": True, "verified": True}, now="2026-06-12T11:17:03")

    new_message = {
        "id": "win32_ocr:same-short-probe",
        "type": "text",
        "sender": "unknown",
        "content": "在不",
        "time": "2026-06-12T11:21:15",
    }
    second = record_capture_result(
        state,
        "许聪",
        messages=[new_message],
        batch=[new_message],
        conversation_type="private",
        session_key="wx:rpa:v1:repeatable-short",
        now="2026-06-12T11:21:16",
    )
    assert_equal(second.get("status"), "captured", "new occurrence of same short probe must not be swallowed")
    assert_true(
        second.get("message_ids") and second.get("message_ids") != first.get("message_ids"),
        f"repeatable short probe ids should differ by occurrence: first={first.get('message_ids')} second={second.get('message_ids')}",
    )


def check_scheduler_capture_filters_non_text_messages_for_normal_customer_session() -> None:
    state = empty_state()
    image_message = {
        "id": "img-visible-1",
        "type": "image",
        "sender": "customer",
        "content": "[图片]",
        "time": "2026-06-07T14:52:00",
    }
    capture = record_capture_result(
        state,
        "许聪",
        messages=[image_message],
        batch=[image_message],
        conversation_type="private",
        session_key="wx:rpa:v1:normal-non-text-filter",
        now="2026-06-07T14:52:01",
    )
    assert_equal(capture.get("status"), "empty", "non-text capture must not become a customer-service Brain task")
    assert_equal(capture.get("batch"), [], "non-text messages should not be reply input batch")
    assert_equal(capture.get("filtered_non_customer_message_count"), 1, "filtered non-text message should be observable")


def check_scheduler_capture_filters_visual_ocr_text_without_keyword_blocking() -> None:
    state = empty_state()
    visual_message = {
        "id": "visual-ocr-text-1",
        "type": "text",
        "sender": "customer",
        "content": "poster headline ABC 123",
        "source_type": "image_ocr",
        "time": "2026-06-20T09:02:00",
    }
    capture = record_capture_result(
        state,
        "许聪",
        messages=[visual_message],
        batch=[visual_message],
        conversation_type="private",
        session_key="wx:rpa:v1:visual-ocr-filter",
        now="2026-06-20T09:02:01",
    )
    assert_equal(capture.get("status"), "empty", "visual OCR text must not become a customer-service Brain task")
    assert_equal(capture.get("batch"), [], "visual OCR text should be excluded from reply input batch")
    assert_equal(capture.get("filtered_non_customer_message_count"), 1, "visual OCR filter should be observable")

    normal_message = {
        **visual_message,
        "id": "normal-text-same-content-1",
        "source_type": "chat_text",
        "time": "2026-06-20T09:03:00",
    }
    normal_capture = record_capture_result(
        state,
        "许聪",
        messages=[normal_message],
        batch=[normal_message],
        conversation_type="private",
        session_key="wx:rpa:v1:normal-text-same-content",
        now="2026-06-20T09:03:01",
    )
    assert_equal(normal_capture.get("status"), "captured", "same content should remain reply-eligible when it is real chat text")
    assert_equal(
        [item.get("id") for item in normal_capture.get("batch") or []],
        ["normal-text-same-content-1"],
        "visual OCR guard must be source-based, not keyword-based",
    )

    nested_visual_message = {
        "id": "visual-ocr-text-nested-1",
        "type": "text",
        "sender": "customer",
        "content": "poster headline nested ABC 123",
        "metadata": {"source_type": "poster_ocr"},
        "time": "2026-06-20T09:04:00",
    }
    nested_capture = record_capture_result(
        state,
        "许聪",
        messages=[nested_visual_message],
        batch=[nested_visual_message],
        conversation_type="private",
        session_key="wx:rpa:v1:visual-ocr-nested-filter",
        now="2026-06-20T09:04:01",
    )
    assert_equal(nested_capture.get("status"), "empty", "nested visual OCR metadata should also be reply-ineligible")
    assert_equal(nested_capture.get("batch"), [], "nested visual OCR message should not enter reply batch")


def check_scheduler_capture_allows_self_for_file_transfer_self_test() -> None:
    state = empty_state()
    self_message = {
        "id": "fta-self-1",
        "type": "text",
        "sender": "self",
        "content": "文件传输助手自测问题",
        "time": "2026-06-07T14:52:00",
    }
    capture = record_capture_result(
        state,
        "文件传输助手",
        messages=[self_message],
        batch=[self_message],
        conversation_type="file_transfer",
        session_key="wx:rpa:v1:file-transfer-self-test",
        now="2026-06-07T14:52:01",
    )
    assert_equal(capture.get("status"), "captured", "file-transfer self-test should still be reply-eligible")
    assert_equal([item.get("id") for item in capture.get("batch") or []], ["fta-self-1"], "file-transfer self batch should be kept")
    assert_equal(capture.get("filtered_non_customer_message_count"), 0, "file-transfer self-test should not be filtered")


def check_runtime_self_only_capture_does_not_submit_llm() -> None:
    root = Path(tempfile.mkdtemp(prefix="scheduler-self-only-"))
    path = root / "state.json"
    store = SchedulerStateStore(tenant_id="unit_self_only", path=path)
    state = store.empty_state()
    record_session_signal(
        state,
        {
            "name": "许聪",
            "content": "之前客服自己发出的内容",
            "unread_detected": True,
            "unread_badge": "1",
            "conversation_type": "private",
            "session_key": "wx:rpa:v1:self-only-runtime",
        },
        now="2026-06-07T14:52:00",
    )
    store.save(state)
    planner_calls: list[dict[str, Any]] = []

    def capture_fn(_session: dict[str, Any]) -> dict[str, Any]:
        self_message = {
            "id": "self-visible-runtime-1",
            "type": "text",
            "sender": "self",
            "content": "之前客服自己发出的内容",
            "time": "2026-06-07T14:52:00",
        }
        return {"ok": True, "messages": [self_message], "batch": [self_message]}

    runtime = CustomerServiceSchedulerRuntime(
        store=store,
        config=SchedulerConfig(enabled=True, capture_max_sessions_per_round=1, planner_max_concurrency=1),
        capture_fn=capture_fn,
        plan_reply_fn=lambda capture, task: planner_calls.append({"capture": capture, "task": task}) or {"ok": True, "reply_text": "不应执行"},
        freshness_fn=lambda _reply_payload: {"ok": True, "stale": False},
        send_fn=lambda _reply_payload: {"ok": True, "verified": True},
    )
    result = runtime.tick(allow_send=False, now="2026-06-07T14:52:02")
    events = result.get("events") or []
    assert_true(any(item.get("event") == "capture_empty" and item.get("target_name") == "许聪" for item in events), f"self-only runtime capture should be empty: {events}")
    assert_equal(planner_calls, [], "self-only capture must not submit planner work")
    reloaded = store.load()
    assert_equal(reloaded.get("llm_tasks"), {}, "self-only capture must not persist queued LLM tasks")
    session = session_by_name(reloaded, "许聪")
    assert_true(not session.get("pending_capture"), "self-only capture must clear pending capture")
    assert_equal(session.get("status"), "idle", "self-only capture should settle idle")


def check_scheduler_planner_reuses_capture_history_backfill_verdict() -> None:
    capture = {
        "capture_id": "capture-history-reuse",
        "target_name": "客户A",
        "context_version": 1,
        "messages": [message("A", 1)],
        "history_backfill": {
            "enabled": True,
            "mode": "anchor_until_found",
            "reason": "visible_anchor_found_no_scroll",
            "gap_risk": False,
        },
    }
    connector = CapturedMessagesConnector(capture)
    payload = connector.get_messages("客户A", exact=True)
    target = TargetConfig(name="客户A", enabled=True, exact=True, allow_self_for_test=False, max_batch_messages=3)
    enriched = maybe_enrich_messages_with_history(
        connector=connector,
        target=target,
        config={"history_backfill": {"enabled": True, "mode": "anchor_until_found"}},
        payload=payload,
        target_state={"processed_message_ids": ["anchor-old"], "processed_content_keys": ["anchor-key"]},
    )
    history = enriched.get("_history_backfill") if isinstance(enriched.get("_history_backfill"), dict) else {}
    assert_true(history.get("planner_reused_scheduler_capture") is True, f"planner should trust scheduler capture history verdict: {history}")
    assert_true(history.get("gap_risk") is False, f"scheduler capture gap verdict should remain false: {history}")


def check_workflow_planner_uses_captured_messages_without_sending() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    rules = load_rules(Path(config["rules_path"]))
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    capture = {
        "capture_id": "capture-unit-greeting",
        "target_name": "文件传输助手",
        "context_version": 1,
        "messages": [{"id": "unit-greet-1", "type": "text", "sender": "self", "content": "你好"}],
        "history_backfill": {"enabled": False},
    }
    planned = plan_reply_with_listen_workflow(
        capture,
        {"task_id": "task-unit-greeting"},
        target_config=target,
        config=config,
        rules=rules,
        workflow_state={"targets": {}},
        allow_fallback_send=True,
    )
    assert_true(bool(planned.get("ok")), f"workflow planner should build reply from captured messages: {planned}")
    reply_text = str(planned.get("reply_text") or "")
    decision = planned.get("decision") if isinstance(planned.get("decision"), dict) else {}
    assert_true(
        decision.get("rule_name") == "realtime_friendly_social_greeting",
        f"planned reply should use friendly greeting rule: {planned}",
    )
    assert_true(
        ("您说" in reply_text) or ("慢慢说" in reply_text) or ("直接说" in reply_text),
        f"planned greeting reply should stay brief and warm: {planned}",
    )
    assert_true("预算" not in reply_text and "用途" not in reply_text and "二手车" not in reply_text, f"pure greeting should not force sales redirect: {planned}")
    event = planned.get("event") or {}
    assert_equal(event.get("action"), "planned", "planner must not send through captured connector")


def check_workflow_planner_handles_short_pending_batch_fallback() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    rules = load_rules(Path(config["rules_path"]))
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    capture = {
        "capture_id": "capture-short-pending-planner",
        "target_name": "文件传输助手",
        "context_version": 3,
        "messages": [],
        "batch": [
            {
                "id": "short_pending:file-transfer:unit",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "content": "在吗",
                "short_pending_recovered": True,
                "short_pending_synthesized_from_monitor": True,
            }
        ],
        "history_backfill": {
            "enabled": True,
            "reason": "visible_anchor_found_no_scroll",
            "short_pending_recovered_from_anchor_empty": True,
        },
    }
    planned = plan_reply_with_listen_workflow(
        capture,
        {"task_id": "task-short-pending-planner"},
        target_config=target,
        config=config,
        rules=rules,
        workflow_state={"targets": {}},
        allow_fallback_send=True,
    )
    assert_true(bool(planned.get("ok")), f"workflow planner should reply to short pending batch fallback: {planned}")
    reply_text = str(planned.get("reply_text") or "")
    decision = planned.get("decision") if isinstance(planned.get("decision"), dict) else {}
    assert_true(decision.get("rule_name") == "realtime_friendly_social_greeting", f"short pending greeting should stay social: {planned}")
    assert_true(reply_text.strip(), f"short pending fallback should produce visible reply text: {planned}")
    assert_true("预算" not in reply_text and "用途" not in reply_text and "二手车" not in reply_text, f"short pending greeting should not force sales redirect: {planned}")


def check_scheduler_authoritative_short_batch_bypasses_legacy_content_key_dedupe() -> None:
    payload = {
        "ok": True,
        "messages": [
            {
                "id": "short_pending:xinshuju:evening",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "content": "晚上好",
                "short_pending_recovered": True,
                "short_pending_synthesized_from_monitor": True,
                "pending_signal_kind": "high_sensitivity_short",
            }
        ],
        "_scheduler_authoritative_batch": [
            {
                "id": "short_pending:xinshuju:evening",
                "type": "text",
                "sender": "unknown",
                "sender_role": "unknown",
                "content": "晚上好",
                "short_pending_recovered": True,
                "short_pending_synthesized_from_monitor": True,
                "pending_signal_kind": "high_sensitivity_short",
            }
        ],
        "_scheduler_authoritative_batch_ids": ["short_pending:xinshuju:evening"],
        "_scheduler_capture_is_authoritative": True,
    }
    target_state = {
        "processed_message_ids": ["old-evening"],
        "processed_content_keys": ["unknown\x1ftext\x1f晚上好"],
        "handoff_message_ids": [],
        "sent_replies": [],
    }
    legacy_selection = select_batch_details(
        payload["messages"],
        target_state=target_state,
        allow_self_for_test=False,
        max_batch_messages=3,
        config={},
    )
    assert_equal(legacy_selection.eligible_count, 0, "legacy content-key dedupe should reproduce the old short-message drop")
    authoritative_selection = select_scheduler_authoritative_batch_details(
        payload,
        target_state=target_state,
        allow_self_for_test=False,
        max_batch_messages=3,
        config={},
    )
    assert_equal(
        [item.get("id") for item in authoritative_selection.batch],
        ["short_pending:xinshuju:evening"],
        "scheduler-authoritative short pending batch must still enter Brain planning",
    )


def check_workflow_planner_uses_warm_short_farewell_without_sales_redirect() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    rules = load_rules(Path(config["rules_path"]))
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    capture = {
        "capture_id": "capture-unit-farewell",
        "target_name": "文件传输助手",
        "context_version": 1,
        "messages": [{"id": "unit-bye-1", "type": "text", "sender": "self", "content": "再见"}],
        "history_backfill": {"enabled": False},
    }
    planned = plan_reply_with_listen_workflow(
        capture,
        {"task_id": "task-unit-farewell"},
        target_config=target,
        config=config,
        rules=rules,
        workflow_state={"targets": {}},
        allow_fallback_send=True,
    )
    assert_true(bool(planned.get("ok")), f"workflow planner should build farewell reply from captured messages: {planned}")
    reply_text = str(planned.get("reply_text") or "")
    decision = planned.get("decision") if isinstance(planned.get("decision"), dict) else {}
    assert_true(
        decision.get("rule_name") == "realtime_friendly_farewell",
        f"planned farewell reply should use local farewell rule: {planned}",
    )
    assert_true(
        ("先忙" in reply_text) or ("再聊" in reply_text) or ("喊我" in reply_text) or ("辛苦" in reply_text),
        f"farewell reply should stay warm and concise: {planned}",
    )
    assert_true("预算" not in reply_text and "用途" not in reply_text and "二手车" not in reply_text, f"farewell should not force sales redirect: {planned}")


def check_scheduler_planner_applies_final_visible_polish_without_sending() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    config["final_visible_llm_polish"] = {
        "enabled": True,
        "required_for_send": True,
        "provider": "manual_json",
        "candidate": {
            "reply": "您好，这边在的，您直接说需求就行。",
            "confidence": 1.0,
            "reason": "unit test scheduler final polish",
        },
    }
    rules = load_rules(Path(config["rules_path"]))
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    capture = {
        "capture_id": "capture-unit-polish",
        "target_name": "文件传输助手",
        "context_version": 1,
        "messages": [{"id": "unit-polish-1", "type": "text", "sender": "self", "content": "你好"}],
        "history_backfill": {"enabled": False},
    }
    planned = plan_reply_with_listen_workflow(
        capture,
        {"task_id": "task-unit-polish"},
        target_config=target,
        config=config,
        rules=rules,
        workflow_state={"targets": {}},
        allow_fallback_send=True,
    )
    assert_true(bool(planned.get("ok")), f"scheduler planner should pass final polish: {planned}")
    reply_text = str(planned.get("reply_text") or "")
    assert_true(reply_text.endswith("您好，这边在的，您直接说需求就行。"), f"planned reply should include final polished body: {planned}")
    decision = planned.get("decision") if isinstance(planned.get("decision"), dict) else {}
    polish = decision.get("final_visible_llm_polish") if isinstance(decision.get("final_visible_llm_polish"), dict) else {}
    assert_true(polish.get("passed") is True, f"final polish metadata should be retained: {planned}")
    event = planned.get("event") if isinstance(planned.get("event"), dict) else {}
    assert_true("send_result" not in event, f"planner must still avoid RPA send: {event}")


def check_scheduler_split_polish_stage_preserves_final_visible_polish_quality() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    config["final_visible_llm_polish"] = {
        "enabled": True,
        "required_for_send": True,
        "provider": "manual_json",
        "candidate": {
            "reply": "您好，这边在的，您直接说需求就行。",
            "confidence": 1.0,
            "reason": "unit test split polish stage",
        },
    }
    rules = load_rules(Path(config["rules_path"]))
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    capture = {
        "capture_id": "capture-unit-polish-split",
        "target_name": "文件传输助手",
        "context_version": 1,
        "messages": [{"id": "unit-polish-split-1", "type": "text", "sender": "self", "content": "你好"}],
        "history_backfill": {"enabled": False},
    }
    planned = plan_reply_with_listen_workflow(
        capture,
        {"task_id": "task-unit-polish-split"},
        target_config=target,
        config=config,
        rules=rules,
        workflow_state={"targets": {}},
        allow_fallback_send=True,
        apply_final_visible_polish=False,
    )
    assert_true(bool(planned.get("ok")), f"planner-only stage should build a draft reply: {planned}")
    planned_decision = planned.get("decision") if isinstance(planned.get("decision"), dict) else {}
    assert_true(
        not isinstance(planned_decision.get("final_visible_llm_polish"), dict),
        f"planner-only stage should not already finalize visible polish: {planned}",
    )
    planner_task = {
        "task_id": "planner-task-unit-polish-split",
        "target_name": target.name,
        "capture_ids": [capture["capture_id"]],
        "input_context_version": capture["context_version"],
        "input_message_ids": [],
        "input_content_keys": [],
        "result": planned,
    }
    polished = polish_reply_with_listen_workflow(
        planner_task,
        {"task_id": "polish-task-unit-polish-split"},
        target_config=target,
        config=config,
        workflow_state={"targets": {}},
    )
    assert_true(bool(polished.get("ok")), f"split polish stage should pass final polish: {polished}")
    reply_text = str(polished.get("reply_text") or "")
    assert_true(reply_text.endswith("您好，这边在的，您直接说需求就行。"), f"split polish should append polished body: {polished}")
    decision = polished.get("decision") if isinstance(polished.get("decision"), dict) else {}
    polish = decision.get("final_visible_llm_polish") if isinstance(decision.get("final_visible_llm_polish"), dict) else {}
    assert_true(polish.get("passed") is True, f"split polish metadata should be retained: {polished}")


def check_scheduler_split_polish_stage_degrades_to_brain_draft_when_polish_unavailable() -> None:
    config = load_config(APP_ROOT / "configs" / "file_transfer_smoke.example.json")
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("customer_profiles", {})["enabled"] = False
    config.setdefault("customer_service_brain", {})["enabled"] = True
    config["customer_service_brain"]["mode"] = "authoritative"
    config["final_visible_llm_polish"] = {
        "enabled": True,
        "required_for_send": True,
        "allow_send_when_unavailable": True,
        "provider": "manual_json",
    }
    target = TargetConfig(
        name="文件传输助手",
        enabled=True,
        exact=True,
        allow_self_for_test=True,
        max_batch_messages=3,
    )
    draft = "在的，刚才表达有点急了。您想随便聊聊也可以，有具体车源问题我再接着帮您看。"
    planner_task = {
        "task_id": "planner-task-unit-polish-degrade",
        "target_name": target.name,
        "capture_ids": ["capture-unit-polish-degrade"],
        "input_context_version": 1,
        "input_message_ids": ["unit-polish-degrade-1"],
        "input_content_keys": [],
        "result": {
            "ok": True,
            "target_name": target.name,
            "reply_text": draft,
            "decision": {
                "reply_text": draft,
                "rule_name": "customer_service_brain_reply",
                "matched": True,
                "need_handoff": False,
                "reason": "guard_passed",
                "visible_reply_owner": "brain",
                "brain_first_visible_reply_required": True,
            },
            "event": {
                "ok": True,
                "target": target.name,
                "action": "planned",
                "combined_content": "你是不是机器人在回我？怎么感觉每句都往车上绕。",
                "customer_service_brain": {
                    "applied": True,
                    "adoptable": True,
                    "visible_reply_owner": "brain",
                    "reason": "guard_passed",
                },
                "customer_service_brain_adopted": True,
                "decision": {
                    "reply_text": draft,
                    "rule_name": "customer_service_brain_reply",
                    "visible_reply_owner": "brain",
                    "brain_first_visible_reply_required": True,
                },
                "reply_style_adapter": {"source_channel": "brain"},
            },
        },
    }
    polished = polish_reply_with_listen_workflow(
        planner_task,
        {"task_id": "polish-task-unit-polish-degrade"},
        target_config=target,
        config=config,
        workflow_state={"targets": {}},
    )
    assert_true(bool(polished.get("ok")), f"split polish should degrade to Brain draft when polish unavailable: {polished}")
    assert_equal(str(polished.get("reply_text") or ""), draft, "Brain draft should be preserved as fallback reply")
    assert_true(bool(polished.get("degraded")), f"split polish should record degraded status: {polished}")
    decision = polished.get("decision") if isinstance(polished.get("decision"), dict) else {}
    final_polish = decision.get("final_visible_llm_polish") if isinstance(decision.get("final_visible_llm_polish"), dict) else {}
    assert_equal(final_polish.get("reply_text"), draft, "final polish metadata should retain fallback Brain draft")
    assert_true(final_polish.get("passed") is not True, f"metadata should still show polish did not pass: {final_polish}")
    assert_true(decision.get("visible_reply_owner") == "brain", f"visible owner must remain Brain: {decision}")


def check_runtime_dual_backend_pools_keep_planner_moving_while_polish_runs() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "scheduler_state.json"
        store = SchedulerStateStore(tenant_id="unit", path=path)
        messages_by_target = {
            "客户A": [message("A", 1)],
            "客户B": [message("B", 1)],
        }

        def capture_fn(session: dict[str, Any]) -> dict[str, Any]:
            target = str(session.get("target_name") or "")
            return {"messages": messages_by_target[target], "batch": messages_by_target[target]}

        def planner(capture: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.02)
            return {"ok": True, "reply_text": f"草稿 {capture.get('target_name')}", "decision": {"rule_name": "unit"}}

        def polish(planner_task: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
            target = str(planner_task.get("target_name") or "")
            if target == "客户A":
                time.sleep(0.22)
            else:
                time.sleep(0.02)
            result = planner_task.get("result") if isinstance(planner_task.get("result"), dict) else {}
            reply_text = str(result.get("reply_text") or "")
            return {"ok": True, "reply_text": f"{reply_text} 已润色", "decision": {"rule_name": "unit", "polished": True}}

        runtime = CustomerServiceSchedulerRuntime(
            store=store,
            config=SchedulerConfig(
                enabled=True,
                capture_max_sessions_per_round=1,
                llm_max_concurrency=1,
                planner_max_concurrency=1,
                polish_max_concurrency=1,
                send_max_replies_per_round=1,
            ),
            capture_fn=capture_fn,
            plan_reply_fn=planner,
            polish_reply_fn=polish,
        )
        try:
            runtime.tick(
                session_signals=[{"name": "客户A", "content": "A新消息", "time": "10:00", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:00:00",
            )
            time.sleep(0.03)
            second = runtime.tick(
                session_signals=[{"name": "客户B", "content": "B新消息", "time": "10:01", "unread_detected": True, "unread_badge": "visual_red_dot"}],
                allow_send=False,
                now="2026-06-02T18:00:01",
            )
            assert_equal(second["summary"]["planner_running"], 1, "planner pool should still accept a new session while polish is active")
            assert_equal(second["summary"]["polish_running"], 1, "polish pool should run independently from planner pool")
            time.sleep(0.03)
            third = runtime.tick(allow_send=False, now="2026-06-02T18:00:02")
            assert_equal(third["summary"]["planner_running"], 0, "fast planner should finish without being blocked by slow polish")
            assert_true(third["summary"]["polish_running"] >= 1, "slow polish should still be running for the older session")
            assert_true(third["summary"]["polish_queued"] >= 1, "second session polish should queue while the first polish is still busy")
            time.sleep(0.25)
            final = runtime.tick(allow_send=False, now="2026-06-02T18:00:03")
            assert_true(final["summary"]["reply_ready"] >= 1, "completed polish should produce ready replies")
        finally:
            runtime.shutdown()


def check_listener_scheduler_config_gate() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps({"targets": []}, ensure_ascii=False), encoding="utf-8")
        assert_true(
            not load_concurrency_scheduler_enabled(path),
            "scheduler should stay off when neither explicit enable nor live low-risk guard exists",
        )
        path.write_text(json.dumps({"concurrency_scheduler": {"enabled": True}}, ensure_ascii=False), encoding="utf-8")
        assert_true(load_concurrency_scheduler_enabled(path), "scheduler should enable only on explicit true")
        live_low_risk = {
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "multi_target": {"enabled": True, "rpa_low_risk_mode": True},
            "live_safety_guard": {
                "enabled": True,
                "allowed_targets": ["客户A"],
                "require_recent_bootstrap": False,
            },
        }
        path.write_text(json.dumps(live_low_risk, ensure_ascii=False), encoding="utf-8")
        assert_true(
            load_concurrency_scheduler_enabled(path),
            "live low-risk RPA guard should infer scheduler enable when not explicitly disabled",
        )
        live_low_risk["concurrency_scheduler"] = {"enabled": False}
        path.write_text(json.dumps(live_low_risk, ensure_ascii=False), encoding="utf-8")
        assert_true(
            not load_concurrency_scheduler_enabled(path),
            "explicit scheduler false should remain the rollback switch",
        )


def check_listener_poll_interval_uses_randomized_window_config() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "poll": {
                        "interval_seconds": 3,
                        "interval_min_seconds": 3,
                        "interval_max_seconds": 5,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = load_managed_poll_interval_settings(path, fallback_seconds=5)
    assert_equal(settings.get("min_seconds"), 3.0, "listener should respect randomized poll minimum")
    assert_equal(settings.get("max_seconds"), 5.0, "listener should respect randomized poll maximum")


def check_live_safety_applies_backend_scheduler_defaults() -> None:
    raw = {
        "targets": [{"name": "客户A", "enabled": True, "exact": True}],
        "multi_target": {"enabled": True, "rpa_low_risk_mode": True},
        "live_safety_guard": {
            "enabled": True,
            "allowed_targets": ["客户A"],
            "require_recent_bootstrap": False,
        },
    }
    merged = apply_customer_service_live_safety_guard(raw, settings={})
    scheduler = merged.get("concurrency_scheduler") if isinstance(merged.get("concurrency_scheduler"), dict) else {}
    multi_target = merged.get("multi_target") if isinstance(merged.get("multi_target"), dict) else {}
    assert_true(scheduler.get("enabled") is True, "live safety should turn on backend scheduler defaults")
    assert_equal(scheduler.get("llm_max_concurrency"), 4, "scheduler should use high-concurrency LLM default")
    assert_equal(scheduler.get("planner_max_concurrency"), 4, "planner concurrency should default to the high-concurrency standard value")
    assert_equal(scheduler.get("polish_max_concurrency"), 4, "polish concurrency should default to the high-concurrency standard value")
    assert_true(multi_target.get("initial_preview_can_raise_unread") is False, "live safety should baseline first-seen previews")
    assert_true(multi_target.get("preview_change_can_raise_unread") is False, "live safety should ignore ordinary preview drift")
    assert_true(multi_target.get("short_preview_can_raise_unread") is True, "live safety should keep short-message fallback")
    assert_true(multi_target.get("require_unread_badge_for_dispatch") is True, "live safety should require a visual unread badge")
    assert_true(multi_target.get("require_preview_signal_with_unread_badge") is True, "live safety should require badge plus preview/time signal")
    raw["concurrency_scheduler"] = {"enabled": False}
    rollback = apply_customer_service_live_safety_guard(raw, settings={})
    rollback_scheduler = rollback.get("concurrency_scheduler") if isinstance(rollback.get("concurrency_scheduler"), dict) else {}
    assert_true(rollback_scheduler.get("enabled") is False, "explicit scheduler false should survive live safety normalization")


def check_live_safety_file_transfer_defaults_to_self_test_target() -> None:
    raw = {
        "targets": [
            {"name": "文件传输助手", "enabled": True, "exact": True},
            {"name": "客户A", "enabled": True, "exact": True, "allow_self_for_test": True},
        ],
        "multi_target": {"enabled": True, "rpa_low_risk_mode": True},
        "live_safety_guard": {
            "enabled": True,
            "allowed_targets": ["文件传输助手", "客户A"],
            "require_recent_bootstrap": False,
        },
    }
    merged = apply_customer_service_live_safety_guard(raw, settings={})
    targets = {str(item.get("name") or ""): item for item in merged.get("targets", []) if isinstance(item, dict)}
    assert_true(targets.get("文件传输助手", {}).get("allow_self_for_test") is True, "File Transfer Assistant should be explicit self-test target")
    assert_true(int(targets.get("文件传输助手", {}).get("max_batch_messages") or 0) >= 8, "File Transfer Assistant self-test should accept multi-message batches")
    assert_true(targets.get("客户A", {}).get("allow_self_for_test") is False, "normal customer targets must never allow self messages")


def check_listener_rpa_send_rate_zero_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "listener_config.json"
        path.write_text(
            json.dumps(
                {
                    "targets": [{"name": "客户A", "enabled": True, "exact": True}],
                    "rpa_humanized_send": {
                        "enabled": True,
                        "send_rate_min_interval_seconds": 0,
                        "send_rate_burst_window_seconds": 600,
                        "send_rate_burst_limit": 20,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        settings = load_rpa_humanized_send_settings(path)
        min_interval = settings.get("send_rate_min_interval_seconds")
        burst_limit = settings.get("send_rate_burst_limit")
        assert_equal(
            int(min_interval if min_interval not in (None, "") else -1),
            0,
            "explicit 0 min-interval must not fallback to non-zero default",
        )
        assert_equal(
            int(burst_limit if burst_limit not in (None, "") else -1),
            20,
            "burst limit should preserve configured value",
        )


def check_managed_bridge_applies_rpa_fast_send_confirmation_env() -> None:
    keys = [
        "WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION",
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM",
        "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS",
        "WECHAT_WIN32_OCR_SEND_TRIGGER_MODE",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS",
        "WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    bridge: ManagedListenerSchedulerBridge | None = None
    try:
        for key in keys:
            os.environ.pop(key, None)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "listener_config.json"
            config = {
                "state_path": str(root / "workflow_state.json"),
                "audit_log_path": str(root / "audit.jsonl"),
                "targets": [{"name": "customer_a", "enabled": True, "exact": True}],
                "rpa_humanized_send": {
                    "enabled": True,
                    "fast_send_confirmation_enabled": True,
                    "input_fast_visual_confirm_enabled": True,
                    "send_input_confirm_attempts": 1,
                    "send_trigger_mode": "enter_only",
                    "send_trigger_delay_min_ms": 520,
                    "send_trigger_delay_max_ms": 1500,
                    "send_after_trigger_delay_min_ms": 260,
                    "send_after_trigger_delay_max_ms": 820,
                },
                "concurrency_scheduler": {"enabled": True},
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            bridge = ManagedListenerSchedulerBridge(
                tenant_id="unit_bridge_env",
                config_path=config_path,
                allow_send=False,
                write_data=False,
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION"),
                "1",
                "bridge reload should enable fast send confirmation for in-process sends",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"),
                "1",
                "bridge reload should enable fast visual input confirmation for in-process sends",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"),
                "1",
                "bridge reload should apply send-input confirmation attempts",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_SEND_TRIGGER_MODE"),
                "enter_only",
                "bridge reload should apply send trigger mode",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MIN_MS"),
                "520",
                "bridge reload should apply pre-trigger send delay min",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_HUMANIZED_SEND_TRIGGER_DELAY_MAX_MS"),
                "1500",
                "bridge reload should apply pre-trigger send delay max",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MIN_MS"),
                "260",
                "bridge reload should apply post-trigger send delay min",
            )
            assert_equal(
                os.environ.get("WECHAT_WIN32_OCR_HUMANIZED_SEND_AFTER_TRIGGER_DELAY_MAX_MS"),
                "820",
                "bridge reload should apply post-trigger send delay max",
            )
    finally:
        if bridge is not None:
            bridge.shutdown()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_managed_bridge_capture_send_marks_workflow_state() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        tenant_id = "unit_bridge"
        settings_path = enable_brain_first_test_settings(tenant_id)
        config_path = root / "listener_config.json"
        state_path = root / "workflow_state.json"
        audit_path = root / "audit.jsonl"
        config = {
            "state_path": str(state_path),
            "audit_log_path": str(audit_path),
            "targets": [
                {
                    "name": "customer_a",
                    "enabled": True,
                    "exact": True,
                    "allow_self_for_test": False,
                    "max_batch_messages": 3,
                }
            ],
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "concurrency_scheduler": {
                "enabled": True,
                "capture_max_sessions_per_round": 1,
                "llm_max_concurrency": 1,
                "send_max_replies_per_round": 1,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id=tenant_id,
            config_path=config_path,
            allow_send=True,
            write_data=False,
        )
        fake = FakeBridgeConnector()
        bridge.connector = fake
        bridge.store = SchedulerStateStore(tenant_id=tenant_id, path=root / "scheduler_state.json")
        if bridge.runtime is not None:
            bridge.runtime.shutdown()
        bridge.runtime = CustomerServiceSchedulerRuntime(
            store=bridge.store,
            config=bridge.scheduler_config,
            capture_fn=bridge._capture_session,
            plan_reply_fn=lambda capture, task: {"ok": True, "reply_text": "可以谈，您方便说下预算吗？", "decision": {"rule_name": "unit"}},
            freshness_fn=lambda reply: {"ok": True, "stale": False},
            send_fn=bridge._send_reply,
            capture_done_fn=bridge._capture_done,
        )
        try:
            bridge.runtime.tick(session_signals=[{"name": "customer_a", "unread_detected": True}], allow_send=True, now="2026-05-25T10:00:00")
            time.sleep(0.03)
            bridge.runtime.tick(allow_send=True, now="2026-05-25T10:00:01")
        finally:
            bridge.shutdown()
            settings_path.unlink(missing_ok=True)
        assert_equal(len(fake.sent), 1, "bridge sender should send exactly one verified reply")
        workflow_state = json.loads(state_path.read_text(encoding="utf-8"))
        target_state = target_state_by_name(workflow_state, "customer_a")
        assert_true("bridge-a-1" in target_state.get("processed_message_ids", []), "send success must mark original workflow state processed")
        assert_true(audit_path.exists(), "send success should append scheduler audit event")


def check_managed_bridge_freshness_preview_fast_pass_without_strict_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_fastpass",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [{"name": "客户A", "unread_detected": False, "last_detected_at": "", "pending_since": "", "last_message_time": "10:00"}]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-fastpass",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-preview-fastpass",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "你好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"preview fast pass should return ok result: {result}")
        assert_true(result.get("stale") is False, f"preview fast pass should not stale clean session: {result}")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "session_preview_fastpath",
            "freshness mode should expose preview fastpath",
        )
        assert_equal(detect_calls["count"], 0, "preview fast pass should skip strict detect scanner")


def check_managed_bridge_freshness_preview_unread_uses_strict_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_unread",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [{"name": "客户A", "unread_detected": True, "last_detected_at": "2026-05-25T10:00:03", "pending_since": "2026-05-25T10:00:03"}]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": True, "gap_risk": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-unread",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-preview-unread",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "你好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"preview unread should still produce ok freshness payload: {result}")
        assert_true(result.get("stale") is True, f"preview unread with strict detect newer result must stale the in-flight reply: {result}")
        assert_true(result.get("has_newer_messages") is True, f"preview unread strict scan should mark newer messages: {result}")
        assert_equal(str(result.get("freshness_mode") or ""), "strict_message_scan", "preview unread should fall back to strict freshness scan")
        assert_equal(detect_calls["count"], 1, "preview unread should trigger exactly one strict detect scan")


def check_managed_bridge_freshness_same_short_signal_fast_passes_without_strict_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_same_short_signal",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [
                {
                    "name": "客户A",
                    "unread_detected": True,
                    "last_detected_at": "2026-05-25T10:00:03",
                    "pending_since": "2026-05-25T10:00:03",
                    "pending_signal_text": "在吗？",
                    "pending_signal_kind": "high_sensitivity_short",
                }
            ]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False, "gap_risk": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-same-short",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-preview-same-short",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "在吗？"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"same short signal fast pass should still return freshness payload: {result}")
        assert_true(result.get("stale") is False, f"same short signal without newer content must not stale reply: {result}")
        assert_equal(str(result.get("freshness_mode") or ""), "session_preview_fastpath", "same short signal should use preview fastpath")
        assert_equal(
            str(result.get("reason") or ""),
            "session_monitor_unread_matches_capture_fast_pass",
            f"same short preview should be recognized as the captured message: {result}",
        )
        assert_equal(detect_calls["count"], 0, "same short signal should not force a fragile strict OCR scan")


def check_managed_bridge_freshness_uses_session_key_for_duplicate_display_names() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "许聪", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_duplicate_name_preview_key",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [
                {
                    "name": "许聪",
                    "session_key": "wx:rpa:v1:session-a",
                    "unread_detected": False,
                    "last_detected_at": "",
                    "pending_since": "",
                    "pending_signal_text": "",
                    "last_message_time": "2026-06-11T20:00:01",
                },
                {
                    "name": "许聪",
                    "session_key": "wx:rpa:v1:session-b",
                    "unread_detected": True,
                    "last_detected_at": "2026-06-11T20:00:02",
                    "pending_since": "2026-06-11T20:00:02",
                    "pending_signal_text": "在不",
                    "pending_signal_kind": "high_sensitivity_short",
                },
            ]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": True, "gap_risk": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-duplicate-name-session-a",
            "target_name": "许聪",
            "session_key": "wx:rpa:v1:session-a",
            "_capture": {
                "capture_id": "capture-duplicate-name-session-a",
                "target_name": "许聪",
                "session_key": "wx:rpa:v1:session-a",
                "exact": True,
                "batch": [{"id": "msg-a-1", "sender": "customer", "content": "晚上好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"session-key scoped preview should return ok freshness payload: {result}")
        assert_true(
            result.get("stale") is False,
            f"unread signal from another same-display-name session must not stale this reply: {result}",
        )
        assert_equal(detect_calls["count"], 0, "same-name different session_key should not force strict scan for this reply")


def check_managed_bridge_pending_capture_same_signal_does_not_stale_ready_reply() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_pending_capture_same_signal",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
        }
        bridge.session_monitor = FakePreviewSessionMonitor(
            [
                {
                    "name": "客户A",
                    "unread_detected": True,
                    "last_detected_at": "2026-05-25T10:00:04",
                    "pending_since": "2026-05-25T10:00:04",
                    "pending_signal_text": "在吗？",
                    "pending_signal_kind": "high_sensitivity_short",
                }
            ]
        )

        def seed_state(state: dict[str, Any]) -> None:
            session = scheduler_state_module.ensure_session(
                state,
                "客户A",
                exact=True,
                conversation_type="private",
                now="2026-05-25T10:00:04",
            )
            session["pending_capture"] = True
            session["status"] = "capture_pending"
            session["pending_message_count"] = 1

        state = bridge.store.load()
        seed_state(state)
        bridge.store.save(state)
        reply = {
            "reply_id": "reply-pending-same-short",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-pending-same-short",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "在吗？"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"pending same signal fast pass should return ok payload: {result}")
        assert_true(result.get("stale") is False, f"same pending signal must not stale the ready reply: {result}")
        assert_equal(
            str(result.get("reason") or ""),
            "scheduler_pending_capture_matches_capture_fast_pass",
            f"pending same signal should be recognized before stale guard: {result}",
        )


def check_managed_bridge_freshness_strict_interval_fallback() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 120,
                "strict_check_after_llm_seconds": 0,
                "strict_check_on_first_send": True,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_strict_fallback",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 120,
            "strict_check_after_llm_seconds": 0,
            "strict_check_on_first_send": True,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [{"name": "客户A", "unread_detected": False, "last_detected_at": "", "pending_since": ""}]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False, "reason": "unit_strict_scan"}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-strict",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-preview-strict",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "你好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"strict fallback should return freshness payload: {result}")
        assert_equal(detect_calls["count"], 1, "strict interval should trigger strict detect scan")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "strict_message_scan",
            "strict scan fallback should label strict freshness mode",
        )


def check_managed_bridge_soft_passes_unconfirmed_short_ocr_strict_freshness() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 120,
                "strict_check_after_llm_seconds": 0,
                "strict_check_on_first_send": True,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_strict_unconfirmed_short_ocr",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = dict(config["scheduler_freshness"])
        bridge.session_monitor = FakePreviewSessionMonitor(
            [{"name": "客户A", "unread_detected": False, "last_detected_at": "", "pending_since": "", "last_message_time": "10:00"}]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "has_newer_messages": True,
                "gap_risk": False,
                "reason": "original_batch_not_visible_assume_stale",
                "newer_messages": [{"id": "ocr-noise-1", "type": "text", "sender": "unknown", "content": "要"}],
            }

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-unconfirmed-short-ocr",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-unconfirmed-short-ocr",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "晚上好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"unconfirmed strict OCR observation should still return ok: {result}")
        assert_true(result.get("stale") is False, f"unconfirmed short OCR fragment must not stale ready reply: {result}")
        assert_equal(
            str(result.get("reason") or ""),
            "strict_freshness_unconfirmed_ocr_observation",
            f"strict short OCR noise should be recorded as unconfirmed observation: {result}",
        )
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "strict_message_scan_ledger_guard",
            "ledger guard should label the guarded strict freshness path",
        )


def check_managed_bridge_freshness_long_llm_uses_task_runtime_not_queue_age() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "客户A", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 40,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_llm_elapsed",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 40,
        }
        detect_calls = {"count": 0}
        bridge.session_monitor = FakePreviewSessionMonitor(
            [{"name": "客户A", "unread_detected": False, "last_detected_at": "", "pending_since": "", "last_message_time": "10:00"}]
        )

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False, "reason": "unit_strict_scan"}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        state = bridge.store.load()
        state.setdefault("llm_tasks", {})["task-llm-duration"] = {
            "task_id": "task-llm-duration",
            "target_name": "客户A",
            "status": "completed",
            "started_at": "2026-05-25T10:00:00",
            "finished_at": "2026-05-25T10:00:05",
        }
        bridge.store.save(state)
        reply = {
            "reply_id": "reply-preview-llm-elapsed",
            "task_id": "task-llm-duration",
            "target_name": "客户A",
            "_capture": {
                "capture_id": "capture-preview-llm-elapsed",
                "target_name": "客户A",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "你好"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"short true LLM runtime should not block fast path: {result}")
        assert_true(result.get("stale") is False, f"short true LLM runtime should not stale reply: {result}")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "session_preview_fastpath",
            "long-queue age should not force strict scan when real LLM runtime is short",
        )
        assert_equal(detect_calls["count"], 0, "real short LLM runtime should skip strict scanner")


def check_managed_bridge_freshness_session_list_preview_fast_pass_without_monitor() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "customer_a", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
                "preview_from_session_list_enabled": True,
                "preview_from_session_list_require_content_match": True,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_session_list_fastpass",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.session_monitor = None
        bridge.connector = FakeBridgeConnector()
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
            "preview_from_session_list_enabled": True,
            "preview_from_session_list_require_content_match": True,
        }
        detect_calls = {"count": 0}

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-session-list-fastpass",
            "target_name": "customer_a",
            "_capture": {
                "capture_id": "capture-preview-session-list-fastpass",
                "target_name": "customer_a",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "这台车还能优惠吗"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"session list preview fast pass should return ok result: {result}")
        assert_true(result.get("stale") is False, f"session list preview fast pass should not stale clean session: {result}")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "session_preview_fastpath",
            "session list preview should still expose session preview fastpath mode",
        )
        assert_equal(detect_calls["count"], 0, "session list preview fast pass should skip strict detect scanner")


def check_managed_bridge_freshness_session_list_mismatch_falls_back_to_strict_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "customer_a", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
                "preview_from_session_list_enabled": True,
                "preview_from_session_list_require_content_match": True,
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_session_list_mismatch",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.session_monitor = None
        bridge.connector = FakeBridgeConnector()
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
            "preview_from_session_list_enabled": True,
            "preview_from_session_list_require_content_match": True,
        }
        detect_calls = {"count": 0}

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False, "reason": "unit_strict_scan_after_mismatch"}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-session-list-mismatch",
            "target_name": "customer_a",
            "_capture": {
                "capture_id": "capture-preview-session-list-mismatch",
                "target_name": "customer_a",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "完全不同的问题"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"strict fallback should return freshness payload: {result}")
        assert_equal(detect_calls["count"], 1, "session list content mismatch should trigger strict detect scan")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "strict_message_scan",
            "session list mismatch should fall back to strict freshness mode",
        )


def check_managed_bridge_freshness_session_list_mismatch_soft_pass_by_default() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "customer_a", "enabled": True, "exact": True}],
            "history_backfill": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "preview_first",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
                "preview_from_session_list_enabled": True,
                # Intentionally omit preview_from_session_list_require_content_match
                # to validate the default soft-pass behavior.
            },
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_preview_session_list_softpass_default",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        bridge.session_monitor = None
        bridge.connector = FakeBridgeConnector()
        bridge.config["scheduler_freshness"] = {
            "enabled": True,
            "mode": "preview_first",
            "strict_check_interval_seconds": 0,
            "strict_check_after_llm_seconds": 0,
            "preview_from_session_list_enabled": True,
        }
        detect_calls = {"count": 0}

        def detect_stub(**_kwargs: Any) -> dict[str, Any]:
            detect_calls["count"] += 1
            return {"ok": True, "has_newer_messages": False}

        bridge._workflow["detect_newer_messages_before_send"] = detect_stub
        reply = {
            "reply_id": "reply-preview-session-list-softpass-default",
            "target_name": "customer_a",
            "_capture": {
                "capture_id": "capture-preview-session-list-softpass-default",
                "target_name": "customer_a",
                "exact": True,
                "batch": [{"id": "msg-1", "sender": "customer", "content": "和预览文本明显不一致的问题"}],
            },
        }
        try:
            result = bridge._freshness_check(reply)
        finally:
            bridge.shutdown()
        assert_true(result.get("ok") is True, f"default session-list mismatch should still return freshness payload: {result}")
        assert_true(result.get("stale") is False, f"default session-list mismatch should not stale clean session: {result}")
        assert_equal(
            str(result.get("freshness_mode") or ""),
            "session_preview_fastpath",
            "default session-list mismatch should stay on fastpath for lower tail latency",
        )
        assert_equal(detect_calls["count"], 0, "default session-list mismatch should skip strict detect scanner")


def check_managed_bridge_soft_passes_unconfirmed_original_batch_not_visible_stale() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [{"name": "新数据测试", "enabled": True, "exact": True}],
            "multi_target": {"enabled": True, "rpa_low_risk_mode": True},
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "scheduler_freshness": {
                "enabled": True,
                "mode": "strict_only",
                "strict_check_interval_seconds": 0,
                "strict_check_after_llm_seconds": 0,
            },
            "concurrency_scheduler": {"enabled": True},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_freshness_original_batch_not_visible",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        try:
            bridge.connector = FakeBridgeConnector()
            bridge.connector.messages = {
                "新数据测试": [
                    {"id": "newer-noisy", "type": "text", "sender": "unknown", "content": "OCR误读片段", "time": "20:03"}
                ]
            }
            bridge.session_monitor = FakePreviewSessionMonitor(
                [
                    {
                        "name": "新数据测试",
                        "session_key": "wx:rpa:v1:new-data",
                        "unread_detected": False,
                        "last_detected_at": "",
                        "pending_since": "",
                        "last_message_time": "20:02",
                        "pending_signal_text": "",
                    }
                ]
            )
            bridge.config["scheduler_freshness"] = dict(config["scheduler_freshness"])
            state = bridge.store.empty_state()
            capture = record_capture_result(
                state,
                "新数据测试",
                messages=[{"id": "old-1", "type": "text", "sender": "customer", "content": "我咋不记得了", "time": "20:02"}],
                batch=[{"id": "old-1", "type": "text", "sender": "customer", "content": "我咋不记得了", "time": "20:02"}],
                conversation_type="group",
                session_key="wx:rpa:v1:new-data",
                now="2026-06-10T20:02:00",
            )
            task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-10T20:02:01")
            reply = complete_llm_task(
                state,
                task["task_id"],
                reply_text="在的，您继续说。",
                decision={},
                now="2026-06-10T20:02:10",
            )["reply"]
            bridge.store.save(state)
            result = bridge._freshness_check(reply)
            assert_true(not bool(result.get("stale")), f"unconfirmed original-batch-not-visible should soft-pass: {result}")
            assert_equal(
                result.get("reason"),
                "strict_freshness_unconfirmed_ocr_observation",
                "freshness should be soft-passed rather than stale without unread/ledger corroboration",
            )
        finally:
            bridge.shutdown()


def check_managed_bridge_collect_signals_skips_busy_sticky_target() -> None:
    class FakeDispatchMonitor:
        def poll(self, connector: Any) -> list[Any]:
            return []

        def select_dispatch_targets(self, *, limit: int | None = None) -> list[Any]:
            return [SimpleNamespace(name="客户A", exact=True, unread_detected=True, conversation_type="private")]

        def pending_targets(self, *, limit: int | None = None) -> list[Any]:
            targets = [
                SimpleNamespace(name="客户A", exact=True, unread_detected=True, conversation_type="private"),
                SimpleNamespace(name="客户B", exact=True, unread_detected=True, conversation_type="private"),
            ]
            if limit is None:
                return targets
            return targets[: max(0, int(limit))]

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [
                {"name": "客户A", "enabled": True, "exact": True},
                {"name": "客户B", "enabled": True, "exact": True},
            ],
            "multi_target": {"enabled": True},
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "concurrency_scheduler": {"enabled": True, "capture_max_sessions_per_round": 1},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_signal_bias",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        try:
            bridge.session_monitor = FakeDispatchMonitor()
            bridge.ignored_session_names = set()
            state = empty_state()
            record_session_signal(
                state,
                {"name": "客户A", "unread_detected": True, "conversation_type": "private"},
                now="2026-06-01T10:00:00",
            )
            capture = record_capture_result(
                state,
                "客户A",
                messages=[message("客户A", 1)],
                batch=[message("客户A", 1)],
                overflow_messages=[],
                history_backfill={},
                exact=True,
                conversation_type="private",
                now="2026-06-01T10:00:00",
            )
            task = enqueue_llm_task(state, str(capture.get("capture_id") or ""), now="2026-06-01T10:00:01")
            mark_llm_started(state, str(task.get("task_id") or ""), now="2026-06-01T10:00:02")
            bridge.store.save(state)
            signals = bridge._collect_session_signals()
            names = [str(item.get("name") or "") for item in signals]
            assert_true(names[:1] == ["客户B"], f"busy sticky target should yield to other unread target: {names}")
        finally:
            bridge.shutdown()


def check_managed_bridge_collect_signals_does_not_dispatch_when_all_pending_busy() -> None:
    class FakeAllBusyMonitor:
        def poll(self, connector: Any) -> list[Any]:
            return []

        def select_dispatch_targets(self, *, limit: int | None = None) -> list[Any]:
            return [
                SimpleNamespace(name="人薩A", exact=True, unread_detected=True, conversation_type="private"),
                SimpleNamespace(name="人薩B", exact=True, unread_detected=True, conversation_type="private"),
            ]

        def pending_targets(self, *, limit: int | None = None) -> list[Any]:
            targets = [
                SimpleNamespace(name="人薩A", exact=True, unread_detected=True, conversation_type="private"),
                SimpleNamespace(name="人薩B", exact=True, unread_detected=True, conversation_type="private"),
            ]
            if limit is None:
                return targets
            return targets[: max(0, int(limit))]

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [
                {"name": "人薩A", "enabled": True, "exact": True},
                {"name": "人薩B", "enabled": True, "exact": True},
            ],
            "multi_target": {"enabled": True},
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "concurrency_scheduler": {"enabled": True, "capture_max_sessions_per_round": 2},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_all_busy_signal_gate",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        try:
            bridge.session_monitor = FakeAllBusyMonitor()
            bridge.ignored_session_names = set()
            state = empty_state()
            for name in ["人薩A", "人薩B"]:
                record_session_signal(
                    state,
                    {"name": name, "unread_detected": True, "conversation_type": "private"},
                    now="2026-06-01T10:00:00",
                )
                capture = record_capture_result(
                    state,
                    name,
                    messages=[message(name, 1)],
                    batch=[message(name, 1)],
                    overflow_messages=[],
                    history_backfill={},
                    exact=True,
                    conversation_type="private",
                    now="2026-06-01T10:00:00",
                )
                task = enqueue_llm_task(state, str(capture.get("capture_id") or ""), now="2026-06-01T10:00:01")
                mark_llm_started(state, str(task.get("task_id") or ""), now="2026-06-01T10:00:02")
            bridge.store.save(state)
            signals = bridge._collect_session_signals()
            assert_equal(signals, [], f"all-busy pending sessions should not drive foreground RPA hopping: {signals}")
        finally:
            bridge.shutdown()


def check_managed_bridge_capture_applies_humanized_switch_delay() -> None:
    class FakeConnector:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_messages(self, name: str, *, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {"ok": True, "messages": []}

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [
                {"name": "客户A", "enabled": True, "exact": True},
                {"name": "客户B", "enabled": True, "exact": True},
            ],
            "multi_target": {
                "enabled": True,
                "switch_human_delay_enabled": True,
                "switch_human_delay_min_seconds": 1.25,
                "switch_human_delay_max_seconds": 1.25,
                "max_targets_per_iteration": 2,
                "capture_one_target_per_round": False,
            },
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "concurrency_scheduler": {"enabled": True, "capture_max_sessions_per_round": 3},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_switch_delay",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        original_sleep = scheduler_module.time.sleep
        original_uniform = scheduler_module.random.uniform
        sleeps: list[float] = []
        fake_connector = FakeConnector()
        try:
            bridge.connector = fake_connector
            scheduler_module.random.uniform = lambda _low, _high: 1.25
            scheduler_module.time.sleep = lambda seconds: sleeps.append(round(float(seconds), 3))
            first = bridge._capture_session({"target_name": "客户A", "exact": True, "conversation_type": "private"})
            second = bridge._capture_session({"target_name": "客户B", "exact": True, "conversation_type": "private"})
            assert_true(first.get("ok") is True and second.get("ok") is True, f"captures should pass: {first}, {second}")
            assert_equal(fake_connector.calls, ["客户A", "客户B"], "capture should call connector in target order")
            assert_true(any(abs(delay - 1.25) < 0.001 for delay in sleeps), f"switch delay should be applied before actual capture: {sleeps}")
            assert_equal(
                int(bridge.scheduler_config.capture_max_sessions_per_round),
                3,
                "two-target unread mode should not clamp scheduler capture width back to 1",
            )
        finally:
            scheduler_module.time.sleep = original_sleep
            scheduler_module.random.uniform = original_uniform
            bridge.shutdown()


def check_session_monitor_event_driven_can_batch_two_unread_targets_without_whitelist_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        monitor = SessionMonitor(
            tenant_id="unit_monitor_two_unread",
            state_path=root / "session_monitor.json",
            whitelist={"客户A", "客户B", "客户C"},
            max_targets_per_iteration=2,
            min_switch_interval_seconds=1,
            dispatch_strategy="event_driven",
        )
        now = "2026-06-03T03:10:00"
        monitor._sessions["客户A"] = SessionState(name="客户A", first_seen_at=now, last_seen_at=now, conversation_type="private")
        monitor._sessions["客户B"] = SessionState(name="客户B", first_seen_at=now, last_seen_at=now, conversation_type="private")
        monitor._sessions["客户C"] = SessionState(name="客户C", first_seen_at=now, last_seen_at=now, conversation_type="private")
        monitor._mark_pending_signal(
            monitor._sessions["客户A"],
            content="预算15万以内",
            now_iso=now,
            priority=50,
        )
        monitor._mark_pending_signal(
            monitor._sessions["客户B"],
            content="想看SUV",
            now_iso=now,
            priority=45,
        )
        selected = monitor.select_dispatch_targets(limit=3)
        assert_equal([item.name for item in selected], ["客户A", "客户B"], "event-driven monitor should batch two already-unread targets")


def check_session_monitor_badge_cleared_preview_change_does_not_dispatch_targets() -> None:
    class FakeConnector:
        def list_sessions(self) -> dict[str, Any]:
            return {
                "ok": True,
                "sessions": [
                    {
                        "name": "客户A",
                        "content": "晚上好，我想看辆家用车",
                        "time": "20:01",
                        "unread_badge": "",
                        "conversation_type": "private",
                    },
                    {
                        "name": "客户B",
                        "content": "预算十万左右，有推荐吗",
                        "time": "20:01",
                        "unread_badge": "",
                        "conversation_type": "private",
                    },
                ],
            }

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        monitor = SessionMonitor(
            tenant_id="unit_monitor_badge_cleared_preview",
            state_path=root / "session_monitor.json",
            whitelist={"客户A", "客户B"},
            max_targets_per_iteration=2,
            min_switch_interval_seconds=1,
            dispatch_strategy="event_driven",
            initial_preview_can_raise_unread=False,
            preview_change_can_raise_unread=False,
            short_preview_can_raise_unread=True,
            require_unread_badge_for_dispatch=True,
            require_preview_signal_with_unread_badge=True,
        )
        now = "2026-06-10T20:00:00"
        monitor._sessions["客户A"] = SessionState(
            name="客户A",
            first_seen_at=now,
            last_seen_at=now,
            last_content_digest="old-a",
            last_message_time="19:58",
            last_dispatched_at="2026-06-10T19:58:10",
            conversation_type="private",
        )
        monitor._sessions["客户B"] = SessionState(
            name="客户B",
            first_seen_at=now,
            last_seen_at=now,
            last_content_digest="old-b",
            last_message_time="19:58",
            last_dispatched_at="2026-06-10T19:58:11",
            conversation_type="private",
        )

        active = monitor.poll(FakeConnector())
        assert_equal(active, [], "badge-cleared preview changes must not drive foreground target switching")
        selected = monitor.select_dispatch_targets(limit=2)
        assert_equal(selected, [], "badge-cleared preview changes must remain passive until ledger/unread evidence exists")


def check_scheduler_cleanup_merges_legacy_name_bucket_into_session_key_bucket() -> None:
    state = empty_state()
    state["sessions"] = {
        "文件传输助手": {
            "target_name": "文件传输助手",
            "display_name": "文件传输助手",
            "pending_capture": True,
            "pending_since": "2026-06-10T10:00:00",
            "processed_message_ids": ["legacy-id"],
        },
        "wx:rpa:v1:file-transfer": {
            "session_key": "wx:rpa:v1:file-transfer",
            "target_name": "文件传输助手",
            "display_name": "文件传输助手",
            "pending_capture": False,
            "processed_message_ids": ["new-id"],
        },
    }
    result = cleanup_scheduler_state(state)
    assert_equal(result.get("migrated_legacy_sessions"), 1, "cleanup should merge legacy display-name session into canonical session_key bucket")
    assert_true("文件传输助手" not in state["sessions"], "legacy display-name bucket should be removed")
    merged = state["sessions"]["wx:rpa:v1:file-transfer"]
    assert_true(bool(merged.get("pending_capture")), "pending state should survive migration")
    assert_equal(
        set(merged.get("processed_message_ids") or []),
        {"legacy-id", "new-id"},
        "processed anchors should be merged during migration",
    )


def check_session_monitor_blocks_ambiguous_duplicate_display_names() -> None:
    class FakeConnector:
        def list_sessions(self) -> dict[str, Any]:
            return {
                "ok": True,
                "sessions": [
                    {
                        "name": "许聪",
                        "content": "晚上好，我想问奥迪",
                        "time": "20:11",
                        "unread_badge": "1",
                        "conversation_type": "private",
                        "row_fingerprint": {"duplicate_name_index": 0},
                    },
                    {
                        "name": "许聪",
                        "content": "预算十万左右",
                        "time": "20:12",
                        "unread_badge": "1",
                        "conversation_type": "private",
                        "row_fingerprint": {"duplicate_name_index": 1},
                    },
                ],
            }

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        monitor = SessionMonitor(
            tenant_id="unit_monitor_ambiguous_duplicate_display_name",
            state_path=root / "session_monitor.json",
            whitelist={"许聪"},
            max_targets_per_iteration=2,
            min_switch_interval_seconds=1,
            dispatch_strategy="event_driven",
            require_unread_badge_for_dispatch=True,
            require_preview_signal_with_unread_badge=True,
        )

        active = monitor.poll(FakeConnector())
        assert_equal(active, [], "duplicate visible display names must not dispatch through a name-only RPA switch")
        selected = monitor.select_dispatch_targets(limit=2)
        assert_equal(selected, [], "ambiguous duplicate display names must not remain pending for send")
        sessions = {item["name"]: item for item in monitor.all_sessions()}
        assert_equal(
            sessions.get("许聪", {}).get("pending_signal_kind"),
            "ambiguous_duplicate_name",
            "ambiguous duplicate name should be visible as a mechanism-layer block reason",
        )


def check_session_monitor_allows_duplicate_display_names_when_session_keys_are_distinct() -> None:
    class FakeConnector:
        def list_sessions(self) -> dict[str, Any]:
            return {
                "ok": True,
                "sessions": [
                    {
                        "name": "许聪",
                        "session_key": "wx:rpa:v1:same-name-a",
                        "content": "晚上好，我想问奥迪",
                        "time": "20:11",
                        "unread_badge": "1",
                        "conversation_type": "private",
                    },
                    {
                        "name": "许聪",
                        "session_key": "wx:rpa:v1:same-name-b",
                        "content": "预算十万左右",
                        "time": "20:12",
                        "unread_badge": "1",
                        "conversation_type": "private",
                    },
                ],
            }

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        monitor = SessionMonitor(
            tenant_id="unit_monitor_distinct_duplicate_display_name",
            state_path=root / "session_monitor.json",
            whitelist={"许聪"},
            max_targets_per_iteration=2,
            min_switch_interval_seconds=1,
            dispatch_strategy="event_driven",
            require_unread_badge_for_dispatch=True,
            require_preview_signal_with_unread_badge=True,
        )

        active = monitor.poll(FakeConnector())
        assert_equal(
            {item.session_key for item in active},
            {"wx:rpa:v1:same-name-a", "wx:rpa:v1:same-name-b"},
            "duplicate display names with distinct session keys should both dispatch",
        )
        selected = monitor.select_dispatch_targets(limit=2)
        assert_equal(
            {item.session_key for item in selected},
            {"wx:rpa:v1:same-name-a", "wx:rpa:v1:same-name-b"},
            "dispatch selection should key duplicate names by session_key",
        )


def check_session_monitor_reload_keeps_display_name_for_session_key_pending() -> None:
    with tempfile.TemporaryDirectory() as temp:
        state_path = Path(temp) / "session_monitor.json"
        state_path.write_text(
            json.dumps(
                {
                    "tenant_id": "unit",
                    "sessions": {
                        "wx:rpa:v1:xucong-session": {
                            "name": "许聪",
                            "display_name": "许聪",
                            "session_key": "wx:rpa:v1:xucong-session",
                            "last_content_digest": "old",
                            "last_message_time": "10:01",
                            "last_unread_badge": "",
                            "unread_detected": True,
                            "priority_score": 70,
                            "first_seen_at": "2026-06-10T10:00:00",
                            "last_seen_at": "2026-06-10T10:01:00",
                            "pending_since": "2026-06-10T10:01:00",
                            "last_detected_at": "2026-06-10T10:01:00",
                            "conversation_type": "private",
                            "pending_signal_text": "嗨",
                            "pending_signal_kind": "high_sensitivity_short",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monitor = SessionMonitor(
            tenant_id="unit_monitor_reload_session_key_pending",
            state_path=state_path,
            whitelist={"许聪"},
            require_unread_badge_for_dispatch=True,
            require_preview_signal_with_unread_badge=True,
            high_sensitivity_short_merge_window_seconds=0,
        )
        pending = monitor.pending_targets(limit=None)
        assert_equal(len(pending), 1, "reloaded session_key-keyed pending target should pass display-name whitelist")
        assert_equal(pending[0].name, "许聪", "reloaded pending target should preserve display name")
        assert_equal(
            pending[0].session_key,
            "wx:rpa:v1:xucong-session",
            "reloaded pending target should keep session_key identity",
        )
        state = empty_state()
        session = record_session_signal(
            state,
            {
                "name": pending[0].name,
                "session_key": pending[0].session_key,
                "conversation_type": pending[0].conversation_type,
                "unread_detected": True,
                "content": "嗨",
                "time": "10:01",
            },
        )
        assert_true(bool(session and session.get("pending_capture")), "scheduler should enqueue reloaded monitor pending signal")
        assert_equal(session.get("target_name"), "许聪", "scheduler pending target should use display name, not session_key")
        assert_equal(
            session.get("session_key"),
            "wx:rpa:v1:xucong-session",
            "scheduler pending should stay session_key-bound",
        )


def check_session_monitor_poll_repairs_legacy_session_key_only_state() -> None:
    class FakeConnector:
        def list_sessions(self) -> dict[str, Any]:
            return {
                "ok": True,
                "sessions": [
                    {
                        "name": "许聪",
                        "session_key": "wx:rpa:v1:xucong-session",
                        "content": "嗨",
                        "time": "10:22",
                        "unread_badge": "visual_red_dot",
                        "conversation_type": "private",
                    }
                ],
            }

    with tempfile.TemporaryDirectory() as temp:
        state_path = Path(temp) / "session_monitor.json"
        state_path.write_text(
            json.dumps(
                {
                    "tenant_id": "unit",
                    "sessions": {
                        "wx:rpa:v1:xucong-session": {
                            "session_key": "wx:rpa:v1:xucong-session",
                            "last_content_digest": "legacy",
                            "last_message_time": "10:21",
                            "last_unread_badge": "",
                            "unread_detected": True,
                            "priority_score": 70,
                            "first_seen_at": "2026-06-10T10:00:00",
                            "last_seen_at": "2026-06-10T10:21:00",
                            "pending_since": "2026-06-10T10:21:00",
                            "last_detected_at": "2026-06-10T10:21:00",
                            "conversation_type": "private",
                            "pending_signal_text": "嗨",
                            "pending_signal_kind": "high_sensitivity_short",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monitor = SessionMonitor(
            tenant_id="unit_monitor_legacy_session_key_only_state",
            state_path=state_path,
            whitelist={"许聪"},
            require_unread_badge_for_dispatch=True,
            require_preview_signal_with_unread_badge=True,
            high_sensitivity_short_merge_window_seconds=0,
        )
        active = monitor.poll(FakeConnector())
        assert_equal(len(active), 1, "poll should repair legacy session_key-only state into active display-name target")
        assert_equal(active[0].name, "许聪", "repaired active target should use display name")
        pending = monitor.pending_targets(limit=None)
        assert_equal(len(pending), 1, "repaired pending target should pass display-name whitelist")
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved_session = saved.get("sessions", {}).get("wx:rpa:v1:xucong-session", {})
        assert_equal(saved_session.get("name"), "许聪", "poll should persist repaired display name")
        assert_equal(saved_session.get("session_key"), "wx:rpa:v1:xucong-session", "poll should preserve session_key")


def check_scheduler_same_display_name_sessions_are_isolated_by_session_key() -> None:
    state = empty_state()
    record_session_signal(
        state,
        {
            "name": "许聪",
            "session_key": "wx:rpa:v1:same-name-a",
            "conversation_type": "private",
            "content": "晚上好，我想看奥迪",
            "time": "20:11",
            "unread_badge": "1",
            "unread_detected": True,
        },
        now="2026-06-10T20:11:00",
    )
    record_session_signal(
        state,
        {
            "name": "许聪",
            "session_key": "wx:rpa:v1:same-name-b",
            "conversation_type": "private",
            "content": "预算十万左右",
            "time": "20:12",
            "unread_badge": "1",
            "unread_detected": True,
        },
        now="2026-06-10T20:12:00",
    )
    assert_true("许聪" not in state["sessions"], "same display-name sessions with explicit keys must not share a name bucket")
    assert_true("wx:rpa:v1:same-name-a" in state["sessions"], "first same-name session should be keyed by session_key")
    assert_true("wx:rpa:v1:same-name-b" in state["sessions"], "second same-name session should be keyed by session_key")
    selected = select_capture_sessions(state, limit=2)
    assert_equal(
        {item.get("session_key") for item in selected},
        {"wx:rpa:v1:same-name-a", "wx:rpa:v1:same-name-b"},
        "capture selection should not let one same-name session suppress the other",
    )
    mark_capture_started(state, "许聪", session_key="wx:rpa:v1:same-name-a", now="2026-06-10T20:12:01")
    selected_after_a_busy = select_capture_sessions(state, limit=2)
    assert_equal(
        [item.get("session_key") for item in selected_after_a_busy],
        ["wx:rpa:v1:same-name-b"],
        "busy state for one session_key must not block another same-name session",
    )


def check_managed_bridge_normalizes_legacy_switch_interval_to_humanized_window() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "listener_config.json"
        config = {
            "state_path": str(root / "workflow_state.json"),
            "audit_log_path": str(root / "audit.jsonl"),
            "targets": [
                {"name": "客户A", "enabled": True, "exact": True},
                {"name": "客户B", "enabled": True, "exact": True},
            ],
            "multi_target": {
                "enabled": True,
                "dispatch_strategy": "event_driven",
                "min_switch_interval_seconds": 25,
            },
            "history_backfill": {"enabled": False},
            "raw_messages": {"enabled": False},
            "customer_profiles": {"enabled": False},
            "concurrency_scheduler": {"enabled": True, "capture_max_sessions_per_round": 3},
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = ManagedListenerSchedulerBridge(
            tenant_id="unit_switch_interval_normalize",
            config_path=config_path,
            allow_send=False,
            write_data=False,
        )
        try:
            assert_true(bool(bridge._switch_human_delay_enabled), "event-driven multi-target mode should add a humanized 1-3s switch delay")
            assert_equal(int(bridge.session_monitor.min_switch_interval_seconds), 1, "legacy hard switch interval should be normalized to 1s anti-bounce gate")
            assert_equal(float(bridge._switch_human_delay_min_seconds), 1.0, "normalized switch delay min should default to 1s")
            assert_equal(float(bridge._switch_human_delay_max_seconds), 3.0, "normalized switch delay max should default to 3s")
            assert_true(not bool(bridge._capture_one_target_per_round), "normalized event-driven multi-target mode should allow two unread captures per round")
            assert_equal(int(bridge.session_monitor.max_targets_per_iteration), 2, "event-driven low-risk mode should allow two pending unread targets")
            assert_equal(
                int(bridge.scheduler_config.capture_max_sessions_per_round),
                3,
                "scheduler capture width should remain available for the two unread target batch",
            )
        finally:
            bridge.shutdown()


def check_repeatable_short_greeting_is_not_blocked_by_processed_content_keys() -> None:
    target_state = {
        "processed_message_ids": ["old-hello-1"],
        "processed_content_keys": [
            "unknown\x1ftext\x1f在吗",
            "unknown\x1ftext\x1f在",
        ],
        "handoff_message_ids": [],
        "sent_replies": [],
    }
    selection = select_batch_details(
        [
            {
                "id": "new-hello-2",
                "type": "text",
                "sender": "unknown",
                "content": "在吗？",
                "time": "2026-06-02T20:11:00",
            }
        ],
        target_state=target_state,
        allow_self_for_test=False,
        max_batch_messages=8,
        config={},
    )
    assert_equal([item["id"] for item in selection.batch], ["new-hello-2"], "repeatable greeting should remain reply-eligible")


def check_anchor_payload_skips_repeatable_short_greeting_keys() -> None:
    target_state = {
        "processed_message_ids": ["old-1"],
        "processed_content_keys": [
            "unknown\x1ftext\x1f在吗",
            "unknown\x1ftext\x1f在",
            "unknown\x1ftext\x1f预算12万想买个省油车",
        ],
        "last_successful_reply_anchor": {
            "message_ids": ["reply-anchor-1"],
            "message_content_keys": [
                "unknown\x1ftext\x1f在吗",
                "unknown\x1ftext\x1f预算12万想买个省油车",
            ],
        },
    }
    payload = customer_service_anchor_payload(target_state)
    anchor_keys = set(payload.get("anchor_content_keys", []) or [])
    assert_true("unknown\x1ftext\x1f预算12万想买个省油车" in anchor_keys, "business content keys must remain anchors")
    assert_true("unknown\x1ftext\x1f在吗" not in anchor_keys, "repeatable greeting must not become a hard anchor")
    assert_true("content\x1f在吗" not in anchor_keys, "repeatable greeting content-only anchor must be filtered")


def check_scheduler_capture_allows_repeated_short_greeting_after_previous_reply() -> None:
    state = empty_state()
    session = scheduler_state_module.ensure_session(state, "客户A", exact=True, conversation_type="private", now="2026-06-02T20:12:00")
    session["processed_message_ids"] = ["old-greet-id"]
    session["processed_content_keys"] = ["b3e8613c7787e8c5ca7cf467"]
    capture = record_capture_result(
        state,
        "客户A",
        messages=[{"id": "new-greet-id", "type": "text", "sender": "unknown", "content": "在？", "time": "2026-06-02T20:12:01"}],
        batch=[{"id": "new-greet-id", "type": "text", "sender": "unknown", "content": "在？", "time": "2026-06-02T20:12:01"}],
        now="2026-06-02T20:12:01",
    )
    assert_equal(capture.get("status"), "captured", "repeated short greeting should still enter capture queue with a new message id")
    assert_equal(capture.get("message_ids"), ["new-greet-id"], "capture should keep the new greeting message")


def check_repeatable_short_message_identity_uses_occurrence_time() -> None:
    first = {
        "id": "win32_ocr:same-short-layout",
        "type": "text",
        "sender": "许聪",
        "content": "好的，谢谢",
        "time": "2026-06-08T13:12:40",
    }
    second = {
        **first,
        "time": "2026-06-08T13:14:44",
    }
    assert_true(message_identity(first), "repeatable short message should still have an identity")
    assert_true(
        message_identity(first) != message_identity(second),
        f"same OCR id/content but different occurrence time must be distinct: {message_identity(first)} vs {message_identity(second)}",
    )


def check_canonical_pending_signal_allows_repeated_long_message_capture() -> None:
    state = empty_state()
    session_key = "wx:rpa:v1:canonical-long-repeat"
    old_message = {
        "id": "win32_ocr:same-long-layout",
        "source_adapter": "win32_ocr",
        "type": "text",
        "sender": "unknown",
        "content": "这个车多少钱，能不能今天看",
        "bubble_rect": {"left": 420, "top": 260, "right": 760, "bottom": 310},
    }
    first = record_capture_result(
        state,
        "许聪",
        messages=[old_message],
        batch=[old_message],
        conversation_type="private",
        session_key=session_key,
        now="2026-06-12T12:00:00",
    )
    task = enqueue_llm_task(state, str(first.get("capture_id") or ""), now="2026-06-12T12:00:01")
    complete_llm_task(
        state,
        str(task.get("task_id") or ""),
        reply_text="可以，今天能看，我先帮您确认这台车。",
        result_payload={"ok": True, "reply_text": "可以，今天能看，我先帮您确认这台车。"},
        now="2026-06-12T12:00:02",
    )
    reply_id = next(iter(state.get("ready_replies") or {}))
    mark_reply_sent(state, reply_id, send_result={"ok": True, "verified": True}, now="2026-06-12T12:00:03")

    new_messages = scheduler_module.annotate_latest_customer_messages_with_pending_signal(
        [old_message],
        {
            "session_key": session_key,
            "pending_since": "2026-06-12T12:15:03",
            "last_detected_at": "2026-06-12T12:15:04",
            "pending_signal_text": "这个车多少钱，能不能今天看",
            "unread_detected": True,
        },
        target_name="许聪",
        conversation_type="private",
        session_key=session_key,
        max_messages=1,
    )
    second = record_capture_result(
        state,
        "许聪",
        messages=new_messages,
        batch=new_messages,
        conversation_type="private",
        session_key=session_key,
        now="2026-06-12T12:15:05",
    )
    assert_equal(second.get("status"), "captured", "new pending signal should capture repeated long customer text")
    assert_true(
        second.get("message_ids") and second.get("message_ids") != first.get("message_ids"),
        f"canonical input ids should differ by pending signal: first={first.get('message_ids')} second={second.get('message_ids')}",
    )


def check_session_ledger_records_canonical_input_ids() -> None:
    root = Path(tempfile.mkdtemp(prefix="session-ledger-canonical-"))
    ledger = SessionLedgerStore(tenant_id="unit", root=root)
    key = "wx:rpa:v1:ledger-canonical-unit"
    message = {
        "id": "win32_ocr:same-layout",
        "source_adapter": "win32_ocr",
        "type": "text",
        "sender": "customer",
        "content": "这个车多少钱，能不能今天看",
        "bubble_rect": {"left": 420, "top": 260, "right": 760, "bottom": 310},
        "pending_signal_id": "pending-signal-ledger",
        "pending_since": "2026-06-12T12:30:00",
    }
    expected_id = message_identity(message)
    ledger.record_capture(
        session_key=key,
        target_name="客户A",
        conversation_type="private",
        capture_id="capture-canonical",
        messages=[message],
        batch=[message],
        history_backfill={"history_continuity": "anchored"},
        context_version=1,
    )
    summary = ledger.load_summary(key)
    assert_true(expected_id and expected_id != "win32_ocr:same-layout", f"expected canonical id, got {expected_id}")
    assert_equal(summary.get("last_unreplied_message_ids"), [expected_id], "ledger should store canonical input id as pending anchor")
    recent = summary.get("recent_messages") or []
    assert_true(
        any(item.get("canonical_input_id") == expected_id and item.get("legacy_message_id") == "win32_ocr:same-layout" for item in recent if isinstance(item, dict)),
        f"ledger recent message should preserve canonical and legacy ids: {recent}",
    )


def check_short_pending_signal_recovers_anchor_empty_batch() -> None:
    recovered = scheduler_module.recover_high_sensitivity_short_pending_batch(
        [
            {
                "id": "bootstrap-visible-short-id",
                "type": "text",
                "sender": "unknown",
                "content": "许聪\n在吗",
                "time": "2026-06-03T15:35:00",
            }
        ],
        {
            "pending_signal_kind": "high_sensitivity_short",
            "pending_signal_text": "在吗",
            "pending_since": "2026-06-03T15:31:48",
        },
        target_name="许聪",
        allow_self_for_test=False,
        max_batch_messages=2,
    )
    assert_true(recovered, "short pending signal should recover a visible matching short message")
    assert_true(
        recovered[0].get("id") != "bootstrap-visible-short-id",
        "recovered short signal should get a synthetic id so stale bootstrap OCR ids do not block it",
    )
    assert_equal(recovered[0].get("content"), "在吗", "speaker-prefixed OCR content should be reduced to the pending short probe")
    assert_equal(
        recovered[0].get("original_message_id"),
        "bootstrap-visible-short-id",
        "recovery should retain the original OCR id for audit",
    )
    state = empty_state()
    session = scheduler_state_module.ensure_session(state, "许聪", exact=True, conversation_type="private", now="2026-06-03T15:31:30")
    session["processed_message_ids"] = ["bootstrap-visible-short-id"]
    capture = record_capture_result(
        state,
        "许聪",
        messages=[
            {
                "id": "bootstrap-visible-short-id",
                "type": "text",
                "sender": "unknown",
                "content": "许聪\n在吗",
                "time": "2026-06-03T15:35:00",
            }
        ],
        batch=recovered,
        now="2026-06-03T15:35:01",
    )
    assert_equal(capture.get("status"), "captured", "synthetic short pending id should enter the LLM queue once")


def check_short_pending_signal_synthesizes_monitor_only_group_preview() -> None:
    recovered = scheduler_module.recover_high_sensitivity_short_pending_batch(
        [],
        {
            "pending_signal_kind": "high_sensitivity_short",
            "pending_signal_text": "许聪：在不",
            "pending_since": "2026-06-04T11:56:20",
        },
        target_name="新数据测试",
        allow_self_for_test=False,
        max_batch_messages=2,
    )
    assert_true(recovered, "monitor-only short group preview should synthesize a reply-eligible message")
    assert_true(
        str(recovered[0].get("id") or "").startswith("short_pending:"),
        f"synthesized short signal should use a stable synthetic id: {recovered}",
    )
    assert_equal(recovered[0].get("content"), "在不", "group speaker prefix must not enter semantic content")
    assert_equal(recovered[0].get("speaker_name"), "许聪", "group speaker should stay as metadata")
    assert_true(
        recovered[0].get("short_pending_synthesized_from_monitor") is True,
        "synthetic recovery should be auditable",
    )
    state = empty_state()
    session = scheduler_state_module.ensure_session(
        state,
        "新数据测试",
        exact=True,
        conversation_type="group",
        now="2026-06-04T11:56:20",
    )
    session["processed_content_keys"] = ["legacy-short-content-key"]
    capture = record_capture_result(
        state,
        "新数据测试",
        messages=[],
        batch=recovered,
        now="2026-06-04T11:56:21",
    )
    assert_equal(capture.get("status"), "captured", "monitor-only short preview should not be dropped as old history")
    assert_equal(capture.get("message_ids"), [recovered[0]["id"]], "capture should retain the synthetic short id")


def check_normal_pending_signal_synthesizes_monitor_only_group_preview() -> None:
    recovered = scheduler_module.recover_pending_signal_batch_from_monitor(
        [],
        {
            "name": "新数据测试",
            "session_key": "wx:rpa:v1:normal-monitor-preview",
            "unread_detected": True,
            "unread_badge": "visual_red_dot",
            "pending_signal_text": "许聪：有没有至少2.0T的？预算高点能接受",
            "pending_signal_kind": "normal",
            "pending_since": "2026-06-11T23:00:57",
        },
        target_name="新数据测试",
        allow_self_for_test=False,
        max_batch_messages=2,
        now="2026-06-11T23:01:05",
        max_signal_age_seconds=300,
    )
    assert_true(recovered, "normal unread group preview should synthesize a Brain input when chat pane capture is empty")
    assert_true(
        str(recovered[0].get("id") or "").startswith("monitor_pending:"),
        f"normal preview recovery should use auditable monitor_pending id: {recovered}",
    )
    assert_equal(recovered[0].get("content"), "有没有至少2.0T的？预算高点能接受", "group speaker prefix must stay out of Brain semantic text")
    assert_equal(recovered[0].get("speaker_name"), "许聪", "group speaker should be retained as metadata only")
    assert_true(
        recovered[0].get("monitor_pending_synthesized_from_preview") is True,
        "normal monitor preview recovery should be explicit in audit metadata",
    )
    state = empty_state()
    capture = record_capture_result(
        state,
        "新数据测试",
        messages=[],
        batch=recovered,
        conversation_type="group",
        session_key="wx:rpa:v1:normal-monitor-preview",
        now="2026-06-11T23:01:06",
    )
    assert_equal(capture.get("status"), "captured", "normal monitor preview should enter the Brain queue, not be swallowed")
    assert_equal(capture.get("message_ids"), [recovered[0]["id"]], "normal preview synthetic id should become the durable reply envelope anchor")


def check_stale_short_pending_signal_does_not_recover() -> None:
    recovered = scheduler_module.recover_high_sensitivity_short_pending_batch(
        [],
        {
            "pending_signal_kind": "high_sensitivity_short",
            "pending_signal_text": "好的，谢谢",
            "pending_since": "2026-06-08T13:12:40",
        },
        target_name="许聪",
        allow_self_for_test=False,
        max_batch_messages=2,
        now="2026-06-08T13:20:00",
        max_signal_age_seconds=120,
    )
    assert_true(not recovered, f"stale monitor short preview must not be resurrected as a fresh turn: {recovered}")


def check_short_pending_signal_does_not_synthesize_media_preview() -> None:
    recovered = scheduler_module.recover_high_sensitivity_short_pending_batch(
        [],
        {
            "pending_signal_kind": "high_sensitivity_short",
            "pending_signal_text": "[图片]",
            "pending_since": "2026-06-04T22:02:50",
        },
        target_name="许聪",
        allow_self_for_test=False,
        max_batch_messages=2,
    )
    assert_true(not recovered, "media-only monitor preview should not synthesize a reply-eligible short message")


def check_mixed_greeting_budget_intent_prefers_product() -> None:
    config = {"intent_router": {"heuristic_first": True, "cache_seconds": 0}}
    mixed = route_intent(
        "你好，我预算12到15万，想买省心家用二手车，主要上下班和接娃，南京能看车吗？",
        config=config,
        target_state={},
    )
    assert_equal(mixed.intent, "product_inquiry", "greeting plus concrete used-car need must not be treated as pure greeting")
    pure = route_intent("你好，在吗", config=config, target_state={})
    assert_equal(pure.intent, "greeting", "standalone greeting should still route as greeting")


def check_handoff_keyword_requires_explicit_customer_request() -> None:
    config = {"intent_router": {"heuristic_first": True, "cache_seconds": 0}}
    style_request = route_intent(
        "回复不用太长，像真人客服一样先给我一个明确方向，再告诉我到店前要补哪些信息。",
        config=config,
        target_state={},
    )
    assert_true(
        style_request.intent != "handoff_request",
        "style request mentioning 真人客服 must not trigger handoff",
    )
    explicit = route_intent("我想转人工，让销售顾问联系我。", config=config, target_state={})
    assert_equal(explicit.intent, "handoff_request", "explicit manual handoff request should still route to handoff")


def check_scheduler_conversation_context_update_does_not_advance_context_version() -> None:
    state = empty_state()
    session = scheduler_state_module.ensure_session(
        state,
        "新数据测试",
        exact=True,
        conversation_type="private",
        now="2026-06-03T13:00:00",
    )
    original_version = int(session.get("context_version") or 0)
    merge_scheduler_conversation_context(
        state,
        "新数据测试",
        {
            "last_product_id": "chejin_qinplus_2022_dmi55",
            "last_product_name": "2022款比亚迪秦PLUS DM-i 55KM",
            "last_unit_price": 8.68,
            "last_product_source": "product_master",
        },
        now="2026-06-03T13:00:01",
    )
    updated = session_by_name(state, "新数据测试")
    context = updated.get("conversation_context", {})
    assert_equal(context.get("last_product_id"), "chejin_qinplus_2022_dmi55", "scheduler should persist product context per session")
    assert_equal(context.get("last_unit_price"), 8.68, "scheduler should persist product price context")
    assert_equal(int(updated.get("context_version") or 0), original_version, "product context update should not stale current LLM task")


def check_managed_bridge_plan_reply_uses_session_key_context() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        tenant_id = "unit_plan_session_key_context"
        store = SchedulerStateStore(tenant_id=tenant_id, path=root / "scheduler_state.json")
        ledger = SessionLedgerStore(tenant_id=tenant_id, root=store.ledger_root)
        scheduler_state = store.empty_state()
        scheduler_state["sessions"] = {
            "wx:rpa:v1:same-name-a": {
                "session_key": "wx:rpa:v1:same-name-a",
                "target_name": "许聪",
                "display_name": "许聪",
                "conversation_type": "private",
                "conversation_context": {"last_customer_need_text": "A会话要奥迪", "last_product_id": "audi-a"},
            },
            "wx:rpa:v1:same-name-b": {
                "session_key": "wx:rpa:v1:same-name-b",
                "target_name": "许聪",
                "display_name": "许聪",
                "conversation_type": "private",
                "conversation_context": {"last_customer_need_text": "B会话要蔚来", "last_product_id": "nio-b"},
            },
        }
        store.save(scheduler_state)

        config_path = root / "listener_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "state_path": str(root / "workflow_state.json"),
                    "audit_log_path": str(root / "audit.jsonl"),
                    "targets": [{"name": "许聪", "enabled": True, "exact": True}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        fake_bridge = SimpleNamespace(
            tenant_id=tenant_id,
            store=store,
            ledger=ledger,
            state_path=root / "workflow_state.json",
            config={"state_path": str(root / "workflow_state.json")},
            rules={},
            target_by_name={"许聪": TargetConfig(name="许聪", enabled=True, exact=True, allow_self_for_test=False, max_batch_messages=3)},
            respond_all_unread_sessions=False,
            _workflow={"load_state": lambda _path: {"version": 1, "targets": {}}},
        )
        fake_bridge._target_for_name = ManagedListenerSchedulerBridge._target_for_name.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._target_for_session = ManagedListenerSchedulerBridge._target_for_session.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._target_state = ManagedListenerSchedulerBridge._target_state.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._merge_session_ledger_summary = ManagedListenerSchedulerBridge._merge_session_ledger_summary.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._merge_scheduler_context_into_workflow_state = ManagedListenerSchedulerBridge._merge_scheduler_context_into_workflow_state.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._workflow_state_snapshot = ManagedListenerSchedulerBridge._workflow_state_snapshot.__get__(fake_bridge, ManagedListenerSchedulerBridge)
        fake_bridge._tenant_environment = ManagedListenerSchedulerBridge._tenant_environment.__get__(fake_bridge, ManagedListenerSchedulerBridge)

        calls: list[dict[str, Any]] = []
        original = scheduler_module.plan_reply_with_listen_workflow

        def fake_plan_reply(capture: dict[str, Any], task: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            target_config = kwargs["target_config"]
            workflow_state = kwargs["workflow_state"]
            target_state = scheduler_module.workflow_target_state_for_target(workflow_state, target_config)
            context = target_state.get("conversation_context") if isinstance(target_state.get("conversation_context"), dict) else {}
            calls.append(
                {
                    "session_key": str(getattr(target_config, "session_key", "") or ""),
                    "context": copy.deepcopy(context),
                }
            )
            return {
                "ok": True,
                "target_name": str(getattr(target_config, "name", "")),
                "reply_text": "测试回复",
                "decision": {"rule_name": "customer_service_brain_reply", "visible_reply_owner": "brain"},
                "event": {"action": "planned", "reason": "unit", "customer_service_brain": {"visible_reply_owner": "brain"}},
            }

        try:
            scheduler_module.plan_reply_with_listen_workflow = fake_plan_reply
            result = ManagedListenerSchedulerBridge._plan_reply(
                fake_bridge,
                {
                    "target_name": "许聪",
                    "exact": True,
                    "conversation_type": "private",
                    "session_key": "wx:rpa:v1:same-name-b",
                },
                {"task_id": "task-b", "target_name": "许聪", "session_key": "wx:rpa:v1:same-name-b"},
            )
        finally:
            scheduler_module.plan_reply_with_listen_workflow = original
        assert_true(result.get("ok"), f"fake planner should pass: {result}")
        assert_equal(calls[0]["session_key"], "wx:rpa:v1:same-name-b", "planner target_config should carry task session_key")
        assert_equal(
            calls[0]["context"].get("last_product_id"),
            "nio-b",
            "planner workflow state should use session_key-bound context, not display-name fallback",
        )


def check_session_key_flows_from_signal_to_capture_and_reply() -> None:
    state = empty_state()
    record_session_signal(
        state,
        {
            "name": "新数据测试",
            "session_key": "wx:rpa:v1:test-session-key",
            "conversation_type": "group",
            "content": "许聪: 你好",
            "time": "04:12",
            "unread_badge": "visual_red_dot",
            "unread_detected": True,
        },
        now="2026-06-07T04:12:00",
    )
    session = state["sessions"]["wx:rpa:v1:test-session-key"]
    assert_equal(session.get("session_key"), "wx:rpa:v1:test-session-key", "session key should be persisted from unread signal")
    capture = record_capture_result(
        state,
        "新数据测试",
        messages=[{"id": "m1", "type": "text", "sender": "unknown", "content": "你好", "time": "04:12"}],
        batch=[{"id": "m1", "type": "text", "sender": "unknown", "content": "你好", "time": "04:12"}],
        history_backfill={"history_continuity": "overflow_unanchored", "overflow_batch": True, "gap_risk": False},
        conversation_type="group",
        session_key="wx:rpa:v1:test-session-key",
        now="2026-06-07T04:12:01",
    )
    assert_equal(capture.get("session_key"), "wx:rpa:v1:test-session-key", "capture should carry session key")
    task = enqueue_llm_task(state, str(capture.get("capture_id")), now="2026-06-07T04:12:02")
    assert_equal(task.get("session_key"), "wx:rpa:v1:test-session-key", "LLM task should carry session key")
    completed = complete_llm_task(
        state,
        str(task.get("task_id")),
        reply_text="晚上好，在的。",
        decision={},
        now="2026-06-07T04:12:03",
    )
    reply = completed.get("reply") or {}
    assert_equal(reply.get("session_key"), "wx:rpa:v1:test-session-key", "ready reply should carry session key")
    assert_true(bool(reply.get("message_content_digest")), "ready reply should carry message digest")
    assert_equal(reply.get("conversation_type"), "group", "ready reply should carry conversation type")


def check_same_display_name_ready_replies_are_isolated_by_session_key() -> None:
    state = empty_state()
    capture_a = record_capture_result(
        state,
        "许聪",
        messages=[{"id": "a1", "type": "text", "sender": "customer", "content": "晚上好，我想看奥迪", "time": "20:11"}],
        batch=[{"id": "a1", "type": "text", "sender": "customer", "content": "晚上好，我想看奥迪", "time": "20:11"}],
        conversation_type="private",
        session_key="wx:rpa:v1:same-name-a",
        now="2026-06-10T20:11:01",
    )
    capture_b = record_capture_result(
        state,
        "许聪",
        messages=[{"id": "b1", "type": "text", "sender": "customer", "content": "预算十万左右", "time": "20:12"}],
        batch=[{"id": "b1", "type": "text", "sender": "customer", "content": "预算十万左右", "time": "20:12"}],
        conversation_type="private",
        session_key="wx:rpa:v1:same-name-b",
        now="2026-06-10T20:12:01",
    )
    task_a = enqueue_llm_task(state, capture_a["capture_id"], now="2026-06-10T20:11:02")
    task_b = enqueue_llm_task(state, capture_b["capture_id"], now="2026-06-10T20:12:02")
    reply_a = complete_llm_task(
        state,
        task_a["task_id"],
        reply_text="奥迪这边我帮您看。",
        decision={},
        now="2026-06-10T20:11:03",
    )["reply"]
    reply_b = complete_llm_task(
        state,
        task_b["task_id"],
        reply_text="十万左右我帮您筛。",
        decision={},
        now="2026-06-10T20:12:03",
    )["reply"]
    assert_equal(reply_a.get("session_key"), "wx:rpa:v1:same-name-a", "reply A should keep session key A")
    assert_equal(reply_b.get("session_key"), "wx:rpa:v1:same-name-b", "reply B should keep session key B")
    ready = select_ready_replies(state, limit=2)
    assert_equal(
        {item.get("session_key") for item in ready},
        {"wx:rpa:v1:same-name-a", "wx:rpa:v1:same-name-b"},
        "ready reply selection should allow two same-display-name sessions when keys differ",
    )


def check_ready_reply_envelope_blocks_session_key_mismatch_before_send() -> None:
    state = empty_state()
    capture = record_capture_result(
        state,
        "许聪",
        messages=[{"id": "m1", "type": "text", "sender": "customer", "content": "晚上好", "time": "14:52"}],
        batch=[{"id": "m1", "type": "text", "sender": "customer", "content": "晚上好", "time": "14:52"}],
        conversation_type="private",
        session_key="wx:rpa:v1:session-a",
        now="2026-06-07T14:52:00",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-07T14:52:01")
    reply = complete_llm_task(
        state,
        task["task_id"],
        reply_text="晚上好，在的。",
        decision={},
        now="2026-06-07T14:52:02",
    )["reply"]
    tampered = copy.deepcopy(reply)
    tampered["session_key"] = "wx:rpa:v1:session-b"
    reason = ready_reply_session_envelope_failure(tampered, capture)
    assert_equal(reason, "reply_session_key_capture_mismatch", "session_key mismatch must block send before RPA")


def check_runtime_requeues_ready_reply_on_message_digest_mismatch() -> None:
    root = Path(tempfile.mkdtemp(prefix="scheduler-envelope-"))
    path = root / "state.json"
    store = SchedulerStateStore(tenant_id="unit_envelope", path=path)
    state = store.empty_state()
    capture = record_capture_result(
        state,
        "许聪",
        messages=[{"id": "m1", "type": "text", "sender": "customer", "content": "给我推荐一台车", "time": "14:52"}],
        batch=[{"id": "m1", "type": "text", "sender": "customer", "content": "给我推荐一台车", "time": "14:52"}],
        conversation_type="private",
        session_key="wx:rpa:v1:session-a",
        now="2026-06-07T14:52:00",
    )
    task = enqueue_llm_task(state, capture["capture_id"], now="2026-06-07T14:52:01")
    reply = complete_llm_task(
        state,
        task["task_id"],
        reply_text="可以，我先按预算和用途帮您筛。",
        decision={},
        now="2026-06-07T14:52:02",
    )["reply"]
    state["ready_replies"][reply["reply_id"]]["input_message_ids"] = ["other-message"]
    state["ready_replies"][reply["reply_id"]]["message_content_digest"] = "message_digest_wrong"
    store.save(state)
    sent: list[dict[str, Any]] = []

    runtime = CustomerServiceSchedulerRuntime(
        store=store,
        config=SchedulerConfig(enabled=True, send_max_replies_per_round=1),
        capture_fn=lambda session: {"ok": True, "messages": [], "batch": []},
        plan_reply_fn=lambda _capture, _task: {"ok": True, "reply_text": "不会执行", "decision": {}},
        freshness_fn=lambda reply_payload: {"ok": True, "stale": False},
        send_fn=lambda reply_payload: sent.append(reply_payload) or {"ok": True, "verified": True},
    )
    result = runtime.tick(allow_send=True, now="2026-06-07T14:52:03")
    assert_equal(sent, [], "digest mismatch must block before send callback")
    events = result.get("events") or []
    assert_true(
        any(item.get("event") == "reply_stale" and item.get("reason") == "reply_message_ids_not_in_capture" for item in events),
        f"mismatch should stale reply and requeue capture: {events}",
    )
    reloaded = store.load()
    session = session_by_name(reloaded, "许聪")
    recaptured_same_tick = any(item.get("event") in {"capture_empty", "capture_completed"} and item.get("target_name") == "许聪" for item in events)
    assert_true(
        bool(session.get("pending_capture")) or recaptured_same_tick,
        "blocked envelope should requeue the session or recapture it in the same tick",
    )


def check_session_ledger_marks_processed_only_after_send(tmp_dir: Path | None = None) -> None:
    root = tmp_dir or Path(tempfile.mkdtemp(prefix="session-ledger-"))
    ledger = SessionLedgerStore(tenant_id="unit", root=root)
    key = "wx:rpa:v1:ledger-unit"
    ledger.record_capture(
        session_key=key,
        target_name="客户A",
        conversation_type="private",
        capture_id="capture-1",
        messages=[{"id": "m1", "content": "你好"}],
        batch=[{"id": "m1", "content": "你好"}],
        history_backfill={"history_continuity": "overflow_unanchored"},
        context_version=1,
    )
    summary = ledger.load_summary(key)
    assert_equal(summary.get("last_captured_message_id"), "m1", "capture should update last captured")
    assert_equal(summary.get("target_name"), "客户A", "ledger summary should keep target name for session-state binding")
    assert_equal(summary.get("last_unreplied_message_ids"), ["m1"], "capture should keep pending input anchors until send")
    assert_true(not summary.get("last_processed_message_id"), "capture alone must not mark message processed")
    assert_true(bool(summary.get("recent_messages")), "capture should persist recent visible messages")
    assert_true("你好" in str(summary.get("context_summary") or ""), "ledger context summary should include captured content")
    ledger.record_reply_sent(
        session_key=key,
        target_name="客户A",
        reply_id="reply-1",
        input_message_ids=["m1"],
        input_content_keys=["unknown\x1ftext\x1f你好"],
        reply_text="你好，在的。",
        send_result={"ok": True, "verified": True},
    )
    summary = ledger.load_summary(key)
    assert_equal(summary.get("last_processed_message_id"), "m1", "send success should update processed marker")
    assert_equal(summary.get("last_unreplied_message_ids"), [], "send success should clear pending input anchors")
    assert_true(bool(summary.get("last_reply_at")), "send success should record reply timestamp")
    anchor = summary.get("last_successful_reply_anchor") or {}
    assert_equal(anchor.get("message_ids"), ["m1"], "ledger should preserve reply anchor ids")
    assert_equal(anchor.get("message_content_keys"), ["unknown\x1ftext\x1f你好"], "ledger should preserve reply content anchors")
    recent_contents = [str(item.get("content") or "") for item in summary.get("recent_messages") or []]
    assert_true("你好，在的。" in recent_contents, "sent reply should be appended to recent ledger history")


def check_scheduler_consults_ledger_before_temp_state(tmp_dir: Path | None = None) -> None:
    root = tmp_dir or Path(tempfile.mkdtemp(prefix="session-ledger-state-"))
    key = "wx:rpa:v1:ledger-state-unit"
    ledger = SessionLedgerStore(tenant_id="unit", root=root)
    ledger.record_capture(
        session_key=key,
        target_name="客户A",
        conversation_type="private",
        capture_id="capture-1",
        messages=[{"id": "m1", "type": "text", "sender": "customer", "content": "之前已经回过的问题", "time": "04:30"}],
        batch=[{"id": "m1", "type": "text", "sender": "customer", "content": "之前已经回过的问题", "time": "04:30"}],
        history_backfill={"history_continuity": "anchored"},
        context_version=1,
    )
    ledger.record_reply_sent(
        session_key=key,
        target_name="客户A",
        reply_id="reply-1",
        input_message_ids=["m1"],
        input_content_keys=["customer\x1ftext\x1f之前已经回过的问题"],
        reply_text="已经回复过。",
        send_result={"ok": True, "verified": True},
    )

    original_store = scheduler_state_module.SessionLedgerStore

    class BoundLedgerStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._delegate = SessionLedgerStore(tenant_id="unit", root=root)

        def load_summary(self, session_key: str) -> dict[str, Any]:
            return self._delegate.load_summary(session_key)

        def record_capture(self, **kwargs: Any) -> None:
            return self._delegate.record_capture(**kwargs)

        def record_reply_sent(self, **kwargs: Any) -> None:
            return self._delegate.record_reply_sent(**kwargs)

    scheduler_state_module.SessionLedgerStore = BoundLedgerStore
    try:
        state = empty_state()
        capture = record_capture_result(
            state,
            "客户A",
            messages=[{"id": "m1", "type": "text", "sender": "customer", "content": "之前已经回过的问题", "time": "04:30"}],
            batch=[{"id": "m1", "type": "text", "sender": "customer", "content": "之前已经回过的问题", "time": "04:30"}],
            conversation_type="private",
            session_key=key,
            now="2026-06-07T04:31:00",
        )
    finally:
        scheduler_state_module.SessionLedgerStore = original_store
    assert_equal(capture.get("status"), "empty", "ledger processed marker should suppress old message after restart")

    target_state: dict[str, Any] = {}
    fake_bridge = SimpleNamespace(ledger=ledger)
    ManagedListenerSchedulerBridge._merge_session_ledger_summary(fake_bridge, target_state, key)
    context = target_state.get("conversation_context") or {}
    assert_true(bool(context.get("ledger_recent_messages")), "ledger recent messages should be injected into workflow context")
    assert_true("之前已经回过的问题" in str(context.get("ledger_context_summary") or ""), "ledger context summary should be injected")
    interaction = target_state.get("conversation_interaction_state") or {}
    assert_true(interaction.get("unanswered_exists") is False, "fully replied ledger should not expose open unanswered state")


def check_scheduler_ledger_injects_unanswered_interaction_state(tmp_dir: Path | None = None) -> None:
    root = tmp_dir or Path(tempfile.mkdtemp(prefix="session-ledger-unanswered-"))
    key = "wx:rpa:v1:ledger-unanswered-unit"
    ledger = SessionLedgerStore(tenant_id="unit", root=root)
    ledger.record_capture(
        session_key=key,
        target_name="客户A",
        conversation_type="private",
        capture_id="capture-1",
        messages=[{"id": "m1", "type": "text", "sender": "customer", "content": "十万左右适合女性开的电车或混动", "time": "04:30"}],
        batch=[{"id": "m1", "type": "text", "sender": "customer", "content": "十万左右适合女性开的电车或混动", "time": "04:30"}],
        history_backfill={"history_continuity": "anchored"},
        context_version=1,
    )
    target_state: dict[str, Any] = {}
    fake_bridge = SimpleNamespace(ledger=ledger)
    ManagedListenerSchedulerBridge._merge_session_ledger_summary(fake_bridge, target_state, key)
    interaction = target_state.get("conversation_interaction_state") or {}
    assert_true(interaction.get("unanswered_exists") is True, f"unreplied ledger input should become interaction state: {interaction}")
    assert_equal(interaction.get("last_unanswered_message_ids"), ["m1"], "unreplied message id should be preserved")
    assert_true(
        "十万左右适合女性" in str(interaction.get("last_unanswered_customer_text") or ""),
        f"unreplied text should be preserved for Brain context: {interaction}",
    )


class FakeAnchorHistoryConnector:
    def get_messages(self, target: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "messages": [
                {"id": "new-1", "type": "text", "sender": "unknown", "content": "我刷了很多消息，现在问最新这个", "time": "04:20"},
            ],
            "history_load": {"ok": True, "anchor_found": False, "scroll_steps": 2},
        }


def check_anchor_missing_after_light_backfill_uses_overflow_batch() -> None:
    payload = {
        "ok": True,
        "messages": [
            {"id": "new-1", "type": "text", "sender": "unknown", "content": "我刷了很多消息，现在问最新这个", "time": "04:20"},
        ],
    }
    target_state = {
        "processed_message_ids": ["old-anchor"],
        "processed_content_keys": [],
        "handoff_message_ids": [],
    }
    enriched = maybe_enrich_messages_with_history(
        connector=FakeAnchorHistoryConnector(),
        target=TargetConfig("客户A", True, True, False, 3),
        config={
            "history_backfill": {
                "enabled": True,
                "mode": "anchor_until_found",
                "max_scroll_steps": 2,
                "overflow_batch_on_anchor_missing": True,
                "block_on_anchor_not_found": True,
                "block_on_gap_risk": True,
            }
        },
        payload=payload,
        target_state=target_state,
    )
    meta = enriched.get("_history_backfill") or {}
    assert_equal(meta.get("history_continuity"), "overflow_unanchored", "anchor miss should downgrade to overflow batch")
    assert_true(meta.get("overflow_batch") is True, "overflow flag should be set")
    assert_true(meta.get("gap_risk") is False, "overflow batch should not block reply")
    selection = select_batch_details(
        enriched.get("messages") or [],
        target_state=target_state,
        allow_self_for_test=False,
        max_batch_messages=3,
        config={},
    )
    assert_equal([item.get("id") for item in selection.batch], ["new-1"], "visible overflow message should remain reply-eligible")


def run_checks() -> dict[str, Any]:
    checks = [
        check_pending_sessions_survive_round_limit,
        check_no_change_signal_does_not_clear_pending,
        check_unread_signal_without_preview_enters_pending,
        check_context_version_marks_old_llm_task_stale,
        check_duplicate_active_capture_does_not_stale_llm_task,
        check_ready_reply_fifo_and_same_session_latest_only,
        check_customer_profile_store_concurrent_json_writes,
        check_session_monitor_keeps_overflow_pending,
        check_session_monitor_empty_preview_does_not_clear_pending,
        check_session_monitor_visual_unread_badge_retriggers_after_reset,
        check_passive_probe_defers_when_monitor_has_unread_signal,
        check_session_monitor_high_sensitivity_short_signal_waits_merge_window,
        check_session_monitor_preserves_high_sensitivity_pending_after_empty_capture,
        check_session_monitor_preserves_normal_pending_after_empty_capture_briefly,
        check_session_monitor_low_disturbance_ignores_normal_preview_without_badge,
        check_session_monitor_low_disturbance_keeps_short_preview_signal,
        check_session_monitor_low_risk_requires_badge_and_preview_signal,
        check_session_monitor_event_driven_dispatch_keeps_sticky_target,
        check_session_monitor_event_driven_dispatch_rotates_under_hot_target,
        check_capture_failed_backoff_blocks_immediate_requeue,
        check_runtime_tick_does_not_wait_for_slow_llm,
        check_runtime_retries_same_capture_for_monitor_only_short_pending_after_brain_no_visible_reply,
        check_runtime_retries_same_capture_for_real_ocr_short_probe_after_brain_no_visible_reply,
        check_runtime_retries_same_capture_for_full_customer_capture_after_brain_no_visible_reply,
        check_runtime_exhausted_brain_no_visible_reply_preserves_internal_handoff,
        check_runtime_retries_same_capture_for_brain_schema_failure,
        check_runtime_same_capture_retry_can_recover_without_rpa_recapture,
        check_scheduler_cleanup_clears_session_ready_refs_without_losing_recent_audit,
        check_scheduler_cleanup_preserves_stale_recoverable_llm_pending_messages,
        check_select_capture_sessions_preserves_recoverable_llm_pending_messages,
        check_runtime_latency_trace_flows_through_reply_lifecycle,
        check_polish_latency_trace_is_inherited_by_ready_reply,
        check_runtime_future_latency_trace_exposes_external_overhead,
        check_planner_event_internal_latency_trace_is_extracted,
        check_scheduler_fast_followup_treats_unread_and_capture_as_urgent,
        check_runtime_repeated_unread_signal_does_not_stale_same_batch,
        check_runtime_send_runner_stales_before_send,
        check_runtime_stale_reply_context_is_preserved_for_brain_repair,
        check_reply_sent_preserves_followup_pending_signal,
        check_runtime_same_tick_fast_llm_send_has_capture_snapshot,
        check_runtime_send_runner_fifo,
        check_runtime_send_event_includes_observability,
        check_runtime_prioritizes_ready_send_before_new_capture,
        check_runtime_recovers_orphaned_running_llm_task_after_restart,
        check_runtime_times_out_running_llm_task_without_swallowing_message,
        check_runtime_restores_missing_llm_task_from_in_memory_snapshot,
        check_runtime_recovers_orphaned_running_polish_task_after_restart,
        check_runtime_restores_missing_polish_task_from_in_memory_snapshot,
        check_runtime_degraded_polish_reply_still_sends,
        check_runtime_polish_failure_result_is_json_safe,
        check_runtime_final_polish_block_requeues_brain_with_feedback,
        check_scheduler_config_derives_planner_timeout_from_brain_budget,
        check_runtime_brain_budget_prevents_premature_scheduler_timeout,
        check_preview_change_during_active_work_defers_capture_without_hopping,
        check_preview_change_without_unread_evidence_is_baseline_only_when_idle,
        check_captured_messages_connector_accepts_history_kwargs,
        check_captured_messages_connector_uses_batch_when_messages_empty,
        check_scheduler_capture_filters_self_only_normal_customer_session,
        check_scheduler_capture_allows_new_occurrence_of_same_short_probe,
        check_scheduler_capture_filters_non_text_messages_for_normal_customer_session,
        check_scheduler_capture_filters_visual_ocr_text_without_keyword_blocking,
        check_scheduler_capture_allows_self_for_file_transfer_self_test,
        check_runtime_self_only_capture_does_not_submit_llm,
        check_scheduler_planner_reuses_capture_history_backfill_verdict,
        check_workflow_planner_uses_captured_messages_without_sending,
        check_workflow_planner_handles_short_pending_batch_fallback,
        check_scheduler_authoritative_short_batch_bypasses_legacy_content_key_dedupe,
        check_workflow_planner_uses_warm_short_farewell_without_sales_redirect,
        check_scheduler_planner_applies_final_visible_polish_without_sending,
        check_scheduler_split_polish_stage_preserves_final_visible_polish_quality,
        check_scheduler_split_polish_stage_degrades_to_brain_draft_when_polish_unavailable,
        check_runtime_dual_backend_pools_keep_planner_moving_while_polish_runs,
        check_listener_scheduler_config_gate,
        check_listener_poll_interval_uses_randomized_window_config,
        check_live_safety_applies_backend_scheduler_defaults,
        check_live_safety_file_transfer_defaults_to_self_test_target,
        check_listener_rpa_send_rate_zero_is_preserved,
        check_managed_bridge_applies_rpa_fast_send_confirmation_env,
        check_managed_bridge_capture_send_marks_workflow_state,
        check_managed_bridge_freshness_preview_fast_pass_without_strict_scan,
        check_managed_bridge_freshness_preview_unread_uses_strict_scan,
        check_managed_bridge_freshness_same_short_signal_fast_passes_without_strict_scan,
        check_managed_bridge_freshness_uses_session_key_for_duplicate_display_names,
        check_managed_bridge_pending_capture_same_signal_does_not_stale_ready_reply,
        check_managed_bridge_freshness_session_list_preview_fast_pass_without_monitor,
        check_managed_bridge_freshness_session_list_mismatch_falls_back_to_strict_scan,
        check_managed_bridge_freshness_session_list_mismatch_soft_pass_by_default,
        check_managed_bridge_soft_passes_unconfirmed_original_batch_not_visible_stale,
        check_managed_bridge_freshness_strict_interval_fallback,
        check_managed_bridge_soft_passes_unconfirmed_short_ocr_strict_freshness,
        check_managed_bridge_freshness_long_llm_uses_task_runtime_not_queue_age,
        check_managed_bridge_collect_signals_skips_busy_sticky_target,
        check_managed_bridge_collect_signals_does_not_dispatch_when_all_pending_busy,
        check_managed_bridge_capture_applies_humanized_switch_delay,
        check_session_monitor_event_driven_can_batch_two_unread_targets_without_whitelist_scan,
        check_session_monitor_badge_cleared_preview_change_does_not_dispatch_targets,
        check_scheduler_cleanup_merges_legacy_name_bucket_into_session_key_bucket,
        check_session_monitor_blocks_ambiguous_duplicate_display_names,
        check_session_monitor_allows_duplicate_display_names_when_session_keys_are_distinct,
        check_session_monitor_reload_keeps_display_name_for_session_key_pending,
        check_session_monitor_poll_repairs_legacy_session_key_only_state,
        check_scheduler_same_display_name_sessions_are_isolated_by_session_key,
        check_managed_bridge_normalizes_legacy_switch_interval_to_humanized_window,
        check_repeatable_short_greeting_is_not_blocked_by_processed_content_keys,
        check_anchor_payload_skips_repeatable_short_greeting_keys,
        check_scheduler_capture_allows_repeated_short_greeting_after_previous_reply,
        check_repeatable_short_message_identity_uses_occurrence_time,
        check_canonical_pending_signal_allows_repeated_long_message_capture,
        check_session_ledger_records_canonical_input_ids,
        check_short_pending_signal_recovers_anchor_empty_batch,
        check_short_pending_signal_synthesizes_monitor_only_group_preview,
        check_normal_pending_signal_synthesizes_monitor_only_group_preview,
        check_stale_short_pending_signal_does_not_recover,
        check_short_pending_signal_does_not_synthesize_media_preview,
        check_mixed_greeting_budget_intent_prefers_product,
        check_handoff_keyword_requires_explicit_customer_request,
        check_scheduler_conversation_context_update_does_not_advance_context_version,
        check_managed_bridge_plan_reply_uses_session_key_context,
        check_session_key_flows_from_signal_to_capture_and_reply,
        check_same_display_name_ready_replies_are_isolated_by_session_key,
        check_ready_reply_envelope_blocks_session_key_mismatch_before_send,
        check_runtime_requeues_ready_reply_on_message_digest_mismatch,
        check_session_ledger_marks_processed_only_after_send,
        check_scheduler_consults_ledger_before_temp_state,
        check_scheduler_ledger_injects_unanswered_interaction_state,
        check_anchor_missing_after_light_backfill_uses_overflow_batch,
    ]
    results = []
    for check in checks:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    return {"ok": not failures, "count": len(results), "failures": failures, "results": results}


def main() -> int:
    result = run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
