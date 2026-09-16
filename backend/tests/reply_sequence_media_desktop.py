"""Pixel fixture from the 2026-09-16 engineer 11 media probe.

Actual OCR/Worker/HTTP/PG media interruption probe; OS and models controlled.

No successful receipt/identity/settlement is injected. Image process_image_slot
and voice prepare/execute are the production entry points. These fixtures are
synthetic desktop scenes, not Windows acceptance evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

import importlib
ROOT = Path(__file__).resolve().parents[2]
fixture_dir = os.environ.get("CHEJIN_SEQUENCE_DESKTOP_FIXTURE")
if not fixture_dir:
    pytest.skip("requires explicitly supplied private desktop fixture", allow_module_level=True)
sys.path.append(fixture_dir)
fixture = importlib.import_module("dynamic_composer_desktop")

OLD_REPLY = "好的，我帮您看看"
NEW_REPLY = "好的，我按新信息重新推荐。"
TRANSCRIPT = "我的预算改成十五万元了"
VOICE_DURATION_TEXT = '5"'
IMAGE_SUMMARY = "客户发来车辆外观图片"
BASE_FRAMES = fixture.derived_frames
FONT = "/System/Library/Fonts/STHeiti Light.ttc"


def record_checkpoint_comparisons(monkeypatch, runner, output):
    original = runner._compare_pre_send_fact_checkpoint_frame
    calls = []
    def observed(**kwargs):
        result = original(**kwargs)
        calls.append({'checkpoint': kwargs['target'].raw.get('pre_send_fact_checkpoint_context'),
            'input': kwargs['sidecar_payload'], 'comparison_only': kwargs.get('comparison_only', False),
            'comparison': result[1]})
        (output/'checkpoint-comparisons.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2,default=str))
        return result
    monkeypatch.setattr(runner, '_compare_pre_send_fact_checkpoint_frame', observed)


def media_frames(kind):
    calibration, frames = BASE_FRAMES(movement=0, reduction=0, reply=OLD_REPLY, final_customer=True)
    font = ImageFont.truetype(FONT, 16)
    avatar = frames['before'].crop((320,520,356,556))
    for label in ('typing', 'cleared'):
        frame = frames[label]
        frame.paste(frames['before'].crop((301,81,778,700)), (301,81))
        draw = ImageDraw.Draw(frame)
        if kind == 'image':
            draw.rectangle((301,81,778,699), fill=(250,250,250))
            frame.paste(frames['before'].crop((301,191,778,700)), (301,81))
            draw.rectangle((301,590,778,699), fill=(250,250,250))
            frame.paste(avatar, (320,550))
            with Image.open(ROOT/'website/assets/vehicles/vehicle-02.jpg') as source:
                frame.paste(source.convert('RGB').resize((192,128)), (370,550))
        else:
            frame.paste(avatar, (320,588))
            draw.rounded_rectangle((370,588,538,628), radius=6, fill=(237,237,237))
            for r in (6,12,18):
                draw.arc((380-r,608-r,380+r,608+r), start=-55, end=55, fill=(35,35,35), width=2)
            draw.text((493,599), VOICE_DURATION_TEXT, font=font, fill=(25,25,25))
    return calibration, frames


class MediaDesktop(fixture.Desktop):
    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs, reply=OLD_REPLY)
        self.kind = kind
        self.arrived = False
        self.transcribed = False
        self.media_clicks = []
        self.menu_open = False
        self.enter_texts = []
        _, next_frames = BASE_FRAMES(movement=0, reduction=0, reply=NEW_REPLY, final_customer=True)
        self.next_input = next_frames['typing'].crop((301,700,778,844))
        self.next_bubble = next_frames['sent'].crop((310,510,768,554))

    def unicode_unit(self, unit):
        self.arrived = True
        super().unicode_unit(unit)

    def key(self, key):
        if key == 13:
            self.enter_texts.append(self.draft)
        super().key(key)

    def voice_click(self, hwnd, x, y, *, bounds, **kwargs):
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
        assert self.kind == 'voice' and self.arrived and not self.draft
        self.media_clicks.append([x,y])
        self.transcribed = True
        self.menu_open = False
        return {'ok': True}

    def right_click(self, hwnd, x, y, *, bounds, **kwargs):
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
        self.menu_open = True
        self.menu_anchor = [x,y]
        return {'ok':True,'screen_x':x,'screen_y':y}

    def observe_menu(self, hwnd, *, anchor_screen, artifact_dir=None, label='', **kwargs):
        # Controlled popup pixels and window metadata; real OCR and the
        # production menu selector consume these, not a hand-made OCR result.
        assert self.menu_open
        image=Image.new('RGB',(160,136),(250,250,250))
        draw=ImageDraw.Draw(image)
        for i,text in enumerate(('转文字','转发','收藏','删除')):
            draw.text((20,12+i*30),text,font=ImageFont.truetype(FONT,16),fill=(25,25,25))
        path=self.directory/'controlled-voice-menu.png';image.save(path)
        items=fixture.sidecar.run_ocr(image)
        return {'ok':True,'image':image,'image_size':image.size,'local_ocr_items':items,
            'screen_origin':[anchor_screen[0],anchor_screen[1]-20],'layout_snapshot_id':'controlled-popup',
            'menu_hwnd':90001,'screenshot_path':str(path),'ocr_item_count':len(items)}

    def current_image(self):
        if not self.arrived:
            return self.frames['before'].copy(), 'before'
        image = self.frames['cleared'].copy()
        if self.transcribed:
            draw = ImageDraw.Draw(image)
            draw.rectangle((548,590,624,628), fill=(250,250,250))
            draw.rounded_rectangle((370,634,650,679), radius=6, fill=(237,237,237))
            draw.text((380,646), TRANSCRIPT, font=ImageFont.truetype(FONT,14), fill=(25,25,25))
        if self.enter_count:
            chat = image.crop((301,221,778,700))
            ImageDraw.Draw(image).rectangle((301,81,778,699), fill=(250,250,250))
            image.paste(chat,(301,81))
            image.paste(self.next_bubble,(310,640))
            return image,'new_reply_sent'
        if self.draft:
            source = self.next_input if self.reply == NEW_REPLY else self.frames['typing'].crop((301,700,778,844))
            image.paste(source,(301,700))
            return image,'typing'
        return image,'media_completed' if self.transcribed else 'media_arrived'

    def capture(self, hwnd, *, artifact_dir=None, label='frame', **kwargs):
        image, state = self.current_image()
        path = self.directory/f'{len(self.captures):03d}-{label}.png'
        image.save(path)
        layout = fixture.register(image,self.calibration,path)
        self.captures.append({'label':label,'path':str(path),'state':state,'layout':layout})
        return image,str(path)


def install_native_voice_desktop(monkeypatch, desktop):
    sidecar = fixture.sidecar
    for name, value in {
        'human_window_image_click_in_bounds': desktop.voice_click,
        'human_window_image_right_click_in_bounds': desktop.right_click,
        'observe_wechat_context_menu': desktop.observe_menu,
        'wait_for_wechat_context_menu_stable': lambda: 0,
        'capture_wechat_window_visible_screen': desktop.capture,
        '_WIN32_IMPORT_ERROR': '', 'configure_dpi_awareness': lambda: None,
        'activate_window': lambda hwnd: None,
    }.items(): monkeypatch.setattr(sidecar, name, value)
    monkeypatch.setattr(sidecar.win32gui, 'IsWindow', lambda hwnd: desktop.menu_open if hwnd == 90001 else True, raising=False)
    monkeypatch.setattr(sidecar.win32gui, 'IsWindowVisible', lambda hwnd: desktop.menu_open if hwnd == 90001 else True, raising=False)
    window = {'hwnd': desktop.calibration['hwnd'], 'pid': 2188, 'class_name': 'WeChatMainWndForPC', 'visible': True}
    monkeypatch.setattr(sidecar, 'ensure_visible_wechat_window', lambda **kw: {'visible_main_windows': [window]})


def install_native_image_probe(monkeypatch, desktop, output):
    from chejin_worker_client import omniauto_vision as vision
    from chejin_worker_client.action_journal import read_action_journal
    from apps.wechat_ai_customer_service.optional_plugins.vision.plugin import BuiltinVisionPlugin
    from test_c2_vision_integration import C2VisionIntegrationTests
    original = vision.process_image_slot
    calls = []
    from chejin_worker_client import task_runner
    native_normalize = task_runner.TaskRunner._normalize_one_image_slot_result
    native_continuity = task_runner._image_action_frame_to_reread_continuity
    continuity_calls = []
    (output/'image-continuity.json').write_text('[]')
    def normalize(result):
        value = native_normalize(result)
        (output/'image-normalization.json').write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
        return value
    def continuity(*args,**kwargs):
        value = native_continuity(*args,**kwargs)
        continuity_calls.append(value)
        (output/'image-continuity.json').write_text(json.dumps(continuity_calls,ensure_ascii=False,indent=2,default=str))
        return value
    monkeypatch.setattr(task_runner.TaskRunner,'_normalize_one_image_slot_result',staticmethod(normalize))
    monkeypatch.setattr(task_runner,'_image_action_frame_to_reread_continuity',continuity)
    def config():
        return {'ready':True,'config':{'customer_image_understanding':{'enabled':True}}}
    def capture_window(context, *, phase, label):
        # Only the Windows capture port is replaced. The production Vision
        # frame parser below performs its own full-frame OCR and selection.
        image, path = desktop.capture(desktop.calibration['hwnd'], label=label)
        return {'ok':True, 'image':image, 'hwnd':desktop.calibration['hwnd'],
            'capture_mode':'controlled_pixels', 'screen_origin':[0,0],
            'layout_snapshot':fixture.sidecar.layout_snapshot_for_image(image),
            'screenshot_path':path, 'validation':{'ok':True}}
    monkeypatch.setattr(fixture.sidecar,'capture_c2_window_context',capture_window)
    def plugin_io(self, context):
        # The external Vision plugin is controlled. Use its actual native
        # capture/parser port, not messages_payload with a different OCR mode.
        payload = self._ports.window_frame.capture_frame({**context, 'phase':'image_candidate'})
        assert payload['ok'],payload
        (output/'native-image-frame.json').write_text(json.dumps(
            {k:v for k,v in payload.items() if k != 'image'},ensure_ascii=False,indent=2,default=str))
        observations = payload['observations']
        order = context['expected_business_screen_order']
        selected = [o for o in observations if o.get('row_kind')=='image_bubble']
        assert len(selected)==1,observations
        picture = ROOT/'website/assets/vehicles/vehicle-02.jpg'
        digest = hashlib.sha256(picture.read_bytes()).hexdigest()
        understanding = {'schema_version':1,'enabled':True,'applied':True,'adoptable':True,'reason':'vision_ready',
            'provider':vision.DEFAULT_VISION_BASE_URL,'request_style':vision.DEFAULT_VISION_REQUEST_STYLE,
            'model':vision.DEFAULT_VISION_MODEL,**C2VisionIntegrationTests.strict_provider_payload(IMAGE_SUMMARY),
            'audit':{'latency_ms':1,'used_fallback':False,'provider_error':'','retry_error':'',
                     'retry_after_non_json':False,'catalog_identity_candidate_count':0}}
        calls.append({'message_id':context['message_id'],'trigger_observation_id':selected[0]['observation_id'],'sha256':digest})
        return {'applied':True,'reason':'vision_ready','customer_image_understanding':understanding,
            'visual_bridge_input':{'schema_version':1,'present':True,'vision_summary':IMAGE_SUMMARY,
                'classification':{'is_vehicle':False,'vehicle_confidence':0.0,'unknown':True},
                'catalog_assist':{'normalized_vehicle_query':'','candidate_names':[],'exact_candidate_name':''},
                'intent_hints':{'wants_catalog_match':False,'wants_similar_recommendation':False,'needs_clarification':True},
                'vehicle_image_retrieval':{'matched':False,'candidate_names':[]},'source_message_ids':[context['message_id']]},
            'clipboard_transaction':{'action_phase':'confirmed','ui_action_performed':True,'current_frame_target_selected':True,
                'physical_identity_inherited_from_prepare':False,'current_frame_selection_evidence':{
                    'selection_policy':'worker_approved_current_business_occurrence','physical_identity_inherited_from_prepare':False,
                    'current_business_screen_order':order},
                'trigger_observation_id':selected[0]['observation_id'],'trigger_business_screen_order':order,
                'action_frame_observations':observations,'action_frame_layout_snapshot_id':payload.get('layout_snapshot_id','controlled-layout'),
                'image_sha256':digest,'right_click_ok':True,'menu_opened':True,'copy_click_ok':True,
                'clipboard_content_read':True,'clipboard_image_valid':True}}
    def observe(**kwargs):
        monkeypatch.setattr(vision,'vision_configuration_status',config)
        kwargs['window_context']={'schema_version':1,'hwnd':desktop.calibration['hwnd'],'pid':2188,
            'class_name':'WeChatMainWndForPC','source':'sidecar_selected_main_window'}
        result=original(**kwargs)
        (output/'native-image.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        (output/'native-image-journal.json').write_text(json.dumps(read_action_journal(kwargs['action_journal_path']),ensure_ascii=False,indent=2,default=str))
        return result
    monkeypatch.setattr(BuiltinVisionPlugin,'run',plugin_io)
    monkeypatch.setattr(vision,'process_image_slot',observe)
    return calls
