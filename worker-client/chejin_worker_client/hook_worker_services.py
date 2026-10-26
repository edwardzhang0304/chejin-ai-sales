"""Hook connection and durable intake hosted by the original TaskRunner.

This is an explicit opt-in lifecycle adapter. It cannot claim a Task, enable
AI, or send a message. A business dispatcher must be attached separately after
reconciling the original Flow, history and receipt barriers. The GUI's default
OCR construction remains separate from this explicit Hook adapter.
"""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
import threading
from typing import Callable

from .api import ApiError
from .emergency_stop import emergency_stop_requested, trigger_emergency_stop
from .hook_backend import BACKEND_WAIT_CODES, HookHeartbeatState, HookWorkerCredentials
from .hook_connection import HookConnectionManager
from .hook_clock import CLOCK_WAIT_CODES
from .hook_gateway import HookLocalError
from .hook_history_wait import is_history_wait
from .hook_formal_runtime import HookRuntimeStopping
from .hook_projection import NativeProjectionStore
from .hook_process import inspect_windows_window_geometry
from . import storage, ui_lock
from . import startup_check_state
from .hook_account_issues import ACCOUNT_ISSUES
from .hook_progress import emit_progress,register_progress,_deliver
from .hook_startup_window import StartupWindowWait


def _protocol_check(error):
    """Only fixed shapes and numeric locators may cross the fault-log boundary."""
    value = getattr(error, 'diagnostic_details', None)
    if not isinstance(value, dict):
        return {}
    safe = {}
    booleans = {'status_is_string', 'status_valid', 'account_present', 'account_is_string',
                'account_has_outer_spaces', 'account_matches_rule'}
    for key, item in value.items():
        if key in booleans and type(item) is bool:
            safe[key] = item
        elif key == 'account_length' and type(item) is int and 0 <= item <= 1048576:
            safe[key] = item
        elif key in {'localId', 'MsgSvrID', 'Type', 'IsSender'} and isinstance(item, str) and re.fullmatch(r'0|[1-9][0-9]{0,19}', item):
            safe[key] = item
        elif key == 'database' and isinstance(item, str) and re.fullmatch(r'MSG[0-9]{1,9}\.db', item):
            safe[key] = item
        elif key == 'identity_failure' and item in ('duplicate_positive_id', 'zero_id_not_outgoing_text'):
            safe[key] = item
    return safe


class HookWorkerServices:
    def __init__(self, connection: HookConnectionManager, projection: NativeProjectionStore, *,
                 intake_interval_seconds: float, projection_batch_size: int, dispatcher=None,
                 recovery_window_check=None) -> None:
        if (type(intake_interval_seconds) not in {int, float}
                or not math.isfinite(intake_interval_seconds) or not 0 < intake_interval_seconds <= 5
                or type(projection_batch_size) is not int or not 1 <= projection_batch_size <= 1000):
            raise ValueError("HOOK_INTAKE_LIMIT_INVALID")
        self.connection, self.projection = connection, projection
        self.dispatcher = dispatcher
        self._recovery_window_check = recovery_window_check
        self.intake_interval_seconds = intake_interval_seconds
        self.projection_batch_size = projection_batch_size
        self._credentials: HookWorkerCredentials | None = None
        self._failure: Callable[[], None] | None = None
        self._last_error: str | None = None
        self._startup_position_payload: dict | None = None
        self._startup_window_key = None
        self._startup_check = startup_check_state.checking()
        self._display_fault: dict | None = None
        self._had_fault=False

    def attach(self, binding, on_persistence_failure: Callable[[], None]) -> None:
        self._had_fault=binding.run_status=='faulted'
        credentials = HookWorkerCredentials(binding.worker_id, binding.worker_token, binding.client_instance_id)
        if self._credentials is not None and self._credentials != credentials:
            raise ValueError("HOOK_RUNTIME_REBIND_REQUIRES_STOP")
        self._credentials, self._failure = credentials, on_persistence_failure
        def persistence_failed():
            self._startup_check = startup_check_state.failed('HOOK_MESSAGE_SAVE_FAILED')
            self._display_fault = dict(self._startup_check)
            on_persistence_failure()
        self.connection.on_persistence_failure = persistence_failed

    def tick(self, runner) -> None:
        """Connection/heartbeat only. Pause and fault never suspend raw persistence."""
        binding = runner.binding
        if binding is None or runner.stop_event.is_set():
            return
        runner._sync_pending_run_status()
        if emergency_stop_requested():
            return  # Existing approved receiver still persists independently.
        with runner._run_status_intent_lock:
            revision = runner._run_status_revision
            captured = HookWorkerCredentials(binding.worker_id, binding.worker_token, binding.client_instance_id)
        if captured != self._credentials:
            raise ValueError("HOOK_RUNTIME_BINDING_CHANGED")
        summary = ui_lock.lock_summary()
        # Lifecycle readiness is not permission for reply generation or sending.
        summary["capabilities"] = {"reply_sequence_version": 1}
        state = HookHeartbeatState("running" if runner.current_task else "idle", "ready", "unknown",
            runner.current_task.id if runner.current_task else None, runner._current_step,
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        if not self.health_snapshot()["ready"]:
            self._startup_check = startup_check_state.failed("HOOK_RECEIVER_STOPPED")
            runner._hook_persistence_failed()
            return
        register_progress(runner,'startup',group='startup',kind='startup')
        emit_progress(runner,'startup','environment')
        try:
            prepared = self.connection.prepare(captured, state)
        except ApiError as exc:
            if exc.code in BACKEND_WAIT_CODES:
                self._startup_check = startup_check_state.failed(exc.code)
                emit_progress(runner,"connection","connection_wait","waiting","connection_wait")
                runner.on_status("connecting")
                return  # Preserve same-account receiver; no work is admitted.
            self._fault(runner, exc.code, http_status=exc.status_code, startup_phase="environment")
            return
        except ui_lock.UiLockError as exc:
            if exc.code in {ui_lock.UI_LOCK_BUSY, ui_lock.UI_LOCK_ACQUIRE_TIMEOUT}:
                return  # An existing physical action owns this same lock.
            self._fault(runner, "HOOK_ACTION_LOCK_FAILED", startup_phase="environment")
            return
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not code.startswith("HOOK_"):
                code = "HOOK_CONNECTION_CHECK_FAILED"
            self._fault(runner, code, local_error=exc, startup_phase="environment")
            return
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in" if prepared else "logged_out"
        if prepared is None:
            self._startup_check = startup_check_state.failed("HOOK_NOT_LOGGED_IN")
            runner.on_status("waiting_wechat")
            return  # A healthy installation may wait for a manual login.
        self._startup_check = startup_check_state.passed()
        if not prepared.process.window_available_for_actions:
            self._startup_check = startup_check_state.failed('HOOK_WINDOW_UNAVAILABLE')
        if self.dispatcher is not None and not getattr(self.dispatcher, 'automatic_transcription_disabled', lambda: False)():
            self._startup_check = startup_check_state.failed('HOOK_NATIVE_AUTO_TEXT_UNCONFIRMED')
        if self.dispatcher is not None:
            try:
                prepared.clock.observe(prepared.account)
            except ValueError as exc:
                code = str(exc)
                if code not in CLOCK_WAIT_CODES:
                    self._fault(runner, code)
                    return
                self._wait_for_clock(code, clock_check=getattr(exc, "clock_check", None))
        profile = prepared.account.worker_profile()
        if self.dispatcher is not None and self._startup_check['canStart']:
            key = self._window_key(prepared)
            if key != self._startup_window_key:
                self._startup_position_payload = None
                # A retained physical flow must settle before startup may move
                # its window. Raw callback persistence and recovery stay alive.
                flow = profile.inflight_flow_state
                local_flow = storage.load_runtime_control().get('inflight_flow_id')
                if local_flow or (isinstance(flow, dict) and flow.get('flow_id')):
                    self._startup_check = startup_check_state.failed('HOOK_STARTUP_WINDOW_PENDING')
                else:
                    try:
                        check_retry = getattr(self.dispatcher, 'check_startup_retry', None)
                        if check_retry is not None:
                            check_retry(prepared)
                        emit_progress(runner,'startup','wechat_window')
                        self.dispatcher.prepare_startup_window(prepared, self.connection.gateway)
                    except StartupWindowWait as exc:
                        self._startup_check = startup_check_state.failed(exc.code)
                        emit_progress(runner,'startup','wechat_window','waiting',
                                      {'HOOK_STARTUP_FOREGROUND_REQUIRED':'startup_foreground',
                                       'HOOK_STARTUP_RETRY_WAIT':'startup_retry',
                                       'HOOK_STARTUP_WINDOW_OBSERVATION_FAILED':'startup_observation',
                                       'HOOK_STARTUP_RETRY_EXHAUSTED':'startup_exhausted'}[exc.code])
                    except ui_lock.UiLockError as exc:
                        if exc.code in {ui_lock.UI_LOCK_BUSY, ui_lock.UI_LOCK_ACQUIRE_TIMEOUT}:
                            self._startup_check = startup_check_state.failed('HOOK_STARTUP_WINDOW_PENDING')
                        else:
                            self._fault(runner,'HOOK_ACTION_LOCK_FAILED');return
                    except Exception as exc:
                        code = getattr(exc,'code',None) or str(exc)
                        emit_progress(runner,'startup','wechat_window','error','failed')
                        self._fault(runner,code if isinstance(code,str) and code.startswith('HOOK_') else 'HOOK_C1_STARTUP_WINDOW_UNVERIFIED',local_error=exc)
                        return
                    else:
                        self._startup_window_key = key
                        emit_progress(runner,'startup','wechat_window','done')
            if self._startup_window_key == key and not self._startup_layout_ready():
                self._startup_check = startup_check_state.failed('HOOK_SEARCH_LAYOUT_RESTART_REQUIRED')
                self._startup_position_payload = None
            if self.startup_window_ready_for(prepared):
                self._startup_position_payload = inspect_windows_window_geometry(prepared.process)
                if self._startup_position_payload is None:
                    self._startup_check = startup_check_state.failed('HOOK_WINDOW_UNAVAILABLE')
        # Original recovery also observes these readiness fields. A pending
        # window must not appear recoverable before it has been prepared.
        runner.last_rpa_component_status = 'ready' if self._startup_check['canStart'] else 'unavailable'
        with runner._run_status_intent_lock:
            current = runner.binding
            if current is not binding or revision != runner._run_status_revision:
                return  # A slow heartbeat must not overwrite a newer local pause.
            if profile.run_status in {"paused", "faulted"} and current.run_status != profile.run_status:
                runner._preserve_stopped_run_status(profile.run_status)
            runner._backend_confirmed_run_status = profile.run_status
            runner._backend_inflight_flow_state = dict(profile.inflight_flow_state)
            runner._backend_fault_recovery = dict(profile.fault_recovery)
            runner._backend_pending_read_recovery = dict(profile.pending_read_recovery)
            # Never turn a local stopped state into running on a heartbeat.
            profile.run_status = current.run_status
            if current.run_status != "faulted":
                self._display_fault = None
                if getattr(self,'_had_fault',False) and current.run_status=='running':
                    # Only an actual confirmed operator recovery clears this
                    # global display. Readiness/heartbeat alone cannot do it.
                    _deliver(runner,{'event':'hook_progress_control','action':'fault_clear'})
                    self._had_fault=False
            runner.on_profile(profile)
        self._last_error = None
        runner._recovery_heartbeat_at = prepared.account.response_received_monotonic or 0
        runner.on_status("bound")
        # Clear this owner's recovered condition before consulting the aggregate
        # start gate: that gate includes the very wait we need to resolve.
        # Business waits/clock calibration and stopped intent remain independent.
        if self._startup_check['canStart']:
            resolve=getattr(self.dispatcher,'resolve_wait',None)
            if resolve is not None:resolve('connection')
            emit_progress(runner,'connection','connection_wait','done')
            emit_progress(runner,'connection','clock_wait','done')
        if self.startup_check_state()['canStart']:
            emit_progress(runner,'startup','environment','done')
            emit_progress(runner,'startup','ready','done')

    def _wait_for_clock(self, code: str, *, context='connection', clock_check=None) -> None:
        # Keep authenticated raw intake alive, but show that new work is blocked.
        # Do not clear an earlier fault or change the operator's run-status intent.
        if context=='connection':self._startup_check = startup_check_state.failed(code)
        self.dispatcher._report_wait(code,context=context,clock_check=clock_check)

    def _fault(self, runner, code: str, *, http_status=None, local_error=None, startup_phase=None) -> None:
        if isinstance(local_error, sqlite3.Error):
            code = 'HOOK_LOCAL_STORAGE_FAILED'
            # Durable recording is a prerequisite for every new physical action.
            # Signal all loops before teardown waits for an in-flight writer.
            trigger_emergency_stop(reason=code, origin='hook_storage')
            runner.stop_event.set()
        operation=getattr(local_error,'operation',None)
        kind=getattr(local_error,'transport_kind',None)
        if not isinstance(operation,str) or operation not in {'IsLoginStatus','ConfigureMsgReciveFullURL','GetDBHandleAndPath','QueryDB','SendTextMsg','GetFriendAndChatRoomList','Voice2Text'}:operation=None
        if not isinstance(kind,str) or kind not in {'timeout','connection_error','request_error'}:kind=None
        detail=startup_check_state.failed(code,operation=operation,transport_kind=kind)
        if startup_phase is not None:
            emit_progress(runner,'startup',startup_phase,'error','interrupted')
        context=getattr(getattr(self.dispatcher,'runtime',None),'_progress_context',None)
        prior=getattr(runner,'_hook_progress_contexts',{}).get(context,{})
        # Without a matching call identity, a prior fault is not evidence of
        # this quarantine's cause. Preserve its log, show the current diagnosis.
        register_progress(runner,'fault:'+code,group='system',kind='system')
        emit_progress(runner,'fault:'+code,'stopped','error','interrupted',
            description=detail['title']+'。'+detail['message'],affected_group=prior.get('group'))
        self._startup_check = detail
        self._display_fault = dict(detail)
        self._had_fault=True
        runner.last_rpa_component_status = "unavailable"
        runner._preserve_stopped_run_status("faulted", repeated_observation=True)
        if self._last_error != code:
            clock = getattr(getattr(self.connection, 'prepared', None), 'clock', None)
            message = (detail['title'] + '。' + detail['message'] if code == 'HOOK_LOCAL_STORAGE_FAILED' or code in ACCOUNT_ISSUES
                else "工作微信连接异常，已停止接单；请检查微信后恢复。")
            try:
                storage.append_log("ERROR", "hook_connection_fault", message,
                    error_code=code, include_exception_text=False,
                    metadata={**dict(getattr(self.dispatcher,'diagnostic_context',{})),
                        'backend_http_status':http_status if type(http_status) is int else None,
                        'local_operation':operation,'transport_kind':kind,
                        'protocol_check':_protocol_check(local_error),
                        'clock_check':dict(getattr(clock, 'failure_detail', None) or {})})
            except sqlite3.Error as exc:
                from .failure_evidence import record_capture_failure
                record_capture_failure('hook_connection_fault', exc, error_code=code)
                # A failed diagnostic write must not hide the original stop reason.
                trigger_emergency_stop(reason='HOOK_LOCAL_STORAGE_FAILED', origin='hook_fault_log')
                runner.stop_event.set()
            runner.on_error(message)
        self._last_error = code

    def intake_loop(self, stop: threading.Event, entered: Callable[[str], None]) -> None:
        entered("hook_intake")
        while not stop.is_set():
            prepared = self.connection.prepared
            if prepared is not None:
                for _ in range(self.projection_batch_size):
                    if stop.is_set() or self.projection.project_next(prepared.registration.scope) is None:
                        break
            stop.wait(self.intake_interval_seconds)

    def business_loop(self, runner, entered) -> None:
        entered('hook_business')
        while not runner.stop_event.is_set():
            if runner.binding is not None:
                try:
                    self.dispatcher.tick(runner,self)
                except HookRuntimeStopping:
                    # Only the explicit normal-stop signal takes this path.
                    # SQLite, lease and unknown-send failures still use existing gates.
                    if not runner.stop_event.is_set():
                        raise
                    return
                except ApiError as exc:
                    if exc.code in BACKEND_WAIT_CODES:
                        self.dispatcher._report_wait(exc.code)
                    else:self._fault(runner,exc.code,http_status=exc.status_code)
                except (ValueError, HookLocalError) as exc:
                    code=str(exc)
                    if isinstance(exc, ValueError) and code in CLOCK_WAIT_CODES:
                        self._wait_for_clock(code,context='business',clock_check=getattr(exc, 'clock_check', None))
                    elif is_history_wait(exc) or (isinstance(exc, ValueError) and code in {'HOOK_RUNTIME_NOT_RUNNING','HOOK_SEND_LATEST_CONTEXT_STALE'}):
                        # Retained-send reconciliation precedes the per-customer
                        # loop. Its changing read must wait through the same
                        # classification without stopping the whole Worker.
                        self.dispatcher._report_wait(code,context='business')
                    else:self._fault(runner,code if code.startswith('HOOK_') else 'HOOK_BUSINESS_CHECK_FAILED',local_error=exc)
                except sqlite3.Error as exc:
                    self._fault(runner, 'HOOK_LOCAL_STORAGE_FAILED', local_error=exc)
                except ui_lock.UiLockError as exc:
                    if exc.code not in {ui_lock.UI_LOCK_BUSY,ui_lock.UI_LOCK_ACQUIRE_TIMEOUT}:raise
                except Exception as exc:
                    code=getattr(exc,'code',None)
                    if not isinstance(code,str) or not code.startswith('HOOK_'):raise
                    self._fault(runner,code,local_error=exc)
            runner.stop_event.wait(self.dispatcher.interval_seconds)

    def fault_recovery_check(self) -> dict:
        """Fresh local check at the existing fault-recovery gate, never an action.

        Initial window placement is cached per process. It cannot prove that a
        later failed C1 left no dialog. Recovery is already behind settlement,
        heartbeat and unknown-result gates in TaskRunner.
        """
        check = self.startup_check_state()
        if not check['canStart']:
            return check
        if self._recovery_window_check is None:
            return check if self.dispatcher is None else startup_check_state.failed('HOOK_C1_STARTUP_WINDOW_UNVERIFIED')
        prepared = self.connection.prepared
        if prepared is None:
            return startup_check_state.failed('HOOK_CONNECTION_CHECK_FAILED')
        try:
            self.connection.gateway.generation_inspector(prepared.process)
            self._recovery_window_check(prepared.process)
            self.connection.gateway.generation_inspector(prepared.process)
        except Exception as exc:
            code = getattr(exc, 'code', None) or str(exc)
            if code not in startup_check_state.ISSUES:
                code = 'HOOK_C1_STARTUP_WINDOW_UNVERIFIED'
            return startup_check_state.failed(code)
        return check

    def startup_check_state(self) -> dict:
        value = dict(self._startup_check)
        if value['canStart'] and not self._startup_layout_ready():
            value = startup_check_state.failed('HOOK_SEARCH_LAYOUT_RESTART_REQUIRED')
        pending=self.dispatcher.pending_wait_snapshot() if hasattr(self.dispatcher,'pending_wait_snapshot') else getattr(self.dispatcher,'_pending_wait_phases',{}) if self.dispatcher else {}
        if value['canStart'] and any(pending.values()):
            phase='connection_wait' if any('connection_wait' in v for v in pending.values()) else 'clock_wait'
            value=startup_check_state.failed('HOOK_BACKEND_TEMPORARILY_UNAVAILABLE' if phase=='connection_wait' else 'HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED')
        if value['canStart'] and getattr(self.dispatcher,'_business_clock_ready',None) is False:
            code=getattr(self.dispatcher,'_business_clock_error',None)
            value=startup_check_state.failed(code) if code else {
                'state':'checking','title':'等待后台时间校验','message':'','canStart':False}
        version=getattr(self.dispatcher,'_observation_query_version',None)
        if value['canStart'] and type(version) is int and version!=1:
            value=startup_check_state.failed('HOOK_MODEL_OBSERVATION_UNSUPPORTED')
        storage_ready=getattr(self.dispatcher,'history_storage_ready',None)
        if value['canStart'] and callable(storage_ready) and not storage_ready():
            value=startup_check_state.failed('HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED')
        if self._display_fault:
            value['faultTitle'] = self._display_fault['title']
            value['faultMessage'] = self._display_fault['message']
        return value

    @staticmethod
    def _window_key(prepared):
        scope = prepared.registration.scope
        return (scope.self_wxid, scope.account_epoch, scope.process_generation)

    def startup_window_ready_for(self, prepared) -> bool:
        return (self._startup_window_key == self._window_key(prepared)
                and self._startup_check['canStart'] and self._startup_layout_ready())

    def _startup_layout_ready(self):
        return getattr(self.dispatcher,'startup_layout_ready',lambda:True)()

    def startup_window_position_payload(self) -> dict | None:
        # GUI reads a published snapshot; it must not wait on connection HTTP locks.
        payload = self._startup_position_payload
        return copy.deepcopy(payload)

    def health_snapshot(self) -> dict:
        return self.connection.receiver_health_snapshot()

    def close(self, *, deadline: float | None = None) -> None:
        self.connection.close(deadline=deadline)  # Wait for the actual callback SQLite commit.
        self._credentials = None
        self._failure = None
        self._startup_position_payload = None
        self._startup_window_key = None
        self._startup_check = startup_check_state.checking()
        self._display_fault = None
