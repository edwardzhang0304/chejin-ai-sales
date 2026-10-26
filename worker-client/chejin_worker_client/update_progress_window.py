"""Small opaque Win32 progress view; no Qt/WebEngine dependency in the updater.

Double buffering prevents erase flashes. Native drawing is supersampled for the
activity ring. The milky surface is intentionally opaque, including on Windows
10: unsupported backdrop effects cannot make the update window disappear.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import math
import time
import uuid

from .update_progress_design import WIDTH, HEIGHT, paint, window_bounds

LRESULT = c.c_ssize_t
WNDPROC = getattr(c, 'WINFUNCTYPE', c.CFUNCTYPE)(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WindowClass(c.Structure):
    _fields_ = [('style', w.UINT), ('procedure', WNDPROC), ('class_extra', c.c_int),
                ('window_extra', c.c_int), ('instance', w.HINSTANCE), ('icon', w.HICON),
                ('cursor', w.HANDLE), ('background', w.HBRUSH), ('menu', w.LPCWSTR),
                ('name', w.LPCWSTR)]


class MonitorInfo(c.Structure):
    _fields_ = [('size',w.DWORD),('monitor',w.RECT),('work',w.RECT),('flags',w.DWORD)]


class PaintStruct(c.Structure):
    _fields_ = [('dc', w.HDC), ('erase', w.BOOL), ('rect', w.RECT),
                ('restore', w.BOOL), ('update', w.BOOL), ('reserved', c.c_byte * 32)]


def _bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


def _apis():
    u, g = c.WinDLL('user32', use_last_error=True), c.WinDLL('gdi32', use_last_error=True)
    ptr = c.POINTER
    for name, result, args in (
        ('MonitorFromWindow',w.HANDLE,(w.HWND,w.DWORD)),
        ('GetMonitorInfoW',w.BOOL,(w.HANDLE,ptr(MonitorInfo))),
        ('LoadCursorW', w.HANDLE, (w.HINSTANCE, c.c_void_p)),
        ('RegisterClassW', w.ATOM, (ptr(WindowClass),)),
        ('UnregisterClassW', w.BOOL, (w.LPCWSTR, w.HINSTANCE)),
        ('CreateWindowExW', w.HWND, (w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,c.c_int,c.c_int,c.c_int,c.c_int,w.HWND,w.HMENU,w.HINSTANCE,w.LPVOID)),
        ('DefWindowProcW', LRESULT, (w.HWND,w.UINT,w.WPARAM,w.LPARAM)),
        ('DestroyWindow', w.BOOL, (w.HWND,)), ('IsWindow', w.BOOL, (w.HWND,)),
        ('ShowWindow', w.BOOL, (w.HWND,c.c_int)),
        ('GetClientRect', w.BOOL, (w.HWND,ptr(w.RECT))),
        ('BeginPaint', w.HDC, (w.HWND,ptr(PaintStruct))),
        ('EndPaint', w.BOOL, (w.HWND,ptr(PaintStruct))),
        ('InvalidateRect', w.BOOL, (w.HWND,ptr(w.RECT),w.BOOL)),
        ('FillRect', c.c_int, (w.HDC,ptr(w.RECT),w.HBRUSH)),
        ('DrawTextW', c.c_int, (w.HDC,w.LPCWSTR,c.c_int,ptr(w.RECT),w.UINT)),
        ('PeekMessageW', w.BOOL, (ptr(w.MSG),w.HWND,w.UINT,w.UINT,w.UINT)),
        ('TranslateMessage', w.BOOL, (ptr(w.MSG),)),
        ('DispatchMessageW', LRESULT, (ptr(w.MSG),)),
        ('SystemParametersInfoW', w.BOOL, (w.UINT,w.UINT,w.LPVOID,w.UINT)),
        ('SetWindowPos', w.BOOL, (w.HWND,w.HWND,c.c_int,c.c_int,c.c_int,c.c_int,w.UINT)),
        ('SetWindowRgn', c.c_int, (w.HWND,w.HRGN,w.BOOL)),
        ('LoadImageW', w.HANDLE, (w.HINSTANCE,w.LPCWSTR,w.UINT,c.c_int,c.c_int,w.UINT)),
    ):
        _bind(u, name, result, *args)
    for name, result, args in (
        ('CreateCompatibleDC', w.HDC,(w.HDC,)),
        ('CreateCompatibleBitmap', w.HBITMAP,(w.HDC,c.c_int,c.c_int)),
        ('SelectObject', w.HGDIOBJ,(w.HDC,w.HGDIOBJ)),
        ('DeleteObject', w.BOOL,(w.HGDIOBJ,)), ('DeleteDC', w.BOOL,(w.HDC,)),
        ('CreateSolidBrush', w.HBRUSH,(w.COLORREF,)),
        ('CreatePen', w.HPEN,(c.c_int,c.c_int,w.COLORREF)),
        ('CreateFontW', w.HFONT,(c.c_int,c.c_int,c.c_int,c.c_int,c.c_int,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.LPCWSTR)),
        ('SetBkMode', c.c_int,(w.HDC,c.c_int)),
        ('SetTextColor', w.COLORREF,(w.HDC,w.COLORREF)),
        ('MoveToEx', w.BOOL,(w.HDC,c.c_int,c.c_int,ptr(w.POINT))),
        ('LineTo', w.BOOL,(w.HDC,c.c_int,c.c_int)),
        ('SetStretchBltMode', c.c_int,(w.HDC,c.c_int)),
        ('StretchBlt', w.BOOL,(w.HDC,c.c_int,c.c_int,c.c_int,c.c_int,w.HDC,c.c_int,c.c_int,c.c_int,c.c_int,w.DWORD)),
        ('CreateRoundRectRgn', w.HRGN,(c.c_int,c.c_int,c.c_int,c.c_int,c.c_int,c.c_int)),
    ):
        _bind(g,name,result,*args)
    return u,g


def _color(value):
    rgb = bytes.fromhex(value.lstrip('#'))
    return rgb[0] | rgb[1] << 8 | rgb[2] << 16


class Canvas:
    def __init__(self, u, g, dc, scale):
        self.u, self.g, self.dc, self.scale = u, g, dc, scale

    def n(self, value):
        return round(value*self.scale)

    def rect(self, box):
        x,y,width,height = box
        return w.RECT(self.n(x),self.n(y),self.n(x+width),self.n(y+height))

    def fill(self, box, color):
        brush = self.g.CreateSolidBrush(_color(color))
        if not brush: raise OSError('progress brush unavailable')
        try: self.u.FillRect(self.dc,c.byref(self.rect(box)),brush)
        finally: self.g.DeleteObject(brush)

    def _stroke(self, points, color, width):
        pen = self.g.CreatePen(0,max(1,self.n(width)),_color(color))
        if not pen: raise OSError('progress pen unavailable')
        old = self.g.SelectObject(self.dc,pen)
        try:
            self.g.MoveToEx(self.dc,self.n(points[0][0]),self.n(points[0][1]),None)
            for x,y in points[1:]: self.g.LineTo(self.dc,self.n(x),self.n(y))
        finally:
            self.g.SelectObject(self.dc,old)
            self.g.DeleteObject(pen)

    def line(self, ends, color, width):
        self._stroke((ends[:2],ends[2:]),color,width)

    def arc(self, box, color, width, start, sweep):
        x,y,a,b = box
        count = max(2,math.ceil(abs(sweep)))
        points = [(x+a/2+a/2*math.cos(math.radians(start+sweep*i/count)),
                   y+b/2+b/2*math.sin(math.radians(start+sweep*i/count))) for i in range(count+1)]
        self._stroke(points,color,width)

    def text(self, box, value, size, color, bold, center):
        font = self.g.CreateFontW(-self.n(size),0,0,0,500 if bold else 400,
                                  0,0,0,1,0,0,4,0,'Microsoft YaHei UI')
        if not font: raise OSError('progress font unavailable')
        old = self.g.SelectObject(self.dc,font)
        try:
            self.g.SetBkMode(self.dc,1)
            self.g.SetTextColor(self.dc,_color(color))
            # All labels wrap within a bounded two-line area; no ellipsis hides a phase.
            rect = self.rect(box)
            flags = 0x10|0x800|(1 if center else 0)
            measured = self.rect(box)
            self.u.DrawTextW(self.dc,value,-1,c.byref(measured),flags|0x400)
            rect.top += max(0, (rect.bottom-rect.top-(measured.bottom-measured.top))//2)
            self.u.DrawTextW(self.dc,value,-1,c.byref(rect),flags)
        finally:
            self.g.SelectObject(self.dc,old)
            self.g.DeleteObject(font)

    def image(self, box, path):
        # Small packaged BMP; no image library or external process at update time.
        bitmap = self.u.LoadImageW(None,str(path),0,self.n(box[2]),self.n(box[3]),0x10)
        if not bitmap: raise OSError('progress brand asset unavailable')
        source = self.g.CreateCompatibleDC(self.dc)
        if not source:
            self.g.DeleteObject(bitmap)
            raise OSError('progress image DC unavailable')
        old = self.g.SelectObject(source,bitmap)
        try:
            self.g.StretchBlt(self.dc,self.n(box[0]),self.n(box[1]),self.n(box[2]),self.n(box[3]),
                              source,0,0,self.n(box[2]),self.n(box[3]),0x00CC0020)
        finally:
            self.g.SelectObject(source,old)
            self.g.DeleteDC(source)
            self.g.DeleteObject(bitmap)


def show_progress_window(display, closed):
    """Run on its own thread. Dismissal destroys this HWND, never the update."""
    u,g = _apis()
    # Thread-local DPI awareness does not change the Worker/Qt process policy.
    old_context = None
    try:
        set_dpi = _bind(u,'SetThreadDpiAwarenessContext',w.HANDLE,w.HANDLE)
        old_context = set_dpi(c.c_void_p(-4))
    except AttributeError:
        set_dpi = None
    class_name = 'CheJinUpdateProgress-' + uuid.uuid4().hex
    k = c.WinDLL('kernel32', use_last_error=True)
    instance = _bind(k,'GetModuleHandleW',w.HINSTANCE,w.LPCWSTR)(None)
    hwnd, registered = None, False
    close_armed = False
    callback_error = []

    def round_window(handle):
        rect = w.RECT()
        u.GetClientRect(handle,c.byref(rect))
        radius = round(14*rect.right/WIDTH)
        region = g.CreateRoundRectRgn(0,0,rect.right+1,rect.bottom+1,radius*2,radius*2)
        if region and not u.SetWindowRgn(handle,region,True): g.DeleteObject(region)

    def repaint(handle):
        ps = PaintStruct()
        dc = u.BeginPaint(handle,c.byref(ps))
        memory = bitmap = old = None
        try:
            if not dc: raise OSError('progress paint DC unavailable')
            rect = w.RECT()
            if not u.GetClientRect(handle,c.byref(rect)): raise OSError('progress bounds unavailable')
            width,height = rect.right,rect.bottom
            memory = g.CreateCompatibleDC(dc)
            bitmap = g.CreateCompatibleBitmap(dc,width*2,height*2)
            if not memory or not bitmap: raise OSError('progress back buffer unavailable')
            old = g.SelectObject(memory,bitmap)
            title,seconds = display()
            paint(Canvas(u,g,memory,width*2/WIDTH),title,seconds,(time.monotonic()*220)%360)
            g.SetStretchBltMode(dc,4)
            g.StretchBlt(dc,0,0,width,height,memory,0,0,width*2,height*2,0x00CC0020)
        finally:
            if old: g.SelectObject(memory,old)
            if bitmap: g.DeleteObject(bitmap)
            if memory: g.DeleteDC(memory)
            u.EndPaint(handle,c.byref(ps))

    @WNDPROC
    def procedure(handle,message,wp,lp):
        nonlocal close_armed
        try:
            if message == 0x14: return 1  # WM_ERASEBKGND: opaque buffer paints everything.
            if message == 0x0f:
                repaint(handle)
                return 0
            if message == 0x21: return 3  # MA_NOACTIVATE
            if message == 0x10:
                u.DestroyWindow(handle)
                return 0
            if message == 0x84:  # HTCAPTION only in the custom title bar, outside X.
                rect = w.RECT()
                # Client coordinates from screen coordinates through MapWindowPoints.
                point = w.POINT(c.c_short(lp & 0xffff).value,c.c_short((lp >> 16)&0xffff).value)
                _bind(u,'ScreenToClient',w.BOOL,w.HWND,c.POINTER(w.POINT))(handle,c.byref(point))
                u.GetClientRect(handle,c.byref(rect))
                scale = rect.right/WIDTH
                if point.y < 34*scale and point.x < 356*scale: return 2
                return 1
            if message in (0x201,0x202):  # X never calls progress.close() or writes results.
                rect = w.RECT(); u.GetClientRect(handle,c.byref(rect))
                x,y = lp & 0xffff,(lp >> 16)&0xffff
                inside = x >= rect.right*356/WIDTH and y < rect.right*34/WIDTH
                if message == 0x201:
                    close_armed = inside
                else:
                    if close_armed and inside: u.DestroyWindow(handle)
                    close_armed = False
                return 0
            if message == 0x2e0:
                suggested = c.cast(lp,c.POINTER(w.RECT)).contents
                info = MonitorInfo(); info.size = c.sizeof(info)
                monitor = u.MonitorFromWindow(handle,2)
                if u.GetMonitorInfoW(monitor,c.byref(info)):
                    work = info.work
                else:
                    work = w.RECT()
                    if not u.SystemParametersInfoW(0x30,0,c.byref(work),0):
                        return 0
                bounds = window_bounds((work.left,work.top,work.right,work.bottom),
                                       wp & 0xffff,(suggested.left,suggested.top))
                u.SetWindowPos(handle,None,*bounds,0x14)
                round_window(handle)
                return 0
            return u.DefWindowProcW(handle,message,wp,lp)
        except Exception as exc:
            callback_error.append(exc)
            return 0  # Never let a Python exception escape through a native callback.

    wc = WindowClass()
    wc.instance = instance
    wc.cursor = u.LoadCursorW(None,c.c_void_p(32512))
    wc.style,wc.procedure,wc.name = 0x20000,procedure,class_name  # CS_DROPSHADOW
    try:
        registered = bool(u.RegisterClassW(c.byref(wc)))
        if not registered: raise OSError('progress window class unavailable')
        work = w.RECT()
        if not u.SystemParametersInfoW(0x30,0,c.byref(work),0):
            raise OSError('progress work area unavailable')
        dpi = 96
        try: dpi = _bind(u,'GetDpiForSystem',w.UINT)() or 96
        except AttributeError: pass
        bounds = window_bounds((work.left,work.top,work.right,work.bottom),dpi)
        hwnd = u.CreateWindowExW(0x08000000,class_name,'车金 Worker 更新',0x80000000,
                                 *bounds,None,None,instance,None)
        if not hwnd: raise OSError('progress window unavailable')
        round_window(hwnd)
        u.ShowWindow(hwnd,4)  # SW_SHOWNOACTIVATE; no foreground/topmost request.
        msg = w.MSG()
        while not closed.wait(0.05) and u.IsWindow(hwnd) and not callback_error:
            while u.PeekMessageW(c.byref(msg),None,0,0,1):
                u.TranslateMessage(c.byref(msg))
                u.DispatchMessageW(c.byref(msg))
            if u.IsWindow(hwnd): u.InvalidateRect(hwnd,None,False)
        if callback_error: raise callback_error[0]
    finally:
        if hwnd and u.IsWindow(hwnd): u.DestroyWindow(hwnd)
        if registered: u.UnregisterClassW(class_name,instance)
        if old_context and set_dpi: set_dpi(old_context)
