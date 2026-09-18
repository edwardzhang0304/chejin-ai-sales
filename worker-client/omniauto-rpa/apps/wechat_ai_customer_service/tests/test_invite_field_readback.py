"""Optional native field evidence; OS boundaries are controlled, not Windows acceptance."""
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


# r4 restores OCR-only admission. Native reads below are retained only for
# evidence and protection against overwriting a manual edit during one refill.
# The retired full-equality/readback override cases live in the frozen r3 source.
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
