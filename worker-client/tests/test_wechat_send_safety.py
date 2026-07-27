from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch


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

    def test_send_journal_is_persisted_before_physical_click(self):
        events: list[str] = []

        with patch.object(
            sidecar,
            "human_client_click",
            side_effect=lambda *_args: events.append("click"),
        ):
            result = sidecar.safe_send_trigger(
                100,
                trigger_mode="click_only",
                send_point=(10, 20),
                focus_guard_func=lambda: {"ok": True},
                before_physical_trigger=lambda: events.append(
                    "trigger_attempted"
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["trigger_attempted", "click"])

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

    def test_uia_send_clears_exact_program_draft_when_paste_confirmation_fails(self):
        class FakeValuePattern:
            def __init__(self):
                self.value = ""

            @property
            def Value(self):
                return self.value

        class FakeEdit:
            BoundingRectangle = SimpleNamespace(
                left=500,
                top=650,
                right=850,
                bottom=760,
            )

            def __init__(self, pattern):
                self.pattern = pattern

            def GetPattern(self, _pattern_id):
                return self.pattern

        pattern = FakeValuePattern()
        edit = FakeEdit(pattern)
        send_button = SimpleNamespace(
            BoundingRectangle=SimpleNamespace(
                left=850,
                top=720,
                right=930,
                bottom=770,
            )
        )
        fake_auto = SimpleNamespace(
            PatternId=SimpleNamespace(ValuePattern=1),
            ControlFromHandle=lambda _hwnd: object(),
        )

        def failed_after_typing(*_args, **_kwargs):
            pattern.value = "AI回复"
            return {
                "ok": False,
                "reason": "input_token_not_detected_after_paste",
                "input_result": {
                    "ok": True,
                    "typed_chars": 4,
                    "method": "clipboard_chunks",
                },
            }

        def clear_value(_key):
            pattern.value = ""

        with (
            patch.dict(sys.modules, {"uiautomation": fake_auto}),
            patch.object(sidecar, "collect_uia_controls", return_value=[]),
            patch.object(sidecar, "select_uia_edit_control", return_value=edit),
            patch.object(sidecar, "select_uia_send_button", return_value=send_button),
            patch.object(
                sidecar,
                "uia_rect_to_dict",
                side_effect=[
                    {"left": 500, "top": 650, "right": 850, "bottom": 760},
                    {"left": 850, "top": 720, "right": 930, "bottom": 770},
                ],
            ),
            patch.object(sidecar, "screen_point_to_client", side_effect=[(675, 705), (890, 745)]),
            patch.object(sidecar, "paste_text_with_confirmation", side_effect=failed_after_typing),
            patch.object(sidecar, "describe_uia_control", return_value={}),
            patch.object(sidecar, "human_client_click"),
            patch.object(sidecar, "hotkey"),
            patch.object(sidecar, "key_press", side_effect=clear_value),
            patch.object(sidecar, "humanized_action_sleep"),
        ):
            result = sidecar.send_with_uia_controls(
                1,
                "AI回复",
                geometry={"width": 960, "height": 820},
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["draft_clear"]["ok"])
        self.assertEqual(
            result["draft_clear"]["reason"],
            "confirmed_program_draft_cleared",
        )
        self.assertEqual(pattern.value, "")

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
                "send_with_uia_controls",
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

    def test_formal_send_payload_has_one_uia_observed_physical_path(self):
        source = inspect.getsource(sidecar.send_payload)

        self.assertIn("send_with_uia_controls(", source)
        self.assertIn("validate_send_context_guard(", source)
        self.assertIn("pre_click_context_check", source)
        self.assertNotIn("send_with_guarded_clicks(", source)
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
