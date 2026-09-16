"""Remark validation; private-image cases use real OCR and controlled Windows I/O."""

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from apps.wechat_ai_customer_service.adapters.add_friend_layout import (
    high_confidence_eight_char_code_visible,
    invite_form_field_verification,
)
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import add_friend_windows as m
from apps.wechat_ai_customer_service.tests.test_add_friend_flow_runtime import (
    PhysicalClickReached, production_boundary,
)


def item(text, confidence=.99, *, x=80, y=260):
    return dict(text=text, confidence=confidence, left=x-35, top=y-10,
                right=x+35, bottom=y+10, center_x=x, center_y=y)


@pytest.mark.parametrize('text,expected', [
    ('CJTRSFZT', True), ('CJTRSFZT|', True), ('cjtrsfzt|', True),
    (' CJTRSFZT| ', True), ('CJTRSFZ', False), ('CJTRSFZ|', False),
    ('CJTRSFZT9', False), ('CJTRSFZTI', False), ('CJTRSFZTl', False),
    ('CJTRSFZT1', False), ('|CJTRSFZT', False), ('CJTR|SFZT', False),
    ('CJTRSFZT||', False), ('CJTRSFZT｜', False), ('CJTRSFZ!', False),
])
def test_only_one_trailing_caret_after_eight_characters(text, expected):
    items = [item(text)]
    original = deepcopy(items)
    result = high_confidence_eight_char_code_visible(items, bounds=[24, 234, 345, 290])
    assert result['ok'] is expected
    assert items == original  # Keep the original OCR evidence, including the caret.
    if expected:
        assert result['observed_text'] == 'cjtrsfzt'


@pytest.mark.parametrize('items,expected', [
    ([item('CJTRSFZT|', .90)], True),
    ([item('CJTRSFZT|', .899)], False),
    ([item('CJTRSFZT|', y=100)], False),
    ([item('CJTRSFZT|'), item('CJABCDEF')], False),
    ([], False),
])
def test_confidence_field_scope_and_uniqueness_still_apply(items, expected):
    result = high_confidence_eight_char_code_visible(items, bounds=[24, 234, 345, 290])
    assert result['ok'] is expected


def test_caret_does_not_bypass_greeting_verification():
    result = invite_form_field_verification(
        verify_message='您好', remark_name='CJTRSFZT', remark_code='CJTRSFZT',
        ocr_items=[item('CJTRSFZT|')],
        field_bounds={'verify_message': [24, 90, 345, 166],
                      'remark_name': [24, 234, 345, 290],
                      'remark_code': [24, 234, 345, 290]},
    )
    assert result['remark_code']['ok']
    assert not result['verify_message']['ok']
    assert not result['ok']


@pytest.fixture
def incident():
    directory = os.environ.get('CHEJIN_REMARK_CARET_INCIDENT')
    if not directory:
        pytest.skip('Private original incident PNGs are required for real OCR replay')
    evidence = Path(directory)
    before = next(evidence.glob('*-add_friend_invite_form_window_candidate_1789547167013.png'))
    filled = next(evidence.glob('*-add_friend_invite_form_filled_before_confirm_window_1789547177040.png'))
    return Image.open(before).convert('RGB'), Image.open(filled).convert('RGB')


def test_original_image_real_ocr_retains_caret_but_verification_passes(incident, tmp_path):
    _, shot = incident
    items = s.run_ocr(shot)
    observed = [row for row in items if row['text'] == 'CJTRSFZT|']
    assert len(observed) == 1, items
    result = high_confidence_eight_char_code_visible(items, bounds=[24, 234, 345, 290])
    (tmp_path / 'real-ocr.json').write_text(json.dumps(
        {'ocr': observed, 'verification': result}, ensure_ascii=False, indent=2), encoding='utf-8')
    assert result['ok'] and result['observed_text'] == 'cjtrsfzt'


def test_original_form_reaches_confirm_once_without_refill(incident, tmp_path):
    """Run the real form flow through its mouse boundary; do not fake an invite receipt."""
    before, filled = incident
    greeting = '您好，我是车金二手车的高磊，您刚咨询过二手车'
    pasted, captures, clicks = [], [], []

    def capture(hwnd, *, artifact_dir, label, popup_window=False):
        shot = before if not pasted else filled
        if pasted:
            assert pasted == [greeting, 'CJTRSFZT']
        path = str(Path(artifact_dir) / f'{label}.png')
        shot.save(path)
        captures.append(label)
        s._register_layout_snapshot(
            hwnd, shot, capture_mode=s.win32_ocr_layout.CAPTURE_MODE_CLIENT_AREA,
            screenshot_path=path, capture_screen_origin=[0, 0], generic_popup=popup_window,
        )
        return shot, path

    def click(hwnd, x, y, *, bounds, action_name='', expected_snapshot_id='', **kwargs):
        snapshot = s.current_layout_snapshot(hwnd)
        assert snapshot and snapshot['layout_snapshot_id'] == expected_snapshot_id
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
        clicks.append(action_name)
        if action_name == 'invite_confirm_button_click':
            raise PhysicalClickReached(action_name)
        return {'ok': True}

    with (
        production_boundary(before, ocr_items=[], click_points=[]),
        patch.object(s, 'capture_wechat_window_visible_screen', side_effect=capture),
        patch.object(s, 'run_ocr_traced', side_effect=lambda image, *a, **kw: s.run_ocr(image)),
        patch.object(s, 'human_window_image_click_in_bounds', side_effect=click),
        patch.object(s, 'hotkey'), patch.object(s, 'key_press'),
        patch.object(s, 'clipboard_copy', side_effect=pasted.append),
        patch.object(m, 'win32con', SimpleNamespace(VK_CONTROL=17, VK_BACK=8)),
    ):
        with pytest.raises(PhysicalClickReached, match='invite_confirm_button_click'):
            result = m.fill_add_friend_invite_form_and_confirm(
                1001, tmp_path, verify_message=greeting,
                remark_name='CJTRSFZT', remark_code='CJTRSFZT',
            )
            pytest.fail(f'Form stopped before confirm: {result.get("error_code")}')
    assert pasted == [greeting, 'CJTRSFZT']
    assert clicks == ['invite_greeting_click', 'invite_remark_click', 'invite_confirm_button_click']
    assert len(captures) == 2
    (tmp_path / 'form-flow.json').write_text(json.dumps(
        {'captures': captures, 'clicks': clicks, 'paste_count': len(pasted),
         'real': ['OCR', 'field targeting', 'form validation', 'confirm gate'],
         'controlled': ['Windows capture', 'keyboard', 'clipboard', 'mouse'],
         'windows_invite_sent': False}, ensure_ascii=False, indent=2), encoding='utf-8')
