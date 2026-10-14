"""Read-only Windows instance inspection. Never launch, focus or kill WeChat."""
from __future__ import annotations

import hashlib
import math
import ntpath
import struct
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .hook_config import HookDeployment


class HookProcessError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _same_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def process_generation(pid: int, started_at: float, exe: str) -> str:
    if (type(pid) is not int or pid <= 0 or type(started_at) not in {int, float}
            or not math.isfinite(started_at) or started_at <= 0):
        raise HookProcessError("HOOK_PROCESS_IDENTITY_INVALID")
    # PID alone is reusable. Keep the same identity across a Worker restart.
    value = f"{pid}:{float(started_at).hex()}:{ntpath.normcase(ntpath.normpath(exe))}"
    return str(uuid.uuid5(uuid.UUID("840e2da0-45df-440b-930c-882aef2e0f31"), value))


def pe_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise HookProcessError("HOOK_COMPONENT_PE_INVALID")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise HookProcessError("HOOK_COMPONENT_PE_INVALID")
        stream.seek(offset)
        signature = stream.read(6)
        if len(signature) != 6 or signature[:4] != b"PE\x00\x00":
            raise HookProcessError("HOOK_COMPONENT_PE_INVALID")
        return struct.unpack_from("<H", signature, 4)[0]


@dataclass(frozen=True)
class HookProcessSnapshot:
    pid: int
    started_at: float
    exe: str
    file_version: str
    exe_machine: int
    dll_machine: int
    dll_sha256: str
    dll_loaded: bool
    command_arguments: tuple[str, ...]
    listener_owners: tuple[int | None, ...]
    visible_window_count: int
    minimized: bool

    @property
    def generation(self) -> str:
        return process_generation(self.pid, self.started_at, self.exe)

    def validate(self, deployment: HookDeployment) -> None:
        _ = self.generation
        if not _same_path(self.exe, deployment.wechat_exe):
            raise HookProcessError("HOOK_PROCESS_PATH_MISMATCH")
        if self.file_version != "3.9.10.16" or self.exe_machine != 0x14C or self.dll_machine != 0x14C:
            raise HookProcessError("HOOK_COMPONENT_VERSION_MISMATCH")
        if self.dll_sha256.lower() != deployment.dll_sha256.lower() or not self.dll_loaded:
            raise HookProcessError("HOOK_COMPONENT_HASH_OR_LOAD_MISMATCH")
        arguments = self.command_arguments
        if (len(arguments) != len(deployment.launch_arguments)
                or set(arguments) != set(deployment.launch_arguments)):
            raise HookProcessError("HOOK_PROCESS_ARGUMENTS_MISMATCH")
        if not self.listener_owners or any(owner != self.pid for owner in self.listener_owners):
            raise HookProcessError("HOOK_API_PORT_OWNER_MISMATCH")

    @property
    def window_available_for_actions(self) -> bool:
        # The foreground window can be Worker or another app; focus is irrelevant.
        return self.visible_window_count == 1 and not self.minimized


def _wechat_process_ids(kernel32=None) -> tuple[int, ...]:
    """Enumerate names in one OS snapshot, then inspect only matching processes.

    Unlike psutil.process_iter, Toolhelp does not construct a Process (and read
    its creation time) for every unrelated process. No cached identity is used.
    A failed/incomplete enumeration is an inspection failure, never an empty list.
    """
    import ctypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260),
        ]

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    snapshot.restype = ctypes.c_void_p
    first, next_entry = kernel32.Process32FirstW, kernel32.Process32NextW
    for function in (first, next_entry):
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        function.restype = ctypes.c_int32
    close = kernel32.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int32
    error = kernel32.GetLastError
    error.argtypes, error.restype = [], ctypes.c_uint32
    handle = snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if handle in (None, ctypes.c_void_p(-1).value):
        raise HookProcessError("HOOK_PROCESS_INSPECTION_FAILED")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = []
        has_entry = first(handle, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.lower() == "wechat.exe":
                found.append(int(entry.th32ProcessID))
            has_entry = next_entry(handle, ctypes.byref(entry))
        if error() != 18:  # ERROR_NO_MORE_FILES is the only successful terminator.
            raise HookProcessError("HOOK_PROCESS_INSPECTION_FAILED")
        return tuple(found)
    finally:
        close(handle)


def inspect_windows_process(deployment: HookDeployment) -> HookProcessSnapshot:
    """Permission errors and ambiguous processes stop inspection, never guess."""
    if sys.platform != "win32":
        raise HookProcessError("HOOK_WINDOWS_REQUIRED")
    import psutil
    import win32api
    import win32gui
    import win32process
    import pywintypes

    try:
        candidates = []
        for pid in _wechat_process_ids():
            process = psutil.Process(pid)
            if _same_path(process.exe(), deployment.wechat_exe):
                candidates.append(process)
        if len(candidates) != 1:
            raise HookProcessError("HOOK_PROCESS_MISSING_OR_AMBIGUOUS")
        process = candidates[0]
        started_at, exe = process.create_time(), process.exe()
        version = win32api.GetFileVersionInfo(exe, "\\")
        ms, ls = version["FileVersionMS"], version["FileVersionLS"]
        file_version = f"{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}"
        dll = Path(deployment.dll_path)
        with dll.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        loaded = any(_same_path(item.path, str(dll)) for item in process.memory_maps())
        owners = tuple(connection.pid for connection in psutil.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN and connection.laddr.port == deployment.hook_api_port)
        windows = []

        def collect(hwnd, _):
            if (win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid
                    and win32gui.GetClassName(hwnd) == "WeChatMainWndForPC"):
                windows.append(hwnd)

        win32gui.EnumWindows(collect, None)
        result = HookProcessSnapshot(process.pid, started_at, exe, file_version,
            pe_machine(Path(exe)), pe_machine(dll), digest, loaded, tuple(process.cmdline()[1:]),
            owners, len(windows), any(win32gui.IsIconic(hwnd) for hwnd in windows))
        if (process.create_time() != started_at or not _same_path(process.exe(), exe)
                or not process.is_running()):
            raise HookProcessError("HOOK_PROCESS_CHANGED_DURING_INSPECTION")
        result.validate(deployment)
        return result
    except HookProcessError:
        raise
    except (psutil.Error, pywintypes.error, OSError, ValueError, KeyError, struct.error):
        raise HookProcessError("HOOK_PROCESS_INSPECTION_FAILED") from None



def inspect_windows_generation(expected: HookProcessSnapshot) -> None:
    """Cheap live PID-reuse check inside an already fully inspected read lease."""
    if sys.platform != "win32":
        raise HookProcessError("HOOK_WINDOWS_REQUIRED")
    import psutil
    try:
        process = psutil.Process(expected.pid)
        if (process.create_time() != expected.started_at
                or not _same_path(process.exe(), expected.exe) or not process.is_running()):
            raise HookProcessError("HOOK_PROCESS_GENERATION_CHANGED")
    except psutil.Error:
        raise HookProcessError("HOOK_PROCESS_GENERATION_CHANGED") from None


def inspect_windows_window_geometry(process: HookProcessSnapshot) -> dict | None:
    """Read the approved instance's physical rectangle without focus or capture.

    This is optional GUI placement metadata, not a new business/login gate.
    A changed process, hidden/minimized/ambiguous window or Win32 failure leaves
    the Worker at its existing position. The caller runs on the heartbeat thread.
    """
    if sys.platform != 'win32' or not process.window_available_for_actions:
        return None
    try:
        import ctypes
        import psutil
        import win32gui
        import win32process
        return _approved_window_geometry(process, psutil.Process(process.pid),
            win32gui, win32process, ctypes.windll.user32)
    except Exception:
        return None  # Presentation metadata must not interrupt facts or tasks.


def _approved_window_geometry(process, live, gui, processes, user32):
    import ctypes
    def same_process():
        return (live.is_running() and live.create_time() == process.started_at
                and _same_path(live.exe(), process.exe))
    if not same_process():
        return None
    dpi = user32.SetThreadDpiAwarenessContext
    dpi.argtypes = [ctypes.c_void_p]
    dpi.restype = ctypes.c_void_p
    previous = dpi(ctypes.c_void_p(-4))
    if not previous:
        return None
    try:
        windows = []
        def collect(hwnd, _):
            if (gui.IsWindowVisible(hwnd) and not gui.IsIconic(hwnd)
                    and gui.GetClassName(hwnd) == 'WeChatMainWndForPC'
                    and processes.GetWindowThreadProcessId(hwnd)[1] == process.pid):
                windows.append(hwnd)
        gui.EnumWindows(collect, None)
        if len(windows) != 1:
            return None
        left, top, right, bottom = gui.GetWindowRect(windows[0])
        if right <= left or bottom <= top or not same_process():
            return None
        return {'ok': True, 'geometry': {'left': left, 'top': top,
            'right': right, 'bottom': bottom, 'width': right-left, 'height': bottom-top}}
    finally:
        dpi(ctypes.c_void_p(previous))
