"""Neutral UI-only controls on synthetic canvases; no customer screenshots."""
import unittest
from unittest.mock import Mock
import numpy as np
from PIL import Image, ImageDraw
from apps.wechat_ai_customer_service.adapters import search_control_icons as icons
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1_flow as f


def header(height=25, query=''):
    reference = icons.empty_references()[0]
    width=round(reference.width*height/reference.height)
    control=reference.resize((width,height),Image.Resampling.BILINEAR)
    image=Image.new('RGB',(800,100),(247,247,247))
    image.paste(control,(66,25))
    rows=[]
    if query:
        ImageDraw.Draw(image).text((66+height,29),query,fill=(60,60,60))
        rows=[dict(text=query,confidence=.99,left=66+height,top=29,right=66+height+100,bottom=42)]
    return f.Frame(image,rows,'header')


class SearchControls(unittest.TestCase):
    def native(self, frame, after=None):
        ui=object.__new__(f.NativeUI)
        ui.main=ui.current=11
        ui.return_to_main=Mock()
        ui.guard=Mock()
        ui.click=Mock()
        ui.replace=Mock()
        ui.owned_search_query='13800000003'
        ui.capture=Mock(side_effect=[frame, after or frame, after or frame])
        return ui

    def test_native_empty_proof_at_25_31_38_50_pixels_without_actions(self):
        for height in (25,31,38,50):
            with self.subTest(height=height):
                fr=header(height)
                ui=self.native(fr)
                ui.clear_search()
                ui.click.assert_not_called();ui.replace.assert_not_called()
                self.assertEqual(ui.capture.call_count,1)
                self.assertEqual(ui.cleanup_action,'already_empty_no_input')

    def test_circle_x_click_once_then_fresh_proof(self):
        dirty=header(query='13800000003');empty=header()
        ui=self.native(dirty,empty);ui.clear_search()
        self.assertEqual(ui.click.call_count,1)
        target=ui.click.call_args.args[1]
        self.assertEqual(target['method'],'clear_template')
        self.assertGreater(target['point'][0],230)
        ui.replace.assert_not_called()
        self.assertEqual(ui.capture.call_count,2)
        self.assertEqual(ui.cleanup_action,'clear_icon_clicked_once')

    def test_icon_no_effect_falls_back_once_at_cached_input_then_proves_empty(self):
        dirty=header(query='13800000003');empty=header()
        ui=self.native(dirty);ui.capture.side_effect=[dirty,dirty,empty]
        ui.search_control_cache={'key':{'hwnd':0,'pid':None,'origin':(0,0),'geometry':None,'dpi':None},'bounds':[66,25,256,50]}
        ui.clear_search()
        self.assertEqual([c.args[2] for c in ui.click.call_args_list],['clear_search_icon','focus_search_cleanup_fallback'])
        self.assertEqual(ui.click.call_args_list[1].args[1]['point'],[161,37])
        ui.replace.assert_called_once_with(dirty,'')
        self.assertEqual(ui.capture.call_count,3)
        self.assertEqual(ui.cleanup_action,'clear_fallback_deleted_once')

    def test_both_attempts_fail_no_third_delete(self):
        ui=self.native(header(query='13800000003'))
        with self.assertRaises(f.FlowStop):ui.clear_search()
        self.assertEqual(ui.click.call_count,2);self.assertEqual(ui.replace.call_count,1)
        self.assertEqual(ui.capture.call_count,3)

    def test_icon_click_clears_input_without_placeholder_and_no_fallback(self):
        ui=self.native(header(query='13800000003'),header())
        ui.clear_search();ui.replace.assert_not_called()
        self.assertEqual(ui.click.call_count,1)

    def test_other_input_after_icon_click_never_deleted(self):
        ui=self.native(header(query='13800000003'),header(query='13900000004'))
        with self.assertRaisesRegex(f.FlowStop,'OWNERSHIP'):ui.clear_search()
        self.assertEqual(ui.click.call_count,1);ui.replace.assert_not_called()

    def test_absent_ocr_cannot_authorize_fallback_or_click(self):
        dirty=header(query='13800000003');dirty.rows=[]
        ui=self.native(dirty)
        with self.assertRaisesRegex(f.FlowStop,'OWNERSHIP'):ui.clear_search()
        ui.click.assert_not_called();ui.replace.assert_not_called()

    def test_text_unreadable_is_not_empty(self):
        for height in (25,31,38,50):
            for glyph in ('1','I','x','12'):
                with self.subTest(height=height,glyph=glyph):
                    fr=header(height,query=glyph);fr.rows=[]
                    with self.assertRaises(f.FlowStop):f.empty_search(fr)

    def test_unmatched_clear_icon_falls_back_only_with_owned_content(self):
        dirty=header(query='13800000003')
        dirty.image.paste((249,249,249),(232,26,255,49))
        ui=self.native(dirty,header())
        ui.clear_search()
        self.assertEqual(ui.click.call_count,1)
        self.assertEqual(ui.click.call_args.args[2],'focus_search_cleanup_fallback')
        ui.replace.assert_called_once_with(dirty,'')

    def test_plain_x_cannot_substitute_clear(self):
        fr=header(query='13800000003')
        fr.image.paste((249,249,249),(231,25,256,50))
        ImageDraw.Draw(fr.image).text((239,29),'X',fill=(90,90,90))
        with self.assertRaisesRegex(f.FlowStop,'SEARCH_CLEAR_ICON'):f.search_clear_target(fr)

    def test_wrong_frame_and_absent_pixels_not_clickable(self):
        fr=header(query='13800000003');fr.name='footer'
        with self.assertRaises(f.FlowStop):f.search_clear_target(fr)
        fr=header();fr.image=Image.new('RGB',fr.image.size,'white')
        with self.assertRaises(f.FlowStop):f.empty_search(fr)

    def test_clear_icon_outside_input_is_ignored(self):
        fr=header(query='13800000003')
        icon=fr.image.crop((231,25,256,50));fr.image.paste((249,249,249),(231,25,256,50))
        fr.image.paste(icon,(320,25))
        with self.assertRaisesRegex(f.FlowStop,'SEARCH_CLEAR_ICON'):f.search_clear_target(fr)

    def test_cache_reused_for_cleanup_without_relocating(self):
        from unittest.mock import patch
        empty=header();dirty=header(query='13800000003')
        ui=self.native(dirty,empty)
        ui.verified_empty_search(empty)  # initial task preparation
        with patch.object(f,'search_control_bounds',side_effect=AssertionError('must use cached position')):
            ui.clear_search()
        self.assertEqual(ui.capture.call_count,2)

    def test_internal_input_moved_with_same_window_does_not_use_old_point(self):
        empty=header();dirty=header(query='13800000003');ui=self.native(dirty)
        ui.verified_empty_search(empty)
        control=dirty.image.crop((66,25,256,50));dirty.image.paste((247,247,247),(66,25,256,50));dirty.image.paste(control,(80,25))
        with self.assertRaisesRegex(f.FlowStop,'GEOMETRY_CHANGED'):ui.clear_search()
        ui.click.assert_not_called();ui.replace.assert_not_called()

    def test_geometry_or_window_change_invalidates_cached_position(self):
        empty=header();ui=self.native(empty);ui.verified_empty_search(empty)
        for field,value in [('hwnd',99),('origin',(200,0)),('geometry',{'width':999})]:
            import copy
            changed=copy.deepcopy(empty);setattr(changed,field,value)
            self.assertIsNone(ui.cached_search_bounds(changed))
            self.assertNotIn('bounds',ui.search_control_cache)

    def test_shared_cache_reused_between_tasks_but_not_other_window(self):
        empty=header();first=self.native(empty);first.verified_empty_search(empty)
        second=self.native(empty);second.search_control_cache=first.search_control_cache
        self.assertEqual(second.cached_search_bounds(empty),[66,25,256,50])
        empty.hwnd=99
        self.assertIsNone(second.cached_search_bounds(empty))


class FlowCleanupIntegration(unittest.TestCase):
    def test_real_flow_cleanup_and_journal_survive_fallback_success_and_failure(self):
        import tempfile
        from pathlib import Path
        from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI
        for cleanup_succeeds in (True,False):
            with self.subTest(cleanup_succeeds=cleanup_succeeds), tempfile.TemporaryDirectory() as folder:
                flow=ReplayUI()
                dirty=header(query='13800000003');empty=header()
                cleanup=SearchControls().native(dirty)
                cleanup.capture.side_effect=[dirty,dirty,empty if cleanup_succeeds else dirty]
                def do_cleanup():
                    cleanup.clear_search()
                    flow.cleanup_action=cleanup.cleanup_action
                flow.clear_search=do_cleanup
                journal=f.SubmitJournal(Path(folder)/'submit.json')
                payload=dict(phone='13800000003',verify_message='测试',remark_name='测试 CJH5N28',remark_code='CJH5N28')
                result=f.run_flow(flow,journal=journal,**payload)
                self.assertEqual(result['status'],'submitted_pending_recipient')
                self.assertTrue(result['submit_attempted']);self.assertFalse(result['delivery_confirmed'])
                self.assertEqual(result['cleanup'],'search_empty_verified' if cleanup_succeeds else 'failed_preserve_business_outcome')
                self.assertEqual(flow.actions.count('confirm_invite_once'),1)
                original=journal.path.read_bytes()
                retry=ReplayUI();again=f.run_flow(retry,journal=journal,**payload)
                self.assertEqual(again['error_code'],'PREVIOUS_SUBMIT_ATTEMPT_REQUIRES_REVIEW')
                self.assertEqual(retry.actions,[]);self.assertEqual(journal.path.read_bytes(),original)
