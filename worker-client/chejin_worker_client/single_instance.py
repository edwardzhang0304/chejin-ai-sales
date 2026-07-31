from __future__ import annotations

import ctypes
from dataclasses import dataclass
import sys
from typing import Any, Callable


WINDOWS_ALREADY_EXISTS = 183
WINDOWS_MUTEX_NAME = "Local\\ChejinWorkerClient.SingleInstance"


class SingleInstanceAlreadyRunning(RuntimeError):
    pass


@dataclass
class SingleInstanceGuard:
    handle: int | None = None
    kernel32: Any | None = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.handle and self.kernel32 is not None:
            self.kernel32.CloseHandle(self.handle)


def _load_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


def acquire_single_instance(
    *,
    platform_name: str | None = None,
    kernel32: Any | None = None,
    get_last_error: Callable[[], int] | None = None,
    set_last_error: Callable[[int], None] | None = None,
) -> SingleInstanceGuard:
    platform = platform_name or sys.platform
    if platform != "win32":
        return SingleInstanceGuard()
    api = kernel32 or _load_kernel32()
    read_last_error = get_last_error or ctypes.get_last_error
    reset_last_error = set_last_error or ctypes.set_last_error
    reset_last_error(0)
    handle = api.CreateMutexW(None, False, WINDOWS_MUTEX_NAME)
    error_code = int(read_last_error())
    if not handle:
        raise OSError(error_code, "WINDOWS_SINGLE_INSTANCE_MUTEX_CREATE_FAILED")
    if error_code == WINDOWS_ALREADY_EXISTS:
        api.CloseHandle(handle)
        raise SingleInstanceAlreadyRunning("CHEJIN_WORKER_ALREADY_RUNNING")
    return SingleInstanceGuard(handle=int(handle), kernel32=api)


def notify_already_running() -> None:
    message = "车金 Worker 客户端已在运行，请先关闭已打开的客户端。"
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "车金 Worker 客户端", 0x30)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)
