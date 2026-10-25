"""Explicit transport selection for the original Worker UI and controls.

Settings are deployment-owned and protected by the existing updater snapshot.
The default remains OCR. Hook never falls back after a configuration failure,
launches WeChat, or replaces the original Start/Pause/rebind/error UI.
"""
from pathlib import Path
from contextlib import contextmanager
import time
import os
from . import storage
from .config import CONFIG
from .hook_config import load_hook_deployment, TRANSPORT_KEY, VOICE_SETTING_KEY
from .hook_backend import HookIdentityClient
from .hook_gateway import HookGateway
from .hook_inbox import NativeInbox
from .hook_projection import create_projection_store
from .hook_connection import HookConnectionManager
from .hook_worker_services import HookWorkerServices
from .hook_formal_client import HookFormalClient
from .hook_dispatcher import HookBusinessDispatcher
from .shared_rules import hook_contract_rules as rules
from .hook_startup_window import StartupWindowAttempts, StartupWindowError, StartupWindowWait, PREPARE_FAILURES



def _setting(name):
    with storage.db_connection() as conn:
        row=conn.execute('SELECT value FROM client_settings WHERE key=?',(name,)).fetchone()
    return None if row is None else rules.loads(row['value'])


def create_hook_services(api):
    selection=_setting(TRANSPORT_KEY)
    if selection is None or selection=={'mode':'ocr'}:return None
    if selection!={'mode':'hook'}:raise ValueError('HOOK_TRANSPORT_SELECTION_INVALID')
    deployment=load_hook_deployment()
    private=CONFIG.app_dir/'transactions'/'hook'
    gateway=HookGateway(deployment,timeout_seconds=60,max_response_bytes=2*1024*1024,lock_timeout_seconds=20)
    backend=HookIdentityClient(api.base_url,CONFIG.api_timeout_seconds,2*1024*1024)
    connection=HookConnectionManager(gateway,backend,
        NativeInbox(max_body_bytes=2*1024*1024,max_inbox_bytes=128*1024*1024),
        receiver_timeout_seconds=10,on_persistence_failure=lambda:None)
    formal=HookFormalClient(api.base_url,CONFIG.api_timeout_seconds)
    native=WindowsC1(private, artifact_root=CONFIG.app_dir/'artifacts')
    def automatic_text_disabled():
        proof=_setting(VOICE_SETTING_KEY)
        return proof=={'deployment_revision':deployment.revision,'disabled':True,'operator_confirmed':True}
    dispatcher=HookBusinessDispatcher(formal,private,native.ui,native.startup,
        automatic_transcription_disabled=automatic_text_disabled,
        startup_layout_check=native.startup_layout_ready, startup_retry_check=native.check_startup_retry)
    return HookWorkerServices(connection,create_projection_store(),
        intake_interval_seconds=.2,projection_batch_size=100,dispatcher=dispatcher,
        recovery_window_check=native.check_recovery_window)


class WindowsC1:
    def __init__(self,root,artifact_root=None):
        self.root=root
        self.artifact_root=artifact_root
        self._ops=None
        self._search_control_cache = {}
        self._startup_attempts = StartupWindowAttempts()
        self._startup_probe_error = None
    def startup_layout_ready(self):
        # NativeUI invalidates this shared map before raising. Readiness is
        # consumed by the original startup/recovery gates, without a screenshot.
        return bool(self._search_control_cache.get('key') and self._search_control_cache.get('bounds')
                    and self._search_control_cache.get('regions'))
    def _window(self,process):
        if self._ops is None:
            from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as ops
            if ops._OCR_IMPORT_ERROR:raise ValueError('HOOK_C1_OCR_RUNTIME_UNAVAILABLE')
            ops.UI_ACTION_GUARD_PATH=self.root/'ui-action-guard.json'
            ops.UI_ACTION_AUDIT_PATH=(self.artifact_root/'runtime'/'ui-action-audit.jsonl'
                if self.artifact_root is not None else self.root/'ui-action-audit.jsonl')
            ops.RENDER_RECOVERY_GUARD_PATH=self.root/'render-recovery.json'
            ops._OCR_ENGINE=ops.RapidOCR()
            self._ops=ops
        ops=self._ops
        # All rectangles are physical pixels, including 125% desktop scaling.
        import ctypes
        if not ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
            raise ValueError('HOOK_C1_DPI_CONTEXT_UNAVAILABLE')
        return ops, self._main_window(ops.win32gui, ops.win32process, process)

    @staticmethod
    def _main_window(gui, processes, process):
        windows=[]
        def collect(hwnd,_):
            if (gui.IsWindowVisible(hwnd) and gui.GetClassName(hwnd)=='WeChatMainWndForPC'
                    and processes.GetWindowThreadProcessId(hwnd)[1]==process.pid):windows.append(hwnd)
        gui.EnumWindows(collect,None)
        if len(windows)!=1:raise ValueError('HOOK_C1_MAIN_WINDOW_AMBIGUOUS')
        return windows[0]

    def _foreground_ready(self, process):
        # This observation must not initialize OCR, set DPI, or activate UI.
        if self._ops is None:
            import win32gui as gui
            import win32process as processes
        else:
            gui, processes = self._ops.win32gui, self._ops.win32process
        hwnd = self._main_window(gui, processes, process)
        return gui.GetForegroundWindow() == hwnd and not gui.IsIconic(hwnd)

    def _startup_diagnostics(self, identifier):
        from .c1_diagnostics import C1Diagnostics
        from .evidence_paths import task_artifacts
        folder = (task_artifacts(self.artifact_root.parent, identifier)
            if self.artifact_root is not None else self.root/'roi'/identifier)
        return C1Diagnostics(folder, identifier)
    def check_recovery_window(self, process):
        """Read-only; reuse startup/C1 popup classification without activating UI."""
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1 import classify_visible_other_windows
        ops, hwnd = self._window(process)
        if ops.win32gui.IsIconic(hwnd):
            raise ValueError('HOOK_WINDOW_UNAVAILABLE')
        if classify_visible_other_windows(ops, hwnd, process.pid)['blocking_window_details']:
            raise ValueError('HOOK_C1_POPUP_PENDING')

    def check_startup_retry(self, process, scope):
        attempts = self._startup_attempts
        attempts.select(process, scope)
        def foreground_ready():
            started = time.monotonic()
            try:
                ready = self._foreground_ready(process)
                self._startup_probe_error = None
                return ready
            except Exception as exc:
                signature = (attempts.key, type(exc).__name__, str(exc))
                if self._startup_probe_error != signature:
                    import uuid
                    diagnostics = self._startup_diagnostics('startup-window-observation-' + uuid.uuid4().hex)
                    diagnostics.write('window-observation.json', {
                        'status': 'failed', 'phase': 'foreground_observation',
                        'elapsed_ms': round((time.monotonic()-started)*1000, 3),
                        'exception': diagnostics.details(exc), 'read_only': True})
                    self._startup_probe_error = signature
                raise StartupWindowWait('HOOK_STARTUP_WINDOW_OBSERVATION_FAILED') from exc
        # No activation, screenshot, or OCR while waiting for the operator.
        attempts.check(foreground_ready)

    def startup(self,process,gateway,scope):
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1 import prepare_window
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1_flow import NativeUI
        import uuid
        attempts = self._startup_attempts
        self.check_startup_retry(process, scope)
        with gateway._locked(process,'hook_startup_window') as lease:
            self.check_startup_retry(process, scope)
            allow_activation = attempts.begin()
            self._search_control_cache.clear()
            calibration_id = 'startup-search-' + uuid.uuid4().hex
            diagnostics = self._startup_diagnostics(calibration_id)
            evidence = diagnostics.folder
            report = {'attempt': attempts.count, 'allow_activation': allow_activation,
                      'phases': [], 'status': 'checking'}
            @contextmanager
            def phase(name):
                entry = {'name': name, 'started_at_unix': time.time()}
                report['phases'].append(entry)
                start = time.monotonic()
                try:
                    yield
                    entry['status'] = 'passed'
                except Exception:
                    entry['status'] = 'failed'
                    raise
                finally:
                    entry['elapsed_ms'] = round((time.monotonic()-start)*1000, 3)
            ops = hwnd = ui = None
            def foreground_snapshot():
                if ops is None:
                    return {}
                try:
                    gui = ops.win32gui
                    fg = gui.GetForegroundWindow()
                    # No window titles or chat contents in metadata.
                    pid = ops.win32process.GetWindowThreadProcessId(fg)[1] if fg else None
                    return {'hwnd': fg, 'pid': pid, 'is_worker_process': pid == os.getpid(),
                            'class': gui.GetClassName(fg) if fg else None, 'is_wechat_main': fg == hwnd}
                except Exception as exc:
                    return {'read_error': type(exc).__name__}
            def guard():
                gateway._history_identity(process,scope)
                lease.raise_if_lost()
            try:
                with phase('account_before_window'):
                    guard()
                with phase('load_ocr_and_find_window'):
                    ops,hwnd=self._window(process)
                with phase('prepare_window'):
                    report['foreground_before'] = foreground_snapshot()
                    result=prepare_window(ops,hwnd,process.pid,artifact_dir=str(self.root/'startup'),
                                          capture_evidence=False, allow_activation=allow_activation)
                    report['window_result'] = result
                    report['foreground_after'] = foreground_snapshot()
                    if not result.get('ok'):
                        raise StartupWindowError(PREPARE_FAILURES.get(result.get('reason'),
                                                                     'HOOK_C1_STARTUP_WINDOW_UNVERIFIED'))
                with phase('account_after_window'):
                    guard()
                with phase('search_calibration'):
                    ui = NativeUI(ops,hwnd,process.pid,evidence,guard,organized_evidence=True,
                        diagnostics=diagnostics,search_control_cache=self._search_control_cache,
                        activate_on_init=False)
                    ui.calibrate_search_control()
                with phase('account_after_calibration'):
                    guard()
                report['status'] = 'passed'
                attempts.completed()
            except Exception as exc:
                attempts.failed()
                self._search_control_cache.clear()
                if diagnostics.initialization_report is None:
                    diagnostics.initialization_failure(exc, ui)
                report['status'] = 'failed'
                report['exception'] = diagnostics.details(exc, ui)
                failures = diagnostics.initialization_report.get('guard_failures', [])
                if failures and failures[-1].get('condition') == 'foreground_changed':
                    raise StartupWindowError('HOOK_STARTUP_FOREGROUND_REQUIRED') from exc
                if failures and failures[-1].get('condition') == 'frame_expired':
                    raise StartupWindowError('HOOK_STARTUP_FRAME_EXPIRED') from exc
                raise
            finally:
                report['evidence'] = getattr(ui, 'evidence_state',
                    (diagnostics.initialization_report or {}).get('evidence', {'frames': [], 'failures': []}))
                diagnostics.write('startup-check.json', report)
    def ui(self,process,preparation,guard):
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1_flow import NativeUI
        if self.artifact_root is not None:
            from .evidence_paths import task_artifacts
            evidence=task_artifacts(self.artifact_root.parent,preparation['task_id'])
        else:
            evidence=self.root/'roi'/preparation['task_id']
        from .c1_diagnostics import C1Diagnostics
        diagnostics=C1Diagnostics(evidence, preparation['task_id'])
        try:
            ops,hwnd=self._window(process)
        except Exception as exc:
            diagnostics.initialization_failure(exc)
            raise
        return NativeUI(ops,hwnd,process.pid,evidence,guard,
            organized_evidence=self.artifact_root is not None, diagnostics=diagnostics,
            search_control_cache=self._search_control_cache, require_startup_search_cache=True)
