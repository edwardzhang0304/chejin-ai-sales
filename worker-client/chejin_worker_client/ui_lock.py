from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG


UI_LOCK_BUSY = "UI_LOCK_BUSY"
UI_LOCK_ACQUIRE_TIMEOUT = "UI_LOCK_ACQUIRE_TIMEOUT"
UI_LOCK_LEASE_EXPIRED = "UI_LOCK_LEASE_EXPIRED"
UI_LOCK_RENEW_FAILED = "UI_LOCK_RENEW_FAILED"
UI_LOCK_OWNER_MISMATCH = "UI_LOCK_OWNER_MISMATCH"
UI_STEP_TIMEOUT = "UI_STEP_TIMEOUT"

LOCK_FILE = CONFIG.app_dir / "runtime" / "worker" / "ui_lock.json"
_PROCESS_LOCK = threading.Lock()


class UiLockError(RuntimeError):
    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _read_lock(path: Path = LOCK_FILE) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return {"corrupted": True, "path": str(path)}
    return payload if isinstance(payload, dict) else {"corrupted": True, "path": str(path)}


def _write_lock(payload: dict[str, Any], path: Path = LOCK_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _delete_lock(path: Path = LOCK_FILE) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _is_expired(payload: dict[str, Any], *, now: datetime | None = None) -> bool:
    expires_at = _parse_iso(payload.get("lease_expires_at"))
    if expires_at is None:
        return True
    return expires_at <= (now or _utc_now())


def lock_summary() -> dict[str, Any]:
    payload = _read_lock()
    if not payload:
        return {"locked": False}
    return {
        "locked": not _is_expired(payload),
        "lock_id": payload.get("lock_id"),
        "fencing_token": payload.get("fencing_token"),
        "operation_type": payload.get("operation_type"),
        "current_step": payload.get("current_step"),
        "lease_expires_at": payload.get("lease_expires_at"),
        "owner": payload.get("owner"),
        "expired": _is_expired(payload),
    }


def force_recover_stale_lock(*, reason: str = "stale_lock_recovered") -> dict[str, Any]:
    with _PROCESS_LOCK:
        payload = _read_lock()
        if not payload:
            return {"recovered": False, "reason": "no_lock"}
        if not _is_expired(payload):
            return {"recovered": False, "reason": "lock_not_expired", "lock": payload}
        _delete_lock()
        return {"recovered": True, "reason": reason, "lock": payload}


def _next_fencing_token(previous: dict[str, Any] | None) -> int:
    try:
        return int((previous or {}).get("fencing_token") or 0) + 1
    except (TypeError, ValueError):
        return 1


@dataclass
class UiLockLease:
    lock_id: str
    owner: str
    fencing_token: int
    operation_type: str
    current_step: str
    path: Path = LOCK_FILE
    _renew_stop: threading.Event | None = None
    _renew_thread: threading.Thread | None = None
    _last_step_started_at: float = 0.0
    _lease_lost: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lease_error: UiLockError | None = field(default=None, init=False, repr=False)

    def start_auto_renew(self, interval_seconds: float | None = None) -> None:
        interval = max(1.0, float(interval_seconds or CONFIG.ui_lock_renew_interval_seconds))
        if self._renew_thread and self._renew_thread.is_alive():
            return
        self.raise_if_lost()
        self._renew_stop = threading.Event()
        self._renew_thread = threading.Thread(target=self._renew_loop, args=(interval,), name="CheJinUiLockRenew", daemon=True)
        self._renew_thread.start()

    def _renew_loop(self, interval: float) -> None:
        assert self._renew_stop is not None
        while not self._renew_stop.wait(interval):
            try:
                self.renew()
            except UiLockError as exc:
                self._lease_error = exc
                self._lease_lost.set()
                break

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    @property
    def lease_error(self) -> UiLockError | None:
        return self._lease_error

    def cancel_requested(self) -> bool:
        return self.lease_lost

    def raise_if_lost(self) -> None:
        if not self.lease_lost:
            return
        if self._lease_error is not None:
            raise self._lease_error
        raise UiLockError(
            UI_LOCK_RENEW_FAILED,
            "微信 UI 锁续租已失败，当前流程不得继续操作微信。",
            data={"lock_id": self.lock_id},
        )

    def renew(self) -> dict[str, Any]:
        self.raise_if_lost()
        with _PROCESS_LOCK:
            payload = _read_lock(self.path)
            if not payload or payload.get("lock_id") != self.lock_id or payload.get("owner") != self.owner:
                raise UiLockError(UI_LOCK_OWNER_MISMATCH, "当前进程不再持有微信 UI 锁。", data={"lock": payload or {}})
            if _is_expired(payload):
                raise UiLockError(UI_LOCK_LEASE_EXPIRED, "微信 UI 锁租约已过期。", data={"lock": payload})
            now = _utc_now()
            payload.update(
                {
                    "current_step": self.current_step,
                    "renewed_at": _iso(now),
                    "lease_expires_at": _iso(now + timedelta(seconds=max(5.0, CONFIG.ui_lock_lease_seconds))),
                }
            )
            try:
                _write_lock(payload, self.path)
            except OSError as exc:
                raise UiLockError(UI_LOCK_RENEW_FAILED, str(exc), data={"lock_id": self.lock_id}) from exc
            return payload

    def update_step(self, current_step: str) -> None:
        self.raise_if_lost()
        self.current_step = str(current_step or self.current_step)
        self._last_step_started_at = time.monotonic()
        self.renew()

    def check_step_timeout(self) -> None:
        self.raise_if_lost()
        if self._last_step_started_at <= 0:
            return
        elapsed = time.monotonic() - self._last_step_started_at
        if elapsed > max(1.0, CONFIG.ui_step_timeout_seconds):
            raise UiLockError(UI_STEP_TIMEOUT, f"微信 UI 步骤超时：{self.current_step}", data={"elapsed_seconds": round(elapsed, 3)})

    def release(self) -> None:
        if self._renew_stop:
            self._renew_stop.set()
        if self._renew_thread and self._renew_thread.is_alive():
            self._renew_thread.join(timeout=1.0)
        with _PROCESS_LOCK:
            payload = _read_lock(self.path)
            if not payload:
                return
            if payload.get("lock_id") != self.lock_id or payload.get("owner") != self.owner:
                raise UiLockError(UI_LOCK_OWNER_MISMATCH, "释放微信 UI 锁失败：锁归属不匹配。", data={"lock": payload})
            _delete_lock(self.path)


def acquire_ui_lock(
    *,
    operation_type: str,
    owner: str,
    current_step: str = "starting",
    timeout_seconds: float | None = None,
) -> UiLockLease:
    timeout = max(0.1, float(timeout_seconds or CONFIG.ui_lock_acquire_timeout_seconds))
    deadline = time.monotonic() + timeout
    last_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        with _PROCESS_LOCK:
            payload = _read_lock()
            last_payload = payload
            if payload and _is_expired(payload):
                _delete_lock()
                payload = None
            if not payload:
                now = _utc_now()
                lock_id = f"uilock_{uuid.uuid4()}"
                record = {
                    "lock_id": lock_id,
                    "owner": owner,
                    "fencing_token": _next_fencing_token(last_payload),
                    "operation_type": operation_type,
                    "current_step": current_step,
                    "acquired_at": _iso(now),
                    "renewed_at": _iso(now),
                    "lease_expires_at": _iso(now + timedelta(seconds=max(5.0, CONFIG.ui_lock_lease_seconds))),
                    "pid": os.getpid(),
                }
                _write_lock(record)
                return UiLockLease(
                    lock_id=lock_id,
                    owner=owner,
                    fencing_token=int(record["fencing_token"]),
                    operation_type=operation_type,
                    current_step=current_step,
                    _last_step_started_at=time.monotonic(),
                )
        time.sleep(0.2)
    code = UI_LOCK_BUSY if last_payload and not _is_expired(last_payload) else UI_LOCK_ACQUIRE_TIMEOUT
    raise UiLockError(code, "微信 UI 锁正在被占用，暂不能执行新的微信操作。", data={"lock": last_payload or {}, "timeout_seconds": timeout})
