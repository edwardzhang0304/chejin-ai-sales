"""Independent regression: real layout registration, controlled OS and OCR I/O.

Missing composer boundaries are an observed product scenario. No customer text
or OCR decision is fabricated to obtain a successful business reply.
"""
import json
from pathlib import Path
import sys
import pytest
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s

# Reuse the installed Sidecar's layout fixtures in both host/shared test runs.
sys.path.insert(0, str(Path(s.__file__).resolve().parents[1] / 'tests'))
from test_input_boundary_gap import calibration, frame
from test_dynamic_composer import install_desktop, register


@pytest.mark.parametrize('roi',[False,True],ids=['full','roi'])
def test_layout_rejection_is_not_an_ocr_io_failure(monkeypatch,tmp_path,roi):
    c=calibration()
    install_desktop(monkeypatch,tmp_path,c)
    raw=frame(boundary=False)
    snapshot=register(raw,c)
    assert not snapshot['valid']
    assert s.navigation_layout_snapshot_for_image(raw)['valid']
    calls=[]
    monkeypatch.setattr(s,'capture_wechat',lambda *a,**kw:(raw,'controlled-no-boundary.png'))
    monkeypatch.setattr(s,'run_ocr',lambda image,**kwargs:calls.append('ocr') or [])
    monkeypatch.setenv('CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED','1')
    try:
        result=s.messages_payload(c['hwnd'],{},target='CJTEST01',history_load_times=0,chat_fact_roi_ocr=roi)
    except Exception as exc:
        result=s.exception_payload_for_sidecar(exc)
    (tmp_path/'result.json').write_text(json.dumps({'roi':roi,'result':result,'ocr_calls':len(calls),'layout':snapshot},indent=2))
    assert result['error_code']=='C2_AVATAR_EVIDENCE_INVALID',result
    assert 'read_call_failure' not in result,result


@pytest.mark.parametrize('roi', [False, True], ids=['full', 'roi'])
def test_real_ocr_io_exception_still_uses_read_failure(monkeypatch,tmp_path,roi):
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    raw=frame();assert register(raw,c)['valid']
    monkeypatch.setattr(s,'capture_wechat',lambda *a,**kw:(raw,'controlled-valid-layout.png'))
    def fail(image):raise OSError('controlled actual OCR I/O failure')
    monkeypatch.setattr(s,'run_ocr',fail)
    monkeypatch.setenv('CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED','1')
    result=s.messages_payload(c['hwnd'],{},target='CJTEST01',history_load_times=0,chat_fact_roi_ocr=roi)
    assert result['error_code']=='MESSAGE_READ_FAILED',result
    assert result['read_call_failure']['call_status']=='failed'


@pytest.mark.parametrize('roi',[False,True],ids=['full','roi'])
def test_send_layout_rejection_does_not_become_retryable_read_failure(monkeypatch,tmp_path,roi):
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    raw=frame(boundary=False);assert not register(raw,c)['valid']
    monkeypatch.setattr(s,'run_ocr',lambda image:[])
    monkeypatch.setenv('CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED','1' if roi else '0')
    with pytest.raises(Exception) as caught:
        s.build_send_fact_snapshot_from_frame(c['hwnd'],target='CJTEST01',text='test',exact=True,
            artifact_dir=None,label='before_input',screenshot=raw,screenshot_path='controlled-no-boundary.png')
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import ReadCallFailed
    (tmp_path/'result.json').write_text(json.dumps({'roi':roi,'exception':type(caught.value).__name__,
        'message':str(caught.value),'retryable_io':isinstance(caught.value,ReadCallFailed)},indent=2))
    assert isinstance(caught.value,s.frame_avatars.AvatarEvidenceError),repr(caught.value)
    assert not isinstance(caught.value,ReadCallFailed)


@pytest.mark.parametrize('roi', [False, True], ids=['full', 'roi'])
def test_uncertified_cached_rows_keep_layout_error(monkeypatch,tmp_path,roi):
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    raw=frame(boundary=False);assert not register(raw,c)['valid']
    calls=[]
    monkeypatch.setattr(s,'run_ocr',lambda image,**kwargs:calls.append('ocr') or [])
    monkeypatch.setenv('CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED','1' if roi else '0')
    with pytest.raises(s.frame_avatars.AvatarEvidenceError):
        s.build_send_fact_snapshot_from_frame(c['hwnd'],target='CJTEST01',text='test',exact=True,
            artifact_dir=None,label='before_input',screenshot=raw,
            screenshot_path='controlled-no-boundary.png',ocr_items=[])
    assert not calls


@pytest.mark.parametrize('kind', ['avatar', 'layout'])
def test_roi_confirmation_fallback_preserves_semantic_exception(monkeypatch,tmp_path,kind):
    """Controlled admission error on the second OCR plan; no Windows claim."""
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    raw=frame();assert register(raw,c)['valid']
    monkeypatch.setenv('CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED','1')
    calls=[]
    monkeypatch.setattr(s,'run_ocr',lambda image,**kwargs:calls.append('ocr') or [])
    monkeypatch.setattr(s,'validate_active_send_target',lambda *a,**kw:{'ok':False})
    prepare=s.avatar_text_input.prepare
    failure=(s.frame_avatars.AvatarEvidenceError({'reason':'controlled_fallback_admission'})
        if kind=='avatar' else s.win32_ocr_layout.LayoutSnapshotError('controlled_fallback_admission'))
    prepares=[]
    def reject_second(image,snapshot):
        prepares.append(image)
        if len(prepares)==2:
            raise failure
        return prepare(image,snapshot)
    monkeypatch.setattr(s.avatar_text_input,'prepare',reject_second)
    with pytest.raises(type(failure)) as caught:
        s.build_send_fact_snapshot_from_frame(c['hwnd'],target='CJTEST01',text='test',exact=True,
            artifact_dir=None,label='before_input',screenshot=raw,screenshot_path='controlled-valid-layout.png')
    assert caught.value is failure
    assert len(prepares)==2 and all(image is raw for image in prepares)
    assert len(calls)==3  # Only the admitted ROI plan ran OCR; no second capture.


@pytest.mark.parametrize('route', ['full', 'roi', 'supplied', 'fallback'])
def test_send_ocr_io_failure_keeps_existing_proof(monkeypatch,tmp_path,route):
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import ReadCallFailed
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    raw=frame();assert register(raw,c)['valid']
    monkeypatch.setenv('CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED','0' if route=='full' else '1')
    monkeypatch.setattr(s,'validate_active_send_target',lambda *a,**kw:{'ok':False})
    calls=[]
    failure=OSError('controlled actual OCR I/O failure')
    def ocr(image, **kwargs):
        calls.append(image)
        if route=='fallback' and len(calls)<=3:
            return []
        raise failure
    monkeypatch.setattr(s,'run_ocr',ocr)
    with pytest.raises(ReadCallFailed) as caught:
        s.build_send_fact_snapshot_from_frame(c['hwnd'],target='CJTEST01',text='test',exact=True,
            artifact_dir=None,label='before_input',screenshot=raw,screenshot_path='controlled-valid-layout.png',
            ocr_items=[] if route=='supplied' else None)
    assert caught.value.__cause__ is failure
    assert caught.value.evidence['operation']=='read'
    assert caught.value.evidence['call_status']=='failed'
    assert len(calls)==(4 if route=='fallback' else 1)


def test_capture_failure_keeps_existing_proof(monkeypatch,tmp_path):
    c=calibration();install_desktop(monkeypatch,tmp_path,c)
    def fail(*args,**kwargs):
        raise OSError('controlled actual capture I/O failure')
    monkeypatch.setattr(s,'capture_wechat',fail)
    result=s.messages_payload(c['hwnd'],{},target='CJTEST01',history_load_times=0)
    assert result['error_code']=='MESSAGE_READ_FAILED'
    assert result['read_call_failure']['operation']=='capture'
