"""Lossy candidate signatures must not replace body correspondence evidence."""
from copy import deepcopy
import pytest
from apps.wechat_ai_customer_service.tests.test_historical_confidence_alignment import scenario, build
from apps.wechat_ai_customer_service.adapters.historical_text_alignment import comparison_projection, verify_correspondence, reconcile_checkpoint_continuity
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations, compare_business_viewport_continuity
from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence


@pytest.mark.parametrize('current,accepted', [
    ('198.000是换电的价格，还是买断电池包的价格？', True),
    ('完全不同的客户问题', False),
])
def test_original_body_is_scored_and_original_fact_remains_unchanged(current, accepted):
    cp, rows = scenario(old=['唯一开场', '198,000是换电的价格，还是买断电池包的价格？', '唯一末句'],
        new=['唯一开场', current, '唯一末句'])
    before = deepcopy((cp, rows))
    result = build(cp, rows)
    assert bool(result) is accepted
    if accepted:
        proof = result['proof']
        assert proof['version'] == 2
        assert proof['pairs'][1]['matched_by'] == 'confidence'
        assert proof['pairs'][1]['scores']['score'] >= 9000
        assert verify_correspondence(proof, cp, rows, pre_frame_id='checkpoint:test', post_frame_id='current',
            new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False)) == result['continuity']
    assert (cp, rows) == before


def test_short_price_punctuation_cannot_fall_back_to_coarse_equality():
    cp, rows = scenario(old=['唯一开场', '12.8', '唯一末句'], new=['唯一开场', '128', '唯一末句'])
    assert build(cp, rows) is None
    with pytest.raises(ValueError, match='TEXT_CORRESPONDENCE_PROOF_INVALID'):
        comparison_projection(cp, rows, pre_frame_id='before', post_frame_id='after')


@pytest.mark.parametrize('identity_keys', [('stable_id',), ('source_message_key',), ('stable_id', 'source_message_key')])
@pytest.mark.parametrize('changed', [False, True])
def test_frozen_suffix_uses_identity_and_keeps_proof_indexes_global(identity_keys, changed):
    question = '198,000是换电的价格，还是买断电池包的价格？'
    cp, rows = scenario(old=['唯一开场', question, '唯一末句'],
        new=[question.replace(',', '.') if changed else question, '唯一末句'])
    entries = cp['recent_messages'][1:]
    old = [e['business_projection'] for e in entries]
    identities = [{k: e[k] for k in identity_keys} for e in entries]
    old_tokens = {i: set(e.get('strong_boundary_tokens') or []) for i, e in enumerate(entries)}
    tokens = boundary_tokens_for_observations(rows, committed_only=False)
    decision = compare_business_viewport_continuity(old,
        normalized_business_message_sequence(rows, message_viewport_bounds=None),
        old_boundary_tokens=old_tokens, new_boundary_tokens=tokens)
    original = deepcopy((cp, rows, decision, identities))
    result = reconcile_checkpoint_continuity(cp, rows, decision, old_projection=old,
        old_boundary_tokens=old_tokens, old_identities=identities, pre_frame_id='before', post_frame_id='after')
    assert result['relation'] == 'business_sequence_equal', result
    assert [(p['old_index'], p['new_index']) for p in result['matched_pairs']] == [(0, 0), (1, 1)]
    if changed:
        proof = result['text_correspondence']
        assert [(p['old_index'], p['new_index']) for p in proof['pairs']] == [(1, 0), (2, 1)]
        verify_correspondence(proof, cp, rows, pre_frame_id='before', post_frame_id='after', new_boundary_tokens=tokens)
    else:
        assert not result.get('text_correspondence')
    assert (cp, rows, decision, identities) == original
