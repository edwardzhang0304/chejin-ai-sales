"""No fixtures are changed after Worker.start; only explicit Start is invoked."""
import json
from pathlib import Path
import sys
import time
import requests
from unittest.mock import patch
from contextlib import nullcontext

from chejin_worker_client import storage
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.task_runner import TaskRunner
from test_task_runner import FakeBridge, completed_image_process_result

request = json.loads(Path(sys.argv[1]).read_text())
destination = Path(sys.argv[2])
original = request['payload']
cid, flow = original['conversation_id'], original['read_run_id']
kind = request['message_type']
binding = Binding(request['worker']['id'], request['worker']['worker_token'], 'followup-test', run_status='running')
api = WorkerApiClient(request['url'] + '/api')
api.inflight_flow_id = flow
if sys.argv[3] == 'prepare':
    target = WechatReadTarget(conversation_id=cid, rpa_session_key=original['rpa_session_key'],
        display_name=original['remark_code'], remark_code=original['remark_code'],
        authorization_revision=original['authorization_revision'],
        unread_generation=original['unread_generation'], read_reason=original['evidence']['authorization_read_reason'])
    storage.save_binding(binding)
    # Feed only a physical observation. The production read owner must create the
    # message identity, alignment evidence, Ledger and Outbox itself. No test writes
    # a successful sequence proof or confirms a local record.
    observation = {'schema_version': 3, 'observation_id': 'original-media-observation',
        'row_kind': 'voice_transcript' if kind == 'voice' else 'image_bubble',
        'sender_role': 'customer', 'sender_role_source': 'parent_voice' if kind == 'voice' else 'same_row_avatar',
        'message_type': kind, 'voice_state': 'transcribed' if kind == 'voice' else 'not_voice',
        'item_state': 'completed' if kind == 'voice' else 'discovered',
        'content_clean': 'Original voice result' if kind == 'voice' else '',
        'parent_voice_anchor_key': 'original-voice-anchor', 'bubble_rect': [420, 180, 650, 320],
        'image_physical_anchor': {'sender_role': 'customer', 'occurrence_index': 0,
            'preceding_stable_message': 'before-original-image', 'following_stable_message': 'after-original-image',
            'bubble_visual_fingerprint': 'original-image-visible-fingerprint'},
        'source_message': {'id': 'original-media-observation', 'type': kind, 'sender_role': 'customer',
                          'frame_visual_id': 'original-image-visible-fingerprint'}}
    target.raw = {'identity_checkpoint': api.get_wechat_read_authorization(binding, cid)['identity_checkpoint']}
    pre_bridge = FakeBridge(RpaResult(ok=True, result_code='unused', message='unused'))
    pre_bridge.get_messages_payloads = [
        {'authoritative_frame_source': 'initial_read', 'observations': [observation]},
        {'authoritative_frame_source': 'final_read', 'observations': [observation]}]
    if kind == 'image':
        # Model a normal final reread with a following text row. Both frames enter
        # the original image continuity matcher; the test supplies no match result.
        following_text = {'schema_version': 3, 'observation_id': 'text-after-image',
            'row_kind': 'text_bubble', 'sender_role': 'customer', 'sender_role_source': 'same_row_avatar',
            'message_type': 'text', 'voice_state': 'not_voice', 'item_state': 'completed',
            'content_clean': 'Text arrived while reading image', 'bubble_rect': [420, 340, 680, 390],
            'source_message': {'id': 'text-after-image', 'type': 'text', 'sender_role': 'customer',
                               'content': 'Text arrived while reading image'}}
        pre_bridge.get_messages_payloads[1]['observations'].append(following_text)
    if kind == 'voice':
        voice_before = {'id': 'voice-before', 'source_adapter': 'win32_ocr', 'native_source_message_id': '',
            'type': 'voice', 'sender_role': 'customer', 'voice_duration': 2, 'content': '[语音] 2"',
            'voice_anchor_stable_key': 'original-voice-anchor', 'frame_visual_id': 'voice-before-frame'}
        voice_after = {**voice_before, 'id': 'voice-after', 'content': 'Original voice result',
                       'frame_visual_id': 'voice-after-frame'}
        pre_bridge.get_messages_payloads = [{'messages': [voice_before]}, {'messages': [voice_after]}]
        pre_bridge.voice_payload = {'ok': True, 'state': 'voice_transcribe_completed', 'action_phase': 'confirmed',
            'business_state': 'completed', 'business_result_confirmed': True, 'ui_action_performed': True,
            'transcribed_messages': [voice_after], 'processed_voice_anchor_keys': ['original-voice-anchor'],
            'failed_voice_anchor_keys': [], 'item_action_outcomes': [{'action_phase': 'confirmed',
                'business_state': 'completed', 'business_result_confirmed': True,
                'physical_anchor_keys': ['original-voice-anchor']}]}
    noop = lambda *_: None
    pre_runner = TaskRunner(api, pre_bridge, on_profile=noop, on_status=noop, on_step=noop,
        on_task=noop, on_result=noop, on_error=noop)
    pre_runner.binding = binding
    assert pre_runner._start_inflight_flow(binding, flow_id=flow, flow_kind='c2_read', conversation_id=cid,
        unread_generation=target.unread_generation, authorization_revision=target.authorization_revision)
    send_original = api.session.send
    def fail_original_upload(prepared, **kwargs):
        if prepared.url.endswith('/messages/ingest'):
            response = requests.Response()
            response.status_code = 503
            response._content = b'{"code":"TEST_ORIGINAL_UPLOAD_UNAVAILABLE","message":"Controlled original transport failure"}'
            return response
        return send_original(prepared, **kwargs)
    api.session.send = fail_original_upload
    crash_before_outbox = (nullcontext() if request['ordinary_outbox'] else
        patch.object(pre_runner, '_stage_payload_ledger', side_effect=OSError('Controlled crash before payload staging')))
    with patch('chejin_worker_client.omniauto_vision.vision_configuration_status',
            return_value={'ready': True, 'config': {'customer_image_understanding': {'enabled': True}}}), \
         patch('chejin_worker_client.omniauto_vision.process_image_slot',
            side_effect=completed_image_process_result(content='Original image result', image_sha256='a' * 64,
                                                      request_style='anthropic_messages_vision')), \
         crash_before_outbox:
        pre_result = pre_runner._read_one_wechat_target(binding, target,
            current_step='state_target_message_read', enforce_read_targets=True)
    api.session.send = send_original
    pending = storage.list_c2_ledger_entries(cid, ingest_state='waiting')
    destination.with_name('original-read.json').write_text(json.dumps({'result': pre_result, 'ledger': pending},
                                                                   ensure_ascii=False, indent=2))
    media_pending = [entry for entry in pending if entry['message_type'] == kind]
    assert len(media_pending) == 1, pre_result
    key = media_pending[0]['source_message_key']
    with storage.db_connection() as conn:
        waiting = storage.unsettled_c2_outbox_rows(conn, read_run_id=flow)
    assert bool(waiting) == request['ordinary_outbox'], waiting
    outbox_id = waiting[0]['outbox_id'] if waiting else None
    payload = storage.load_c2_outbox_entry(outbox_id)['payload'] if outbox_id else None
    api.set_run_status(binding, 'faulted')
    headers = {'X-Worker-Token': binding.worker_token, 'X-Client-Instance-Id': binding.client_instance_id,
               'X-Inflight-Flow-Id': flow}
    finish = requests.post(request['url'] + '/api/workers/' + binding.worker_id + '/inflight-flow/finish',
        headers=headers, json={'flow_id': flow, 'terminal_kind': 'technical_failed', 'conversation_id': cid,
                              'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'}, timeout=10)
    assert finish.status_code == 200, finish.text
    if not request.get('keep_customer_valid'):
        invalid = requests.post(request['url'] + '/api/leads/' + request['lead_id'] + '/mark-invalid',
            json={'invalid_reason': 'test_data'}, timeout=10)
        assert invalid.status_code == 200, invalid.text
    binding.run_status = 'faulted'
    storage.save_binding(binding)
    storage.save_c2_state('inflight_finish_receipt:' + flow, {'terminal_kind': 'technical_failed',
        'conversation_id': cid, 'error_code': 'MESSAGE_CONTRACT_REVISION_MISMATCH'})
    api.inflight_flow_id = None
    destination.with_name('prepared-media.json').write_text(json.dumps({'key': key, 'outbox_id': outbox_id, 'payload': payload}, ensure_ascii=False))
    sys.exit(0)

prepared = json.loads(destination.with_name('prepared-media.json').read_text())
key, outbox_id, payload = prepared['key'], prepared['outbox_id'], prepared['payload']
binding = storage.load_binding()
api.inflight_flow_id = None
exchanges, physical, errors = [], [], []
clicked = False
send = api.session.send
def transport(prepared, **kwargs):
    response = send(prepared, **kwargs)
    if '/wechat/' in prepared.url or prepared.url.endswith(('/inflight-flow/finish', '/run-status', '/claim')):
        exchanges.append({'url': prepared.url, 'status': response.status_code, 'response': response.json()})
    return response
api.session.send = transport
class Boundary(FakeBridge):
    def sidecar_active(self): return False
    def run_add_friend(self, task, emit_step, cancel_check=None):
        assert clicked and task.id == request['next_task_id']
        physical.append(task.id)
        return super().run_add_friend(task, emit_step, cancel_check)
    def get_messages(self, *args, **kwargs): raise AssertionError('No new WeChat read during recovery')
    def locate_chat(self, *args, **kwargs): raise AssertionError('No WeChat locate during recovery')
    def execute_voice_action(self, *args, **kwargs): raise AssertionError('No repeated voice action')
    def process_image(self, *args, **kwargs): raise AssertionError('No repeated image action')
noop = lambda *_: None
runner = TaskRunner(api, Boundary(RpaResult(ok=True, result_code='invite_sent', message='Controlled physical result')),
    on_profile=noop, on_status=noop, on_step=noop, on_task=noop, on_result=noop, on_error=errors.append)
before = {'ledger': storage.load_c2_ledger_entry(cid, key), 'outbox': storage.load_c2_outbox_entry(outbox_id) if outbox_id else None}
def snapshot():
    return {'ready': runner.fault_recovery_state(), 'ledger': storage.load_c2_ledger_entry(cid, key),
        'runtime': storage.load_runtime_control(), 'blockers': storage.update_install_business_blockers(),
        'outbox': storage.load_c2_outbox_entry(outbox_id) if outbox_id else None,
        'exchanges': exchanges, 'physical': physical, 'errors': errors}
runner.start(storage.load_binding())
try:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not runner.fault_recovery_state()['ready']:
        time.sleep(.1)
    destination.write_text(json.dumps({'before': before, 'after': snapshot()}, ensure_ascii=False, indent=2))
    assert runner.fault_recovery_state()['ready'], snapshot()
    assert storage.load_c2_ledger_entry(cid, key)['ingest_state'] == 'confirmed'
    assert not physical and storage.load_binding().run_status == 'faulted'
    recovered_before_start = snapshot()
    if outbox_id:
        assert storage.load_c2_outbox_entry(outbox_id)['payload'] == payload
    if request.get('stop_after_recovery_checks'):
        # A valid A may naturally have a reply ahead of the C1 task for B.
        # This case measures only recovery; never cancel A to force B first.
        sys.exit(0)
    clicked = True
    assert runner.set_run_status('running'), errors
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if physical and not storage.load_runtime_control().get('inflight_flow_id'): break
        time.sleep(.1)
    assert physical == [request['next_task_id']], snapshot()
    assert not storage.load_runtime_control().get('inflight_flow_id'), snapshot()
    destination.write_text(json.dumps({'before': before, 'recovered_before_start': recovered_before_start,
                                     'after': snapshot(), 'next_customer_completed': True},
                                     ensure_ascii=False, indent=2))
finally:
    runner.stop_for_update(timeout_seconds=5)
