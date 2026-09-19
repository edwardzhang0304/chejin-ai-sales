"""SP file lifecycle primitives. No scheduling, Worker database or updater code."""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

MAX_PATH_UNITS = 240
MAX_COMMAND_UNITS = 32767


def utf16_units(value):
    return len(str(value).encode("utf-16-le")) // 2


def normalized_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("SEND_REQUEST_ABSOLUTE_PATH_REQUIRED")
    resolved = path.resolve()
    text = str(resolved)
    if os.name == "nt":
        drive, tail = ntpath.splitdrive(text)
        if (text.startswith("\\\\") or not re.fullmatch(r"[A-Za-z]:", drive)
                or not tail.startswith("\\")):
            raise ValueError("SEND_REQUEST_LOCAL_PATH_REQUIRED")
    if utf16_units(text) > MAX_PATH_UNITS:
        raise ValueError("SEND_REQUEST_PATH_BUDGET_EXCEEDED")
    return resolved


def command_units(command):
    # list2cmdline is the quoting routine used by Windows subprocess.Popen.
    return utf16_units(subprocess.list2cmdline([str(x) for x in command])) + 1


def validate_command(command):
    length = command_units(command)
    if length > MAX_COMMAND_UNITS:
        raise ValueError("SEND_REQUEST_COMMAND_BUDGET_EXCEEDED")
    return length


def local_app_data():
    if os.name != "nt":
        raise OSError("Windows KnownFolder unavailable; tests must inject an isolated root")
    # FOLDERID_LocalAppData; NULL token selects the current user.
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091").bytes_le)
    value = ctypes.c_void_p()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    query = shell.SHGetKnownFolderPath
    query.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    query.restype = ctypes.c_long
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    try:
        status = query(ctypes.byref(guid), 0, None, ctypes.byref(value))
        if status != 0 or not value.value:
            raise OSError("LocalAppData KnownFolder lookup failed")
        return Path(ctypes.wstring_at(value))
    finally:
        ole.CoTaskMemFree(value)


def write_package(raw, *, request_id, app_dir, fallback_root=local_app_data, before_write=None):
    if not re.fullmatch(r"[0-9a-f]{32}", str(request_id)) or not isinstance(raw, bytes):
        raise ValueError("SEND_REQUEST_IDENTITY_INVALID")
    seen, failures = set(), []
    for index in range(2):
        temporary = None
        owned = False
        try:
            root = Path(app_dir)/"ipc" if index == 0 else Path(fallback_root())/"CheJinWorker"/"ipc"
            root = root.resolve()
            key = os.path.normcase(str(root))
            if key in seen:
                continue
            seen.add(key)
            final = normalized_path(root/(request_id+".json"))
            temporary = normalized_path(root/(request_id+".tmp"))
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if final.exists() or temporary.exists():
                raise FileExistsError("SEND_REQUEST_ID_COLLISION")
            reference = {"request_id": request_id, "path": str(final), "sha256": hashlib.sha256(raw).hexdigest(),
                         "byte_count": len(raw), "selected_root": str(root), "root_choice": index,
                         "path_utf16_units": utf16_units(final), "root_failures": list(failures),
                         "temporary_path": str(temporary)}
            # The existing action journal must own the exact paths and bytes
            # before either file can survive an interruption. No file scan.
            if before_write is not None:
                before_write(dict(reference))
            # Exclusive creation prevents clobbering another live request.
            with temporary.open("xb") as handle:
                owned = True
                os.chmod(temporary, 0o600)
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            if final.exists():
                raise FileExistsError("SEND_REQUEST_ID_COLLISION")
            os.rename(temporary, final)
            owned = False
            committed = final.read_bytes()
            if committed != raw:
                raise OSError("SEND_REQUEST_COMMIT_BYTES_DIFFER")
            return {**reference, "sha256": hashlib.sha256(committed).hexdigest()}
        except (OSError, ValueError) as exc:
            failures.append({"choice": index, "reason": type(exc).__name__, "detail": str(exc)})
            if owned:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, FileExistsError):
                raise
    error = OSError("SEND_REQUEST_ROOTS_UNAVAILABLE")
    error.failures = failures
    raise error


def read_package(path):
    resolved = normalized_path(path)
    if not re.fullmatch(r"[0-9a-f]{32}\.json", resolved.name):
        raise ValueError("SEND_REQUEST_FILENAME_INVALID")
    return resolved, resolved.read_bytes()


def remove_package(reference):
    path = normalized_path(reference["path"])
    if path.name != reference["request_id"]+".json":
        raise ValueError("SEND_REQUEST_IDENTITY_INVALID")
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("SEND_REQUEST_DIGEST_INVALID")
    path.unlink(missing_ok=True)
