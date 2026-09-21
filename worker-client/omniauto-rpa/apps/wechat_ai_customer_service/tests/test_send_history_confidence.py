"""Constructed frames through the real matcher; no OCR/physical-send claim."""
from copy import deepcopy

import pytest
from apps.wechat_ai_customer_service.tests.test_historical_confidence_alignment import scenario
from apps.wechat_ai_customer_service.tests.test_historical_text_alignment import frame
from apps.wechat_ai_customer_service.adapters import text_correspondence as rules

OLD = '我比较感兴趣，比亚迪和特斯拉，这些热门车型'
REPLY = '好的，我帮您留意合适的新能源车型，有消息及时联系您。'


def compact(rows):
    return [dict(observation_id=r['observation_id'], sender_role=r['sender_role'],
        row_kind=r['row_kind'], content_normalized=r.get('content_clean', '')) for r in rows]


def case():
    cp, _ = scenario(['唯一开场锚点', OLD], ['唯一开场锚点', OLD])
    before = frame(['唯一开场锚点', OLD[:-1]])
    after = frame(['唯一开场锚点', OLD[:-1]+'雨', REPLY, '请优先帮我留意新能源'])
    after[2]['sender_role'] = 'self'
    return before, after, {'checkpoint': cp, 'baseline_observations': before}


def match(before, after, history):
    return rules.find_new_matching_self_message(compact(before), compact(after), REPLY,
        historical_alignment=history, current_observations=after)


def test_old_ocr_drift_does_not_veto_the_actual_new_reply():
    before, after, history = case()
    frozen = deepcopy((before, after, history))
    found = match(before, after, history)
    assert found and found['observation_id'] == after[2]['observation_id']
    assert found['following_customer_observation_ids'] == [after[3]['observation_id']]
    assert (before, after, history) == frozen
    assert rules.find_new_matching_self_message(compact(before), compact(after), REPLY) is None


@pytest.mark.parametrize('damage', ['no_send', 'wrong_reply', 'two_self', 'role', 'wrong_history',
                                  'checkpoint_hash', 'compact_body', 'compact_id', 'old_self_only'])
def test_history_similarity_does_not_replace_send_or_evidence_checks(damage):
    before, after, history = case()
    if damage == 'no_send': after.pop(2)
    elif damage == 'wrong_reply': after[2]['content_clean'] = '这是销售另外手工写的一条消息'
    elif damage == 'two_self': after[3]['sender_role'] = 'self'
    elif damage == 'role': after[1]['sender_role'] = 'self'
    elif damage == 'wrong_history': after[1]['content_clean'] = '毫不相干的新话题'
    elif damage == 'checkpoint_hash': history['checkpoint']['checkpoint_digest'] = '0'*64
    elif damage == 'old_self_only':
        before = deepcopy(after[:3]); history['baseline_observations'] = before
    left, right = compact(before), compact(after)
    if damage == 'compact_body': right[1]['content_normalized'] = '被替换的正文'
    if damage == 'compact_id': right[1]['observation_id'] = '被替换的编号'
    assert rules.find_new_matching_self_message(left, right, REPLY,
        historical_alignment=history, current_observations=after) is None


@pytest.mark.parametrize('kind', ['text_bubble', 'voice_bubble', 'voice_transcript', 'image_bubble'])
def test_following_customer_occurrence_is_preserved_even_with_identical_text(kind):
    before, after, history = case()
    after[3].update(row_kind=kind, message_type={'voice_bubble':'voice', 'voice_transcript':'voice',
        'image_bubble':'image'}.get(kind, 'text'), content_clean=OLD)
    def snapshot(identity, rows):
        return {'ok': True, 'frame_observation': {'frame_id': identity},
            'validation': {'confirmed_target': 'CJTEST01'}, 'input_region': {'has_visible_text': False},
            'message_sequence': compact(rows), 'observations': rows}
    baseline = snapshot('pre', before); baseline['receipt_historical_alignment'] = history
    evidence = {'send_result': {'confirmed': True, 'result': 'sent', 'send_baseline': baseline,
        'sent_confirmation': {'ok': True, 'confirmed_observation': after[2], 'snapshot': snapshot('post', after)}}}
    proof = rules.confirmed_post_send_customer_suffix(evidence, target='CJTEST01', text=REPLY)
    assert proof and proof['observation_ids'] == [after[3]['observation_id']]
    evidence['send_result']['confirmed'] = False
    assert rules.confirmed_post_send_customer_suffix(evidence, target='CJTEST01', text=REPLY) is None
