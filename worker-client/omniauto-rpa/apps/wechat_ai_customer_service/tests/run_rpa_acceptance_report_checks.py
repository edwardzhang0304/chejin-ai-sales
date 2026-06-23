"""Offline checks for RPA acceptance report gates."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.rpa_acceptance_report import (  # noqa: E402
    collect_rpa_acceptance_report,
    render_rpa_acceptance_markdown,
)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def good_listener_config() -> dict[str, Any]:
    return {
        "rpa_humanized_send": {
            "enabled": True,
            "input_method": "sendinput_unicode",
            "adaptive_speed_enabled": True,
            "fast_send_confirmation_enabled": True,
        },
        "rpa_operator_guard": {
            "enabled": True,
            "block_manual_input": True,
            "floating_indicator_enabled": True,
            "control_hotkey": "f8",
        },
        "transport_risk_guard": {
            "enabled": True,
            "passive_logout_probe_enabled": True,
        },
        "history_backfill": {"enabled": True},
        "semantic_batch_planner": {"enabled": True},
        "concurrency_scheduler": {"enabled": True},
    }


def make_runtime_fixture(root: Path, *, config: dict[str, Any] | None = None, running: bool = False, guard: bool = False) -> None:
    tenant_root = root / "tenants" / "unit"
    write_json(tenant_root / "customer_service" / "listener_config.json", config or good_listener_config())
    write_json(tenant_root / "customer_service" / "runtime_status.json", {"state": "idle" if running else "stopped", "running": running})
    write_json(tenant_root / "recorder" / "runtime_status.json", {"state": "stopped", "running": False})
    if guard:
        write_json(tenant_root / "customer_service" / "operator_guard.pid.json", {"pid": os.getpid()})
        write_json(tenant_root / "customer_service" / "operator_guard.state.json", {"pid": os.getpid(), "phase": "running", "hooks_installed": True})


class FakeConnector:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def capabilities(self, *, interactive: bool = False) -> dict[str, Any]:
        result = dict(self.payload)
        result["interactive"] = interactive
        return result


def check_good_fixture_passes_without_live_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_good_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root)
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={}, wechat_probe="none")
        assert_true(report["status"] == "pass", f"good offline fixture should pass: {report}")
        markdown = render_rpa_acceptance_markdown(report)
        assert_true("RPA Acceptance Report" in markdown and "rpa_only_transport" in markdown, "markdown should list checks")


def check_recorder_only_fixture_uses_guard_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_recorder_only_") as tmp:
        root = Path(tmp)
        tenant_root = root / "tenants" / "unit"
        write_json(tenant_root / "customer_service" / "runtime_status.json", {"state": "stopped", "running": False})
        write_json(tenant_root / "recorder" / "runtime_status.json", {"state": "stopped", "running": False})
        write_json(tenant_root / "recorder" / "settings.json", {"enabled": True, "capture_interval_seconds": 5})
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={}, wechat_probe="none")
        assert_true(report["status"] == "pass", f"recorder-only fixture should pass with effective guard defaults: {report}")
        guard = report["snapshots"]["operator_guard_config"]
        assert_true(guard.get("_source") == "recorder.effective_defaults", f"expected recorder defaults source: {guard}")


def check_wxauto4_enabled_fails() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_wxauto_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root)
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={"WECHAT_ENABLE_WXAUTO4": "1"}, wechat_probe="none")
        assert_true(report["status"] == "fail", f"wxauto4 env must fail: {report}")
        assert_true(any(item["id"] == "rpa_only_transport" for item in report["summary"]["failures"]), "rpa-only gate should fail")


def check_clipboard_once_fails_for_normal_acceptance() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_clipboard_") as tmp:
        root = Path(tmp)
        config = good_listener_config()
        config["rpa_humanized_send"]["input_method"] = "clipboard_once"
        make_runtime_fixture(root, config=config)
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={}, wechat_probe="none")
        assert_true(report["status"] == "fail", f"clipboard_once must fail normal acceptance: {report}")
        assert_true(any(item["id"] == "humanized_send_policy" for item in report["summary"]["failures"]), "humanized send gate should fail")


def check_fixed_window_origin_required() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_window_origin_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root)
        report = collect_rpa_acceptance_report(
            runtime_root=root,
            tenant_id="unit",
            env={"WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN": "0"},
            wechat_probe="none",
        )
        assert_true(report["status"] == "fail", f"disabled fixed origin must fail: {report}")
        assert_true(any(item["id"] == "window_normalization_policy" for item in report["summary"]["failures"]), "window normalization gate should fail")


def check_running_without_guard_fails() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_guard_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root, running=True, guard=False)
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={}, wechat_probe="none")
        assert_true(report["status"] == "fail", f"running without guard must fail: {report}")
        assert_true(any(item["id"] == "customer_service_runtime_guard" for item in report["summary"]["failures"]), "guard gate should fail")


def check_running_with_guard_passes() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_guard_ok_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root, running=True, guard=True)
        report = collect_rpa_acceptance_report(runtime_root=root, tenant_id="unit", env={}, wechat_probe="none")
        assert_true(report["status"] == "pass", f"running with guard should pass: {report}")


def check_probe_detects_blank_or_wxauto() -> None:
    with tempfile.TemporaryDirectory(prefix="rpa_acceptance_probe_") as tmp:
        root = Path(tmp)
        make_runtime_fixture(root)
        blank = collect_rpa_acceptance_report(
            runtime_root=root,
            tenant_id="unit",
            env={},
            wechat_probe="passive",
            connector=FakeConnector({"ok": False, "online": False, "adapter": "win32_ocr", "state": "blank_render_detected"}),
        )
        assert_true(blank["status"] == "fail", f"blank render should fail: {blank}")
        wxauto = collect_rpa_acceptance_report(
            runtime_root=root,
            tenant_id="unit",
            env={},
            wechat_probe="passive",
            connector=FakeConnector({"ok": True, "online": True, "adapter": "wxauto4", "scheme": "wxauto4", "state": "wxauto4_reserve_ready"}),
        )
        assert_true(wxauto["status"] == "fail", f"wxauto capability should fail: {wxauto}")
        auxiliary = collect_rpa_acceptance_report(
            runtime_root=root,
            tenant_id="unit",
            env={},
            wechat_probe="passive",
            connector=FakeConnector({"ok": False, "online": False, "adapter": "win32_ocr", "state": "auxiliary_shell_window_detected"}),
        )
        assert_true(auxiliary["status"] == "fail", f"auxiliary shell should fail: {auxiliary}")


def run_all() -> dict[str, Any]:
    checks = [
        check_good_fixture_passes_without_live_probe,
        check_recorder_only_fixture_uses_guard_defaults,
        check_wxauto4_enabled_fails,
        check_clipboard_once_fails_for_normal_acceptance,
        check_fixed_window_origin_required,
        check_running_without_guard_fails,
        check_running_with_guard_passes,
        check_probe_detects_blank_or_wxauto,
    ]
    results = []
    failures = []
    for check in checks:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:  # noqa: BLE001
            item = {"name": check.__name__, "ok": False, "error": repr(exc)}
            results.append(item)
            failures.append(item)
    return {"ok": not failures, "count": len(results), "failures": failures, "results": results}


if __name__ == "__main__":
    summary = run_all()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if summary["ok"] else 1)
