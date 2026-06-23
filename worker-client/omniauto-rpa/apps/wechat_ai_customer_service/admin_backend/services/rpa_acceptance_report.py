"""RPA acceptance snapshot and gate evaluation helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import psutil

from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnector
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import CustomerServiceRuntime
from apps.wechat_ai_customer_service.admin_backend.services.recorder_runtime import RecorderRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service"

RUNNING_STATES = {"idle", "thinking", "paused"}
STOPPED_STATES = {"stopped", ""}
RISK_CAPABILITY_STATES = {
    "blank_render_detected",
    "login_window_detected",
    "main_window_geometry_invalid",
    "main_window_not_found",
    "no_supported_wechat_transport",
    "wechat_not_online",
    "auxiliary_shell_window_detected",
}


def collect_rpa_acceptance_report(
    *,
    tenant_id: str = "chejin",
    runtime_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    wechat_probe: str = "none",
    connector: WeChatConnector | None = None,
    require_guard_when_running: bool = True,
    allow_clipboard_once: bool = False,
) -> dict[str, Any]:
    """Collect a low-risk RPA acceptance snapshot.

    ``wechat_probe`` supports ``none``, ``passive`` and ``interactive``. The
    passive probe is screenshot/OCR based and must not send messages.
    """

    active_env = dict(os.environ if env is None else env)
    root = Path(runtime_root or DEFAULT_RUNTIME_ROOT)
    tenant_root = root / "tenants" / tenant_id
    checks: list[dict[str, Any]] = []

    customer_config = read_json(tenant_root / "customer_service" / "listener_config.json")
    recorder_settings = read_json(tenant_root / "recorder" / "settings.json")
    customer_status = read_effective_runtime_status(
        root,
        tenant_id=tenant_id,
        label="customer_service",
        raw_path=tenant_root / "customer_service" / "runtime_status.json",
    )
    customer_guard = read_guard_runtime(tenant_root / "customer_service")
    recorder_status = read_effective_runtime_status(
        root,
        tenant_id=tenant_id,
        label="recorder",
        raw_path=tenant_root / "recorder" / "runtime_status.json",
    )
    recorder_guard = read_guard_runtime(tenant_root / "recorder")
    send_guard_state = read_json(PROJECT_ROOT / "runtime" / "wechat_win32_ocr_send_guard.json")

    capability: dict[str, Any] = {}
    if wechat_probe not in {"none", "passive", "interactive"}:
        raise ValueError("wechat_probe must be one of: none, passive, interactive")
    if wechat_probe != "none":
        capability = probe_wechat_capability(
            connector or WeChatConnector(),
            mode=wechat_probe,
        )

    add_rpa_only_checks(checks, active_env, capability)
    add_wechat_capability_checks(checks, capability, probe_mode=wechat_probe)
    add_window_normalization_policy_checks(checks, active_env)
    add_humanized_send_checks(checks, customer_config, active_env, allow_clipboard_once=allow_clipboard_once)
    operator_guard_config = effective_operator_guard_config(customer_config, recorder_settings)
    add_operator_guard_config_checks(checks, operator_guard_config)
    add_runtime_guard_checks(
        checks,
        label="customer_service",
        status_payload=customer_status,
        guard_payload=customer_guard,
        require_guard_when_running=require_guard_when_running,
    )
    add_runtime_guard_checks(
        checks,
        label="recorder",
        status_payload=recorder_status,
        guard_payload=recorder_guard,
        require_guard_when_running=require_guard_when_running,
    )
    add_rate_guard_checks(checks, active_env, send_guard_state)

    summary = summarize_checks(checks)
    return {
        "ok": summary["status"] == "pass",
        "status": summary["status"],
        "summary": summary,
        "tenant_id": tenant_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "wechat_probe": wechat_probe,
        "paths": {
            "runtime_root": str(root),
            "tenant_root": str(tenant_root),
        },
        "snapshots": {
            "wechat_capability": capability,
            "customer_service_status": customer_status,
            "customer_service_operator_guard": customer_guard,
            "recorder_status": recorder_status,
            "recorder_operator_guard": recorder_guard,
            "send_guard_state": compact_send_guard_state(send_guard_state),
            "listener_config": compact_listener_config(customer_config),
            "recorder_settings": compact_recorder_settings(recorder_settings),
            "operator_guard_config": operator_guard_config,
        },
        "checks": checks,
    }


def probe_wechat_capability(connector: WeChatConnector, *, mode: str) -> dict[str, Any]:
    previous = os.environ.get("WECHAT_WIN32_OCR_PASSIVE_PROBE")
    if mode == "passive":
        os.environ["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = "1"
    try:
        return connector.capabilities(interactive=(mode == "interactive"))
    except Exception as exc:  # noqa: BLE001 - acceptance reports should capture failures.
        return {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "state": "capability_probe_failed",
            "error": repr(exc),
        }
    finally:
        if mode == "passive":
            if previous is None:
                os.environ.pop("WECHAT_WIN32_OCR_PASSIVE_PROBE", None)
            else:
                os.environ["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = previous


def add_rpa_only_checks(checks: list[dict[str, Any]], env: Mapping[str, str], capability: dict[str, Any]) -> None:
    reserve_env_enabled = env_flag(env.get("WECHAT_ENABLE_WXAUTO4"), default=False)
    adapter = str(capability.get("adapter") or "")
    scheme = str(capability.get("scheme") or "")
    reserve_enabled = bool(capability.get("wxauto4_reserve_enabled"))
    reserve_status = capability.get("reserve_status") if isinstance(capability.get("reserve_status"), dict) else {}
    if reserve_env_enabled or adapter == "wxauto4" or scheme == "wxauto4" or reserve_enabled:
        add_check(
            checks,
            "rpa_only_transport",
            "fail",
            "wxauto4 reserve path is active or selected; RPA-only acceptance requires it to stay disabled.",
            {
                "WECHAT_ENABLE_WXAUTO4": env.get("WECHAT_ENABLE_WXAUTO4", ""),
                "adapter": adapter,
                "scheme": scheme,
                "wxauto4_reserve_enabled": reserve_enabled,
                "reserve_status": reserve_status,
            },
        )
        return
    add_check(
        checks,
        "rpa_only_transport",
        "pass",
        "wxauto4 reserve is disabled and no capability selected wxauto4.",
        {
            "WECHAT_ENABLE_WXAUTO4": env.get("WECHAT_ENABLE_WXAUTO4", ""),
            "adapter": adapter,
            "scheme": scheme,
        },
    )


def add_wechat_capability_checks(checks: list[dict[str, Any]], capability: dict[str, Any], *, probe_mode: str) -> None:
    if probe_mode == "none":
        add_check(checks, "wechat_capability_probe", "skip", "WeChat live probe was not requested.", {})
        return
    state = str(capability.get("state") or "")
    adapter = str(capability.get("adapter") or "")
    online = bool(capability.get("online"))
    receive = capability.get("receive") if isinstance(capability.get("receive"), dict) else {}
    send = capability.get("send") if isinstance(capability.get("send"), dict) else {}
    if state in RISK_CAPABILITY_STATES or not online:
        add_check(
            checks,
            "wechat_capability_probe",
            "fail",
            "WeChat capability probe detected an unsafe or offline state.",
            {"state": state, "adapter": adapter, "online": online, "reason": capability.get("reason"), "error": capability.get("error")},
        )
        return
    if adapter != "win32_ocr":
        add_check(
            checks,
            "wechat_capability_probe",
            "fail",
            "WeChat capability is not using the RPA Win32/OCR adapter.",
            {"state": state, "adapter": adapter, "online": online},
        )
        return
    if not receive.get("ok") or not send.get("ok"):
        add_check(
            checks,
            "wechat_capability_probe",
            "warn",
            "RPA adapter is online, but receive/send capability is not fully ready.",
            {"state": state, "adapter": adapter, "receive": receive, "send": send},
        )
        return
    add_check(
        checks,
        "wechat_capability_probe",
        "pass",
        "WeChat is online through Win32/OCR RPA with receive and send capability.",
        {"state": state, "adapter": adapter, "scheme": capability.get("scheme")},
    )


def add_humanized_send_checks(
    checks: list[dict[str, Any]],
    listener_config: dict[str, Any],
    env: Mapping[str, str],
    *,
    allow_clipboard_once: bool,
) -> None:
    config = listener_config.get("rpa_humanized_send") if isinstance(listener_config.get("rpa_humanized_send"), dict) else {}
    enabled = bool(config.get("enabled", True))
    method = str(config.get("input_method") or env.get("WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD") or "sendinput_unicode")
    rate_guard_enabled = env_flag(env.get("WECHAT_WIN32_OCR_SEND_RATE_GUARD"), default=True)
    if not enabled:
        add_check(checks, "humanized_send_policy", "fail", "Humanized RPA typing is disabled.", {"config": config})
        return
    if method == "clipboard_once" and not allow_clipboard_once:
        add_check(
            checks,
            "humanized_send_policy",
            "fail",
            "clipboard_once is only allowed for explicit burst-test mode, not normal RPA acceptance.",
            {"input_method": method, "allow_clipboard_once": allow_clipboard_once},
        )
        return
    if not rate_guard_enabled:
        add_check(checks, "humanized_send_policy", "fail", "RPA send rate guard is disabled.", {"WECHAT_WIN32_OCR_SEND_RATE_GUARD": env.get("WECHAT_WIN32_OCR_SEND_RATE_GUARD", "")})
        return
    add_check(
        checks,
        "humanized_send_policy",
        "pass",
        "Humanized typing and send-rate guard are enabled.",
        {
            "input_method": method,
            "adaptive_speed_enabled": bool(config.get("adaptive_speed_enabled", True)),
            "fast_send_confirmation_enabled": bool(config.get("fast_send_confirmation_enabled", False)),
        },
    )


def add_window_normalization_policy_checks(checks: list[dict[str, Any]], env: Mapping[str, str]) -> None:
    normalize_enabled = env_flag(env.get("WECHAT_WIN32_OCR_WINDOW_NORMALIZE"), default=True)
    fixed_origin = env_flag(env.get("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN"), default=True)
    if not normalize_enabled:
        add_check(checks, "window_normalization_policy", "fail", "WeChat window normalization is disabled.", {"WECHAT_WIN32_OCR_WINDOW_NORMALIZE": env.get("WECHAT_WIN32_OCR_WINDOW_NORMALIZE", "")})
        return
    if not fixed_origin:
        add_check(checks, "window_normalization_policy", "fail", "WeChat window fixed-origin normalization is disabled.", {"WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN": env.get("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", "")})
        return
    add_check(
        checks,
        "window_normalization_policy",
        "pass",
        "WeChat window normalization uses a fixed safe origin before RPA operations.",
        {
            "WECHAT_WIN32_OCR_WINDOW_WIDTH": env.get("WECHAT_WIN32_OCR_WINDOW_WIDTH", ""),
            "WECHAT_WIN32_OCR_WINDOW_HEIGHT": env.get("WECHAT_WIN32_OCR_WINDOW_HEIGHT", ""),
            "WECHAT_WIN32_OCR_WINDOW_LEFT": env.get("WECHAT_WIN32_OCR_WINDOW_LEFT", ""),
            "WECHAT_WIN32_OCR_WINDOW_TOP": env.get("WECHAT_WIN32_OCR_WINDOW_TOP", ""),
        },
    )


def add_operator_guard_config_checks(checks: list[dict[str, Any]], config: dict[str, Any]) -> None:
    failures = []
    if not bool(config.get("enabled", False)):
        failures.append("disabled")
    if not bool(config.get("block_manual_input", False)):
        failures.append("manual_input_not_blocked")
    if not bool(config.get("floating_indicator_enabled", False)):
        failures.append("floating_indicator_disabled")
    if str(config.get("control_hotkey") or "").lower() != "f8":
        failures.append("control_hotkey_not_f8")
    if failures:
        add_check(checks, "operator_guard_config", "fail", "RPA operator guard configuration is not acceptance-ready.", {"failures": failures, "config": config})
        return
    add_check(checks, "operator_guard_config", "pass", "Operator guard blocks manual input, shows floating indicator, and uses F8.", {"config": config})


def effective_operator_guard_config(listener_config: dict[str, Any], recorder_settings: dict[str, Any]) -> dict[str, Any]:
    listener_guard = listener_config.get("rpa_operator_guard") if isinstance(listener_config.get("rpa_operator_guard"), dict) else {}
    if listener_guard:
        return {**listener_guard, "_source": "customer_service.listener_config"}
    recorder_guard = recorder_settings.get("rpa_operator_guard") if isinstance(recorder_settings.get("rpa_operator_guard"), dict) else {}
    if recorder_settings:
        return {
            "enabled": True,
            "block_manual_input": True,
            "floating_indicator_enabled": True,
            "control_hotkey": "f8",
            **recorder_guard,
            "_source": "recorder.effective_defaults",
        }
    return {}


def add_runtime_guard_checks(
    checks: list[dict[str, Any]],
    *,
    label: str,
    status_payload: dict[str, Any],
    guard_payload: dict[str, Any],
    require_guard_when_running: bool,
) -> None:
    state = str(status_payload.get("state") or "")
    running = bool(status_payload.get("running")) or state in RUNNING_STATES
    if state in STOPPED_STATES and not status_payload.get("running"):
        add_check(checks, f"{label}_runtime_guard", "pass", f"{label} is stopped; no active guard is required.", {"state": state})
        return
    if not running:
        add_check(checks, f"{label}_runtime_guard", "warn", f"{label} runtime state is unknown.", {"state": state, "status": status_payload})
        return
    guard_running = bool(guard_payload.get("running"))
    hooks_installed = bool((guard_payload.get("state") if isinstance(guard_payload.get("state"), dict) else {}).get("hooks_installed"))
    phase = str((guard_payload.get("state") if isinstance(guard_payload.get("state"), dict) else {}).get("phase") or "")
    if require_guard_when_running and (not guard_running or not hooks_installed):
        add_check(
            checks,
            f"{label}_runtime_guard",
            "fail",
            f"{label} appears active but the operator guard is not fully running.",
            {"state": state, "guard": guard_payload},
        )
        return
    if state == "paused" and phase not in {"paused", "pause"}:
        add_check(
            checks,
            f"{label}_runtime_guard",
            "warn",
            f"{label} is paused but guard phase is not paused.",
            {"state": state, "guard_phase": phase},
        )
        return
    add_check(checks, f"{label}_runtime_guard", "pass", f"{label} runtime and operator guard are aligned.", {"state": state, "guard_phase": phase})


def add_rate_guard_checks(checks: list[dict[str, Any]], env: Mapping[str, str], send_guard_state: dict[str, Any]) -> None:
    if env_flag(env.get("WECHAT_WIN32_OCR_SEND_RATE_GUARD"), default=True) is False:
        add_check(checks, "send_rate_guard", "fail", "Send-rate guard is disabled.", {"WECHAT_WIN32_OCR_SEND_RATE_GUARD": env.get("WECHAT_WIN32_OCR_SEND_RATE_GUARD", "")})
        return
    events = send_guard_state.get("events") if isinstance(send_guard_state.get("events"), list) else []
    now_ts = time.time()
    recent = [item for item in events if isinstance(item, dict) and now_ts - float(item.get("at") or 0) <= 600]
    add_check(
        checks,
        "send_rate_guard",
        "pass",
        "Send-rate guard is enabled.",
        {"recent_10m_events": len(recent), "total_events": len(events)},
    )


def read_guard_runtime(runtime_dir: Path) -> dict[str, Any]:
    pid_record = read_json(runtime_dir / "operator_guard.pid.json")
    state = read_json(runtime_dir / "operator_guard.state.json")
    pid = int(pid_record.get("pid") or state.get("pid") or 0)
    running = process_alive(pid)
    return {
        "running": running,
        "pid": pid if running else None,
        "pid_record": pid_record,
        "state": state if running or state else {},
    }


def read_effective_runtime_status(root: Path, *, tenant_id: str, label: str, raw_path: Path) -> dict[str, Any]:
    """Read runtime status with process-aware normalization for the real runtime root.

    The compact ``runtime_status.json`` is intentionally UI-friendly and can be
    left in an active state if a test process exits before writing ``stopped``.
    For acceptance gates we need the effective state, so the default runtime
    root goes through the runtime controller, which cross-checks PID files and
    live processes. Temp fixtures still read the raw JSON to keep offline tests
    deterministic.
    """
    raw_status = read_json(raw_path)
    if not is_default_runtime_root(root):
        return raw_status
    try:
        if label == "customer_service":
            status = CustomerServiceRuntime(tenant_id=tenant_id).status()
        elif label == "recorder":
            status = RecorderRuntime(tenant_id=tenant_id).status()
        else:
            return raw_status
    except Exception as exc:  # noqa: BLE001 - acceptance reports should keep rendering.
        raw_status["_effective_status_error"] = repr(exc)
        return raw_status
    if raw_status:
        status["_raw_status_state"] = raw_status.get("state")
        status["_raw_status_updated_at"] = raw_status.get("updated_at")
    return status


def is_default_runtime_root(root: Path) -> bool:
    try:
        return Path(root).resolve() == DEFAULT_RUNTIME_ROOT.resolve()
    except OSError:
        return False


def process_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
    except Exception:
        return False


def read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:  # noqa: BLE001 - reports should survive corrupted runtime files.
        return {"_read_error": repr(exc), "_path": str(path)}


def add_check(checks: list[dict[str, Any]], check_id: str, status: str, message: str, details: dict[str, Any]) -> None:
    checks.append({"id": check_id, "status": status, "ok": status in {"pass", "skip"}, "message": message, "details": details})


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: sum(1 for item in checks if item.get("status") == status) for status in ("pass", "warn", "fail", "skip")}
    if counts["fail"]:
        status = "fail"
    elif counts["warn"]:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "counts": counts,
        "failures": [item for item in checks if item.get("status") == "fail"],
        "warnings": [item for item in checks if item.get("status") == "warn"],
    }


def compact_listener_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "rpa_humanized_send": config.get("rpa_humanized_send") if isinstance(config.get("rpa_humanized_send"), dict) else {},
        "rpa_operator_guard": config.get("rpa_operator_guard") if isinstance(config.get("rpa_operator_guard"), dict) else {},
        "transport_risk_guard": config.get("transport_risk_guard") if isinstance(config.get("transport_risk_guard"), dict) else {},
        "history_backfill": config.get("history_backfill") if isinstance(config.get("history_backfill"), dict) else {},
        "semantic_batch_planner": config.get("semantic_batch_planner") if isinstance(config.get("semantic_batch_planner"), dict) else {},
        "concurrency_scheduler": config.get("concurrency_scheduler") if isinstance(config.get("concurrency_scheduler"), dict) else {},
    }


def compact_recorder_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": settings.get("enabled"),
        "capture_interval_seconds": settings.get("capture_interval_seconds"),
        "history_backfill_enabled": settings.get("history_backfill_enabled"),
        "rpa_operator_guard": settings.get("rpa_operator_guard") if isinstance(settings.get("rpa_operator_guard"), dict) else {},
    }


def compact_send_guard_state(state: dict[str, Any]) -> dict[str, Any]:
    events = state.get("events") if isinstance(state.get("events"), list) else []
    return {
        "event_count": len(events),
        "latest_event": events[-1] if events else {},
        "updated_at": state.get("updated_at"),
    }


def env_flag(value: str | None, *, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def render_rpa_acceptance_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    lines = [
        "# RPA Acceptance Report",
        "",
        f"- Tenant: {report.get('tenant_id', '')}",
        f"- Created at: {report.get('created_at', '')}",
        f"- Status: {report.get('status', '')}",
        f"- Checks: pass={counts.get('pass', 0)}, warn={counts.get('warn', 0)}, fail={counts.get('fail', 0)}, skip={counts.get('skip', 0)}",
        "",
        "## Checks",
        "",
    ]
    for item in report.get("checks") or []:
        lines.append(f"- [{str(item.get('status') or '').upper()}] {item.get('id', '')}: {item.get('message', '')}")
    lines.append("")
    return "\n".join(lines)
