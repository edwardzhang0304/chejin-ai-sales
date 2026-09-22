"""A punctuation-only candidate match must not bypass historical verification."""
from copy import deepcopy
import pytest
import test_historical_alignment_flow as fixtures
from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.task_runner import TaskRunner, _bind_worker_continuity_contract_to_send_guard
from chejin_worker_client.pre_send_checkpoint import compare_checkpoint_to_observations
from chejin_worker_client.shared_rules import text_correspondence


@pytest.fixture
def price_history(monkeypatch, request):
    old, new = getattr(request, 'param', (
        '198,000是换电的价格，还是买断电池包的价格？',
        '198.000是换电的价格，还是买断电池包的价格？'))
    monkeypatch.setattr(fixtures, 'TEXTS', ['唯一开场', old, '唯一末句'])
    cp, before, current, target = fixtures.setup_case()
    current[1]['content_clean'] = new
    cp['historical_match_policy'] = c2_contract_v3()['text_correspondence_contract']['historical_match_policy']
    cp['checkpoint_digest'] = text_correspondence.checkpoint_digest(cp)
    return cp, before, current, target


@pytest.mark.parametrize('phase', ['initial', 'presend', 'sidecar', 'media', 'context'])
def test_punctuation_match_requires_proof_in_every_historical_entry(price_history, phase):
    cp, before, current, target = price_history
    frozen = deepcopy((cp, before, current))
    if phase == 'initial':
        result, errors = TaskRunner.__new__(TaskRunner)._align_initial_identity_frame(target=target,
            sidecar_payload={'ok': True, 'frame_id': 'current', 'observations': current}, read_run_id='price-read')
        assert not errors
        proof = result['sequence_alignment_evidence'].get('text_correspondence')
    elif phase == 'presend':
        result = compare_checkpoint_to_observations(fixtures.frozen_checkpoint(cp), current,
            before_frame_id='original', after_frame_id='current', current_tail_complete=True, historical_checkpoint=cp)
        assert result['comparison_result'] == 'checkpoint_equal', result
        proof = result.get('text_correspondence')
    elif phase == 'sidecar':
        comparison = compare_checkpoint_to_observations(fixtures.frozen_checkpoint(cp), before,
            before_frame_id='original', after_frame_id='baseline', current_tail_complete=True, historical_checkpoint=cp)
        guard = _bind_worker_continuity_contract_to_send_guard(
            fixtures.fixtures.production_send_context_guard(before, layout_ok=True), before,
            checkpoint=fixtures.frozen_checkpoint(cp), checkpoint_comparison=comparison,
            empty_welcome_baseline=False, historical_checkpoint=cp)
        result = fixtures.fixtures.production_sidecar_module().validate_send_context_guard(guard,
            fixtures.fixtures.production_send_context_guard(current, layout_ok=True), current_observations=current)
        assert result['ok'], result
        proof = (result['worker_continuity_decision'].get('text_correspondence') or {}).get('current')
    elif phase == 'context':
        from chejin_worker_client.historical_alignment import admit_current_context_frame
        result = admit_current_context_frame(cp, {'ok': True, 'frame_id': 'current', 'observations': current})
        assert result['history_load']['anchor_found'], result
        proof = result['history_load']['historical_decision'].get('text_correspondence')
    else:
        from chejin_worker_client.historical_alignment import reconcile_viewports
        from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations, compare_business_viewport_continuity
        from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
        tokens = boundary_tokens_for_observations(before, committed_only=False)
        decision = compare_business_viewport_continuity(
            normalized_business_message_sequence(before, message_viewport_bounds=None),
            normalized_business_message_sequence(current, message_viewport_bounds=None),
            old_boundary_tokens=tokens, new_boundary_tokens=boundary_tokens_for_observations(current, committed_only=False))
        result = reconcile_viewports(cp, before, current, decision, old_boundary_tokens=tokens)
        proof = (result.get('historical_viewport_correspondence') or {}).get('current')
    assert proof and proof['version'] == 2
    changed = next(p for p in proof['pairs'] if p['new_index'] == 1)
    assert changed['matched_by'] == 'confidence' and changed['scores']['score'] >= 9000
    assert (cp, before, current) == frozen


@pytest.mark.parametrize('price_history', [('12.8', '128')], indirect=True)
def test_short_numeric_change_cannot_fall_back_to_punctuation_blind_match(price_history):
    cp, before, current, target = price_history
    result, errors = TaskRunner.__new__(TaskRunner)._align_initial_identity_frame(target=target,
        sidecar_payload={'ok': True, 'frame_id': 'current', 'frame_observation': {'frame_id': 'current'},
                         'observations': current}, read_run_id='price-read')
    assert errors, result
    from chejin_worker_client.text_recheck import differing_text_observation_ids
    assert result['historical_match_diagnostics']['reason'] == 'insufficient_score_or_margin'
    assert differing_text_observation_ids(result['_text_recheck_old_projection'], result) == [current[1]['observation_id']]


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('append', [False, True])
def test_visible_tail_keeps_local_indexes_and_full_history_proof(price_history, changed, append):
    cp, before, drift, _ = price_history
    frozen = fixtures.frozen_checkpoint(cp)
    frozen['committed_tail'] = frozen['committed_tail'][1:]
    current = deepcopy((drift if changed else before)[1:])
    if append:
        added = deepcopy(current[-1])
        added.update(observation_id='new-customer', content_clean='请问周日几点营业',
                     bubble_rect=[100, 600, 450, 640])
        current.append(added)
    original = deepcopy((cp, frozen, current))
    result = compare_checkpoint_to_observations(frozen, current, before_frame_id='before',
        after_frame_id='after', current_tail_complete=True, historical_checkpoint=cp)
    assert result['comparison_result'] == ('checkpoint_unique_prefix_with_suffix' if append else 'checkpoint_equal'), result
    assert [(p['pre_sequence_index'], p['post_sequence_index']) for p in result['matched_pairs']] == [(0, 0), (1, 1)]
    assert result['new_suffix_observation_ids'] == (['new-customer'] if append else [])
    if changed:
        from chejin_worker_client.shared_rules import historical_text_alignment as rules
        from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
        proof = result['text_correspondence']
        assert [(p['old_index'], p['new_index']) for p in proof['pairs']] == [(1, 0), (2, 1)]
        assert proof['pairs'][0]['scores']['score'] >= 9000
        assert proof['checkpoint_digest'] == cp['checkpoint_digest']
        verified = rules.verify_correspondence(proof, cp, current, pre_frame_id='before', post_frame_id='after',
            new_boundary_tokens=boundary_tokens_for_observations(current, committed_only=False))
        assert verified['new_suffix_indexes'] == ([2] if append else [])
    else:
        assert not result.get('text_correspondence')
    assert (cp, frozen, current) == original


@pytest.mark.parametrize('price_history', [('198,000', '198.000')], indirect=True)
def test_visible_tail_low_score_still_rejects(price_history):
    cp, _, drift, _ = price_history
    frozen = fixtures.frozen_checkpoint(cp)
    frozen['committed_tail'] = frozen['committed_tail'][1:]
    result = compare_checkpoint_to_observations(frozen, drift[1:], before_frame_id='before',
        after_frame_id='after', current_tail_complete=True, historical_checkpoint=cp)
    assert result['comparison_result'] == 'checkpoint_not_continuous', result
    assert result['historical_match_diagnostics']['reason'] == 'insufficient_score_or_margin'
    assert result['historical_match_diagnostics']['best_score'] == 8571
    assert not result.get('text_correspondence')


@pytest.mark.parametrize('damage', ['conflicting_source', 'unknown', 'duplicate', 'reordered', 'projection'])
def test_visible_tail_mapping_cannot_substitute_message_identity(price_history, damage):
    cp, before, _, _ = price_history
    frozen = deepcopy(fixtures.frozen_checkpoint(cp))
    frozen['committed_tail'] = frozen['committed_tail'][1:]
    tail = frozen['committed_tail']
    if damage == 'conflicting_source':
        tail[0]['source_message_key'] = cp['recent_messages'][0]['source_message_key']
    elif damage == 'unknown':
        tail[0]['worker_stable_id'] = 'worker-message-999'
    elif damage == 'duplicate':
        tail[1] = deepcopy(tail[0])
    elif damage == 'reordered':
        tail.reverse()
    else:
        tail[0]['business_projection']['normalized_content_signature'] = 'different'
    result = compare_checkpoint_to_observations(frozen, before[1:], before_frame_id='before',
        after_frame_id='after', current_tail_complete=True, historical_checkpoint=cp)
    assert result['comparison_result'] == 'checkpoint_not_continuous'
    assert result['reason'] == 'TEXT_CORRESPONDENCE_BASELINE_INVALID'
