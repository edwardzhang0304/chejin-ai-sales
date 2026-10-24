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
from .c1_layout import MAIN_REGIONS, main_regions, form_regions, valid_rect, main_key


class FlowStop(RuntimeError):
    def __init__(self, code, *, search_geometry=None):
        super().__init__(code)
        self.search_geometry = search_geometry


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


def verify_cached_search_geometry(frame: Frame, bounds, *, focused=False) -> None:
    """Validate local chrome around the saved input; never return a new position.

    The small halo detects changed/clipped edges. Component coordinates are
    diagnostic observations only; every action still uses the startup bounds.
    Focused input chrome retains the existing antialias-border tolerance.
    """
    if not valid_rect(bounds, frame.image.width, frame.image.height):
        raise FlowStop('SEARCH_STARTUP_LAYOUT_INVALIDATED')
    tolerance = max(1, round((bounds[3]-bounds[1])*.15)) if focused else 0
    halo = tolerance+1
    region = [max(0,bounds[0]-halo),max(0,bounds[1]-halo),
              min(frame.image.width,bounds[2]+halo),min(frame.image.height,bounds[3]+halo)]
    crop = frame.image.crop(tuple(region))
    observed = [[b[0]+region[0],b[1]+region[1],b[2]+region[0],b[3]+region[1]]
                for kind in (('search','white') if focused else ('search',))
                for b in components(crop,kind)]
    matches = [b for b in observed if all(abs(a-c)<=tolerance for a,c in zip(b,bounds))]
    if len(matches)!=1:
        raise FlowStop('SEARCH_CONTROL_GEOMETRY_CHANGED', search_geometry={
            'expected_bounds':list(bounds),'observed_local_components':observed,
            'verification_region':region,'tolerance':tolerance})


def empty_search(frame: Frame, cached_bounds=None) -> dict:
    w, h = frame.image.size
    # The default grey 搜索 placeholder is an empty input, not a query.
    aliases = ('搜索', 'Q搜索', 'O搜索', '0搜索', '搜素', 'Q搜素', 'O搜素', '0搜素')
    if not any(clean(r.get('text')) in aliases for r in frame.rows):
        b = list(cached_bounds) if cached_bounds is not None else search_control_bounds(frame)
        if cached_bounds is None:
            b = focused_empty_bounds(frame, b)
        else:
            focused_search_empty_pixels(frame.image.crop(tuple(b)))
        return {'bounds':b,'point':[(b[0]+b[2])//2,(b[1]+b[3])//2],'frame':frame.name,'proof':'focused_empty_pixels'}
    row = unique_text(frame, aliases, region=[0, 0, w, h])
    if cached_bounds is None:
        result = target(frame, row, filled='search')  # Startup / standalone replay only.
    else:
        b = list(cached_bounds)
        verify_cached_search_geometry(frame, b)
        if not contains(b,row):raise FlowStop('SEARCH_CONTROL_GEOMETRY_CHANGED')
        result = {'bounds':b,'point':[(b[0]+b[2])//2,(b[1]+b[3])//2],'frame':frame.name}
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


def focused_search_empty_pixels(image) -> None:
    from ..search_control_icons import empty_pixel_error
    error = empty_pixel_error(image)
    if error:
        raise FlowStop(error)


def focused_empty_bounds(frame: Frame, bounds: list[int]) -> list[int]:
    # Antialiased borders may lie outside the neutral-fill component at higher
    # DPI. Only extend symmetrically nearby, and still prove EVERY native pixel
    # including the border. Never erase/rescale observed text to make it match.
    error = None
    radius = max(1, round((bounds[3]-bounds[1])*.15))
    for margin in range(radius+1):
        b = [bounds[0]-margin, bounds[1]-margin, bounds[2]+margin, bounds[3]+margin]
        if b[0] < 0 or b[1] < 8 or b[2] > frame.image.width or b[3] > frame.image.height:
            continue
        try:
            focused_search_empty_pixels(frame.image.crop(tuple(b)))
            return b
        except FlowStop as exc:
            if error is None or str(exc) == 'SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED':
                error = exc
    raise error or FlowStop('SEARCH_EMPTY_STYLE_UNSUPPORTED')


def search_clear_target(frame: Frame, cached_bounds=None) -> dict:
    """Only a unique circle-X inside the current search input can clear it."""
    from ..search_control_icons import clear_icon_candidates
    if frame.name != 'header':raise FlowStop('SEARCH_FRAME_INVALID')
    b = list(cached_bounds) if cached_bounds is not None else search_control_bounds(frame)
    height = b[3]-b[1]
    candidates = clear_icon_candidates(frame.image,
        search_bounds=[b[2]-height, b[1], b[2], b[3]])
    if len(candidates) != 1:
        raise FlowStop('SEARCH_CLEAR_ICON_MISSING_OR_AMBIGUOUS')
    return {**candidates[0], 'frame': frame.name}


def search_query(frame: Frame, query: str, *, bounds=None) -> dict:
    w, h = frame.image.size
    upper = bounds if bounds is not None else [0, 0, w, min(h, 90)]
    row = unique_text(frame, (query,), region=upper)
    return target(frame, row)


def restricted(frame: Frame) -> bool:
    tokens = ('操作频繁', '账号异常', '安全验证', '限制使用', '被限制', '稍后再试', '请重新登录')
    return any(any(token in clean(r.get('text')) for token in tokens) for r in frame.rows)


def lookup(frame: Frame, query: str, *, search_bounds=None) -> dict:
    if restricted(frame):
        raise FlowStop('ACCOUNT_RESTRICTED')
    search_query(frame, query, bounds=search_bounds)
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


class InviteFormLayout:
    """One form instance's field positions, with fresh content/shape checks."""
    def __init__(self, frame):
        self.key = (frame.hwnd, frame.origin, dict(frame.geometry) if frame.geometry is not None else None, frame.image.size)
        self.fields = form_fields(frame)
        self.labels = {label: box(unique_text(frame, (label,))) for label in
                       ('申请添加朋友', '发送添加朋友申请', '备注名')}

    def current(self, frame):
        if (frame.hwnd, frame.origin, frame.geometry, frame.image.size) != self.key:
            raise FlowStop('INVITE_FORM_LAYOUT_CHANGED')
        for label, bounds in self.labels.items():
            if box(unique_text(frame, (label,))) != bounds:
                raise FlowStop('INVITE_FORM_LAYOUT_CHANGED')
        whites = components(frame.image, 'white')
        if any(field['bounds'] not in whites for field in self.fields.values()):
            raise FlowStop('INVITE_FORM_LAYOUT_CHANGED')
        return {key: {**field, 'frame': frame.name} for key, field in self.fields.items()}


def verify_fields(frame: Frame, greeting: str, remark: str, *, layout=None) -> dict:
    fields = layout.current(frame) if layout is not None else form_fields(frame)
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
        ui.guard_phase = name
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
        ui.owned_search_query = query
        ui.replace(frame, query)
        step('query_verified_and_network_lookup')
        frame = ui.capture('lookup')
        t = ui.lookup_target(frame, query) if hasattr(ui, 'lookup_target') else lookup(frame, query)
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
        form_layout = InviteFormLayout(frame)
        fields = form_layout.fields
        report['form_layout'] = {'source_frame':frame.name, 'hwnd':frame.hwnd,
                                 'origin':frame.origin, 'geometry':frame.geometry,
                                 'field_bounds':{key:list(value['bounds']) for key,value in fields.items()}}
        ui.click(frame, fields['greeting'], 'focus_greeting')
        ui.replace(frame, verify_message)
        # Reuse this form's positions. NativeUI still checks age, focus,
        # window geometry and occlusion before each input operation.
        ui.click(frame, fields['remark'], 'focus_remark')
        ui.replace(frame, remark_name)
        step('verify_remark_shortcode')
        frame = ui.capture('fields_filled')
        verify_fields(frame, verify_message, remark_name, layout=form_layout)
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
        details = getattr(ui, 'exception_diagnostic', None)
        if details is not None:report['exception'] = details(exc)
        if report['submit_attempted']:report['status'] = 'submission_unknown'
        if report['steps']:progress(report['steps'][-1],'waiting' if report['submit_attempted'] else 'error','failed')
    finally:
        if getattr(ui, 'window_events', None):
            report['window_events'] = ui.window_events
        if search_owned and not form_open and not form_transition:
            try:
                progress('cleanup','current')
                ui.guard_phase = 'cleanup'
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
                details = getattr(ui, 'exception_diagnostic', None)
                if details is not None:report['cleanup_exception'] = details(exc)
        elif form_open:
            report['cleanup'] = 'deferred_invite_dialog_still_open'
        elif form_transition:
            report['cleanup'] = 'deferred_invite_dialog_unconfirmed'
        if getattr(ui, 'cleanup_fallback_used', False):
            report['cleanup_fallback_used'] = True
        if getattr(ui, 'cleanup_control_match', None):
            report['cleanup_control_match'] = ui.cleanup_control_match
        if getattr(ui, 'guard_failures', None):
            report['guard_failures'] = list(ui.guard_failures)
        if getattr(ui, 'evidence_state', None) is not None:
            report['evidence'] = ui.evidence_state
        recorder = getattr(ui, 'record_outcome_diagnostic', None)
        if recorder is not None:
            try:
                recorder(report)
            except Exception as exc:
                fallback = getattr(ui, 'record_capture_error', None)
                if fallback is not None:fallback('diagnostics/outcome.json', exc, report)
    return report


class NativeUI:
    """Bounded ROI capture + original humanized input, with per-frame guards."""
    def __init__(self, ops, hwnd: int, pid: int, evidence: Path, identity_guard, *, organized_evidence=False, diagnostics=None, search_control_cache=None, require_startup_search_cache=False):
        self.diagnostics = diagnostics
        self.search_control_cache = search_control_cache if search_control_cache is not None else {}
        self.require_startup_search_cache = require_startup_search_cache
        self.evidence_state = {'frames': [], 'failures': []}
        self.guard_phase = 'initialization'
        self.guard_operation = 'create_ui'
        try:
            self._initialize(ops, hwnd, pid, evidence, identity_guard, organized_evidence=organized_evidence)
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.initialization_failure(exc, self)
            raise

    def _initialize(self, ops, hwnd, pid, evidence, identity_guard, *, organized_evidence):
        self.ops, self.main, self.pid = ops, hwnd, pid
        self.current = hwnd;self.evidence = evidence;self.identity_guard = identity_guard
        self.organized_evidence = organized_evidence
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
        self.screenshot_evidence = evidence/'screenshots' if organized_evidence else evidence
        self.ocr_evidence = evidence/'ocr' if organized_evidence else evidence
        self.screenshot_evidence.mkdir(parents=True, exist_ok=True)
        self.ocr_evidence.mkdir(parents=True, exist_ok=True)
        if not ops.ensure_dpi_awareness_status().get('per_monitor_aware'):
            raise FlowStop('DPI_AWARENESS_UNVERIFIED')
        if abs(self.initial['width']-1060) > 6 or abs(self.initial['height']-740) > 6:
            self._guard_stop('initial_window_size_rejected', code='ACCEPTED_WINDOW_SIZE_CHANGED',
                             observed={'geometry': self.initial, 'accepted_size': [1060, 740], 'tolerance': 6})
        from .hook_c1 import classify_visible_other_windows
        if classify_visible_other_windows(ops, hwnd, pid)['blocking_window_details']:
            raise FlowStop('CLOSE_WECHAT_POPUP_FIRST')
        ops.activate_window(hwnd, foreground_only=True)
        self.guard()

    def exception_diagnostic(self, exc):
        diagnostics = getattr(self, 'diagnostics', None)
        details = diagnostics.details(exc, self) if diagnostics is not None else {
            'exception_type': type(exc).__name__, 'phase': getattr(self, 'guard_phase', None),
            'operation': getattr(self, 'guard_operation', None)}
        if isinstance(exc,FlowStop) and exc.search_geometry is not None:
            details['search_control'] = exc.search_geometry
        return details

    def record_capture_error(self, target, exc, report):
        diagnostics = getattr(self, 'diagnostics', None)
        if diagnostics is not None:
            diagnostics.capture_failure(target, exc, report)
        else:
            state = report.setdefault('evidence', {'frames': [], 'failures': []})
            state['failures'].append({'target': target, **self.exception_diagnostic(exc)})

    def record_outcome_diagnostic(self, report):
        diagnostics = getattr(self, 'diagnostics', None)
        if diagnostics is not None:
            diagnostics.write('outcome.json', report)
        elif getattr(self, 'organized_evidence', False):
            folder = self.evidence/'diagnostics'
            folder.mkdir(parents=True, exist_ok=True)
            (folder/'outcome.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')

    def _save_frame_material(self, target, writer):
        try:
            writer()
        except Exception as exc:
            self.record_capture_error(target, exc, {'evidence': self.evidence_state})
            raise  # Preserve the original write-failure stop, never authorize input.

    def _guard_stop(self, condition, *, code='WINDOW_FOCUS_OR_GEOMETRY_CHANGED', observed=None):
        """Best-effort metadata only; a failed diagnostic must never unblock input.

        observed holds the value used by the failed check. The supplemental
        snapshot is sampled afterwards and may already reflect a changed window.
        No titles, chat text, process paths, screenshots or Hook calls are read.
        """
        if (condition in {'main_window_missing','main_geometry_changed','main_process_changed','main_dpi_changed'}
                or (self.current == self.main and condition in {'capture_geometry_changed','frame_geometry_changed'})):
            cache = getattr(self, 'search_control_cache', None)
            if cache is not None:cache.clear()
        try:
            failures = getattr(self, 'guard_failures', None)
            if failures is None:
                failures = self.guard_failures = []
            if len(failures) < 8:
                event = {'condition': condition, 'error_code': code,
                         'phase': getattr(self, 'guard_phase', 'initialization'),
                         'operation': getattr(self, 'guard_operation', 'guard'),
                         'time_utc': time.time(), 'observed': observed or {},
                         'expected': {'main_hwnd': self.main, 'current_hwnd': self.current,
                                      'pid': self.pid, 'main_geometry': dict(self.initial),
                                      'dpi': getattr(self, 'dpi', None)},
                         'snapshot': {}, 'read_errors': []}
                failures.append(event)
                o = self.ops; gui = o.win32gui
                def read(name, callback):
                    try:
                        value = callback()
                        event['snapshot'][name] = value
                        return value
                    except Exception as exc:
                        event['read_errors'].append({'field': name, 'type': type(exc).__name__})
                foreground = read('foreground_hwnd', gui.GetForegroundWindow)
                for name, hwnd in [('main', self.main), ('current', self.current), ('foreground', foreground)]:
                    if hwnd is None or not read(name + '_exists', lambda: bool(gui.IsWindow(hwnd))):
                        continue
                    read(name + '_pid', lambda: o.win32process.GetWindowThreadProcessId(hwnd)[1])
                    read(name + '_class', lambda: gui.GetClassName(hwnd))
                    read(name + '_visible', lambda: bool(gui.IsWindowVisible(hwnd)))
                    read(name + '_minimized', lambda: bool(gui.IsIconic(hwnd)))
                    read(name + '_geometry', lambda: o.get_window_geometry(hwnd))
                if hasattr(self, 'dpi'):
                    read('main_dpi', lambda: o.window_dpi_scale(self.main))
        except Exception:
            pass  # The original stop remains authoritative even if evidence fails.
        raise FlowStop(code)

    def _guard_main_window(self):
        self.identity_guard()
        o = self.ops;gui = o.win32gui
        if not gui.IsWindow(self.main):
            self._guard_stop('main_window_missing')
        if gui.IsIconic(self.main):
            self._guard_stop('main_window_minimized')
        if not gui.IsWindowVisible(self.main):
            self._guard_stop('main_window_hidden')
        geometry = o.get_window_geometry(self.main)
        if geometry != self.initial:
            self._guard_stop('main_geometry_changed', observed={'geometry': geometry})
        pid = o.win32process.GetWindowThreadProcessId(self.main)[1]
        if pid != self.pid:
            self._guard_stop('main_process_changed', observed={'pid': pid})
        if hasattr(self, 'dpi'):
            dpi = o.window_dpi_scale(self.main)
            if abs(dpi-self.dpi)>.01:
                self._guard_stop('main_dpi_changed', code='WINDOW_DPI_CHANGED', observed={'dpi': dpi})

    def guard(self, frame=None):
        self._guard_main_window()
        o = self.ops;gui = o.win32gui
        if not gui.IsWindow(self.current):
            self._guard_stop('current_window_missing')
        foreground = gui.GetForegroundWindow()
        if foreground != self.current:
            self._guard_stop('foreground_changed', observed={'foreground_hwnd': foreground})
        pid = o.win32process.GetWindowThreadProcessId(self.current)[1]
        if pid != self.pid:
            self._guard_stop('current_process_changed', observed={'pid': pid})
        if frame is not None:
            if frame.hwnd != self.current:
                self._guard_stop('frame_window_changed', code='STALE_ROI_FRAME', observed={'frame_hwnd': frame.hwnd})
            geometry = o.get_window_client_geometry(self.current)
            if geometry != frame.geometry:
                self._guard_stop('frame_geometry_changed', code='STALE_ROI_FRAME',
                                 observed={'geometry': geometry, 'frame_geometry': frame.geometry})
            age = time.monotonic()-frame.timestamp
            if age > 15:
                self._guard_stop('frame_expired', code='STALE_ROI_FRAME', observed={'age_seconds': age})
            if frame is not self.last_frame:
                self._guard_stop('frame_replaced', code='STALE_ROI_FRAME')

    def _bounds(self, label: str, w: int, h: int):
        if label in MAIN_REGIONS:
            if getattr(self, 'require_startup_search_cache', False):
                cache = self.search_control_cache
                geometry = self.ops.get_window_client_geometry(self.main)
                key = main_key(self.main, self.pid, geometry, self.dpi)
                regions = cache.get('regions', {})
                if (cache.get('key') != key or set(regions) != set(MAIN_REGIONS)
                        or any(not valid_rect(r, w, h) for r in regions.values())):
                    cache.clear()
                    raise FlowStop('SEARCH_STARTUP_LAYOUT_INVALIDATED')
                return tuple(regions[label])
            return main_regions(w, h)[label]  # Startup calibration / standalone replay.
        if label == 'search_empty':return tuple(self.search_empty_bounds)
        if label.startswith('fields') or label == 'footer':
            geometry = self.ops.get_window_client_geometry(self.current)
            key = (self.current, dict(geometry), self.ops.window_dpi_scale(self.current))
            cache = getattr(self, '_invite_regions', None)
            if cache is None:
                self._invite_regions = (key, form_regions(w, h))
            elif cache[0] != key:
                raise FlowStop('INVITE_FORM_LAYOUT_CHANGED')
            return self._invite_regions[1]['fields' if label.startswith('fields') else 'footer']
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
        self.guard_operation = 'capture:' + label
        if not hasattr(self, 'evidence_state'):self.evidence_state = {'frames': [], 'failures': []}
        material = {'label': label, 'screenshot': 'not_produced', 'ocr': 'not_produced', 'action_authorized': False}
        self.evidence_state['frames'].append(material)
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
        screenshot = f'{self.count:02d}-{label}.png'
        material.update(screenshot='produced_not_saved', screenshot_file=screenshot)
        self._save_frame_material('screenshots/'+screenshot, lambda: image.save(
            getattr(self,'screenshot_evidence',self.evidence)/screenshot))
        material['screenshot'] = 'saved'
        geometry = o.get_window_client_geometry(self.current)
        if geometry != c:
            self._guard_stop('capture_geometry_changed', code='CAPTURE_GEOMETRY_CHANGED',
                             observed={'geometry': geometry, 'frame_geometry': c})
        self.guard()
        self.guard_operation = 'ocr:' + label
        rows = o.run_ocr(image);self.ocr_count += 1
        ocr_file = f'{self.count:02d}-{label}.json'
        material.update(ocr='produced_not_saved', ocr_file=ocr_file)
        self._save_frame_material('ocr/'+ocr_file, lambda: (getattr(self,'ocr_evidence',self.evidence)/ocr_file).write_text(
            json.dumps(rows,ensure_ascii=False),encoding='utf-8'))
        material['ocr'] = 'saved'
        self.guard()
        frame = Frame(image, rows, label, self.current, (left, top), c, captured_at)
        self.last_frame = frame
        self.guard(frame)
        if restricted(frame):raise FlowStop('ACCOUNT_RESTRICTED')
        material['action_authorized'] = True
        return frame

    def click(self, frame: Frame, t: dict, action: str):
        self.guard_operation = 'click:' + action
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
        self.guard_operation = 'replace:' + frame.name
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
        self._invite_regions = None
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
                elif event.get('same_process') is False:
                    self._guard_stop('invite_foreground_process_changed', observed={'foreground_hwnd': foreground})
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
                foreground = gui.GetForegroundWindow()
                if foreground != card:
                    self._guard_stop('cleanup_foreground_changed', observed={'foreground_hwnd': foreground})
                if time.monotonic() >= deadline:
                    raise FlowStop('CONTACT_CARD_CLOSE_UNCONFIRMED')
                time.sleep(.1)
            self.current = self.main
        self.guard()

    def cached_search_bounds(self, frame):
        cache = getattr(self, 'search_control_cache', None)
        key = {'hwnd': frame.hwnd, 'pid': getattr(self, 'pid', None), 'origin': frame.origin,
               'geometry': dict(frame.geometry) if frame.geometry is not None else None,
               'dpi': getattr(self, 'dpi', None)}
        if cache is None:
            cache = self.search_control_cache = {}
        if cache.get('key') != key:
            cache.clear()
            if getattr(self, 'require_startup_search_cache', False):
                raise FlowStop('SEARCH_STARTUP_LAYOUT_INVALIDATED')
            cache['key'] = key
        if getattr(self, 'require_startup_search_cache', False):
            bounds = cache.get('bounds')
            if not valid_rect(bounds, frame.image.width, frame.image.height):
                cache.clear()
                raise FlowStop('SEARCH_STARTUP_LAYOUT_INVALIDATED')
        return cache.get('bounds')

    def lookup_target(self, frame, query):
        return lookup(frame, query, search_bounds=self.cached_search_bounds(frame))

    def calibrate_search_control(self):
        """Startup read-only calibration; existing text is never removed."""
        frame = self.capture('header')
        bounds = search_control_bounds(frame)
        try:
            target = empty_search(frame)
            bounds = target['bounds']
        except FlowStop:
            from ..search_control_icons import magnifier_candidates
            height = bounds[3]-bounds[1]
            candidates = magnifier_candidates(frame.image,
                search_bounds=[bounds[0],bounds[1],bounds[0]+height,bounds[3]])
            if len(candidates) != 1:
                raise FlowStop('SEARCH_STARTUP_CONTROL_UNRESOLVED')
        self.guard(frame)
        self.cached_search_bounds(frame)
        self.search_control_cache['bounds'] = list(bounds)
        self.search_control_cache['regions'] = main_regions(frame.geometry['width'], frame.geometry['height'])
        def save_layout():
            folder = self.evidence/'diagnostics'
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'search-layout.json').write_text(
                json.dumps({'source':'startup','bounds':bounds,'key':self.search_control_cache['key'],
                            'regions':self.search_control_cache['regions'],
                            'read_only':True},ensure_ascii=False),encoding='utf-8')
        self._save_frame_material('diagnostics/search-layout.json', save_layout)

    def verified_empty_search(self, frame=None):
        frame = frame or self.capture('header')
        bounds = self.cached_search_bounds(frame)
        try:
            target = empty_search(frame, bounds)
        except FlowStop as exc:
            if str(exc)=='SEARCH_CONTROL_GEOMETRY_CHANGED':self.search_control_cache.clear()
            if str(exc)!='SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED':raise
        else:
            if bounds is None:self.search_control_cache['bounds'] = list(target['bounds'])
            return frame, target
        # One purposeful input-only observation for a possible blinking caret.
        # Never mask a thin glyph; success needs an actually mark-free frame.
        self.guard(frame)
        self.search_empty_bounds = list(bounds) if bounds is not None else search_control_bounds(frame)
        self.ops.humanized_action_sleep(450,550)
        narrow=self.capture('search_empty')
        focused_search_empty_pixels(narrow.image)
        if bounds is None:self.search_control_cache['bounds'] = list(self.search_empty_bounds)
        b=[0,0,narrow.image.width,narrow.image.height]
        return narrow,{'bounds':b,'point':[b[2]//2,b[3]//2],'frame':narrow.name,'proof':'focused_empty_pixels'}

    def require_owned_search_query(self, frame, bounds):
        # An icon alone is not proof of text, nor permission to erase someone
        # else's input. Only this flow's exact query authorizes deletion.
        # Validate cached bounds against this already captured frame, without
        # selecting a new point. Internal layout changes must not reuse it.
        try:
            verify_cached_search_geometry(frame,bounds,focused=True)
        except FlowStop:
            getattr(self,'search_control_cache',{}).clear()
            raise
        expected = getattr(self, 'owned_search_query', None)
        if not expected:
            raise FlowStop('SEARCH_CONTENT_OWNERSHIP_UNRESOLVED')
        from ..search_control_icons import clear_icon_candidates, magnifier_candidates
        height = bounds[3]-bounds[1]
        left_icons = magnifier_candidates(frame.image,
            search_bounds=[bounds[0],bounds[1],bounds[0]+height,bounds[3]])
        right_icons = clear_icon_candidates(frame.image,
            search_bounds=[bounds[2]-height,bounds[1],bounds[2],bounds[3]])
        matches = 0
        for row in frame.rows:
            value = clean(row.get('text'))
            if not value:continue
            r = box(row)
            if r[2] <= bounds[0] or r[0] >= bounds[2] or r[3] <= bounds[1] or r[1] >= bounds[3]:continue
            if not contains(bounds,row):
                raise FlowStop('SEARCH_CONTENT_OWNERSHIP_UNRESOLVED')
            left_proved = len(left_icons)==1
            right_proved = len(right_icons)==1
            merged_icon = (value in ('Q'+expected,'O'+expected) and left_proved
                           and r[0] < bounds[0]+height*.8)
            if (value == expected or merged_icon) and row.get('confidence',0) >= .8:
                matches += 1
                continue
            # Exempt OCR icon tokens only in the pixel-verified icon itself.
            if value in ('Q','O','0') and left_proved and contains(left_icons[0]['bounds'],row):continue
            if value in ('X','x','×') and right_proved and contains(right_icons[0]['bounds'],row):continue
            # Unknown/low-confidence text inside or crossing the input is not
            # discarded; it prevents both icon deletion and keyboard fallback.
            raise FlowStop('SEARCH_CONTENT_OWNERSHIP_UNRESOLVED')
        if matches != 1:
            raise FlowStop('SEARCH_CONTENT_OWNERSHIP_UNRESOLVED')

    def clear_search(self):
        self.guard_operation = 'clear_search'
        self.return_to_main()
        frame = self.capture('header')
        try:
            frame, target = self.verified_empty_search(frame)
        except FlowStop as exc:
            if str(exc) in {'SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED',
                            'SEARCH_CONTROL_GEOMETRY_CHANGED', 'SEARCH_STARTUP_LAYOUT_INVALIDATED'}:raise
        else:
            self.cleanup_empty_proof = target.get('proof', 'placeholder_and_ocr')
            self.cleanup_action = 'already_empty_no_input'
            self.guard(frame)
            return
        bounds = self.cached_search_bounds(frame)
        if bounds is None:
            # Compatibility for a standalone cleanup caller. Normal C1 caches
            # this on the initial, verified empty header before typing.
            bounds = search_control_bounds(frame)
            self.search_control_cache['bounds'] = list(bounds)
        self.require_owned_search_query(frame, bounds)
        try:
            target = search_clear_target(frame, bounds)
        except FlowStop as exc:
            if str(exc) != 'SEARCH_CLEAR_ICON_MISSING_OR_AMBIGUOUS':raise
        else:
            self.cleanup_control_match = {k:target[k] for k in ('method','bounds','confidence','template_scale')}
            self.click(frame, target, 'clear_search_icon')
            frame = self.capture('header')
            try:
                checked, target = self.verified_empty_search(frame)
            except FlowStop as exc:
                if str(exc) in {'SEARCH_EMPTY_CARET_OR_TEXT_UNRESOLVED',
                                'SEARCH_CONTROL_GEOMETRY_CHANGED', 'SEARCH_STARTUP_LAYOUT_INVALIDATED'}:raise
            else:
                self.cleanup_empty_proof = target.get('proof','placeholder_and_ocr')
                self.cleanup_action = 'clear_icon_clicked_once'
                self.guard(checked)
                return
        # At most one fallback, at the cached input point. Its fresh screenshot
        # proves retained owned content; it does not relocate the input.
        self.require_owned_search_query(frame, bounds)
        target = {'bounds':list(bounds),'point':[(bounds[0]+bounds[2])//2,(bounds[1]+bounds[3])//2], 'frame':frame.name}
        self.cleanup_fallback_used = True
        self.click(frame, target, 'focus_search_cleanup_fallback')
        self.replace(frame, '')
        checked, target = self.verified_empty_search(self.capture('header'))
        self.cleanup_empty_proof = target.get('proof','placeholder_and_ocr')
        self.cleanup_action = 'clear_fallback_deleted_once'
        self.guard(checked)
