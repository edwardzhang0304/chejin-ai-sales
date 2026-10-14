"""Connection preparation for the existing runtime's single supervision loop.

This component owns the receiver's lifecycle, not business start/pause or AI.
The caller owns polling, the original fault gate and historical synchronization.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from http.server import HTTPServer
import math
import threading
import time
from typing import Callable

from . import ui_lock
from .api import ApiError
from .hook_backend import BACKEND_WAIT_CODES, HookAccountReceipt, HookHeartbeatIdentity, HookHeartbeatState, HookIdentityClient, HookWorkerCredentials
from .hook_gateway import HookGateway, HookLocalError, HookCallbackRegistration
from .hook_inbox import NativeInbox
from .hook_process import HookProcessSnapshot
from .hook_receiver import create_receiver


@dataclass(frozen=True)
class HookPreparedConnection:
    account: HookAccountReceipt
    process: HookProcessSnapshot
    registration: HookCallbackRegistration

    @property
    def business_ready(self) -> bool:
        return False  # Connection preparation cannot prove history or task readiness.


class HookConnectionManager:
    def __init__(self, gateway: HookGateway, backend: HookIdentityClient, inbox: NativeInbox, *,
                 receiver_timeout_seconds: float, on_persistence_failure: Callable[[], None]) -> None:
        if (type(receiver_timeout_seconds) not in {int, float} or not math.isfinite(receiver_timeout_seconds)
                or not 0 < receiver_timeout_seconds <= 30):
            raise ValueError("HOOK_RECEIVER_TIMEOUT_INVALID")
        self.gateway, self.backend, self.inbox = gateway, backend, inbox
        self.receiver_timeout_seconds = receiver_timeout_seconds
        self.on_persistence_failure = on_persistence_failure
        self._operation_lock = threading.RLock()
        self._credentials: HookWorkerCredentials | None = None
        self._account: HookAccountReceipt | None = None
        self._prepared: HookPreparedConnection | None = None
        self._receiver: HTTPServer | None = None
        self._receiver_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None

    @property
    def prepared(self) -> HookPreparedConnection | None:
        with self._operation_lock:
            if (self._receiver_thread is None or not self._receiver_thread.is_alive()
                    or self._receiver is None or self._receiver.fileno() < 0):
                return None
            return self._prepared

    def receiver_health_snapshot(self) -> dict:
        """Local liveness only; never wait on a backend request's operation lock.

        A missing receiver during manual logout is a waiting business state.
        A receiver that was registered and has died is a runtime failure.
        Reconfiguration remains serialized by the normal operation lock.
        """
        registered = self._prepared is not None
        thread, receiver = self._receiver_thread, self._receiver
        alive = bool(thread and thread.is_alive() and receiver and receiver.fileno() >= 0)
        return {"ready": not registered or alive, "receiver_required": registered,
                "receiver_alive": alive}

    def _close_receiver(self) -> None:
        # shutdown waits until the current callback completes SQLite commit.
        # Do not swap the scope of a live HTTP handler or delete stored records.
        self._prepared = None
        receiver, thread = self._receiver, self._receiver_thread
        if receiver is None:
            return
        if thread is not None and thread.is_alive():
            if thread is threading.current_thread():
                raise HookLocalError("HOOK_RECEIVER_SELF_JOIN_REJECTED")
            if self._shutdown_thread is None:
                self._shutdown_thread = threading.Thread(target=receiver.shutdown,
                    name="CheJinHookReceiverDrain", daemon=True)
                self._shutdown_thread.start()
            budget = self.receiver_timeout_seconds + 1
            deadline = time.monotonic() + budget
            self._shutdown_thread.join(timeout=budget)
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if self._shutdown_thread.is_alive() or thread.is_alive():
                raise HookLocalError("HOOK_RECEIVER_STOP_UNCONFIRMED", outcome_unknown=True)
        receiver.server_close()
        self._receiver = self._receiver_thread = None
        self._shutdown_thread = None

    def close(self) -> None:
        """Called by original stop/update/exit cleanup; not from receiver callbacks."""
        with self._operation_lock:
            self._close_receiver()

    def _report_logout(self, credentials, process, state):
        # Report the last authenticated account as logged out, never as a new
        # observed login. This immediately closes server AI admission while
        # retaining the old account binding and original rebind latch.
        if self._account is not None:
            identity=HookHeartbeatIdentity(self._account.self_wxid,process.generation,
                self.gateway.deployment.revision,process.dll_sha256,account_epoch=self._account.account_epoch)
            self._account=self.backend.heartbeat(credentials,identity,replace(state,wechat_status='logged_out'))

    def prepare(self, credentials: HookWorkerCredentials, state: HookHeartbeatState) -> HookPreparedConnection | None:
        """One bounded connection attempt; no retry, polling or automatic Start.

        None means WeChat is logged out. The original heartbeat loop must still
        report that observed status. A backend network outage preserves an
        already approved receiver for this same observed account/process.
        """
        with self._operation_lock:
            if self._credentials != credentials:
                self._close_receiver()
                self._account = None
                self._credentials = credentials
            try:
                process = self.gateway.inspect()
                old = self.prepared
                if (old and old.process.generation == process.generation
                        and ui_lock.lock_summary().get("locked")):
                    # The original runtime also heartbeats its last verified
                    # identity while an action owns the UI lease. Do not wait
                    # behind Voice2Text or tear down passive receiving for C1.
                    scope = old.registration.scope
                    identity = HookHeartbeatIdentity(scope.self_wxid,process.generation,
                        self.gateway.deployment.revision,process.dll_sha256,account_epoch=scope.account_epoch)
                    account = self.backend.heartbeat(credentials,identity,replace(state,wechat_status="logged_in"))
                    if account.blocked or account.rebind_required:
                        self._close_receiver()
                        raise HookLocalError("HOOK_ACCOUNT_NOT_CONFIRMED")
                    self._account = account
                    self._prepared = HookPreparedConnection(account,process,old.registration)
                    return self._prepared
                login = self.gateway.login_status(process)
            except ApiError as exc:
                if exc.code not in BACKEND_WAIT_CODES or self.prepared is None:
                    self._close_receiver()
                raise
            except ui_lock.UiLockError as exc:
                if exc.code not in {ui_lock.UI_LOCK_BUSY,ui_lock.UI_LOCK_ACQUIRE_TIMEOUT}:
                    self._close_receiver()
                raise
            except HookLocalError as exc:
                old = self.prepared
                from .hook_active_calls import unresolved
                isolated=exc.code=='HOOK_PROCESS_ACTIVE_CALL_QUARANTINED' or unresolved(process.generation)
                if (not isolated or old is None
                        or old.process.generation != process.generation):
                    self._close_receiver()
                raise  # Same approved receiver keeps saving while calls are isolated.
            except Exception:
                self._close_receiver()
                raise
            if not login.logged_in:
                self._close_receiver()
                self._report_logout(credentials,process,state)
                return None
            old = self.prepared
            if old and (old.process.generation != process.generation or old.account.self_wxid != login.self_wxid):
                self._close_receiver()
                old = None
            identity = HookHeartbeatIdentity(login.self_wxid, process.generation,
                self.gateway.deployment.revision, process.dll_sha256,
                account_epoch=self._account.account_epoch if self._account else None)
            try:
                account = self.backend.heartbeat(credentials, identity, replace(state, wechat_status="logged_in"))
            except ApiError as exc:
                if exc.code not in BACKEND_WAIT_CODES or old is None:
                    self._close_receiver()
                raise
            self._account = account
            if account.blocked or account.rebind_required:
                self._close_receiver()
                raise HookLocalError("HOOK_ACCOUNT_NOT_CONFIRMED")
            scope = account.receive_scope(self.gateway.deployment.hook_api_port)
            # Backend requests hold no WeChat action lease. Reinspect afterwards.
            try:
                current = self.gateway.inspect()
                if current.generation != process.generation:
                    raise HookLocalError("HOOK_PROCESS_GENERATION_CHANGED")
                if old and old.registration.scope == scope:
                    # Login may change without a process restart while the
                    # backend request is in flight. Never reuse its old scope
                    # merely because the PID/generation still matches.
                    current_login = self.gateway.login_status(current)
                    if not current_login.logged_in:
                        self._close_receiver()
                        self._report_logout(credentials,current,state)
                        return None
                    if current_login.self_wxid != scope.self_wxid:
                        raise HookLocalError("HOOK_CALLBACK_ACCOUNT_MISMATCH")
                    result = HookPreparedConnection(account, current, old.registration)
                    self._prepared = result
                    return result
                self._close_receiver()
                self._receiver = create_receiver(port=self.gateway.deployment.receiver_port, inbox=self.inbox,
                    scope=scope, request_timeout_seconds=self.receiver_timeout_seconds,
                    on_persistence_failure=self.on_persistence_failure)
                self._receiver_thread = threading.Thread(target=self._receiver.serve_forever,
                    name="CheJinHookReceiver", daemon=True)
                self._receiver_thread.start()
                registration = self.gateway.register_callback(current, scope, self._receiver, self._receiver_thread)
                self._prepared = HookPreparedConnection(account, current, registration)
                return self._prepared
            except Exception:
                self._close_receiver()
                raise
