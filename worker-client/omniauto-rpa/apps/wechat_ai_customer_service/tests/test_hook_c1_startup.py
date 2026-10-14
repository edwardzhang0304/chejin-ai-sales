from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PIL import Image,ImageDraw

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning as planner
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


def plan(dpi=1, work=None, before=None, profile='hook-3.9-c1'):
    return planner.plan_normalize_wechat_window(before or {}, dpi_scale=dpi,
        work_area=work or {'left':0,'top':0,'width':1920,'height':1040},startup_profile=profile)


@pytest.mark.parametrize('dpi,size',[(1,(1060,740)),(1.25,(1060,740)),(1.5,(1060,740)),(2,(1060,740))])
def test_hook_outer_size_matches_screenshot_pixels_on_current_monitor(dpi,size):
    value=plan(dpi,{'left':-2500,'top':40,'width':2500,'height':1800})
    assert value['ok'] and (value['width'],value['height'])==size
    assert (value['left'],value['top'])==(-2500+round(12*dpi),40+round(12*dpi))


def test_already_normalized_and_too_small_screen():
    assert not plan(before={'left':12,'top':12,'width':1060,'height':740})['move']
    assert not plan(work={'left':0,'top':0,'width':650,'height':590})['ok']


def test_old_default_profile_is_unchanged_and_unknown_profile_stops():
    assert plan(profile='ocr')['requested_target']=={'width':800,'height':852}
    assert plan(profile='invalid')=={'ok':False,'move':False,'reason':'startup_profile_unknown'}


def make_image():
    image=Image.new('RGB',(1044,710),(245,245,245))
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,67,709),fill=(45,45,45))
    draw.rectangle((68,0,375,709),fill=(235,235,235))
    return image


def test_search_is_inside_sidebar_and_needs_no_chat_or_composer():
    anchors=[{'text':'搜索','left':83,'top':37,'right':119,'bottom':54,'confidence':.99},
             {'text':'搜索','left':640,'top':270,'right':680,'bottom':292,'confidence':.99}]
    value=hook_c1.main_search_target(make_image(),anchors)
    assert value['ok'] and value['point']==[101,45]
    assert value['click_executed'] is False
    assert set(value['layout']['regions'])=={'left_nav_bounds','sidebar_bounds','sidebar_header_bounds'}


@pytest.mark.parametrize('anchors',[
    [],[{'text':'搜索','left':640,'top':270,'right':680,'bottom':292,'confidence':.99}],
    [{'text':'搜索','left':83,'top':37,'right':119,'bottom':54,'confidence':.2}],
    [{'text':'搜索','left':83,'top':37,'right':119,'bottom':54,'confidence':.99},
     {'text':'搜索','left':163,'top':37,'right':199,'bottom':54,'confidence':.99}],
])
def test_missing_outside_weak_or_ambiguous_search_cannot_authorize_click(anchors):
    assert not hook_c1.main_search_target(make_image(),anchors)['ok']


def fake_ops(monkeypatch,tmp_path):
    geometry={'left':12,'top':12,'right':1072,'bottom':752,'width':1060,'height':740}
    client={'left':0,'top':0,'right':1044,'bottom':710,'width':1044,'height':710,'screen_left':20,'screen_top':42}
    gui=SimpleNamespace(IsWindowVisible=lambda h:True,IsIconic=lambda h:False,
        GetClassName=lambda h:'WeChatMainWndForPC',GetForegroundWindow=lambda:11,
        GetWindow=lambda h,flag:0,
        EnumWindows=lambda cb,arg:cb(11,arg),IsZoomed=lambda h:False,MoveWindow=Mock())
    monkeypatch.setattr(sidecar,'win32gui',gui)
    monkeypatch.setattr(sidecar,'get_window_geometry',lambda h:dict(geometry))
    monkeypatch.setattr(sidecar,'get_window_client_geometry',lambda h:dict(client))
    monkeypatch.setattr(sidecar,'window_dpi_scale',lambda h:1)
    monkeypatch.setattr(sidecar,'screen_work_area',lambda h:{'left':0,'top':0,'width':1920,'height':1040})
    monkeypatch.setattr(sidecar,'invalidate_layout_snapshot',Mock())
    monkeypatch.setattr(sidecar,'humanized_action_sleep',Mock())
    monkeypatch.setattr(sidecar,'activate_window',Mock())
    monkeypatch.setattr(sidecar,'win32process',SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,99)))
    monkeypatch.setattr(sidecar,'ensure_dpi_awareness_status',lambda:{'per_monitor_aware':True})
    monkeypatch.setattr(sidecar,'capture_wechat',Mock(return_value=(make_image(),str(tmp_path/'image.png'))))
    monkeypatch.setattr(sidecar,'_OCR_IMPORT_ERROR','not installed in this fixture')
    return gui,client


def test_real_normalizer_preserves_old_client_gate_and_accepts_hook_c1(monkeypatch,tmp_path):
    gui,client=fake_ops(monkeypatch,tmp_path)
    assert sidecar.normalize_wechat_window(11,startup_profile='hook-3.9-c1')['ok']
    # Original OCR minimum is still 720; a compact frame cannot pass it.
    gui.MoveWindow=Mock()
    assert not sidecar.normalize_wechat_window(11)['ok']
    assert not sidecar.normalize_wechat_window(11,startup_profile='unknown')['ok']


def test_prepare_uses_original_normalizer_no_clicks_and_reports_missing_ocr(monkeypatch,tmp_path):
    fake_ops(monkeypatch,tmp_path)
    value=hook_c1.prepare_window(sidecar,11,99,artifact_dir=str(tmp_path))
    assert value['ok'] and value['status']=='window_geometry_verified_ocr_pending'
    assert value['screenshot_size']==[1044,710] and value['ocr_call_count']==0
    assert value['click_actions']==value['input_actions']==0 and not value['business_ready']
    sidecar.activate_window.assert_called_once_with(11,foreground_only=True)


@pytest.mark.parametrize('boundary',['popup','wrong_pid','focus','dpi','capture_changed'])
def test_preparation_stops_at_unsafe_boundary(monkeypatch,tmp_path,boundary):
    gui,client=fake_ops(monkeypatch,tmp_path)
    if boundary=='popup':gui.EnumWindows=lambda cb,arg:(cb(11,arg),cb(12,arg))
    if boundary=='wrong_pid':monkeypatch.setattr(sidecar,'win32process',SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,100)))
    if boundary=='focus':gui.GetForegroundWindow=lambda:12
    if boundary=='dpi':monkeypatch.setattr(sidecar,'ensure_dpi_awareness_status',lambda:{'per_monitor_aware':False})
    if boundary=='capture_changed':client['width']=1000
    value=hook_c1.prepare_window(sidecar,11,99,artifact_dir=str(tmp_path))
    assert not value['ok'] and value['status']=='stopped' and value['click_actions']==0


@pytest.mark.parametrize('case', ['main_shadow', 'other_owner', 'real_popup', 'large_shadow',
                                 'inside_main', 'foreground_shadow', 'other_process', 'both'])
def test_actual_wechat_shadow_and_real_popup_boundaries(monkeypatch,tmp_path,case):
    gui,_=fake_ops(monkeypatch,tmp_path)
    main={'left':330,'top':52,'right':1193,'bottom':707,'width':863,'height':655}
    shadow={'left':314,'top':36,'right':1209,'bottom':723,'width':895,'height':687}
    if case=='large_shadow':shadow.update(left=200,width=1009)
    if case=='inside_main':shadow.update(left=340,width=869)
    gui.GetClassName=lambda h: 'WeChatMainWndForPC' if h==11 else ('ContactDialog' if case=='real_popup' or h==13 else 'popupshadow')
    gui.GetWindow=lambda h,flag: 77 if case=='other_owner' else 11
    if case=='foreground_shadow':gui.GetForegroundWindow=lambda:12
    gui.EnumWindows=lambda cb,arg: [cb(h,arg) for h in ([11,12,13] if case=='both' else [11,12])]
    if case=='other_process':
        monkeypatch.setattr(sidecar,'win32process',SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,100 if h==12 else 99)))
    monkeypatch.setattr(sidecar,'get_window_geometry',lambda h: dict(main if h==11 else shadow))
    # Keep the original normalizer under separate tests; this fixture isolates
    # classification of the native window inventory supplied by the user.
    monkeypatch.setattr(sidecar,'normalize_wechat_window',Mock(return_value={'ok':True,'after':main,'after_dpi_scale':1}))
    value=hook_c1.prepare_window(sidecar,11,99,artifact_dir=str(tmp_path))
    allowed=case in {'main_shadow','other_process'}
    assert value['ok'] is allowed
    if allowed:
        assert value['status']=='window_geometry_verified_ocr_pending'
        assert len(value['ignored_main_window_shadows'])==(1 if case=='main_shadow' else 0)
        sidecar.normalize_wechat_window.assert_called_once()
    else:
        assert value['reason']=='hook_c1_close_wechat_popup_first'
        assert value['blocking_windows']==1
        assert len(value['blocking_window_details'])==1
        sidecar.normalize_wechat_window.assert_not_called()
        sidecar.capture_wechat.assert_not_called()


@pytest.mark.parametrize('dpi', [0, float('nan'), float('inf'), 5])
def test_invalid_shadow_dpi_cannot_skip_blocker(dpi):
    rect={'left':0,'top':0,'right':800,'bottom':700,'width':800,'height':700}
    assert not hook_c1.main_window_shadow_bounds_match(rect,rect,dpi_scale=dpi)


@pytest.mark.parametrize('dpi,size',[(1,(800,852)),(1.25,(1000,1065)),(1.5,(1200,1278))])
def test_old_ocr_dpi_profile_keeps_its_original_targets(dpi,size):
    value=plan(dpi,{'left':0,'top':0,'width':2500,'height':1800},profile='ocr')
    assert value['ok'] and (value['width'],value['height'])==size


def test_hook_physical_target_is_stable_at_125_percent():
    before={'left':15,'top':15,'width':1060,'height':740}
    value=plan(1.25,before=before)
    assert value['ok'] and not value['move']
    assert value['requested_target']=={'width':1060,'height':740}


def test_actual_125_percent_oversized_frame_is_resized_once(monkeypatch,tmp_path):
    gui,_=fake_ops(monkeypatch,tmp_path)
    current={'left':15,'top':15,'right':1340,'bottom':940,'width':1325,'height':925}
    monkeypatch.setattr(sidecar,'get_window_geometry',lambda h:dict(current))
    monkeypatch.setattr(sidecar,'get_window_client_geometry',lambda h:dict(current))
    monkeypatch.setattr(sidecar,'window_dpi_scale',lambda h:1.25)
    monkeypatch.setattr(sidecar,'screen_work_area',lambda h:{'left':0,'top':0,'width':1920,'height':1030})
    def move(hwnd,left,top,width,height,repaint):
        current.update(left=left,top=top,right=left+width,bottom=top+height,width=width,height=height)
    gui.MoveWindow=Mock(side_effect=move)
    value=sidecar.normalize_wechat_window(11,startup_profile='hook-3.9-c1')
    assert value['ok'] and value['applied']
    assert value['after']['width']==1060 and value['after']['height']==740
    assert value['after_dpi_scale']==1.25
    gui.MoveWindow.assert_called_once_with(11,15,15,1060,740,True)


def test_compact_profile_fits_smaller_work_area_without_enlarging():
    value=plan(1.25,{'left':-900,'top':20,'width':900,'height':700})
    assert value['ok'] and value['width']<=1060 and value['height']<=740
    assert value['right']<=-900+900-15 and value['bottom']<=20+700-15


def search_roi_ops(monkeypatch,tmp_path):
    gui,client=fake_ops(monkeypatch,tmp_path)
    import math
    image=make_image().crop((0,0,math.ceil(client['width']*.62),64))
    anchors=[{'text':'搜索','left':83,'top':37,'right':119,'bottom':54,'confidence':.99}]
    monkeypatch.setattr(sidecar,'_OCR_IMPORT_ERROR','')
    monkeypatch.setattr(sidecar,'try_image_grab',Mock(return_value=image))
    monkeypatch.setattr(sidecar,'run_ocr',Mock(return_value=anchors))
    return gui,client,image


def test_roi_uses_client_width_without_manufacturing_pixels():
    image=make_image().crop((0,0,500,80))
    rows=[{'text':'搜索','left':83,'top':37,'right':119,'bottom':54,'confidence':.99}]
    assert not hook_c1.main_search_target(image,rows)['ok']
    value=hook_c1.main_search_target(image,rows,full_client_width=1044)
    assert value['ok'] and value['point']==[101,45]
    assert value['layout']['regions']['sidebar_bounds']==[68,0,376,80]


@pytest.mark.parametrize('width',[100,0,-1,'1060'])
def test_roi_invalid_reference_width_stops(width):
    image=make_image().crop((0,0,500,80))
    assert not hook_c1.main_search_target(image,[],full_client_width=width)['ok']


def test_roi_captures_once_no_full_window_no_resize_no_ui_input(monkeypatch,tmp_path):
    _,client,image=search_roi_ops(monkeypatch,tmp_path)
    path=tmp_path/'roi.png'
    value=hook_c1.inspect_main_search_roi(sidecar,11,99,screenshot_path=str(path))
    assert value['ok'] and value['status']=='search_roi_recognition_verified'
    assert value['screenshot_call_count']==value['ocr_call_count']==1
    assert Image.open(path).size==image.size
    assert value['target_screen_point']==[121,87]
    sidecar.try_image_grab.assert_called_once_with((20,42,20+image.width,42+64))
    sidecar.run_ocr.assert_called_once_with(image)
    sidecar.capture_wechat.assert_not_called()
    sidecar.win32gui.MoveWindow.assert_not_called()
    assert value['click_actions']==value['input_actions']==0


@pytest.mark.parametrize('boundary',['engine','focus','size','dpi','existing_image','capture_size','ocr_unresolved','ocr_frame_changed'])
def test_roi_failure_never_recaptures_or_clicks(monkeypatch,tmp_path,boundary):
    gui,client,image=search_roi_ops(monkeypatch,tmp_path)
    path=tmp_path/'roi.png'
    if boundary=='engine':monkeypatch.setattr(sidecar,'_OCR_IMPORT_ERROR','not installed')
    if boundary=='focus':gui.GetForegroundWindow=lambda:12
    if boundary=='size':monkeypatch.setattr(sidecar,'get_window_geometry',lambda h:{'width':1325,'height':925})
    if boundary=='dpi':monkeypatch.setattr(sidecar,'ensure_dpi_awareness_status',lambda:{'per_monitor_aware':False})
    if boundary=='existing_image':path.write_bytes(b'previous')
    if boundary=='capture_size':sidecar.try_image_grab.return_value=Image.new('RGB',(10,10))
    if boundary=='ocr_unresolved':sidecar.run_ocr.return_value=[]
    if boundary=='ocr_frame_changed':
        def changed(_):
            client['width']=1000
            return []
        sidecar.run_ocr.side_effect=changed
    value=hook_c1.inspect_main_search_roi(sidecar,11,99,screenshot_path=str(path))
    assert not value['ok'] and value['screenshot_call_count']<=1
    assert sidecar.try_image_grab.call_count<=1 and sidecar.run_ocr.call_count<=1
    assert value['click_actions']==value['input_actions']==0
    sidecar.capture_wechat.assert_not_called()
    gui.MoveWindow.assert_not_called()
    if boundary=='existing_image':
        assert path.read_bytes()==b'previous' and sidecar.try_image_grab.call_count==0


def test_header_roi_can_resolve_thin_separator_on_equal_surfaces():
    image=Image.new('RGB',(657,80),(245,245,245))
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,67,79),fill=(45,45,45))
    draw.line((379,0,379,79),fill=(210,210,210))
    rows=[{'text':'搜索','left':110,'top':40,'right':140,'bottom':58,'confidence':.99}]
    value=hook_c1.main_search_target(image,rows,full_client_width=1060)
    assert value['ok']
    assert value['layout']['regions']['sidebar_bounds'][2]==379


def test_header_text_strokes_are_not_full_height_separators():
    image=Image.new('RGB',(657,80),(245,245,245))
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,67,79),fill=(45,45,45))
    draw.line((379,35,379,55),fill=(40,40,40))
    rows=[{'text':'搜索','left':110,'top':40,'right':140,'bottom':58,'confidence':.99}]
    assert not hook_c1.main_search_target(image,rows,full_client_width=1060)['ok']


def test_geometry_only_startup_takes_no_screenshot_or_ocr(monkeypatch,tmp_path):
    fake_ops(monkeypatch,tmp_path)
    monkeypatch.setattr(sidecar,'run_ocr',Mock(side_effect=AssertionError('startup must not OCR')))
    value=hook_c1.prepare_window(sidecar,11,99,artifact_dir=str(tmp_path),capture_evidence=False)
    assert value['ok'] and value['status']=='window_geometry_verified'
    assert value['screenshot_call_count']==value['ocr_call_count']==0
    sidecar.capture_wechat.assert_not_called();sidecar.run_ocr.assert_not_called()
    assert value['click_actions']==value['input_actions']==0


def test_geometry_only_startup_rejects_changed_frame_without_capture(monkeypatch,tmp_path):
    fake_ops(monkeypatch,tmp_path)
    normalize=sidecar.normalize_wechat_window
    def change_after_normalization(*args,**kwargs):
        value=normalize(*args,**kwargs)
        monkeypatch.setattr(sidecar,'window_dpi_scale',lambda _:1.5)
        return value
    monkeypatch.setattr(sidecar,'normalize_wechat_window',change_after_normalization)
    value=hook_c1.prepare_window(sidecar,11,99,artifact_dir=str(tmp_path),capture_evidence=False)
    assert not value['ok']
    sidecar.capture_wechat.assert_not_called()
