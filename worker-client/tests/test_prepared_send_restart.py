"""Reachability probe for a durable prepared send across a fresh capture.

External task API and desktop are controlled. Real Worker creates journal and
SQLite intent; no evidence is invented by the test after the crash point.
"""
import json
import multiprocessing
import os
from copy import deepcopy
from datetime import timedelta
import pytest
from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge, identity_checkpoint_for_facts, production_sidecar_module
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client import storage, task_runner
from chejin_worker_client import ui_lock
from chejin_worker_client.action_journal import read_action_journal


@pytest.mark.parametrize('fresh_ids', [False, True])
def test_prepared_same_message_after_process_exit(harness, monkeypatch, tmp_path, fresh_ids):
    task = harness.make_chat_reply_task(task_id='prepared-restart-audit', status='running')
    class SequenceApi(FakeApi):
        sent_done = False
        def get_wechat_message_batch(self, binding, batch_id):
            result = super().get_wechat_message_batch(binding, batch_id)
            result['authorization']['identity_checkpoint'] = deepcopy(self.read_targets[0].raw['identity_checkpoint'])
            result['reply_sequence'] = {'segment_count': 2, 'batch_id': batch_id,
                'conversation_id': 'conv-1', 'terminal': self.sent_done}
            return result
        def sent_ack(self, binding, claim, **kwargs):
            result = super().sent_ack(binding, claim, **kwargs)
            self.sent_done = True
            return result
    api = SequenceApi(task)
    harness.authorize_chat_reply_target(api)
    api.read_targets[0].raw['identity_checkpoint'] = identity_checkpoint_for_facts('conv-1', [{'content': '你好'}])
    api.message_ingest_result = 'duplicated'
    class Bridge(FakeBridge):
        captures = 0
        resumed = False
        prepared_at_send = None
        def send_reply(self, *args, **kwargs):
            self.prepared_at_send = deepcopy(read_action_journal(journal_path))
            return super().send_reply(*args, **kwargs)
        def sidecar_active(self):
            return False
        def get_messages(self, **kwargs):
            self.captures += 1
            # Desktop/OCR are controlled. IDs are NOT arbitrary native IDs:
            # the real Sidecar parser produces its frame-local win32_ocr ID
            # from the unchanged bubble after a small vertical move.
            y = 320 if fresh_ids and self.resumed else 300
            sidecar = production_sidecar_module()
            with monkeypatch.context() as ui:
                ui.setattr(sidecar, 'message_row_avatar_role_details',
                    lambda *args, **kw: {'role': 'customer', 'state': 'confirmed'})
                messages = sidecar.parse_messages_from_ocr(
                    [{'text': '你好', 'left': 350, 'top': y, 'right': 410,
                      'bottom': y + 20, 'center_x': 380, 'center_y': y + 10,
                      'confidence': 0.98}], (1000, 1000), target='张三',
                    layout_snapshot={'valid': True, 'message_viewport_bounds': [300, 100, 1000, 800],
                        'input_bounds': [300, 800, 1000, 1000]})
            assert len(messages) == 1 and messages[0]['content'] == '你好'
            assert messages[0]['sender_role'] == 'customer'
            assert messages[0]['source_adapter'] == 'win32_ocr'
            observations = sidecar.build_message_observations_v3(messages)
            self.get_messages_payloads = [sidecar.sanitize_sidecar_contract_output(
                {'ok': True, 'messages': messages, 'observations': observations})]
            return super().get_messages(**kwargs)
    bridge = Bridge(RpaResult(ok=True, result_code='unused'))
    first, seen = harness.make_runner(api, bridge)
    binding = Binding('worker-1', 'token', 'client-1', run_status='running')
    first.binding = binding
    storage.save_binding(binding)
    initialize = task_runner.initialize_action_journal
    journal_path = bridge.send_transaction_journal_path('reply-action-1')
    server_snapshot_path = tmp_path / 'server-at-exit.json'
    def exit_after_journal(*args, **kwargs):
        value = initialize(*args, **kwargs)
        if kwargs.get('action_kind') == 'send':
            server_snapshot_path.write_text(json.dumps({
                'inflight_flow_id': api.inflight_flow_id,
                'inflight_flow_state': api.inflight_flow_state,
                'events': api.events, 'sent_replies': bridge.sent_replies,
            }, ensure_ascii=False, default=str))
            # Abrupt process exit: no finally block, Flow/lock cleanup or
            # receipt synthesis. The actual Worker already saved its intent
            # and journal before this injected failure boundary.
            os._exit(71)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(task_runner, 'initialize_action_journal', exit_after_journal)
        child = multiprocessing.get_context('fork').Process(target=first.tick_once)
        child.start()
        child.join(15)
        if child.is_alive():
            child.terminate()
            child.join(5)
        assert child.exitcode == 71
    produced = [deepcopy(read_action_journal(journal_path))]
    external_state = json.loads(server_snapshot_path.read_text())
    assert not external_state['sent_replies']
    api.inflight_flow_id = external_state['inflight_flow_id']
    api.inflight_flow_state = external_state['inflight_flow_state']
    api.events = external_state['events']
    assert storage.load_reply_send_ack_outbox('reply-action-1')['status'] == 'intent'
    assert produced[0]['action_phase'] == 'not_attempted'
    assert not produced[0]['pre_action_identity_sequence'][0]['native_source_message_id']
    # Model an elapsed lease, not manual lock deletion or database edits.
    clock_now = ui_lock._utc_now
    monkeypatch.setattr(ui_lock, '_utc_now', lambda: clock_now() + timedelta(seconds=120))
    before = {'journal': produced[0], 'runtime': storage.load_runtime_control(), 'events': list(api.events)}
    # Same durable store, same external task and send claim; newly constructed
    # Runner executes normal restart barriers, then re-reads current screen.
    restarted, second_seen = harness.make_runner(api, bridge)
    restarted.binding = binding
    # The two flags TaskRunner.start loads from this same durable store.
    restarted._restart_recovery_flow_id = before['runtime']['inflight_flow_id']
    restarted._restart_backend_probe_pending = True
    bridge.resumed = True
    api.claim_send_duplicated = True
    actual_pre_sequences = []
    make_pre_sequence = task_runner.build_pre_action_identity_sequence
    def observe_pre_sequence(*args, **kwargs):
        value = make_pre_sequence(*args, **kwargs)
        actual_pre_sequences.append(deepcopy(value))
        return value
    monkeypatch.setattr(task_runner, 'build_pre_action_identity_sequence', observe_pre_sequence)
    restarted.tick_once()
    observed = {'before': before, 'events': api.events, 'seen': second_seen,
        'sent_replies': bridge.sent_replies, 'runtime': storage.load_runtime_control(),
        'ack': storage.load_reply_send_ack_outbox('reply-action-1'),
        'logs': storage.read_logs(limit=100),
        'remaining_journal': read_action_journal(bridge.send_transaction_journal_path('reply-action-1')),
        'actual_pre_sequences': actual_pre_sequences,
        'prepared_at_send': bridge.prepared_at_send,
        'child_exit_code': child.exitcode, 'expired_lease_clock_only_seconds': 120}
    assert actual_pre_sequences
    original_item = produced[0]['pre_action_identity_sequence'][0]
    current_item = actual_pre_sequences[-1][0]
    observed['changed_pre_sequence_fields'] = [k for k in original_item if original_item[k] != current_item[k]]
    for key in ('normalized_content_hash', 'sender_role', 'message_type', 'native_source_message_id', 'worker_stable_id'):
        assert original_item[key] == current_item[key]
    (tmp_path/'probe.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2, default=str))
    assert len(bridge.sent_replies) == 1, observed
    assert 'sent_ack:sent:None' in api.events, observed
    assert bridge.prepared_at_send['pre_action_identity_sequence'] == actual_pre_sequences[-1]
    if fresh_ids:
        original = bridge.prepared_at_send['original_send_preparation']
        assert original['pre_action_identity_sequence'] == produced[0]['pre_action_identity_sequence']
