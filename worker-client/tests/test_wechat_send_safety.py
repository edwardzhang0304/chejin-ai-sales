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


INCIDENT_LONG_REPLY = (
    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
    "你主要家用、通勤还是跑长途，更偏轿车还是SUV，"
    "对空间、油耗或能源类型有要求吗？"
)


def incident_post_send_enhanced_ocr_items() -> list[dict[str, object]]:
    """Exact seven OCR records captured from the 2026-08-15 incident."""

    texts = [
        "可以，10万左右可以按你的需求帮你筛选合适",
        "的二手车。",
        "你主要家用、",
        "通勤还是跑长途，更",
        "偏轿车还是SUV，对空间、",
        "油耗或能源类型有",
        "要求吗?",
    ]
    boxes = [
        (505.0, 552.0, 859.5, 571.5),
        (506.5, 577.0, 601.0, 594.5),
        (597.0, 577.0, 708.0, 594.5),
        (701.0, 578.0, 860.5, 594.5),
        (505.5, 600.5, 705.5, 619.0),
        (714.0, 601.0, 855.0, 619.5),
        (505.5, 623.5, 571.5, 642.5),
    ]
    confidences = [0.9969, 0.9204, 0.9975, 0.9965, 0.9760, 0.9988, 0.9276]
    return [
        {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": (left + right) / 2,
            "center_y": (top + bottom) / 2,
            "confidence": confidence,
        }
        for text, (left, top, right, bottom), confidence in zip(
            texts,
            boxes,
            confidences,
            strict=True,
        )
    ]


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

    def test_send_context_guard_ignores_sidebar_only_visual_change(self):
        before = Image.new("RGB", (981, 860), "white")
        after = before.copy()
        ImageDraw.Draw(before).rectangle((40, 210, 75, 235), fill="red")
        ImageDraw.Draw(after).rectangle((40, 210, 88, 235), fill="red")
        before_region = sidecar.send_context_message_region_fingerprint(before)
        after_region = sidecar.send_context_message_region_fingerprint(after)
        self.assertEqual(before_region["sha256"], after_region["sha256"])

        expected = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "明天继续磨，有点烦了",
                }
            ],
            message_region_sha256=before_region["sha256"],
            message_region_bounds=before_region["bounds"],
        )
        # Full-window OCR can vary when an unrelated sidebar badge changes.
        current = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "明天继续磨有点烦了",
                }
            ],
            message_region_sha256=after_region["sha256"],
            message_region_bounds=after_region["bounds"],
        )

        result = sidecar.validate_send_context_guard(expected, current)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "message_region_unchanged")

    def test_send_context_guard_still_blocks_current_chat_visual_change(self):
        before = Image.new("RGB", (981, 860), "white")
        after = before.copy()
        ImageDraw.Draw(after).rectangle((520, 520, 800, 570), fill="gray")
        before_region = sidecar.send_context_message_region_fingerprint(before)
        after_region = sidecar.send_context_message_region_fingerprint(after)
        self.assertNotEqual(before_region["sha256"], after_region["sha256"])

        expected = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "在吗",
                }
            ],
            message_region_sha256=before_region["sha256"],
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
            ],
            message_region_sha256=after_region["sha256"],
        )

        result = sidecar.validate_send_context_guard(expected, current)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C3_CONTEXT_CHANGED_BEFORE_SEND")

    def test_pre_enter_target_switch_blocks_even_when_message_region_is_identical(self):
        geometry = {
            "left": 0,
            "top": 0,
            "right": 981,
            "bottom": 860,
            "width": 981,
            "height": 860,
        }
        context_guard = {
            "schema_version": 1,
            "sequence": [],
            "message_count": 0,
            "bottom": None,
            "message_region_sha256": "a" * 64,
        }
        strict_target = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        baseline = {
            "ok": True,
            "validation": strict_target,
            "input_region": {"has_visible_text": False},
            "matching_self_message_count": 0,
            "message_sequence": [],
            "send_context_guard": context_guard,
        }
        switched_target = {
            **baseline,
            "ok": False,
            "validation": {
                "ok": False,
                "reason": "active_title_target_mismatch",
                "confirmation_confidence": "target_mismatch",
                "geometry": geometry,
            },
        }
        captured_check: dict[str, object] = {}

        def reject_after_target_switch(*_args, **kwargs):
            check = kwargs["before_send_trigger_check"]()
            captured_check.update(check)
            return {
                **check,
                "physical_send_triggered": False,
            }

        with (
            patch.object(sidecar, "recover_send_window_guard", return_value={"ok": True}),
            patch.object(sidecar, "active_send_guard_is_strong", side_effect=lambda value: value.get("confirmation_confidence") == "active_title_strict"),
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_send_geometry", return_value={"ok": True}),
            patch.object(sidecar, "consume_input_region_precheck_ocr_seed", return_value=None),
            patch.object(sidecar, "capture_send_fact_snapshot", side_effect=[baseline, switched_target]),
            patch.object(sidecar, "send_with_visual_input", side_effect=reject_after_target_switch),
        ):
            result = sidecar.send_payload(
                1,
                {"ok": True},
                target="CJV6P3R8",
                text="AI回复",
                exact=False,
                skip_send_rate_guard=True,
                expected_context_guard=context_guard,
                validated_guard=strict_target,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "SEND_TARGET_NOT_CONFIRMED")
        self.assertEqual(captured_check["reason"], "send_target_not_confirmed_before_enter")
        self.assertFalse(result["physical_send_triggered"])

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

    def test_post_send_enhanced_ocr_recovers_exact_self_text_from_image_candidate(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "visual-self-candidate",
            "type": "image",
            "message_type": "image",
            "sender": "unknown",
            "sender_role": "unknown",
            "visual_side": "self",
            "is_self_image": True,
            "content": "[图片]",
            "bubble_rect": [489, 505, 878, 613],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def enhanced_ocr(_image):
            return [
                {
                    "text": "你好，10万左右我可以按预算帮你筛选。",
                    "left": 20,
                    "top": 16,
                    "right": 410,
                    "bottom": 48,
                    "center_x": 215,
                    "center_y": 32,
                    "confidence": 0.99,
                },
                {
                    "text": "你更偏向轿车还是SUV？",
                    "left": 20,
                    "top": 58,
                    "right": 310,
                    "bottom": 90,
                    "center_x": 165,
                    "center_y": 74,
                    "confidence": 0.98,
                },
            ]

        expected = "你好，10万左右我可以按预算帮你筛选。你更偏向轿车还是SUV？"
        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text=expected,
                ocr_runner=enhanced_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(
            sidecar.normalized_send_confirmation_text(messages[0]["content"]),
            sidecar.normalized_send_confirmation_text(expected),
        )
        self.assertIn(
            "send_confirmation_enhanced_roi_ocr",
            messages[0]["quality_flags"],
        )

    def test_post_send_enhanced_ocr_recovers_incident_fullwidth_question_mark(self):
        """Regression for the 2026-08-15 long green-bubble incident.

        OCR recognized all content but emitted an ASCII question mark for the
        visible full-width Chinese question mark. Raw equality kept the text
        bubble classified as an image for all six confirmation attempts.
        """

        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "incident-self-structural-candidate",
            "type": "image",
            "message_type": "image",
            "sender": "self",
            "sender_role": "self",
            "visual_side": "self",
            "is_self_image": True,
            "content": "[图片]",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def incident_enhanced_ocr(_image):
            return incident_post_send_enhanced_ocr_items()

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=INCIDENT_LONG_REPLY,
                ocr_runner=incident_enhanced_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(
            sidecar._normalized_send_ocr_correspondence_text(
                messages[0]["content"]
            ),
            sidecar._normalized_send_ocr_correspondence_text(INCIDENT_LONG_REPLY),
        )
        self.assertEqual(diagnostics["expected_coverage"], 1.0)
        self.assertEqual(diagnostics["observed_coverage"], 1.0)
        self.assertEqual(
            diagnostics["text_correspondence_reason"],
            "unicode_normalized_exact_program_text",
        )

    def test_post_send_enhanced_ocr_one_real_character_change_is_high_overlap_text(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "self-image-one-character-different",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        changed_items = incident_post_send_enhanced_ocr_items()
        changed_items[3]["text"] = "通勤还是跑短途，更"

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=INCIDENT_LONG_REPLY,
                ocr_runner=lambda _image: changed_items,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertTrue(diagnostics["recovered"])
        self.assertEqual(
            diagnostics["text_correspondence_reason"],
            "high_overlap_program_text",
        )
        self.assertGreater(diagnostics["expected_coverage"], 0.95)
        self.assertEqual(messages[0]["type"], "text")

    def test_post_send_enhanced_ocr_low_overlap_does_not_confirm_ai_reply(self):
        correspondence = sidecar._send_ocr_text_correspondence(
            INCIDENT_LONG_REPLY,
            "10万左右二手车推荐，点击图片查看车型和价格",
        )

        self.assertFalse(correspondence["accepted"])
        self.assertEqual(correspondence["reason"], "ocr_text_low_overlap")
        self.assertLess(correspondence["expected_coverage"], 0.80)

    def test_send_ocr_format_canonicalization_matrix(self):
        equivalent_pairs = [
            ("ＡＩ回复１２３！？", "ai回复123!?"),
            ("好的。", "好的."),
            ("“可以”", '"可以"'),
            ("‘可以’", "'可以'"),
            ("稍等……", "稍等..."),
            ("A—B–C", "a-b-c"),
            ("【推荐】", "[推荐]"),
            ("「推荐」", '"推荐"'),
            ("SUV\u00a0推荐\u200b", "suv推荐"),
            ("已完成👍️", "已完成👍"),
        ]

        for expected, observed in equivalent_pairs:
            with self.subTest(expected=expected, observed=observed):
                correspondence = sidecar._send_ocr_text_correspondence(
                    expected,
                    observed,
                )
                self.assertTrue(correspondence["accepted"])
                self.assertTrue(correspondence["exact"])

    def test_send_ocr_high_overlap_threshold_boundaries(self):
        expected = "abcdefghijklmnopqrst"

        accepted = sidecar._send_ocr_text_correspondence(
            expected,
            "abcdefghijklmnop",
        )
        rejected = sidecar._send_ocr_text_correspondence(
            expected,
            "abcdefghijklmno",
        )
        reordered = sidecar._send_ocr_text_correspondence(
            expected,
            "ponmlkjihgfedcba",
        )

        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["expected_coverage"], 0.8)
        self.assertEqual(accepted["reason"], "high_overlap_program_text")
        self.assertFalse(rejected["accepted"])
        self.assertLess(rejected["expected_coverage"], 0.8)
        self.assertFalse(reordered["accepted"])

    def test_send_context_sequence_ignores_ocr_format_only_differences(self):
        expected = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "请看【车型Ａ】……",
                }
            ]
        )
        current = sidecar.build_send_context_guard(
            [
                {
                    "row_kind": "text_bubble",
                    "sender_role": "customer",
                    "content_clean": "请看[车型a]...",
                }
            ]
        )

        result = sidecar.validate_send_context_guard(expected, current)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "message_sequence_unchanged")

    def test_format_drift_on_old_row_cannot_become_new_send_fact(self):
        baseline = [
            {
                "sequence_index": 0,
                "observation_id": "self-old",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "好的……",
            }
        ]
        current = [
            {
                "sequence_index": 0,
                "observation_id": "self-old-new-ocr-id",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "好的...",
            }
        ]

        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                baseline,
                current,
                "好的……",
            )
        )

    def test_post_send_enhanced_ocr_nearby_text_is_text_but_not_ai_reply(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "real-self-image-with-marketing-copy",
            "type": "image",
            "message_type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }

        def image_text_ocr(_image):
            return [
                {
                    "text": "10万左右二手车推荐，点击图片查看车型和价格",
                    "left": 20,
                    "top": 20,
                    "right": 620,
                    "bottom": 52,
                    "center_x": 320,
                    "center_y": 36,
                    "confidence": 0.99,
                }
            ]

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJMU5YT9",
                expected_text=(
                    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
                    "你主要家用、通勤还是跑长途？"
                ),
                ocr_runner=image_text_ocr,
            )
        )

        self.assertTrue(diagnostics["attempted"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertFalse(diagnostics["recovered"])
        self.assertFalse(diagnostics["ai_reply_correspondence_confirmed"])
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(
            sidecar.normalized_send_confirmation_text(messages[0]["content"]),
            "10万左右二手车推荐，点击图片查看车型和价格",
        )
        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                [],
                [
                    {
                        "sequence_index": 0,
                        "observation_id": "real-self-image-with-marketing-copy",
                        "row_kind": "text_bubble",
                        "sender_role": "self",
                        "content_normalized": sidecar.normalized_send_confirmation_text(
                            messages[0]["content"]
                        ),
                    }
                ],
                (
                    "可以，10万左右可以按你的需求帮你筛选合适的二手车。"
                    "你主要家用、通勤还是跑长途？"
                ),
            )
        )

    def test_incident_long_text_recovery_completes_sent_confirmation_chain(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        structural_image = {
            "id": "incident-self-structural-candidate",
            "message_id": "incident-self-structural-candidate",
            "type": "image",
            "message_type": "image",
            "visual_side": "self",
            "sender_role": "self",
            "bubble_rect": [489, 532, 878, 653],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        customer_message = {
            "id": "customer-question",
            "message_id": "customer-question",
            "observation_id": "customer-question",
            "type": "text",
            "message_type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "你好我想问10万左右的二手车有推荐的么",
            "bubble_rect": [487, 431, 803, 451],
        }
        enhanced_items = incident_post_send_enhanced_ocr_items()
        enhanced_items[0]["text"] = "可以，10万左右可以按需求帮你筛选合适"
        baseline_sequence = [
            {
                "sequence_index": 0,
                "observation_id": "customer-question",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "content_normalized": "你好我想问10万左右的二手车有推荐的么",
            }
        ]
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_active_send_target", return_value=validation),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[customer_message, structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "send_context_message_region_fingerprint",
                return_value={"sha256": "post-send-frame", "bounds": [390, 100, 980, 690]},
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJMU5YT9",
                text=INCIDENT_LONG_REPLY,
                exact=False,
                artifact_dir=None,
                label="incident_send_result_confirm",
                screenshot=frame,
                screenshot_path="incident-send-result.png",
                ocr_items=[],
                recover_expected_self_text=True,
            )

        with patch.object(
            sidecar,
            "capture_send_fact_snapshot",
            side_effect=AssertionError("initial snapshot should settle the send"),
        ):
            result = sidecar.confirm_reply_sent(
                1,
                target="CJMU5YT9",
                text=INCIDENT_LONG_REPLY,
                exact=False,
                baseline_match_count=0,
                baseline_message_sequence=baseline_sequence,
                artifact_dir=None,
                max_attempts=1,
                initial_snapshot=snapshot,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(
            result["confirmed_message"]["recovered_from_structural_observation_id"],
            "incident-self-structural-candidate",
        )

    def test_post_send_enhanced_ocr_retypes_readable_self_only(self):
        frame = Image.new("RGB", (980, 860), "white")
        candidates = [
            {
                "id": "self-real-image",
                "type": "image",
                "sender_role": "self",
                "bubble_rect": [520, 420, 850, 620],
                "avatar_alignment": {"role": "self"},
            },
            {
                "id": "customer-image",
                "type": "image",
                "sender_role": "customer",
                "bubble_rect": [400, 640, 710, 790],
                "avatar_alignment": {"role": "customer"},
            },
        ]
        calls = []

        def enhanced_ocr(_image):
            calls.append(True)
            return [
                {
                    "text": "图片里的其他文字",
                    "left": 10,
                    "top": 10,
                    "right": 180,
                    "bottom": 40,
                    "confidence": 0.99,
                }
            ]

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                candidates,
                target="CJTEST01",
                expected_text="这是程序本次真正发送的回复",
                ocr_runner=enhanced_ocr,
            )
        )

        self.assertEqual(calls, [True])
        self.assertFalse(diagnostics["recovered"])
        self.assertTrue(diagnostics["reclassified_as_text"])
        self.assertEqual([item["type"] for item in messages], ["text", "image"])

    def test_post_send_enhanced_ocr_without_readable_text_stays_image(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "self-image-no-readable-text",
            "type": "image",
            "sender_role": "self",
            "visual_side": "self",
            "bubble_rect": [520, 420, 850, 620],
            "avatar_alignment": {"role": "self"},
        }

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=lambda _image: [
                    {
                        "text": "……",
                        "left": 10,
                        "top": 10,
                        "right": 50,
                        "bottom": 40,
                        "confidence": 0.99,
                    }
                ],
            )
        )

        self.assertFalse(diagnostics["recovered"])
        self.assertFalse(diagnostics.get("reclassified_as_text", False))
        self.assertEqual(diagnostics["reason"], "enhanced_ocr_has_no_readable_text")
        self.assertEqual(messages[0]["type"], "image")

    def test_post_send_global_ocr_text_wins_without_redundant_local_ocr(self):
        frame = Image.new("RGB", (980, 860), "white")
        global_text = {
            "id": "global-self-text",
            "type": "text",
            "sender": "self",
            "sender_role": "self",
            "content": "AI回复",
            "bubble_rect": [700, 650, 860, 710],
        }

        def unexpected_local_ocr(_image):
            self.fail("全局 OCR 已确认全文时不应再运行局部 OCR")

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [global_text],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=unexpected_local_ocr,
            )
        )

        self.assertFalse(diagnostics["attempted"])
        self.assertFalse(diagnostics["recovered"])
        self.assertEqual(diagnostics["reason"], "already_observed_as_self_text")
        self.assertEqual(messages, [global_text])

    def test_post_send_local_ocr_cannot_replace_self_candidate_without_avatar_confirmation(self):
        frame = Image.new("RGB", (980, 860), "white")
        structural_image = {
            "id": "visual-self-without-avatar",
            "type": "image",
            "visual_side": "self",
            "sender_role": "unknown",
            "bubble_rect": [600, 520, 870, 610],
            "avatar_alignment": {"role": "unknown"},
        }

        def unexpected_local_ocr(_image):
            self.fail("同排头像未确认 self 时不应运行类型恢复")

        messages, diagnostics = (
            sidecar.recover_expected_self_text_from_structural_candidates(
                frame,
                [structural_image],
                target="CJTEST01",
                expected_text="AI回复",
                ocr_runner=unexpected_local_ocr,
            )
        )

        self.assertFalse(diagnostics["attempted"])
        self.assertFalse(diagnostics["recovered"])
        self.assertEqual(messages[0]["type"], "image")

    def test_post_send_snapshot_wires_structural_candidate_recovery_into_sequence(self):
        frame = Image.new("RGB", (980, 860), "white")
        geometry = {
            "left": 0,
            "top": 0,
            "right": 980,
            "bottom": 860,
            "width": 980,
            "height": 860,
        }
        validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        structural_image = {
            "id": "visual-self-candidate",
            "message_id": "visual-self-candidate",
            "type": "image",
            "message_type": "image",
            "visual_side": "self",
            "sender_role": "self",
            "bubble_rect": [489, 505, 878, 613],
            "avatar_alignment": {"role": "self", "confirmed": True},
        }
        enhanced_items = [
            {
                "text": "AI回复",
                "left": 520,
                "top": 530,
                "right": 650,
                "bottom": 560,
                "center_x": 585,
                "center_y": 545,
                "confidence": 0.99,
            }
        ]
        with (
            patch.object(sidecar, "get_window_geometry", return_value=geometry),
            patch.object(sidecar, "validate_active_send_target", return_value=validation),
            patch.object(sidecar, "active_send_guard_is_strong", return_value=True),
            patch.object(
                sidecar,
                "parse_current_chat_frame_messages",
                return_value=[structural_image],
            ),
            patch.object(
                sidecar,
                "enhanced_ocr_items_for_structural_chat_candidate",
                return_value=enhanced_items,
            ),
            patch.object(
                sidecar,
                "send_context_message_region_fingerprint",
                return_value={"sha256": "frame-sha", "bounds": [390, 100, 980, 720]},
            ),
            patch.object(
                sidecar,
                "input_text_region_state",
                return_value={"has_visible_text": False},
            ),
        ):
            snapshot = sidecar.build_send_fact_snapshot_from_frame(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                artifact_dir=None,
                label="send_result_confirm_test",
                screenshot=frame,
                screenshot_path="send-result.png",
                ocr_items=[],
                recover_expected_self_text=True,
            )

        self.assertTrue(snapshot["ok"])
        self.assertTrue(snapshot["enhanced_text_recovery"]["recovered"])
        self.assertEqual(snapshot["matching_self_message_count"], 1)
        self.assertEqual(snapshot["message_sequence"][0]["row_kind"], "text_bubble")
        self.assertEqual(snapshot["message_sequence"][0]["content_normalized"], "ai回复")

    def test_sent_confirmation_requests_enhanced_recovery_only_for_post_send_snapshot(self):
        snapshot = {
            "ok": True,
            "matching_self_message_count": 1,
            "input_region": {"has_visible_text": False},
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
        with patch.object(
            sidecar,
            "capture_send_fact_snapshot",
            return_value=snapshot,
        ) as capture:
            result = sidecar.confirm_reply_sent(
                1,
                target="CJTEST01",
                text="AI回复",
                exact=False,
                baseline_match_count=0,
                baseline_message_sequence=[],
                artifact_dir=None,
                max_attempts=1,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(capture.call_args.kwargs["recover_expected_self_text"])

    def test_enhanced_recovery_cannot_turn_an_old_image_candidate_into_new_send_fact(self):
        baseline = [
            {
                "sequence_index": 0,
                "observation_id": "visual-self-old",
                "row_kind": "image_bubble",
                "sender_role": "self",
                "content_normalized": "[图片]",
            }
        ]
        current = [
            {
                "sequence_index": 0,
                "observation_id": "enhanced-text-old",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_normalized": "AI回复",
                "recovered_from_structural_observation_id": "visual-self-old",
            }
        ]

        self.assertIsNone(
            sidecar.find_new_matching_self_message(
                baseline,
                current,
                "AI回复",
            )
        )

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
            "validation": strict_guard,
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
