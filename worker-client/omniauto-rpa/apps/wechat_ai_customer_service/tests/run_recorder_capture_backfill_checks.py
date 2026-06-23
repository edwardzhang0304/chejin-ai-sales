"""Dry-run checks for smart recorder burst-message backfill."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


os.environ["WECHAT_STORAGE_BACKEND"] = "json"
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.recorder_service import RecorderService  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_runtime_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.recorder_loop import adjusted_sleep_interval  # noqa: E402


TEST_TENANT = "recorder_capture_backfill_test"


class FakeConnector:
    def __init__(self, visible: list[dict[str, Any]], loaded: list[dict[str, Any]] | None = None, *, loaded_ok: bool = True) -> None:
        self.visible = visible
        self.loaded = loaded if loaded is not None else visible
        self.loaded_ok = loaded_ok
        self.history_load_calls: list[int] = []

    def get_messages(self, target: str, exact: bool = True, history_load_times: int = 0) -> dict[str, Any]:
        if history_load_times:
            self.history_load_calls.append(history_load_times)
            return {
                "ok": self.loaded_ok,
                "target": target,
                "exact": exact,
                "history_load": {"ok": self.loaded_ok, "requested_load_times": history_load_times},
                "messages": self.loaded,
            }
        return {"ok": True, "target": target, "exact": exact, "messages": self.visible}

    def send_text(self, target: str, text: str, exact: bool = True, *, skip_send_rate_guard: bool = False) -> dict[str, Any]:
        return {"ok": True, "target": target, "text": text, "exact": exact}


def main() -> int:
    results = []
    try:
        with tenant_context(TEST_TENANT):
            cleanup_runtime()
            for check in (
                check_anchor_in_visible_window_skips_history_load,
                check_missing_anchor_triggers_history_backfill,
                check_missing_anchor_after_backfill_reports_gap_risk,
                check_burst_recovery_shortens_next_loop_sleep,
            ):
                try:
                    results.append({"name": check.__name__, "ok": True, "details": check()})
                except Exception as exc:
                    results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
                finally:
                    cleanup_runtime()
        payload = {"ok": all(item["ok"] for item in results), "results": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    finally:
        with tenant_context(TEST_TENANT):
            cleanup_runtime()


def check_anchor_in_visible_window_skips_history_load() -> dict[str, Any]:
    service, conversation = seeded_service_with_messages([msg("m1", "第一条订单 100元"), msg("m2", "第二条订单 200元")])
    connector = FakeConnector([msg("m2", "第二条订单 200元"), msg("m3", "第三条订单 300元")])
    service.connector = connector
    result = service.capture_conversation(conversation, auto_learn=False, use_llm=False, send_notification=False)
    recovery = result.get("capture_recovery") or {}
    assert_equal(connector.history_load_calls, [], "visible anchor should not load history")
    assert_equal(result.get("inserted_count"), 1, "only new visible message should be inserted")
    assert_true(recovery.get("anchor_found") is True, "anchor should be found")
    assert_equal(recovery.get("anchor_source"), "initial_window", "anchor source")
    return {"inserted_count": result.get("inserted_count"), "recovery": recovery}


def check_missing_anchor_triggers_history_backfill() -> dict[str, Any]:
    service, conversation = seeded_service_with_messages([msg("m1", "第一条订单 100元"), msg("m2", "第二条订单 200元")])
    visible = [msg("m5", "第五条订单 500元"), msg("m6", "第六条订单 600元")]
    loaded = [
        msg("m2", "第二条订单 200元"),
        msg("m3", "第三条订单 300元"),
        msg("m4", "第四条订单 400元"),
        msg("m5", "第五条订单 500元"),
        msg("m6", "第六条订单 600元"),
    ]
    connector = FakeConnector(visible, loaded)
    service.connector = connector
    result = service.capture_conversation(conversation, auto_learn=False, use_llm=False, send_notification=False)
    recovery = result.get("capture_recovery") or {}
    stored_contents = [item.get("content") for item in RawMessageStore(tenant_id=TEST_TENANT).list_messages_advanced(conversation_id=conversation["conversation_id"], limit=20)]
    assert_equal(connector.history_load_calls, [3], "missing anchor should load history")
    assert_equal(result.get("inserted_count"), 4, "history load should recover missed middle messages")
    assert_true("第三条订单 300元" in stored_contents, "missed message m3 should be stored")
    assert_true("第四条订单 400元" in stored_contents, "missed message m4 should be stored")
    assert_true(recovery.get("anchor_found") is True, "history should find anchor")
    assert_true(recovery.get("gap_risk") is False, "successful backfill should not flag gap")
    return {"inserted_count": result.get("inserted_count"), "history_load_calls": connector.history_load_calls, "recovery": recovery}


def check_missing_anchor_after_backfill_reports_gap_risk() -> dict[str, Any]:
    service, conversation = seeded_service_with_messages([msg("m1", "第一条订单 100元")])
    visible = [msg("m5", "第五条订单 500元"), msg("m6", "第六条订单 600元")]
    loaded = [msg("m4", "第四条订单 400元"), *visible]
    connector = FakeConnector(visible, loaded)
    service.connector = connector
    result = service.capture_conversation(conversation, auto_learn=False, use_llm=False, send_notification=False)
    recovery = result.get("capture_recovery") or {}
    assert_equal(connector.history_load_calls, [3], "gap scenario should still attempt history load")
    assert_true(recovery.get("gap_risk") is True, "unresolved anchor should flag gap risk")
    assert_equal(recovery.get("reason"), "anchor_missing_after_history_load", "gap reason")
    assert_equal(result.get("inserted_count"), 3, "visible loaded messages should still be stored")
    return {"inserted_count": result.get("inserted_count"), "recovery": recovery}


def check_burst_recovery_shortens_next_loop_sleep() -> dict[str, Any]:
    normal = adjusted_sleep_interval({"items": [{"capture_recovery": {"gap_risk": False, "history_load_applied": False}}]}, 30)
    recovered = adjusted_sleep_interval({"items": [{"capture_recovery": {"gap_risk": False, "history_load_applied": True}}]}, 30)
    risky = adjusted_sleep_interval({"items": [{"capture_recovery": {"gap_risk": True, "history_load_applied": True}}]}, 30)
    assert_equal(normal, 30, "normal loop interval")
    assert_equal(recovered, 5, "history backfill should temporarily speed up loop")
    assert_equal(risky, 5, "gap risk should temporarily speed up loop")
    return {"normal": normal, "recovered": recovered, "risky": risky}


def seeded_service_with_messages(initial_messages: list[dict[str, Any]]) -> tuple[RecorderService, dict[str, Any]]:
    store = RawMessageStore(tenant_id=TEST_TENANT)
    conversation = store.upsert_conversation(
        {
            "target_name": "高频测试群",
            "display_name": "高频测试群",
            "conversation_type": "group",
            "selected_by_user": True,
            "learning_enabled": False,
            "notify_enabled": False,
            "source": {"type": "wechat_session_discovery"},
        }
    )
    store.upsert_messages(conversation, initial_messages, source_module="seed", learning_enabled=False, create_batch=False)
    service = RecorderService(tenant_id=TEST_TENANT)
    return service, conversation


def msg(message_id: str, content: str, *, sender: str = "客户", time_text: str | None = None) -> dict[str, Any]:
    suffix = int(message_id[1:]) if message_id[1:].isdigit() else 0
    return {
        "id": message_id,
        "type": "text",
        "sender": sender,
        "content": content,
        "time": time_text or f"2026-05-22 10:{suffix:02d}:00",
    }


def cleanup_runtime() -> None:
    root = tenant_runtime_root(TEST_TENANT)
    if root.exists():
        shutil.rmtree(root)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
