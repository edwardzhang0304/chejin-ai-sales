"""Avatar input isolation: synthetic geometry, explicit real-frame replay separately."""
import copy
import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from test_avatar_object_classification import bubble_frame, parse
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import avatar_text_input as m


def synthetic(role="customer", dpi=1.0, **kwargs):
    raw, layout, rows = bubble_frame(role=role, dpi=dpi, **kwargs)
    layout.update(image_width=raw.width, image_height=raw.height, frame_id="synthetic-frame")
    return raw, layout, rows


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("dpi", [1.0, 1.25, 1.5])
def test_only_avatar_pixels_change_and_original_role_survives(role, dpi):
    raw, layout, rows = synthetic(role, dpi)
    original = np.asarray(raw).copy()
    table = s.frame_avatars.avatar_table(raw, layout)
    derived, info = m.prepare(raw, layout)
    assert np.array_equal(original, np.asarray(raw))
    assert derived is not raw and derived.size == raw.size
    allowed = np.zeros(original.shape[:2], dtype=bool)
    for l, t, r, b in info["rectangles"]:
        allowed[t:b, l:r] = True
    assert np.array_equal(np.asarray(derived)[~allowed], original[~allowed])
    assert np.any(np.asarray(derived)[allowed] != original[allowed])
    assert s.frame_avatars.avatar_table(raw, layout) is table
    assert s.layout_snapshot_for_image(derived) is None
    assert not hasattr(derived, "_chejin_frame_avatars")
    messages = parse(raw, layout, rows)
    assert len(messages) == 1 and messages[0]["sender_role"] == role
    assert messages[0]["content"] == rows[0]["text"]


def test_real_body_letters_and_adjacent_pixels_are_not_removed_by_word():
    raw, layout, rows = synthetic()
    rows[0]["text"] = "UNI ABC 123"
    derived, info = m.prepare(raw, layout)
    b = rows[0]
    box = (b["left"], b["top"], b["right"], b["bottom"])
    assert derived.crop(box).tobytes() == raw.crop(box).tobytes()
    assert parse(raw, layout, rows)[0]["content"] == "UNI ABC 123"
    for l, t, r, bottom in info["rectangles"]:
        assert derived.getpixel((r, t)) == raw.getpixel((r, t))
        assert derived.getpixel((l, bottom)) == raw.getpixel((l, bottom))


def test_no_confirmed_avatar_keeps_pixels_and_invalid_layout_is_not_empty_success():
    raw, layout, _ = synthetic(variant="missing")
    derived, info = m.prepare(raw, layout)
    assert not info["rectangles"] and derived.tobytes() == raw.tobytes()
    with pytest.raises(s.frame_avatars.AvatarEvidenceError):
        m.prepare(raw, {**layout, "invalidated": True})


def test_cache_provenance_rejects_raw_copied_modified_and_wrong_frame_results():
    raw, layout, rows = synthetic()
    _, info = m.prepare(raw, layout)
    certified = m.record(raw, rows, info, ["full_frame"])
    assert m.matches(raw, layout, certified)
    assert not m.matches(raw, layout, list(certified))
    assert not m.matches(raw, {**layout, "frame_id": "different"}, certified)
    assert not m.matches(raw, {**layout, "layout_snapshot_id": "different"}, certified)
    assert not m.matches(raw, {**layout, "dpi_scale": 1.5}, certified)
    changed = raw.copy(); changed.putpixel((0, 0), (0, 0, 0))
    assert not m.matches(changed, layout, certified)
    changed_rows = copy.deepcopy(certified); changed_rows[0]["text"] += "x"
    assert not m.matches(raw, layout, changed_rows)
    changed_source = copy.deepcopy(certified); changed_source.provenance["regions"] = ["other_roi"]
    assert not m.matches(raw, layout, changed_source)
    assert m.cache_suffix(certified) != m.cache_suffix(list(certified))


@pytest.mark.parametrize("enabled,expected", [(False, 1), (True, 3)])
def test_ordinary_full_and_roi_keep_original_call_budget(enabled, expected):
    raw, layout, _ = synthetic()
    # This fixture supplies the existing semantic shell, not OCR or avatar outputs.
    layout.update(chat_header_bounds=[348, 0, raw.width, 110])
    calls = []
    def ocr(image):
        calls.append(image)
        return []
    with patch.object(s, "layout_snapshot_for_image", return_value=layout), patch.object(s, "run_ocr", side_effect=ocr):
        rows, plan = s.run_ocr_for_chat_fact_frame(raw, purpose="test", source="test", enabled=enabled)
    assert len(calls) == plan["ocr_call_count"] == expected
    assert m.matches(raw, layout, rows)
    assert all(image is not raw for image in calls)
    if enabled:
        assert not m.matches(raw,{**layout,'input_bounds':[1,2,3,4]},rows)
        assert not m.matches(raw,{**layout,'chat_header_bounds':None},rows)


def test_pure_target_diagnostic_can_run_without_avatar_and_cannot_certify_body():
    raw, layout, _ = synthetic()
    with patch.object(s, "layout_snapshot_for_image", return_value=None), patch.object(s, "run_ocr", return_value=[]) as ocr:
        rows = s._run_chat_text_ocr(raw, "target", diagnostic_fallback=True)
    ocr.assert_called_once_with(raw)
    assert not m.matches(raw, layout, rows)


@pytest.fixture(scope="module")
def real_frames(tmp_path_factory):
    root = os.environ.get("CHEJIN_AVATAR_MASK_INCIDENT_ROOT")
    if not root:
        pytest.skip("Private original incident PNG/report required")
    directory = Path(root) / "bundle/artifacts/wechat_c2/messages"
    results = {}
    for name, prefix in [("before", "20260919_111002"), ("after", "20260919_111201")]:
        report_path = next(directory.glob(prefix + "*/wechat_messages_frame_review.json"))
        report = json.loads(report_path.read_text())
        details = next(e["result"] for e in report["events"] if "message_ocr_items" in e.get("result", {}))
        target = next(e["result"]["confirmed_target"] for e in report["events"] if "confirmed_target" in e.get("result", {}))
        from PIL import Image
        png = next(report_path.parent.glob("send_guard_*.png"))
        raw = Image.open(png).convert("RGB"); layout = details["layout_snapshot"]
        s._LAYOUT_SNAPSHOT_STORE.put(layout)
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(raw)] = layout["layout_snapshot_id"]
        rect = layout["window_rect"]
        geometry = dict(zip(("left", "top", "right", "bottom"), rect))
        geometry.update(width=rect[2]-rect[0], height=rect[3]-rect[1])
        output = tmp_path_factory.mktemp("avatar-mask-" + name)
        disabled = patch.object(m, 'prepare', side_effect=lambda image, current_layout: (image, m.provenance(image,current_layout))) if os.environ.get('AVATAR_MASK_DISABLE_PREPROCESSING')=='1' else nullcontext()
        with disabled, patch.object(s, "capture_wechat", return_value=(raw, str(png))), patch.object(s, "get_window_geometry", return_value=geometry), patch.object(s, "window_dpi_scale", return_value=1):
            result = s.messages_payload(1, {}, target=target, confirm_target=target,
                history_load_times=0, artifact_dir=str(output))
        assert result["ok"], result
        assert not result["observation_validation_errors"]
        results[name] = (result, raw, layout, png)
    return results


def test_original_png_through_real_ocr_and_formal_messages(real_frames):
    before, after = real_frames["before"][0], real_frames["after"][0]
    assert len(before["observations"]) == 5
    assert len(after["observations"]) == 6
    assert [x["sender_role"] for x in after["observations"]] == ["self", "customer", "self", "customer", "self", "customer"]
    assert after["observations"][0]["content_clean"] == before["observations"][1]["content_clean"]
    assert all("UNI" not in x["content_clean"] for x in after["observations"])
    assert after["frame_observation"]["screenshot_path"] == str(real_frames["after"][3])
    assert "avatar_mask_v1" in after["frame_observation"]["ocr_cache_key"]


def test_disable_preprocessing_reproduces_original_avatar_split(real_frames):
    _, raw, layout, _ = real_frames["after"]
    # Genuine original OCR; no injected text, role or pre-built messages.
    with patch.object(m, "prepare", side_effect=lambda image, layout: (image, m.provenance(image, layout))):
        rows = s._run_chat_text_ocr(raw, "ablation")
    messages = s.parse_messages_from_ocr(rows, raw.size, target="replay", screenshot=raw, layout_snapshot=layout)
    assert len(messages) == 7 and any(x["content"] == "UNI\n车" for x in messages)


def geometry_for(layout):
    l,t,r,b=layout['window_rect']
    return dict(left=l,top=t,right=r,bottom=b,width=r-l,height=b-t)


@pytest.mark.parametrize('source,expected_calls', [('roi',3),('full',1),('supplied',0),('legacy',1),('wrong_frame',1)])
def test_send_snapshot_sources_keep_budget_and_all_facts(real_frames, monkeypatch, source, expected_calls):
    payload, raw, layout, png = real_frames['after']
    target=payload['target_confirmation']['confirmed_target']
    monkeypatch.setenv('CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED','0' if source=='full' else '1')
    rows=None
    if source in {'supplied','legacy','wrong_frame'}:
        image=real_frames['before'][1] if source=='wrong_frame' else raw
        rows=s._run_chat_text_ocr(image,'test_seed')
        if source=='legacy':rows=list(rows)
    with patch.object(s,'get_window_geometry',return_value=geometry_for(layout)), patch.object(s,'window_dpi_scale',return_value=1), patch.object(s,'run_ocr',wraps=s.run_ocr) as ocr, patch.object(s,'capture_wechat') as capture:
        result=s.build_send_fact_snapshot_from_frame(1,target=target,text='',exact=True,artifact_dir=None,
            label='mask-'+source,screenshot=raw,screenshot_path=str(png),ocr_items=rows)
    assert result['ok'],result
    assert len(result['observations'])==6
    assert all('UNI' not in o.get('content_clean','') for o in result['observations'])
    assert ocr.call_count==expected_calls
    capture.assert_not_called()
    assert result['frame_observation']['screenshot_path']==str(png)


@pytest.mark.parametrize('legacy', [False,True])
def test_target_seed_consumed_once_without_recapture(real_frames, legacy):
    payload,raw,layout,png=real_frames['after']
    target=payload['target_confirmation']['confirmed_target'];geometry=geometry_for(layout)
    rows=s._run_chat_text_ocr(raw,'seed_origin')
    if legacy:rows=list(rows)
    args=dict(hwnd=1,target=target,exact=True,geometry=geometry)
    s.remember_target_ready_prevalidation_ocr_seed(**args,screenshot=raw,ocr_items=rows,screenshot_path=str(png))
    with patch.object(s,'get_window_geometry',return_value=geometry), patch.object(s,'run_ocr',wraps=s.run_ocr) as ocr, patch.object(s,'capture_wechat') as capture:
        seed=s.consume_target_ready_prevalidation_ocr_seed(**args,ttl_seconds=180)
        assert seed is not None
        assert m.matches(raw,layout,seed['ocr_items'])
        assert s.consume_target_ready_prevalidation_ocr_seed(**args,ttl_seconds=180) is None
    assert ocr.call_count==int(legacy)==seed['ocr_call_count']
    capture.assert_not_called()


def test_enhanced_region_uses_same_mask_then_existing_scale_and_mapping():
    from PIL import ImageEnhance, Image
    raw,layout,_=synthetic()
    avatar=s.frame_avatars.avatar_table(raw,layout)['components'][0]['bounds']
    expected,_=m.prepare(raw,layout)
    l,t,r,b=avatar
    expected=expected.crop((l-4,t-4,r+4,b+4))
    expected=ImageEnhance.Contrast(expected).enhance(1.55)
    expected=ImageEnhance.Sharpness(expected).enhance(1.45)
    expected=expected.resize((expected.width*2,expected.height*2),Image.Resampling.LANCZOS)
    calls=[]
    def engine(image):
        calls.append(image)
        assert image.tobytes()==expected.tobytes()
        return [dict(text='test',left=10,top=10,right=20,bottom=20,center_x=15,center_y=15)]
    with patch.object(s,'layout_snapshot_for_image',return_value=layout):
        rows=s.enhanced_ocr_items_for_structural_chat_candidate(raw,avatar,ocr_runner=engine)
    assert len(calls)==1 and rows[0]['left']==l+1 and rows[0]['top']==t+1


def test_real_ocr_keeps_same_letters_inside_message_bubble():
    from PIL import ImageDraw, ImageFont
    # Synthetic pixels; the model really reads these letters, not seeded rows.
    raw,layout,_=synthetic(size=110)
    font_path=os.environ.get('AVATAR_MASK_TEST_FONT')
    font=ImageFont.truetype(font_path,22) if font_path else ImageFont.load_default(size=22)
    ImageDraw.Draw(raw).text((419,333),'UNI ABC123',font=font,fill=(20,20,20))
    with patch.object(s,'layout_snapshot_for_image',return_value=layout):
        rows=s._run_chat_text_ocr(raw,'synthetic_real_letters')
    result=parse(raw,layout,rows)
    assert len(result)==1 and result[0]['sender_role']=='customer'
    assert result[0]['content'].replace(' ','')=='UNIABC123'


@contextmanager
def verified_frame_io(raw, layout, png):
    """Only desktop geometry/capture/registered layout are controlled."""
    import uuid
    def register(hwnd, image, **kwargs):
        current={**copy.deepcopy(layout),'layout_snapshot_id':str(uuid.uuid4()),'frame_id':str(uuid.uuid4()),'executable':True}
        s._LAYOUT_SNAPSHOT_STORE.put(current)
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)]=current['layout_snapshot_id']
        return current
    with patch.object(s,'capture_wechat',return_value=(raw,str(png))) as capture, patch.object(s,'get_window_geometry',return_value=geometry_for(layout)), patch.object(s,'get_window_client_geometry',return_value={'screen_left':0,'screen_top':0}), patch.object(s,'window_dpi_scale',return_value=1), patch.object(s,'_register_layout_snapshot',side_effect=register):
        yield capture


@pytest.fixture(scope='module',params=[False,True],ids=['full','roi'])
def recheck_frames(request,real_frames,tmp_path_factory):
    import shutil
    payload,raw,layout,source=real_frames['after']
    target=payload['target_confirmation']['confirmed_target']
    out=tmp_path_factory.mktemp('local-recheck')
    png=out/'original.png';shutil.copyfile(source,png)
    with verified_frame_io(raw,layout,png):
        result=s.messages_payload(1,{},target=target,confirm_target=target,history_load_times=0,
            artifact_dir=str(out),chat_fact_roi_ocr=request.param,retain_text_recheck_frame=True)
    assert result['ok'],result
    path=Path(result['text_recheck_frame_path'])
    frame=json.loads(path.read_text())
    assert frame['text_ocr_preprocessing']['provenance']['method']=='avatar_mask_v1'
    return result,raw,layout,png,frame


def run_local_recheck(case,tmp_path,stage,mutate=None):
    import argparse,hashlib
    payload,raw,layout,png,original_frame=case
    original=copy.deepcopy(payload);frame=copy.deepcopy(original_frame)
    if mutate:mutate(frame)
    path=Path(original['text_recheck_frame_path'])
    path.write_text(json.dumps(frame,ensure_ascii=False))
    original['text_recheck_frame_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    if stage=='validate':
        original.pop('text_recheck_frame_path',None)
        original.pop('text_recheck_frame_sha256',None)
    request={'stage':stage,'payload':original,'observation_ids':[original['observations'][-1]['observation_id']]}
    request_path=tmp_path/'request.json';request_path.write_text(json.dumps(request,ensure_ascii=False))
    target=original['target_confirmation']['confirmed_target']
    args=argparse.Namespace(artifact_dir=str(tmp_path/'result'),text_recheck_request=str(request_path),target=target,remark_code=target)
    with verified_frame_io(raw,layout,png) as capture, patch.object(s,'run_ocr',wraps=s.run_ocr) as ocr:
        result=s.replay_text_bubble_request(1,{},args)
    return result,capture.call_count,ocr.call_count


def test_validate_needs_no_local_ocr_source_or_file(recheck_frames,tmp_path):
    result,captures,calls=run_local_recheck(recheck_frames,tmp_path,'validate',lambda f:f.pop('text_ocr_preprocessing'))
    assert result['ok'],result
    assert captures==calls==0


def test_real_local_ocr_merges_with_verified_base_after_new_layout_ids(recheck_frames,tmp_path):
    result,captures,calls=run_local_recheck(recheck_frames,tmp_path,'ocr')
    assert result['ok'],result
    assert captures==0 and calls==1
    original=recheck_frames[0]
    assert [o['content_clean'] for o in result['observations']]==[o['content_clean'] for o in original['observations']]
    assert result['frame_observation']['screenshot_sha256']==original['frame_observation']['screenshot_sha256']


@pytest.mark.parametrize('defect',['missing','method','pixels','size','viewport','dpi','rectangles','regions','rows'])
def test_old_or_inconsistent_saved_ocr_is_rejected_before_any_new_ocr(recheck_frames,tmp_path,defect):
    def change(frame):
        if defect=='missing':frame.pop('text_ocr_preprocessing');return
        source=frame['text_ocr_preprocessing']['provenance']
        if defect=='rows':frame['ocr_items'][0]['text']='changed';return
        key={'method':'method','pixels':'raw_rgb_sha256','size':'image_size','viewport':'viewport','dpi':'dpi_scale','rectangles':'rectangles','regions':'regions'}[defect]
        source[key]={'method':'raw_ocr','pixels':'0'*64,'size':[1,2],'viewport':[0,0,1,1],'dpi':2,'rectangles':[],'regions':['unknown']}[defect]
    result,captures,calls=run_local_recheck(recheck_frames,tmp_path,'ocr',change)
    assert result['ok'] is False and result['reason'].startswith('text_recheck_preprocessing_'),result
    assert captures==calls==0
