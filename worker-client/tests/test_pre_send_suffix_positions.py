"""New facts keep their position in the complete frame, including old rows."""
import pytest
from copy import deepcopy

from chejin_worker_client.models import WechatReadTarget
from test_wechat_c2 import build_v3_message_ingest_payload


@pytest.mark.parametrize('old_count', [1, 2, 7])
@pytest.mark.parametrize('with_geometry', [False, True])
def test_suffix_positions_match_full_frame_business_projection(old_count, with_geometry):
    def row(index):
        value = {'schema_version': 3, 'observation_id': f'row-{index}', 'row_kind': 'text_bubble',
            'sender_role': 'customer', 'sender_role_source': 'same_row_avatar', 'message_type': 'text',
            'voice_state': 'not_voice', 'content_clean': f'客户问题{index}',
            'source_message': {'id': f'row-{index}', 'source_adapter': 'win32_ocr'}}
        if with_geometry: value['bubble_rect'] = [320, 100 + index * 50, 550, 140 + index * 50]
        return value
    rows = [row(i) for i in range(old_count + 2)]
    target = WechatReadTarget(conversation_id='conv-suffix', rpa_session_key='wx:test',
        display_name='CJORDER01', remark_code='CJORDER01', authorization_revision='rev-test')
    payload = build_v3_message_ingest_payload(target, {
        'observation_schema_version': 3, 'observations': rows[old_count:],
        'authoritative_evidence_observations': rows,
    })
    assert len(payload['messages']) == 2
    for expected, message in enumerate(payload['messages'], start=old_count+1):
        assert message['message_position']['screen_order'] == expected
        assert message['raw_payload']['business_projection']['screen_order'] == expected-1
    assert len(payload['evidence']['observations']) == len(rows)


@pytest.mark.parametrize('change', ['none', 'missing_ledger', 'unconfirmed', 'missing_origin',
    'wrong_role', 'wrong_type', 'duplicate_pair', 'bad_index', 'bad_comparison'])
def test_prefix_evidence_requires_confirmed_original_fact(monkeypatch, change):
    from chejin_worker_client import task_runner
    row = {'observation_id': 'visible-old', 'row_kind': 'text_bubble', 'message_type': 'text', 'sender_role': 'customer'}
    fact = {'source_message_key': 'original-source-key', 'message_type': 'text', 'sender_role': 'customer'}
    ledger = {'ingest_state': 'confirmed', 'origin_read_run_id': 'old-read', 'terminal_state': 'completed'}
    comparison = {'comparison_result': 'checkpoint_unique_prefix_with_suffix', 'current_prefix_count': 1,
        'matched_pairs': [{'pre_sequence_index': 0, 'post_sequence_index': 0}]}
    target = WechatReadTarget(conversation_id='conv-prefix', rpa_session_key='wx:test', display_name='CJORDER01',
        raw={'pre_send_fact_checkpoint_context': {'checkpoint': {'committed_tail': [fact]}}})
    payload = {'pre_send_fact_checkpoint_prefix_count': 1, 'pre_send_fact_checkpoint_comparison': comparison, 'observations': [row]}
    if change == 'missing_ledger': ledger = None
    elif change == 'unconfirmed': ledger['ingest_state'] = 'outbox_waiting'
    elif change == 'missing_origin': ledger['origin_read_run_id'] = ''
    elif change == 'wrong_role': fact['sender_role'] = 'self'
    elif change == 'wrong_type': fact['message_type'] = 'voice'
    elif change == 'duplicate_pair': comparison['matched_pairs'] *= 2
    elif change == 'bad_index': comparison['matched_pairs'][0]['pre_sequence_index'] = 1
    elif change == 'bad_comparison': comparison['comparison_result'] = 'checkpoint_not_continuous'
    original = deepcopy(payload)
    def read(conversation_id, key):
        assert conversation_id == target.conversation_id and key == fact['source_message_key']
        return ledger
    monkeypatch.setattr(task_runner, 'load_c2_ledger_entry', read)
    if change != 'none':
        with pytest.raises(ValueError): task_runner._confirmed_pre_send_prefix_slots(target, payload, 'current-read', 'visual_top')
    else:
        states = task_runner._confirmed_pre_send_prefix_slots(target, payload, 'current-read', 'visual_top')
        assert states[0]['source_message_key'] == 'original-source-key'
        assert states[0]['fact_scope'] == 'historical' and states[0]['delivery_state'] == 'backend_confirmed'
    assert payload == original


@pytest.mark.parametrize('change', ['fresh', 'stale_pairs', 'changed_history', 'invalid_guard'])
def test_final_media_frame_rechecks_frozen_prefix_without_reidentifying(monkeypatch, change):
    from chejin_worker_client import task_runner
    from test_task_runner import FakeBridge
    from test_pre_send_checkpoint import _checkpoint, _fact

    fact = _fact('old-1', sender_role='customer', message_type='text', content='请介绍一下')
    checkpoint = _checkpoint(fact)
    row = deepcopy(fact['_business_observation'])
    for key in list(row):
        if key.startswith('_worker'): row.pop(key)
    row.update(observation_id='fresh-frame-old-row', sender_role_source='same_row_avatar')
    if change == 'changed_history': row['content_clean'] = '这已是另一段会话'
    target = WechatReadTarget(conversation_id='conv-prefix', rpa_session_key='wx:test', display_name='CJORDER01',
        authorization_revision='test-revision', raw={'pre_send_fact_checkpoint_context': {'checkpoint': checkpoint}})
    payload = {'ok': True, 'frame_id': 'fresh-media-frame', 'authoritative_frame_source': 'final_read',
        'observation_schema_version': 3, 'observations': [row],
        'send_context_guard': FakeBridge._send_context_guard([row])}
    if change == 'stale_pairs':
        payload['pre_send_fact_checkpoint_prefix_count'] = 7
        payload['pre_send_fact_checkpoint_comparison'] = {'comparison_result': 'checkpoint_equal',
            'current_prefix_count': 7, 'after_frame_id': 'obsolete-frame', 'matched_pairs': []}
    if change == 'invalid_guard': payload['send_context_guard'] = {}
    original_rows = deepcopy(payload['observations'])
    runner = task_runner.TaskRunner.__new__(task_runner.TaskRunner)
    if change in {'fresh', 'stale_pairs'}:
        # The media pipeline has already aligned its final rows. Produce that
        # existing evidence with the actual serializer, not made-up IDs.
        _, baseline = runner._compare_pre_send_fact_checkpoint_frame(
            target=target, sidecar_payload=payload, read_run_id='current-read', comparison_only=True)
        payload['sequence_alignment_evidence'] = task_runner._checkpoint_alignment_evidence(checkpoint, [row], baseline)
    monkeypatch.setattr(runner, '_assign_sequence_new_suffix_identities',
        lambda **kw: pytest.fail('final prefix comparison assigned new identity'))
    monkeypatch.setattr(task_runner, 'load_c2_ledger_entry', lambda *a: {
        'ingest_state': 'confirmed', 'origin_read_run_id': 'original-read', 'terminal_state': 'completed'})
    if change in {'changed_history', 'invalid_guard'}:
        with pytest.raises(ValueError, match='C2_PRE_SEND_CHECKPOINT_NOT_CONTINUOUS'):
            runner._build_final_slot_incremental_plan(target=target, sidecar_payload=payload, read_run_id='current-read')
    else:
        result = runner._build_final_slot_incremental_plan(target=target, sidecar_payload=payload, read_run_id='current-read')
        assert result['preliminary_payload']['messages'] == []
        assert result['slot_ledger_states'][0]['origin_read_run_id'] == 'original-read'
        assert payload['pre_send_fact_checkpoint_prefix_count'] == 1
        assert payload['pre_send_fact_checkpoint_comparison']['after_frame_id'] == 'frame:fresh-media-frame'
    assert payload['observations'] == original_rows


@pytest.mark.parametrize('change', ['none', 'native_proof', 'other_frame', 'not_settled', 'bad_mapping', 'incomplete',
    'bad_guard', 'changed_facts', 'physical_use'])
def test_settled_voice_frame_only_supplies_fact_comparison(monkeypatch, change):
    from chejin_worker_client import task_runner
    from test_task_runner import FakeBridge
    from test_pre_send_checkpoint import _checkpoint, _fact
    fact = _fact('old-1', sender_role='customer', message_type='text', content='原来的问题')
    rows = [deepcopy(fact['_business_observation'])]
    guard = FakeBridge._send_context_guard(rows)
    result = {'voice_action_stage': 'execute', 'transcript_binding_status': 'confirmed',
        'confirmed_action_mapping': {'binding_confirmed': True}, 'post_frame_id': 'action-frame',
        'message_viewport_change_evidence': guard}
    payload = {'ok': True, 'observations': rows, 'authoritative_frame_source': 'final_read',
        'ui_frame_invalidated': True, 'post_frame_id': 'action-frame', 'final_frame_reusable': True,
        'business_continuity_evidence': {'relation': 'business_sequence_equal'}, 'voice_transcription': result}
    if change == 'native_proof':
        guard.pop('sequence_sha256'); guard.pop('bottom')
    if change == 'other_frame': result['post_frame_id'] = 'other-action'
    if change == 'not_settled': payload['business_continuity_evidence']['relation'] = 'business_sequence_not_continuous'
    if change == 'bad_mapping': result['confirmed_action_mapping']['binding_confirmed'] = False
    if change == 'incomplete': payload['final_frame_reusable'] = False
    if change == 'bad_guard': guard['ok'] = False
    if change == 'changed_facts': rows[0]['content_clean'] = '另一个问题'
    target = WechatReadTarget(conversation_id='conv-prefix', rpa_session_key='wx:test', display_name='CJORDER01',
        raw={'pre_send_fact_checkpoint_context': {'checkpoint': _checkpoint(fact)}})
    original = deepcopy(payload)
    runner = task_runner.TaskRunner.__new__(task_runner.TaskRunner)
    monkeypatch.setattr(runner, '_assign_sequence_new_suffix_identities',
        lambda **kw: pytest.fail('read-only frame allocated new identity'))
    prepared, comparison = runner._compare_pre_send_fact_checkpoint_frame(
        target=target, sidecar_payload=payload, read_run_id='current-read', comparison_only=change != 'physical_use')
    assert (comparison['comparison_result'] == 'checkpoint_equal') == (change in {'none', 'native_proof'})
    assert prepared['ui_frame_invalidated'] is True
    assert payload == original
