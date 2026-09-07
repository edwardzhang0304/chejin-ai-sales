"""Cross-process data ownership during the Worker/Updater handoff.

Normal connections hold a shared OS lock. The updater holds the exclusive
lock until its authenticated child has completed startup. No token is stored.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading
from typing import Any
import psutil
from .single_instance import acquire_single_instance

_PROTOCOL = 2
_permit: tuple[Path, dict[str, Any], str] | None = None
_local = threading.RLock()
_instance_guard = None


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def authenticate(value: Any, token: str) -> str:
    return hmac.new(token.encode(), canonical(value), hashlib.sha256).hexdigest()


def process_identity(pid: int) -> dict[str, Any]:
    try:
        p = psutil.Process(pid)
        return {"pid": pid, "create_time": p.create_time(), "exe": str(Path(p.exe()).resolve())}
    except (psutil.Error, OSError) as exc:
        raise RuntimeError("UPDATE_PROCESS_IDENTITY_UNVERIFIABLE") from exc


def identity_alive(expected: dict[str, Any]) -> bool:
    try:
        p = psutil.Process(int(expected["pid"]))
        if p.status() == psutil.STATUS_ZOMBIE and p.create_time() == expected["create_time"]:
            return False
        actual = process_identity(int(expected["pid"]))
    except (RuntimeError, psutil.Error):
        if not psutil.pid_exists(int(expected["pid"])):
            return False
        raise
    if actual != expected:
        raise RuntimeError("UPDATE_PROCESS_IDENTITY_MISMATCH")
    return True


def lock_path(data_dir: Path) -> Path:
    directory = data_dir.resolve(strict=False)
    # Outside protected evidence directories; never unlink an OS lock inode.
    return directory.parent / ("." + directory.name + ".update-access.lock")


class _WindowsRangeLock:
    def __init__(self, file, exclusive: bool):
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        self.api.LockFileEx.restype = wintypes.BOOL
        self.api.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, ctypes.POINTER(Overlapped)]
        self.api.UnlockFileEx.restype = wintypes.BOOL
        self.handle = msvcrt.get_osfhandle(file.fileno())
        self.overlapped = Overlapped()
        # CRT _locking's read mode is also exclusive. Use actual Win32 shared
        # locks so the normal lifetime guard and SQLite connections can coexist.
        if not self.api.LockFileEx(self.handle, 1 | (2 if exclusive else 0), 0, 1, 0,
                                   ctypes.byref(self.overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        import ctypes
        if not self.api.UnlockFileEx(self.handle, 0, 1, 0, ctypes.byref(self.overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())


class DataAccess:
    def __init__(self, path: Path, exclusive: bool):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.closed = False
        self.instance_guard = None
        self.windows_lock = None
        try:
            if os.name == "nt":
                self.windows_lock = _WindowsRangeLock(self.file, exclusive)
            else:
                import fcntl
                fcntl.flock(self.file, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.closed = True
            raise RuntimeError("UPDATE_DATA_DIRECTORY_BUSY") from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.windows_lock is not None:
                self.windows_lock.close()
        finally:
            self.file.close()
            if self.instance_guard is not None:
                self.instance_guard.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _owner(plan: dict[str, Any]) -> Path:
    return Path(plan["data_baseline_path"]).parent / "data-owner.json"


def acquire_update_access(plan: dict[str, Any], token: str) -> DataAccess:
    guard = DataAccess(lock_path(Path(plan["data_dir"])), True)
    try:
        payload = {"schema_version": _PROTOCOL, "request": plan["update_request_id"],
                   "data_dir": str(Path(plan["data_dir"]).resolve()), "owner": process_identity(os.getpid())}
        # The existing Windows mutex also excludes released clients that do not
        # yet know the new data-directory lock. The authenticated child joins
        # the same mutex object before the updater releases its handle.
        guard.instance_guard = acquire_single_instance()
        with _owner(plan).open("x", encoding="utf-8") as f:
            json.dump({"payload": payload, "auth": authenticate(payload, token)}, f)
            f.flush()
            os.fsync(f.fileno())
        return guard
    except Exception:
        guard.close()
        raise


def _verify_owner(plan: dict[str, Any], token: str) -> None:
    try:
        record = json.loads(_owner(plan).read_text(encoding="utf-8"))
        p = record["payload"]
        valid = (hmac.compare_digest(record["auth"], authenticate(p, token))
                 and p["schema_version"] == _PROTOCOL and p["request"] == plan["update_request_id"]
                 and p["data_dir"] == str(Path(plan["data_dir"]).resolve())
                 and identity_alive(p["owner"]))
        if not valid:
            raise ValueError()
    except (ValueError, KeyError, OSError, TypeError) as exc:
        raise RuntimeError("UPDATE_DATA_OWNER_INVALID") from exc


def authorize_update_writer(plan: dict[str, Any], token: str) -> None:
    """Claim exactly one child; a second process cannot reuse the handoff."""
    global _permit, _instance_guard
    _verify_owner(plan, token)
    directory = Path(plan["data_dir"]).resolve()
    # A missing exclusive owner is a lost handoff, even if a stale owner file exists.
    try:
        access = DataAccess(lock_path(directory), False)
    except RuntimeError as exc:
        if str(exc) != "UPDATE_DATA_DIRECTORY_BUSY":
            raise
    else:
        access.close()
        raise RuntimeError("UPDATE_DATA_OWNER_LOST")
    claim = _owner(plan).with_name("new-worker-claim.json")
    payload = {"request": plan["update_request_id"], "child": process_identity(os.getpid())}
    try:
        with claim.open("x", encoding="utf-8") as f:
            json.dump({"payload": payload, "auth": authenticate(payload, token)}, f)
    except FileExistsError:
        try:
            existing = json.loads(claim.read_text(encoding="utf-8"))
            if existing["payload"] != payload or not hmac.compare_digest(existing["auth"], authenticate(payload, token)):
                raise ValueError()
        except (ValueError, KeyError, OSError) as invalid:
            raise RuntimeError("UPDATE_DATA_CHILD_ALREADY_CLAIMED") from invalid
    with _local:
        if _instance_guard is None:
            _instance_guard = acquire_single_instance(join_authenticated_update=True)
        try:
            _verify_owner(plan, token)
        except Exception:
            _instance_guard.release()
            _instance_guard = None
            raise
        _permit = (directory, dict(plan), token)


def acquire_data_access(data_dir: Path) -> DataAccess | None:
    try:
        return DataAccess(lock_path(data_dir), False)
    except RuntimeError as exc:
        if str(exc) != "UPDATE_DATA_DIRECTORY_BUSY":
            raise
        with _local:
            permit = _permit
        if permit is None or permit[0] != data_dir.resolve():
            raise
        _verify_owner(permit[1], permit[2])
        return None


def clear_update_writer() -> None:
    global _permit, _instance_guard
    with _local:
        _permit = None
        if _instance_guard is not None:
            _instance_guard.release()
            _instance_guard = None
