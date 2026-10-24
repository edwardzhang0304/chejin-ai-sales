"""Exact Hook task shortcode admission with the shared legacy caret normalizer.

Full flow uses real startup/formal/NativeUI/controller/journal/export; native
Windows operations and OCR are synthetic. No physical invitation is sent.
"""
import copy
import json
from unittest.mock import patch

import pytest
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as flow
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import frames
from test_hook_search_cache_consumer import native_invite
from test_c1_evidence_chain import scene,original,outcome,TASK
from test_failure_evidence_boundaries import home


@pytest.mark.parametrize('expected',['CJH5N28','CJ8NZRD9'])
@pytest.mark.parametrize('suffix,accepted',[('',True),('|',True),('||',False),('I',False),('l',False),('1',False),('｜',False)])
def test_exact_shortcode_and_only_single_ascii_caret(expected,suffix,accepted):
    frame=frames()['fields'];bounds=flow.form_fields(frame)['remark']['bounds']
    row=next(r for r in frame.rows if flow.contains(bounds,r))
    row.update(text=expected+suffix,confidence=.99)
    before=copy.deepcopy(frame.rows)
    if accepted:flow.verify_fields(frame,'您好',expected)
    else:
        with pytest.raises(flow.FlowStop,match='INVITE_FIELD_VERIFICATION_FAILED'):
            flow.verify_fields(frame,'您好',expected)
    assert frame.rows==before  # Do not rewrite diagnostic OCR evidence.


@pytest.mark.parametrize('change',['wrong_code','lowercase','leading_bar','internal_bar','low_confidence','outside','duplicate','separate_bar','prefix_name'])
def test_caret_never_authorizes_ambiguous_or_wrong_task_code(change):
    expected='CJ8NZRD9';frame=frames()['fields'];bounds=flow.form_fields(frame)['remark']['bounds']
    row=next(r for r in frame.rows if flow.contains(bounds,r));row.update(text=expected+'|',confidence=.99)
    if change=='wrong_code':row['text']='CJ8NZRD8|'
    elif change=='lowercase':row['text']=expected.lower()+'|'
    elif change=='leading_bar':row['text']='|'+expected
    elif change=='internal_bar':row['text']=expected[:3]+'|'+expected[3:]
    elif change=='low_confidence':row['confidence']=.899
    elif change=='outside':row['left']=bounds[0]-1
    elif change=='duplicate':frame.rows.append(copy.deepcopy(row))
    elif change=='separate_bar':
        row['text']=expected;frame.rows.append({**row,'text':'|'})
    else:row['text']='测试'+expected+'|';expected='测试'+expected
    with pytest.raises(flow.FlowStop,match='INVITE_FIELD_VERIFICATION_FAILED'):
        flow.verify_fields(frame,'您好',expected)


@pytest.mark.parametrize('case',['normal','caret','wrong_code','low_confidence','separate_bar'])
def test_native_form_completion_without_extra_capture_or_weaker_admission(native_invite,monkeypatch,case):
    s,physical=native_invite;ocr=s.ops.run_ocr.side_effect;raw=[]
    def observe(image):
        rows=ocr(image)
        if image.info['scene_label']=='fields' and physical['fields']==2:
            row=next(r for r in rows if r['top']==204)
            if case=='caret':row['text']+='|'
            elif case=='wrong_code':row['text']='CJOTHER|'
            elif case=='low_confidence':row['text']+='|';row['confidence']=.899
            elif case=='separate_bar':rows.append({**row,'text':'|'})
            raw.extend(copy.deepcopy(rows))
        return rows
    s.ops.run_ocr.side_effect=observe
    with patch.object(flow,'form_fields',wraps=flow.form_fields) as locate:
        result=s.execute(monkeypatch)
    assert locate.call_count==1 and physical['fields']==2
    assert s.ops.clipboard_copy.call_count==2  # No refill/copy-back detour.
    assert s.grabs[:5]==['header','lookup','card','fields','fields']
    z=s.export();saved=outcome(z)
    assert json.loads(z[f'tasks/{TASK}/ocr/05-fields_filled.json'])==raw
    if case in {'normal','caret'}:
        assert result['status']=='submitted_pending_recipient',result
        assert result['field_verification']['verified']
        assert result['cleanup']=='search_empty_verified'
        assert s.grabs==['header','lookup','card','fields','fields','footer','header']
        assert physical['clicks']==6 and result['submit_attempted']
        assert not result['delivery_confirmed'] and not result['ai_admission']
        before=(original(s)/'submit-attempt.json').read_bytes()
        assert json.loads(before)['outcome']=='unknown'
        with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
        assert (original(s)/'submit-attempt.json').read_bytes()==before and physical['clicks']==6
    else:
        assert result['error_code']=='INVITE_FIELD_VERIFICATION_FAILED'
        assert saved['error_code']==result['error_code']
        assert physical['clicks']==5 and not result['submit_attempted']
        assert s.grabs==['header','lookup','card','fields','fields']
        assert not (original(s)/'submit-attempt.json').exists()
