"""Native UI harness; lifecycle/ABI tests, not a substitute for Windows rendering."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import threading

import pytest

from chejin_worker_client import update_progress_window as native
from chejin_worker_client.update_progress import UpdateProgress, LABELS
from chejin_worker_client.update_progress_design import BRAND_PATH, window_bounds, paint


class NativeHarness:
    def __init__(self, event='close', fail_paint=False):
        self.calls, self.functions = [], {}
        self.callback = None
        self.event, self.fail_paint = event, fail_paint
        self.alive, self.dispatched = False, False
        self.handle = 2**40
        self.next_object = self.handle+100
        self.owned = set()

    def __getattr__(self, name):
        if name not in self.functions:
            def fn(*args):
                self.calls.append((name,args))
                if name == 'RegisterClassW':
                    self.callback = c.cast(args[0],c.POINTER(native.WindowClass)).contents.procedure
                if name == 'SystemParametersInfoW':
                    rect=c.cast(args[2],c.POINTER(w.RECT)).contents
                    rect.left,rect.top,rect.right,rect.bottom=0,0,1920,1080
                if name == 'CreateWindowExW':
                    self.alive=True
                    return self.handle
                if name == 'GetDpiForSystem': return 144
                if name == 'IsWindow': return self.alive
                if name == 'DestroyWindow': self.alive=False
                if name == 'GetClientRect':
                    rect=c.cast(args[1],c.POINTER(w.RECT)).contents
                    rect.right,rect.bottom=600,450
                if name == 'PeekMessageW':
                    if self.dispatched:return 0
                    self.dispatched=True
                    return 1
                if name == 'DispatchMessageW':
                    assert self.callback(self.handle,0x21,0,0)==3
                    assert self.callback(self.handle,0x14,0,0)==1
                    if self.event=='paint': self.callback(self.handle,0x0f,0,0)
                    if self.event=='x':
                        # Mouse-up alone must not close it, then a genuine click does.
                        point=570 | (20 << 16)
                        self.callback(self.handle,0x202,0,point);assert self.alive
                        self.callback(self.handle,0x201,0,point)
                        self.callback(self.handle,0x202,0,point)
                    else:self.callback(self.handle,0x10,0,0)
                if name=='CreateCompatibleBitmap' and self.fail_paint:return 0
                if name in {'CreateSolidBrush','CreatePen','CreateFontW','CreateCompatibleDC',
                            'CreateCompatibleBitmap','LoadImageW','CreateRoundRectRgn'}:
                    self.next_object+=1;self.owned.add(self.next_object);return self.next_object
                if name in {'DeleteObject','DeleteDC'}: self.owned.remove(args[0])
                if name == 'SetWindowRgn': self.owned.remove(args[1]) # OS owns region now.
                if name in {'BeginPaint','GetModuleHandleW','LoadCursorW','SelectObject'}:return self.handle
                if name == 'SetThreadDpiAwarenessContext':return 0 # no context to restore in harness
                return 1
            self.functions[name]=fn
        return self.functions[name]


@pytest.mark.parametrize('event',['close','x','paint'])
def test_native_lifecycle_dismissal_never_finishes_phase(tmp_path, monkeypatch,event):
    harness=NativeHarness(event)
    monkeypatch.setattr(c,'WinDLL',lambda *a,**k:harness,raising=False)
    progress=UpdateProgress(tmp_path/'plan.json','updater');progress.begin('replace')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    progress._window()
    assert progress.phase_active and not progress.closed.is_set()
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    creation=[args for name,args in harness.calls if name=='CreateWindowExW']
    assert len(creation)==1 and creation[0][0]==0x08000000
    assert creation[0][10]==harness.handle # pointer-sized module handle
    assert harness.CreateWindowExW.restype==w.HWND
    assert harness.SelectObject.restype==w.HGDIOBJ
    assert harness.DefWindowProcW.restype==c.c_ssize_t
    assert ('ShowWindow',(harness.handle,4)) in harness.calls
    assert not harness.owned
    assert any(name=='UnregisterClassW' for name,_ in harness.calls)


def test_paint_failure_cleans_resources_and_preserves_business(tmp_path,monkeypatch):
    harness=NativeHarness('paint',True)
    monkeypatch.setattr(c,'WinDLL',lambda *a,**k:harness,raising=False)
    progress=UpdateProgress(tmp_path/'plan.json','updater')
    with progress.stage('replace'):
        progress._window()
    assert not harness.owned
    assert not (tmp_path/'update-result.json').exists()
    assert any(name=='EndPaint' for name,_ in harness.calls)
    assert (progress.phase,'show_window') in progress.reported_failures


@pytest.mark.parametrize('dpi',[96,120,144,192])
@pytest.mark.parametrize('work',[(0,0,1920,1040),(-1280,0,0,720),(0,0,640,400)])
def test_scaled_window_stays_in_available_desktop(dpi,work):
    x,y,width,height=window_bounds(work,dpi)
    assert work[0]<=x and work[1]<=y
    assert x+width<=work[2] and y+height<=work[3]
    assert abs(width/height-4/3)<.01


def test_design_uses_packaged_brand_and_real_phase_labels():
    assert BRAND_PATH.is_file() and BRAND_PATH.read_bytes()[:2]==b'BM'
    class Recorder:
        def __init__(self):self.texts=[]
        def text(self,box,text,*args):
            assert box[0]>=0 and box[0]+box[2]<=400
            assert box[1]>=0 and box[1]+box[3]<=300
            self.texts.append(text)
        def __getattr__(self,name):return lambda *args:None
    for label in LABELS.values():
        recorder=Recorder();paint(recorder,label,1234,180)
        assert recorder.texts==['车金 Worker 更新',label,'已等待 1234 秒']


def test_dpi_change_clamps_suggested_position_and_size():
    x,y,width,height=window_bounds((-640,0,0,400),192,(-100,350))
    assert x==-533 and y==0 and width==533 and height==400
