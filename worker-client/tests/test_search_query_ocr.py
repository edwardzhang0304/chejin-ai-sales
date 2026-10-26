"""Synthetic chrome and native capture chain; no real customer pixels committed."""
import copy
import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from apps.wechat_ai_customer_service.adapters.search_control_icons import empty_references
from apps.wechat_ai_customer_service.adapters.search_query_ocr import prepare_search_query_ocr
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as flow
from test_c1_evidence_chain import scene, home, original, outcome


def query_image(height=31, caret=True):
    reference = empty_references()[1]
    control = reference.resize((round(reference.width * height / 31), height), Image.Resampling.BILINEAR)
    image = Image.new('RGB', (550, 320), 'white')
    image.paste(control, (30, 20))
    draw = ImageDraw.Draw(image)
    draw.text((30 + height + 2, 20 + height // 4), '13800000003', fill='black')
    if caret:
        draw.line((30 + height + 82, 25, 30 + height + 82, 20 + height - 5), fill='black')
    draw.text((50, 90), 'network lookup', fill='black')
    return image, [30, 20, 30 + control.width, 20 + height]


@pytest.mark.parametrize('height', [25, 31, 37, 50, 62])
@pytest.mark.parametrize('caret', [True, False])
def test_verified_icons_only_are_excluded_and_all_text_pixels_are_unchanged(height, caret):
    image, bounds = query_image(height, caret)
    before = np.asarray(image).copy()
    prepared, excluded = prepare_search_query_ocr(image, bounds)
    assert len(excluded) == 2
    allowed = np.zeros(before.shape[:2], dtype=bool)
    for left, top, right, bottom in excluded:
        allowed[top:bottom, left:right] = True
        assert left >= bounds[0] and right <= bounds[2]
        assert right <= bounds[0] + height or left >= bounds[2] - height
    assert np.array_equal(np.asarray(prepared)[~allowed], before[~allowed])
    assert np.array_equal(np.asarray(image), before)
    assert prepared.size == image.size


def test_unrecognized_icon_and_extra_character_in_icon_strip_are_not_erased():
    image, bounds = query_image()
    # Extra glyph in template whitespace must prevent pixel proof even if the
    # overall correlation still looks like an icon. No string stripping follows.
    ImageDraw.Draw(image).rectangle((51, 25, 53, 43), fill='black')
    prepared, excluded = prepare_search_query_ocr(image, bounds)
    assert not any(r[0] < bounds[0] + 31 for r in excluded)
    assert np.array_equal(np.asarray(prepared)[20:51, 30:61], np.asarray(image)[20:51, 30:61])


@pytest.mark.parametrize('text', ['1380000000', '138000000003', '13800000004',
    'Q13800000003', 'O13800000003', '013800000003', '13800000003X'])
def test_actual_extra_or_wrong_characters_remain_rejected(text):
    row = dict(text=text, confidence=.99, left=65, top=25, right=200, bottom=45)
    frame = flow.Frame(Image.new('RGB', (550, 320)), [row], 'lookup')
    with pytest.raises(flow.FlowStop, match='SEARCH_QUERY_UNCONFIRMED'):
        flow.search_query(frame, '13800000003', bounds=[30, 20, 267, 51])


@pytest.mark.parametrize('damage', ['duplicate', 'extra_row', 'partial_extra_row', 'partial_query', 'low_confidence', 'nan', 'wrong_region'])
def test_exact_query_alone_is_not_enough_when_evidence_is_ambiguous(damage):
    row = dict(text='13800000003', confidence=.99, left=65, top=25, right=200, bottom=45)
    rows = [row]
    if damage == 'duplicate': rows.append(copy.deepcopy(row))
    if damage == 'extra_row': rows.append({**row, 'text':'Q', 'left':210, 'right':220})
    if damage == 'partial_extra_row': rows.append({**row, 'text':'Q', 'left':25, 'right':35})
    if damage == 'partial_query': row.update(left=25)
    if damage == 'low_confidence': row['confidence'] = .79
    if damage == 'nan': row['confidence'] = float('nan')
    if damage == 'wrong_region': row.update(top=100, bottom=120)
    frame = flow.Frame(Image.new('RGB', (550, 320)), rows, 'lookup')
    with pytest.raises(flow.FlowStop, match='SEARCH_QUERY_UNCONFIRMED'):
        flow.search_query(frame, '13800000003', bounds=[30, 20, 267, 51])


def test_native_capture_uses_one_ocr_copy_and_exports_reconstructible_input(scene, monkeypatch):
    s = scene()
    bounds = s.native._search_control_cache['bounds']
    height = bounds[3] - bounds[1]
    grab, ocr = s.ops.try_image_grab.side_effect, s.ops.run_ocr.side_effect
    raw = []
    def with_chrome(bbox):
        image = grab(bbox)
        if image.info['scene_label'] == 'lookup':
            control = empty_references()[1].resize((bounds[2]-bounds[0], height))
            image.paste(control, tuple(bounds[:2]))
            ImageDraw.Draw(image).text((bounds[0]+height+2, bounds[1]+5), '13800000003', fill='black')
            raw.append(image.copy())
        return image
    def inspect_ocr(image):
        rows = ocr(image)
        if image.info['scene_label'] == 'lookup':
            assert image.tobytes() != raw[-1].tobytes()
            # Simulated OCR boundary, now seeing only numbers in the input.
            assert image.getpixel((bounds[0]+4, bounds[1]+4)) == (255,255,255)
        return rows
    s.ops.try_image_grab.side_effect = with_chrome
    s.ops.run_ocr.side_effect = inspect_ocr
    result = s.execute(monkeypatch)
    assert result['status'] == 'not_found', result
    assert s.grabs == s.ocr_calls == ['header', 'lookup', 'card', 'header']
    evidence = outcome(s.export())
    lookup = next(f for f in evidence['evidence']['frames'] if f['label']=='lookup')
    assert len(lookup['ocr_excluded_icon_bounds']) == 2
    assert lookup['ocr_excluded_icon_fill'] == [255, 255, 255]
    import io
    z = s.export()
    saved = Image.open(io.BytesIO(next(v for n,v in z.items() if n.endswith('/screenshots/'+lookup['screenshot_file'])))).convert('RGB')
    assert saved.tobytes() == raw[-1].tobytes()
    for rect in lookup['ocr_excluded_icon_bounds']:
        saved.paste(tuple(lookup['ocr_excluded_icon_fill']), tuple(rect))
    expected, _ = prepare_search_query_ocr(raw[-1], bounds)
    assert saved.tobytes() == expected.tobytes()
    assert not (original(s)/'submit-attempt.json').exists()
