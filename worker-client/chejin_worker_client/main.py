from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
import sys

from .preflight import format_json, format_text, has_blocking_failures, run_preflight, write_report
from .single_instance import (
    SingleInstanceAlreadyRunning,
    acquire_single_instance,
    notify_already_running,
)


def emit_cli_output(value: str) -> None:
    """Windowed PyInstaller builds may intentionally have no stdout stream."""

    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    stream.write(str(value) + "\n")
    stream.flush()


def bootstrap_qt_plugins() -> None:
    if os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
        return
    try:
        import PySide6

        plugins = Path(PySide6.__file__).resolve().parent / "plugins"
        platforms = plugins / "platforms"
        if (platforms / "qwindows.dll").exists():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plugins))
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms))
            os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    except Exception:
        return


def run_bundled_omniauto_sidecar(argv: list[str]) -> int:
    """Dispatch the packaged sidecar inside the same frozen executable."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        raise RuntimeError("bundled_omniauto_sidecar_requires_frozen_runtime")
    omniauto_root = Path(frozen_root) / "omniauto-rpa"
    if str(omniauto_root) not in sys.path:
        sys.path.insert(0, str(omniauto_root))
    from apps.wechat_ai_customer_service.adapters import (
        wechat_win32_ocr_sidecar,
    )

    if "--daemon" in argv:
        return int(wechat_win32_ocr_sidecar.run_daemon_loop())
    try:
        payload = wechat_win32_ocr_sidecar.run_sidecar_cli(argv)
    except Exception as exc:
        payload = wechat_win32_ocr_sidecar.exception_payload_for_sidecar(
            exc,
            state="win32_ocr_failed",
        )
    emit_cli_output(json.dumps(payload, ensure_ascii=True))
    return 0 if bool(payload.get("ok")) else 1


def run_bundled_vision_provider_worker() -> int:
    """Run the killable, stdin/stdout-only Vision provider child."""

    from .vision_provider_worker import main as provider_main

    return int(provider_main())


def run_bundled_omniauto_ocr_worker() -> int:
    """Run OmniAuto OCR outside the Qt GUI process."""

    from .omniauto_ocr_worker import main as ocr_main

    return int(ocr_main())


def run_bundled_omniauto_ocr_probe() -> int:
    from .omniauto_ocr_client import probe_omniauto_ocr_subprocess

    result = probe_omniauto_ocr_subprocess()
    diagnostic_path = os.environ.get("CHEJIN_PACKAGING_DIAGNOSTIC_PATH")
    if diagnostic_path:
        try:
            with Path(diagnostic_path).open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "argv": list(sys.argv),
                            "ocr_probe_result": result,
                        },
                        ensure_ascii=True,
                    )
                    + "\n"
                )
        except Exception:
            pass
    emit_cli_output(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") is True else 1


def _bundled_omniauto_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        raise RuntimeError("bundled_omniauto_worker_requires_frozen_runtime")
    omniauto_root = Path(frozen_root) / "omniauto-rpa"
    if str(omniauto_root) not in sys.path:
        sys.path.insert(0, str(omniauto_root))
    return omniauto_root


def run_bundled_omniauto_vision_wechat_worker(argv: list[str]) -> int:
    """Run the Vision-owned WeChat desktop worker in the frozen executable."""

    _bundled_omniauto_root()
    from apps.wechat_ai_customer_service.optional_plugins.vision.integrations import (
        wechat_worker,
    )

    return int(wechat_worker.main(argv))


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--omniauto-sidecar":
        return run_bundled_omniauto_sidecar(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "--vision-provider-worker":
        return run_bundled_vision_provider_worker()
    if len(sys.argv) >= 2 and sys.argv[1] == "--omniauto-ocr-worker":
        return run_bundled_omniauto_ocr_worker()
    if len(sys.argv) >= 2 and sys.argv[1] == "--omniauto-ocr-probe":
        return run_bundled_omniauto_ocr_probe()
    if len(sys.argv) >= 2 and sys.argv[1] == "--omniauto-vision-wechat-worker":
        return run_bundled_omniauto_vision_wechat_worker(sys.argv[2:])

    parser = argparse.ArgumentParser(prog="chejin-worker-client")
    parser.add_argument("--preflight", action="store_true", help="运行客户端环境预检后退出。")
    parser.add_argument("--preflight-format", choices=["text", "json"], default="text", help="预检输出格式。")
    parser.add_argument("--skip-backend", action="store_true", help="预检时跳过后端 readyz 检查。")
    parser.add_argument("--skip-wechat", action="store_true", help="预检时跳过微信桌面客户端探测。")
    parser.add_argument("--write-report", type=Path, default=None, help="把预检 JSON 报告写入指定路径。")
    parser.add_argument("--wechat-diagnostics", action="store_true", help="采集微信窗口诊断信息后退出。")
    parser.add_argument("--post-update-plan", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--post-rollback-plan", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--post-update-token", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.post_update_plan is not None:
        from .update_diagnostics import record_update_startup_failure

        if not args.post_update_token:
            record_update_startup_failure(
                args.post_update_plan, phase="post_update_verification",
                exc=RuntimeError("UPDATE_STARTUP_TOKEN_MISSING"), exit_code=3,
            )
            return 3
        try:
            from .post_update_health import verify_post_update_startup
            from .update_startup_context import set_update_startup_context

            plan = verify_post_update_startup(
                args.post_update_plan.resolve(strict=True),
                str(args.post_update_token),
            )
            set_update_startup_context(
                {"mode": "updated", "plan": plan, "token": str(args.post_update_token)}
            )
        except Exception as exc:
            record_update_startup_failure(
                args.post_update_plan, phase="post_update_verification", exc=exc, exit_code=3,
            )
            return 3
    elif args.post_rollback_plan is not None:
        from .update_startup_context import set_update_startup_context

        set_update_startup_context(
            {
                "mode": "rolled_back",
                "plan_path": str(args.post_rollback_plan.resolve(strict=False)),
            }
        )

    if args.wechat_diagnostics:
        from .rpa_bridge import RpaBridge

        emit_cli_output(
            json.dumps(RpaBridge().diagnose_wechat(), ensure_ascii=False, indent=2)
        )
        return 0

    if args.preflight:
        checks = run_preflight(check_backend=not args.skip_backend, check_wechat=not args.skip_wechat)
        if args.write_report is not None:
            write_report(checks, args.write_report)
        emit_cli_output(
            format_json(checks)
            if args.preflight_format == "json"
            else format_text(checks)
        )
        return 1 if has_blocking_failures(checks) else 0

    try:
        instance_guard = acquire_single_instance()
    except SingleInstanceAlreadyRunning:
        if args.post_update_plan is not None:
            record_update_startup_failure(
                args.post_update_plan, phase="single_instance",
                exc=RuntimeError("UPDATE_STARTUP_INSTANCE_ALREADY_RUNNING"), exit_code=2,
            )
        notify_already_running()
        return 2

    bootstrap_qt_plugins()
    from .runtime_supervision import (
        install_runtime_supervision,
        mark_runtime_clean_exit,
    )

    install_runtime_supervision()
    try:
        if os.environ.get("CHEJIN_WORKER_UI_MODE") == "pyside":
            from .ui import run_app
        else:
            from .web_ui import run_app

        exit_code = run_app()
        mark_runtime_clean_exit(exit_code)
        return exit_code
    finally:
        instance_guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
