"""Architect CJ35-P1 regression: labelled synthetic pixels/OCR, real parsers.

No Windows, real model, or physical message-send acceptance claims. Fresh
observations must retain the complete image BEFORE Worker history alignment.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from PIL import ImageDraw
from test_frame_avatars import synthetic_frame, draw_avatar, row, s
from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge, identity_checkpoint_for_facts
from chejin_worker_client.models import WechatReadTarget, RpaResult
from apps.wechat_ai_customer_service.optional_plugins.vision.capture import wechat
from apps.wechat_ai_customer_service.optional_plugins.vision.capture import surface


@pytest.fixture(autouse=True)
def optional_prefix_ablation(monkeypatch):
    if os.environ.get('CHEJIN_DISABLE_IMAGE_PREFIX') == '1':
        original=surface.detect_visual_image_bubbles
        def without_prefix(*args,**kwargs):
            kwargs.pop('readable_top',None)
            return original(*args,**kwargs)
        monkeypatch.setattr(surface,'detect_visual_image_bubbles',without_prefix)


def textured_image(draw, left, top, right, bottom):
    for y in range(top, bottom, 8):
        for x in range(left, right, 8):
            tone = 35 if ((x-left+y-top)//8) % 2 else 220
            draw.rectangle((x,y,min(x+7,right-1),min(y+7,bottom-1)),fill=(tone,150,80))


def prefix_scene(old_role='customer', next_role='customer', old_type='text',
                 next_type='image', *, next_top=208, history=False, dpi=1):
    image,layout=synthetic_frame();draw=ImageDraw.Draw(image)
    old_x=400 if old_role=='customer' else 930
    old_left=470 if old_role=='customer' else 680
    draw_avatar(image,old_x,82)
    if old_type=='image': textured_image(draw,old_left,90,old_left+200,196)
    else: draw.rectangle((old_left,90,old_left+200,195),fill=(130,220,150))
    rows=[row('顶部旧消息',109,left=old_left,right=old_left+180)]
    if history:
        image.paste('white',(470,137,1000,196))
        draw_avatar(image,400,148)
        draw.rectangle((470,148,670,188),fill=(130,220,150))
        rows.append(row('唯一已入库的历史锚点',157,left=482,right=650))
    next_x=400 if next_role=='customer' else 930
    next_left=470 if next_role=='customer' else 680
    draw_avatar(image,next_x,next_top)
    if next_type=='image': textured_image(draw,next_left,next_top,next_left+200,next_top+136)
    elif next_type=='voice':
        draw.rectangle((next_left,next_top,next_left+200,next_top+40),fill=(130,220,150))
        rows.append(row('6"',next_top+9,left=next_left+12,right=next_left+62))
    else:
        draw.rectangle((next_left,next_top,next_left+200,next_top+40),fill=(130,220,150))
        rows.append(row('这条完整消息必须保留',next_top+9,left=next_left+12,right=next_left+188))
    draw_avatar(image,400,430)
    rows.append(row('后续完整客户问题',439,left=470,right=800))
    if dpi!=1:
        image=image.resize((round(image.width*dpi),round(image.height*dpi)))
        layout={**layout,'dpi_scale':dpi,
                'message_viewport_bounds':[round(v*dpi) for v in layout['message_viewport_bounds']],
                'input_bounds':[round(v*dpi) for v in layout['input_bounds']]}
        rows=[{k:(v*dpi if k in {'top','bottom','left','right','center_x','center_y'} else v) for k,v in r.items()} for r in rows]
    return image,layout,rows


def observe_scene(image,layout,rows,*,label):
    original=hashlib.sha256(image.tobytes()).hexdigest()
    parsed=s.parse_messages_from_ocr(rows,image.size,target='CJTEST01',screenshot=image,layout_snapshot=layout)
    diagnostics=[]
    messages=s.merge_structural_image_messages(image,rows,parsed,target='CJTEST01',layout_snapshot=layout,image_candidate_diagnostics=diagnostics)
    assert hashlib.sha256(image.tobytes()).hexdigest()==original
    observations=s.build_message_observations_v3(messages,{'detected':False})
    payload={'messages':messages,'observations':observations,'frame_id':'synthetic-prefix',
             'top_message_fragment':s.frame_avatars.avatar_table(image,layout)['top_fragments']}
    out=os.environ.get('CHEJIN_IMAGE_PREFIX_EVIDENCE')
    if out:
        p=Path(out);p.mkdir(parents=True,exist_ok=True)
        image.save(p/(label+'.png'))
        (p/(label+'.json')).write_text(json.dumps({'fixture':'synthetic pixels and OCR rows',
            'pixel_sha256':original,'layout':layout,'ocr':rows,'table':s.frame_avatars.avatar_table(image,layout),
            'diagnostics':diagnostics,'payload':payload},ensure_ascii=False,indent=2))
    return payload,diagnostics


@pytest.mark.parametrize('old_role',['customer','self'])
@pytest.mark.parametrize('next_role',['customer','self'])
@pytest.mark.parametrize('old_type',['text','image'])
@pytest.mark.parametrize('next_type',['text','voice','image'])
def test_top_fragment_preserves_complete_next_message_and_tail(old_role,next_role,old_type,next_type):
    image,layout,rows=prefix_scene(old_role,next_role,old_type,next_type)
    table=s.frame_avatars.avatar_table(image,layout)
    assert table['top_fragments'] and table['readable_top']<208
    payload,diag=observe_scene(image,layout,rows,label=f'{old_role}-{next_role}-{old_type}-{next_type}')
    assert [m['type'] for m in payload['messages']]==[next_type,'text']
    assert [m['sender_role'] for m in payload['messages']]==[next_role,'customer']
    assert payload['messages'][-1]['content']=='后续完整客户问题'
    assert all('顶部旧消息' not in m.get('content','') for m in payload['messages'])
    assert any(d['event']=='image_candidate_top_prefix_excluded' for d in diag)


@pytest.mark.parametrize('role',['customer','self'])
@pytest.mark.parametrize('gap',[2,6,12,24])
@pytest.mark.parametrize('dpi',[1,1.25,1.5])
def test_dense_same_side_image_keeps_original_coordinates(role,gap,dpi):
    image,layout,rows=prefix_scene(role,role,'image','image',next_top=196+gap,dpi=dpi)
    payload,_=observe_scene(image,layout,rows,label=f'dense-{role}-{gap}-{dpi}')
    assert [m['type'] for m in payload['messages']]==['image','text']
    picture=payload['messages'][0]
    assert picture['sender_role']==role
    bounds=picture['bounds']
    expected=[(470 if role=='customer' else 680)*dpi,(196+gap)*dpi,
              (670 if role=='customer' else 880)*dpi,(332+gap)*dpi]
    overlap=max(0,min(bounds[2],expected[2])-max(bounds[0],expected[0]))*max(0,min(bounds[3],expected[3])-max(bounds[1],expected[1]))
    assert overlap/((expected[2]-expected[0])*(expected[3]-expected[1]))>.95,(bounds,expected)
    assert picture['image_physical_anchor']['bubble_visual_fingerprint']==wechat.image_bubble_visual_fingerprint(image,bounds)


def test_fresh_image_observation_survives_existing_history(harness):
    image,layout,rows=prefix_scene(history=True,next_top=244)
    payload,_=observe_scene(image,layout,rows,label='history-image-text')
    assert [m['type'] for m in payload['messages']]==['text','image','text'],payload
    api=FakeApi(None);bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
    runner,_=harness.make_runner(api,bridge)
    checkpoint=identity_checkpoint_for_facts('conv-prefix',[{'content':'顶部旧消息'},{'content':'唯一已入库的历史锚点'}])
    target=WechatReadTarget(conversation_id='conv-prefix',display_name='CJTEST01',remark_code='CJTEST01',
        rpa_session_key='test',authorization_revision='revision-prefix',raw={'identity_checkpoint':checkpoint})
    prepared,errors=runner._align_initial_identity_frame(target=target,sidecar_payload=payload,read_run_id='fresh-prefix')
    assert not errors,errors
    suffix=prepared['sequence_alignment_evidence']['new_suffix_observation_ids']
    assert suffix==[o['observation_id'] for o in payload['observations'][1:]],prepared
    assert [o['message_type'] for o in prepared['observations']]==['text','image','text']
    assert not api.message_payloads and not bridge.sent_replies


def test_dense_history_candidate_is_rejected_instead_of_absorbing_text():
    image,layout,rows=prefix_scene(history=True)
    parsed=s.parse_messages_from_ocr(rows,image.size,target='CJTEST01',screenshot=image,layout_snapshot=layout)
    with pytest.raises(RuntimeError,match='C2_IMAGE_OBSERVATION_FAILED') as error:
        s.merge_structural_image_messages(image,rows,parsed,target='CJTEST01',layout_snapshot=layout)
    payload=s.exception_payload_for_sidecar(error.value)
    assert payload['error_code']=='C2_IMAGE_OBSERVATION_FAILED'
    assert payload['avatar_evidence']['reason']=='image_candidate_spans_multiple_avatar_rows'
    assert len(payload['avatar_evidence']['avatar_table']['components'])==3


@pytest.mark.parametrize('invalid',[99,801,137.5,float('nan'),'137'])
def test_invalid_exclusion_boundary_fails_closed(invalid):
    image,layout,_=prefix_scene()
    with pytest.raises(ValueError,match='invalid_current_frame_readable_top'):
        wechat.detect_visual_image_bubbles(image,message_viewport_bounds=layout['message_viewport_bounds'],readable_top=invalid)


def public_prefix_frame(tmp_path,monkeypatch,*,dense):
    """Only screenshot/Win32/OCR boundaries are supplied; real startup + C2."""
    from test_chat_viewport_boundary import calibrated_frame
    image,_,rows=prefix_scene(history=True,next_top=208 if dense else 244)
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,59,899),fill=(225,225,225))
    draw.rectangle((60,0,373,899),fill=(235,235,235))
    draw.rectangle((374,0,999,98),fill='white')
    draw.line((374,99,999,99),fill=(200,200,200))
    draw.line((374,799,999,799),fill=(200,200,200))
    rows=[row('搜索',45,left=75,right=129),row('CJTEST01',45,left=460,right=610),*rows]
    layout,geometry=calibrated_frame(image,ocr=rows)
    assert layout['message_viewport_bounds'][1]==100,layout
    path=tmp_path/'synthetic-public-frame.png';image.save(path)
    monkeypatch.setattr(s,'capture_wechat',lambda *_a,**_k:(image,str(path)))
    monkeypatch.setattr(s,'get_window_geometry',lambda *_a,**_k:geometry)
    monkeypatch.setattr(s,'window_dpi_scale',lambda *_a,**_k:1)
    monkeypatch.setattr(s,'run_ocr',lambda *_a,**_k:[dict(r) for r in rows])
    try:
        payload=s.messages_payload(1,{},target='CJTEST01',history_load_times=0,
            confirm_target='CJTEST01',confirm_exact=True,artifact_dir=str(tmp_path/'public-report'))
    except RuntimeError as exc:
        # Same production serializer used by the CLI; never invent an error.
        payload=s.exception_payload_for_sidecar(exc)
    return s.sanitize_sidecar_contract_output(payload)


@pytest.mark.parametrize('dense',[False,True])
def test_public_messages_entry_preserves_image_or_marks_frame_error(tmp_path,monkeypatch,dense):
    payload=public_prefix_frame(tmp_path,monkeypatch,dense=dense)
    if dense:
        assert not payload['ok'] and payload['error_code']=='C2_IMAGE_OBSERVATION_FAILED',payload
    else:
        assert payload['ok'] and not payload['observation_validation_errors'],payload
        assert [x['message_type'] for x in payload['observations']]==['text','image','text']
