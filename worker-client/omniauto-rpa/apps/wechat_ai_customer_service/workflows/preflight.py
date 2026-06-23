"""Preflight checks before running WeChat customer-service automation.

The preflight is read-only: it checks configuration, sidecar availability,
WeChat login status, recent sessions, review-queue health, and target-name
risks. It does not send messages or open target chats.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS_ROOT = APP_ROOT / "adapters"
WORKFLOWS_ROOT = Path(__file__).resolve().parent
for path in (WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from customer_service_review_queue import build_review_queue
from listen_and_reply import CONFIG_PATH, load_config, parse_targets, resolve_path
from wechat_connector import WeChatConnector


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--target",
        action="append",
        help="Extra target to check in addition to enabled config targets. Can be passed multiple times.",
    )
    parser.add_argument("--skip-wechat", action="store_true", help="Only check local files and state.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human-readable output.")
    args = parser.parse_args()

    result = run_preflight(args.config, extra_targets=args.target or [], skip_wechat=args.skip_wechat)
    if args.json:
        print_json(result)
    else:
        print_human(result)
    return 0 if result.get("ok") else 1


def run_preflight(config_path: Path, extra_targets: list[str], skip_wechat: bool) -> dict[str, Any]:
    checks = []
    warnings = []
    errors = []

    config = load_config(config_path)
    local_checks = check_local_config(config_path, config)
    checks.extend(local_checks["checks"])
    warnings.extend(local_checks["warnings"])
    errors.extend(local_checks["errors"])

    configured_targets = [target.name for target in parse_targets(config)]
    targets = unique_list([*configured_targets, *[target.strip() for target in extra_targets if target.strip()]])

    state_path = resolve_path(config.get("state_path"))
    audit_path = resolve_path(config.get("audit_log_path"))
    queue = build_review_queue(config_path=config_path, include_resolved=False, limit=100)
    queue_counts = queue.get("counts", {})
    if queue_counts.get("open_pending_customer_data", 0):
        warnings.append("There are open pending customer-data records.")
    if queue_counts.get("handoff", 0) or queue_counts.get("audit_attention", 0):
        warnings.append("There are handoff or audit-attention records to review before expanding targets.")

    connector = WeChatConnector()
    wechat: dict[str, Any] = {
        "skipped": bool(skip_wechat),
        "wxauto4_reserve_enabled": connector.wxauto4_reserve_enabled(),
        "sidecar_python": str(connector.sidecar_python),
        "sidecar_script": str(connector.sidecar_script),
        "compat_sidecar_script": str(connector.compat_sidecar_script),
    }
    if not connector.compat_sidecar_script.exists():
        errors.append(f"Missing primary WeChat RPA sidecar script: {connector.compat_sidecar_script}")
    if wechat["wxauto4_reserve_enabled"]:
        if not connector.sidecar_python.exists():
            warnings.append(f"Missing wxauto4 reserve sidecar Python: {connector.sidecar_python}")
        if not connector.sidecar_script.exists():
            warnings.append(f"Missing wxauto4 reserve sidecar script: {connector.sidecar_script}")

    if not skip_wechat and not errors:
        # Preflight is user-triggered, so it should recover a minimized/tray
        # WeChat window instead of preserving the background health check's
        # passive semantics.
        status = connector.status(interactive=True)
        wechat["status"] = status
        if not status.get("ok") or not status.get("online"):
            errors.append("WeChat is not connected to a logged-in main window.")
        else:
            sessions_payload = connector.list_sessions()
            wechat["sessions"] = sessions_payload
            if not sessions_payload.get("ok"):
                warnings.append("Could not read recent WeChat sessions.")

    target_reports = build_target_reports(
        config_targets=configured_targets,
        requested_targets=targets,
        wechat=wechat,
    )
    for report in target_reports:
        warnings.extend(report.get("warnings", []))
        errors.extend(report.get("errors", []))

    passed = not errors
    return {
        "ok": passed,
        "config_path": str(config_path.resolve()),
        "state_path": str(state_path),
        "audit_log_path": str(audit_path),
        "checks": checks,
        "warnings": unique_list(warnings),
        "errors": unique_list(errors),
        "configured_targets": configured_targets,
        "target_reports": target_reports,
        "review_queue_counts": queue_counts,
        "rpa_humanized_send": config.get("rpa_humanized_send", {}),
        "wechat": compact_wechat_report(wechat),
        "recommended_next_steps": recommended_next_steps(passed, warnings, target_reports, config_path),
    }


def check_local_config(config_path: Path, config: dict[str, Any]) -> dict[str, list[str]]:
    checks = []
    warnings = []
    errors = []

    checks.append(f"Config loaded: {config_path.resolve()}")
    for key in ["rules_path", "state_path", "audit_log_path"]:
        if not config.get(key):
            errors.append(f"Config is missing {key}.")
            continue
        path = resolve_path(config.get(key))
        if key == "rules_path":
            if path.exists():
                checks.append(f"Rules file exists: {path}")
            else:
                errors.append(f"Rules file does not exist: {path}")
        else:
            parent = path.parent
            if parent.exists():
                checks.append(f"{key} parent exists: {parent}")
            else:
                warnings.append(f"{key} parent will be created on first write: {parent}")

    data_capture = config.get("data_capture", {}) or {}
    if data_capture.get("enabled"):
        workbook_path = resolve_path(data_capture.get("workbook_path"))
        if workbook_path.parent.exists():
            checks.append(f"Customer workbook parent exists: {workbook_path.parent}")
        else:
            warnings.append(f"Customer workbook parent will be created on first write: {workbook_path.parent}")
        required = data_capture.get("required_fields", []) or []
        if not required:
            warnings.append("data_capture.required_fields is empty.")
    else:
        warnings.append("data_capture is disabled.")

    return {"checks": checks, "warnings": warnings, "errors": errors}


def build_target_reports(
    config_targets: list[str],
    requested_targets: list[str],
    wechat: dict[str, Any],
) -> list[dict[str, Any]]:
    status = wechat.get("status") or {}
    my_info = status.get("my_info") if isinstance(status, dict) else {}
    display_name = str((my_info or {}).get("display_name") or "")
    sessions = extract_session_names((wechat.get("sessions") or {}).get("sessions", []))

    reports = []
    for target in requested_targets:
        warnings = []
        errors = []
        if target not in config_targets:
            warnings.append(f"Target is not enabled in config: {target}")
        if display_name and target == display_name:
            warnings.append(
                f"Target name matches current login display_name ({display_name}); confirm this is not the bot account."
            )
        exact_recent_session = target in sessions
        partial_recent_sessions = [name for name in sessions if target and target in name and name != target]
        if sessions and not exact_recent_session:
            warnings.append(f"Target is not in recent session list: {target}")
        reports.append(
            {
                "target": target,
                "enabled_in_config": target in config_targets,
                "exact_recent_session": exact_recent_session,
                "partial_recent_sessions": partial_recent_sessions,
                "warnings": warnings,
                "errors": errors,
            }
        )
    return reports


def extract_session_names(sessions: Any) -> list[str]:
    result = []
    if not isinstance(sessions, list):
        return result
    for item in sessions:
        if isinstance(item, dict):
            name = item.get("name") or item.get("chat_name") or item.get("title")
        else:
            name = str(item)
        if name:
            result.append(str(name))
    return unique_list(result)


def compact_wechat_report(wechat: dict[str, Any]) -> dict[str, Any]:
    status = wechat.get("status") or {}
    sessions_payload = wechat.get("sessions") or {}
    session_names = extract_session_names(sessions_payload.get("sessions", []))
    return {
        "skipped": wechat.get("skipped", False),
        "sidecar_python": wechat.get("sidecar_python"),
        "sidecar_script": wechat.get("sidecar_script"),
        "compat_sidecar_script": wechat.get("compat_sidecar_script"),
        "wxauto4_reserve_enabled": bool(wechat.get("wxauto4_reserve_enabled")),
        "adapter": status.get("adapter"),
        "compat_reason": status.get("compat_reason"),
        "online": bool(status.get("online")),
        "login_window_exists": status.get("login_window_exists"),
        "my_info": status.get("my_info"),
        "recent_session_count": len(session_names),
        "recent_sessions": session_names[:30],
    }


def recommended_next_steps(
    passed: bool,
    warnings: list[str],
    target_reports: list[dict[str, Any]],
    config_path: Path,
) -> list[str]:
    if not passed:
        return ["Fix preflight errors before running the customer-service workflow."]
    steps = []
    if warnings:
        steps.append("Review warnings before enabling any real-contact send mode.")
    if any(report.get("enabled_in_config") for report in target_reports):
        steps.append(
            "Bootstrap newly enabled targets before dry-run: "
            f"uv run python apps/wechat_ai_customer_service/workflows/listen_and_reply.py --config {config_path} --once --bootstrap"
        )
        steps.append(
            "Then run dry-run only: "
            f"uv run python apps/wechat_ai_customer_service/workflows/listen_and_reply.py --config {config_path} --once"
        )
    steps.append("Keep --send disabled until dry-run output and review queue are clean.")
    return steps


def unique_list(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def print_human(payload: dict[str, Any]) -> None:
    lines = [
        "WeChat customer-service preflight",
        f"ok: {payload.get('ok')}",
        f"config: {payload.get('config_path')}",
        "",
        "Checks:",
    ]
    for check in payload.get("checks", []):
        lines.append(f"  OK - {check}")
    if payload.get("warnings"):
        lines.append("")
        lines.append("Warnings:")
        for warning in payload.get("warnings", []):
            lines.append(f"  WARN - {warning}")
    if payload.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for error in payload.get("errors", []):
            lines.append(f"  ERR - {error}")

    wechat = payload.get("wechat", {}) or {}
    lines.extend(
        [
            "",
            "WeChat:",
            f"  online: {wechat.get('online')}",
            f"  login_window_exists: {wechat.get('login_window_exists')}",
            f"  my_info: {wechat.get('my_info')}",
            f"  recent_session_count: {wechat.get('recent_session_count')}",
        ]
    )
    sessions = wechat.get("recent_sessions") or []
    if sessions:
        lines.append("  recent_sessions:")
        for name in sessions[:10]:
            lines.append(f"    - {name}")

    lines.append("")
    lines.append("Targets:")
    for report in payload.get("target_reports", []):
        lines.append(
            f"  - {report.get('target')}: "
            f"enabled={report.get('enabled_in_config')}, "
            f"recent_exact={report.get('exact_recent_session')}"
        )
        for warning in report.get("warnings", []):
            lines.append(f"    WARN - {warning}")

    counts = payload.get("review_queue_counts", {}) or {}
    lines.extend(
        [
            "",
            "Review queue:",
            (
                f"  open_pending={counts.get('open_pending_customer_data', 0)}, "
                f"handoff={counts.get('handoff', 0)}, "
                f"audit_attention={counts.get('audit_attention', 0)}"
            ),
            "",
            "Next:",
        ]
    )
    for step in payload.get("recommended_next_steps", []):
        lines.append(f"  - {step}")

    text = "\n".join(lines).rstrip() + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
