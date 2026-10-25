"""Locate once, fill both inputs, then verify only the exact shortcode."""
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


@pytest.mark.parametrize('change', ['one_pixel_label', 'missing_labels', 'control_pixels'])
def test_filled_page_does_not_revalidate_form_layout(tmp_path, change):
    ui = ReplayUI(); capture = ui.capture
    def changed(label):
        frame = capture(label)
        if label == 'fields_filled':
            if change == 'one_pixel_label':
                next(r for r in frame.rows if r['text'] == '发送添加朋友申请')['right'] -= 1
            elif change == 'missing_labels':
                frame.rows = [r for r in frame.rows if r['text'] == 'CJH5N28' or r['top'] >= frame.image.height*.8]
            else:
                ImageDraw.Draw(frame.image).rectangle((0, 0, frame.image.width, ui.frames['fields'].image.height-1), fill=(100, 100, 100))
        return frame
    ui.capture = changed
    with patch.object(f, 'form_fields', wraps=f.form_fields) as locate:
        result = f.run_flow(ui, phone='13800000003', verify_message='您好',
            remark_name='CJH5N28', remark_code='CJH5N28', journal=f.SubmitJournal(tmp_path/'submit.json'))
    assert locate.call_count == 1
    assert result['status'] == 'submitted_pending_recipient'
    assert ui.actions.count('confirm_invite_once') == 1
    assert ui.captures == ['header', 'lookup', 'card', 'fields', 'fields_filled']


@pytest.mark.parametrize('change', ['wrong', 'missing', 'low_confidence', 'outside'])
def test_shortcode_failure_still_blocks_submission(tmp_path, change):
    ui = ReplayUI(); capture = ui.capture
    def changed(label):
        frame = capture(label)
        if label == 'fields_filled':
            row = next(r for r in frame.rows if r['text'] == 'CJH5N28')
            if change == 'wrong': row['text'] = 'CJOTHER'
            elif change == 'missing': frame.rows.remove(row)
            elif change == 'low_confidence': row['confidence'] = .89
            else: row['left'] = -1
        return frame
    ui.capture = changed; journal = f.SubmitJournal(tmp_path/'submit.json')
    result = f.run_flow(ui, phone='13800000003', verify_message='您好',
        remark_name='CJH5N28', remark_code='CJH5N28', journal=journal)
    assert result['error_code'] == 'INVITE_FIELD_VERIFICATION_FAILED'
    assert not journal.path.exists() and 'confirm_invite_once' not in ui.actions
    assert 'search_cleanup' not in ui.actions


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


@pytest.mark.parametrize('label', ['fields', 'fields_filled', 'footer'])
def test_native_capture_distinguishes_input_words_from_system_notice(label):
    from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import NativeGuardTests
    case = NativeGuardTests(); case.setUp()
    try:
        case.ops.try_image_grab = lambda rect: case.fr.image.resize((rect[2]-rect[0], rect[3]-rect[1]))
        case.ops.run_ocr = lambda image: [dict(text='请稍后再试联系我', confidence=.99,
            left=25, top=88, right=300, bottom=102)]
        if label == 'footer':
            with pytest.raises(f.FlowStop, match='ACCOUNT_RESTRICTED'):
                case.ui.capture(label)
        else:
            assert case.ui.capture(label).rows[0]['text'] == '请稍后再试联系我'
        assert case.ui.count == 1 and case.ui.ocr_count == 1
    finally:
        case.tearDown()
