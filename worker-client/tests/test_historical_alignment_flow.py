"""Production comparison entrances with constructed desktop frames, no Windows claim."""
import copy
import hashlib
from types import SimpleNamespace

import pytest
import test_task_runner as fixtures
from test_c2_identity_gate_receipts import harness
from chejin_worker_client.models import WechatReadTarget
from chejin_worker_client.task_runner import TaskRunner, _bind_worker_continuity_contract_to_send_guard
from chejin_worker_client.pre_send_checkpoint import compare_checkpoint_to_observations
from chejin_worker_client.shared_rules import text_correspondence

TEXTS = ["周末带家人出去看看", "一般两厢，平时接送孩子", "顺便看看后备箱空间"]


def setup_case():
    facts = [{"content": text} for text in TEXTS]
    checkpoint = fixtures.identity_checkpoint_for_facts("d1-conversation", facts)
    checkpoint["conversation_id"] = "d1-conversation"
    checkpoint["text_correspondence_context"] = {"version": 1, "known_entities": []}
    for row, text in zip(checkpoint["recent_messages"], TEXTS):
        row["effective_text"] = {"text": text, "version": 0, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    checkpoint["checkpoint_digest"] = text_correspondence.checkpoint_digest(checkpoint)
    rows = [fixtures.TaskRunnerTest._ai_send_observation(f"now-{i}", sender_role="customer", content=text)
            for i, text in enumerate(TEXTS)]
    for i, row in enumerate(rows):
        row["bubble_rect"] = [100, 120 + i*130, 450, 160+i*130]
        row["contract_errors"] = []
    current = copy.deepcopy(rows)
    current[1]["content_clean"] = "般两厢，平时接送孩子"
    target = WechatReadTarget(conversation_id="d1-conversation", remark_code="CJTEST01", display_name="CJTEST01",
        rpa_session_key="", read_reason="unread", authorization_revision="d1-revision",
        raw={"identity_checkpoint": checkpoint})
    return checkpoint, rows, current, target


def test_initial_worker_alignment_keeps_original_and_reuses_all_old_ids():
    checkpoint, _, current, target = setup_case()
    original = copy.deepcopy((checkpoint, current))
    runner = TaskRunner.__new__(TaskRunner)
    aligned, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={"ok": True, "frame_id": "d1-now", "observations": current}, read_run_id="d1-read")
    assert errors == []
    assert [r.get("_worker_stable_id") for r in aligned["observations"]] == ["worker-message-1", "worker-message-2", "worker-message-3"]
    assert aligned["sequence_alignment_evidence"]["text_correspondence"]["pairs"][1]["matched_by"] == "context_ocr"
    assert aligned["sequence_alignment_evidence"]["new_suffix_observation_ids"] == []
    assert (checkpoint, current) == original


def frozen_checkpoint(checkpoint):
    return {"schema_version": 5, "committed_tail": [{**e, "worker_stable_id": e["stable_id"]}
            for e in checkpoint["recent_messages"]]}


def test_same_rule_reaches_pre_send_and_physical_guard():
    checkpoint, before, current, target = setup_case()
    frozen = frozen_checkpoint(checkpoint)
    compared = compare_checkpoint_to_observations(frozen, current, before_frame_id="before", after_frame_id="after",
        current_tail_complete=True, historical_checkpoint=checkpoint)
    assert compared["comparison_result"] == "checkpoint_equal", compared
    assert compared["text_correspondence"]
    sidecar = fixtures.production_sidecar_module()
    # The baseline itself can contain the tolerated OCR error. A later correct
    # frame must not be judged against that error as a new historical fact.
    guard = _bind_worker_continuity_contract_to_send_guard(
        fixtures.production_send_context_guard(current, layout_ok=True), current,
        checkpoint=frozen, checkpoint_comparison=compared, empty_welcome_baseline=False,
        historical_checkpoint=checkpoint)
    result = sidecar.validate_send_context_guard(guard,
        fixtures.production_send_context_guard(before, layout_ok=True), current_observations=before)
    assert result["ok"], result
    changed = copy.deepcopy(before)
    changed[1]["content_clean"] = "不一般两厢，平时接送孩子"
    blocked = sidecar.validate_send_context_guard(guard,
        fixtures.production_send_context_guard(changed, layout_ok=True), current_observations=changed)
    assert not blocked["ok"]


def test_disabling_correspondence_reproduces_old_failure():
    checkpoint, _, current, target = setup_case()
    checkpoint.pop("text_correspondence_context")
    runner = TaskRunner.__new__(TaskRunner)
    _, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={"ok": True, "frame_id": "d1-disabled", "observations": current}, read_run_id="d1-read")
    assert errors
    compared = compare_checkpoint_to_observations(frozen_checkpoint(checkpoint), current,
        before_frame_id="before", after_frame_id="after", current_tail_complete=True)
    assert compared["comparison_result"] == "checkpoint_continuity_context_expansion_required"
    assert compared["old_tail_fully_consumed"] is False


def test_expired_context_rechecks_same_frozen_frame_without_changing_ids_or_text():
    from chejin_worker_client.historical_alignment import refreshed_correspondence
    checkpoint, _, current, target = setup_case()
    runner = TaskRunner.__new__(TaskRunner)
    aligned, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok':True,'frame_id':'frozen-read','observations':current},read_run_id='original-read')
    assert not errors
    payload = {'evidence':{'observations':current,'sequence_alignment_evidence':aligned['sequence_alignment_evidence']}}
    before = copy.deepcopy(payload)
    updated = copy.deepcopy(checkpoint)
    updated['text_correspondence_context']['known_entities'] = [{'kind':'person','value':'张师傅'}]
    updated['checkpoint_digest'] = text_correspondence.checkpoint_digest(updated)
    proof = refreshed_correspondence(payload, updated)
    assert proof and proof['checkpoint_digest']==updated['checkpoint_digest']
    assert payload==before
    # A newly registered protected name overlapping the edit cannot be
    # silently reclassified as ordinary text by the refresh.
    updated['text_correspondence_context']['known_entities'] = [{'kind':'person','value':'一般两厢'}]
    updated['checkpoint_digest'] = text_correspondence.checkpoint_digest(updated)
    assert refreshed_correspondence(payload, updated) is None


@pytest.mark.parametrize('compatible', [True, False])
@pytest.mark.parametrize('previous_error', ['TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED', 'ConnectionError'])
def test_worker_refresh_persists_same_frame_and_caps_failed_refreshes(harness, compatible, previous_error):
    from test_gate_only_outbox import setup_gate
    from chejin_worker_client import storage
    runner, api, bridge, binding, payload = setup_gate(harness)
    checkpoint, _, current, target = setup_case()
    aligned, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok': True, 'frame_id': 'frozen-read', 'observations': current}, read_run_id='original-read')
    assert not errors
    payload['evidence']['observations'] = current
    payload['evidence']['sequence_alignment_evidence'] = aligned['sequence_alignment_evidence']
    before = copy.deepcopy(payload)
    updated = copy.deepcopy(checkpoint)
    updated['text_correspondence_context']['known_entities'] = [
        {'kind': 'person', 'value': '张师傅' if compatible else '一般两厢'}]
    updated['checkpoint_digest'] = text_correspondence.checkpoint_digest(updated)
    calls = []
    def authorize(*args, **kwargs):
        calls.append('authorize')
        return {'allowed': True, 'authorization_revision': 'fresh-authority', 'identity_checkpoint': updated}
    api.get_wechat_read_authorization = authorize
    outbox = storage.enqueue_c2_outbox(payload)
    for attempt in range(1, 4):
        storage.transition_c2_outbox(outbox, status='refresh_pending',
            error=previous_error, increment_refresh=True)
        ok = runner._refresh_c2_outbox_authorization(binding=binding, payload=payload, outbox_id=outbox)
        if compatible:
            assert ok
            item = storage.load_c2_outbox_entry(outbox)
            assert item['status'] == 'waiting'
            assert item['payload']['messages'] == before['messages']
            assert item['payload']['evidence']['observations'] == before['evidence']['observations']
            assert item['payload']['evidence']['sequence_alignment_evidence']['text_correspondence']['checkpoint_digest'] == updated['checkpoint_digest']
            audit = storage.load_c2_state(f'text_correspondence_refresh:{outbox}:1')
            assert audit['original_proof'] == before['evidence']['sequence_alignment_evidence']['text_correspondence']
            break
        assert not ok
    if not compatible:
        item = storage.load_c2_outbox_entry(outbox)
        assert item['status'] == 'capability_paused' and item['payload'] == before
        assert item['last_error'] == 'TEXT_CORRESPONDENCE_REFRESH_EXHAUSTED'
        assert len(calls) == 2
    assert not bridge.message_reads and not bridge.sent_replies
    assert payload == before


@pytest.mark.parametrize('identity', ['original', 'unproven', 'untranscribed'])
def test_completed_voice_uses_same_text_rule_but_keeps_media_identity_gate(harness, identity):
    texts = [TEXTS[1], TEXTS[0], TEXTS[2]]
    checkpoint = fixtures.identity_checkpoint_for_facts('voice-history', [
        {'content': texts[0], 'message_type': 'voice', 'native_source_message_id': 'original-voice'},
        {'content': texts[1]}, {'content': texts[2]}])
    checkpoint['conversation_id'] = 'voice-history'
    checkpoint['text_correspondence_context'] = {'version': 1, 'known_entities': []}
    for row, text in zip(checkpoint['recent_messages'], texts):
        row['effective_text'] = {'version': 0, 'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    checkpoint['checkpoint_digest'] = text_correspondence.checkpoint_digest(checkpoint)
    observations = [fixtures.TaskRunnerTest._ai_send_observation(f'voice-now-{i}', sender_role='customer', content=text)
                    for i, text in enumerate(texts)]
    for i, row in enumerate(observations):
        row['bubble_rect'] = [100, 120+i*130, 450, 160+i*130]
        row['contract_errors'] = []
    observations[0].update(content_clean='般两厢，平时接送孩子', message_type='voice',
        row_kind='voice_transcript' if identity != 'untranscribed' else 'voice_bubble',
        voice_state='transcribed' if identity != 'untranscribed' else 'untranscribed',
        native_source_message_id='original-voice' if identity != 'unproven' else 'another-voice')
    target = WechatReadTarget(conversation_id='voice-history', remark_code='CJTEST01', display_name='CJTEST01',
        rpa_session_key='', authorization_revision='voice-revision', raw={'identity_checkpoint': checkpoint})
    runner, _ = harness.make_runner(fixtures.FakeApi(None), fixtures.FakeBridge(fixtures.RpaResult(ok=True, result_code='unused')))
    aligned, errors = runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok': True, 'frame_id': 'voice-now', 'observations': observations}, read_run_id='voice-current')
    if identity == 'original':
        assert not errors, errors
        assert aligned['observations'][0]['_worker_stable_id'] == 'worker-message-1'
        assert aligned['observations'][0]['content_clean'] == '般两厢，平时接送孩子'
    else:
        assert errors
