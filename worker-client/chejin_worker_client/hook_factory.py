"""Explicit transport selection for the original Worker UI and controls.

Settings are deployment-owned and protected by the existing updater snapshot.
The default remains OCR. Hook never falls back after a configuration failure,
launches WeChat, or replaces the original Start/Pause/rebind/error UI.
"""
from pathlib import Path
from . import storage
from .config import CONFIG
from .hook_config import load_hook_deployment, TRANSPORT_KEY, VOICE_SETTING_KEY
from .hook_backend import HookIdentityClient
from .hook_gateway import HookGateway
from .hook_inbox import NativeInbox
from .hook_projection import NativeProjectionStore
from .hook_connection import HookConnectionManager
from .hook_worker_services import HookWorkerServices
from .hook_formal_client import HookFormalClient
from .hook_dispatcher import HookBusinessDispatcher
from .shared_rules import hook_contract_rules as rules



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
    native=WindowsC1(private)
    def automatic_text_disabled():
        proof=_setting(VOICE_SETTING_KEY)
        return proof=={'deployment_revision':deployment.revision,'disabled':True,'operator_confirmed':True}
    dispatcher=HookBusinessDispatcher(formal,private,native.ui,native.startup,
        automatic_transcription_disabled=automatic_text_disabled)
    return HookWorkerServices(connection,NativeProjectionStore(max_projection_bytes=128*1024*1024,max_entries=200),
        intake_interval_seconds=.2,projection_batch_size=100,dispatcher=dispatcher)


class WindowsC1:
    def __init__(self,root):self.root=root;self._ops=None
    def _window(self,process):
        if self._ops is None:
            from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as ops
            if ops._OCR_IMPORT_ERROR:raise ValueError('HOOK_C1_OCR_RUNTIME_UNAVAILABLE')
            ops.UI_ACTION_GUARD_PATH=self.root/'ui-action-guard.json'
            ops.UI_ACTION_AUDIT_PATH=self.root/'ui-action-audit.jsonl'
            ops.RENDER_RECOVERY_GUARD_PATH=self.root/'render-recovery.json'
            ops._OCR_ENGINE=ops.RapidOCR()
            self._ops=ops
        ops=self._ops
        # All rectangles are physical pixels, including 125% desktop scaling.
        import ctypes
        if not ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
            raise ValueError('HOOK_C1_DPI_CONTEXT_UNAVAILABLE')
        windows=[]
        def collect(hwnd,_):
            if (ops.win32gui.IsWindowVisible(hwnd) and ops.win32gui.GetClassName(hwnd)=='WeChatMainWndForPC'
                    and ops.win32process.GetWindowThreadProcessId(hwnd)[1]==process.pid):windows.append(hwnd)
        ops.win32gui.EnumWindows(collect,None)
        if len(windows)!=1:raise ValueError('HOOK_C1_MAIN_WINDOW_AMBIGUOUS')
        return ops,windows[0]
    def startup(self,process,gateway,scope):
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1 import prepare_window
        with gateway._locked(process,'hook_startup_window') as lease:
            gateway._history_identity(process,scope)
            ops,hwnd=self._window(process)
            result=prepare_window(ops,hwnd,process.pid,artifact_dir=str(self.root/'startup'),capture_evidence=False)
            gateway._history_identity(process,scope)
            lease.raise_if_lost()
            if not result.get('ok'):raise ValueError('HOOK_C1_STARTUP_WINDOW_UNVERIFIED')
    def ui(self,process,preparation,guard):
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1_flow import NativeUI
        ops,hwnd=self._window(process)
        return NativeUI(ops,hwnd,process.pid,self.root/'roi'/preparation['task_id'],guard)
