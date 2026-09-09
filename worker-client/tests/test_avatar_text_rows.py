"""Real incident PNG -> real OCR -> production messages pipeline.

Only OS capture/window metadata are replaced. No fabricated OCR or message
projection is used in incident replay. Synthetic containment cases are labelled.
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'omniauto-rpa'))
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s

FIXTURES = Path(os.environ.get(
    'CHEJIN_AVATAR_TEXT_FIXTURES',
    str(Path(__file__).parent / 'fixtures/avatar_text_20260909'),
))
PROVENANCE = (
    json.loads((FIXTURES / 'provenance.json').read_text())
    if (FIXTURES / 'provenance.json').is_file() else {}
)


def replay_frame(name, output_dir):
    path = FIXTURES / name
    if not PROVENANCE or not path.is_file():
        message = 'Private Windows incident fixtures required; set CHEJIN_AVATAR_TEXT_FIXTURES'
        if os.environ.get('CHEJIN_AVATAR_TEXT_FIXTURES'):
            pytest.fail(message + ' to a complete fixture directory')
        pytest.skip(message)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PROVENANCE['sha256'][name]
    image = Image.open(path).convert('RGB')
    layout = s.win32_ocr_layout
    geometry = PROVENANCE['geometry']
    client = dict(left=0, top=0, right=image.width, bottom=image.height)
    # Production structural calibration derives every region from the image.
    calibration = layout.build_startup_layout_calibration(
        hwnd=1, process_id=1, image=image, ocr_items=s.run_ocr(image),
        window_rect=geometry, client_rect=client, client_screen_origin=[489, 118],
        dpi_scale=1, capture_mode=layout.CAPTURE_MODE_CLIENT_AREA,
    )
    assert calibration['executable'], calibration
    snapshot = layout.build_layout_snapshot(
        hwnd=1, frame_id=name, capture_mode=layout.CAPTURE_MODE_CLIENT_AREA,
        image_size=image.size, capture_screen_origin=[489, 118], window_rect=geometry,
        client_rect=client, client_screen_origin=[489, 118], dpi_scale=1,
        regions={k: calibration[k] for k in layout.REQUIRED_LAYOUT_REGION_NAMES},
        anchors=calibration['anchors'], confidence=calibration['confidence'],
        executable=True, screenshot_path=str(path),
    )
    s._LAYOUT_SNAPSHOT_STORE.put(snapshot)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = snapshot['layout_snapshot_id']
    with patch.object(s, 'capture_wechat', return_value=(image, str(path))), patch.object(
        s, 'get_window_geometry', return_value=geometry
    ), patch.object(s, 'window_dpi_scale', return_value=1):
        result = s.messages_payload(
            1, {}, target='CJNPTTDE', history_load_times=0,
            confirm_target='CJNPTTDE', confirm_exact=True, artifact_dir=str(output_dir),
        )
    assert result['ok'], result
    return s.sanitize_sidecar_contract_output(result), image, snapshot


@pytest.fixture(scope='module')
def frames(tmp_path_factory):
    directory = tmp_path_factory.mktemp('avatar-text-real-ocr')
    return {name: replay_frame(name, directory / name) for name in ('previous.png', 'current.png')}


def test_real_incident_message_is_whole_and_next_customers_are_preserved(frames):
    before = frames['previous.png'][0]
    after = frames['current.png'][0]
    assert len(before['observations']) == 6
    assert len(after['observations']) == 7
    texts = [x['content_clean'].replace('\n', '') for x in after['observations']]
    assert texts.count('我通过了你的朋友验证请求，现在我们可以开始聊天了') == 1
    assert 'UNI' not in ''.join(texts)
    assert texts[-1] == '想买个手动挡1万以内'
    assert texts[2] == '你好' and texts[4] == '我想买车'
    assert not after['observation_validation_errors']


def test_existing_frame_report_preserves_raw_ocr_and_exclusion_reason(frames):
    payload = frames['current.png'][0]
    report = json.loads(Path(payload['review_path']).with_suffix('.json').read_text())
    details = next(e['result'] for e in report['events'] if 'message_ocr_items' in e.get('result', {}))
    uni = [r for r in details['message_ocr_items'] if r['text'] == 'UNI']
    assert uni and all(r['excluded_avatar_component_id'] for r in uni)
    assert any(r['text'] == '聊天了' and not r['excluded_avatar_component_id'] for r in details['message_ocr_items'])
    assert details['avatar_table']['state'] == 'complete'
    assert all(r['left'] >= details['layout_snapshot']['message_viewport_bounds'][0] for r in details['message_ocr_items'])


def test_disable_filter_reproduces_all_eight_original_message_ids(frames):
    _, image, snapshot = frames['current.png']
    raw = s.run_ocr(image)
    with patch.object(s.frame_avatars, 'containing_component', return_value=None):
        broken = s.parse_messages_from_ocr(raw, image.size, target='CJNPTTDE', screenshot=image, layout_snapshot=snapshot)
    assert [m['id'] for m in broken] == PROVENANCE['recorded_broken_ids']
    assert broken[2]['content'] == 'UNI\n聊天了'


@pytest.mark.parametrize('role', ['customer', 'self'])
def test_synthetic_exact_containment_only(role):
    # Numerical containment counterexamples, not Windows screenshot claims.
    table = {'state': 'complete', 'components': [{'component_id': 'one', 'role': role, 'bounds': [10, 10, 50, 50]}],
             'unresolved': [{'bounds': [70, 10, 110, 50]}]}
    assert s.frame_avatars.containing_component(table, [11, 11, 49, 49])['component_id'] == 'one'
    for rect in ([49, 11, 60, 25], [0, 11, 49, 49], [12, 12, 12, 30], [75, 15, 90, 40]):
        assert s.frame_avatars.containing_component(table, rect) is None
    assert s.frame_avatars.containing_component({**table, 'state': 'invalid'}, [11, 11, 49, 49]) is None
    assert s.frame_avatars.containing_component({**table, 'components': table['components'] * 2}, [11, 11, 49, 49]) is None


def test_synthetic_uni_text_inside_real_bubble_is_not_keyword_filtered(frames):
    _, image, snapshot = frames['current.png']
    # Change text at the OCR boundary only: protects real messages saying UNI.
    rows = s.run_ocr(image)
    rows = [{**row, 'text': 'UNI'} if row['text'] == '你好' else row for row in rows]
    messages = s.parse_messages_from_ocr(rows, image.size, target='CJNPTTDE', screenshot=image, layout_snapshot=snapshot)
    assert sum(m['content'] == 'UNI' for m in messages) == 1
