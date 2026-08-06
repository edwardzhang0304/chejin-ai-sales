from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chejin_worker_client.main import main


def _restore_frozen_worker_stdio() -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    if len(sys.argv) < 2 or sys.argv[1] not in {
        "--omniauto-ocr-worker",
        "--vision-provider-worker",
    }:
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


def _write_packaging_diagnostic(exc: BaseException) -> None:
    path_value = os.environ.get("CHEJIN_PACKAGING_DIAGNOSTIC_PATH")
    if not path_value:
        return
    payload = {
        "pid": os.getpid(),
        "argv": list(sys.argv),
        "exception_type": type(exc).__name__,
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    try:
        with Path(path_value).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _restore_frozen_worker_stdio()
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        _write_packaging_diagnostic(exc)
        raise
