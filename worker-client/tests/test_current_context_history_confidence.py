"""Current-screen context admission; no simulated claim of scroll/Windows OCR."""
from copy import deepcopy

import pytest
from test_c2_identity_gate_receipts import harness
from test_historical_confidence_flow import case
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import RpaResult
from chejin_worker_client.historical_alignment import admit_current_context_frame
from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.shared_rules import text_correspondence
import test_historical_alignment_flow as original_frames


@pytest.mark.parametrize('variant', ['ocr', 'exact', 'new_repeat', 'wrong_role', 'unrelated', 'no_frame', 'ui_action', 'scrolled', 'invalid_frame'])
def test_context_uses_shared_history_and_keeps_new_occurrences(monkeypatch, variant):
    cp, before, current, _ = case(monkeypatch)
    if variant == 'exact': current = before
    if variant == 'new_repeat':
        row = deepcopy(current[1]); row.update(observation_id='repeated-new-message', bubble_rect=[100,800,450,850])
        current.append(row)
    if variant == 'wrong_role': current[1]['sender_role'] = 'self'
    if variant == 'unrelated': current[1]['content_clean'] = '完全无关的话题'
    payload = {'ok': True, 'frame_id': '' if variant == 'no_frame' else 'context-frame', 'observations': current}
    if variant == 'ui_action': payload['ui_action_performed'] = True
    if variant == 'scrolled': payload['history_load'] = {'scroll_steps': 1}
    if variant == 'invalid_frame': payload['observation_validation_errors'] = ['invalid']
    frozen = deepcopy((cp, payload))
    result = admit_current_context_frame(cp, payload)
    load = result['history_load']
    assert load['anchor_found'] == (variant in {'ocr', 'exact', 'new_repeat'}), load
    assert load['scroll_steps'] == 0
    assert load['viewport_unchanged'] == (variant not in {'ui_action', 'scrolled'})
    assert load['restored_to_latest'] is False  # Never invent a performed scroll.
    if variant == 'new_repeat':
        assert load['historical_decision']['new_suffix_indexes'] == [3]
    assert (cp, payload) == frozen


@pytest.mark.parametrize('entry', ['pre_send', 'media'])
@pytest.mark.parametrize('bad_history', [False, True])
def test_both_worker_context_entries_use_hc_without_legacy_anchor_search(
    harness, monkeypatch, entry, bad_history,
):
    monkeypatch.setattr(original_frames, 'TEXTS', ['唯一开场', '中间的独立锚点',
        '我计划周末带家人一起看看适合城市通勤的新能源车'])
    cp, before, current, target = original_frames.setup_case()
    cp['historical_match_policy'] = c2_contract_v3()['text_correspondence_contract']['historical_match_policy']
    cp['checkpoint_digest'] = text_correspondence.checkpoint_digest(cp)
    current = deepcopy(before)
    current[2]['content_clean'] = current[2]['content_clean'].replace('一起', '起')
    if bad_history: current[1]['sender_role'] = 'self'
    bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
    calls = []
    def read(**kwargs):
        calls.append(kwargs)
        value = bridge._contractual_message_payload({'ok': True, 'frame_id': f'read-{len(calls)}',
                                                     'observations': deepcopy(current)})
        if kwargs.get('history_mode'):
            # The production C2 route clamps this to one frame/zero scrolls.
            # Use the actual old navigation matcher on the returned frame.
            from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import message_anchor_match_type, normalize_anchor_reply_key
            found = any(message_anchor_match_type({'content': r['content_clean'], 'sender': 'self'},
                anchor_ids=set(kwargs.get('anchor_ids') or []), anchor_content_keys=set(),
                reply_content_keys={normalize_anchor_reply_key(t) for t in kwargs.get('reply_content_keys') or []}) for r in current)
            value['history_load'] = {'ok': True, 'anchor_found': found, 'scroll_steps': 0,
                                     'restored_to_latest': False, 'stopped_reason': 'max_scroll_steps_reached'}
        return value
    bridge.get_messages = read
    runner, _ = harness.make_runner(FakeApi(None), bridge)
    committed, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok': True, 'frame_id': 'before-context', 'observations': before}, read_run_id='old-context')
    assert not errors
    frozen = original_frames.frozen_checkpoint(cp)
    for history_entry, row in zip(frozen['committed_tail'], before):
        history_entry['strong_boundary_anchor'] = {'normalized_content': row['content_clean']}
    target.raw['pre_send_fact_checkpoint_context'] = {'checkpoint': frozen}
    if entry == 'pre_send':
        result = runner._expand_pre_send_continuity_context_once(target=target, target_label='CJTEST01',
            locate_payload={}, expected_confirmed_self_text='', cancel_check=None, read_run_id='context-test')
    else:
        result = runner._expand_media_continuity_context_once(target=target, target_label='CJTEST01',
            pre_payload=committed, cancel_check=None,
            operation_phase='authorized_read', read_run_id='context-test')
    assert result['ok'] is (not bad_history), result
    assert len(calls) == (1 if bad_history else 2)
    assert not calls[0].get('history_mode')
    assert not calls[0].get('anchor_ids') and not calls[0].get('reply_content_keys')
    assert calls[0]['max_scroll_steps'] == 0 and calls[0]['max_snapshots'] == 1
    if not bad_history:
        aligned, errors = runner._align_initial_identity_frame(target=target,
            sidecar_payload=result['payload'], read_run_id='context-test')
        assert not errors
        assert aligned['sequence_alignment_evidence']['text_correspondence']['version'] == 2
