"""Structural pixel checks; OCR and avatar rows are supplied in these units.

The separate numeric-voice HTTP test runs actual OCR and avatar detection.
"""
from copy import deepcopy

from PIL import Image, ImageDraw, ImageFont, ImageOps
import pytest

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.voice_regions import numeric_transcript_regions
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


def scene(text='15', role='customer', scale=1):
    image = Image.new('RGB', (600, 300), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((100, 60, 270, 100), radius=6, fill=(237, 237, 237))
    draw.rounded_rectangle((100, 106, 310, 151), radius=6, fill=(237, 237, 237))
    for radius in (6, 12, 18):
        draw.arc((113-radius, 80-radius, 113+radius, 80+radius), -55, 55, fill=(35, 35, 35), width=2)
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Light.ttc', 16)
    draw.text((245, 70), '5"', font=font, fill=(25, 25, 25))
    draw.text((110, 118), text, font=font, fill=(25, 25, 25))
    def box(values):
        a,b,c,d = values
        if role == 'self': a,c = 600-c,600-a
        return [v*scale for v in (a,b,c,d)]
    def row(text, values, owned=False):
        a,b,c,d = box(values)
        result = dict(text=text, left=a, top=b, right=c, bottom=d, center_x=(a+c)/2, center_y=(b+d)/2)
        if owned:
            result['avatar_alignment'] = {'role':role, role:{'foreground_bounds':box((50,60,86,96))}}
        return result
    rows = [row('5', (246,73,255,84), True), row(text, (110,120,110+9*len(text),136))]
    if role == 'self': image = ImageOps.mirror(image)
    image = image.resize((int(600*scale), int(300*scale)))
    return image, rows


@pytest.mark.parametrize('text', ['0', '5', '15', '007'])
@pytest.mark.parametrize('role', ['customer', 'self'])
@pytest.mark.parametrize('scale', [.75, 1, 1.5, 2])
def test_numeric_body_has_unique_structural_voice_owner(text, role, scale):
    image, rows = scene(text, role, scale)
    original = deepcopy(rows)
    regions = numeric_transcript_regions(rows, image, [0,0,*image.size])
    assert list(regions) == [1]
    assert regions[1]['parent'] == [rows[0][k] for k in ('left','top','right','bottom')]
    assert rows == original
    group = [{**rows[0], '_voice_duration_region': True}, {**rows[1], '_voice_transcript_region': regions[1]}]
    assert not sidecar.message_group_is_untranscribed_voice_placeholder(group)
    assert sidecar.strip_voice_duration_prefix_from_message_content('5\n'+text, group) == (text, True)


@pytest.mark.parametrize('change', ['ordinary_number', 'new_avatar', 'far_below', 'other_column',
    'two_owners', 'no_avatar', 'same_header_row', 'different_avatar_role'])
def test_numeric_text_cannot_borrow_an_unproven_or_different_voice(change):
    image, rows = scene()
    if change == 'ordinary_number':
        ImageDraw.Draw(image).rectangle((102,62,190,98), fill=(237,237,237))
    elif change in {'new_avatar', 'different_avatar_role'}:
        role = 'customer' if change == 'new_avatar' else 'self'
        rows[1]['avatar_alignment'] = {'role':role, role:{'foreground_bounds':[50,106,86,142]}}
    elif change in {'far_below', 'other_column'}:
        pixels = image.crop((100,106,311,152))
        ImageDraw.Draw(image).rectangle((100,106,311,152), fill=(250,250,250))
        dx,dy = (0,80) if change == 'far_below' else (100,0)
        image.paste(pixels,(100+dx,106+dy))
        for key in ('left','right','center_x'): rows[1][key] += dx
        for key in ('top','bottom','center_y'): rows[1][key] += dy
    elif change == 'two_owners': rows.append(deepcopy(rows[0]))
    elif change == 'no_avatar': rows[0].pop('avatar_alignment')
    elif change == 'same_header_row':
        rows[1].update(top=73,bottom=84,center_y=78.5)
    assert numeric_transcript_regions(rows, image, [0,0,*image.size]) == {}


def test_two_adjacent_voices_keep_separate_numeric_bodies():
    first, rows = scene('15')
    image = Image.new('RGB', (600, 600), (250,250,250))
    image.paste(first,(0,0))
    second, others = scene('5')
    image.paste(second,(0,250))
    for row in others:
        for key in ('top','bottom','center_y'): row[key] += 250
        if row.get('avatar_alignment'):
            bounds = row['avatar_alignment']['customer']['foreground_bounds']
            bounds[1] += 250; bounds[3] += 250
    rows += others
    regions = numeric_transcript_regions(rows, image, [0,0,*image.size])
    assert list(regions) == [1,3]
    assert regions[1]['parent'] != regions[3]['parent']


def test_missing_frame_does_not_claim_numeric_transcript():
    _, rows = scene()
    assert numeric_transcript_regions(rows, None, [0,0,600,300]) == {}
