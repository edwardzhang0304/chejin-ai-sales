from __future__ import annotations

import argparse
import os
import json
from pathlib import Path

from .preflight import format_json, format_text, has_blocking_failures, run_preflight, write_report


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


def main() -> int:
    parser = argparse.ArgumentParser(prog="chejin-worker-client")
    parser.add_argument("--preflight", action="store_true", help="运行客户端环境预检后退出。")
    parser.add_argument("--preflight-format", choices=["text", "json"], default="text", help="预检输出格式。")
    parser.add_argument("--skip-backend", action="store_true", help="预检时跳过后端 readyz 检查。")
    parser.add_argument("--skip-wechat", action="store_true", help="预检时跳过微信桌面客户端探测。")
    parser.add_argument("--write-report", type=Path, default=None, help="把预检 JSON 报告写入指定路径。")
    parser.add_argument("--wechat-diagnostics", action="store_true", help="采集微信窗口诊断信息后退出。")
    args = parser.parse_args()

    if args.wechat_diagnostics:
        from .rpa_bridge import RpaBridge

        print(json.dumps(RpaBridge().diagnose_wechat(), ensure_ascii=False, indent=2))
        return 0

    if args.preflight:
        checks = run_preflight(check_backend=not args.skip_backend, check_wechat=not args.skip_wechat)
        if args.write_report is not None:
            write_report(checks, args.write_report)
        print(format_json(checks) if args.preflight_format == "json" else format_text(checks))
        return 1 if has_blocking_failures(checks) else 0

    bootstrap_qt_plugins()
    if os.environ.get("CHEJIN_WORKER_UI_MODE") == "pyside":
        from .ui import run_app
    else:
        from .web_ui import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
