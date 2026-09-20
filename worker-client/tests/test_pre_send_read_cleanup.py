"""Real Sidecar -> recovery -> SQLite; OS controlled, derived pixels, real OCR."""
import hashlib
import json
from types import SimpleNamespace

import pytest
import dynamic_composer_desktop as fixture
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import ReadCallFailed, validate_proof
from chejin_worker_client import storage, pre_send_read_recovery as recovery
from chejin_worker_client.action_journal import initialize_action_journal


@pytest.mark.parametrize('scenario', ['normal', 'hint', 'remaining', 'replacement_fails',
    'twice', 'clear_error', 'foreign_draft', 'wrong_action', 'crash_recheck',
    'reused_frame', 'stale_capture', 'missing_capture', 'post_reused_frame', 'post_stale_capture'])
def test_once_only_clear_read_retry(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path/'worker')
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker/client.sqlite3')
    storage.begin_runtime_flow('test-flow', 'chat_reply')
    text='好的，我帮您看看'
    calibration, frames=fixture.derived_frames(reply=text, movement=0, reduction=0)
    desktop=fixture.Desktop(monkeypatch, tmp_path/'desktop', calibration, frames, reply=text)
    if scenario=='hint':
        from PIL import ImageDraw, ImageFont
        font=ImageFont.truetype('/System/Library/Fonts/STHeiti Light.ttc', 14)
        for frame in (frames['before'],frames['sent']):
            ImageDraw.Draw(frame).text((320,712),'按住鼠标 语音输入文字',font=font,fill=(159,159,166))
    key=desktop.key;cleanup_calls=[];in_cleanup=[]
    original_cleanup=sidecar.clear_confirmed_program_draft
    def cleanup(*args,**kwargs):
        cleanup_calls.append(True);in_cleanup.append(True)
        try:return original_cleanup(*args,**kwargs)
        finally:in_cleanup.pop()
    monkeypatch.setattr(sidecar,'clear_confirmed_program_draft',cleanup)
    def keyboard(value):
        if in_cleanup and value==8 and scenario=='clear_error':raise OSError('controlled Backspace failure')
        if (in_cleanup and value==8 and scenario in {'remaining','replacement_fails','reused_frame','stale_capture','missing_capture'}) or (injected and value==46 and scenario=='replacement_fails'):
            desktop.keys.append(value);desktop.selected=False;return
        key(value)
    monkeypatch.setattr(sidecar,'key_press',keyboard)
    build=sidecar.build_send_fact_snapshot_from_frame
    injected=[];baseline_frames=[]
    native_capture=sidecar.capture_send_fact_snapshot
    def capture(*args,**kwargs):
        value=native_capture(*args,**kwargs)
        if kwargs.get('label')=='send_baseline':baseline_frames.append(value['frame_observation'])
        if kwargs.get('label')=='send_post_guard_and_result_confirm_1':
            frame=value['frame_observation']
            if scenario=='post_reused_frame':frame['frame_id']=baseline_frames[-1]['frame_id']
            if scenario=='post_stale_capture':
                frame.update(captured_monotonic=baseline_frames[-1]['captured_monotonic'],
                             screenshot_sha256=baseline_frames[-1]['screenshot_sha256'])
        return value
    monkeypatch.setattr(sidecar,'capture_send_fact_snapshot',capture)
    def read(*args,**kwargs):
        if kwargs.get('label')=='send_pre_trigger_context_reused' and (not injected or scenario=='twice'):
            injected.append(kwargs['label'])
            if scenario=='foreign_draft':desktop.draft='not this program draft'
            raise ReadCallFailed(operation='read',reason='controlled transient read failure')
        value=build(*args,**kwargs)
        if kwargs.get('label')=='send_pre_trigger_context_reused' and len(baseline_frames)==2:
            frame=value['frame_observation']
            if scenario=='reused_frame':frame['frame_id']=baseline_frames[-1]['frame_id']
            if scenario=='stale_capture':frame['captured_monotonic']=baseline_frames[-1]['captured_monotonic']
            if scenario=='missing_capture':frame.pop('captured_monotonic',None)
        return value
    monkeypatch.setattr(sidecar,'build_send_fact_snapshot_from_frame',read)
    guard=desktop.expected_guard();journal=tmp_path/'action.json'
    initialize_action_journal(journal,action_kind='send',transaction_id='test-action',
        conversation_id='synthetic',items=[{'journal_item_id':'test-action'}])
    calls=[];states=[]
    def send():
        if calls and scenario=='crash_recheck':raise RuntimeError('controlled interruption after durable retry start')
        value=sidecar.send_payload(calibration['hwnd'],{},target='CJMKZUTH',text=text,exact=True,
            skip_send_rate_guard=True,artifact_dir=str(tmp_path/'desktop'),
            expected_context_guard=guard,action_journal_path=str(journal))
        if scenario=='wrong_action' and 'program_draft_cleanup' in value.get('pre_send_read_failure_fact',{}):
            value['pre_send_read_failure_fact']['program_draft_cleanup']['reply_action_id']='another-action'
        calls.append(value);return value
    runner=SimpleNamespace(set_run_status=lambda value:states.append(value))
    claim=SimpleNamespace(raw={},reply_action_id='test-action',task_id='test-task',reply_text_hash=hashlib.sha256(text.encode()).hexdigest())
    target=SimpleNamespace(conversation_id='synthetic',authorization_revision='test-auth',remark_code='CJMKZUTH')
    if scenario in {'crash_recheck','wrong_action'}:
        error,match=(RuntimeError,'controlled interruption') if scenario=='crash_recheck' else (ValueError,'PROOF_INVALID')
        with pytest.raises(error,match=match):
            recovery.send_with_recheck(runner,None,target=target,claim=claim,send=send)
        record=recovery.pending_records(include_current=True)[0]
        if scenario=='crash_recheck':assert record['send_in_progress'] is True
        assert recovery._retain_input_requirement(record)['input_safety']['status']=='pending'
        result={}
    else:result=recovery.send_with_recheck(runner,None,target=target,claim=claim,send=send)
    evidence={'scenario':scenario,'calls':calls,'states':states,'result':result,'enters':desktop.enter_count,
        'draft':desktop.draft,'keys':desktop.keys,'captures':desktop.captures,
        'pending_input':recovery.input_pending_records(),'records':recovery.pending_records(include_current=True)}
    (tmp_path/'result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert injected
    assert all(x['label']!='send_program_draft_cleanup' for x in desktop.captures)
    first=calls[0]['pre_send_read_failure_fact'];assert first['input_state']=='unverified'
    if scenario in {'normal','hint','remaining'}:
        assert len(calls)==2 and desktop.enter_count==1 and result['ok']
        assert states==[] and not recovery.input_pending_records()
        assert first['program_draft_cleanup']['cleanup']['cleared'] is False
        assert len(cleanup_calls)==1
    elif scenario=='twice':
        assert len(calls)==2 and desktop.enter_count==0 and states==['faulted']
        assert validate_proof(result['pre_send_read_failure'])['outcome']=='exhausted'
        assert not recovery.input_pending_records()
        recovery.mark_settled('test-action')
        assert not recovery.input_pending_records()
        monkeypatch.setattr(recovery,'BOOT_ID','restarted')
        assert not recovery.pending_records()
    elif scenario in {'post_reused_frame','post_stale_capture'}:
        assert len(calls)==2 and desktop.enter_count==1
        assert result['error_code']=='SEND_RESULT_UNKNOWN' and not result['ok']
    else:
        assert desktop.enter_count==0
        if scenario in {'clear_error','foreign_draft','wrong_action'}:
            assert len(calls)==1 and states==['faulted']
            if scenario!='wrong_action':assert recovery.input_pending_records()
        if scenario=='replacement_fails':
            assert len(calls)==2 and not result['ok']
            assert 'focused_input_draft_mismatch' in str(result)
        if scenario in {'reused_frame','stale_capture','missing_capture'}:
            assert len(calls)==2 and result['error_code']=='C3_SEND_FRAME_TIMEPOINT_INVALID'
