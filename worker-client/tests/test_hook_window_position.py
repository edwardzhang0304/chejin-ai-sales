"""Optional Hook window metadata feeds original once-only Worker positioning."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from chejin_worker_client.hook_process import _approved_window_geometry
from chejin_worker_client.hook_worker_services import HookWorkerServices
from test_web_ui_binding_behavior import _headless_web_ui_module, WebUiBindingBehaviorTest


def fake_windows(handles=(1,)):
    process=SimpleNamespace(pid=100,started_at=12.5,exe=r'C:\HookLab\WeChat\WeChat.exe')
    live=SimpleNamespace(is_running=lambda:True,create_time=lambda:12.5,exe=lambda:process.exe)
    gui=SimpleNamespace(IsWindowVisible=lambda _:True,IsIconic=lambda _:False,
        GetClassName=lambda _:'WeChatMainWndForPC',
        EnumWindows=lambda f,p:[f(h,p) for h in handles],GetWindowRect=lambda _:(15,15,1075,755))
    owners=SimpleNamespace(GetWindowThreadProcessId=lambda _:(1,100))
    dpi=Mock(return_value=-3)
    return process,live,gui,owners,SimpleNamespace(SetThreadDpiAwarenessContext=dpi)


def test_reads_approved_main_rectangle_and_restores_thread_dpi():
    values=fake_windows()
    result=_approved_window_geometry(*values)
    assert result=={'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}}
    assert [call.args[0].value for call in values[-1].SetThreadDpiAwarenessContext.call_args_list]==[18446744073709551612,18446744073709551613]


@pytest.mark.parametrize('case',['pid_reuse','other_owner','hidden','minimized','shadow','ambiguous','invalid_rect'])
def test_untrusted_or_unavailable_window_never_used(case):
    process,live,gui,owners,user32=fake_windows((1,2) if case=='ambiguous' else (1,))
    if case=='pid_reuse':live.create_time=lambda:99.0
    if case=='other_owner':owners.GetWindowThreadProcessId=lambda _:(1,999)
    if case=='hidden':gui.IsWindowVisible=lambda _:False
    if case=='minimized':gui.IsIconic=lambda _:True
    if case=='shadow':gui.GetClassName=lambda _:'popupshadow'
    if case=='invalid_rect':gui.GetWindowRect=lambda _:(15,15,15,15)
    assert _approved_window_geometry(process,live,gui,owners,user32) is None


def test_changed_process_after_rectangle_read_is_rejected():
    process,live,gui,owners,user32=fake_windows()
    live.create_time=Mock(side_effect=[12.5,99.0])
    assert _approved_window_geometry(process,live,gui,owners,user32) is None
    assert user32.SetThreadDpiAwarenessContext.call_count==2


def test_window_read_failure_restores_dpi_context():
    values=fake_windows();values[2].EnumWindows=Mock(side_effect=OSError('gone'))
    with pytest.raises(OSError):_approved_window_geometry(*values)
    assert values[-1].SetThreadDpiAwarenessContext.call_count==2


def test_hook_ui_uses_published_geometry_without_ocr_then_keeps_manual_position():
    with _headless_web_ui_module() as module:
        window=WebUiBindingBehaviorTest._window(module,None)
        payload={'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}}
        window.runner.hook_services=SimpleNamespace(startup_window_position_payload=Mock(return_value=payload))
        # Accessing the OCR probe is prohibited in Hook mode.
        window.rpa_bridge=object()
        window.move=Mock()
        available=SimpleNamespace(x=lambda:0,y=lambda:0,width=lambda:1536,height=lambda:864)
        screen=SimpleNamespace(availableGeometry=lambda:available,devicePixelRatio=lambda:1.25)
        module.QGuiApplication=SimpleNamespace(screenAt=lambda _:screen,primaryScreen=lambda:screen)
        window._position_next_to_wechat_once()
        window._position_next_to_wechat_once()
        window.move.assert_called_once_with(872,12)
        window.runner.hook_services.startup_window_position_payload.assert_called_once()


def test_missing_hook_window_does_not_use_other_account_ocr_geometry():
    with _headless_web_ui_module() as module:
        window=WebUiBindingBehaviorTest._window(module,None)
        window.runner.hook_services=SimpleNamespace(startup_window_position_payload=lambda:None)
        window.rpa_bridge=object();window.move=Mock()
        window._position_next_to_wechat_once()
        window.move.assert_not_called()
        assert not window._startup_position_attempted


def test_gui_snapshot_has_no_connection_lock_or_calls_and_cannot_mutate_cache():
    services=object.__new__(HookWorkerServices)
    services.connection=object()
    services._startup_position_payload={'ok':True,'geometry':{'left':15}}
    payload=services.startup_window_position_payload();payload['geometry']['left']=999
    assert services._startup_position_payload['geometry']['left']==15
