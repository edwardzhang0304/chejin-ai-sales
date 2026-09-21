"""Exercise preparation admission; the physical voice operation is stopped by a sentinel."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_c2_identity_gate_receipts import harness
from test_historical_confidence_flow import case
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.task_runner import FlowOutcomeAccumulator


class AtPhysicalVoiceBoundary(BaseException):
    pass


@pytest.mark.parametrize('actual_new_message', [False, True])
def test_old_ocr_change_does_not_spend_another_viewport_change(harness, monkeypatch, actual_new_message):
    cp, before, noisy, target = case(monkeypatch)
    bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
    voice = {'schema_version': 3, 'observation_id': 'new-voice', 'sender_role': 'customer',
        'sender_role_source': 'same_row_avatar', 'row_kind': 'voice_bubble', 'message_type': 'voice',
        'voice_state': 'untranscribed', 'voice_duration': '5', 'voice_anchor_key': 'new-voice',
        'bubble_rect': [100, 650, 300, 700],
        'source_message': {'id': 'new-voice', 'type': 'voice', 'sender_role': 'customer'}}
    before.append(deepcopy(voice)); noisy.append(deepcopy(voice))
    runner, _ = harness.make_runner(FakeApi(None), bridge)
    initial = bridge._contractual_message_payload({'frame_id': 'voice-before', 'observations': before})
    initial, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload=initial, read_run_id='prepare-hc')
    assert not errors
    initial['pre_send_reidentification_attempts'] = 1
    if actual_new_message:
        new = deepcopy(noisy[0]); new.update(observation_id='actually-new',content_clean='新要求：改天再说',bubble_rect=[100,740,450,780])
        noisy.append(new)
    bridge.last_message_payload = bridge._contractual_message_payload({'frame_id': 'voice-prepared', 'observations': noisy})
    calls = []
    def stop_at_physical_boundary(**kwargs):
        calls.append(kwargs)
        raise AtPhysicalVoiceBoundary()
    bridge.execute_voice_action = stop_at_physical_boundary
    args = dict(binding=Binding('worker','token','instance',run_status='running'), target=target,
        target_label='CJTEST01',sidecar_payload=initial,lease=Mock(),action_cancel_requested=lambda:False,
        enforce_read_targets=False,read_run_id='prepare-hc',excluded_voice_anchor_keys=set(),
        flow_outcomes=FlowOutcomeAccumulator(origin_read_run_id='prepare-hc'),operation_phase='pre_send_refresh')
    if actual_new_message:
        result = runner._finish_new_visible_voices_in_current_chat(**args)
        assert result['error_code'] == 'C2_PRE_SEND_MESSAGE_VIEWPORT_CHANGED_AGAIN'
        assert calls == []
    else:
        with pytest.raises(AtPhysicalVoiceBoundary): runner._finish_new_visible_voices_in_current_chat(**args)
        assert len(calls) == 1 and calls[0]['selected_pre_observation_id'] == 'new-voice'
