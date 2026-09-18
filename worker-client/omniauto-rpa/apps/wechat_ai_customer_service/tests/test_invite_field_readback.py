"""D3/D6 actual field/OCR disagreements; OS boundaries are controlled, not Windows acceptance."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.wechat_ai_customer_service.adapters.add_friend_layout import invite_form_field_verification
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.field_value_readback import _read_control, _TextClipboard


GREETING = '您好，我是车金二手车的顾问'
CODE = 'CJTRSFZT'
BOUNDS = {'verify_message': [0, 0, 300, 80], 'remark_name': [0, 100, 300, 160],
          'remark_code': [0, 100, 300, 160]}


def item(text, y, confidence=.99):
    return {'text': text, 'confidence': confidence, 'left': 10, 'top': y,
            'right': 250, 'bottom': y+15, 'center_x': 130, 'center_y': y+7}


def value(text, **extra):
    return {'available': True, 'source_verified': True, 'value': text, 'method': 'uia_value', **extra}


def verify(greeting=GREETING, code=CODE, confidence=.99, fields=None):
    return invite_form_field_verification(verify_message=GREETING, remark_name=CODE, remark_code=CODE,
        ocr_items=[item(greeting, 20), item(code, 120, confidence)], field_bounds=BOUNDS, field_values=fields)


@pytest.mark.parametrize('method', ['uia_value', 'uia_text', 'clipboard_copy'])
def test_real_greeting_can_resolve_ocr_omission_without_changing_observed_text(method):
    fields = {'verify_message': value(GREETING, method=method)}
    original = deepcopy(fields)
    result = verify(greeting=GREETING.replace('顾', ''), fields=fields)
    assert result['ok']
    assert result['verify_message']['matched_by'] == method
    assert fields == original


@pytest.mark.parametrize('actual', [GREETING.replace('顾', ''), GREETING+'请回复', '错字段', GREETING+'!'])
def test_actual_mismatch_vetoes_even_matching_ocr(actual):
    assert not verify(fields={'verify_message': value(actual)})['ok']


@pytest.mark.parametrize('fields', [None, {'verify_message': {'available': False}},
    {'verify_message': value(GREETING, source_verified=False)},
    {'verify_message': value(GREETING, identity_changed=True)}])
def test_unavailable_unverified_or_changed_field_cannot_fill_an_ocr_gap(fields):
    assert not verify(greeting=GREETING.replace('顾', ''), fields=fields)['ok']


@pytest.mark.parametrize('actual,ocr,confidence,expected', [
    (CODE, CODE, .899, True), (None, CODE, .899, False), (None, CODE, .90, True),
    ('CJABCDEF', CODE, .99, False), ('CJABCDEF', 'CJABCDEF', .99, False),
    (CODE, 'CJTRSFZ1', .99, False), (CODE, 'CJTRSFZ1', .899, False),
    ('cjtrsfzt', CODE+'|', .899, True), (CODE, CODE+'9', .99, False),
    ('ＣＪＴＲＳＦＺＴ', CODE, .899, False),
])
def test_full_expected_code_and_independent_readback_required(actual, ocr, confidence, expected):
    fields = {'remark_name': value(actual)} if actual is not None else None
    result = verify(code=ocr, confidence=confidence, fields=fields)
    assert result['ok'] is expected


def test_greeting_does_not_pass_just_because_phone_digits_are_visible():
    result = invite_form_field_verification(verify_message='您好我是张顾问13812345678',
        remark_name=CODE, remark_code=CODE, ocr_items=[item('13812345678', 20), item(CODE, 120)],
        field_bounds=BOUNDS)
    assert not result['ok']


class Clipboard:
    def __init__(self):
        self.sequence, self.owner, self.text = 10, 123, 'original clipboard'
        self.restores = []
    def snapshot(self):
        return self.sequence, [(13, self.text)]
    def current(self):
        return self.sequence, self.owner, self.text
    def restore(self, sequence, saved):
        self.restores.append(sequence)
        if self.sequence != sequence:
            return 'third_party_change_preserved'
        self.text = saved[0][1]
        return 'restored'


class Control:
    ProcessId = 20
    BoundingRectangle = {'left': 10, 'top': 20, 'right': 200, 'bottom': 70}
    def __init__(self, text, pattern):
        self.text, self.pattern, self.focused = text, pattern, False
    def GetRuntimeId(self):
        return [1, 2]
    def GetValuePattern(self):
        if self.pattern != 'value': raise RuntimeError('value unavailable')
        return SimpleNamespace(Value=self.text)
    def GetTextPattern(self):
        if self.pattern != 'text': raise RuntimeError('text unavailable')
        return SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _: self.text))
    def SetFocus(self):
        self.focused = True


@pytest.mark.parametrize('pattern', ['value', 'text'])
def test_native_reader_returns_control_value_without_knowing_expected(pattern):
    control, clipboard = Control('independent actual field', pattern), Clipboard()
    ops = SimpleNamespace(uia_rect_to_dict=dict, hotkey=lambda *_: pytest.fail('UIA read must not type'))
    result = _read_control(control, identity=(20, (1, 2), (10, 20, 200, 70)), guard=lambda: True,
                          auto=None, clipboard=clipboard, ops=ops, pid=20)
    assert result['available'] and result['value'] == 'independent actual field'
    assert clipboard.text == 'original clipboard' and not clipboard.restores


@pytest.mark.parametrize('case', ['normal', 'stale', 'wrong_owner', 'focus_change', 'third_party'])
def test_copy_sequence_focus_and_compare_and_restore(monkeypatch, case):
    control, clipboard = Control('actual field from copy', None), Clipboard()
    keys = []
    focused = [True]
    def key(_ctrl, key):
        keys.append(chr(key))
        if key == ord('C') and case != 'stale':
            clipboard.sequence += 1
            clipboard.text = control.text
            if case == 'wrong_owner': clipboard.owner = 999
            if case == 'focus_change': focused[0] = False
    ops = SimpleNamespace(uia_rect_to_dict=dict, hotkey=key, win32con=SimpleNamespace(VK_CONTROL=17),
        win32process=SimpleNamespace(GetWindowThreadProcessId=lambda owner: (1, 20 if owner == 123 else 99)))
    auto = SimpleNamespace(GetFocusedControl=lambda: control if focused[0] else Control('other', None))
    # Different field within the same app has a different identity.
    other = Control('other', None); other.GetRuntimeId = lambda: [3, 4]
    auto.GetFocusedControl = lambda: control if focused[0] else other
    if case == 'third_party':
        old_restore = clipboard.restore
        def changed_before_restore(sequence, saved):
            clipboard.sequence += 1; clipboard.text = 'new third-party clipboard'
            return old_restore(sequence, saved)
        clipboard.restore = changed_before_restore
    result = _read_control(control, identity=(20, (1, 2), (10, 20, 200, 70)), guard=lambda: True,
                          auto=auto, clipboard=clipboard, ops=ops, pid=20)
    assert keys == ['A', 'C']
    assert result['available'] is (case in {'normal', 'third_party'})
    if case in {'normal', 'focus_change', 'stale'}: assert clipboard.text == 'original clipboard'
    if case == 'third_party': assert clipboard.text == 'new third-party clipboard'
    if case == 'wrong_owner': assert not clipboard.restores


def test_clipboard_unknown_format_is_not_cleared_or_copied():
    calls = []
    cb = SimpleNamespace(OpenClipboard=lambda: calls.append('open'), CloseClipboard=lambda: calls.append('close'),
                         EnumClipboardFormats=lambda _: 2)  # bitmap
    with pytest.raises(ValueError, match='not_restorable'):
        _TextClipboard(cb, None).snapshot()
    assert calls == ['open', 'close']


@pytest.mark.parametrize('case,can_confirm,review_count', [
    ('ocr_ok', True, 1), ('uia_greeting', True, 1), ('copy_greeting', True, 2),
    ('actual_wrong', False, 1), ('wrong_code', False, 1), ('low_code', True, 1),
    ('low_unavailable', False, 2), ('ocr_conflict_recovers', True, 2),
    ('ocr_conflict_persists', False, 2), ('focus_changed', False, 2),
])
def test_form_flow_confirms_once_without_refilling(monkeypatch, tmp_path, case, can_confirm, review_count):
    """Real form orchestration/validation, controlled layout/OCR/native reads.

    No invite receipt is fabricated: stop at the physical confirm boundary.
    Native source extraction is covered separately above; this is not Windows UAT.
    """
    from PIL import Image
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows as form
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import field_value_readback as native

    class ConfirmReached(Exception): pass
    class Ops:
        def __init__(self):
            self.reviews, self.pastes, self.clicks, self.snapshot_no = 0, [], [], 1
            self.shot = Image.new('RGB', (400, 400), 'white')
        def targets(self):
            return {name: {'bounds': bounds, 'click_bounds': bounds, 'x': bounds[0]+10,
                          'y': bounds[1]+10, 'layout_snapshot_id': str(self.snapshot_no)}
                    for name,bounds in [('invite_greeting_textarea', BOUNDS['verify_message']),
                        ('invite_remark_input', BOUNDS['remark_name']),
                        ('invite_confirm_button', [10, 300, 150, 360])]}
        def layout_snapshot_for_image(self, shot):
            return {'window_rect': [0, 0, 400, 400], 'layout_snapshot_id': str(self.snapshot_no)}
        def add_friend_paced_pause(self, *args, **kwargs): return 0
        def capture_wechat_window_visible_screen(self, *args, **kwargs): return self.shot, 'controlled-before.png'
        def run_ocr_on_screen_region(self, *args): return []
        def paste_invite_form_text(self, hwnd, target, text, **kwargs):
            self.pastes.append((kwargs['action_name'],text)); return {'ok': True}
        def capture_invite_form_field_review(self, *args, **kwargs):
            self.reviews += 1; self.snapshot_no += 1
            greeting = GREETING if case in {'ocr_ok','low_code','low_unavailable'} else GREETING.replace('顾','')
            code = 'CJABCDEF' if case == 'wrong_code' else CODE
            if case.startswith('ocr_conflict') and (self.reviews == 1 or case.endswith('persists')): code = 'CJTRSFZ1'
            confidence = .899 if case in {'low_code','low_unavailable'} else .99
            items = [item(greeting, 20),item(code,120,confidence)]
            checks = invite_form_field_verification(verify_message=GREETING, remark_name=CODE,
                remark_code=CODE, ocr_items=items,field_bounds=BOUNDS)
            targets=self.targets()
            return dict(shot=self.shot,screenshot_path='controlled-filled.png',annotated_path='',
                ocr_items=items,ocr_seconds=0,targets_map=targets,targets=list(targets.values()),
                field_verification=checks)
        def human_window_image_click_in_bounds(self, hwnd,x,y,**kwargs):
            assert kwargs['expected_snapshot_id'] == str(self.snapshot_no)
            self.clicks.append(kwargs['action_name']); raise ConfirmReached()

    ops=Ops()
    read_calls=[]
    def read_fields(hwnd, targets, **kwargs):
        read_calls.append(targets)
        if case in {'ocr_ok'}: pytest.fail('normal OCR must not perform native reads')
        if case == 'low_unavailable': return {'remark_name': {'available': False}}
        fields={'verify_message':value(GREETING), 'remark_name':value(CODE)}
        if case=='actual_wrong': fields['verify_message']=value('您好，我是车金二手车问')
        if case=='wrong_code': fields['remark_name']=value('CJABCDEF')
        if case=='focus_changed': fields['verify_message']=value(GREETING,identity_changed=True)
        for name,field in fields.items():
            field.update(hwnd=hwnd,window_rect=[0,0,400,400],field_bounds=BOUNDS[name])
            if case=='copy_greeting': field.update(method='clipboard_copy',requires_fresh_layout=True)
        return fields
    monkeypatch.setattr(form,'_SIDECAR_OPS',ops)
    monkeypatch.setattr(form,'add_friend_invite_form_targets',lambda *a,**kw:ops.targets())
    monkeypatch.setattr(form,'draw_add_friend_screen_annotation',lambda *a,**kw:'')
    monkeypatch.setattr(native,'read_invite_fields',read_fields)
    if can_confirm:
        with pytest.raises(ConfirmReached):
            form.fill_add_friend_invite_form_and_confirm(1001,tmp_path,verify_message=GREETING,remark_name=CODE,remark_code=CODE)
    else:
        result=form.fill_add_friend_invite_form_and_confirm(1001,tmp_path,verify_message=GREETING,remark_name=CODE,remark_code=CODE)
        assert not result['ok'] and result['confirm']['skipped']
        assert result['fill_retry_attempts']==[]
    assert ops.pastes==[('invite_greeting',GREETING),('invite_remark',CODE)]
    assert ops.reviews==review_count
    assert ops.clicks==(['invite_confirm_button_click'] if can_confirm else [])
    assert len(read_calls)==(0 if case=='ocr_ok' else 1)


def test_failed_read_of_our_copy_still_restores_original():
    control, clipboard = Control('actual', None), Clipboard()
    def key(ctrl,key):
        if key==ord('C'): clipboard.sequence+=1; clipboard.text=None
    ops=SimpleNamespace(uia_rect_to_dict=dict,hotkey=key,win32con=SimpleNamespace(VK_CONTROL=17),
        win32process=SimpleNamespace(GetWindowThreadProcessId=lambda hwnd:(1,20)))
    result=_read_control(control,identity=(20,(1,2),(10,20,200,70)),guard=lambda:True,
        auto=SimpleNamespace(GetFocusedControl=lambda:control),clipboard=clipboard,ops=ops,pid=20)
    assert not result['available'] and result['clipboard_restore']=='restored'
    assert clipboard.text=='original clipboard'


@pytest.mark.parametrize('case', ['normal', 'wrong_process', 'ambiguous', 'foreground_changed', 'snapshot_changed'])
def test_reader_selects_only_unique_field_in_current_window(monkeypatch,case):
    import sys
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.field_value_readback import read_invite_fields
    control=Control('actual native value', 'value')
    if case=='wrong_process':control.ProcessId=99
    controls=[control,control] if case=='ambiguous' else [control]
    monkeypatch.setitem(sys.modules,'uiautomation',SimpleNamespace(ControlFromHandle=lambda hwnd:object()))
    monkeypatch.setitem(sys.modules,'win32clipboard',SimpleNamespace())
    snapshot={'window_rect':[0,0,400,400],'capture_screen_origin':[0,0]}
    ops=SimpleNamespace(
        _current_click_snapshot=lambda *a,**kw:(snapshot,'stale' if case=='snapshot_changed' else ''),
        foreground_window_matches_target=lambda hwnd:{'ok':case!='foreground_changed'},
        win32process=SimpleNamespace(GetWindowThreadProcessId=lambda hwnd:(1,20)),
        win32gui=SimpleNamespace(GetWindowRect=lambda hwnd:(0,0,400,400)),
        collect_uia_controls=lambda *a,**kw:controls,safe_uia_attr=lambda c,key:'EditControl',
        uia_rect_to_dict=dict,_screen_rect_inside_bounds=lambda rect,bounds:all([
            rect['left']>=bounds[0],rect['top']>=bounds[1],rect['right']<=bounds[2],rect['bottom']<=bounds[3]]),
    )
    result=read_invite_fields(1001,{'verify_message':{'bounds':BOUNDS['verify_message'],'layout_snapshot_id':'one'}},ops=ops)['verify_message']
    assert result['available'] is (case=='normal')
    if case=='normal':
        assert result['value']=='actual native value' and result['field_bounds']==BOUNDS['verify_message']
    if case=='foreground_changed':assert result['identity_changed'] is True
