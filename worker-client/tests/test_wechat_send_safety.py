from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from PIL import Image, ImageDraw


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


class WechatSendSafetyTest(unittest.TestCase):
    def test_generic_journal_updates_matching_voice_item_before_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-action.json"
            path.write_text(
                json.dumps(
                    {
                        "action_kind": "voice",
                        "action_phase": "not_attempted",
                        "items": {
                            "source-1": {
                                "source_message_key": "source-1",
                                "physical_anchor_keys": ["anchor-1"],
                                "action_phase": "not_attempted",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            sidecar.write_action_phase_journal(
                str(path),
                "trigger_attempted",
                physical_anchor_keys=["anchor-1"],
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["items"]["source-1"]["action_phase"],
                "trigger_attempted",
            )

    def test_voice_journal_terminal_update_covers_every_physical_alias_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-action.json"
            path.write_text(
                json.dumps(
                    {
                        "action_kind": "voice",
                        "action_phase": "not_attempted",
                        "items": {
                            "source-stable": {
                                "source_message_key": "source-stable",
                                "physical_anchor_keys": ["voice-stable:one"],
                                "action_phase": "not_attempted",
                            },
                            "source-structural": {
                                "source_message_key": "source-structural",
                                "physical_anchor_keys": [
                                    "voice-structural:one"
                                ],
                                "action_phase": "not_attempted",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            sidecar.write_action_phase_journal(
                str(path),
                "confirmed",
                physical_anchor_keys=[
                    "voice-stable:one",
                    "voice-structural:one",
                ],
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={"state": "completed"},
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["action_phase"], "confirmed")
            self.assertEqual(
                {
                    item["action_phase"]
                    for item in payload["items"].values()
                },
                {"confirmed"},
            )
            self.assertTrue(
                all(
                    item["business_result_confirmed"]
                    for item in payload["items"].values()
                )
            )

    def test_generic_journal_rejects_missing_or_uninitialized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-action.json"

            with self.assertRaisesRegex(
                ValueError,
                "ACTION_JOURNAL_NOT_INITIALIZED",
            ):
                sidecar.write_action_phase_journal(
                    str(path),
                    "trigger_attempted",
                    physical_anchor_keys=["anchor-1"],
                )

            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "ACTION_JOURNAL_ITEMS_MISSING",
            ):
                sidecar.write_action_phase_journal(
                    str(path),
                    "trigger_attempted",
                    physical_anchor_keys=["anchor-1"],
                )

    def test_send_journal_is_persisted_before_enter_trigger(self):
        events: list[str] = []

        with patch.object(
            sidecar,
            "key_press",
            side_effect=lambda *_args: events.append("enter"),
        ):
            result = sidecar.safe_send_trigger(
                100,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: events.append(
                    "trigger_attempted"
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["trigger_attempted", "enter"])

    def test_enter_exception_after_journal_is_trigger_attempted_unknown(self):
        events: list[str] = []
        with patch.object(
            sidecar,
            "key_press",
            side_effect=RuntimeError("keyboard disconnected"),
        ):
            result = sidecar.safe_send_trigger(
                100,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: events.append(
                    "trigger_attempted"
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(events, ["trigger_attempted"])
        self.assertTrue(result["physical_send_triggered"])
        self.assertEqual(result["action_phase"], "trigger_attempted")
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")

    def test_green_send_button_is_readiness_evidence_not_click_target(self):
        image = Image.new("RGB", (980, 860), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((870, 790, 950, 832), fill=(26, 190, 92))

        result = sidecar.send_button_ready_evidence(
            image,
            geometry={
                "width": 980,
                "height": 860,
            },
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["used_as_click_target"])

    def test_missing_green_send_button_does_not_block_verified_enter(self):
        frame = Image.new("RGB", (960, 820), "white")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {
                        "ok": False,
                        "reason": "active_green_send_button_not_observed",
                    },
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "method": "keyboard_enter",
                    "physical_send_triggered": True,
                },
            ) as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=lambda **_kwargs: {"ok": True},
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["send_button_ready"]["ok"])
        trigger.assert_called_once()

    def test_green_send_button_never_overrides_changed_context(self):
        frame = Image.new("RGB", (960, 820), "white")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": True,
                    "cleared": True,
                    "reason": "confirmed_program_draft_cleared",
                },
            ),
            patch.object(sidecar, "safe_send_trigger") as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=lambda **_kwargs: {
                    "ok": False,
                    "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C3_CONTEXT_CHANGED_BEFORE_SEND",
        )
        trigger.assert_not_called()

    def test_visual_input_uses_green_as_evidence_and_enter_as_trigger(self):
        observed_trigger: dict[str, object] = {}
        with (
            patch.object(
                sidecar,
                "locate_visual_send_input",
                return_value={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                    "physical_send_triggered": False,
                },
            ),
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_mode": "sendinput_unicode",
                    "send_button_ready": {
                        "ok": True,
                        "reason": "active_green_send_button_observed",
                        "used_as_click_target": False,
                    },
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                side_effect=lambda *_args, **kwargs: (
                    observed_trigger.update(kwargs)
                    or {
                        "ok": True,
                        "method": "keyboard_enter",
                        "send_trigger_mode": "enter_only",
                    }
                ),
            ),
        ):
            result = sidecar.send_with_visual_input(
                100,
                "AI回复",
                geometry={"width": 980, "height": 860},
                before_input_region_seed={
                    "input_region": {
                        "has_visible_text": False,
                        "bounds": [400, 680, 880, 800],
                    }
                },
                before_send_trigger_check=lambda: {"ok": True},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(observed_trigger["trigger_mode"], "enter_only")
        self.assertNotIn("send_point", observed_trigger)
        self.assertIn("keyboard_enter", result["method"])

    def test_unconfirmed_cleanup_becomes_hard_send_block(self):
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": False},
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": False,
                    "cleared": False,
                    "reason": "program_draft_not_proven",
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_DRAFT_CLEANUP_UNCONFIRMED",
        )
        self.assertFalse(result["physical_send_triggered"])

    def test_input_exception_never_deletes_coincidentally_matching_human_draft(self):
        value_pattern = SimpleNamespace(Value="AI回复")
        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                side_effect=RuntimeError("input driver stopped"),
            ),
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar, "hotkey") as hotkey,
            patch.object(sidecar, "key_press") as key_press,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "value_pattern": value_pattern,
                },
                geometry={"width": 960, "height": 820},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_DRAFT_CLEANUP_UNCONFIRMED",
        )
        self.assertEqual(
            result["paste"]["input_result"]["input_progress"],
            "unknown",
        )
        self.assertIsNone(
            result["paste"]["input_result"]["typed_chars"],
        )
        click.assert_not_called()
        hotkey.assert_not_called()
        key_press.assert_not_called()

    def test_post_input_frame_is_reused_for_context_check(self):
        frame = Image.new("RGB", (960, 820), "white")
        observed: dict[str, object] = {}

        def context_check(**kwargs):
            observed.update(kwargs)
            return {"ok": True}

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                    "_post_input_screenshot_path": "post-input.png",
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertTrue(result["ok"])
        self.assertIs(observed["screenshot"], frame)
        self.assertEqual(
            observed["screenshot_path"],
            "post-input.png",
        )
        self.assertNotIn("ocr_items", observed)

    def test_post_input_full_ocr_is_reused_for_context_check(self):
        frame = Image.new("RGB", (960, 820), "white")
        ocr_items = [{"text": "CJUAT728", "left": 400, "top": 20}]
        observed: dict[str, object] = {}

        def context_check(**kwargs):
            observed.update(kwargs)
            return {"ok": True}

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                    },
                    "send_button_ready": {"ok": True},
                    "_post_input_screenshot": frame,
                    "_post_input_screenshot_path": "post-input.png",
                    "_post_input_ocr_items": ocr_items,
                },
            ),
            patch.object(
                sidecar,
                "safe_send_trigger",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "visual_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertTrue(result["ok"])
        self.assertIs(observed["screenshot"], frame)
        self.assertIs(observed["ocr_items"], ocr_items)

    def test_send_reuses_one_baseline_and_first_post_send_confirmation_frame(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 960,
            "bottom": 820,
            "width": 960,
            "height": 820,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
        }
        baseline = {
            "ok": True,
            "validation": validation,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
        }
        post_send = {
            "ok": True,
            "validation": validation,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 1,
            "message_sequence": [
                {
                    "sequence_index": 0,
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_normalized": "AI回复",
                }
            ],
            "observations": [
                {
                    "observation_id": "self-new",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": "AI回复",
                }
            ],
        }
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                side_effect=[baseline, post_send],
            ) as capture,
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": True,
                    "method": "visual_input.sendinput_unicode+keyboard_enter",
                    "physical_send_triggered": True,
                },
            ),
            patch.object(sidecar, "validate_post_send_target") as old_post_guard,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJUAT728",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            capture.call_args_list[0].kwargs["label"],
            "send_baseline",
        )
        self.assertEqual(
            capture.call_args_list[1].kwargs["label"],
            "send_post_guard_and_result_confirm_1",
        )
        old_post_guard.assert_not_called()

    def test_all_pre_send_pacing_finishes_before_final_frame_capture(self):
        frame = Image.new("RGB", (960, 820), "white")
        events: list[tuple[object, ...]] = []

        def wait(low, high):
            events.append(("wait", low, high))

        def capture(_hwnd, *, artifact_dir=None, label="capture"):
            events.append(("capture", label))
            return frame, f"{label}.png"

        with (
            patch.object(sidecar, "activate_window"),
            patch.object(sidecar.time, "sleep"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "type_text_with_sendinput_unicode",
                return_value={
                    "ok": True,
                    "method": "sendinput_unicode",
                    "typed_chars": 4,
                },
            ),
            patch.object(sidecar, "humanized_sleep_ms", side_effect=wait),
            patch.object(sidecar, "capture_wechat", side_effect=capture),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": True},
            ),
            patch.object(
                sidecar,
                "input_region_visual_delta_confirms",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "run_ocr_for_input_confirmation",
                return_value=([], "roi"),
            ),
            patch.object(
                sidecar,
                "send_button_ready_evidence",
                return_value={"ok": False},
            ),
        ):
            result = sidecar.paste_text_with_confirmation(
                1,
                "AI回复",
                points={"input_point": [650, 720], "send_point": None},
                geometry={"width": 960, "height": 820},
                settings={
                    "enabled": True,
                    "method": "sendinput_unicode",
                    "send_post_input_delay_min_ms": 11,
                    "send_post_input_delay_max_ms": 12,
                    "send_trigger_delay_min_ms": 21,
                    "send_trigger_delay_max_ms": 22,
                },
                before_input_region_seed={
                    "input_region": {"has_visible_text": False},
                },
                verified_input_point=(650, 720),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            [
                ("wait", 11, 12),
                ("wait", 21, 22),
                ("capture", "send_input_probe_1"),
            ],
        )

    def test_message_arriving_during_wait_is_seen_in_final_frame_and_blocks_enter(self):
        frame = Image.new("RGB", (960, 820), "white")
        frame.info["new_customer_message"] = True

        def context_check(*, screenshot=None, **_kwargs):
            return {
                "ok": not bool(
                    screenshot
                    and screenshot.info.get("new_customer_message")
                ),
                "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND",
            }

        with (
            patch.object(
                sidecar,
                "paste_text_with_confirmation",
                return_value={
                    "ok": True,
                    "input_result": {"ok": True, "typed_chars": 4},
                    "send_button_ready": {"ok": False},
                    "_post_input_screenshot": frame,
                },
            ),
            patch.object(
                sidecar,
                "clear_confirmed_program_draft",
                return_value={
                    "ok": True,
                    "cleared": True,
                    "reason": "confirmed_program_draft_cleared",
                },
            ),
            patch.object(sidecar, "safe_send_trigger") as trigger,
        ):
            result = sidecar.execute_send_transaction(
                1,
                "AI回复",
                locator={
                    "ok": True,
                    "path": "uia_input",
                    "input_point": (650, 720),
                    "value_pattern": None,
                },
                geometry={"width": 960, "height": 820},
                before_send_trigger_check=context_check,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C3_CONTEXT_CHANGED_BEFORE_SEND",
        )
        trigger.assert_not_called()

    def test_action_journal_failure_never_presses_enter(self):
        with patch.object(sidecar, "key_press") as key_press:
            result = sidecar.safe_send_trigger(
                1,
                trigger_mode="enter_only",
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: (_ for _ in ()).throw(
                    OSError("disk full")
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "SEND_ACTION_JOURNAL_WRITE_FAILED",
        )
        key_press.assert_not_called()

    def test_mismatched_copyback_releases_selection_without_editing_draft(self):
        with (
            patch.object(
                sidecar,
                "clipboard_read",
                side_effect=["原剪贴板", "销售人工草稿"],
            ),
            patch.object(sidecar, "clipboard_copy"),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(sidecar, "hotkey"),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.confirm_exact_program_draft_focus(
                1,
                input_point=(650, 720),
                expected_text="AI回复",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "focused_input_draft_mismatch",
        )
        self.assertTrue(result["selection_released"])
        key_press.assert_called_once_with(0x27)

    def test_copyback_exception_after_select_all_releases_selection(self):
        with (
            patch.object(
                sidecar,
                "clipboard_read",
                return_value="原剪贴板",
            ),
            patch.object(sidecar, "clipboard_copy"),
            patch.object(sidecar, "human_client_click"),
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "hotkey",
                side_effect=RuntimeError("select-all driver stopped"),
            ),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.confirm_exact_program_draft_focus(
                1,
                input_point=(650, 720),
                expected_text="AI回复",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "input_focus_copyback_failed",
        )
        self.assertTrue(result["selection_released"])
        key_press.assert_called_once_with(0x27)

    def test_send_context_guard_blocks_new_message_after_final_refresh(self):
        expected = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "在吗",
                }
            ]
        )
        current = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "在吗",
                },
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "补充一句",
                },
            ]
        )

        result = sidecar.validate_send_context_guard(expected, current)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C3_CONTEXT_CHANGED_BEFORE_SEND")
        self.assertEqual(result["expected_message_count"], 1)
        self.assertEqual(result["current_message_count"], 2)

    def test_send_context_guard_accepts_same_structure_without_coordinates(self):
        expected = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "voice_transcript",
                    "sender_role": "customer",
                    "content_clean": "我下午有空",
                    "parent_voice_anchor_key": "voice:customer:4",
                    "bubble_rect": {"left": 100, "top": 200},
                }
            ]
        )
        current = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "voice_transcript",
                    "sender_role": "customer",
                    "content_clean": "我下午有空",
                    "parent_voice_anchor_key": "voice:customer:4",
                    "bubble_rect": {"left": 100, "top": 260},
                }
            ]
        )

        result = sidecar.validate_send_context_guard(expected, current)

        self.assertTrue(result["ok"])

    def test_send_reply_match_count_requires_self_role_and_exact_normalized_text(self):
        messages = [
            {"sender_role": "customer", "content": "您好，可以继续沟通"},
            {"sender_role": "self", "content": "您好， 可以继续沟通"},
            {"sender_role": "self", "content": "另一条"},
        ]

        self.assertEqual(sidecar.send_reply_match_count(messages, "您好，可以继续沟通"), 1)

    def test_unknown_input_draft_is_preserved_without_any_delete_key(self):
        geometry = {"width": 960, "height": 820}
        before_seed = {
            "input_region": {
                "has_visible_text": True,
                "reason": "ocr_or_dark_pixels",
                "ocr_hits": 1,
                "dark_ratio": 0.03,
                "mean": 240.0,
            }
        }
        with (
            patch.object(sidecar, "activate_window"),
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "key_press") as key_press,
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.paste_text_with_confirmation(
                1,
                "AI回复",
                points={"input_point": [650, 720], "send_point": [900, 775]},
                geometry=geometry,
                before_input_region_seed=before_seed,
                verified_input_point=(650, 720),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_input_draft_present")
        self.assertEqual(result["error_code"], "WECHAT_INPUT_DRAFT_PRESENT")
        key_press.assert_not_called()
        click.assert_not_called()

    def test_confirmed_program_draft_is_cleared_after_input_confirmation_failure(self):
        class FakeValuePattern:
            def __init__(self):
                self.value = "AI回复"

            @property
            def Value(self):
                return self.value

        value_pattern = FakeValuePattern()

        def clear_value(_key):
            value_pattern.value = ""

        with (
            patch.object(sidecar, "human_client_click"),
            patch.object(sidecar, "hotkey"),
            patch.object(sidecar, "key_press", side_effect=clear_value),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.clear_confirmed_program_draft(
                1,
                value_pattern=value_pattern,
                input_point=(650, 720),
                expected_text="AI回复",
                paste_result={
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                        "method": "clipboard_chunks",
                    }
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["cleared"])
        self.assertEqual(result["reason"], "confirmed_program_draft_cleared")
        self.assertEqual(value_pattern.value, "")

    def test_unproven_or_changed_draft_is_never_cleared(self):
        value_pattern = SimpleNamespace(Value="销售人工草稿")
        with (
            patch.object(sidecar, "human_client_click") as click,
            patch.object(sidecar, "hotkey") as hotkey,
            patch.object(sidecar, "key_press") as key_press,
        ):
            result = sidecar.clear_confirmed_program_draft(
                1,
                value_pattern=value_pattern,
                input_point=(650, 720),
                expected_text="AI回复",
                paste_result={
                    "input_result": {
                        "ok": True,
                        "typed_chars": 4,
                        "method": "clipboard_chunks",
                    }
                },
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["reason"], "program_draft_not_proven")
        click.assert_not_called()
        hotkey.assert_not_called()
        key_press.assert_not_called()

    def test_sent_confirmation_requires_new_matching_bubble_and_empty_input(self):
        baseline_sequence = [
            {
                "sequence_index": 0,
                "observation_id": "customer-1",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "content_normalized": "在吗",
            }
        ]
        snapshots = [
            {
                "ok": True,
                "matching_self_message_count": 1,
                "input_region": {"has_visible_text": True},
                "message_sequence": baseline_sequence,
                "observations": [],
            },
            {
                "ok": True,
                "matching_self_message_count": 2,
                "input_region": {"has_visible_text": False},
                "message_sequence": [
                    *baseline_sequence,
                    {
                        "sequence_index": 1,
                        "observation_id": "self-new",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": "AI回复",
                    },
                ],
                "observations": [
                    {
                        "observation_id": "self-new",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_clean": "AI回复",
                    }
                ],
            },
        ]
        with (
            patch.object(sidecar, "capture_send_fact_snapshot", side_effect=snapshots),
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=1,
                baseline_message_sequence=baseline_sequence,
                artifact_dir=None,
                max_attempts=2,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "new_stable_self_bubble_and_empty_input")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["confirmed_observation"]["observation_id"], "self-new")

    def test_sent_confirmation_returns_unknown_when_only_window_is_readable(self):
        with (
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                return_value={
                    "ok": True,
                    "matching_self_message_count": 2,
                    "input_region": {"has_visible_text": False},
                    "message_sequence": [
                        {
                            "sequence_index": 0,
                            "observation_id": "self-old",
                            "row_kind": "text_bubble",
                            "sender_role": "self",
                            "content_normalized": "AI回复",
                        }
                    ],
                    "observations": [],
                },
            ),
            patch.object(sidecar.time, "sleep"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=1,
                baseline_message_sequence=[
                    {
                        "sequence_index": 0,
                        "observation_id": "self-old",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": "AI回复",
                    }
                ],
                artifact_dir=None,
                max_attempts=2,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")

    def test_blank_window_after_physical_send_is_always_unknown(self):
        geometry = {"width": 960, "height": 820}
        strict_guard = {
            "ok": True,
            "online": True,
            "geometry": geometry,
            "title": "CJTEST01",
        }
        baseline = {
            "ok": True,
            "validation": strict_guard,
            "input_region": {"has_visible_text": False},
            "send_context_guard": {"schema_version": 1, "sequence": []},
            "matching_self_message_count": 0,
            "message_sequence": [],
        }
        with (
            patch.object(
                sidecar,
                "recover_send_window_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value=strict_guard,
            ),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(
                sidecar,
                "validate_send_geometry",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "consume_input_region_precheck_ocr_seed",
                return_value=None,
            ),
            patch.object(
                sidecar,
                "capture_send_fact_snapshot",
                return_value=baseline,
            ),
            patch.object(
                sidecar,
                "validate_send_context_guard",
                return_value={"ok": True},
            ),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": True,
                    "physical_send_triggered": True,
                },
            ),
            patch.object(
                sidecar,
                "validate_post_send_target",
                return_value={"ok": False, "reason": "blank_render"},
            ),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard={"schema_version": 1, "sequence": []},
                validated_guard=strict_guard,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_RESULT_UNKNOWN")
        self.assertTrue(result["physical_send_triggered"])
        self.assertTrue(result["send_result"]["physical_send_triggered"])

    def test_uia_screen_coordinates_use_screen_to_client(self):
        fake_win32gui = SimpleNamespace(
            ScreenToClient=lambda hwnd, point: (
                point[0] - 108,
                point[1] - 132,
            )
        )
        with patch.object(sidecar, "win32gui", fake_win32gui):
            self.assertEqual(
                sidecar.screen_point_to_client(1001, 910, 742),
                (802, 610),
            )

    def test_formal_send_uses_visual_input_then_enter_without_uia(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "input_region": {
                "has_visible_text": False,
                "bounds": [400, 680, 880, 800],
            },
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": {
                "schema_version": 1,
                "sequence": [],
                "message_count": 0,
                "bottom": None,
            },
        }
        visual_calls: list[str] = []
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                side_effect=lambda *_args, **_kwargs: (
                    visual_calls.append("visual")
                    or {
                        "ok": True,
                        "method": "visual_input.sendinput_unicode+keyboard_enter",
                        "physical_send_triggered": True,
                    }
                ),
            ),
            patch.object(sidecar, "validate_post_send_target", return_value={"ok": True}),
            patch.object(
                sidecar,
                "confirm_reply_sent",
                return_value={"ok": True, "reason": "new_self_bubble"},
            ),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=baseline["send_context_guard"],
                validated_guard=strict_guard,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(visual_calls, ["visual"])
        self.assertFalse(hasattr(sidecar, "send_with_uia_controls"))
        self.assertEqual(result["send_result"]["mode"], "visual_only")
        self.assertIn("keyboard_enter", result["send_result"]["method"])

    def test_focused_blank_visual_input_reaches_one_enter_with_journal(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 981,
            "bottom": 860,
            "width": 981,
            "height": 860,
        }
        frame = Image.new("RGB", (981, 860), "white")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((408, 709, 409, 731), fill="black")
        draw.rectangle((394, 798, 520, 801), fill="black")
        input_region = sidecar.input_text_region_state(
            frame,
            [],
            geometry=geometry,
        )
        self.assertFalse(input_region["has_visible_text"])
        self.assertTrue(input_region["caret_like_dark_pixels"])

        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "input_region": input_region,
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
        }
        enter_events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "send-action.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "action_kind": "send",
                        "action_phase": "not_attempted",
                        "items": {
                            "reply-1": {
                                "source_message_key": "reply-1",
                                "action_phase": "not_attempted",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def record_enter(_key):
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["action_phase"], "trigger_attempted")
                enter_events.append("enter")

            with (
                patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
                patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
                patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
                patch.object(sidecar, "get_window_geometry", return_value=geometry),
                patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
                patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
                patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
                patch.object(
                    sidecar,
                    "paste_text_with_confirmation",
                    return_value={
                        "ok": True,
                        "input_mode": "sendinput_unicode",
                        "input_result": {"ok": True, "typed_chars": 4},
                        "send_button_ready": {
                            "ok": False,
                            "reason": "active_green_send_button_not_observed",
                            "used_as_click_target": False,
                        },
                        "_post_input_screenshot": frame,
                    },
                ),
                patch.object(
                    sidecar,
                    "build_send_fact_snapshot_from_frame",
                    return_value=baseline,
                ),
                patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
                patch.object(
                    sidecar,
                    "confirm_exact_program_draft_focus",
                    return_value={"ok": True, "reason": "exact_program_draft_confirmed"},
                ),
                patch.object(sidecar, "key_press", side_effect=record_enter),
                patch.object(sidecar, "validate_post_send_target", return_value={"ok": True}),
                patch.object(
                    sidecar,
                    "confirm_reply_sent",
                    return_value={"ok": True, "reason": "new_self_bubble"},
                ),
                patch.object(sidecar, "humanized_action_sleep"),
            ):
                result = sidecar.send_payload(
                    1,
                    {"ok": True},
                    target="CJUAT728",
                    text="AI回复",
                    exact=False,
                    skip_send_rate_guard=True,
                    expected_context_guard=context_guard,
                    validated_guard=strict_guard,
                    action_journal_path=str(journal_path),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["send_result"]["mode"], "visual_only")
        self.assertEqual(enter_events, ["enter"])

    def test_formal_send_failure_is_reported_from_the_single_visual_path(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        strict_guard = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": {
                "schema_version": 1,
                "sequence": [],
                "message_count": 0,
                "bottom": None,
            },
        }
        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "validate_active_send_target", return_value=strict_guard),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", return_value=baseline),
            patch.object(sidecar, "validate_send_context_guard", return_value={"ok": True}),
            patch.object(
                sidecar,
                "send_with_visual_input",
                return_value={
                    "ok": False,
                    "reason": "visual_input_not_confirmed",
                    "error_code": "SEND_INPUT_NOT_READY",
                    "physical_send_triggered": False,
                },
            ),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJTEST01",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=baseline["send_context_guard"],
                validated_guard=strict_guard,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_INPUT_NOT_READY")
        self.assertFalse(result["physical_send_triggered"])
        self.assertFalse(hasattr(sidecar, "send_with_uia_controls"))

    def test_formal_send_payload_has_one_visual_locator(self):
        source = inspect.getsource(sidecar.send_payload)

        self.assertIn("send_with_visual_input(", source)
        self.assertNotIn("send_with_uia_controls(", source)
        self.assertIn("validate_send_context_guard(", source)
        self.assertIn("pre_trigger_context_check", source)
        self.assertNotIn("calculate_send_points(", source)

    def test_send_action_current_only_gate_precedes_all_send_work(self):
        source = Path(sidecar.__file__).read_text(encoding="utf-8")
        start = source.index('    if action == "send":')
        end = source.index("def use_passive_probe_mode(", start)
        send_action = source[start:end]

        gate = send_action.index("SEND_CURRENT_CHAT_ONLY_REQUIRED")
        payload_call = send_action.index("send_payload(")
        self.assertLess(gate, payload_call)
        self.assertNotIn("open_chat(", send_action)


if __name__ == "__main__":
    unittest.main()
