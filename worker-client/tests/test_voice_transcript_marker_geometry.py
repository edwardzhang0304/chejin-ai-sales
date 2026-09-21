"""Compare duration-marker geometry to the same marker after expansion.

Coordinates reproduce the engineer 11 pixel probe; text is synthetic. These
focused checks complement, rather than replace, the native OCR/HTTP chain.
"""
from copy import deepcopy
from voice_icon_fixtures import classified_duration

import pytest
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


def case(scale=1.0, role='customer'):
    def rect(left, top, right, bottom):
        if role == 'self': left, right = 1000 - right, 1000 - left
        return dict(zip(('left', 'top', 'right', 'bottom'), [v * scale for v in (left, top, right, bottom)]))
    marker = rect(495,602,504,612)
    anchor = {'source': 'parser_voice_message_context_menu_anchor', 'click_bounds': [503,607,539,627],
        'item': {'text': '5', 'message_type': 'voice', 'sender_role': role, **marker,
                 'parser_bubble_rect': list(marker.values())}}
    message = {'type': 'voice', 'sender': role, 'sender_role': role,
        'content': '新的预算要求', 'content_raw_ocr': '5"\n新的预算要求', 'voice_duration_text': '5"',
        'quality_flags': ['voice_duration_prefix_removed'], 'bubble_rect': rect(379,600,534,663),
        'ocr_items': [{'text': '5"', **rect(493,600,506,614)}, {'text': '新的预算要求', **rect(379,646,534,663)}],
        'avatar_alignment': {'role': role, role: {'present': True}}}
    message['ocr_items'][0] = classified_duration(message['ocr_items'][0])
    return anchor, message


def decide(anchor, message, others=None):
    return sidecar.combined_voice_transcript_anchor_match_evidence(message, anchor, (2000, 1800), after_messages=others or [message])


@pytest.mark.parametrize('scale', [.5, 1., 1.5, 2.])
@pytest.mark.parametrize('role', ['customer', 'self'])
def test_expanded_transcript_uses_same_marker_without_changing_click(scale, role):
    anchor, message = case(scale, role)
    original = deepcopy(anchor)
    result = decide(anchor, message)
    assert result['accepted'], result
    assert result['comparison_basis'] == 'voice_duration_marker'
    assert anchor == original


@pytest.mark.parametrize('change', ['far_row', 'wrong_lane', 'wrong_role', 'two_local_voices',
    'two_markers', 'missing_marker', 'marker_outside_message'])
def test_same_duration_does_not_authorize_wrong_or_ambiguous_voice(change):
    anchor, message = case()
    candidates = [message]
    if change in {'far_row', 'wrong_lane'}:
        fields = ('top', 'bottom') if change == 'far_row' else ('left', 'right')
        for bounds in [message['bubble_rect'], *message['ocr_items']]:
            for field in fields: bounds[field] -= 200
    elif change == 'wrong_role':
        message.update(sender='self', sender_role='self', avatar_alignment={'role': 'self', 'self': {'present': True}})
    elif change == 'two_local_voices':
        other = deepcopy(message); other['content'] = '另外一条语音'
        candidates.append(other)
    elif change == 'two_markers':
        message['ocr_items'].append({**message['ocr_items'][0], 'text': '6"'})
    elif change == 'missing_marker':
        message['ocr_items'] = message['ocr_items'][1:]
    elif change == 'marker_outside_message':
        message['ocr_items'][0]['top'] -= 200
    assert not decide(anchor, message, candidates)['accepted']


def test_neighbour_with_same_duration_is_not_selected():
    anchor, message = case()
    other = deepcopy(message)
    for rect in [other['bubble_rect'], *other['ocr_items']]:
        rect['top'] -= 200; rect['bottom'] -= 200
    assert decide(anchor, message, [other, message])['accepted']
    assert not decide(anchor, other, [other, message])['accepted']
