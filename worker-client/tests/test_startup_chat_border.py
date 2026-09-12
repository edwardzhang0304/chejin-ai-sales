"""Real saved-frame sequence and synthetic geometry; no desktop actions."""
import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image, ImageDraw
from test_chat_viewport_boundary import calibrated_frame, s

@pytest.mark.parametrize('recorded_layout', [False, True])
def test_real_group_calibration_then_customer_read(tmp_path, recorded_layout):
    root=os.environ.get('CHEJIN_CJ7_ARTIFACTS_ROOT')
    if not root:pytest.skip('Private incident frames: CHEJIN_CJ7_ARTIFACTS_ROOT')
    root=Path(root)
    before=root/'tasks/50fc6a67-4505-41a8-a596-ee486b62c534/20260912_180933/add_friend_pre_click_main_window_1789207774733.png'
    after=root/'wechat_c2/messages/20260912_181135_message-20260912_181135-fe134a73/messages_1789207896915.png'
    layout,geometry=calibrated_frame(Image.open(before).convert('RGB'),frame_id=before.stem)
    # Protect calibration independently of the second-layer border exclusion.
    assert layout['message_viewport_bounds']==[300,81,784,700]
    if recorded_layout:
        plan=json.loads((before.parent/'add_friend_entry_click_plan.json').read_text())
        m=plan['before']['planned_targets'][0]['metadata']
        layout.update(m['dynamic_layout_bounds'])
        layout['toolbar_bounds']=m['startup_calibration']['regions']['toolbar']
        assert layout['message_viewport_bounds']==[300,80,784,700]
    # Reuse the old conversation's regions, never calibrate the read frame.
    image=Image.open(after).convert('RGB')
    s._LAYOUT_SNAPSHOT_STORE.put(layout)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)]=layout['layout_snapshot_id']
    with patch.object(s,'capture_wechat',return_value=(image,str(after))),patch.object(s,'get_window_geometry',return_value=geometry),patch.object(s,'window_dpi_scale',return_value=1):
        result=s.messages_payload(1,{},target='CJ7RBTUC',history_load_times=0,confirm_target='CJ7RBTUC',confirm_exact=True,artifact_dir=str(tmp_path))
    assert result['ok'],result
    assert [m['content'].replace('\n','') for m in result['messages']]==[
        '您好，我是车金二手车的高磊，您刚咨询过二手车',
        '我通过了你的朋友验证请求，现在我们可以开始聊天了']
    table=s.frame_avatars.avatar_table(image,layout)
    assert len(table['components'])==2 and not table['unresolved']
    assert any(x['reason']=='viewport_connected_border' for x in table['excluded'])==recorded_layout
    (tmp_path/'evidence.json').write_text(json.dumps({'layout':layout,'table':table,'result':s.sanitize_sidecar_contract_output(result)},ensure_ascii=False,indent=2))

@pytest.mark.parametrize('scale',[1,1.25,1.5,2])
@pytest.mark.parametrize('width',[600,1000])
@pytest.mark.parametrize('thickness',[1,3])
def test_separator_exit_ignores_partial_bubble(scale,width,thickness):
    width=round(width*scale);height=round(400*scale);edge=round(80*scale)
    line_height=max(1,round(thickness*scale))
    image=Image.new('RGB',(width,height),(250,250,250));d=ImageDraw.Draw(image)
    d.rectangle((0,edge,width-1,edge+line_height-1),fill=(240,240,240))
    d.rectangle((round(width*.15),edge+line_height,round(width*.40),edge+round(35*scale)),fill=(238,238,240))
    actual=s.win32_ocr_layout._separator_content_start(image,left=0,right=width,edge_y=edge-1,measured_row_height=round(24*scale))
    assert actual==edge+line_height

@pytest.mark.parametrize('scale',[1,1.5,2])
@pytest.mark.parametrize('attached',[False,True])
def test_border_exclusion_requires_no_inward_attachment(scale,attached):
    size=(round(600*scale),round(500*scale))
    image=Image.new('RGB',size,(250,250,250));d=ImageDraw.Draw(image)
    left,top,right,bottom=[round(v*scale) for v in (100,80,600,420)]
    stroke=max(1,round(2*scale))
    d.rectangle((left,top,right-1,top+stroke-1),fill=(220,220,220))
    d.rectangle((right-stroke,top,right-1,bottom-1),fill=(220,220,220))
    box=[round(v*scale) for v in (546,180,580,214)]
    d.rectangle(box,fill=(60,90,120))
    if attached:d.rectangle((box[2],box[1]+stroke,right-1,box[1]+2*stroke),fill=(60,90,120))
    layout={'valid':True,'dpi_scale':scale,'message_viewport_bounds':[left,top,right,bottom]}
    table=s.frame_avatars.avatar_table(image,layout)
    borders=[x for x in table['excluded'] if x['reason']=='viewport_connected_border']
    assert bool(borders) is not attached
    if attached:assert table['unresolved'],table
    else:assert len(table['components'])==1 and not table['unresolved']
