"""Best-effort update evidence, outside the protected business data directory.

Never serialize arguments, exception messages/locals, plans or credentials.
Unknown exceptions retain their type, OS codes and source locations instead.
This module has only stdlib dependencies so early startup can use it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
import traceback


MAX_DIAGNOSTIC_BYTES = 256 * 1024


def update_error_code(exc: BaseException) -> str:
    value = getattr(exc, "code", None) or str(exc)
    if isinstance(value, str) and re.fullmatch(r"UPDATE_[A-Z0-9_]{1,100}", value):
        return value
    return "UPDATE_STARTUP_EXCEPTION"


def record_update_startup_failure(
    plan_path: Path,
    *,
    phase: str,
    exc: BaseException,
    exit_code: int | None = None,
) -> None:
    try:
        if not plan_path.is_absolute():
            plan_path = plan_path.absolute()
        path = plan_path.parent / "worker-startup.jsonl"
        record = {
            "schema_version": 1,
            "timestamp_epoch": time.time(),
            "pid": os.getpid(),
            "phase": phase,
            "error_code": update_error_code(exc),
            "exception_type": type(exc).__name__,
            "exit_code": exit_code,
            "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None),
            "frames": [
                {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(exc.__traceback__)[-12:]
            ],
        }
        differences = getattr(exc, "differences", None)
        if isinstance(differences, list):
            record["differences"] = differences
        data = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        current_size = path.stat().st_size if path.exists() else 0
        if path.exists() and current_size + len(data.encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
            path.replace(path.with_suffix(".previous.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(data)
    except Exception:
        # A full disk, permissions or a malformed exception must never alter
        # startup validation, task settlement or the rollback decision.
        try:
            sys.stderr.write(data if "data" in locals() else "update_diagnostic_write_failed\n")
        except Exception:
            pass


def token_argument(option: str, token: str) -> str:
    """Keep opaque values beginning with '-' out of argparse's option grammar."""
    if option not in {"--token", "--post-update-token"}:
        raise ValueError("UPDATE_TOKEN_OPTION_INVALID")
    return option + "=" + token


def record_update_handoff(path: Path, phase: str, **fields) -> None:
    """Fixed-shape, secret-free lifecycle evidence; failure never grants admission."""
    allowed = {"elapsed_seconds", "remaining_seconds", "exit_code", "token_leading_hyphen",
               "exception_type", "stage", "state"}
    try:
        record = {"phase": phase, "pid": os.getpid(), "timestamp_epoch": time.time(),
                  "monotonic_seconds": time.monotonic()}
        record.update({k: v for k, v in fields.items() if k in allowed})
        data = json.dumps(record, ensure_ascii=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size + len(data) > MAX_DIAGNOSTIC_BYTES:
            path.replace(path.with_suffix(".previous.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(data)
    except Exception:
        pass
