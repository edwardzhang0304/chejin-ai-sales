"""Real startup/cache/ROI adapter, synthetic Win32/OCR, no physical input."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import pytest
from PIL import Image
from chejin_worker_client.hook_factory import WindowsC1
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1, hook_c1_flow as flow
from apps.wechat_ai_customer_service.tests import test_hook_c1_flow as flow_tests
from apps.wechat_ai_customer_service.tests.test_search_control_icons import header


@pytest.fixture
def calibration(tmp_path):
    scene=flow_tests.NativeGuardTests();scene.setUp()
    ops=scene.ops
    ops.ensure_dpi_awareness_status=lambda:{'per_monitor_aware':True}
    ops.window_dpi_scale=lambda h:1
    ops.activate_window=Mock()
    state={'frame':header()}
    ops.try_image_grab=Mock(side_effect=lambda box:state['frame'].image.crop((0,0,box[2]-box[0],box[3]-box[1])))
    ops.run_ocr=Mock(side_effect=lambda im:state['frame'].rows)
    native=WindowsC1(tmp_path/'transactions'/'hook',tmp_path/'artifacts')
    native._window=Mock(return_value=(ops,11))
    gateway=SimpleNamespace(_locked=Mock(return_value=nullcontext(SimpleNamespace(raise_if_lost=Mock()))),_history_identity=Mock())
    yield native,gateway,SimpleNamespace(pid=99),state,ops,tmp_path
    scene.tearDown()


@pytest.mark.parametrize('query',['','13800000003'])
def test_startup_captures_once_no_clear_and_task_reuses_cached_position(calibration,monkeypatch,query):
    native,gateway,process,state,ops,root=calibration
    state['frame']=header(query=query)
    monkeypatch.setattr(hook_c1,'prepare_window',Mock(return_value={'ok':True}))
    native.startup(process,gateway,{})
    assert ops.try_image_grab.call_count==1 and ops.run_ocr.call_count==1
    assert native._search_control_cache['bounds']==[66,25,256,50]
    assert native._search_control_cache['key']['pid']==99
    ops.win32api.SetCursorPos.assert_not_called();ops.win32api.mouse_event.assert_not_called()
    reports=list((root/'artifacts'/'tasks').glob('startup-search-*/diagnostics/search-layout.json'))
    assert len(reports)==1 and json.loads(reports[0].read_text())['read_only']
    state['frame']=header()
    ui=native.ui(process,{'task_id':'synthetic-task'},Mock())
    with patch.object(flow,'search_control_bounds',side_effect=AssertionError('position must come from startup')):
        frame,target=ui.verified_empty_search()
    assert target['bounds']==[66,25,256,50]
    assert ops.try_image_grab.call_count==2  # current content proof, no relocation capture
    ops.win32api.mouse_event.assert_not_called()


def test_startup_unrecognized_frame_stops_without_stale_cache_or_input(calibration,monkeypatch):
    native,gateway,process,state,ops,root=calibration
    native._search_control_cache['bounds']=[1,2,3,4]
    state['frame'].image=Image.new('RGB',state['frame'].image.size,'white')
    monkeypatch.setattr(hook_c1,'prepare_window',Mock(return_value={'ok':True}))
    with pytest.raises(flow.FlowStop):native.startup(process,gateway,{})
    assert native._search_control_cache=={}
    ops.win32api.mouse_event.assert_not_called()
    assert list((root/'artifacts'/'tasks').glob('startup-search-*/diagnostics/initialization.json'))


@pytest.mark.parametrize('change',['missing','missing_bounds','corrupt_bounds','origin','geometry','hwnd','pid','dpi'])
def test_task_invalidated_startup_cache_stops_without_relocation_or_input(calibration,monkeypatch,change):
    native,gateway,process,state,ops,root=calibration
    monkeypatch.setattr(hook_c1,'prepare_window',Mock(return_value={'ok':True}))
    native.startup(process,gateway,{})
    ui=native.ui(process,{'task_id':'invalidated-layout'},Mock())
    frame=ui.capture('header')
    if change=='missing':native._search_control_cache.clear()
    elif change=='missing_bounds':native._search_control_cache.pop('bounds')
    elif change=='corrupt_bounds':native._search_control_cache['bounds']=[66,25,66,50]
    elif change=='origin':frame.origin=(200,0)
    elif change=='geometry':frame.geometry={**frame.geometry,'width':999}
    elif change=='hwnd':frame.hwnd=12
    elif change=='pid':ui.pid=100
    elif change=='dpi':ui.dpi=1.25
    with patch.object(flow,'search_control_bounds',side_effect=AssertionError('task must not relocate')):
        with pytest.raises(flow.FlowStop,match='SEARCH_STARTUP_LAYOUT_INVALIDATED'):
            ui.verified_empty_search(frame)
        with patch.object(ui,'capture',return_value=frame), patch.object(ui,'return_to_main'):
            with pytest.raises(flow.FlowStop,match='SEARCH_STARTUP_LAYOUT_INVALIDATED'):
                ui.clear_search()
    assert not native._search_control_cache
    ops.win32api.SetCursorPos.assert_not_called();ops.win32api.mouse_event.assert_not_called()
    # The same startup entry can re-establish a valid layout without input.
    native.startup(process,gateway,{})
    fresh=native.ui(process,{'task_id':'after-startup-recheck'},Mock())
    with patch.object(flow,'search_control_bounds',side_effect=AssertionError('must reuse refreshed startup')):
        fresh.verified_empty_search()
