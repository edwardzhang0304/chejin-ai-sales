"""Original-pixel append + real OCR/CLI/Worker checks; external I/O controlled."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from test_chat_viewport_boundary import calibrated_frame, s
from test_c2_identity_gate_receipts import harness
from test_frame_avatars import synthetic_frame, draw_avatar, row
from test_image_prefix_boundary import observe_scene, textured_image

ORIGINAL_SHA='94006ee0b369eb46b69c2857c5a01a3af578efc25c66b0e9e414cf35dccfe10e'
TAIL=['我想买一个手动挡车型','这两台车图片有吗']


def compact(value):
    return ''.join(str(value).split())


def read_frame(image,layout,geometry,directory):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'input.png';image.save(path)
    s._LAYOUT_SNAPSHOT_STORE.put(layout)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)]=layout['layout_snapshot_id']
    with patch.object(s,'capture_wechat',return_value=(image,str(path))),patch.object(s,'get_window_geometry',return_value=geometry),patch.object(s,'window_dpi_scale',return_value=1):
        try:
            result=s.messages_payload(1,{},target='CJ35C76M',history_load_times=0,confirm_target='CJ35C76M',confirm_exact=True,artifact_dir=str(directory))
        except Exception as exc:
            result=s.exception_payload_for_sidecar(exc)
    # Apply the real CLI boundary before giving a result to Worker. Raw
    # parser-local message IDs are intentionally forbidden across this seam.
    result=s.sanitize_sidecar_contract_output(result)
    (directory/'evidence.json').write_text(json.dumps({'kind':'original-pixel composition, not Windows UAT',
        'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'layout':layout,
        'avatars':s.frame_avatars.avatar_table(image,layout),'result':s.sanitize_sidecar_contract_output(result)},ensure_ascii=False,indent=2),encoding='utf-8')
    return result


@pytest.fixture(scope='module')
def original(tmp_path_factory):
    index=os.environ.get('CHEJIN_CJ35_REPLAY')
    if not index:pytest.skip('Private original index required: CHEJIN_CJ35_REPLAY')
    data=json.loads(Path(index).read_text(encoding='utf-8'))
    source=next(Path(f['original_file']) for f in data['frames'] if f['sha256']==ORIGINAL_SHA)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==ORIGINAL_SHA
    image=Image.open(source).convert('RGB')
    layout,geometry=calibrated_frame(image,frame_id='margin-before')
    assert layout['message_viewport_bounds']==[392,81,784,667]
    before=read_frame(image,layout,geometry,tmp_path_factory.mktemp('original-before'))
    assert before['ok'] and len(before['messages'])==6
    assert compact(before['messages'][-1]['content'])==TAIL[-1]
    return image,layout,geometry,before


def append_two(original,gap):
    original_image,layout,geometry,_=original
    left,top,right,bottom=layout['message_viewport_bounds']
    canvas=Image.new('RGB',(780-left,bottom-top+120),original_image.getpixel((770,600)))
    canvas.paste(original_image.crop((left,top,780,bottom)),(0,0))
    # 60px is a labelled diagnostic composition, not native WeChat row height.
    for index,source_y in enumerate((402,609)):
        canvas.paste(original_image.crop((left,source_y,780,source_y+56)),(0,bottom-top+index*60))
    shift=202-(top+gap)
    image=original_image.copy()
    image.paste(canvas.crop((0,shift,canvas.width,shift+bottom-top)),(left,top))
    for rect in ((780,0,784,844),(0,0,392,844),(392,0,784,top),(392,bottom,784,844)):
        assert image.crop(rect).tobytes()==original_image.crop(rect).tobytes()
    return image,dict(layout,frame_id=f'append-gap-{gap}'),geometry


@pytest.mark.parametrize('gap',[0,1,2,3])
def test_original_pixels_keep_complete_messages(original,tmp_path,gap):
    image,layout,geometry=append_two(original,gap)
    result=read_frame(image,layout,geometry,tmp_path)
    assert result['ok'],result
    assert not result.get('observation_validation_errors'),result
    assert [compact(m['content']) for m in result['messages'][-2:]]==TAIL
    assert [m['sender_role'] for m in result['messages'][-2:]]==['customer','customer']
    if gap:
        assert compact(result['messages'][0]['content'])=='我通过了你的朋友验证请求，现在我们可以开始聊天了'
        assert len(result['messages'])==7
        assert not result['top_message_fragment']
    else:
        assert len(result['messages'])==6 and result['top_message_fragment']


def test_real_locate_cli_reads_near_top_frame(original,tmp_path):
    from chejin_worker_client.rpa_bridge import RpaBridge
    image,layout,geometry=append_two(original,1)
    s._LAYOUT_SNAPSHOT_STORE.put(layout)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)]=layout['layout_snapshot_id']
    image_path=tmp_path/'input.png';image.save(image_path)
    ocr=s.run_ocr(image)
    calls=[]
    probe={'visible_main_windows':[{'hwnd':1,'title':'WeChat','geometry':geometry}]}
    def transport(args,**kwargs):
        output=io.StringIO()
        with patch.object(sys,'argv',[s.__file__,*args]),contextlib.redirect_stdout(output):
            code=s.main()
        result=json.loads(output.getvalue())
        calls.append({'argv':args,'exit_code':code,'result':result})
        return result
    bridge=RpaBridge();bridge.mode='win32_ocr'
    with contextlib.ExitStack() as stack:
        for name,value in [('configure_dpi_awareness',None),('activate_window',None),('humanized_action_sleep',None),
                           ('ensure_visible_wechat_window',probe),('calibrated_business_window_binding',{'ok':True}),
                           ('build_c2_window_context',{}),('get_window_geometry',geometry),('window_dpi_scale',1)]:
            stack.enter_context(patch.object(s,name,return_value=value))
        stack.enter_context(patch.object(s,'_WIN32_IMPORT_ERROR',None))
        stack.enter_context(patch.object(s,'locate_chat_target_for_c2',return_value={'ok':True,'opened':True,'guard':{'ok':True,'geometry':geometry},
            '_chat_fact_seed':{'screenshot':image,'screenshot_path':str(image_path),'ocr_items':ocr}}))
        stack.enter_context(patch.object(bridge,'_call_omniauto_process',side_effect=transport))
        result=bridge.locate_chat(display_name='CJ35C76M',rpa_session_key='',remark_code='CJ35C76M',target_mode='current',capture_initial_messages=True)
    (tmp_path/'cli-evidence.json').write_text(json.dumps({'boundary':'real Bridge/CLI/OCR; in-process transport and OS/target confirmation controlled','calls':calls},ensure_ascii=False,indent=2),encoding='utf-8')
    assert len(calls)==1 and '--capture-initial-messages' in calls[0]['argv']
    assert calls[0]['exit_code']==0 and result['ok'],result
    assert [compact(m['content']) for m in result['initial_messages_snapshot']['messages'][-2:]]==TAIL


@pytest.mark.parametrize('has_history',[False,True])
def test_worker_frame_admission_and_unchanged_history_guard(original,tmp_path,harness,has_history):
    from test_task_runner import FakeApi,FakeBridge,identity_checkpoint,identity_checkpoint_for_facts
    from chejin_worker_client.models import Binding,WechatReadTarget,RpaResult
    from chejin_worker_client.storage import save_binding,load_binding,load_runtime_control
    image,layout,geometry=append_two(original,1)
    current=read_frame(image,layout,geometry,tmp_path/'after')
    assert current['ok'],current
    # Previous truth comes from separate real OCR of the unchanged original;
    # never fabricate history by slicing the current frame.
    checkpoint=(identity_checkpoint_for_facts('conv-margin',original[3]['messages'])
                if has_history else identity_checkpoint())
    api=FakeApi(None);bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
    if has_history:
        api.message_ingest_read_completion={'result':'retry_required','error_code':'C2_UNREAD_RESULT_INCONCLUSIVE'}
    bridge.get_messages_payloads=[current]
    runner,_=harness.make_runner(api,bridge)
    binding=Binding('worker-test','test-token','instance-test',run_status='running')
    runner.binding=binding;save_binding(binding)
    target=WechatReadTarget(conversation_id='conv-margin',display_name='CJ35C76M',remark_code='CJ35C76M',rpa_session_key='test',authorization_revision='revision-conv-margin',unread_generation=1,raw={'identity_checkpoint':checkpoint})
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=False,wait_for_brain=False)
    evidence={'boundary':'real OCR, Worker, SQLite; API and physical UI controlled','checkpoint':checkpoint,'result':result,
              'operations':bridge.c2_operation_order,'ingest_payloads':api.message_payloads,
              'flow_events':api.inflight_flow_events,'runtime_control':load_runtime_control(),'stored_status':load_binding().run_status}
    (tmp_path/'worker-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    assert not result.get('worker_faulted'),result
    assert load_binding().run_status=='running'
    assert len(api.message_payloads)==1
    if has_history:
        # This independent real-OCR pair changes one pre-existing AI glyph:
        # 另一台 -> 另-台. Do not repair its history or loosen that separate
        # identity guard just to make an end-to-end reply assertion pass.
        assert '另一台' in compact(original[3]['messages'][-2]['content'])
        assert '另-台' in compact(current['messages'][-4]['content'])
        assert result['error_code']=='MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'
        assert api.message_payloads[0]['messages']==[]
        assert any(':retry_required:' in event for event in api.inflight_flow_events)
    else:
        assert result['ok'],result
        assert len(api.message_payloads[0]['messages'])==7
        assert [compact(m['content']) for m in api.message_payloads[0]['messages'][-2:]]==TAIL
    assert not bridge.sent_replies
    assert not load_runtime_control()['inflight_flow_id']


@pytest.mark.parametrize('scale',[1,1.5,2])
@pytest.mark.parametrize('role',['customer','self'])
@pytest.mark.parametrize('next_type',['text','voice','image'])
def test_near_top_keeps_following_complete_media(scale,role,next_type):
    image,layout=synthetic_frame(dpi=scale)
    left=470 if role=='customer' else 680
    x=400 if role=='customer' else 930
    top=layout['message_viewport_bounds'][1]
    draw_avatar(image,x,(top+1)/scale,dpi=scale)
    d=ImageDraw.Draw(image)
    d.rectangle((round(left*scale),top+round(8*scale),round((left+170)*scale),top+round(36*scale)),fill=(130,220,150))
    rows=[row('顶部完整旧消息',top/scale+8,left=left+10,right=left+160)]
    draw_avatar(image,x,300,dpi=scale)
    if next_type=='image':textured_image(d,round(left*scale),round(300*scale),round((left+200)*scale),round(436*scale))
    else:
        d.rectangle((round(left*scale),round(300*scale),round((left+200)*scale),round(340*scale)),fill=(130,220,150))
        rows.append(row('6"' if next_type=='voice' else '下一条完整文字',309,left=left+12,right=left+(62 if next_type=='voice' else 180)))
    draw_avatar(image,400,550,dpi=scale)
    rows.append(row('后续完整客户问题',559,left=470,right=800))
    rows=[{k:(v*scale if k in {'top','bottom','left','right','center_x','center_y'} else v) for k,v in r.items()} for r in rows]
    payload,_=observe_scene(image,layout,rows,label=f'near-top-{scale}-{role}-{next_type}')
    assert [m['type'] for m in payload['messages']]==['text',next_type,'text']
    assert [m['sender_role'] for m in payload['messages']]==[role,role,'customer']
    assert payload['messages'][0]['content']=='顶部完整旧消息'
    assert payload['messages'][-1]['content']=='后续完整客户问题'
