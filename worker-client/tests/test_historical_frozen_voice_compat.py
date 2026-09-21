"""Synthetic proof saved by unmodified 0.9.94, never a fabricated send."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from chejin_worker_client.historical_alignment import refreshed_correspondence
from chejin_worker_client.shared_rules import historical_text_alignment as rules, text_correspondence
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations


def fixture():
    path = Path(rules.__file__).parents[1] / 'tests/fixtures/frozen_voice_hc_v2.json'
    return json.loads(path.read_text())


@pytest.mark.parametrize('damage', ['none', 'score', 'source', 'observation', 'body', 'checkpoint', 'frame'])
def test_original_frozen_voice_proof_verifies_only_when_unchanged(damage):
    saved = fixture(); proof = saved['saved']['proof']; cp = saved['checkpoint']; rows = saved['observations']
    args = dict(pre_frame_id=proof['pre_frame_id'], post_frame_id=proof['post_frame_id'],
                new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))
    if damage == 'score': proof['best_score'] = 10000
    if damage == 'source': proof['pairs'][0]['source_message_key'] = 'wrong-source'
    if damage == 'observation': proof['pairs'][0]['observation_id'] = 'wrong-id'
    if damage == 'body': rows[1]['content_clean'] = '另外的语音'
    if damage == 'checkpoint': cp['recent_messages'][0]['effective_text']['text'] = '篡改权威正文'
    if damage == 'frame': args['post_frame_id'] = 'different-frame'
    if damage == 'none':
        assert rules.verify_correspondence(proof, cp, rows, **args) == saved['saved']['continuity']
    else:
        with pytest.raises(ValueError): rules.verify_correspondence(proof, cp, rows, **args)


@pytest.mark.parametrize('damage', ['none', 'body', 'version', 'observation'])
def test_frozen_refresh_changes_only_authority_digest(damage):
    saved = fixture(); proof = saved['saved']['proof']; cp = saved['checkpoint']
    payload = {'evidence': {'observations': saved['observations'], 'sequence_alignment_evidence': {
        'pre_frame_id': proof['pre_frame_id'], 'post_frame_id': proof['post_frame_id'], 'text_correspondence': proof}}}
    cp['text_correspondence_context']['known_entities'] = [{'kind': 'person', 'value': '测试员'}]
    if damage == 'body': payload['evidence']['observations'][1]['content_clean'] = '篡改语音'
    if damage == 'version': cp['recent_messages'][3]['effective_text']['version'] = 1
    if damage == 'observation': proof['pairs'][0]['observation_id'] = 'wrong-id'
    cp['checkpoint_digest'] = text_correspondence.checkpoint_digest(cp)
    if damage == 'version':
        # Incomplete effective-version metadata is also invalid authority.
        with pytest.raises(ValueError):
            rules.verify_correspondence({**proof, 'checkpoint_digest': cp['checkpoint_digest']},
                cp, payload['evidence']['observations'],
                pre_frame_id=proof['pre_frame_id'], post_frame_id=proof['post_frame_id'],
                new_boundary_tokens=boundary_tokens_for_observations(payload['evidence']['observations'], committed_only=False))
        # The Worker wrapper deliberately catches that rejection and returns
        # no replacement proof. Both layers must reject the changed version.
        assert refreshed_correspondence(payload, cp) is None
        return
    result = refreshed_correspondence(payload, cp)
    assert bool(result) == (damage == 'none')
    if result: assert result == {**proof, 'checkpoint_digest': cp['checkpoint_digest']}


def test_frozen_two_frame_comparison_preserves_saved_voice_mapping():
    saved = fixture(); cp = saved['checkpoint']; old = saved['baseline']; new = saved['observations']
    frozen = {'baseline': None, 'current': saved['saved']['proof']}
    result = rules.compare_historical_viewports(cp, old, new,
        old_boundary_tokens=boundary_tokens_for_observations(old, committed_only=False), frozen_correspondence=frozen)
    assert result[2]['relation'] == 'business_sequence_equal'
    assert result[2]['text_correspondence'] == frozen
    changed = deepcopy(frozen); changed['current']['best_score'] = 10000
    with pytest.raises(ValueError):
        rules.compare_historical_viewports(cp, old, new,
            old_boundary_tokens=boundary_tokens_for_observations(old, committed_only=False), frozen_correspondence=changed)
