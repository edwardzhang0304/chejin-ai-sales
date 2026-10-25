"""Startup position authority through native C1 and the real evidence exporter.

Win32/OCR/screens are synthetic. No sales account, Windows input or delivery is
simulated as a real acceptance result. Current images are still collected.
"""
import copy
import json
from unittest.mock import Mock, patch

import pytest
from PIL import Image
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1, hook_c1_flow as flow
from apps.wechat_ai_customer_service.tests import test_hook_c1_flow as flow_tests
from test_hook_search_startup_cache import calibration
from test_c1_evidence_chain import scene, outcome, original, TASK
from test_failure_evidence_boundaries import home


def placeholder(alias='搜索'):
    record = next(r for r in json.loads((flow_tests.FIX/'native-roi-synthetic.json').read_text()) if r['label']=='header')
    image = Image.open(flow_tests.FIX/'native-header.png').convert('RGB')
    rows = copy.deepcopy(record['rows'])
    next(r for r in rows if flow.clean(r['text']) in ('搜索','Q搜索'))['text'] = alias
    return flow.Frame(image, rows, 'header')


def forbid_task_location():
    # Startup/standalone may locate, but task placeholder must never call target.
    return patch.object(flow, 'target', side_effect=AssertionError('task relocated search input'))


@pytest.mark.parametrize('alias',['搜索','Q搜索','O搜索','0搜索','搜素','Q搜素','O搜素','0搜素'])
def test_placeholder_uses_startup_coordinates_without_relocating(calibration,monkeypatch,alias):
    native,gateway,process,state,ops,_=calibration
    state['frame']=placeholder()
    monkeypatch.setattr(hook_c1,'prepare_window',Mock(return_value={'ok':True}))
    native.startup(process,gateway,{})
    saved=copy.deepcopy(native._search_control_cache)
    state['frame']=placeholder(alias)
    ui=native.ui(process,{'task_id':'placeholder-cache'},Mock())
    with forbid_task_location(), patch.object(flow,'search_control_bounds',side_effect=AssertionError('task recalibration')):
        frame,target=ui.verified_empty_search()
        ui.clear_search()
    assert target['bounds']==saved['bounds']
    assert native._search_control_cache==saved
    assert ops.try_image_grab.call_count==3  # one startup + two current content checks
    assert ops.run_ocr.call_count==3
    ops.win32api.mouse_event.assert_not_called()


@pytest.mark.parametrize('bad',['low_confidence','duplicate','foreign_text'])
def test_cached_position_never_overrules_current_content_or_layout(calibration,monkeypatch,bad):
    native,gateway,process,state,ops,_=calibration
    state['frame']=placeholder()
    monkeypatch.setattr(hook_c1,'prepare_window',Mock(return_value={'ok':True}))
    native.startup(process,gateway,{})
    ui=native.ui(process,{'task_id':'bad-current-search'},Mock())
    saved=copy.deepcopy(native._search_control_cache)
    state['frame']=placeholder()
    row=next(r for r in state['frame'].rows if flow.clean(r['text'])=='搜索')
    if bad=='low_confidence':row['confidence']=.79
    elif bad=='duplicate':state['frame'].rows.append(copy.deepcopy(row))
    elif bad=='foreign_text':state['frame'].rows.append({**row,'text':'13900000004'})
    with forbid_task_location():
        with pytest.raises(flow.FlowStop):ui.verified_empty_search()
    ops.win32api.mouse_event.assert_not_called()
    assert native._search_control_cache==saved


def test_formal_flow_retains_current_content_checks_and_cache_authority(scene,monkeypatch):
    s=scene();saved=copy.deepcopy(s.native._search_control_cache)
    original_target=flow.target
    def task_target(frame,row,*,filled=None):
        assert filled!='search','task relocated search input'
        return original_target(frame,row,filled=filled)
    with patch.object(flow,'target',side_effect=task_target), patch.object(flow,'search_control_bounds',side_effect=AssertionError('task recalibration')):
        result=s.execute(monkeypatch)
    assert result['status']=='not_found' and result['cleanup']=='search_empty_verified'
    assert s.grabs==['header','lookup','card','header']
    assert s.ocr_calls==s.grabs  # realtime phone readback, result and cleanup, not cached screenshots
    assert s.native._search_control_cache==saved
    assert outcome(s.export())['status']=='not_found'


@pytest.mark.parametrize('during_cleanup',[False,True])
def test_actual_window_move_still_stops_and_exports_evidence(scene,monkeypatch,during_cleanup):
    s=scene();saved=copy.deepcopy(s.native._search_control_cache)
    grab=s.grab;header_count=0
    def changed(bbox):
        nonlocal header_count
        image=grab(bbox)
        if image.info['scene_label']=='header':
            header_count+=1
            if not during_cleanup or header_count==2:
                s.boundary.geom['left'] += 15
        return image
    s.ops.try_image_grab.side_effect=changed
    result=s.execute(monkeypatch)
    z=s.export();saved_result=outcome(z)
    error='cleanup_error_code' if during_cleanup else 'error_code'
    assert saved_result[error]=='WINDOW_FOCUS_OR_GEOMETRY_CHANGED'
    assert saved_result['guard_failures'][-1]['condition']=='main_geometry_changed'
    assert not s.native.startup_layout_ready()
    frames=[x for x in saved_result['evidence']['frames'] if x['label']=='header']
    assert frames[-1]['screenshot']=='saved' and frames[-1]['ocr']=='not_produced'
    assert any(n.startswith(f'tasks/{TASK}/screenshots/') and n.endswith('-header.png') for n in z)
    assert not result['submit_attempted'] and not (original(s)/'submit-attempt.json').exists()
    if not during_cleanup:s.ops.win32api.mouse_event.assert_not_called()
    # The failure goes through the normal formal receipt. It is not a replay permit.
    before=(original(s)/'outcome.json').read_bytes()
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
    assert (original(s)/'outcome.json').read_bytes()==before


@pytest.fixture
def native_invite(scene):
    s=scene();o=s.ops;b=s.boundary
    rows={r['label']:copy.deepcopy(r['rows']) for r in json.loads((flow_tests.FIX/'native-roi-synthetic.json').read_text())}
    physical={'clicks':0,'dialog':False,'clipboard':'','fields':0}
    main_geometry=o.get_window_geometry
    main_client=o.get_window_client_geometry
    o.get_window_geometry=lambda h:main_geometry(h) if h==11 else {'left':300,'top':80,'width':450,'height':780}
    o.get_window_client_geometry=lambda h:main_client(h) if h==11 else {'screen_left':300,'screen_top':80,'width':450,'height':780}
    o.win32gui.IsWindow=lambda h:h==11 or physical['dialog']
    o.win32gui.IsWindowVisible=o.win32gui.IsWindow
    o.win32gui.GetClassName=lambda h:'WeChatMainWndForPC' if h==11 else 'InviteDialog'
    def enum(callback,arg):
        callback(11,arg)
        if physical['dialog']:callback(22,arg)
    o.win32gui.EnumWindows=enum
    def mouse(event,*args):
        if event!=o.win32con.MOUSEEVENTF_LEFTUP:return
        physical['clicks']+=1
        if physical['clicks']==3:
            physical['dialog']=True;b.foreground=b.hit=22
        elif physical['clicks']==6:
            physical['dialog']=False;b.foreground=b.hit=11
    o.win32api.mouse_event.side_effect=mouse
    o.clipboard_copy.side_effect=lambda text:physical.update(clipboard=text)
    o.clipboard_read.side_effect=lambda:physical['clipboard']
    def grab(bbox):
        size=(bbox[2]-bbox[0],bbox[3]-bbox[1])
        label={(659,90):'header',(403,317):'lookup',(489,405):'card',(450,280):'fields',(450,156):'footer',(450,780):'review'}[size]
        s.grabs.append(label)
        if label=='review':
            image=Image.new('RGB',(450,780),(240,240,240))
            image.paste(Image.open(flow_tests.FIX/'native-fields.png'),(0,0))
            image.paste(Image.open(flow_tests.FIX/'native-footer.png'),(0,624))
        else:
            image=Image.open(flow_tests.FIX/f'native-{label}.png').convert('RGB')
        image.info['scene_label']=label
        return image
    def ocr(image):
        label=image.info['scene_label'];s.ocr_calls.append(label)
        result=copy.deepcopy(rows['fields' if label=='review' else label])
        if label=='review':result.extend({**r,'top':r['top']+624,'bottom':r['bottom']+624} for r in rows['footer'])
        if label in ('fields','review'):
            physical['fields']+=1
            if physical['fields']==2:
                # Native paste above must really have reached the second field.
                assert physical['clipboard']==s.preparation['remark_name']
                next(r for r in result if r['top']==204)['text']=physical['clipboard']
        return result
    o.try_image_grab.side_effect=grab;o.run_ocr.side_effect=ocr
    return s,physical


def test_full_native_invite_uses_startup_map_and_single_form_layout(native_invite,monkeypatch):
    s,physical=native_invite;o=s.ops
    saved=copy.deepcopy(s.native._search_control_cache)
    original_target=flow.target
    def task_target(frame,row,*,filled=None):
        assert filled!='search','task relocated search input'
        return original_target(frame,row,filled=filled)
    with patch.object(flow,'target',side_effect=task_target), patch.object(flow,'search_control_bounds',side_effect=AssertionError('task recalibration')), patch.object(flow,'form_fields',wraps=flow.form_fields) as locate_form:
        result=s.execute(monkeypatch)
    assert result['status']=='submitted_pending_recipient',result
    assert result['cleanup']=='search_empty_verified'
    assert result['submit_attempted'] and not result['delivery_confirmed'] and not result['ai_admission']
    assert locate_form.call_count==1
    assert s.grabs==['header','lookup','card','fields','review','header']
    assert physical['clicks']==6 and o.clipboard_copy.call_count==2
    assert s.native._search_control_cache==saved
    before=(original(s)/'submit-attempt.json').read_bytes()
    assert json.loads(before)['outcome']=='unknown'
    assert outcome(s.export())['submit_attempted']
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
    assert (original(s)/'submit-attempt.json').read_bytes()==before and physical['clicks']==6
