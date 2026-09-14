"""Saved OCR / synthetic profile replay; native mouse and capture are controlled.

No private screenshot is distributed. CHEJIN_EXISTING_CONTACT_OCR can point
to the locally retained OCR list for the incident replay; the blank image
only supplies dimensions to production layout/close code, not image evidence.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows as m
from apps.wechat_ai_customer_service.tests.test_add_friend_flow_runtime import (
    PhysicalClickReached, production_boundary,
)


def item(text, x, y, scale=1):
    left, top, right, bottom = [v*scale for v in (x-30, y-9, x+30, y+9)]
    return dict(text=text, confidence=.99, left=left, top=top, right=right,
                bottom=bottom, center_x=x*scale, center_y=y*scale)


def profile(*, title=True, action='发消息', scale=1):
    items = [item('朋友资料', 90, 230, scale), item('备注', 80, 265, scale),
             item('测试联系人', 190, 265, scale)]
    if title:
        items.insert(0, item('添加朋友', 200, 22, scale))
    if action:
        items.append(item(action, 180, 540, scale))
    return items, (round(400*scale), round(600*scale))


def profile_runtime(items, size, directory, *, visibility='destroyed'):
    """Run the formal search-result handler and formal close helper.

    Only native captures/OCR/mouse/window presence and pacing are controlled.
    No patched classification, target selection, cleanup or terminal result.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shot = Image.new('RGB', size, 'white')
    path = directory / 'synthetic-surface.png'
    shot.save(path)
    clicks = []
    with production_boundary(shot, ocr_items=items, click_points=clicks):
        s._register_layout_snapshot(1001, shot,
            capture_mode=s.win32_ocr_layout.CAPTURE_MODE_CLIENT_AREA,
            screenshot_path=str(path), capture_screen_origin=[0, 0], generic_popup=True)
        def click(hwnd, x, y, *, bounds, action_name='', expected_snapshot_id=''):
            clicks.append(dict(hwnd=hwnd, x=x, y=y, bounds=bounds,
                               action_name=action_name, layout_snapshot_id=expected_snapshot_id))
            if action_name == 'add_contact_entry_click':
                raise PhysicalClickReached(action_name)
            return {'ok': True}
        with ExitStack() as stack:
            stack.enter_context(patch.object(s, 'human_window_image_click_in_bounds', side_effect=click))
            stack.enter_context(patch.object(s, 'win32gui', SimpleNamespace(
                IsWindow=lambda hwnd: visibility != 'destroyed', IsWindowVisible=lambda hwnd: False)))
            result = m.click_add_contact_entry_from_search_result(1001, directory,
                result_shot=shot, result_path=str(path), result_items=items, query='13800000000')
    return result, clicks


@pytest.mark.parametrize('scale', [1, 1.25, 1.5, 2])
@pytest.mark.parametrize('title', [True, False])
def test_profile_message_action_with_or_without_window_title(scale, title):
    items, size = profile(title=title, scale=scale)
    assert m.classify_add_friend_ocr_surface(items, size)['result_code'] == 'already_friend'


@pytest.mark.parametrize('label', ['添加朋友', '添加到通讯录', '添加至通讯录', '添加通讯录'])
@pytest.mark.parametrize('title', [True, False])
def test_real_add_action_still_takes_precedence(label, title):
    items, size = profile(title=title)
    items.append(item(label, 200, 440))
    assert m.classify_add_friend_ocr_surface(items, size)['state'] == 'add_contact_entry'


@pytest.mark.parametrize('text', ['', '视频号', '昵称：发消息'])
def test_window_title_or_profile_copy_does_not_prove_existing_friend(text):
    items, size = profile(action=text)
    result = m.classify_add_friend_ocr_surface(items, size)
    assert result['state'] == 'unknown'
    assert result['result_code'] != 'already_friend'


@pytest.mark.parametrize('text,state', [('该用户不存在', 'phone_not_found'), ('操作频繁', 'account_restricted')])
def test_explicit_failure_keeps_precedence(text, state):
    items, size = profile()
    items.append(item(text, 200, 350))
    assert m.classify_add_friend_ocr_surface(items, size)['state'] == state


@pytest.mark.parametrize('visibility', ['destroyed', 'hidden'])
def test_existing_contact_runs_original_close_once_then_completes(tmp_path, visibility):
    items, size = profile()
    result, clicks = profile_runtime(items, size, tmp_path, visibility=visibility)
    assert result['task_status'] == 'completed' and result['result_code'] == 'already_friend'
    assert result['post_confirm_cleanup']['closed'] is True
    assert [c['action_name'] for c in clicks] == ['already_friend_add_friend_dialog_close']
    assert all(c['layout_snapshot_id'] and c['bounds'][0] <= c['x'] <= c['bounds'][2]
               and c['bounds'][1] <= c['y'] <= c['bounds'][3] for c in clicks)


@pytest.mark.parametrize('label', ['添加朋友', '添加到通讯录', '添加至通讯录', '添加通讯录'])
def test_nonfriend_reaches_original_add_button_not_dialog_title(tmp_path, label):
    items, size = profile(action=label)
    # The full search handler must reach the real existing add-contact path,
    # not manufacture already_friend or invoke the dialog-close branch.
    with pytest.raises(PhysicalClickReached, match='add_contact_entry_click'):
        profile_runtime(items, size, tmp_path)


def test_private_saved_incident_ocr_reaches_close_and_completed(tmp_path):
    source = os.environ.get('CHEJIN_EXISTING_CONTACT_OCR')
    if not source:
        pytest.skip('Private saved OCR required for incident replay; synthetic cases run normally')
    items = json.loads(Path(source).read_text())
    assert len(items) == 18 and any(x['text'] == '添加朋友' and x['top'] == 13 for x in items)
    assert any(x['text'] == '发消息' for x in items)
    # Actual screenshot dimensions were not uploaded. These dimensions merely
    # contain all saved OCR boxes; no claim of native screenshot replay.
    size = (max(x['right'] for x in items)+1, max(x['bottom'] for x in items)+1)
    classified = m.classify_add_friend_ocr_surface(items, size)
    result, clicks = profile_runtime(items, size, tmp_path)
    assert classified['result_code'] == 'already_friend'
    assert result['task_status'] == 'completed' and result['result_code'] == 'already_friend'
    assert result['post_confirm_cleanup']['closed']
    assert [c['action_name'] for c in clicks] == ['already_friend_add_friend_dialog_close']
    destination = os.environ.get('CHEJIN_EXISTING_CONTACT_EVIDENCE')
    if destination:
        Path(destination).write_text(json.dumps({'classification': classified, 'result': result,
            'clicks': clicks, 'kind': 'Saved OCR + controlled physical boundary, not new OCR or Windows UAT'},
            ensure_ascii=False, indent=2))
