"""Positions are reused; fresh form identity/content still authorize each action."""
from copy import deepcopy
from unittest.mock import patch
import pytest
from PIL import ImageDraw
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as f
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


def test_full_submission_locates_fields_once_and_preserves_unknown_journal(tmp_path):
    ui=ReplayUI();journal=f.SubmitJournal(tmp_path/'submit.json')
    with patch.object(f,'form_fields',wraps=f.form_fields) as locate:
        result=f.run_flow(ui,phone='13800000003',verify_message='您好',remark_name='CJH5N28',remark_code='CJH5N28',journal=journal)
    assert locate.call_count==1
    assert result['status']=='submitted_pending_recipient' and not result['delivery_confirmed']
    original=journal.path.read_bytes()
    again=f.run_flow(ui,phone='13800000003',verify_message='您好',remark_name='CJH5N28',remark_code='CJH5N28',journal=journal)
    assert again['error_code']=='PREVIOUS_SUBMIT_ATTEMPT_REQUIRES_REVIEW' and journal.path.read_bytes()==original
    assert ui.actions.count('confirm_invite_once')==1


@pytest.mark.parametrize('change',['hwnd','origin','geometry','label','control','shortcode'])
def test_changed_form_never_uses_cached_fields_to_submit(tmp_path,change):
    ui=ReplayUI();capture=ui.capture
    def changed(label):
        frame=capture(label)
        if label=='fields_filled':
            if change=='hwnd':frame.hwnd=123
            elif change=='origin':frame.origin=(100,100)
            elif change=='geometry':frame.geometry={'width':999}
            elif change=='label':
                row=next(r for r in frame.rows if r['text']=='备注名');row['left']+=2
            elif change=='control':
                b=f.form_fields(frame)['remark']['bounds'];ImageDraw.Draw(frame.image).rectangle(b,fill=(100,100,100))
            else:frame.rows[-1]['text']='CJOTHER'
        return frame
    ui.capture=changed;journal=f.SubmitJournal(tmp_path/'submit.json')
    result=f.run_flow(ui,phone='13800000003',verify_message='您好',remark_name='CJH5N28',remark_code='CJH5N28',journal=journal)
    assert result['error_code'] in {'INVITE_FORM_LAYOUT_CHANGED','INVITE_FIELD_VERIFICATION_FAILED'}
    assert not journal.path.exists() and 'confirm_invite_once' not in ui.actions
    assert 'search_cleanup' not in ui.actions


def test_mutating_geometry_does_not_mutate_saved_form_identity():
    frame=ReplayUI().frames['fields'];frame.geometry={'width':450}
    layout=f.InviteFormLayout(frame);frame.geometry['width']=451
    with pytest.raises(f.FlowStop,match='LAYOUT_CHANGED'):layout.current(frame)


def test_both_inputs_use_the_same_initial_frame_then_one_shortcode_read(tmp_path):
    ui=ReplayUI();replace=ui.replace;used=[]
    def record(frame,text):
        if frame.name.startswith('fields'):used.append(frame)
        replace(frame,text)
    ui.replace=record
    result=f.run_flow(ui,phone='13800000003',verify_message='您好',remark_name='CJH5N28',
        remark_code='CJH5N28',journal=f.SubmitJournal(tmp_path/'submit.json'))
    assert result['status']=='submitted_pending_recipient'
    assert len(used)==2 and used[0] is used[1]
    assert [name for name in ui.captures if name.startswith('fields')]==['fields','fields_filled']
