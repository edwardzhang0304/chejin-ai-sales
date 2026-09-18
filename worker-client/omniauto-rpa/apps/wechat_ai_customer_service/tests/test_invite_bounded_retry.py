"""Formal form orchestration, controlled desktop: never claims Windows acceptance."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows as form
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import field_value_readback as native

GREETING = '您好，我是测试顾问'
CODE = 'CJABCDEF'


def item(text, left, top, right, bottom):
    return dict(text=text, confidence=.99, left=left, top=top, right=right,
                bottom=bottom, center_x=(left+right)/2, center_y=(top+bottom)/2)


class ConfirmReached(Exception):
    pass


class Desktop:
    """A form whose remark moves after greeting edits, with observable failed writes."""
    def __init__(self, *, displacement, failed_retry=False, close_mode='closed', bad_greeting=False):
        self.displacement = displacement
        self.failed_retry = failed_retry
        self.close_mode = close_mode
        self.bad_greeting = bad_greeting
        self.greeting = '原申请语'
        self.remark = 'Original contact'
        self.shift = 0
        self.actions = []
        self.snapshots = {}
        self.images = []
        self.alive = True
        self.win32gui = SimpleNamespace(IsWindow=lambda hwnd:self.alive, IsWindowVisible=lambda hwnd:self.alive)

    def add_friend_paced_pause(self, *a, **k): return 0

    def layout_snapshot_for_image(self, shot): return self.snapshots[id(shot)]

    def capture_wechat_window_visible_screen(self, hwnd, **kwargs):
        shot = Image.new('RGB', (468, 809), 'white')
        self.images.append(shot)
        self.snapshots[id(shot)] = dict(executable=True, surface_bounds=[0,0,468,809],
            window_rect=[0,0,468,809], layout_snapshot_id=f'frame-{len(self.images)}')
        return shot, 'controlled-form.png'

    def run_ocr_on_screen_region(self, *a):
        return [item('申请添加朋友',182,22,288,44), item('发送添加朋友申请',54,84,176,101),
                item(self.greeting,54,129,220,155), item('备注',52,240+self.shift,87,261+self.shift),
                item(self.remark,55,291+self.shift,180,314+self.shift),
                item('确定',128,739,171,765), item('取消',298,739,341,765)]

    def paste_invite_form_text(self, hwnd, target, text, *, action_name, **kwargs):
        self.actions.append((action_name, deepcopy(target)))
        if action_name == 'invite_greeting':
            self.greeting = '未写全' if self.bad_greeting else text
            self.shift = self.displacement
        elif action_name == 'invite_greeting_retry':
            self.greeting = text
            self.shift += self.displacement  # another UI reflow before remark retry
        elif action_name.startswith('invite_remark'):
            # The old coordinates only succeed when no reflow happened. A
            # retry must use the current locator, not a separately forced value.
            expected_y = max(261+self.shift+64, int(809*.39))
            expected_y = int((261+self.shift+8+expected_y)/2)
            if target['y'] == expected_y and not self.failed_retry:
                self.remark = text
        return {'ok': True}

    def capture_invite_form_field_review(self, *a, **kw):
        return form.capture_invite_form_field_review(*a, **kw)

    def human_window_image_click_in_bounds(self, hwnd, x, y, **kwargs):
        self.actions.append((kwargs['action_name'], {'x':x,'y':y,'snapshot':kwargs['expected_snapshot_id']}))
        assert kwargs['expected_snapshot_id'] == self.snapshots[id(self.images[-1])]['layout_snapshot_id']
        if kwargs['action_name'] == 'invite_confirm_button_click':
            assert self.remark == CODE and self.greeting == GREETING
            raise ConfirmReached()
        assert kwargs['action_name'] == 'failed_invite_cancel'
        assert 298 <= x <= 341 and 739 <= y <= 765  # current OCR cancel, not confirm or title-bar coordinates
        if self.close_mode == 'exception': raise OSError('controlled close error')
        if self.close_mode == 'closed': self.alive = False
        return {'ok': self.close_mode != 'click_failed'}


def install(monkeypatch, **kwargs):
    desktop = Desktop(**kwargs)
    monkeypatch.setattr(form, '_SIDECAR_OPS', desktop)
    monkeypatch.setattr(form, 'draw_add_friend_screen_annotation', lambda *a,**k:'')
    monkeypatch.setattr(native, 'read_invite_fields', lambda *a,**k:{
        'verify_message':{'available':False,'reason':'field_control_not_unique'},
        'remark_name':{'available':False,'reason':'field_control_not_unique'}})
    return desktop


def run(tmp_path):
    return form.fill_add_friend_invite_form_and_confirm(1001,tmp_path,
        verify_message=GREETING,remark_name=CODE,remark_code=CODE)


@pytest.mark.parametrize('displacement', [0, 22, 37, 64])
def test_moving_field_is_filled_once_at_fresh_location(monkeypatch, tmp_path, displacement):
    desktop=install(monkeypatch,displacement=displacement)
    with pytest.raises(ConfirmReached): run(tmp_path)
    names=[name for name,_ in desktop.actions]
    assert names == ['invite_greeting','invite_remark'] + (
        ['invite_remark_retry'] if displacement else []) + ['invite_confirm_button_click']
    assert len(desktop.images) == (4 if displacement else 2)


def test_greeting_retry_reflows_before_remark_retry(monkeypatch,tmp_path):
    desktop=install(monkeypatch,displacement=22,bad_greeting=True)
    with pytest.raises(ConfirmReached): run(tmp_path)
    assert [name for name,_ in desktop.actions] == [
        'invite_greeting','invite_remark','invite_greeting_retry','invite_remark_retry','invite_confirm_button_click']
    assert desktop.actions[3][1]['y'] > desktop.actions[1][1]['y'] + 22


@pytest.mark.parametrize('actual_value', ['Original contact', 'Someone manually changed this'])
@pytest.mark.parametrize('bad_greeting', [False, True])
def test_native_read_of_untouched_original_can_retry_but_changed_value_cannot(monkeypatch,tmp_path,actual_value,bad_greeting):
    desktop=install(monkeypatch,displacement=22,bad_greeting=bad_greeting)
    def read(hwnd,targets,**kwargs):
        return {'remark_name':dict(available=True,source_verified=True,value=actual_value,
            method='uia_value',hwnd=hwnd,window_rect=[0,0,468,809],field_bounds=targets['remark_name']['bounds'])}
    monkeypatch.setattr(native,'read_invite_fields',read)
    if actual_value=='Original contact':
        with pytest.raises(ConfirmReached):run(tmp_path)
        assert [name for name,_ in desktop.actions].count('invite_remark_retry')==1
    else:
        result=run(tmp_path)
        assert not result['ok'] and result['confirm']['skipped']
        assert not any(name.endswith('_retry') for name,_ in desktop.actions)


@pytest.mark.parametrize('close_mode', ['closed','still_visible','click_failed','exception'])
def test_persistent_failure_cancels_but_never_confirms_or_retries_forever(monkeypatch,tmp_path,close_mode):
    desktop=install(monkeypatch,displacement=22,failed_retry=True,close_mode=close_mode)
    result=run(tmp_path)
    assert result['error_code']=='INVITE_FIELD_VERIFICATION_FAILED'
    assert result['confirm']['skipped'] is True
    assert [name for name,_ in desktop.actions] == [
        'invite_greeting','invite_remark','invite_remark_retry','failed_invite_cancel']
    cleanup=result['after']['final_status']['pre_confirm_cleanup']
    assert cleanup['closed'] is (close_mode=='closed')
    assert len(result['fill_retry_attempts'])==1


def test_missing_cancel_anchor_does_not_guess_a_close_coordinate(monkeypatch,tmp_path):
    desktop=install(monkeypatch,displacement=22,failed_retry=True)
    original=desktop.run_ocr_on_screen_region
    monkeypatch.setattr(desktop,'run_ocr_on_screen_region',lambda *a:[i for i in original(*a) if i['text']!='取消'])
    result=run(tmp_path)
    assert result['error_code']=='INVITE_FIELD_VERIFICATION_FAILED'
    assert [name for name,_ in desktop.actions]==['invite_greeting','invite_remark','invite_remark_retry']
    assert not result['after']['final_status']['pre_confirm_cleanup']['attempted']


@pytest.mark.parametrize('parent_title', ['添加朋友', '微信'])
def test_parent_cleanup_requires_its_own_current_identity(monkeypatch,tmp_path,parent_title):
    desktop=install(monkeypatch,displacement=22,failed_retry=True)
    original_capture=desktop.capture_wechat_window_visible_screen
    original_ocr=desktop.run_ocr_on_screen_region
    original_click=desktop.human_window_image_click_in_bounds
    original_is_window=desktop.win32gui.IsWindow
    parent_alive=[True]
    parent_images=set()
    def capture(hwnd,**kwargs):
        shot,path=original_capture(hwnd,**kwargs)
        if hwnd==2002:
            parent_images.add(id(shot))
            desktop.snapshots[id(shot)]['surface_kind']='popup'
        return shot,path
    def ocr(shot,*args):
        return [item(parent_title,182,22,288,44)] if id(shot) in parent_images else original_ocr(shot,*args)
    def click(hwnd,x,y,**kwargs):
        if hwnd!=2002:return original_click(hwnd,x,y,**kwargs)
        assert parent_title=='添加朋友'
        assert kwargs['action_name']=='failed_invite_parent_close'
        assert kwargs['expected_snapshot_id']==desktop.snapshots[id(desktop.images[-1])]['layout_snapshot_id']
        desktop.actions.append((kwargs['action_name'],dict(x=x,y=y)))
        parent_alive[0]=False
        return {'ok':True}
    desktop.capture_wechat_window_visible_screen=capture
    desktop.run_ocr_on_screen_region=ocr
    desktop.human_window_image_click_in_bounds=click
    desktop.win32gui.IsWindow=lambda hwnd:parent_alive[0] if hwnd==2002 else original_is_window(hwnd)
    desktop.win32gui.IsWindowVisible=desktop.win32gui.IsWindow
    result=form.fill_add_friend_invite_form_and_confirm(1001,tmp_path,
        verify_message=GREETING,remark_name=CODE,remark_code=CODE,parent_dialog_hwnd=2002)
    cleanup=result['after']['final_status']['pre_confirm_cleanup']
    assert cleanup['closed']
    assert cleanup['parent']['closed'] is (parent_title=='添加朋友')
    assert ('failed_invite_parent_close' in [name for name,_ in desktop.actions]) is (parent_title=='添加朋友')


@pytest.mark.parametrize('readback_available', [False, True])
def test_unchanged_empty_field_still_gets_its_one_refill(monkeypatch, tmp_path, readback_available):
    desktop = install(monkeypatch, displacement=22)
    desktop.greeting = ''
    original_paste = desktop.paste_invite_form_text
    def paste(hwnd, target, text, *, action_name, **kwargs):
        if action_name == 'invite_greeting':
            desktop.actions.append((action_name, target))
            desktop.shift = 22
            return {'ok': True}  # reported input action, but no text reached the field
        return original_paste(hwnd, target, text, action_name=action_name, **kwargs)
    desktop.paste_invite_form_text = paste
    def read(hwnd, targets, **kwargs):
        return {'verify_message': dict(available=True, source_verified=True, value='',
            method='uia_value', hwnd=hwnd, window_rect=[0,0,468,809],
            field_bounds=targets['verify_message']['bounds'])}
    if readback_available:
        monkeypatch.setattr(native, 'read_invite_fields', read)
    confirmed = False
    try:
        result = run(tmp_path)
    except ConfirmReached:
        confirmed = True
        result = {'physical_confirm_boundary_reached': True}
    assert confirmed, {'actions':[name for name,_ in desktop.actions],
                       'checks':result.get('field_verification'), 'result':result.get('error_code')}
    assert sum(name=='invite_greeting_retry' for name,_ in desktop.actions) == 1


@pytest.mark.parametrize('ocr_code', ['CJABCDEX', CODE+'|'])
@pytest.mark.parametrize('extra_greeting', ['', '，您好'])
def test_original_ocr_pass_uses_task_value_without_any_native_read(monkeypatch, tmp_path, ocr_code, extra_greeting):
    desktop = install(monkeypatch, displacement=0)
    original = desktop.run_ocr_on_screen_region
    def ocr(*args):
        rows = original(*args)
        for row in rows:
            if row['text'] == CODE: row['text'] = ocr_code
            elif row['text'] == GREETING: row['text'] += extra_greeting
        return rows
    desktop.run_ocr_on_screen_region = ocr
    monkeypatch.setattr(native, 'read_invite_fields', lambda *a, **k: pytest.fail('old OCR pass must not read field'))
    with pytest.raises(ConfirmReached): run(tmp_path)
    assert [name for name, _ in desktop.actions] == ['invite_greeting', 'invite_remark', 'invite_confirm_button_click']
    assert desktop.remark == CODE  # always paste task value, never the OCR candidate
    assert len(desktop.images) == 2


@pytest.mark.parametrize('damage', ['identity', 'bounds', 'window'])
def test_native_identity_change_never_refills_or_confirms(monkeypatch, tmp_path, damage):
    desktop = install(monkeypatch, displacement=22)
    def read(hwnd, targets, **kwargs):
        actual = dict(available=True, source_verified=True, value='Original contact',
                      hwnd=hwnd, window_rect=[0,0,468,809], field_bounds=targets['remark_name']['bounds'])
        if damage == 'identity': actual['identity_changed'] = True
        elif damage == 'bounds': actual['field_bounds'] = [0,0,1,1]
        else: actual['hwnd'] = hwnd+1
        return {'remark_name': actual}
    monkeypatch.setattr(native, 'read_invite_fields', read)
    result = run(tmp_path)
    assert result['confirm']['skipped']
    assert not result['after']['final_status']['pre_confirm_cleanup']['attempted']
    assert [name for name, _ in desktop.actions] == ['invite_greeting', 'invite_remark']


@pytest.mark.parametrize('actual', [CODE, None])
def test_low_confidence_readback_never_overrides_old_ocr(monkeypatch, tmp_path, actual):
    desktop = install(monkeypatch, displacement=0)
    original = desktop.run_ocr_on_screen_region
    def ocr(*args):
        rows = original(*args)
        for row in rows:
            if row['text'] == CODE: row['confidence'] = .899
        return rows
    desktop.run_ocr_on_screen_region = ocr
    if actual is not None:
        monkeypatch.setattr(native, 'read_invite_fields', lambda hwnd, targets, **kw: {
            'remark_name': dict(available=True,source_verified=True,value=actual,hwnd=hwnd,
                               window_rect=[0,0,468,809],field_bounds=targets['remark_name']['bounds'])})
    result = run(tmp_path)
    assert result['confirm']['skipped'] and result['error_code'] == 'INVITE_FIELD_VERIFICATION_FAILED'
    assert sum(name=='invite_remark_retry' for name,_ in desktop.actions) <= 1
    assert not any(name=='invite_confirm_button_click' for name,_ in desktop.actions)


def test_fresh_old_ocr_pass_is_not_vetoed_by_native_text(monkeypatch,tmp_path):
    desktop=install(monkeypatch,displacement=0)
    original=desktop.run_ocr_on_screen_region
    def ocr(*args):
        rows=original(*args)
        if len(desktop.images)==2:
            for row in rows:
                if row['text']==GREETING:row['text']='首帧漏字'
        return rows
    desktop.run_ocr_on_screen_region=ocr
    monkeypatch.setattr(native,'read_invite_fields',lambda hwnd,targets,**kw:{
        'verify_message':dict(available=True,source_verified=True,value='回读与OCR不一致',hwnd=hwnd,
                              window_rect=[0,0,468,809],field_bounds=targets['verify_message']['bounds'])})
    with pytest.raises(ConfirmReached):run(tmp_path)
    assert [name for name,_ in desktop.actions]==['invite_greeting','invite_remark','invite_confirm_button_click']


@pytest.mark.parametrize('lost_after_greeting_retry',[False,True])
def test_refill_requires_current_invite_identity(monkeypatch,tmp_path,lost_after_greeting_retry):
    desktop=install(monkeypatch,displacement=22,bad_greeting=lost_after_greeting_retry)
    original=desktop.run_ocr_on_screen_region
    def ocr(*args):
        rows=original(*args)
        if (len(desktop.images)>=4 if lost_after_greeting_retry else len(desktop.images)>=3):
            rows=[row for row in rows if row['text']!='申请添加朋友']
        return rows
    desktop.run_ocr_on_screen_region=ocr
    result=run(tmp_path)
    assert result['confirm']['skipped']
    names=[name for name,_ in desktop.actions]
    assert 'invite_remark_retry' not in names
    assert names.count('invite_greeting_retry')==int(lost_after_greeting_retry)
    assert not result['after']['final_status']['pre_confirm_cleanup']['attempted']
