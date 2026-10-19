"""Synthetic pixel controls and OCR rows exercise locator and interaction contracts.

No real screenshots or customer data are required. These fixtures are not
Windows recognition, native typing, or delivery evidence.
"""
import copy,json,tempfile,unittest
from pathlib import Path
from PIL import Image
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as f
FIX=Path(__file__).parent/'fixtures'/'hook_c1_synthetic'

def frames():
    out={}
    rows=json.loads((FIX/'ocr-synthetic.json').read_text())
    for r in rows:
        if 'duplicate_of' in r:continue
        with Image.open(FIX/f"screenshot-{r['screenshot']}-{r['label']}.png") as im:
            image=im.convert('RGB')
        out[r['label']]=f.Frame(image,r['rows'],r['label'])
    with Image.open(FIX/'full-header.png') as im:image=im.convert('RGB')
    out['header']=f.Frame(image,json.loads((FIX/'header-rows.json').read_text()),'header')
    return out

class ReplayUI:
    def __init__(self):
        self.frames=frames();self.actions=[];self.captures=[];self.texts={};self.cleanup=True
        self.fail='';self.kind='new_contact';self.closed=True;self.guard_count=0
    def guard(self,*args):
        self.guard_count+=1
        if self.fail=='guard':raise f.FlowStop('ACCOUNT_CHANGED')
    def capture(self,name):
        self.captures.append(name)
        if self.fail==name:raise f.FlowStop('ROI_CAPTURE_FAILED')
        frame=copy.deepcopy(self.frames['fields' if name.startswith('fields') else name])
        frame.name=name
        if name.startswith('fields_after') or name=='fields_filled':
            for row in frame.rows:
                if 95 < row['top'] < 139 and 'greeting' in self.texts:row['text']=self.texts['greeting']
                if 192 < row['top'] < 235 and 'remark' in self.texts:row['text']=self.texts['remark']
        if self.fail=='wrong_remark' and name=='fields_filled':
            frame.rows[-1]['text']='错误备注'
        if name=='card' and self.kind!='new_contact':
            row=next(r for r in frame.rows if r['text']=='添加到通讯录')
            row['text']='发消息' if self.kind=='already_friend' else '该用户不存在'
        return frame
    def click(self,frame,t,action):
        self.actions.append(action)
        if self.fail==action:raise f.FlowStop('PHYSICAL_CLICK_FAILED')
        self.last=action
    def replace(self,frame,text):
        key={'focus_greeting':'greeting','focus_remark':'remark','focus_main_search':'query'}[self.last]
        self.texts[key]=text
    def select_invite_dialog(self):self.actions.append('dialog_selected')
    def observe_submit(self):
        if self.fail=='observe':raise f.FlowStop('WINDOW_FOCUS_OR_GEOMETRY_CHANGED')
        return {'form_open':not self.closed,'form_closed':self.closed,'basis':'simulated_dialog_state'}
    def existing_chat_verified(self,*args):self.actions.append('existing_chat_verified')
    def clear_search(self):
        self.actions.append('search_cleanup')
        if not self.cleanup:raise f.FlowStop('SEARCH_CLEANUP_FAILED')

class Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.j=f.SubmitJournal(Path(self.tmp.name)/'submit.json');self.ui=ReplayUI()
    def tearDown(self):self.tmp.cleanup()
    def run_flow(self,**kwargs):
        payload=dict(phone='13800000003',verify_message='您好我是测试销售',remark_name='测试 CJH5N28',remark_code='CJH5N28');payload.update(kwargs)
        return f.run_flow(self.ui,journal=self.j,**payload)
    def test_complete_replay_synthetic_ocr_anchors_simulated_input(self):
        r=self.run_flow();self.assertEqual(r['status'],'submitted_pending_recipient');self.assertFalse(r['delivery_confirmed']);self.assertFalse(r['ai_admission'])
        self.assertEqual(self.ui.actions,['focus_main_search','network_lookup','open_invite_form','dialog_selected','focus_greeting','focus_remark','confirm_invite_once','search_cleanup'])
        self.assertEqual(self.ui.captures,['header','lookup','card','fields','fields_after_greeting','fields_filled','footer']);self.assertEqual(r['cleanup'],'search_empty_verified')
        self.assertTrue(json.loads(self.j.path.read_text())['submit_attempted'])
    def test_no_second_submit_after_unknown(self):
        self.ui.fail='observe';r=self.run_flow();self.assertEqual(r['status'],'submission_unknown');self.assertTrue(self.j.path.exists())
        self.ui=ReplayUI();r=self.run_flow();self.assertEqual(r['error_code'],'PREVIOUS_SUBMIT_ATTEMPT_REQUIRES_REVIEW');self.assertFalse(self.ui.actions)
    def test_submit_failed_still_durable(self):
        self.ui.fail='confirm_invite_once';r=self.run_flow();self.assertEqual(r['status'],'submission_unknown');self.assertTrue(self.j.path.exists())
    def test_wrong_field_never_confirms(self):
        self.ui.fail='wrong_remark';r=self.run_flow();self.assertEqual(r['error_code'],'INVITE_FIELD_VERIFICATION_FAILED');self.assertNotIn('confirm_invite_once',self.ui.actions);self.assertFalse(self.j.path.exists())
    def test_failed_dialog_transition_never_cleans_through_old_card(self):
        from unittest.mock import Mock
        self.ui.select_invite_dialog = Mock(side_effect=f.FlowStop('UNEXPECTED_WECHAT_DIALOG'))
        r = self.run_flow()
        self.assertEqual(r['error_code'], 'UNEXPECTED_WECHAT_DIALOG')
        self.assertEqual(r['cleanup'], 'deferred_invite_dialog_unconfirmed')
        self.assertNotIn('cleanup_error_code', r)
        self.assertFalse(r['submit_attempted']);self.assertFalse(self.j.path.exists())
        self.assertNotIn('search_cleanup', self.ui.actions)
        self.assertNotIn('confirm_invite_once', self.ui.actions)

    def test_non_form_candidate_cannot_authorize_typing_or_submit(self):
        self.ui.frames['fields'].rows=[]
        r=self.run_flow()
        self.assertFalse(r['submit_attempted']);self.assertFalse(self.j.path.exists())
        self.assertNotIn('focus_greeting',self.ui.actions)
        self.assertNotIn('confirm_invite_once',self.ui.actions)
        self.assertNotIn('search_cleanup',self.ui.actions)
        self.assertEqual(r['cleanup'],'deferred_invite_dialog_still_open')

    def test_open_click_failure_conservatively_defers_cleanup(self):
        self.ui.fail='open_invite_form'
        r=self.run_flow()
        self.assertEqual(r['cleanup'],'deferred_invite_dialog_unconfirmed')
        self.assertFalse(r['submit_attempted']);self.assertNotIn('search_cleanup',self.ui.actions)

    def test_original_contract_missing_code(self):
        r=self.run_flow(remark_name='无短码');self.assertEqual(r['error_code'],'TASK_PAYLOAD_INVALID');self.assertFalse(self.ui.actions)
    def test_account_change_no_actions(self):
        self.ui.fail='guard';r=self.run_flow();self.assertEqual(r['error_code'],'ACCOUNT_CHANGED');self.assertFalse(self.ui.actions)
    def test_existing_friend_no_invite_no_remark(self):
        self.ui.kind='already_friend';r=self.run_flow();self.assertEqual(r['status'],'already_friend');self.assertNotIn('open_invite_form',self.ui.actions);self.assertNotIn('remark',self.ui.texts);self.assertFalse(self.j.path.exists())
    def test_not_found_no_submit(self):
        self.ui.kind='not_found';r=self.run_flow();self.assertEqual(r['status'],'not_found');self.assertFalse(self.j.path.exists())
    def test_cleanup_failure_preserves_submit_outcome(self):
        self.ui.cleanup=False;r=self.run_flow();self.assertEqual(r['status'],'submitted_pending_recipient');self.assertEqual(r['cleanup'],'failed_preserve_business_outcome')
    def test_remaining_form_is_unknown_no_auto_retry(self):
        self.ui.closed=False;r=self.run_flow();self.assertEqual(r['status'],'submission_unknown');self.assertEqual(r['cleanup'],'deferred_invite_dialog_still_open');self.assertEqual(self.ui.actions.count('confirm_invite_once'),1)
    def test_query_only_in_lower_search_not_accepted(self):
        frame=self.ui.frames['lookup'];frame.rows=[r for r in frame.rows if r['top']>90]
        with self.assertRaises(f.FlowStop):f.lookup(frame,'13800000003')
    def test_wrong_phone_fails(self):
        with self.assertRaises(f.FlowStop):f.lookup(self.ui.frames['lookup'],'13800000004')
    def test_duplicate_network_anchor_fails(self):
        frame=self.ui.frames['lookup'];frame.rows.append(copy.deepcopy(next(r for r in frame.rows if '网络查找' in r['text'])))
        with self.assertRaises(f.FlowStop):f.lookup(frame,'13800000003')
    def test_green_button_needs_actual_pixels(self):
        frame=self.ui.frames['card'];frame.image=Image.new('RGB',frame.image.size,'white')
        with self.assertRaises(f.FlowStop):f.classify_card(frame)
    def test_restricted_dominates_add_button(self):
        frame=self.ui.frames['card'];frame.rows.append(dict(text='操作频繁',confidence=.99,left=0,top=0,right=80,bottom=20))
        with self.assertRaisesRegex(f.FlowStop,'ACCOUNT_RESTRICTED'):f.classify_card(frame)
    def test_ambiguous_card_fails(self):
        frame=self.ui.frames['card'];frame.rows.append(dict(text='发消息',confidence=.99,left=0,top=0,right=80,bottom=20))
        with self.assertRaises(f.FlowStop):f.classify_card(frame)
    def test_low_confidence_remarks_no_submit(self):
        frame=self.ui.frames['fields'];frame.rows[-1]['confidence']=.70
        with self.assertRaises(f.FlowStop):f.verify_fields(frame,frame.rows[2]['text'],frame.rows[-1]['text'])
    def test_exact_existing_fixture_not_completion_proof(self):
        frame=self.ui.frames['existing'];self.assertTrue(any(r['text']=='模拟字段' for r in frame.rows))
        with self.assertRaises(f.FlowStop):f.classify_card(frame)
    def test_red_annotation_cannot_authorize_button(self):
        frame=self.ui.frames['header'];row=f.unique_text(frame,('Q搜索','搜索'));t=f.target(frame,row,filled='search');self.assertEqual(t['bounds'],[83,31,320,62])
    def test_failure_at_each_pre_submit_stage_never_retries(self):
        for stage in ['header','lookup','card','fields','fields_after_greeting','fields_filled','footer']:
            with self.subTest(stage=stage):
                self.ui=ReplayUI();self.ui.fail=stage;r=self.run_flow();self.assertFalse(r['submit_attempted']);self.assertNotIn('confirm_invite_once',self.ui.actions)

    def test_original_search_placeholder_variants_still_require_real_input_pixels(self):
        for variant in ('搜索','Q搜索','O搜索','0搜索','搜素','Q搜素','O搜素','0搜素'):
            with self.subTest(variant=variant):
                frame=copy.deepcopy(self.ui.frames['header'])
                row=f.unique_text(frame,('搜索','Q搜索'));row['text']=variant
                self.assertEqual(f.empty_search(frame)['bounds'],[83,31,320,62])

    def test_query_cannot_be_accepted_as_placeholder_variant(self):
        frame=copy.deepcopy(self.ui.frames['header'])
        row=f.unique_text(frame,('搜索','Q搜索'));row['text']='O搜索13800000003'
        with self.assertRaises(f.FlowStop):f.empty_search(frame)

    def test_placeholder_variant_on_unresolved_pixels_is_rejected(self):
        frame=copy.deepcopy(self.ui.frames['header'])
        row=f.unique_text(frame,('搜索','Q搜索'));row['text']='O搜索'
        frame.image=Image.new('RGB',frame.image.size,'white')
        with self.assertRaises(f.FlowStop):f.empty_search(frame)


    def focused_header(self):
        frame=copy.deepcopy(self.ui.frames['header']);bounds=f.empty_search(frame)['bounds']
        with Image.open(FIX/'focused-empty-search.png') as crop:frame.image.paste(crop.convert('RGB'),tuple(bounds[:2]))
        frame.rows=[r for r in frame.rows if not f.contains(bounds,r)]
        return frame,bounds

    def test_actual_focused_empty_input_pixels_without_placeholder(self):
        frame,bounds=self.focused_header()
        self.assertEqual(f.empty_search(frame)['bounds'],bounds)
        self.assertEqual(f.empty_search(frame)['proof'],'focused_empty_pixels')

    def test_focused_query_not_recognized_by_ocr_still_rejected(self):
        from PIL import ImageDraw
        for label in ('1','I','x','123456','搜索'):
            with self.subTest(text=label):
                frame,bounds=self.focused_header()
                ImageDraw.Draw(frame.image).text((bounds[0]+27,bounds[1]+7),label,fill=(70,70,70))
                with self.assertRaises(f.FlowStop):f.empty_search(frame)

    def test_single_stroke_at_text_origin_is_unresolved_not_a_caret_pass(self):
        from PIL import ImageDraw
        frame,bounds=self.focused_header()
        ImageDraw.Draw(frame.image).line([(bounds[0]+27,bounds[1]+6),(bounds[0]+27,bounds[1]+24)],fill=(50,50,50))
        with self.assertRaisesRegex(f.FlowStop,'CARET_OR_TEXT_UNRESOLVED'):f.empty_search(frame)

    def test_blank_rectangle_cannot_replace_search_control_icons(self):
        frame,bounds=self.focused_header();frame.image.paste((249,249,249),tuple(bounds))
        with self.assertRaises(f.FlowStop):f.empty_search(frame)

    def test_modified_icon_border_and_colored_residue_rejected(self):
        from PIL import ImageDraw
        for offset in ((10,15),(220,15),(0,15),(50,15),(207,15)):
            with self.subTest(offset=offset):
                frame,bounds=self.focused_header();x,y=offset
                ImageDraw.Draw(frame.image).rectangle((bounds[0]+x,bounds[1]+y,bounds[0]+x+2,bounds[1]+y+2),fill='red')
                with self.assertRaises(f.FlowStop):f.empty_search(frame)

    def test_placeholder_plus_residual_query_is_not_empty(self):
        frame=copy.deepcopy(self.ui.frames['header']);bounds=f.empty_search(frame)['bounds']
        frame.rows.append(dict(text='123',confidence=.99,left=bounds[0]+45,top=bounds[1]+5,right=bounds[0]+75,bottom=bounds[3]-5))
        with self.assertRaisesRegex(f.FlowStop,'SEARCH_QUERY_REMAINS'):f.empty_search(frame)

    def test_focused_pixels_reject_wrong_height_and_other_frame(self):
        frame,bounds=self.focused_header();frame.name='fields'
        with self.assertRaises(f.FlowStop):f.empty_search(frame)
        with self.assertRaises(f.FlowStop):f.focused_search_empty_pixels(frame.image.crop(tuple(bounds)).resize((237,32)))


class NativeGuardTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.tmp=tempfile.TemporaryDirectory();self.geom={'left':12,'top':12,'width':1060,'height':740}
        self.client={'width':1063,'height':738,'screen_left':12,'screen_top':12};self.foreground=11;self.hit=11
        self.gui=SimpleNamespace(IsWindow=lambda h:True,IsIconic=lambda h:False,GetForegroundWindow=lambda:self.foreground,
            GetWindow=lambda h,k:0,WindowFromPoint=lambda p:self.hit,GetAncestor=lambda h,k:h,
            IsWindowVisible=lambda h:True,GetClassName=lambda h:'WeChatMainWndForPC' if h==11 else 'ContactProfileWnd',
            EnumWindows=lambda fn,arg:fn(11,arg))
        self.ops=SimpleNamespace(win32gui=self.gui,win32process=SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,99)),
            get_window_geometry=lambda h:dict(self.geom),get_window_client_geometry=lambda h:dict(self.client),
            humanized_action_sleep=Mock(),add_friend_paced_pause=Mock(),ensure_left_button_released=Mock(),require_active_ui_action_budget=Mock(),
            win32api=SimpleNamespace(GetCursorPos=lambda:(10,10),SetCursorPos=Mock(),mouse_event=Mock()),
            win32con=SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2,MOUSEEVENTF_LEFTUP=4))
        self.ui=object.__new__(f.NativeUI);self.ui.ops=self.ops;self.ui.main=11;self.ui.pid=99;self.ui.current=11;self.ui.initial=dict(self.geom)
        self.ui.identity_guard=Mock();self.ui.count=0;self.ui.ocr_count=0;self.ui.last_frame=None;self.ui.evidence=Path(self.tmp.name)
        self.fr=frames()['header'];self.fr.hwnd=11;self.fr.origin=(12,12);self.fr.geometry=dict(self.client);self.fr.timestamp=f.time.monotonic();self.ui.last_frame=self.fr
    def tearDown(self):self.tmp.cleanup()
    def test_foreign_foreground_stops(self):
        self.foreground=12
        with self.assertRaisesRegex(f.FlowStop,'WINDOW_FOCUS'):self.ui.guard(self.fr)
    def test_moved_window_stops(self):
        self.geom['left']+=1
        with self.assertRaises(f.FlowStop):self.ui.guard(self.fr)
    def test_expired_or_replaced_frame_stops(self):
        self.fr.timestamp-=16
        with self.assertRaises(f.FlowStop):self.ui.guard(self.fr)
        self.fr.timestamp=f.time.monotonic();self.ui.last_frame=None
        with self.assertRaises(f.FlowStop):self.ui.guard(self.fr)
    def test_occluded_click_no_mouse_down(self):
        self.hit=12
        with self.assertRaisesRegex(f.FlowStop,'TARGET_OCCLUDED'):self.ui.click(self.fr,f.empty_search(self.fr),'test')
        self.ops.win32api.mouse_event.assert_not_called()
    def test_focus_changes_during_mouse_move_no_mouse_down(self):
        def move(*args):self.foreground=12
        self.ops.win32api.SetCursorPos.side_effect=move
        with self.assertRaises(f.FlowStop):self.ui.click(self.fr,f.empty_search(self.fr),'test')
        self.ops.win32api.mouse_event.assert_not_called()
    def test_click_never_uses_fullscreen_or_hardcoded_absolute_coordinates(self):
        self.fr.origin=(-800,200)
        self.ui.click(self.fr,f.empty_search(self.fr),'test')
        self.assertEqual(self.ops.win32api.SetCursorPos.call_args.args[0],(-599,246))
        self.assertEqual(self.ops.win32api.mouse_event.call_count,2)
    def test_capture_single_exact_roi_once(self):
        from unittest.mock import Mock
        data=json.loads((FIX/'native-roi-synthetic.json').read_text());record=next(r for r in data if r['label']=='header')
        with Image.open(FIX/'native-header.png') as im:image=im.convert('RGB')
        self.ops.try_image_grab=Mock(return_value=image);self.ops.run_ocr=Mock(return_value=record['rows'])
        frame=self.ui.capture('header')
        self.ops.try_image_grab.assert_called_once_with((12,12,671,102));self.ops.run_ocr.assert_called_once_with(image)
        self.assertEqual(self.ui.count,1);self.assertEqual(self.ui.ocr_count,1);f.empty_search(frame)
    def test_capture_failure_no_retry_no_ocr(self):
        from unittest.mock import Mock
        self.ops.try_image_grab=Mock(return_value=None);self.ops.run_ocr=Mock()
        with self.assertRaises(f.FlowStop):self.ui.capture('header')
        self.ops.try_image_grab.assert_called_once();self.ops.run_ocr.assert_not_called()
    def test_geometry_change_after_capture_no_ocr(self):
        from unittest.mock import Mock
        with Image.open(FIX/'native-header.png') as im:image=im.convert('RGB')
        def capture(bbox):self.client['screen_left']+=1;return image
        self.ops.try_image_grab=Mock(side_effect=capture);self.ops.run_ocr=Mock()
        with self.assertRaises(f.FlowStop):self.ui.capture('header')
        self.ops.run_ocr.assert_not_called()
    def test_all_runtime_rois_synthetic_ocr_recognition(self):
        data=json.loads((FIX/'native-roi-synthetic.json').read_text())
        for record in data:
            label=record['label']
            with self.subTest(label=label):
                with Image.open(FIX/f'native-{label}.png') as im:image=im.convert('RGB')
                frame=f.Frame(image,record['rows'],label)
                if label=='header':f.empty_search(frame)
                elif label=='lookup':f.lookup(frame,'13800000003')
                elif label=='card':self.assertEqual(f.classify_card(frame)[0],'new_contact')
                elif label=='fields':self.assertEqual(set(f.form_fields(frame)),{'greeting','remark'})
                elif label=='footer':f.target(frame,f.unique_text(frame,('确定',)),filled='green')
    def test_dpi_change_stops_even_without_resize(self):
        self.ui.dpi=1.25;self.ops.window_dpi_scale=lambda h:1.5
        with self.assertRaisesRegex(f.FlowStop,'WINDOW_DPI_CHANGED'):self.ui.guard(self.fr)
    def test_ocr_slow_frame_expires_no_click(self):
        from unittest.mock import Mock,patch
        data=json.loads((FIX/'native-roi-synthetic.json').read_text());record=next(r for r in data if r['label']=='header')
        with Image.open(FIX/'native-header.png') as im:image=im.convert('RGB')
        self.ops.try_image_grab=Mock(return_value=image);self.ops.run_ocr=Mock(return_value=record['rows'])
        with patch.object(f.time,'monotonic',side_effect=[1.0,20.0]):
            with self.assertRaisesRegex(f.FlowStop,'STALE_ROI_FRAME'):self.ui.capture('header')
        self.ops.win32api.mouse_event.assert_not_called()

    def test_owned_contact_surface_captured_only(self):
        from unittest.mock import Mock
        self.foreground=22;self.gui.GetWindow=lambda h,k:11
        main=dict(self.client);card=dict(width=349,height=235,screen_left=390,screen_top=101)
        self.ops.get_window_client_geometry=lambda h:dict(card if h==22 else main)
        fr=frames()['card'];self.ops.try_image_grab=Mock(return_value=fr.image);self.ops.run_ocr=Mock(return_value=fr.rows)
        frame=self.ui.capture('card')
        self.assertEqual(self.ui.current,22);self.ops.try_image_grab.assert_called_once_with((390,101,739,336))
        self.assertEqual(f.classify_card(frame)[0],'new_contact')
    def test_other_process_contact_surface_rejected(self):
        from unittest.mock import Mock
        self.foreground=22;self.gui.GetWindow=lambda h,k:11
        self.ops.win32process.GetWindowThreadProcessId=lambda h:(1,100 if h==22 else 99)
        self.ops.try_image_grab=Mock()
        with self.assertRaisesRegex(f.FlowStop,'UNEXPECTED_CONTACT_SURFACE'):self.ui.capture('card')
        self.ops.try_image_grab.assert_not_called()

    def card_setup(self):
        from unittest.mock import Mock
        self.foreground=22
        main=dict(self.client);card=dict(width=349,height=235,screen_left=390,screen_top=101)
        self.ops.get_window_client_geometry=lambda h:dict(main if h==11 else card)
        fr=frames()['card'];self.ops.try_image_grab=Mock(return_value=fr.image);self.ops.run_ocr=Mock(return_value=fr.rows)

    def test_same_process_indirect_owner_card_accepted(self):
        self.card_setup();self.gui.GetWindow=lambda h,k:{22:33,33:11}.get(h,0)
        frame=self.ui.capture('card')
        self.assertEqual(f.classify_card(frame)[0],'new_contact')
        self.assertEqual([e['hwnd'] for e in self.ui.window_events[-1]['owner_chain']],[33,11])
        self.assertEqual(self.ui.window_events[-1]['basis'],'same_process_owner_chain')

    def test_new_visible_unowned_card_accepted_after_guarded_lookup(self):
        self.card_setup();self.ui.lookup_visible_windows={11}
        frame=self.ui.capture('card')
        self.assertEqual(f.classify_card(frame)[0],'new_contact')
        self.assertEqual(self.ui.window_events[-1]['basis'],'newly_visible_unowned_card_candidate')
        self.ops.try_image_grab.assert_called_once()

    def test_preexisting_unowned_card_no_capture(self):
        self.card_setup();self.ui.lookup_visible_windows={11,22}
        with self.assertRaisesRegex(f.FlowStop,'UNEXPECTED_CONTACT_SURFACE'):self.ui.capture('card')
        self.ops.try_image_grab.assert_not_called()
        self.assertFalse(self.ui.window_events[-1]['accepted'])

    def test_unowned_card_without_lookup_baseline_no_capture(self):
        self.card_setup()
        with self.assertRaises(f.FlowStop):self.ui.capture('card')
        self.ops.try_image_grab.assert_not_called()

    def test_unowned_card_outside_main_no_capture(self):
        self.card_setup();self.ui.lookup_visible_windows={11}
        read=self.ops.get_window_client_geometry
        self.ops.get_window_client_geometry=lambda h:{**read(h),'screen_left':1200} if h==22 else read(h)
        with self.assertRaises(f.FlowStop):self.ui.capture('card')
        self.ops.try_image_grab.assert_not_called()

    def test_indirect_owner_other_process_no_capture(self):
        self.card_setup();self.gui.GetWindow=lambda h,k:{22:33,33:11}.get(h,0)
        self.ops.win32process.GetWindowThreadProcessId=lambda h:(1,100 if h==33 else 99)
        with self.assertRaises(f.FlowStop):self.ui.capture('card')
        self.ops.try_image_grab.assert_not_called()

    def test_search_shadow_and_other_noncard_classes_no_capture(self):
        self.card_setup();self.ui.lookup_visible_windows={11}
        for name in ('Search2Wnd','popupshadow','ChatContactMenu','TrayNotifyWnd'):
            with self.subTest(window_class=name):
                self.gui.GetClassName=lambda h:name if h==22 else 'WeChatMainWndForPC'
                with self.assertRaises(f.FlowStop):self.ui.capture('card')
                self.ops.try_image_grab.assert_not_called()

    def test_network_click_records_pretransition_visible_windows(self):
        self.ui.click(self.fr,f.empty_search(self.fr),'network_lookup')
        self.assertEqual(self.ui.lookup_visible_windows,{11})
        self.assertEqual(self.ui.window_events[-1]['stage'],'network_lookup_before_click')
        self.assertEqual(self.ops.win32api.mouse_event.call_count,2)

    def synthetic_clipped_form(self):
        with Image.open(FIX/'formal-clipped-fields.png') as im:image=im.convert('RGB')
        rows=json.loads((FIX/'formal-clipped-fields-ocr-synthetic.json').read_text())['rows']
        return f.Frame(image,rows,'fields_filled')

    def test_synthetic_long_greeting_clipping_does_not_block_correct_shortcode(self):
        frame=self.synthetic_clipped_form()
        fields=f.verify_fields(frame,'您好，我是车金二手车的Hook连接联调，您刚咨询过二手车','CJ8NZRD9')
        self.assertEqual(set(fields),{'greeting','remark'})
        visible=''.join(f.clean(r['text']) for r in frame.rows if f.contains(fields['greeting']['bounds'],r))
        self.assertEqual(visible,'二手车的Hook连接联调，您刚咨询过二手车')
        self.assertFalse(hasattr(self.ui,'verify_invite_fields'))

    def test_wrong_shortcode_on_synthetic_form_still_blocks(self):
        with self.assertRaisesRegex(f.FlowStop,'INVITE_FIELD_VERIFICATION_FAILED'):
            f.verify_fields(self.synthetic_clipped_form(),'原申请文案','CJ8NZRD8')

    def test_missing_low_confidence_or_extra_remark_text_still_blocks(self):
        for change in ('missing','confidence','extra'):
            with self.subTest(change=change):
                frame=self.synthetic_clipped_form();row=next(r for r in frame.rows if r['text']=='CJ8NZRD9')
                if change=='missing':frame.rows.remove(row)
                if change=='confidence':row['confidence']=.89
                if change=='extra':frame.rows.append({**row,'text':'OTHER'})
                with self.assertRaises(f.FlowStop):f.verify_fields(frame,'原申请文案','CJ8NZRD9')

    def input_setup(self):
        from unittest.mock import Mock
        self.ops.hotkey=Mock();self.ops.key_press=Mock();self.ops.clipboard_copy=Mock()
        self.ops.clipboard_read=Mock();self.ops.add_friend_paced_pause=Mock()
        self.ops.type_text_with_sendinput_unicode=Mock();self.ops.humanized_input_settings=Mock(return_value={})
        self.ops.win32con.VK_CONTROL=17;self.ops.win32con.VK_BACK=8;self.ops.win32con.VK_ESCAPE=27;self.ops.win32con.WM_CLOSE=16

    def test_both_invite_fields_use_clipboard_once_not_unicode_typing(self):
        self.input_setup()
        for name,text in [('fields','您好，请通过申请'),('fields_after_greeting','联调 CJABC234')]:
            with self.subTest(field=name):
                self.fr.name=name;self.ops.clipboard_read.return_value=text
                self.ui.replace(self.fr,text)
                self.ops.clipboard_copy.assert_called_with(text)
        self.assertEqual(self.ops.clipboard_copy.call_count,2)
        self.assertEqual(len(self.ui.clipboard_field_events),2)
        self.assertTrue(all(e['copy_calls']==1 and e['paste_calls']==1 and e['clipboard_match']
                            and e['completion_guard_passed'] for e in self.ui.clipboard_field_events))
        self.assertNotIn('您好',json.dumps(self.ui.clipboard_field_events,ensure_ascii=False))
        self.assertEqual([call.args for call in self.ops.hotkey.call_args_list],[(17,65),(17,86),(17,65),(17,86)])
        self.ops.type_text_with_sendinput_unicode.assert_not_called()

    def test_changed_clipboard_does_not_paste(self):
        self.input_setup();self.fr.name='fields';self.ops.clipboard_read.return_value='OTHER'
        with self.assertRaisesRegex(f.FlowStop,'CLIPBOARD_CONTENT_CHANGED'):self.ui.replace(self.fr,'EXPECTED')
        self.assertNotIn((17,86),[call.args for call in self.ops.hotkey.call_args_list])
        self.assertEqual(self.ui.clipboard_field_events[0]['paste_calls'],0)
        self.assertFalse(self.ui.clipboard_field_events[0]['clipboard_match'])

    def test_focus_change_after_clipboard_copy_does_not_paste(self):
        self.input_setup();self.fr.name='fields'
        self.ops.clipboard_copy.side_effect=lambda text:setattr(self,'foreground',22)
        with self.assertRaises(f.FlowStop):self.ui.replace(self.fr,'EXPECTED')
        self.assertNotIn((17,86),[call.args for call in self.ops.hotkey.call_args_list])

    def test_placeholder_search_cleanup_skips_all_input_and_main_stays_visible(self):
        from unittest.mock import Mock
        self.input_setup();self.fr.name='header'
        def capture(name):self.ui.last_frame=self.fr;return self.fr
        self.ui.capture=Mock(side_effect=capture);self.ui.click=Mock()
        self.ui.clear_search()
        self.ops.key_press.assert_not_called();self.ops.hotkey.assert_not_called()
        self.ops.clipboard_copy.assert_not_called();self.ops.type_text_with_sendinput_unicode.assert_not_called()
        self.ui.capture.assert_called_once_with('header');self.ui.click.assert_not_called()
        self.assertEqual(self.ui.cleanup_action,'already_empty_no_input')


    def test_default_search_placeholder_and_magnifier_variants_need_no_cleanup(self):
        from unittest.mock import Mock
        for text in ('搜索', 'Q搜索', 'O搜索'):
            with self.subTest(placeholder=text):
                self.input_setup();self.fr.name='header'
                row=next(r for r in self.fr.rows if f.clean(r.get('text')) in ('搜索','Q搜索','O搜索'))
                row['text']=text
                def capture(name):self.ui.last_frame=self.fr;return self.fr
                self.ui.capture=Mock(side_effect=capture);self.ui.click=Mock()
                self.ui.clear_search()
                self.ui.capture.assert_called_once_with('header')
                self.ui.click.assert_not_called();self.ops.hotkey.assert_not_called()
                self.ops.key_press.assert_not_called()
                self.assertEqual(self.ui.cleanup_action,'already_empty_no_input')

    def test_nonempty_search_clears_once_and_checks_one_new_header(self):
        from unittest.mock import Mock
        self.input_setup();self.fr.name='header'
        dirty=copy.deepcopy(self.fr)
        next(r for r in dirty.rows if f.clean(r.get('text')) in ('搜索','Q搜索'))['text']='13800000003'
        def capture(name):
            frame=dirty if self.ui.capture.call_count==1 else self.fr
            self.ui.last_frame=frame;return frame
        self.ui.capture=Mock(side_effect=capture);self.ui.click=Mock()
        self.ui.clear_search()
        self.assertEqual(self.ui.capture.call_count,2)
        self.assertEqual([c.args for c in self.ops.hotkey.call_args_list],[(17,65)])
        self.assertEqual([c.args for c in self.ops.key_press.call_args_list],[(8,)])
        self.assertEqual(self.ui.click.call_count,1)
        self.assertEqual(self.ui.cleanup_action,'query_cleared_once')

    def test_phone_pacing_uses_real_typer_one_digit_and_four_to_five_seconds(self):
        from unittest.mock import Mock,patch
        from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
        self.input_setup();self.fr.name='header'
        units=[];delays=[]
        self.ops.humanized_input_settings.return_value={'chunk_min_chars':6,'chunk_max_chars':12,'inter_chunk_delay_scale':.35,'micro_pause_every_chars':2}
        def type_phone(text,settings,**kwargs):
            with patch.object(sidecar,'humanized_sleep_ms',side_effect=lambda lo,hi:delays.append((lo,hi))):
                return sidecar.type_text_with_sendinput_unicode(text,settings,send_unit_func=units.append,**kwargs)
        self.ops.type_text_with_sendinput_unicode.side_effect=type_phone
        self.ui.replace(self.fr,'13800000003')
        self.assertEqual(units,[ord(c) for c in '13800000003'])
        self.assertEqual(delays,[(400,450)]*11)
        self.assertEqual(sum(x[0] for x in delays),4400)
        self.assertEqual(sum(x[1] for x in delays),4950)
        self.assertEqual(self.ops.type_text_with_sendinput_unicode.call_count,1)

    def test_ambiguous_stroke_has_one_narrow_observation_and_needs_blank_pixels(self):
        from unittest.mock import Mock
        from PIL import ImageDraw
        self.input_setup();self.fr.name='header'
        b=f.empty_search(self.fr)['bounds']
        with Image.open(FIX/'focused-empty-search.png') as im:crop=im.convert('RGB')
        self.fr.image.paste(crop,tuple(b[:2]));self.fr.rows=[]
        ImageDraw.Draw(self.fr.image).line([(b[0]+27,b[1]+6),(b[0]+27,b[1]+24)],fill=(50,50,50))
        narrow=f.Frame(crop,[],'search_empty',11,(0,0),self.client,self.fr.timestamp)
        def capture(name):self.ui.last_frame=narrow;return narrow
        self.ui.capture=Mock(side_effect=capture)
        actual,t=self.ui.verified_empty_search(self.fr)
        self.assertIs(actual,narrow);self.assertEqual(t['bounds'],[0,0,237,31])
        self.ui.capture.assert_called_once_with('search_empty')
        self.assertEqual(self.ui._bounds('search_empty',1060,740),tuple(b))
        ImageDraw.Draw(narrow.image).line([(27,6),(27,24)],fill=(50,50,50))
        self.ui.last_frame=self.fr;self.ui.capture.reset_mock()
        with self.assertRaises(f.FlowStop):self.ui.verified_empty_search(self.fr)
        self.ui.capture.assert_called_once_with('search_empty')

    def test_close_only_verified_card_not_main_or_dialog(self):
        from unittest.mock import Mock
        self.input_setup();self.foreground=22;self.ui.current=22;self.ui.card_hwnd=22
        visible={11,22};self.gui.IsWindowVisible=lambda h:h in visible
        def close(hwnd,msg,a,b):visible.remove(22);self.foreground=11
        self.gui.PostMessage=Mock(side_effect=close)
        self.ui.return_to_main()
        self.gui.PostMessage.assert_called_once_with(22,16,0,0)
        self.assertEqual(self.ui.current,11);self.ops.key_press.assert_not_called()

    def test_unknown_cleanup_dialog_is_not_closed(self):
        from unittest.mock import Mock
        self.input_setup();self.foreground=22;self.ui.current=22;self.ui.card_hwnd=33
        self.gui.PostMessage=Mock()
        with self.assertRaisesRegex(f.FlowStop,'UNEXPECTED_CLEANUP_WINDOW'):self.ui.return_to_main()
        self.gui.PostMessage.assert_not_called();self.ops.key_press.assert_not_called()

    def test_hidden_main_guard_refuses_keyboard_action(self):
        self.input_setup();self.gui.IsWindowVisible=lambda h:False if h==11 else True
        with self.assertRaises(f.FlowStop):self.ui.replace(self.fr,'TEST')
        self.ops.hotkey.assert_not_called();self.ops.key_press.assert_not_called()

    def select_dialog_samples(self, samples, *, baseline=None):
        from unittest.mock import patch
        self.ui.current=22;self.ui.invite_visible_windows={11,22} if baseline is None else baseline
        elapsed=[0.0];index=[0]
        self.foreground=33
        def sample():return samples[min(index[0],len(samples)-1)]
        self.gui.GetForegroundWindow=lambda:sample().get('hwnd',33)
        self.gui.IsWindowVisible=lambda h:True if h==11 else sample().get('visible',True)
        self.gui.GetClassName=lambda h:sample().get('class','SyntheticInviteWnd')
        self.ops.win32process.GetWindowThreadProcessId=lambda h:(1,99 if h in {11,22} else sample().get('pid',99))
        self.ops.get_window_client_geometry=lambda h:dict(width=sample()['width'],height=sample()['height'],screen_left=320,screen_top=125)
        def sleep(seconds):elapsed[0]+=seconds;index[0]+=1
        with patch.object(f.time,'monotonic',side_effect=lambda:elapsed[0]),patch.object(f.time,'sleep',side_effect=sleep):
            self.ui.select_invite_dialog()
        return elapsed[0]

    def test_invite_transient_size_then_stable_normal_form(self):
        elapsed=self.select_dialog_samples([dict(width=450,height=235),dict(width=450,height=780)])
        self.assertEqual(self.ui.current,33);self.assertAlmostEqual(elapsed,.3)
        self.assertEqual(len(self.ui.window_events),2)
        self.assertTrue(self.ui.window_events[-1]['accepted'])
        self.assertFalse(any('title' in e for e in self.ui.window_events))
        self.ops.win32api.mouse_event.assert_not_called()

    def test_normal_invite_requires_two_equal_samples(self):
        elapsed=self.select_dialog_samples([dict(width=450,height=780)])
        self.assertAlmostEqual(elapsed,.15)
        self.ops.add_friend_paced_pause.assert_called_once_with('verify',reason='after_add_contact_entry_click_before_capture')
        self.assertEqual(len(self.ui.window_events),1)

    def test_persistent_zero_size_invite_is_bounded_and_diagnosed(self):
        with self.assertRaisesRegex(f.FlowStop,'UNEXPECTED_WECHAT_DIALOG'):
            self.select_dialog_samples([dict(width=450,height=0)])
        self.assertEqual(self.ui.current,22)
        self.assertEqual(len(self.ui.window_events),2)
        self.assertEqual(self.ui.window_events[-1]['stage'],'invite_dialog_timeout')
        self.ops.win32api.mouse_event.assert_not_called()

    def test_preexisting_or_shadow_or_hidden_candidate_never_accepted(self):
        for fields,baseline in [({}, {11,22,33}),({'class':'popupshadow'},None),({'visible':False},None)]:
            with self.subTest(fields=fields,baseline=baseline):
                with self.assertRaisesRegex(f.FlowStop,'UNEXPECTED_WECHAT_DIALOG'):
                    self.select_dialog_samples([dict(width=450,height=780,**fields)],baseline=baseline)
                self.assertEqual(self.ui.current,22)

    def test_invite_dimensions_are_not_a_fixed_template_gate(self):
        for width,height in [(450,780),(700,1000),(280,500)]:
            with self.subTest(width=width,height=height):
                self.select_dialog_samples([dict(width=width,height=height)])
                self.assertEqual(self.ui.current,33)
                self.ops.win32api.mouse_event.assert_not_called()

    def test_foreign_dialog_immediately_stops_without_input(self):
        with self.assertRaisesRegex(f.FlowStop,'WINDOW_FOCUS_OR_GEOMETRY_CHANGED'):
            self.select_dialog_samples([dict(width=450,height=780,pid=123)])
        self.ops.win32api.mouse_event.assert_not_called()

    def test_invite_transition_keeps_main_geometry_and_identity_guards(self):
        self.ui.identity_guard.side_effect=f.FlowStop('ACCOUNT_CHANGED')
        with self.assertRaisesRegex(f.FlowStop,'ACCOUNT_CHANGED'):
            self.select_dialog_samples([dict(width=450,height=780)])
        self.ui.identity_guard.side_effect=None
        self.geom['left']+=1
        with self.assertRaisesRegex(f.FlowStop,'WINDOW_FOCUS_OR_GEOMETRY_CHANGED'):
            self.select_dialog_samples([dict(width=450,height=780)])

if __name__=='__main__':unittest.main()
