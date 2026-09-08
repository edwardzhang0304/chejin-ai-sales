"""Real Windows screenshot replay; only physical/OS/OCR boundaries are replaced.

OCR labels are manually transcribed. State transitions select existing source
screenshots, not evidence that a real mouse closed a window on this Mac.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'omniauto-rpa'))
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s

FIXTURES = Path(os.environ.get(
    'CHEJIN_SEARCH_CLEANUP_FIXTURES',
    str(Path(__file__).parent / 'fixtures/search_cleanup_20260907'),
))


@pytest.fixture(autouse=True)
def require_private_windows_screenshots():
    # Real customer screenshots remain private. Missing evidence is a skip,
    # never a passing screenshot replay or a synthetic replacement.
    if not all((FIXTURES / f'{name}.png').is_file() for name in ('idle', 'focused', 'results')):
        pytest.skip('Private Windows screenshots required; set CHEJIN_SEARCH_CLEANUP_FIXTURES')


def ocr(text, box, scale=1):
    left, top, right, bottom = [value * scale for value in box]
    return dict(text=text, left=left, top=top, right=right, bottom=bottom,
                center_x=(left + right) / 2, center_y=(top + bottom) / 2, confidence=.99)


class ScreenshotDesktop:
    def __init__(self, scale=1, origin=(0, 0), *, close_works=True, focus_works=True, blocker=False):
        self.scale, self.origin = scale, origin
        self.state = 'idle'
        self.close_works, self.focus_works, self.blocker = close_works, focus_works, blocker
        self.clicks, self.keys, self.frames = [], [], []
        self.images = {}
        for name in ('idle', 'focused', 'results'):
            image = Image.open(FIXTURES / f'{name}.png').convert('RGB')
            if scale != 1:
                image = image.resize((round(image.width * scale), round(image.height * scale)))
            self.images[name] = image
        self.width, self.height = self.images['idle'].size
        self.geometry = dict(left=origin[0], top=origin[1], width=self.width, height=self.height,
                             right=origin[0] + self.width, bottom=origin[1] + self.height)
        # Regions come from the actual original image, including separators
        # and the plus icon, rather than hand-authored layout rectangles.
        label = ocr('搜索', (124, 64, 156, 79), scale)
        self.layout = s.win32_ocr_layout.build_structural_layout_regions(
            self.images['idle'], ocr_items=[label], search_anchor_items=[label])
        assert self.layout['ok'], self.layout

    def capture(self, _hwnd, *, label='', **_kwargs):
        image = self.images[self.state].copy()
        snapshot = s.win32_ocr_layout.build_layout_snapshot(
            hwnd=1, frame_id=f'replay-{id(image)}',
            capture_mode=s.win32_ocr_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
            image_size=image.size, capture_screen_origin=self.origin,
            window_rect=self.geometry, client_rect=dict(left=0, top=0, width=self.width, height=self.height),
            client_screen_origin=self.origin, dpi_scale=self.scale,
            regions=self.layout['regions'], anchors=self.layout['anchors'],
            confidence=self.layout['confidence'], conflicts=self.layout['conflicts'], executable=True)
        s._LAYOUT_SNAPSHOT_STORE.put(snapshot)
        s._LATEST_LAYOUT_SNAPSHOT_BY_HWND[1] = snapshot['layout_snapshot_id']
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = snapshot['layout_snapshot_id']
        image.info['replay_state'] = self.state
        self.frames.append(image)  # retain identities just like captured frames
        return image, str(FIXTURES / f'{self.state}.png')

    def read_ocr(self, image):
        state = image.info.get('replay_state')
        if image.size != (self.width, self.height):
            return []  # explicit external ROI OCR miss: real full-frame fallback
        rows = [ocr('CJDZSKVN', (393, 60, 488, 80), self.scale)]
        rows.append(ocr('CJ3N95EU' if state == 'results' else '搜索',
                        (124, 64, 201 if state == 'results' else 156, 79), self.scale))
        if state == 'results':
            rows += [ocr('网络查找微信号：CJ3N95EU', (160, 119, 387, 142), self.scale),
                     ocr('搜索网络结果', (137, 183, 245, 201), self.scale)]
        elif state == 'focused':
            rows.append(ocr('搜索网络结果', (160, 107, 270, 132), self.scale))
        else:
            rows.append(ocr('CJDZSKVN', (148, 119, 244, 141), self.scale))
        if self.blocker and state == 'results':
            # Explicit synthetic OCR fault injection, not a label from the
            # underlying source image. Exercise the existing login guard.
            rows.append(ocr('请使用微信扫描二维码', (410, 300, 690, 330), self.scale))
        return rows

    def click(self, hwnd, x, y, *, bounds, action_name='', expected_snapshot_id='', **_kwargs):
        snapshot = s.current_layout_snapshot(hwnd)
        assert snapshot['layout_snapshot_id'] == expected_snapshot_id
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
        screen = s.win32_ocr_layout.image_point_to_screen(snapshot, [x, y])
        self.clicks.append(dict(action=action_name, point=[x, y], bounds=bounds,
                                screen_point=screen, snapshot=expected_snapshot_id))
        if 'header_blank' in action_name:
            assert s.win32_ocr_layout.point_in_bounds([x, y], snapshot['chat_header_bounds'])
            if self.close_works:
                self.state = 'idle'
        else:
            assert s.win32_ocr_layout.point_in_bounds([x, y], snapshot['sidebar_header_bounds'])
            # Ground truth from the original screenshot: being somewhere in
            # the header is insufficient (the old reference clicked above it).
            observed_input = [round(v * self.scale) for v in (91, 55, 312, 88)]
            assert s.win32_ocr_layout.point_in_bounds([x, y], observed_input), (action_name, [x, y])
            if self.focus_works and self.state == 'idle':
                self.state = 'focused'
        return {'ok': True}

    def hotkey(self, modifier, key, **_kwargs):
        self.keys.append([modifier, key])
        if key == ord('V'):
            self.state = 'results'

    def key_press(self, key, **_kwargs):
        self.keys.append([key])
        if key == 8:
            self.state = 'focused'

    def type_text(self, *_args, **_kwargs):
        self.state = 'results'
        return {'ok': True}

    def boundaries(self):
        stack = ExitStack()
        replacements = dict(capture_wechat=self.capture, capture_wechat_window_visible_screen=self.capture,
                            run_ocr=self.read_ocr, get_window_geometry=lambda _: self.geometry,
                            basic_send_window_guard=lambda _: {'ok': True},
                            recover_send_window_guard=lambda *a, **k: {'ok': True},
                            human_window_image_click_in_bounds=self.click, hotkey=self.hotkey,
                            key_press=self.key_press, clipboard_copy=lambda _: None,
                            type_text_with_sendinput_unicode=self.type_text,
                            humanized_action_sleep=lambda *a, **k: None,
                            win32con=SimpleNamespace(VK_CONTROL=17, VK_BACK=8, VK_RETURN=13))
        for name, value in replacements.items():
            stack.enter_context(patch.object(s, name, value))
        stack.enter_context(patch.dict(os.environ, {'WECHAT_WIN32_OCR_TARGET_SEARCH_INPUT_METHOD': 'clipboard'}))
        return stack


@pytest.mark.parametrize('scale,origin', [(1, (0, 0)), (1.25, (317, 83)), (1.5, (-1280, 160))])
def test_search_no_match_closes_then_real_first_screen_parser_sees_customer(scale, origin, tmp_path):
    desktop = ScreenshotDesktop(scale, origin)
    with desktop.boundaries():
        baseline, _ = desktop.capture(1)
        result = s.open_chat_by_remark_code_search(1, target='CJ3N95EU', remark_code='CJ3N95EU',
            baseline_screenshot=baseline, baseline_ocr_items=desktop.read_ocr(baseline), baseline_geometry=desktop.geometry)
        assert result['ok'] is False and result['reason'] == 'remark_code_search_no_match', result
        assert result['search_cleanup']['ok'] is True, result
        scan = s.sessions_payload(1, {}, scan_id='after-failed-search')
        assert scan['ok'] is True, scan
        assert 'CJDZSKVN' in [item['name'] for item in scan['sessions']], scan
    cleanup_clicks = [c for c in desktop.clicks if 'dismiss' in c['action']]
    assert len(cleanup_clicks) == 2
    assert cleanup_clicks[0]['snapshot'] != cleanup_clicks[1]['snapshot']
    output = Path(os.environ.get('CHEJIN_SEARCH_EVIDENCE_DIR', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / f'replay-{scale}.json').write_text(json.dumps({
        'fixture': 'Original Windows screenshot replay; manual OCR labels; OS actions simulated. Scaled frames are synthetic DPI variants.',
        'scale': scale, 'window_origin': origin, 'locate_reason': result['reason'],
        'cleanup': result['search_cleanup'], 'clicks': cleanup_clicks,
        'after_scan_names': [i['name'] for i in scan['sessions']]}, ensure_ascii=False, indent=2))


def test_failed_close_is_recorded_and_obscured_frame_is_not_a_successful_scan():
    desktop = ScreenshotDesktop(close_works=False)
    with desktop.boundaries():
        baseline, _ = desktop.capture(1)
        result = s.open_chat_by_remark_code_search(1, target='CJ3N95EU', remark_code='CJ3N95EU',
            baseline_screenshot=baseline, baseline_ocr_items=desktop.read_ocr(baseline), baseline_geometry=desktop.geometry)
        assert result['reason'] == 'remark_code_search_no_match'
        assert result['search_cleanup']['ok'] is False
        assert result['search_cleanup']['attempts'] == 1
        scan = s.sessions_payload(1, {})
        assert scan['ok'] is False and scan['reason'] == 'sidebar_search_active'


def test_cleanup_does_not_clear_text_without_confirmed_search_focus():
    desktop = ScreenshotDesktop(focus_works=False)
    with desktop.boundaries():
        result = s.dismiss_sidebar_search_state(1)
    assert not result['ok'] and result['reason'] == 'search_cleanup_focus_not_confirmed'
    assert not desktop.keys


def test_blocking_login_surface_prevents_cleanup_clicks():
    desktop = ScreenshotDesktop(blocker=True)
    desktop.state = 'results'
    with desktop.boundaries():
        result = s.dismiss_sidebar_search_state(1)
    assert not result['ok'] and result['reason'] == 'search_cleanup_surface_blocked'
    assert not desktop.clicks and not desktop.keys


def test_missing_layout_never_falls_back_to_fixed_click_coordinates():
    desktop = ScreenshotDesktop()
    desktop.state = 'results'
    original_capture = desktop.capture
    def capture_without_layout(*args, **kwargs):
        image, path = original_capture(*args, **kwargs)
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID.pop(id(image))
        return image, path
    desktop.capture = capture_without_layout
    with desktop.boundaries():
        result = s.dismiss_sidebar_search_state(1)
    assert not result['ok'] and result['reason'] == 'sidebar_search_target_unresolved'
    assert not desktop.clicks and not desktop.keys


def test_empty_exit_ocr_does_not_mean_search_was_closed():
    desktop = ScreenshotDesktop()
    desktop.state = 'results'
    original_ocr = desktop.read_ocr
    desktop.read_ocr = lambda image: [] if image.info.get('replay_state') == 'idle' else original_ocr(image)
    with desktop.boundaries():
        result = s.dismiss_sidebar_search_state(1)
    assert not result['ok'] and result['reason'] == 'search_exit_not_confirmed'


def test_cleanup_capture_error_preserves_original_search_failure():
    desktop = ScreenshotDesktop()
    original_capture = desktop.capture
    def broken_cleanup_capture(*args, **kwargs):
        if kwargs.get('label') == 'open_chat_search_dismiss_before':
            raise OSError('Synthetic capture failure')
        return original_capture(*args, **kwargs)
    desktop.capture = broken_cleanup_capture
    with desktop.boundaries():
        baseline, _ = desktop.capture(1)
        result = s.open_chat_by_remark_code_search(1, target='CJ3N95EU', remark_code='CJ3N95EU',
            baseline_screenshot=baseline, baseline_ocr_items=desktop.read_ocr(baseline), baseline_geometry=desktop.geometry)
    assert result['reason'] == 'remark_code_search_no_match'
    assert result['search_cleanup'] == {'ok': False, 'reason': 'search_cleanup_exception', 'exception_type': 'OSError'}


def test_placeholder_with_separate_magnifier_ocr_is_not_a_residual_query():
    desktop = ScreenshotDesktop()
    desktop.state = 'results'
    original_ocr = desktop.read_ocr
    def icon_ocr(image):
        rows = original_ocr(image)
        if image.info.get('replay_state') == 'idle':
            rows.append(ocr('Q', (102, 64, 115, 79)))
        return rows
    desktop.read_ocr = icon_ocr
    with desktop.boundaries():
        result = s.dismiss_sidebar_search_state(1)
        assert result['ok'] and result['query_empty'], result
        assert s.sessions_payload(1, {})['ok']
