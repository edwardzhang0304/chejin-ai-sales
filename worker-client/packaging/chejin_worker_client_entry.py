from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
import traceback


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ENTRY_VERSION = "0.9.59"
_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s'\"]+"),
    re.compile(
        r"(?i)((?:worker[_ -]?token|token|api[_ -]?key|password|secret)\s*[:=]\s*)"
        r"[^\s,;'\"]+"
    ),
)


def _restore_frozen_worker_stdio() -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    if len(sys.argv) < 2:
        return
    mode = sys.argv[1]
    supported_modes = {
        "--omniauto-ocr-worker",
        "--vision-provider-worker",
        "--omniauto-vision-wechat-worker",
    }
    if mode == "--omniauto-sidecar" and "--daemon" in sys.argv[2:]:
        supported_modes.add("--omniauto-sidecar")
    if mode not in supported_modes:
        return

    import ctypes
    from ctypes import wintypes
    import io
    import msvcrt

    get_std_handle = ctypes.windll.kernel32.GetStdHandle
    get_std_handle.argtypes = [wintypes.DWORD]
    get_std_handle.restype = wintypes.HANDLE

    def text_stream(std_handle_id: int, flags: int, mode: str):
        handle = get_std_handle(std_handle_id & 0xFFFFFFFF)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid_handle):
            raise OSError(f"windows_standard_handle_unavailable_{std_handle_id}")
        descriptor = msvcrt.open_osfhandle(int(handle), flags)
        binary = os.fdopen(descriptor, mode, buffering=0, closefd=False)
        return io.TextIOWrapper(
            binary,
            encoding="utf-8",
            errors="replace",
            line_buffering="w" in mode,
        )

    sys.stdin = text_stream(-10, os.O_RDONLY, "rb")
    sys.stdout = text_stream(-11, os.O_WRONLY, "wb")
    sys.stderr = text_stream(-12, os.O_WRONLY, "wb")


def _diagnostic_path() -> Path:
    configured = os.environ.get("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", "").strip()
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "CheJinWorker" / "diagnostics" / "startup-crash.jsonl"


def _known_secret_values() -> set[str]:
    values = {
        str(value).strip()
        for key, value in os.environ.items()
        if re.search(r"(?i)(?:token|api[_-]?key|password|secret|cookie)", key)
        and str(value).strip()
        and len(str(value).strip()) >= 6
    }
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        try:
            payload = json.loads(
                (Path(frozen_root) / "vision-runtime.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            api_key = str(payload.get("vision_api_key") or "").strip()
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            api_key = ""
        if api_key:
            values.add(api_key)
    return values


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + _REDACTED, redacted)
    for secret in sorted(_known_secret_values(), key=len, reverse=True):
        redacted = redacted.replace(secret, _REDACTED)
    return redacted


def _load_build_identity() -> dict[str, str]:
    candidates: list[Path] = []
    configured = os.environ.get("CHEJIN_BUILD_IDENTITY_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "runtime-build-identity.json")
    candidates.append(ROOT / "runtime-build-identity.json")

    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        return {
            "version": str(payload.get("version") or ENTRY_VERSION),
            "build_commit": str(payload.get("git_commit") or "unknown"),
        }
    return {"version": ENTRY_VERSION, "build_commit": "unknown"}


def _write_startup_diagnostic(exc: BaseException) -> None:
    identity = _load_build_identity()
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": identity["version"],
        "build_commit": identity["build_commit"],
        "windows_version": platform.platform(),
        "exception_type": type(exc).__name__,
        "traceback": _redact_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        ),
    }
    try:
        path = _diagnostic_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--startup-crash-probe":
        raise RuntimeError("intentional_startup_crash_probe")
    from chejin_worker_client.main import main

    return main


def run() -> int:
    try:
        _restore_frozen_worker_stdio()
        return int(_load_main()())
    except SystemExit:
        raise
    except BaseException as exc:
        _write_startup_diagnostic(exc)
        if getattr(sys, "frozen", False):
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(run())
