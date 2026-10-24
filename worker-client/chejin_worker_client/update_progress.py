"""Best-effort upgrade progress. Never grants installation or data access."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time

_startup_progress = None

LABELS = {
    'old_exit': '正在等待旧客户端安全退出',
    'data_lock': '正在保护本机数据',
    'data_snapshot': '正在核对绑定和未完成记录',
    'package_check': '正在校验新程序文件',
    'replace': '正在切换程序',
    'new_start': '正在启动新客户端',
    'startup_files': '新客户端正在检查程序完整性',
    'startup_data': '新客户端正在核对原数据',
    'startup_ui': '正在打开新客户端窗口',
    'window_shown': '新客户端窗口已打开，正在完成检查',
    'rollback': '升级未完成，正在恢复原客户端',
}


class UpdateProgress:
    def __init__(self, plan_path: Path, role: str):
        if role not in {'updater', 'worker'}:
            raise ValueError('invalid progress role')
        self.root = plan_path.parent
        self.role = role
        self.phase = ''
        self.phase_active = False
        self.started = time.monotonic()
        self.closed = threading.Event()
        self.thread = None

    def _write(self, phase, event, elapsed_ms=0):
        try:
            from .storage_maintenance import append_bounded_log
            record = {'phase': phase, 'event': event, 'elapsed_ms': elapsed_ms,
                      'timestamp_epoch': time.time(), 'pid': os.getpid()}
            text = json.dumps(record, ensure_ascii=True)
            path = self.root / (self.role + '-progress.json')
            temporary = path.with_suffix('.tmp')
            temporary.write_text(text, encoding='utf-8')
            os.replace(temporary, path)
            append_bounded_log(path.with_suffix('.jsonl'), text + '\n')
        except Exception:
            pass

    def begin(self, phase):
        if phase not in LABELS:
            raise ValueError('invalid progress phase')
        self.finish()
        self.phase, self.started = phase, time.monotonic()
        self.phase_active = True
        self._write(phase, 'started')

    def finish(self, event='completed'):
        if self.phase_active:
            self._write(self.phase, event, round((time.monotonic() - self.started) * 1000))
            self.phase_active = False

    @contextmanager
    def stage(self, phase):
        self.begin(phase)
        try:
            yield
        except BaseException:
            self.finish('failed')
            raise
        else:
            self.finish()

    def text(self):
        phase = self.phase
        seconds = int(time.monotonic() - self.started)
        if phase == 'new_start':
            try:
                path = self.root / 'worker-progress.json'
                if path.stat().st_size <= 4096:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    # Presentation only: these records never affect health or rollback.
                    if data.get('phase') in {'startup_files', 'startup_data', 'startup_ui', 'window_shown'}:
                        phase = data['phase']
                        seconds = max(0, int(time.time() - float(data['timestamp_epoch'])))
            except Exception:
                pass
        return LABELS.get(phase, '正在准备升级') + f'\n\n本阶段已等待 {seconds} 秒\n请稍候，原绑定和业务记录将保留。'

    def show(self):
        if os.name == 'nt':
            try:
                self.thread = threading.Thread(target=self._window, name='update-progress', daemon=True)
                self.thread.start()
            except Exception:
                pass

    def close(self):
        # Closing the informational window cannot certify a running phase.
        self.finish("interrupted")
        self.closed.set()
        if self.thread is not None:
            try:
                self.thread.join(timeout=0.5)
            except RuntimeError:
                pass

    def _window(self):
        # Native controls avoid importing Qt into the standalone one-file updater.
        # Closing this informational window does not cancel or terminate installation.
        try:
            import ctypes as c
            from ctypes import wintypes as w
            u = c.WinDLL('user32', use_last_error=True)
            u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                         c.c_int, c.c_int, c.c_int, c.c_int,
                                         w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
            u.CreateWindowExW.restype = w.HWND
            u.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
            u.IsWindow.argtypes = [w.HWND]
            u.DestroyWindow.argtypes = [w.HWND]
            u.PeekMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
            u.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
            u.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
            u.DispatchMessageW.restype = c.c_ssize_t
            hwnd = u.CreateWindowExW(0x08000000, 'STATIC', '车金 Worker 更新',
                                    0x10C80000, -2147483648, -2147483648, 440, 180,
                                    None, None, None, None)
            if not hwnd:
                return
            label = u.CreateWindowExW(0, 'STATIC', self.text(), 0x50000000,
                                     20, 20, 395, 110, hwnd, None, None, None)
            try:
                msg = w.MSG()
                while not self.closed.wait(0.1) and u.IsWindow(hwnd):
                    while u.PeekMessageW(c.byref(msg), None, 0, 0, 1):
                        u.TranslateMessage(c.byref(msg))
                        u.DispatchMessageW(c.byref(msg))
                    if label:
                        u.SetWindowTextW(label, self.text())
            finally:
                if u.IsWindow(hwnd):
                    u.DestroyWindow(hwnd)
        except Exception:
            pass  # UI availability cannot decide upgrade success.


def record_window_shown(context):
    """Called on the first UI event-loop turn, not merely when EXE is launched."""
    global _startup_progress
    try:
        if not context or context.get('mode') != 'updated':
            return
        marker = Path(context['plan']['healthy_marker_path'])
        progress = UpdateProgress(marker.parent / 'update-plan.json', 'worker')
        previous = json.loads((marker.parent / 'worker-progress.json').read_text(encoding='utf-8'))
        if previous.get('phase') == 'startup_ui':
            elapsed = max(0, round((time.time() - float(previous['timestamp_epoch'])) * 1000))
            progress._write('startup_ui', 'completed', elapsed)
        if _startup_progress is not None:
            _startup_progress.phase_active = False
        progress._write('window_shown', 'completed')
    except Exception:
        pass
    finally:
        if _startup_progress is not None:
            _startup_progress.close()
            _startup_progress = None


def start_worker_progress(plan_path, plan):
    global _startup_progress
    progress = UpdateProgress(plan_path, 'worker')
    _startup_progress = progress
    if plan.get('restart_progress_protocol') != 1:
        progress.show()
    return progress
