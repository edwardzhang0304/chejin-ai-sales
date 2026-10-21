"""Fixed local routes for identity and callback registration, not a send API.

Only this gateway's local requests hold the existing WeChat action lease.
Callbacks, backend HTTP and SQLite projection do not hold that lease.
"""
from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from http.server import HTTPServer
from threading import Thread,local
from typing import Callable

import requests
import uuid

from . import ui_lock
from .hook_config import HookDeployment
from .hook_inbox import HookReceiveScope
from .hook_history import HookHistoryCatalog, HookHistoryQuery, HookHistoryCapture
from .hook_process import HookProcessSnapshot, inspect_windows_process, inspect_windows_generation
from .shared_rules import hook_contract_rules as rules


@dataclass(frozen=True)
class HookLocalResponse:
    raw_body: bytes

    @property
    def payload(self) -> dict:
        result = rules.loads(self.raw_body.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("HOOK_LOCAL_RESPONSE_INVALID")
        return result


class HookLocalError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)  # No upstream body, wxid, URL or credential.
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class HookLoginStatus:
    onlinestatus: str
    self_wxid: str | None
    process_generation: str

    @property
    def logged_in(self) -> bool:
        return self.onlinestatus == "3"


@dataclass(frozen=True)
class HookCallbackRegistration:
    scope: HookReceiveScope
    callback_url: str
    history_required: bool = True
    business_ready: bool = False


class _HistoryReadSession:
    """One capture's lease; each SQL keeps live account/catalog/response checks.

    No backend call, projection, cached identity, or physical action belongs here.
    Full component inspection happens at both lease boundaries; PID reuse is
    checked around every read. A caught read failure poisons the whole session.
    """
    def __init__(self, gateway, process, scope, lease):
        self.gateway, self.process, self.scope, self.lease = gateway, process, scope, lease
        self.closed = self.failed = self.uncertain = False
        self.deadline = time.monotonic() + 30.0
        self.api_calls = 0
        self.api_seconds = 0.0

    def check_budget(self):
        if time.monotonic() >= self.deadline:
            self.failed = True
            raise ValueError('HOOK_HISTORY_READ_BUDGET_EXCEEDED')

    @contextmanager
    def _reading(self, process, scope):
        if self.closed or self.failed or process != self.process or scope != self.scope:
            raise HookLocalError("HOOK_HISTORY_SESSION_INVALID")
        try:
            self.check_budget()
            self.lease.raise_if_lost()
            self.gateway.generation_inspector(process)
            yield self.lease
            self.check_budget()
            self.gateway.generation_inspector(process)
            self.lease.raise_if_lost()
        except BaseException:
            self.failed = True
            raise

    def history_catalog(self, process, scope):
        with self._reading(process, scope):
            return self.gateway._history_catalog(process, scope)

    def history_capture(self, process, catalog, query):
        with self._reading(process, catalog.scope) as lease:
            return self.gateway._history_capture(process, catalog, query, lease,
                                                 self.gateway.generation_inspector)


class HookGateway:
    def __init__(self, deployment: HookDeployment, *, timeout_seconds: float,
                 max_response_bytes: int, lock_timeout_seconds: float,
                 inspector: Callable[[HookDeployment], HookProcessSnapshot] = inspect_windows_process,
                 generation_inspector: Callable[[HookProcessSnapshot], None] | None = None) -> None:
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 60 or type(max_response_bytes) is not int
                or not 0 < max_response_bytes <= 2 * 1024 * 1024
                or type(lock_timeout_seconds) not in {int, float} or not math.isfinite(lock_timeout_seconds)
                or not 0 < lock_timeout_seconds <= 60):
            raise ValueError("HOOK_GATEWAY_LIMIT_INVALID")
        self.deployment = deployment
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.lock_timeout_seconds = lock_timeout_seconds
        self.inspector = inspector
        # Injected platforms retain their inspector unless they explicitly supply
        # a separate live identity checker; never call Windows APIs in replay.
        self.generation_inspector = generation_inspector or (
            inspect_windows_generation if inspector is inspect_windows_process else self._same_process)
        self._call_context=local()

    def inspect(self) -> HookProcessSnapshot:
        result = self.inspector(self.deployment)
        result.validate(self.deployment)
        return result

    def _same_process(self, expected: HookProcessSnapshot) -> None:
        if self.inspect().generation != expected.generation:
            raise HookLocalError("HOOK_PROCESS_GENERATION_CHANGED")

    @contextmanager
    def _locked(self, process: HookProcessSnapshot, step: str):
        process.validate(self.deployment)
        from .hook_active_calls import unresolved
        from .hook_voice import process_voice_call_unresolved
        # Acquire the original action lease before classifying durable calls.
        # A live owner normally has a calling/transcribing row. A contender must
        # wait for that owner, then inspect its committed outcome; only a row
        # still unresolved after acquiring the lease is abandoned or unknown.
        lease = ui_lock.acquire_ui_lock(operation_type="hook_control", owner=f"hook-{process.generation}",
            current_step=step, timeout_seconds=self.lock_timeout_seconds)
        try:
            lease.start_auto_renew()
            lease.raise_if_lost()
            self._same_process(process)
            if process_voice_call_unresolved(process.generation) or unresolved(process.generation):
                raise HookLocalError("HOOK_PROCESS_ACTIVE_CALL_QUARANTINED")
            old_generation=getattr(self._call_context,'generation',None)
            self._call_context.generation=process.generation
            try:yield lease
            finally:self._call_context.generation=old_generation
            lease.raise_if_lost()
            self._same_process(process)
        finally:
            lease.release()

    @contextmanager
    def history_session(self, process, scope):
        if getattr(self._call_context, 'history_reader', None) is not None:
            raise HookLocalError("HOOK_HISTORY_SESSION_INVALID")
        from .hook_active_calls import reserve, finish
        with self._locked(process, "hook_history_capture") as lease:
            reader = _HistoryReadSession(self, process, scope, lease)
            call_id = str(uuid.uuid4())
            # Persist BEFORE the first DLL read. A crash anywhere in this lease
            # leaves calling; an unknown HTTP outcome leaves uncertain. Neither
            # can expire or authorize a later action in this process generation.
            reserve(process.generation, call_id, 'history_capture')
            reader.http_session = requests.Session()
            reader.http_session.trust_env = False
            self._call_context.history_reader = reader
            try:
                yield reader
                reader.check_budget()
                if reader.failed:
                    raise HookLocalError("HOOK_HISTORY_SESSION_INVALID")
            finally:
                reader.closed = True
                self._call_context.history_reader = None
                reader.http_session.close()
                finish(process.generation, call_id, not reader.uncertain)

    def _response(self,route,payload,*,uncertain_on_failure=False):
        reader = getattr(self._call_context, 'history_reader', None)
        if reader is not None:
            if reader.closed or reader.failed or reader.uncertain:
                raise HookLocalError("HOOK_HISTORY_SESSION_INVALID")
            if route not in {"IsLoginStatus", "GetDBHandleAndPath", "QueryDB"}:
                reader.failed = True
                raise HookLocalError("HOOK_ROUTE_NOT_ALLOWED")
            reader.check_budget()
            started = time.perf_counter()
            reader.api_calls += 1
            try:
                return self._response_http(route,payload,uncertain_on_failure=uncertain_on_failure)
            except BaseException as exc:
                reader.failed = True
                reader.uncertain = not isinstance(exc, HookLocalError) or self._unfinished(exc)
                raise
            finally:
                reader.api_seconds += time.perf_counter() - started
        generation=getattr(self._call_context,'generation',None)
        if generation is None:
            return self._response_http(route,payload,uncertain_on_failure=uncertain_on_failure)
        from .hook_active_calls import reserve,finish
        call_id=str(uuid.uuid4())
        reserve(generation,call_id,route)
        try:
            result=self._response_http(route,payload,uncertain_on_failure=uncertain_on_failure)
        except HookLocalError as exc:
            finish(generation,call_id,not self._unfinished(exc))
            raise
        except BaseException:
            finish(generation,call_id,False)
            raise
        finish(generation,call_id,True)
        return result

    @staticmethod
    def _unfinished(error):
        return error.outcome_unknown or error.code == 'HOOK_LOCAL_TRANSPORT_FAILED'

    def _response_http(self, route: str, payload: dict, *, uncertain_on_failure: bool = False) -> HookLocalResponse:
        if route not in {"IsLoginStatus", "ConfigureMsgReciveFullURL", "GetDBHandleAndPath", "QueryDB", "SendTextMsg", "GetFriendAndChatRoomList", "Voice2Text"}:
            raise HookLocalError("HOOK_ROUTE_NOT_ALLOWED")
        try:
            reader = getattr(self._call_context, 'history_reader', None)
            shared = reader.http_session if reader is not None else None
            with (nullcontext(shared) if shared is not None else requests.Session()) as session:
                session.trust_env = False  # No proxy, netrc credentials or redirects on loopback.
                with session.post(self.deployment.api_base_url + "/" + route, json=payload,
                        timeout=self.timeout_seconds, allow_redirects=False, stream=True) as response:
                    raw = bytearray()
                    if response.status_code != 200:
                        raise HookLocalError("HOOK_HTTP_RESPONSE_REJECTED", outcome_unknown=uncertain_on_failure)
                    for chunk in response.iter_content(chunk_size=8192):
                        raw.extend(chunk)
                        if len(raw) > self.max_response_bytes:
                            raise HookLocalError("HOOK_RESPONSE_TOO_LARGE", outcome_unknown=uncertain_on_failure)
            result = HookLocalResponse(bytes(raw))
            _ = result.payload
            return result
        except requests.RequestException as exc:
            # Keep the stable fault code and quarantine semantics. Only bounded
            # categories cross into diagnostics, never exception text or URLs.
            error=HookLocalError("HOOK_LOCAL_TRANSPORT_FAILED", outcome_unknown=uncertain_on_failure)
            error.operation=route
            error.transport_kind=('timeout' if isinstance(exc,requests.Timeout) else
                'connection_error' if isinstance(exc,requests.ConnectionError) else 'request_error')
            raise error from None
        except (ValueError, RecursionError):
            raise HookLocalError("HOOK_LOCAL_RESPONSE_INVALID", outcome_unknown=uncertain_on_failure) from None

    def _post(self, route: str, payload: dict, *, uncertain_on_failure: bool = False) -> dict:
        return self._response(route, payload, uncertain_on_failure=uncertain_on_failure).payload

    def _login(self, process: HookProcessSnapshot) -> HookLoginStatus:
        response = self._post("IsLoginStatus", {})
        status = response.get("onlinestatus")
        wxid = response.get("selfwxid")
        status_valid = isinstance(status, str) and status in {"0", "1", "2", "3"}
        identity_valid = isinstance(wxid, str) and re.fullmatch(r"wxid_[A-Za-z0-9_]{1,59}", wxid) is not None
        if not status_valid or status == "3" and not identity_valid:
            error = HookLocalError("HOOK_LOGIN_RESPONSE_INVALID" if not status_valid else "HOOK_LOGIN_IDENTITY_MISSING")
            error.operation = "IsLoginStatus"
            error.diagnostic_details = {
                "status_is_string": isinstance(status, str), "status_valid": status_valid,
                "account_present": "selfwxid" in response, "account_is_string": isinstance(wxid, str),
                "account_length": len(wxid) if isinstance(wxid, str) else None,
                "account_has_outer_spaces": wxid != wxid.strip() if isinstance(wxid, str) else None,
                "account_matches_rule": identity_valid,
            }
            raise error
        wxid = wxid if status == "3" else None
        return HookLoginStatus(status, wxid, process.generation)

    def login_status(self, process: HookProcessSnapshot) -> HookLoginStatus:
        with self._locked(process, "hook_login_check"):
            return self._login(process)

    def register_callback(self, process: HookProcessSnapshot, scope: HookReceiveScope,
                          receiver: HTTPServer, receiver_thread: Thread) -> HookCallbackRegistration:
        if (scope.process_generation != process.generation or scope.hook_api_port != self.deployment.hook_api_port
                or getattr(receiver, "hook_receive_scope", None) != scope
                or receiver.server_address != ("127.0.0.1", self.deployment.receiver_port)
                or receiver.fileno() < 0 or not receiver_thread.is_alive()):
            raise HookLocalError("HOOK_RECEIVER_NOT_READY")
        with self._locked(process, "hook_callback_register") as lease:
            identity = self._login(process)
            if not identity.logged_in or identity.self_wxid != scope.self_wxid:
                raise HookLocalError("HOOK_CALLBACK_ACCOUNT_MISMATCH")
            lease.raise_if_lost()
            self._same_process(process)
            response = self._post("ConfigureMsgReciveFullURL", {"RecvType": 1,
                "CallBackURL": self.deployment.callback_url}, uncertain_on_failure=True)
            if (type(response.get("code")) is not int or response["code"] != 0
                    or response.get("CallBackURL") != self.deployment.callback_url
                    or type(response.get("RecvType")) is not int or response["RecvType"] != 1):
                raise HookLocalError("HOOK_CALLBACK_REGISTRATION_UNCONFIRMED", outcome_unknown=True)
            lease.raise_if_lost()
            self._same_process(process)
            identity = self._login(process)
            if not identity.logged_in or identity.self_wxid != scope.self_wxid:
                raise HookLocalError("HOOK_CALLBACK_ACCOUNT_CHANGED", outcome_unknown=True)
            if receiver.fileno() < 0 or not receiver_thread.is_alive():
                raise HookLocalError("HOOK_RECEIVER_STOPPED", outcome_unknown=True)
            return HookCallbackRegistration(scope, self.deployment.callback_url)


    def _history_identity(self, process: HookProcessSnapshot, scope: HookReceiveScope) -> None:
        if scope.process_generation != process.generation or scope.hook_api_port != self.deployment.hook_api_port:
            raise HookLocalError("HOOK_HISTORY_INSTANCE_MISMATCH")
        login = self._login(process)
        if not login.logged_in or login.self_wxid != scope.self_wxid:
            raise HookLocalError("HOOK_HISTORY_ACCOUNT_MISMATCH")

    def history_catalog(self, process: HookProcessSnapshot, scope: HookReceiveScope) -> HookHistoryCatalog:
        with self._locked(process, "hook_history_catalog"):
            return self._history_catalog(process, scope)

    def _history_catalog(self, process, scope):
        self._history_identity(process, scope)
        catalog = HookHistoryCatalog.capture(scope, self._post("GetDBHandleAndPath", {}))
        self._history_identity(process, scope)
        return catalog

    def history_read(self, process: HookProcessSnapshot, catalog: HookHistoryCatalog,
                     query: HookHistoryQuery) -> dict:
        return self.history_capture(process, catalog, query).payload

    def history_capture(self, process: HookProcessSnapshot, catalog: HookHistoryCatalog,
                        query: HookHistoryQuery) -> HookHistoryCapture:
        """Only validated local templates; callers must persist before advancing."""
        if not isinstance(catalog, HookHistoryCatalog) or not isinstance(query, HookHistoryQuery):
            raise HookLocalError("HOOK_HISTORY_QUERY_INVALID")
        query.validate_scope(catalog)
        with self._locked(process, "hook_history_read") as lease:
            return self._history_capture(process, catalog, query, lease, self._same_process)

    def _history_capture(self, process, catalog, query, lease, process_check):
        if not isinstance(catalog, HookHistoryCatalog) or not isinstance(query, HookHistoryQuery):
            raise HookLocalError("HOOK_HISTORY_QUERY_INVALID")
        query.validate_scope(catalog)
        self._history_identity(process, catalog.scope)
        before = HookHistoryCatalog.capture(catalog.scope, self._post("GetDBHandleAndPath", {}))
        if before != catalog:
            raise HookLocalError("HOOK_HISTORY_CATALOG_CHANGED")
        lease.raise_if_lost()
        process_check(process)
        response = self._response("QueryDB", query.payload())
        after = HookHistoryCatalog.capture(catalog.scope, self._post("GetDBHandleAndPath", {}))
        if after != catalog:
            raise HookLocalError("HOOK_HISTORY_CATALOG_CHANGED")
        self._history_identity(process, catalog.scope)
        return HookHistoryCapture(catalog, query, response.raw_body)


    def send_text_once(self,process,scope,permit,expected_barrier,barrier,begin,mark_trigger):
        from .hook_sender import NativeSendPermit
        if not isinstance(permit,NativeSendPermit):raise HookLocalError('HOOK_SEND_PERMIT_INVALID')
        permit.validate_scope(process,scope)
        with self._locked(process,'hook_send_text') as lease:
            self._history_identity(process,scope)
            if barrier()!=expected_barrier:raise HookLocalError('HOOK_SEND_NEW_OBSERVATION')
            begin()
            lease.raise_if_lost()
            self._history_identity(process,scope)
            permit.validate_scope(process,scope)
            if barrier()!=expected_barrier:raise HookLocalError('HOOK_SEND_NEW_OBSERVATION')
            mark_trigger()
            return self._response('SendTextMsg',{'toid':permit.peer_wxid,'msg':permit.reply_text},uncertain_on_failure=True)

    def friend_list(self,process,scope):
        with self._locked(process,'hook_formal_friend_read'):
            self._history_identity(process,scope)
            result=self._response('GetFriendAndChatRoomList',{'type':'1'})
            self._history_identity(process,scope)
            return result
