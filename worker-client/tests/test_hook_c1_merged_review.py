"""One current review image authorizes shortcode + button; native flow/ZIP retained."""
import copy
import json
from unittest.mock import patch

import pytest
from PIL import ImageDraw
from chejin_worker_client.hook_progress import HookProgressTimeline, INVITE_ACTION_PHASES
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as flow
from test_hook_search_cache_consumer import native_invite
from test_c1_evidence_chain import scene, original, outcome, home


@pytest.mark.parametrize('damage', ['missing_button','duplicate_button','disabled_button',
    'button_outside_footer','low_button_confidence','wrong_shortcode','low_shortcode_confidence','restricted_footer'])
def test_combined_review_cannot_submit_bad_content(native_invite,monkeypatch,damage):
    s,physical=native_invite;o=s.ops;ocr=o.run_ocr.side_effect
    grab=o.try_image_grab.side_effect
    def disabled(bbox):
        image=grab(bbox)
        if damage=='disabled_button' and image.info['scene_label']=='review':
            ImageDraw.Draw(image).rectangle((0,624,450,780),fill=(220,220,220))
        return image
    o.try_image_grab.side_effect=disabled
    def changed(image):
        rows=ocr(image)
        if image.info['scene_label']!='review':return rows
        button=next(r for r in rows if r['text']=='确定')
        remark=next(r for r in rows if r['top']==204)
        if damage=='missing_button':rows.remove(button)
        elif damage=='duplicate_button':rows.append(copy.deepcopy(button))
        elif damage=='button_outside_footer':button.update(top=400,bottom=420)
        elif damage=='low_button_confidence':button['confidence']=.5
        elif damage=='wrong_shortcode':remark['text']='CJXXXXX'
        elif damage=='low_shortcode_confidence':remark['confidence']=.5
        elif damage=='restricted_footer':rows.append(dict(text='操作频繁',confidence=.99,left=5,right=100,top=630,bottom=650))
        return rows
    o.run_ocr.side_effect=changed
    result=s.execute(monkeypatch)
    assert not result['submit_attempted'] and physical['clicks']==5
    assert not (original(s)/'submit-attempt.json').exists()
    assert outcome(s.export())['error_code']==result['error_code']
    saved=(original(s)/'outcome.json').read_bytes()
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
    assert physical['clicks']==5 and (original(s)/'outcome.json').read_bytes()==saved


def test_full_native_progress_precedes_actions_and_review_is_shared(native_invite,monkeypatch):
    s,physical=native_invite;o=s.ops;events=[];operations=[];timeline=HookProgressTimeline()
    timeline.apply({'event':'hook_progress_group','context':'invite','group':'invite','kind':'invite'})
    create=s.native.ui
    def make(*args,**kwargs):
        ui=create(*args,**kwargs)
        def progress(phase,state,note=None):
            event=dict(event='hook_progress',context='invite',group='invite',phase=phase,state=state,
                       note=note,time=f'10:00:{len(events):02d}')
            events.append(event);timeline.apply(event)
        ui.on_progress=progress
        for method in ('capture','click','replace','select_invite_dialog','observe_submit','clear_search'):
            fn=getattr(ui,method)
            def wrapped(*a,_fn=fn,_method=method,**kw):
                current=next(e['phase'] for e in reversed(events) if e['state']=='current')
                label=a[0] if _method=='capture' else a[2] if _method=='click' else _method
                operations.append((label,current))
                return _fn(*a,**kw)
            setattr(ui,method,wrapped)
        return ui
    monkeypatch.setattr(s.native,'ui',make)
    # User wording in the greeting is not a system restriction; footer is.
    s.preparation['verify_message']='您好，稍后再试是本次申请语'
    (original(s)/'preparation.json').write_text(json.dumps(s.preparation))
    ocr=o.run_ocr.side_effect
    def greeting_words(image):
        rows=ocr(image)
        if image.info['scene_label']=='review':
            next(r for r in rows if r['top']==108)['text']=s.preparation['verify_message']
        return rows
    o.run_ocr.side_effect=greeting_words
    result=s.execute(monkeypatch)
    assert result['formal_submission_proof_saved'],result
    assert s.grabs==s.ocr_calls==['header','lookup','card','fields','review','header']
    assert physical['clicks']==6
    expected={'focus_main_search':'search_input','network_lookup':'query_verified_and_network_lookup',
        'card':'contact_result','open_invite_form':'invite_form_open','select_invite_dialog':'invite_form_open',
        'focus_greeting':'invite_form_fields','focus_remark':'invite_form_fields',
        'fields_filled':'verify_remark_shortcode','confirm_invite_once':'confirm_once','clear_search':'cleanup'}
    for operation,phase in operations:
        if operation in expected:assert phase==expected[operation],(operation,phase)
    model=timeline.model()
    for phase in INVITE_ACTION_PHASES:
        current=next((e for e in events if e['phase']==phase and e['state']=='current'),None)
        if current:
            item=timeline.latest[('invite',phase)]
            assert item['time']==current['time']
            assert item['state']=='done'
    assert [e['phase'] for e in events if e['state']=='current']==[
        'search_empty','search_input','query_verified_and_network_lookup','contact_result',
        'invite_form_open','invite_form_fields','verify_remark_shortcode','confirm_once','cleanup']
    assert outcome(s.export())['submit_attempted']
    assert any(name.endswith('-fields_filled.png') for name in s.export())
    assert not any(name.endswith('-footer.png') for name in s.export())
    assert model


@pytest.mark.parametrize('terminal',['done','error','waiting'])
def test_invite_timestamp_is_start_time_and_new_task_has_own_time(terminal):
    t=HookProgressTimeline()
    def event(context,state,time):return dict(event='hook_progress',context=context,
        phase='invite_form_fields',state=state,time=time)
    t.apply(event('one','current','10:00:01'));t.apply(event('one',terminal,'10:00:09'))
    t.apply(event('two','current','10:00:10'))
    assert t.latest[('one','invite_form_fields')]['time']=='10:00:01'
    assert t.latest[('one','invite_form_fields')]['state']==terminal
    assert t.latest[('two','invite_form_fields')]['time']=='10:00:10'


def test_progress_does_not_check_old_foreground_before_contact_popup_adoption(native_invite,monkeypatch):
    s,physical=native_invite;o=s.ops;b=s.boundary;card={'visible':False}
    geometry=o.get_window_geometry;client=o.get_window_client_geometry
    o.get_window_geometry=lambda h:dict(left=400,top=90,width=489,height=405) if h==33 else geometry(h)
    o.get_window_client_geometry=lambda h:dict(screen_left=400,screen_top=90,width=489,height=405) if h==33 else client(h)
    is_window=o.win32gui.IsWindow;cls=o.win32gui.GetClassName
    o.win32gui.IsWindow=lambda h:card['visible'] if h==33 else is_window(h)
    o.win32gui.IsWindowVisible=o.win32gui.IsWindow
    o.win32gui.GetClassName=lambda h:'ContactProfileWnd' if h==33 else cls(h)
    enum=o.win32gui.EnumWindows
    def windows(callback,arg):
        enum(callback,arg)
        if card['visible']:callback(33,arg)
    o.win32gui.EnumWindows=windows
    mouse=o.win32api.mouse_event.side_effect
    def click(event,*args):
        mouse(event,*args)
        if event==o.win32con.MOUSEEVENTF_LEFTUP and physical['clicks']==2:
            card['visible']=True;b.foreground=b.hit=33
    o.win32api.mouse_event.side_effect=click
    result=s.execute(monkeypatch)
    assert result['formal_submission_proof_saved'],result
    assert physical['clicks']==6
    assert any(e.get('stage')=='contact_result_foreground' and e.get('accepted') for e in result['window_events'])
