"""Native planning -> approved monitor payload -> actual Qt placement methods.

Only Win32/Qt surfaces are substituted; dimensions never come from fixtures.
"""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
import test_web_ui_binding_behavior as ui_fixture
from chejin_worker_client.hook_process import _approved_window_geometry
from chejin_worker_client.hook_worker_services import HookWorkerServices
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as ops
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1


@pytest.mark.parametrize('scale,width,height,origin', [
    (1,1920,1040,(0,0)), (1.25,1920,1040,(0,0)),
    (1.5,2560,1400,(1920,0)), (2,3840,2080,(-3840,-200)),
    (1.25,1366,728,(-1366,20)), (1,1366,728,(0,0)),
])
def test_actual_preparation_and_qt_share_scale(monkeypatch,tmp_path,scale,width,height,origin):
    x,y=origin
    rect={'left':x+50,'top':y+50,'width':800,'height':700,'right':x+850,'bottom':y+750}
    def move(_hwnd,left,top,w,h,_repaint):
        rect.update(left=left,top=top,width=w,height=h,right=left+w,bottom=top+h)
    gui=NS(IsWindowVisible=lambda _:True,IsIconic=lambda _:False,
        GetClassName=lambda _:'WeChatMainWndForPC',GetForegroundWindow=lambda:11,
        GetWindow=lambda *_:0,EnumWindows=lambda cb,arg:cb(11,arg),IsZoomed=lambda _:False,
        MoveWindow=Mock(side_effect=move),GetWindowRect=lambda _:tuple(rect[k] for k in ('left','top','right','bottom')))
    owners=NS(GetWindowThreadProcessId=lambda _:(1,99))
    for name,value in dict(win32gui=gui,win32process=owners,
        get_window_geometry=lambda _:dict(rect),
        get_window_client_geometry=lambda _:{'width':rect['width']-16,'height':rect['height']-30},
        window_dpi_scale=lambda _:scale,
        screen_work_area=lambda _:{'left':x,'top':y,'width':width,'height':height},
        ensure_dpi_awareness_status=lambda:{'per_monitor_aware':True},
        invalidate_layout_snapshot=Mock(),humanized_action_sleep=Mock(),activate_window=Mock(),
        capture_wechat=Mock(side_effect=AssertionError('no new screenshots'))).items():
        monkeypatch.setattr(ops,name,value)
    prepared=hook_c1.prepare_window(ops,11,99,artifact_dir=str(tmp_path),capture_evidence=False)
    assert prepared['ok'],prepared
    assert prepared['click_actions']==prepared['input_actions']==prepared['screenshot_call_count']==0
    process=NS(pid=99,started_at=12.5,exe=r'C:\fixture\WeChat.exe')
    live=NS(is_running=lambda:True,create_time=lambda:12.5,exe=lambda:process.exe)
    user32=NS(SetThreadDpiAwarenessContext=Mock(return_value=-3),GetDpiForWindow=lambda _:round(96*scale))
    payload=_approved_window_geometry(process,live,gui,owners,user32,monitor_info=lambda _:{
        'Device':'DISPLAY2','Monitor':(x,y,x+width,y+height),'Work':(x,y,x+width,y+height)})
    assert payload['monitor']['dpi_scale']==scale
    with ui_fixture._headless_web_ui_module() as module:
        window=ui_fixture.WebUiBindingBehaviorTest._window(module,None)
        service=HookWorkerServices(NS(on_persistence_failure=None),None,intake_interval_seconds=1,projection_batch_size=10)
        service._startup_position_payload=payload
        window.runner.hook_services=service
        first=service.startup_window_position_payload()
        first['monitor']['bounds'][0]=99999
        assert service.startup_window_position_payload()['monitor']['bounds'][0]==x
        # Deliberately different Qt origin: globally dividing native coordinates is wrong.
        qx,qy=(-1700,80) if x<0 else (x,y)
        area=NS(x=lambda:qx,y=lambda:qy,width=lambda:round(width/scale),height=lambda:round(height/scale))
        screen=NS(name=lambda:'DISPLAY2',availableGeometryChanged=Mock(),availableGeometry=lambda:area,
                  geometry=lambda:area,devicePixelRatio=lambda:scale)
        module.QGuiApplication=NS(screens=lambda:[screen],screenAt=Mock(side_effect=AssertionError('native coordinates are not Qt coordinates')))
        window.move=Mock();window.rpa_bridge=object()
        window._position_next_to_wechat_once()
        wx,wy=window.move.call_args.args
        assert abs(window.height()*scale/rect['height'] - 720/740)<.003
        assert abs(window.width()*scale/rect['width'] - 380/1060)<.003
        assert wy==qy+round((rect['top']-y)/scale)
        assert wx>=qx+round((rect['right']-x)/scale)+12
        assert wx+window.width()<=qx+area.width()
        assert wy+window.height()<=qy+area.height()
        if scale==1.25 and width==1920:
            assert (rect['width'],rect['height'])==(1325,925)
            assert (window.width(),window.height())==(380,720)
        calls=window.move.call_count
        window._position_next_to_wechat_once()
        assert window.move.call_count==calls
        assert window._startup_position_attempted
    ops.capture_wechat.assert_not_called()


@pytest.mark.parametrize('dpi',[0,float('nan'),float('inf'),500])
def test_invalid_native_monitor_dpi_never_reaches_qt(dpi):
    import test_hook_window_position as fixture
    values=fixture.fake_windows()
    values[-1].GetDpiForWindow=lambda _:dpi
    assert _approved_window_geometry(*values,monitor_info=lambda _:{
        'Device':'DISPLAY1','Monitor':(0,0,1920,1080),'Work':(0,0,1920,1040)}) is None
