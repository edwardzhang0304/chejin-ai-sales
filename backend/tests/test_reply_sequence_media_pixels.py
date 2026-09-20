"""Native media + real OCR + Worker + HTTP + PostgreSQL + SQLite.

OS input/screens and external models are controlled. The production C2 entry
must generate, cancel, read media, regenerate, send and settle without helpers
creating tasks, receipts or manually finishing a Flow.
"""
import json
import os
import ast
import copy
from pathlib import Path

import pytest
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
import test_c3_api as fixtures
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, SentAck, HandoffEvent
from app.models.worker import Worker
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from chejin_worker_client import task_runner, storage, rpa_bridge, omniauto_vision
from chejin_worker_client.api import WorkerApiClient
from reply_sequence_heartbeat import live_test_worker_heartbeat
from chejin_worker_client.models import Binding
from chejin_worker_client.ui_lock import lock_summary
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from reply_sequence_media_desktop import (
    fixture, MediaDesktop, media_frames, install_native_image_probe,
    OLD_REPLY, NEW_REPLY, TRANSCRIPT, IMAGE_SUMMARY,
)


@pytest.mark.parametrize('kind', ['voice', 'image'])
def test_typing_media_interrupts_sequence_and_finishes(
    http_api, monkeypatch, async_generation, tmp_path, kind,
):
    assert fixture.sidecar is sidecar
    assert Path(sidecar.__file__).is_relative_to(Path(task_runner.__file__).parents[1] / 'omniauto-rpa')
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path / 'worker')
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker/worker_client.sqlite3')
    monkeypatch.setattr(fixtures, 'client', http_api)
    ablation = os.environ.get('CHEJIN_SEQUENCE_MEDIA_ABLATION', '')
    if ablation in {'voice_marker', 'image_pending'}:
        # Explicit private original-source replay, not a fabricated media result.
        original = Path(os.environ['CHEJIN_SEQUENCE_ABLATION_SOURCE'])
        tree = ast.parse(original.read_text(encoding='utf-8'))
        name = 'combined_voice_transcript_anchor_match_evidence' if kind == 'voice' else '_normalize_one_image_slot_result'
        node = copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name))
        node.decorator_list = []
        module = sidecar if kind == 'voice' else task_runner
        namespace = dict(vars(module))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(original), 'exec'), namespace)
        if kind == 'voice': monkeypatch.setattr(sidecar, name, namespace[name])
        else: monkeypatch.setattr(task_runner.TaskRunner, name, staticmethod(namespace[name]))
    calibration, frames = media_frames(kind)
    desktop = MediaDesktop(monkeypatch, tmp_path / 'desktop', calibration, frames, kind=kind)
    image_calls = install_native_image_probe(monkeypatch, desktop, tmp_path) if kind == 'image' else []
    monkeypatch.setattr(sidecar, 'human_window_image_click_in_bounds', desktop.voice_click)
    monkeypatch.setattr(sidecar, 'human_window_image_right_click_in_bounds', desktop.right_click)
    monkeypatch.setattr(sidecar, 'observe_wechat_context_menu', desktop.observe_menu)
    monkeypatch.setattr(sidecar, 'wait_for_wechat_context_menu_stable', lambda: 0)
    monkeypatch.setattr(sidecar, 'capture_wechat_window_visible_screen', desktop.capture)
    monkeypatch.setattr(sidecar.win32gui, 'IsWindow', lambda hwnd: desktop.menu_open if hwnd == 90001 else True, raising=False)
    monkeypatch.setattr(sidecar.win32gui, 'IsWindowVisible', lambda hwnd: desktop.menu_open if hwnd == 90001 else True, raising=False)
    monkeypatch.setattr(sidecar, '_WIN32_IMPORT_ERROR', '')
    monkeypatch.setattr(sidecar, 'configure_dpi_awareness', lambda: None)
    monkeypatch.setattr(sidecar, 'activate_window', lambda hwnd: None)
    window = {'hwnd': calibration['hwnd'], 'pid': 2188, 'class_name': 'WeChatMainWndForPC', 'visible': True}
    monkeypatch.setattr(sidecar, 'ensure_visible_wechat_window', lambda **kw: {'visible_main_windows': [window]})
    expected_fact = TRANSCRIPT if kind == 'voice' else IMAGE_SUMMARY
    model_calls = []

    class Model:
        def generate_reply_decision(self, **kwargs):
            model_calls.append(kwargs)
            # Media content, not invocation count, selects the new answer.
            if any(message.get('content') == expected_fact
                   for message in kwargs['message_batch']['messages']
                   if message.get('sender_role') == 'customer'):
                desktop.reply = NEW_REPLY
                parts = [NEW_REPLY]
            else:
                parts = [OLD_REPLY, '资料需要按实际情况核对，' * 8 + '请稍等。', '看车时间由销售进一步确认，' * 8]
                if ablation == 'generation': async_generation['suppress'] = True
            return AIEngineDecision(decision='send_reply', reply_text=' '.join(parts), guard_result='pass',
                raw_payload={'omniauto_brain_result': {'brain_plan': {'reply_segments': parts}}})

    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', Model)
    worker = fixtures._create_worker(); fixtures._create_sales(worker['id'])
    fixtures._create_lead(remark_code='CJMKZUTH'); session = fixtures._scan(worker, remark_code='CJMKZUTH')
    with SessionLocal() as db:
        conv = db.get(Conversation, session['conversation_id'])
        conv.friend_state = 'friend_active'; conv.status = 'waiting_user_reply'
        db.get(Worker, worker['id']).local_lock_summary = {'capabilities': {'reply_sequence_version': 1}}
        db.commit()
    api = WorkerApiClient(http_api.get('/healthz').url.removesuffix('/healthz') + '/api')
    binding = Binding(worker['id'], worker['worker_token'], 'client-c3', run_status='running')
    storage.save_binding(binding); api.set_run_status(binding, 'running')
    wire, sends, voice_calls, ocr_calls, binding_checks = [], [], [], [], []
    raw_send = api.session.send
    def observed_http(request, **kwargs):
        response = raw_send(request, **kwargs)
        wire.append({'path': request.url.split('/api')[-1], 'status': response.status_code})
        return response
    monkeypatch.setattr(api.session, 'send', observed_http)
    native_ocr = sidecar.run_ocr
    def observed_ocr(image, **kwargs):
        value = native_ocr(image, **kwargs)
        ocr_calls.append({'size': list(image.size), 'rows': len(value), 'capture': len(desktop.captures) - 1})
        return value
    monkeypatch.setattr(sidecar, 'run_ocr', observed_ocr)
    native_bind = sidecar._bind_voice_transcripts_for_action
    def observed_binding(messages, anchor, image_size, **kwargs):
        result = native_bind(messages, anchor, image_size, **kwargs)
        for message in messages:
            if message.get('content') == TRANSCRIPT:
                binding_checks.append({'anchor': anchor, 'transcript': message,
                    'decision': sidecar.combined_voice_transcript_anchor_match_evidence(message, anchor, image_size, after_messages=messages),
                    'bound_count': len(result)})
        return result
    monkeypatch.setattr(sidecar, '_bind_voice_transcripts_for_action', observed_binding)
    bridge = rpa_bridge.RpaBridge(); bridge.mode = 'real'
    def physical_io(args, **kwargs):
        def option(name, default=''): return args[args.index(name)+1] if name in args else default
        if args[0] == 'voice-transcribe':
            stage = option('--voice-action-stage')
            value = sidecar.run_sidecar_cli(args)
            voice_calls.append({'stage': stage, 'result': value})
            (tmp_path / f'voice-{len(voice_calls)}-{stage}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        elif args[0] == 'send':
            assert lock_summary()['locked']
            value = sidecar.send_payload(calibration['hwnd'], {}, target=option('--target'), text=option('--text'),
                exact=True, skip_send_rate_guard=True, artifact_dir=str(tmp_path/'desktop'),
                expected_context_guard=fixture.send_context_from_args(args), action_journal_path=option('--action-journal'))
            sends.append(value)
        else:
            assert args[0] in {'messages', 'open-chat'}, args
            value = sidecar.messages_payload(calibration['hwnd'], {'ok': True}, target='CJMKZUTH',
                history_load_times=0, max_scroll_steps=0, max_snapshots=1, confirm_target='CJMKZUTH', confirm_exact=True,
                chat_fact_roi_ocr='--chat-fact-roi-ocr' in args, expected_confirmed_self_text=option('--expected-confirmed-self-text'))
            if args[0] == 'open-chat':
                value = {'ok': True, 'guard': value['target_confirmation'], 'initial_messages_snapshot': value, 'state': 'chat_target_confirmed'}
        return json.loads(json.dumps(sidecar.sanitize_sidecar_contract_output(value)))
    monkeypatch.setattr(bridge, '_call_omniauto', physical_io)
    monkeypatch.setattr(bridge, 'prepare_startup_layout_for_new_transaction', lambda **kw: {'ok': True, 'layout_snapshot': calibration})
    monkeypatch.setattr(omniauto_vision, 'vision_configuration_status', lambda: {'ready': True})
    errors = []
    runner = task_runner.TaskRunner(api, bridge, on_profile=lambda _: None, on_status=lambda _: None,
        on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None, on_error=errors.append)
    runner.binding = binding
    target = next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id == session['conversation_id'])
    try:
        result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=True)
    finally:
        runner._stop_task_lease_guard()
    with SessionLocal() as db:
        actions = list(db.scalars(select(ReplyAction)))
        receipts = list(db.scalars(select(SentAck)))
        record = {'kind': kind, 'ablation': ablation, 'result': result, 'sends': sends, 'voice_calls': voice_calls,
            'voice_binding': binding_checks, 'voice_clicks': desktop.media_clicks, 'image_calls': image_calls,
            'ocr_calls': ocr_calls, 'model_requests': model_calls, 'background': async_generation['counts'],
            'wire': wire, 'captures': desktop.captures, 'keys': desktop.keys, 'enters': desktop.enter_texts,
            'actions': [{'text': a.reply_text, 'status': a.status, 'segment_count': a.segment_count} for a in actions],
            'receipts': [a.send_result for a in receipts], 'handoffs': len(list(db.scalars(select(HandoffEvent)))),
            'backend_flow': db.get(Worker, worker['id']).inflight_flow_state, 'runtime': storage.load_runtime_control(),
            'pending_ack': storage.has_pending_reply_send_ack_outbox(), 'lock': lock_summary(), 'errors': errors}
    (tmp_path/'media-chain.json').write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    # Assert the automatic chain before making any separate idempotence calls.
    assert desktop.enter_texts == [NEW_REPLY], record
    assert len(sends) == 2 and sends[0]['error_code'] == 'C3_CONTEXT_CHANGED_BEFORE_SEND', record
    assert sends[0]['action_phase'] == 'not_attempted' and sends[0]['guard']['visual']['draft_clear']['clear_attempted'] is True
    assert len(model_calls) == 2 and expected_fact in json.dumps(model_calls[-1], ensure_ascii=False, default=str)
    assert async_generation['counts'] == {'scheduled': 2, 'executed': 2, 'generated': 2}
    assert any(a.segment_count > 1 for a in actions) and [a.reply_text for a in actions if a.status == 'sent'] == [NEW_REPLY]
    assert sorted(record['receipts']) == ['failed', 'sent'] and record['handoffs'] == 0
    assert not record['backend_flow'] and not record['runtime']['inflight_flow_id'] and not record['pending_ack'] and not record['lock']['locked']
    assert not record['runtime']['pause_requested'] and runner.binding.run_status == 'running'
    if kind == 'voice':
        assert [v['stage'] for v in voice_calls] == ['prepare', 'execute'] and len(desktop.media_clicks) == 1
        assert binding_checks and all(c['decision']['accepted'] for c in binding_checks)
    else:
        assert len(image_calls) == 1
        pending = json.loads((tmp_path/'native-image.json').read_text())
        assert pending['business_state'] == 'confirmed_result_pending_continuity' and not pending['business_result_confirmed']
        assert json.loads((tmp_path/'image-continuity.json').read_text())
