"""Native C1 timing/cleanup regression; synthetic OS/OCR, no real input."""
import copy
from unittest.mock import Mock, patch

import pytest
from PIL import ImageDraw
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as flow
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import frames
from test_c1_evidence_chain import scene, original, home
from test_hook_search_cache_consumer import native_invite


def test_placeholder_edge_change_reuses_startup_bounds_without_pixel_relocation():
    frame=frames()['header'];bounds=flow.empty_search(frame)['bounds']
    for delta in (-1,1):
        changed=copy.deepcopy(frame)
        x=bounds[2]-1 if delta<0 else bounds[2]
        color=frame.image.getpixel((bounds[2]+3,bounds[1]+4)) if delta<0 else frame.image.getpixel((bounds[0]+4,bounds[1]+4))
        ImageDraw.Draw(changed.image).line((x,bounds[1],x,bounds[3]-1),fill=color)
        with patch.object(flow,'components',side_effect=AssertionError('repeated pixel geometry check')):
            assert flow.empty_search(changed,bounds)['bounds']==bounds
        changed.rows.append(dict(text='123',confidence=.99,left=bounds[0]+90,top=bounds[1]+4,right=bounds[0]+120,bottom=bounds[3]-4))
        with pytest.raises(flow.FlowStop,match='SEARCH_QUERY_REMAINS'):
            flow.empty_search(changed,bounds)


def test_cached_search_bounds_outside_frame_are_not_reused():
    frame=frames()['header']
    with pytest.raises(flow.FlowStop,match='SEARCH_STARTUP_LAYOUT_INVALIDATED'):
        flow.empty_search(frame,[-1,0,frame.image.width,frame.image.height])


@pytest.mark.parametrize('mode',['slow_positions','stale_final_shortcode'])
def test_native_full_invite_separates_cached_positions_from_fresh_content(native_invite,monkeypatch,mode):
    s,physical=native_invite;clock=[1000.0];o=s.ops;ocr=o.run_ocr.side_effect
    monkeypatch.setattr(flow.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(flow.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    o.humanized_action_sleep.side_effect=lambda low,high:clock.__setitem__(0,clock[0]+high/1000)
    o.add_friend_paced_pause.side_effect=lambda tier,**kw:clock.__setitem__(0,clock[0]+(3.5 if tier=='input' else .35))
    def slow_ocr(image):
        if image.info['scene_label']=='fields':
            if physical['fields']==0:clock[0]+=18  # position OCR only
            elif mode=='stale_final_shortcode':clock[0]+=16
        return ocr(image)
    o.run_ocr.side_effect=slow_ocr
    result=s.execute(monkeypatch)
    if mode=='slow_positions':
        assert result['status']=='submitted_pending_recipient',result
        assert result['cleanup']=='search_empty_verified'
        assert physical['clicks']==6 and o.clipboard_copy.call_count==2
        assert s.grabs==['header','lookup','card','fields','fields','footer','header']
        saved=(original(s)/'submit-attempt.json').read_bytes()
        with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
        assert (original(s)/'submit-attempt.json').read_bytes()==saved
        assert physical['clicks']==6
    else:
        assert result['error_code']=='STALE_ROI_FRAME'
        assert not result['submit_attempted']
        assert not (original(s)/'submit-attempt.json').exists()
    assert clock[0]-1000>39


def test_dialog_observation_budget_starts_after_configured_pause():
    from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import NativeGuardTests
    case=NativeGuardTests();case.setUp()
    try:
        case.ops.add_friend_paced_pause.side_effect=lambda *a,**kw:flow.time.sleep(6)
        elapsed=case.select_dialog_samples([dict(width=450,height=780)])
        assert elapsed==pytest.approx(6.15)
        assert case.ui.current==33
        case.ops.win32api.mouse_event.assert_not_called()
    finally:case.tearDown()


def test_initialization_reads_dpi_awareness_once(tmp_path):
    from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import NativeGuardTests
    case=NativeGuardTests();case.setUp();o=case.ops
    try:
        o.ensure_dpi_awareness_status=Mock(return_value={'per_monitor_aware':True})
        o.window_dpi_scale=lambda h:1
        with patch('apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1.classify_visible_other_windows',return_value={'blocking_window_details':[]}):
            flow.NativeUI(o,11,99,tmp_path/'artifacts',Mock(),activate_on_init=False)
        o.ensure_dpi_awareness_status.assert_called_once()
    finally:case.tearDown()
