"""Explicit WeChat 3.9 C1 OCR flow; default OCR routes are unchanged.

The controller consumes one current ROI per state. Native and screenshot replay
ports share the controller. An attempted submit is durable before the click;
UI disappearance alone never certifies delivery or admits a contact to AI.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import time
import random
from pathlib import Path
from typing import Any
from ..add_friend_contract import validate_add_friend_entry_click_contract


class FlowStop(RuntimeError):
    pass


def clean(value: Any) -> str:
    return ''.join(str(value or '').split())


def box(row: dict) -> list[int]:
    return [int(row[k]) for k in ('left', 'top', 'right', 'bottom')]


def contains(rect: list[int], row: dict) -> bool:
    b = box(row)
    return rect[0] <= b[0] < b[2] <= rect[2] and rect[1] <= b[1] < b[3] <= rect[3]


@dataclass
class Frame:
    image: Any
    rows: list[dict]
    name: str
    hwnd: int = 0
    origin: tuple[int, int] = (0, 0)
    geometry: Any = None
    timestamp: float = 0


def unique_text(frame: Frame, labels: tuple[str, ...], *, region=None) -> dict:
    matches = [r for r in frame.rows if clean(r.get('text')) in labels
               and float(r.get('confidence', 0)) >= .80 and (region is None or contains(region, r))]
    if len(matches) != 1:
        raise FlowStop('OCR_ANCHOR_MISSING_OR_AMBIGUOUS')
    return matches[0]


def components(image, kind: str) -> list[list[int]]:
    """Actual filled pixel components, never screenshot annotation outlines."""
    import numpy as np
    import cv2
    a = np.array(image.convert('RGB'))
    r, g, b = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
    if kind == 'green':
        mask = (g > 125) & (r < 85) & (b < 170) & (g-r > 65) & (g-b > 20)
    elif kind == 'white':
        mask = (r >= 250) & (g >= 250) & (b >= 250)
    elif kind == 'search':
        mask = (((r >= 210) & (r <= 229)) | ((r >= 248) & (r <= 249))) & (abs(r-g) <= 2) & (abs(r-b) <= 2)
    else:
        raise ValueError(kind)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), 8)
    result = []
    for x, y, w, h, area in stats[1:count]:
        if w >= 70 and h >= 22 and area/(w*h) >= .65:
            result.append([int(x), int(y), int(x+w), int(y+h)])
    return result


def target(frame: Frame, row: dict, *, filled: str | None = None) -> dict:
    b = box(row)
    if filled:
        candidates = [c for c in components(frame.image, filled) if contains(c, row)]
        if len(candidates) != 1:
            raise FlowStop('PIXEL_CONTROL_MISSING_OR_AMBIGUOUS')
        b = candidates[0]
    return {'bounds': b, 'point': [(b[0]+b[2])//2, (b[1]+b[3])//2], 'frame': frame.name}


def empty_search(frame: Frame) -> dict:
    w, h = frame.image.size
    # The default grey 搜索 placeholder is an empty input, not a query.
    aliases = ('搜索', 'Q搜索', 'O搜索', '0搜索', '搜素', 'Q搜素', 'O搜素', '0搜素')
    if not any(clean(r.get('text')) in aliases for r in frame.rows):
        b=search_control_bounds(frame)
        focused_search_empty_pixels(frame.image.crop(tuple(b)))
        return {'bounds':b,'point':[(b[0]+b[2])//2,(b[1]+b[3])//2],'frame':frame.name,'proof':'focused_empty_pixels'}
    row = unique_text(frame, aliases, region=[0, 0, w, h])
    result = target(frame, row, filled='search')
    # Restrict the control to the left header; Q is an OCR rendering of the
    # magnifier, admitted only inside the actual neutral input pixels.
    b = result['bounds']
    if b[1] < 8 or b[3] > h or b[2]-b[0] > .6*w:
        raise FlowStop('SEARCH_CONTROL_OUT_OF_REGION')
    # A placeholder is insufficient when another OCR row still contains a query.
    aliases = {'搜索', 'Q搜索', 'O搜索', '0搜索', '搜素', 'Q搜素', 'O搜素', '0搜素'}
    for other in frame.rows:
        value = clean(other.get('text'))
        icon = value in {'Q','O','0'} and other['right'] <= b[0]+(b[3]-b[1])*.75
        if contains(b, other) and value and value not in aliases and not icon:
            raise FlowStop('SEARCH_QUERY_REMAINS')
    return result


def search_control_bounds(frame: Frame) -> list[int]:
    if frame.name != 'header':raise FlowStop('SEARCH_FRAME_INVALID')
    w,h = frame.image.size
    choices = [c for c in components(frame.image,'search') + components(frame.image,'white')
               if c[1]>=8 and c[3]<=h and c[2]-c[0]<=.6*w]
    if len(choices)!=1:raise FlowStop('SEARCH_CLEANUP_CONTROL_UNRESOLVED')
    return choices[0]


# User-provided 237x31 empty focused input, containing only neutral UI pixels
# and the magnifier/clear icons. No contact, message or desktop content.
_EMPTY_SEARCH_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAO0AAAAfCAIAAACEdd3KAAAEaklEQVR4nO3cTU/qShgHcKa0QNupvF0D3sgHgJgoISxMTFwYo2FldMlnk7Bjwco97l2xgISzEC8x4caA59ILzkBLceYGmhAuL4rgiZjz/FZ16JQh+afOPJ0U9Xo9h8NhGgalxDRNB+cOADYeQsjldqsqdns8wz97vV77X73b7aqKqmoYIeGrRwjA+zjnlBBKiaIoXp9fNA2j2+0Ggn/IsrxEdwA2hc/vd3s8//x8drs9AqVEVTGEGHxHsixr2hYhL0LfNFWMv3o8AKwIa3hgWQLnHCG06kUA+GIIDTMsfvUwAPgfQki73e73+w6Hw+Vyeb1evMR8AXIMNgJjTNf1VqtlWda4cVhMa7clSfL7/YFAQBAWFtMgx+DrMcbq9TohZO6nlmU1m01KaSQSWRRlqBaDjQ7xGKW0Xq8zxla5HzcajUqlQilVFCUWi4XD4TUGDMAcuq6PQ/z6+ioIwmThgTGGRuyps67rwWDwAznmnBcKhVwuJ4qi0+lkjFmWdXV1lUqloL4BPlGr1bIPXl9fi8ViJBKZvF3WajXTNGOx2Pjkj+W4UCjc3Nyk0+lkMokxppQWi8V8Po8QSqVSn/k7wG+MEDJe2AmCEIlEqtUq5zwcDnPOa7Vas9nc29sbn29ZVqfT2draWirHjUYjl8ul0+mTkxO7RdO04+Njp9OZyWQSiUQoFPplPw38Rtrt9vgYIWTHt1KpiKJoGMbj42MymdQ0barLsjm2L5RMJqfa4/E4xrhSqUCOwaew68STwuGwKIo/fvxACM2GeG6XhfUKSqnT6ZytP8uyLEkSpXS9wQOwEOfcMAyEEOfcNE2+3Ebi+TlWFIUxNptXwzAGg4GiKMtcGoB3uVyuqZZarXZ/f39wcBCLxcrlcqPRmDpBkqRlcxyLxSzLKhaLU+2lUokQMl48ArAmr9c7PmaMPTw8NJtNezoRDAaj0Wi1Wn16eprs4vP5Zq8zf34cDoevrq7y+bzT6YzH47IsG4ZRKpWy2Ww0Gt3e3l53+ACMYIwlSbJLFggh0zT39vbsOfF42TdZ55UkaXaR91bdza4TZzIZ+5sGgwEhZH9/v1wuFwqFs7OzRR0B+BC/399sNu3gzv6r39nZmTp57kUW5tiuEycSicnneaFQ6O7uLpfLWZZ1fn4uirA9A6wrEAjQkXfPVBQlEAjM/eidIIZGJlsODw8Hg0E2m8UYHx8fw7M9sCb78ce7WywURXljn9AqN9SjoyOM8fX1ta7rFxcXEGWwJkEQdnd3Z/dt2n7Vvk2EUDwev7y8vL29PT09XWabMwBvEwQhONLpdMb76CVJ8vl8cxd2U9Bf1fvQzp8rzHQ558O3BajqRzsC8IkYY09/11fff4wQghCDDTHc67nkoz8ANpCdXkGSXF3YLwG+LUqIy+0WVA2/vHQMY/iWNwC+F8PodTptRVVFj0dWVfXn8zPGGta0N0obAGwOzhl5IS8vHXlIGb6ncJjrXo8S0rf68L5N4PgOEEKiJGGseUbvJfwPBTr8Dq/4S3YAAAAASUVORK5CYII='


def focused_search_empty_pixels(image) -> None:
    """Fail closed unless every input pixel matches the observed empty style.

    Match both icon shapes and the border, with a uniform middle stretched only
    horizontally. OCR silence is never emptiness proof. A single vertical mark
    at the text origin is unresolved (caret OR text), not an accepted caret.
    """
    import base64,io
    import numpy as np
    import cv2
    from PIL import Image
    a=np.array(image.convert('RGB')).astype(int)
    height,width=a.shape[:2]
    if height!=31 or not 180<=width<=330:raise FlowStop('SEARCH_EMPTY_STYLE_UNSUPPORTED')
    with Image.open(io.BytesIO(base64.b64decode(_EMPTY_SEARCH_PNG))) as im:
        ref=np.array(im.convert('RGB')).astype(int)
    expected=np.repeat(ref[:,100:101,:],width,axis=1)
    expected[:,:31,:]=ref[:,:31,:];expected[:,-31:,:]=ref[:,-31:,:]
    different=np.max(np.abs(a-expected),axis=2)>18
    if not different.any():return
    count,_,stats,_=cv2.connectedComponentsWithStats(different.astype('uint8'),8)
    marks=stats[1:count]
    if len(marks)==1:
        x,y,w,h,area=map(int,marks[0])
        if 24<=x<=30 and w<=2 and 4<=y and y+h<=27 and 10<=h<=23 and area>=h:
            raise FlowStop('SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED')
    raise FlowStop('SEARCH_QUERY_OR_STYLE_REMAINS')


def search_query(frame: Frame, query: str) -> dict:
    w, h = frame.image.size
    upper = [0, 0, w, min(h, 90)]
    row = unique_text(frame, (query,), region=upper)
    return target(frame, row)


def restricted(frame: Frame) -> bool:
    tokens = ('操作频繁', '账号异常', '安全验证', '限制使用', '被限制', '稍后再试', '请重新登录')
    return any(any(token in clean(r.get('text')) for token in tokens) for r in frame.rows)


def lookup(frame: Frame, query: str) -> dict:
    if restricted(frame):
        raise FlowStop('ACCOUNT_RESTRICTED')
    search_query(frame, query)
    return target(frame, unique_text(frame, ('网络查找手机/QQ号', '网络查找手机／QQ号')))


def classify_card(frame: Frame) -> tuple[str, dict | None]:
    if restricted(frame):
        raise FlowStop('ACCOUNT_RESTRICTED')
    texts = [clean(r.get('text')) for r in frame.rows]
    absent = any(any(t in s for t in ('该用户不存在', '用户不存在', '帐号不存在', '账号不存在', '无法找到该用户')) for s in texts)
    add = [r for r in frame.rows if clean(r.get('text')) == '添加到通讯录' and r.get('confidence', 0) >= .80]
    chat = [r for r in frame.rows if clean(r.get('text')) in ('发消息', '发送消息') and r.get('confidence', 0) >= .80]
    if sum((absent, bool(add), bool(chat))) != 1:
        raise FlowStop('CONTACT_RESULT_MISSING_OR_AMBIGUOUS')
    if absent:
        return 'not_found', None
    if add:
        return 'new_contact', target(frame, unique_text(frame, ('添加到通讯录',)), filled='green')
    return 'already_friend', target(frame, unique_text(frame, ('发消息', '发送消息')), filled='green')


def form_fields(frame: Frame) -> dict:
    unique_text(frame, ('申请添加朋友',))
    result = {}
    whites = components(frame.image, 'white')
    for key, label in [('greeting', '发送添加朋友申请'), ('remark', '备注名')]:
        row = unique_text(frame, (label,))
        below = [c for c in whites if 0 < c[1]-row['bottom'] < frame.image.height*.15
                 and c[0] <= row['left']+8 and c[2] >= row['right']
                 and c[2]-c[0] >= frame.image.width*.55
                 and c[3]-c[1] <= frame.image.height*.30]
        if len(below) != 1:
            raise FlowStop('INVITE_FIELD_MISSING_OR_AMBIGUOUS')
        b = below[0]
        result[key] = {'bounds': b, 'point': [(b[0]+b[2])//2, (b[1]+b[3])//2], 'frame': frame.name}
    if result['greeting']['bounds'][3] >= result['remark']['bounds'][1]:
        raise FlowStop('INVITE_FIELDS_OVERLAP')
    return result


def verify_fields(frame: Frame, greeting: str, remark: str) -> dict:
    fields = form_fields(frame)
    # User-confirmed policy: the original greeting is pasted once, but it may
    # scroll horizontally. The final OCR gate checks the remark shortcode only.
    # Labels, pixel control bounds, non-overlap and action/identity guards remain.
    rows = [r for r in frame.rows if contains(fields['remark']['bounds'], r)]
    actual = ''.join(clean(r.get('text')) for r in rows)
    if actual != clean(remark) or not rows or any(r.get('confidence', 0) < .90 for r in rows):
        raise FlowStop('INVITE_FIELD_VERIFICATION_FAILED')
    return fields


class SubmitJournal:
    """Exclusive, fsynced pre-click record; failed/unknown attempts are retained."""
    def __init__(self, path: Path, *, clock=None):
        self.path = path
        self.clock = clock

    def begin(self, query: str, greeting: str, remark: str):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {'submit_attempted': True, 'outcome': 'unknown', 'time_utc': self.clock.now().timestamp() if self.clock else time.time(),
                  'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
                  'greeting_sha256': hashlib.sha256(greeting.encode()).hexdigest(),
                  'remark_sha256': hashlib.sha256(remark.encode()).hexdigest()}
        if self.clock:
            record.update(local_time_utc=time.time(), clock_source='authenticated_backend_heartbeat',
                          clock_evidence=self.clock.evidence())
        with self.path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream);stream.flush();os.fsync(stream.fileno())


def run_flow(ui, *, phone: str, verify_message: str, remark_name: str,
             remark_code: str, journal: SubmitJournal) -> dict:
    validated = validate_add_friend_entry_click_contract(phone=phone, wechat='',
        verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)
    report = {'status': 'stopped', 'submit_attempted': False, 'delivery_confirmed': False,
              'ai_admission': False, 'cleanup': 'not_started', 'steps': []}
    query = validated['query']
    if (not validated['ok'] or not re.fullmatch(r'1\d{10}', query)
            or not re.fullmatch(r'[A-Z0-9]{7,8}', remark_code)
            or any('\n' in s or '\r' in s or '\t' in s for s in (verify_message, remark_name))
            or len(verify_message) > 40 or len(remark_name) > 20):
        report['error_code'] = 'TASK_PAYLOAD_INVALID';return report
    if journal.path.exists():
        report['error_code'] = 'PREVIOUS_SUBMIT_ATTEMPT_REQUIRES_REVIEW';return report
    search_owned = False
    form_open = False
    form_transition = False
    def progress(name,state,note=None):
        try:
            callback=getattr(ui,'on_progress',None)
            if callback is not None:callback(name,state,note)
        except Exception:pass
    def step(name):
        ui.guard()
        if report['steps']:progress(report['steps'][-1],'done')
        report['steps'].append(name);progress(name,'current')
    try:
        step('search_empty')
        if hasattr(ui,'verified_empty_search'):
            frame,t = ui.verified_empty_search()
        else:
            frame = ui.capture('header');t = empty_search(frame)
        ui.click(frame, t, 'focus_main_search')
        search_owned = True
        ui.replace(frame, query)
        step('query_verified_and_network_lookup')
        frame = ui.capture('lookup')
        t = lookup(frame, query)
        ui.click(frame, t, 'network_lookup')
        frame = ui.capture('card')
        step('contact_result')
        kind, t = classify_card(frame)
        if kind == 'not_found':
            report['status'] = 'not_found';progress('contact_result','error','not_found');return report
        if kind == 'already_friend':
            ui.click(frame, t, 'open_existing_chat')
            ui.existing_chat_verified(frame, t)
            report['status'] = 'already_friend';progress('contact_result','done','already_friend');return report
        # From this point a click may have opened a new surface even if the
        # subsequent observation fails. Never clean up through the old card.
        form_transition = True
        ui.click(frame, t, 'open_invite_form')
        ui.select_invite_dialog();form_open = True
        step('invite_form_fields')
        frame = ui.capture('fields')
        fields = form_fields(frame)
        ui.click(frame, fields['greeting'], 'focus_greeting')
        ui.replace(frame, verify_message)
        # Filling one input changes its text but not the other field. Reacquire
        # only this ROI to obtain a new frame for the next click.
        frame = ui.capture('fields_after_greeting')
        fields = form_fields(frame)
        ui.click(frame, fields['remark'], 'focus_remark')
        ui.replace(frame, remark_name)
        step('verify_remark_shortcode')
        frame = ui.capture('fields_filled')
        verify_fields(frame, verify_message, remark_name)
        report['field_verification'] = {'mode': 'remark_shortcode_only', 'verified': True, 'greeting_readback_calls': 0}
        step('confirm_once')
        footer = ui.capture('footer')
        if restricted(footer):raise FlowStop('ACCOUNT_RESTRICTED')
        t = target(footer, unique_text(footer, ('确定',)), filled='green')
        ui.guard()
        journal.begin(query, verify_message, remark_name)
        report['submit_attempted'] = True
        ui.click(footer, t, 'confirm_invite_once')
        outcome = ui.observe_submit()
        form_open = outcome.get('form_open', True)
        form_transition = not outcome.get('form_closed', False)
        report['submission_observation'] = outcome
        # Even a dismissed form is just local submission evidence. Recipient
        # confirmation or an explicit sent signal is needed for delivery.
        report['status'] = 'submitted_pending_recipient' if outcome.get('form_closed') else 'submission_unknown'
        progress('confirm_once','done' if outcome.get('form_closed') else 'waiting','recipient' if outcome.get('form_closed') else 'failed')
        if outcome.get('restricted'):report['error_code'] = 'ACCOUNT_RESTRICTED'
        return report
    except FileExistsError:
        report['error_code'] = 'PREVIOUS_SUBMIT_ATTEMPT_REQUIRES_REVIEW'
    except Exception as exc:
        code = str(exc)
        report['error_code'] = code if re.fullmatch('[A-Z_]+', code) else 'C1_ACTION_FAILED'
        if report['submit_attempted']:report['status'] = 'submission_unknown'
        if report['steps']:progress(report['steps'][-1],'waiting' if report['submit_attempted'] else 'error','failed')
    finally:
        if getattr(ui, 'window_events', None):
            report['window_events'] = ui.window_events
        if search_owned and not form_open and not form_transition:
            try:
                progress('cleanup','current')
                ui.clear_search()
                progress('cleanup','done')
                report['cleanup'] = 'search_empty_verified'
                if getattr(ui, 'cleanup_action', None):
                    report['cleanup_action'] = ui.cleanup_action
            except Exception as exc:
                progress('cleanup','error','failed')
                report['cleanup'] = 'failed_preserve_business_outcome'
                code = str(exc)
                report['cleanup_error_code'] = code if re.fullmatch('[A-Z_]+', code) else 'SEARCH_CLEANUP_FAILED'
        elif form_open:
            report['cleanup'] = 'deferred_invite_dialog_still_open'
        elif form_transition:
            report['cleanup'] = 'deferred_invite_dialog_unconfirmed'
    return report


class NativeUI:
    """Bounded ROI capture + original humanized input, with per-frame guards."""
    def __init__(self, ops, hwnd: int, pid: int, evidence: Path, identity_guard):
        self.ops, self.main, self.pid = ops, hwnd, pid
        self.current = hwnd;self.evidence = evidence;self.identity_guard = identity_guard
        if not ops.ensure_dpi_awareness_status().get('per_monitor_aware'):
            raise FlowStop('DPI_AWARENESS_UNVERIFIED')
        self.initial = ops.get_window_geometry(hwnd)
        self.dpi = ops.window_dpi_scale(hwnd)
        self.count = 0;self.ocr_count = 0;self.last_frame = None
        self.greeting_expected = None
        self.clipboard_field_events = []
        self.window_events = []
        self.lookup_visible_windows = None
        evidence.mkdir(parents=True, exist_ok=True)
        if not ops.ensure_dpi_awareness_status().get('per_monitor_aware'):
            raise FlowStop('DPI_AWARENESS_UNVERIFIED')
        if abs(self.initial['width']-1060) > 6 or abs(self.initial['height']-740) > 6:
            raise FlowStop('ACCEPTED_WINDOW_SIZE_CHANGED')
        from .hook_c1 import classify_visible_other_windows
        if classify_visible_other_windows(ops, hwnd, pid)['blocking_window_details']:
            raise FlowStop('CLOSE_WECHAT_POPUP_FIRST')
        ops.activate_window(hwnd, foreground_only=True)
        self.guard()

    def _guard_main_window(self):
        self.identity_guard()
        o = self.ops;gui = o.win32gui
        if (not gui.IsWindow(self.main) or gui.IsIconic(self.main)
                or not gui.IsWindowVisible(self.main)
                or o.get_window_geometry(self.main) != self.initial
                or o.win32process.GetWindowThreadProcessId(self.main)[1] != self.pid):
            raise FlowStop('WINDOW_FOCUS_OR_GEOMETRY_CHANGED')
        if hasattr(self,'dpi') and abs(o.window_dpi_scale(self.main)-self.dpi)>.01:
            raise FlowStop('WINDOW_DPI_CHANGED')

    def guard(self, frame=None):
        self._guard_main_window()
        o = self.ops;gui = o.win32gui
        if (not gui.IsWindow(self.current) or gui.GetForegroundWindow() != self.current
                or o.win32process.GetWindowThreadProcessId(self.current)[1] != self.pid):
            raise FlowStop('WINDOW_FOCUS_OR_GEOMETRY_CHANGED')
        if frame is not None:
            if (frame.hwnd != self.current or o.get_window_client_geometry(self.current) != frame.geometry
                    or time.monotonic()-frame.timestamp > 15 or frame is not self.last_frame):
                raise FlowStop('STALE_ROI_FRAME')

    def _bounds(self, label: str, w: int, h: int):
        if label == 'header':return (0, 0, int(w*.62), min(h, 90))
        if label == 'search_empty':return tuple(self.search_empty_bounds)
        if label == 'lookup':return (0, 0, int(w*.38), int(h*.43))
        if label == 'card':return (int(w*.34), 0, int(w*.80), int(h*.55))
        if label.startswith('fields'):return (0, 0, w, int(h*.36))
        if label == 'footer':return (0, int(h*.80), w, h)
        if label == 'chat_header':return (int(w*.36), 0, int(w*.80), min(h, 90))
        raise ValueError(label)

    def visible_wechat_windows(self):
        gui = self.ops.win32gui
        result = set()
        def collect(hwnd, _):
            if (gui.IsWindowVisible(hwnd)
                    and self.ops.win32process.GetWindowThreadProcessId(hwnd)[1] == self.pid):
                result.add(hwnd)
        gui.EnumWindows(collect, None)
        if len(result) > 32:
            raise FlowStop('WECHAT_WINDOW_INVENTORY_LIMIT')
        return result

    def record_window(self, stage, hwnd):
        # Metadata at the failing instant, no title, screenshot or credentials.
        self.identity_guard()
        event = self._window_metadata(stage, hwnd)
        if not hasattr(self, 'window_events'): self.window_events = []
        self.window_events.append(event)
        return event

    def _window_metadata(self, stage, hwnd):
        # Caller already verified identity; diagnostics need no second Hook call.
        gui = self.ops.win32gui
        event = {'stage': stage, 'hwnd': int(hwnd or 0)}
        chain = []
        if gui.IsWindow(hwnd):
            same = self.ops.win32process.GetWindowThreadProcessId(hwnd)[1] == self.pid
            event['same_process'] = same
            if same:
                event.update(window_class=gui.GetClassName(hwnd), visible=bool(gui.IsWindowVisible(hwnd)),
                             client=self.ops.get_window_client_geometry(hwnd))
                owner = gui.GetWindow(hwnd, 4); seen = {hwnd}
                while owner and owner not in seen and len(chain) < 8:
                    seen.add(owner)
                    same_owner = self.ops.win32process.GetWindowThreadProcessId(owner)[1] == self.pid
                    entry = {'hwnd': int(owner), 'same_process': same_owner, 'is_main': owner == self.main}
                    if same_owner: entry['window_class'] = gui.GetClassName(owner)
                    chain.append(entry)
                    if owner == self.main or not same_owner: break
                    owner = gui.GetWindow(owner, 4)
                event['owner_chain'] = chain
        return event

    def select_contact_surface(self):
        foreground = self.ops.win32gui.GetForegroundWindow()
        event = self.record_window('contact_result_foreground', foreground)
        if foreground == self.main:
            self.current = self.main
            return
        gui = self.ops.win32gui
        c = event.get('client', {})
        chain = event.get('owner_chain', [])
        owned = bool(chain and chain[-1]['is_main'] and all(e['same_process'] for e in chain))
        # WeChat may create an unowned popup on network lookup. It must have
        # become visible during this guarded click, remain inside the main
        # client, and still pass card OCR + actual green-button pixels.
        baseline = getattr(self, 'lookup_visible_windows', None)
        main = self.ops.get_window_client_geometry(self.main)
        newly_visible_unowned = (baseline is not None and foreground not in baseline
            and event.get('same_process') and not gui.GetWindow(foreground, 4)
            and c.get('screen_left', -1) >= main['screen_left']
            and c.get('screen_top', -1) >= main['screen_top']
            and c.get('screen_left', -1)+c.get('width', 0) <= main['screen_left']+main['width']
            and c.get('screen_top', -1)+c.get('height', 0) <= main['screen_top']+main['height'])
        excluded = {'popupshadow', 'Search2Wnd', 'ChatContactMenu', 'TrayNotifyWnd',
                    'tooltips_class32', 'EmotionTipWnd', 'SessionDragWnd'}
        if (not event.get('same_process') or not event.get('visible')
                or event.get('window_class') in excluded
                or not 250 <= c.get('width', 0) <= 650 or not 160 <= c.get('height', 0) <= 600
                or not (owned or newly_visible_unowned)):
            event['accepted'] = False
            raise FlowStop('UNEXPECTED_CONTACT_SURFACE')
        event.update(accepted=True, basis='same_process_owner_chain' if owned else 'newly_visible_unowned_card_candidate')
        self.current = foreground
        self.card_hwnd = foreground

    def capture(self, label: str) -> Frame:
        # The one caret observation already waits in verified_empty_search.
        if label != 'search_empty':
            self.ops.humanized_action_sleep(1800, 2400) if label == 'card' else self.ops.humanized_action_sleep(450, 800)
        if label == 'card':
            self.select_contact_surface()
        self.guard()
        o = self.ops;c = o.get_window_client_geometry(self.current)
        bounds = (0,0,c['width'],c['height']) if label=='card' and self.current!=self.main else self._bounds(label, c['width'], c['height'])
        left, top = c['screen_left']+bounds[0], c['screen_top']+bounds[1]
        captured_at=time.monotonic()
        image = o.try_image_grab((left, top, c['screen_left']+bounds[2], c['screen_top']+bounds[3]))
        self.count += 1
        if image is None or image.size != (bounds[2]-bounds[0], bounds[3]-bounds[1]):
            raise FlowStop('ROI_CAPTURE_FAILED')
        # No second screenshot / full-frame fallback when OCR fails.
        image.save(self.evidence/f'{self.count:02d}-{label}.png')
        if o.get_window_client_geometry(self.current) != c:
            raise FlowStop('CAPTURE_GEOMETRY_CHANGED')
        self.guard()
        rows = o.run_ocr(image);self.ocr_count += 1
        self.guard()
        frame = Frame(image, rows, label, self.current, (left, top), c, captured_at)
        self.last_frame = frame
        self.guard(frame)
        (self.evidence/f'{self.count:02d}-{label}.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8')
        if restricted(frame):raise FlowStop('ACCOUNT_RESTRICTED')
        return frame

    def click(self, frame: Frame, t: dict, action: str):
        self.guard(frame)
        p = t['point'];b = t['bounds']
        if not (b[0] <= p[0] < b[2] and b[1] <= p[1] < b[3]
                and 0 <= b[0] < b[2] <= frame.image.width and 0 <= b[1] < b[3] <= frame.image.height):
            raise FlowStop('TARGET_OUTSIDE_CURRENT_ROI')
        x, y = frame.origin[0]+p[0], frame.origin[1]+p[1]
        hit = self.ops.win32gui.WindowFromPoint((x,y))
        root = self.ops.win32gui.GetAncestor(hit, 2)
        if root != self.current:raise FlowStop('TARGET_OCCLUDED')
        o = self.ops
        o.require_active_ui_action_budget('hook_c1_'+action,
            metadata={'roi':frame.name,'frame_age_ms':round((time.monotonic()-frame.timestamp)*1000)})
        # Preserve original humanized cursor/button rhythm, with another account,
        # lease, focus, geometry and occlusion check immediately before down.
        o.ensure_left_button_released()
        sx,sy=o.win32api.GetCursorPos();steps=random.randint(6,11)
        for index in range(1,steps+1):
            ratio=index/steps;ease=ratio*ratio*(3-2*ratio)
            o.win32api.SetCursorPos((round(sx+(x-sx)*ease),round(sy+(y-sy)*ease)))
            time.sleep(random.uniform(.016,.052))
        o.humanized_action_sleep(100,240)
        self.guard(frame)
        hit=o.win32gui.WindowFromPoint((x,y))
        if o.win32gui.GetAncestor(hit,2)!=self.current:raise FlowStop('TARGET_OCCLUDED')
        if action == 'open_invite_form':
            self.invite_visible_windows = self.visible_wechat_windows()
        if action == 'network_lookup':
            self.lookup_visible_windows = self.visible_wechat_windows()
            self.record_window('network_lookup_before_click', self.current)
            self.guard(frame)
        try:
            o.win32api.mouse_event(o.win32con.MOUSEEVENTF_LEFTDOWN,0,0,0,0)
            o.humanized_action_sleep(60,150)
            o.win32api.mouse_event(o.win32con.MOUSEEVENTF_LEFTUP,0,0,0,0)
            o.humanized_action_sleep(180,360)
        finally:o.ensure_left_button_released()

    def replace(self, frame: Frame, text: str):
        self.guard(frame)
        self.ops.hotkey(self.ops.win32con.VK_CONTROL, ord('A'))
        self.guard(frame)
        self.ops.key_press(self.ops.win32con.VK_BACK)
        if frame.name.startswith('fields'):
            # Restore the existing invite-form workflow: one Unicode clipboard
            # paste per field, with the same input pauses and fresh-frame guard.
            self.ops.add_friend_paced_pause('input', reason='hook_c1_after_field_clear_before_clipboard')
            self.guard(frame)
            # Counts represent native calls in this run, with no field values.
            event={'frame':frame.name,'copy_calls':0,'paste_calls':0,
                   'clipboard_match':False,'completion_guard_passed':False}
            if not hasattr(self,'clipboard_field_events'):self.clipboard_field_events=[]
            self.clipboard_field_events.append(event)
            event['copy_calls']+=1
            self.ops.clipboard_copy(text)
            self.ops.add_friend_paced_pause('input', reason='hook_c1_after_clipboard_copy_before_paste')
            self.guard(frame)
            if self.ops.clipboard_read() != text:
                raise FlowStop('CLIPBOARD_CONTENT_CHANGED')
            event['clipboard_match']=True
            event['paste_calls']+=1
            self.ops.hotkey(self.ops.win32con.VK_CONTROL, ord('V'))
            self.ops.add_friend_paced_pause('input', reason='hook_c1_after_field_paste')
            self.guard(frame)
            event['completion_guard_passed']=True
            return
        if not text:
            return
        settings = self.ops.humanized_input_settings()
        settings.update(typo_probability=0.0,typo_max=0)
        if re.fullmatch(r'1\d{10}', text):
            # One digit per chunk, 4.4-4.95 seconds of pacing for eleven digits.
            # Explicit local profile cannot inherit the fast text chunk scale.
            settings.update(chunk_min_chars=1, chunk_max_chars=1,
                char_delay_min_ms=400, char_delay_max_ms=450,
                micro_pause_every_chars=0, inter_chunk_delay_scale=1.0,
                adaptive_speed_enabled=False)
        def guarded():
            try:self.guard();return {'ok':True}
            except FlowStop:return {'ok':False}
        result = self.ops.type_text_with_sendinput_unicode(text, settings, window_guard_func=guarded)
        if not result.get('ok'):raise FlowStop('TEXT_INPUT_FAILED')

    def select_invite_dialog(self):
        self.contact_surface = self.current
        baseline = getattr(self, 'invite_visible_windows', None)
        if baseline is None:
            raise FlowStop('INVITE_FORM_WINDOW_NOT_FOUND')
        deadline = time.monotonic()+5
        # Reuse the original post-add-contact verification pacing once. The
        # bounded observation below never re-clicks or repeats this pause.
        self.ops.add_friend_paced_pause('verify', reason='after_add_contact_entry_click_before_capture')
        previous = None
        observed = None
        last_candidate = None
        gui = self.ops.win32gui
        while time.monotonic() < deadline:
            self._guard_main_window()
            foreground = gui.GetForegroundWindow()
            candidate = None
            if foreground not in {self.main, self.contact_surface}:
                event = self._window_metadata('invite_dialog_candidate', foreground)
                # Bound diagnostics to changed observations; no title or content.
                signature = {k:v for k,v in event.items() if k != 'stage'}
                if signature != observed:
                    if not hasattr(self, 'window_events'): self.window_events = []
                    self.window_events.append(event)
                observed = signature
                last_candidate = foreground
                c = event.get('client', {})
                if (event.get('same_process') and event.get('visible')
                        and foreground not in baseline
                        and event.get('window_class') not in {'popupshadow', 'tooltips_class32'}
                        and c.get('width', 0) > 0 and c.get('height', 0) > 0):
                    candidate = signature
                elif not event.get('same_process'):
                    raise FlowStop('WINDOW_FOCUS_OR_GEOMETRY_CHANGED')
            if candidate is not None and candidate == previous:
                # No fixed-size form template: existing form OCR, pixels,
                # field readback and per-action guards still authorize input.
                self.current = foreground
                self.guard()
                self.window_events[-1].update(accepted=True, basis='new_visible_stable_invite_candidate')
                return
            previous = candidate
            time.sleep(.15)
        if last_candidate is not None:
            self.record_window('invite_dialog_timeout', gui.GetForegroundWindow())
            raise FlowStop('UNEXPECTED_WECHAT_DIALOG')
        raise FlowStop('INVITE_FORM_WINDOW_NOT_FOUND')

    def observe_submit(self):
        dialog = self.current;deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            self.identity_guard()
            if not self.ops.win32gui.IsWindow(dialog) or not self.ops.win32gui.IsWindowVisible(dialog):
                self.current = self.ops.win32gui.GetForegroundWindow()
                if self.current not in {self.main,getattr(self,'contact_surface',self.main)}:
                    raise FlowStop('UNEXPECTED_POST_SUBMIT_SURFACE')
                self.guard()
                return {'form_open':False,'form_closed':True,'basis':'dialog_dismissed_only'}
            time.sleep(.15)
        # Reuse a bounded fields region once to record failure; never resubmit.
        self.current = dialog
        frame = self.capture('fields_after_submit')
        return {'form_open':True,'form_closed':False,'restricted':restricted(frame)}

    def existing_chat_verified(self, card: Frame, button: dict):
        # A selected first sidebar row alone is not identity evidence. Compare
        # the current chat title with the exact name read in the result card.
        rows=[r for r in card.rows if r.get('confidence',0)>=.90
              and 70 < r['top'] < button['bounds'][1]-40
              and clean(r.get('text')) not in ('微信号','地区','个性签名')]
        if not rows:raise FlowStop('EXISTING_CONTACT_NAME_UNRESOLVED')
        expected=clean(rows[0]['text'])
        if self.ops.win32gui.GetForegroundWindow()==self.main:self.current=self.main
        self.return_to_main()
        frame = self.capture('chat_header')
        if not any(r.get('confidence',0)>=.90 and clean(r.get('text'))==expected for r in frame.rows):
            raise FlowStop('EXISTING_CHAT_NOT_CONFIRMED')

    def return_to_main(self):
        if self.current != self.main:
            # Never send Esc to the main window: WeChat 3.9 can hide it to the
            # tray. Close only the already verified ContactProfileWnd handle.
            self.guard()
            gui = self.ops.win32gui
            if (self.current != getattr(self, 'card_hwnd', None)
                    or gui.GetClassName(self.current) != 'ContactProfileWnd'):
                raise FlowStop('UNEXPECTED_CLEANUP_WINDOW')
            card = self.current
            self.ops.require_active_ui_action_budget('hook_c1_close_verified_contact_card',
                metadata={'window_class':'ContactProfileWnd'})
            gui.PostMessage(card, self.ops.win32con.WM_CLOSE, 0, 0)
            deadline = time.monotonic()+2
            while gui.IsWindow(card) and gui.IsWindowVisible(card):
                self.identity_guard()
                if gui.GetForegroundWindow() != card:
                    raise FlowStop('WINDOW_FOCUS_OR_GEOMETRY_CHANGED')
                if time.monotonic() >= deadline:
                    raise FlowStop('CONTACT_CARD_CLOSE_UNCONFIRMED')
                time.sleep(.1)
            self.current = self.main
        self.guard()

    def verified_empty_search(self, frame=None):
        frame = frame or self.capture('header')
        try:
            return frame,empty_search(frame)
        except FlowStop as exc:
            if str(exc)!='SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED':raise
        # One purposeful input-only observation for a possible blinking caret.
        # Never mask a thin glyph; success needs an actually mark-free frame.
        self.guard(frame)
        self.search_empty_bounds=search_control_bounds(frame)
        self.ops.humanized_action_sleep(450,550)
        narrow=self.capture('search_empty')
        focused_search_empty_pixels(narrow.image)
        b=[0,0,narrow.image.width,narrow.image.height]
        return narrow,{'bounds':b,'point':[b[2]//2,b[3]//2],'frame':narrow.name,'proof':'focused_empty_pixels'}

    def clear_search(self):
        self.return_to_main()
        frame = self.capture('header')
        try:
            t = empty_search(frame)
        except FlowStop as exc:
            if str(exc) == 'SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED':
                frame, t = self.verified_empty_search(frame)
                self.cleanup_empty_proof = t.get('proof', 'placeholder_and_ocr')
                self.cleanup_action = 'already_empty_no_input'
                self.guard(frame)
                return
        else:
            self.cleanup_empty_proof = t.get('proof', 'placeholder_and_ocr')
            self.cleanup_action = 'already_empty_no_input'
            self.guard(frame)
            return
        b=search_control_bounds(frame)
        t={'bounds':b,'point':[(b[0]+b[2])//2,(b[1]+b[3])//2]}
        self.click(frame,t,'focus_search_cleanup')
        self.replace(frame,'')
        frame,t=self.verified_empty_search(self.capture('header'))
        self.cleanup_empty_proof=t.get('proof','placeholder_and_ocr')
        self.cleanup_action = 'query_cleared_once'
        self.guard(frame)
