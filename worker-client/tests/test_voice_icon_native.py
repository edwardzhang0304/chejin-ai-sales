"""Real local OCR and production parser/voice selector on controlled desktops.

Voice bubble pixels come from user originals; OS/capture/layout acquisition is
not exercised. No mock replaces OCR, icon scoring, avatar association or parser.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw
import test_wechat_win32_ocr_sidecar_voice as fixture_module
sidecar=fixture_module.sidecar

FIXTURES=Path(__file__).parent/'fixtures/voice_icons'


@pytest.mark.parametrize('role',['customer','self'])
@pytest.mark.parametrize('scale',[1,1.5])
def test_real_ocr_voice_reaches_one_action_target(role,scale):
    image=Image.new('RGB',(965,852),(250,250,250))
    draw=ImageDraw.Draw(image);draw.rectangle((0,0,376,851),fill=(238,238,238))
    voice=Image.open(FIXTURES/('customer_9s.png' if role=='customer' else 'self_2s.png')).convert('RGB')
    image.paste(voice,(448,285) if role=='customer' else (680,285))
    image=image.resize((round(image.width*scale),round(image.height*scale)))
    avatar=(398,315,444,361) if role=='customer' else (900,315,946,361)
    fixture_module.WechatWin32OcrVoiceSelectionTest.draw_avatar(ImageDraw.Draw(image),tuple(round(v*scale) for v in avatar))
    fixture=fixture_module.WechatWin32OcrVoiceSelectionTest()
    fixture._semantic_layouts={};fixture._latest_semantic_layout=None
    snapshot=fixture._semantic_layout_for_image(image)
    snapshot["dpi_scale"]=scale
    with patch.object(sidecar,'layout_snapshot_for_image',return_value=snapshot):
        items=sidecar._run_chat_text_ocr(image, "voice_icon_native_test")
        try:
            messages=sidecar.parse_current_chat_frame_messages(items,image.size,target='CJVOICE1',screenshot=image)
        except sidecar.frame_avatars.AvatarEvidenceError as error:
            raise AssertionError({'items': items, 'evidence': error.evidence}) from error
        voices=[item for item in messages if item.get('type')=='voice']
        assert len(voices)==1,(items,messages)
        voice=voices[0]
        assert voice['sender_role']==role
        # Merged glyph/number OCR can be undecidable. Do not invent a duration.
        assert voice.get('voice_duration') in ({None,9} if role=='customer' else {None,2})
        observations=sidecar.build_unified_voice_observations_v3(image,items,image.size,parsed_messages=messages)
        candidates=[item for item in observations if item.get('action_target')]
        assert len(candidates)==1,observations
        assert not candidates[0].get('contract_errors'),candidates[0]
        assert candidates[0]['sender_role']==role
        assert candidates[0]['voice_state']=='untranscribed'
        assert candidates[0]['action_target']['item']['_voice_visual_evidence']['state']=='confirmed'


@pytest.mark.parametrize('with_duration_ocr',[True,False])
def test_uncertain_voice_is_retained_and_blocked_without_actions(with_duration_ocr):
    from PIL import ImageFilter
    from test_voice_icon_proof import row
    image=Image.new('RGB',(965,852),(250,250,250))
    voice=Image.open(FIXTURES/'customer_9s.png').convert('RGB').filter(ImageFilter.GaussianBlur(1.6))
    image.paste(voice,(448,285))
    fixture_module.WechatWin32OcrVoiceSelectionTest.draw_avatar(ImageDraw.Draw(image),(398,315,444,361))
    fixture=fixture_module.WechatWin32OcrVoiceSelectionTest()
    fixture._semantic_layouts={};fixture._latest_semantic_layout=None
    snapshot=fixture._semantic_layout_for_image(image)
    items=[row('9"',[537,327,568,350])] if with_duration_ocr else []
    with patch.object(sidecar,'layout_snapshot_for_image',return_value=snapshot), \
         patch.object(sidecar,'run_ocr',side_effect=AssertionError('no OCR retry permitted here')):
        messages=sidecar.parse_current_chat_frame_messages(items,image.size,target='CJVOICE1',screenshot=image)
        uncertain=[m for m in messages if 'voice_icon_unconfirmed' in m.get('quality_flags',[])]
        assert len(uncertain)==1,messages
        proof=uncertain[0]['_voice_visual_evidence']
        assert proof['review_count']==1 and proof['icon_score']<proof['icon_threshold']
        observations=sidecar.build_message_observations_v3(messages)
        assert any('OBSERVATION_VOICE_ICON_UNCONFIRMED' in o.get('contract_errors',[]) for o in observations)
        assert all(not o['content_clean'] for o in observations if 'voice_icon_unconfirmed' in o.get('quality_flags',[]))
        if not with_duration_ocr:
            assert all(not o['content_raw'] for o in observations)
        assert not any(o.get('action_target') for o in sidecar.build_unified_voice_observations_v3(image,items,image.size,parsed_messages=messages))


def test_real_budget_pixels_reach_worker_sqlite_and_ingest(harness):
    """Real OCR/parser/Worker/SQLite; controlled API receipt, no actual send."""
    from test_task_runner import FakeApi,FakeBridge,identity_checkpoint_for_facts
    from chejin_worker_client.models import Binding,WechatReadTarget,RpaResult
    from chejin_worker_client.storage import save_binding,load_runtime_control
    image=Image.new('RGB',(965,852),(250,250,250))
    for name,top in [('text_15w.png',300),('text_fuel.png',390)]:
        image.paste(Image.open(FIXTURES/name).convert('RGB'),(458,top))
        fixture_module.WechatWin32OcrVoiceSelectionTest.draw_avatar(ImageDraw.Draw(image),(398,top,444,top+46))
    fixture=fixture_module.WechatWin32OcrVoiceSelectionTest()
    fixture._semantic_layouts={};fixture._latest_semantic_layout=None
    snapshot=fixture._semantic_layout_for_image(image)
    with patch.object(sidecar,'layout_snapshot_for_image',return_value=snapshot):
        items=sidecar._run_chat_text_ocr(image,'voice_budget_native_test')
        messages=sidecar.parse_current_chat_frame_messages(items,image.size,target='CJVOICE1',screenshot=image)
        observations=sidecar.build_message_observations_v3(messages)
        assert [o['content_clean'] for o in observations]==['15w','油车']
        assert all(o['message_type']=='text' for o in observations)
        assert not sidecar.build_unified_voice_observations_v3(image,items,image.size,parsed_messages=messages)
    api=FakeApi(None);bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
    bridge.get_messages_payloads=[{'ok':True,'frame_id':'budget-original-crops','observation_schema_version':3,'observations':observations}]
    runner,_=harness.make_runner(api,bridge)
    binding=Binding('worker-test','test-token','instance-test',run_status='running')
    runner.binding=binding;save_binding(binding)
    target=WechatReadTarget(conversation_id='voice-budget-probe',display_name='CJVOICE1',remark_code='CJVOICE1',rpa_session_key='test-session',authorization_revision='revision-voice-budget-probe',unread_generation=1,
                           raw={'identity_checkpoint':identity_checkpoint_for_facts('voice-budget-probe',[])})
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=False,wait_for_brain=False)
    assert result.get('ok'),result
    sent=[m for payload in api.message_payloads for m in payload.get('messages',[])]
    assert [m['content'] for m in sent]==['15w','油车'],sent
    assert all(m['message_type']=='text' for m in sent)
    assert not load_runtime_control()['inflight_flow_id']
    assert not bridge.sent_replies


# Reuse the existing isolated SQLite fixture, never the user's database.
from test_c2_identity_gate_receipts import harness
