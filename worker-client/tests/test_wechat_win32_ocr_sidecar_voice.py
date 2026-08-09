from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw


os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-voice-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")


REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_PATH = REPO_ROOT / "worker-client" / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
OMNIAUTO_ROOT = REPO_ROOT / "worker-client" / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

spec = importlib.util.spec_from_file_location("wechat_win32_ocr_sidecar", SIDECAR_PATH)
sidecar = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(sidecar)

from apps.wechat_ai_customer_service.adapters import wechat_connector
from chejin_worker_client.task_runner import _untranscribed_voice_observations


def ocr_item(text: str, left: int, top: int, right: int, bottom: int) -> dict:
    return {
        "text": text,
        "left": float(left),
        "top": float(top),
        "right": float(right),
        "bottom": float(bottom),
        "center_x": (left + right) / 2.0,
        "center_y": (top + bottom) / 2.0,
        "confidence": 0.92,
    }


def unified_voice_observation(anchor: dict | None, visible_button: dict | None = None) -> dict | None:
    if anchor is None:
        return None
    return {
        "schema_version": 3,
        "row_kind": "voice_bubble",
        "voice_state": "untranscribed",
        "sender_role": str((anchor.get("item") or {}).get("sender_role") or "customer"),
        "action_target": anchor,
        "visible_button_target": visible_button,
        "evidence_sources": ["test_fixture"],
        "source_message": {},
    }


class WechatWin32OcrVoiceSelectionTest(unittest.TestCase):
    @staticmethod
    def draw_avatar(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=5, fill=(63, 92, 138))
        draw.rectangle((left + 7, top + 6, right - 5, bottom - 18), fill=(218, 173, 91))
        draw.ellipse((left + 12, top + 12, right - 12, bottom - 6), fill=(90, 178, 135))

    def test_voice_transcript_role_always_inherits_bound_parent_voice(self) -> None:
        observations = sidecar.build_message_observations_v3(
            [
                {
                    "id": "voice-transcript-5s",
                    "type": "voice",
                    "sender_role": "customer",
                    "content": "可以呀，没问题。",
                    "voice_duration": 5,
                    "voice_anchor_stable_key": "voice-stable:customer-5s",
                    # This belongs to the original voice row. It must not make
                    # the transcript itself claim a same-row avatar.
                    "avatar_alignment": {"role": "customer"},
                    "sender_role_evidence": ["avatar_row_structure_confirmed"],
                }
            ]
        )

        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation["row_kind"], "voice_transcript")
        self.assertEqual(observation["sender_role"], "customer")
        self.assertEqual(observation["sender_role_source"], "parent_voice")
        self.assertEqual(observation["parent_voice_anchor_key"], "voice-stable:customer-5s")

    def test_voice_transcript_without_parent_anchor_is_not_trusted(self) -> None:
        observation = sidecar.build_message_observations_v3(
            [
                {
                    "id": "unbound-transcript",
                    "type": "voice",
                    "sender_role": "self",
                    "content": "没有父语音",
                    "avatar_alignment": {"role": "self"},
                }
            ]
        )[0]

        self.assertEqual(observation["row_kind"], "voice_transcript")
        self.assertEqual(observation["sender_role_source"], "unknown")
        self.assertIsNone(observation["parent_voice_anchor_key"])

    def test_final_message_parse_attaches_parent_anchor_to_every_visible_transcript(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        items = [
            ocr_item('23"', 486, 172, 545, 200),
            ocr_item("然后，你看那个数字人直播这块儿，如果真要聊，有", 485, 225, 889, 248),
            ocr_item("没有什么案例能展示的？", 486, 251, 690, 271),
            ocr_item('5"', 487, 458, 534, 484),
            ocr_item("可以呀，没问题。回头我把我那个中转站怎么操作告", 486, 512, 888, 532),
            ocr_item("诉你，可以批量生成的，很简单。", 484, 534, 740, 556),
        ]

        def avatar_role(_image, bounds, _image_size):
            top = int(bounds[1])
            if top in {172, 458}:
                return {"role": "customer", "side": "left", "confidence": 0.99}
            return {"role": "", "side": "", "confidence": 0.0}

        with patch.object(sidecar, "message_row_avatar_role_details", side_effect=avatar_role):
            messages = sidecar.parse_messages_from_ocr(
                items,
                image.size,
                target="CJONE001许聪",
                screenshot=image,
            )

        self.assertEqual([message["voice_duration"] for message in messages], [23, 5])
        parent_keys = [str(message.get("parent_voice_anchor_key") or "") for message in messages]
        self.assertTrue(all(key.startswith("voice-structural:") for key in parent_keys))
        self.assertEqual(len(set(parent_keys)), 2)
        observations = sidecar.build_message_observations_v3(messages)
        self.assertEqual([item["sender_role_source"] for item in observations], ["parent_voice", "parent_voice"])
        self.assertTrue(all(not item.get("contract_errors") for item in observations))

    def test_equal_duration_transcripts_use_relative_order_not_absolute_y(self) -> None:
        first_frame = [
            {"type": "voice", "sender_role": "customer", "voice_duration": 5, "bubble_rect": {"top": 180, "bottom": 240}, "quality_flags": []},
            {"type": "voice", "sender_role": "customer", "voice_duration": 5, "bubble_rect": {"top": 420, "bottom": 480}, "quality_flags": []},
        ]
        shifted_frame = [
            {"type": "voice", "sender_role": "customer", "voice_duration": 5, "bubble_rect": {"top": 80, "bottom": 140}, "quality_flags": []},
            {"type": "voice", "sender_role": "customer", "voice_duration": 5, "bubble_rect": {"top": 320, "bottom": 380}, "quality_flags": []},
        ]

        sidecar.attach_structural_voice_anchor_keys(first_frame)
        sidecar.attach_structural_voice_anchor_keys(shifted_frame)

        self.assertEqual(
            [item["parent_voice_anchor_key"] for item in first_frame],
            [item["parent_voice_anchor_key"] for item in shifted_frame],
        )
        self.assertNotEqual(first_frame[0]["parent_voice_anchor_key"], first_frame[1]["parent_voice_anchor_key"])

    def test_messages_frame_reuses_screenshot_and_falls_back_to_same_frame_title_roi(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        items = [ocr_item("发送", 870, 790, 930, 820)]
        title_items = [ocr_item("CJR8S5K3 虾丸子大人", 430, 48, 650, 78)]
        snapshot = {
            "screenshot": image,
            "screenshot_path": "messages.png",
            "ocr_items": items,
            "messages": [],
            "visible_untranscribed_voice": {"detected": False},
        }
        with patch.object(sidecar, "capture_message_history_snapshots", return_value=[snapshot]), patch.object(
            sidecar,
            "get_window_geometry",
            return_value={"width": 965, "height": 852, "left": 0, "top": 0, "right": 965, "bottom": 852},
        ), patch.object(
            sidecar,
            "run_ocr_on_screen_region",
            return_value=title_items,
        ) as title_roi_ocr, patch.object(sidecar, "capture_wechat") as capture:
            payload = sidecar.messages_payload(
                101,
                {},
                target="CJR8S5K3 虾丸子大人",
                history_load_times=0,
                confirm_target="CJR8S5K3",
            )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["target_confirmation"]["ok"])
        timing = payload["target_confirmation"]["timing"]
        self.assertTrue(timing["validate_active_send_target_frame_reused"])
        self.assertTrue(timing["validate_active_send_target_supplied_frame_title_roi_match"])
        title_roi_ocr.assert_called_once()
        self.assertIs(title_roi_ocr.call_args.args[0], image)
        capture.assert_not_called()

    def test_successful_messages_frame_writes_review_artifacts(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        items = [
            ocr_item("CJR8S5K3 虾丸子大人", 430, 48, 650, 78),
            ocr_item("测试消息", 470, 300, 570, 330),
        ]
        snapshot = {
            "screenshot": image,
            "screenshot_path": "messages.png",
            "ocr_items": items,
            "messages": [],
            "visible_untranscribed_voice": {"detected": False},
        }

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            sidecar,
            "capture_message_history_snapshots",
            return_value=[snapshot],
        ), patch.object(
            sidecar,
            "get_window_geometry",
            return_value={
                "width": 965,
                "height": 852,
                "left": 0,
                "top": 0,
                "right": 965,
                "bottom": 852,
            },
        ):
            payload = sidecar.messages_payload(
                101,
                {},
                target="CJR8S5K3 虾丸子大人",
                history_load_times=0,
                confirm_target="CJR8S5K3",
                artifact_dir=tmp,
            )

            self.assertTrue(payload["ok"])
            self.assertNotIn(
                "review_error",
                payload,
                payload.get("review_error"),
            )
            self.assertTrue(Path(payload["review_path"]).is_file())
            self.assertTrue(Path(payload["evidence_path"]).is_file())
            self.assertTrue(
                (Path(tmp) / "wechat_messages_frame_review.json").is_file()
            )


    def test_reused_frame_skips_title_roi_when_full_ocr_already_matches(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        full_items = [
            ocr_item("CJR8S5K3 虾丸子大人", 430, 48, 650, 78),
            ocr_item("发送", 870, 790, 930, 820),
        ]

        with patch.object(
            sidecar,
            "get_window_geometry",
            return_value={"width": 965, "height": 852, "left": 0, "top": 0, "right": 965, "bottom": 852},
        ), patch.object(sidecar, "run_ocr_on_screen_region") as title_roi_ocr, patch.object(
            sidecar,
            "capture_wechat",
        ) as capture:
            guard = sidecar.validate_active_send_target(
                101,
                "CJR8S5K3",
                exact=False,
                screenshot=image,
                ocr_items=full_items,
                screenshot_path="messages.png",
            )

        self.assertTrue(guard["ok"])
        self.assertTrue(guard["timing"]["validate_active_send_target_frame_reused"])
        self.assertNotIn("validate_active_send_target_supplied_frame_title_roi_match", guard["timing"])
        title_roi_ocr.assert_not_called()
        capture.assert_not_called()

    def test_reused_frame_title_roi_still_blocks_wrong_target(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        full_items = [ocr_item("发送", 870, 790, 930, 820)]
        wrong_title_items = [ocr_item("聂安的家", 430, 48, 560, 78)]

        with patch.object(
            sidecar,
            "get_window_geometry",
            return_value={"width": 965, "height": 852, "left": 0, "top": 0, "right": 965, "bottom": 852},
        ), patch.object(
            sidecar,
            "run_ocr_on_screen_region",
            return_value=wrong_title_items,
        ) as title_roi_ocr, patch.object(sidecar, "capture_wechat") as capture:
            guard = sidecar.validate_active_send_target(
                101,
                "CJR8S5K3",
                exact=False,
                screenshot=image,
                ocr_items=full_items,
                screenshot_path="messages.png",
            )

        self.assertFalse(guard["ok"])
        self.assertEqual(guard["reason"], "target_title_not_confirmed")
        self.assertFalse(guard["timing"]["validate_active_send_target_supplied_frame_title_roi_match"])
        title_roi_ocr.assert_called_once()
        capture.assert_not_called()

    def test_voice_before_frame_blocks_click_when_target_is_wrong(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        guard = {"ok": False, "online": True, "reason": "target_title_not_confirmed"}

        with patch.object(sidecar, "capture_wechat", return_value=(image, "voice_before.png")) as capture, patch.object(
            sidecar,
            "run_ocr",
            return_value=[],
        ), patch.object(
            sidecar,
            "get_window_geometry",
            return_value={"width": 965, "height": 852, "left": 0, "top": 0, "right": 965, "bottom": 852},
        ), patch.object(sidecar, "validate_active_send_target", return_value=guard) as validate, patch.object(
            sidecar,
            "open_voice_transcribe_context_menu",
        ) as open_menu:
            payload = sidecar.voice_transcribe_payload(
                101,
                {},
                target="CJR8S5K3 虾丸子大人",
                confirm_target="CJR8S5K3",
                max_duration_seconds=60,
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE")
        self.assertEqual(capture.call_count, 1)
        self.assertIs(validate.call_args.kwargs["screenshot"], image)
        open_menu.assert_not_called()

    def test_adjacent_same_side_bubbles_with_separate_avatar_rows_stay_separate(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (398, 198, 444, 240))
        self.draw_avatar(draw, (398, 244, 444, 286))
        items = [
            ocr_item("第一条独立消息", 470, 207, 650, 229),
            ocr_item("第二条独立消息", 470, 251, 650, 273),
        ]

        messages = sidecar.parse_messages_from_ocr(items, image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual([item["content"] for item in messages], ["第一条独立消息", "第二条独立消息"])

    def test_history_merge_keeps_repeated_long_messages_as_distinct_occurrences(self) -> None:
        repeated = "这是一条会被真实发送两次的较长消息"
        merged = sidecar.merge_message_history_snapshots(
            [
                {
                    "messages": [
                        {"id": "first", "sender": "customer", "type": "text", "content": repeated},
                        {"id": "second", "sender": "customer", "type": "text", "content": repeated},
                    ]
                }
            ]
        )

        self.assertEqual([item["id"] for item in merged], ["first", "second"])

    def test_nearby_equal_duration_voice_anchors_do_not_share_exclusion_keys(self) -> None:
        def anchor(center_y: float) -> dict:
            return {
                "source": "voice_duration_context",
                "item": {
                    "left": 470,
                    "top": center_y - 15,
                    "right": 570,
                    "bottom": center_y + 15,
                    "center_x": 520,
                    "center_y": center_y,
                    "text": '4"',
                    "voice_duration_text": '4"',
                    "sender_role": "customer",
                },
                "click_bounds": [470, center_y - 15, 570, center_y + 15],
            }

        first = sidecar.voice_context_anchor_exclusion_keys(anchor(300), (965, 852))
        second = sidecar.voice_context_anchor_exclusion_keys(anchor(390), (965, 852))

        self.assertFalse(first & second)

    def test_equal_transcripts_from_distinct_voice_anchors_are_not_deduped(self) -> None:
        first = {
            "sender": "customer",
            "type": "voice",
            "content": "好的",
            "voice_anchor_stable_key": "customer:4s:y10",
        }
        second = {
            "sender": "customer",
            "type": "voice",
            "content": "好的",
            "voice_anchor_stable_key": "customer:4s:y14",
        }

        self.assertNotEqual(
            sidecar.sidecar_message_content_key(first),
            sidecar.sidecar_message_content_key(second),
        )

    def test_new_message_comparison_counts_repeated_occurrences(self) -> None:
        message = {"sender": "customer", "type": "voice", "content": "好的"}

        new_items = sidecar.sidecar_new_message_occurrences(
            [dict(message), dict(message)],
            [dict(message)],
        )

        self.assertEqual(len(new_items), 1)

    def test_left_avatar_row_overrides_wide_text_geometry_as_customer(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (398, 198, 444, 244))
        item = ocr_item("客户发来的一条很长消息", 470, 210, 820, 238)

        messages = sidecar.parse_messages_from_ocr([item], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_role"], "customer")
        self.assertEqual(messages[0]["sender_role_algorithm"], "wechat_avatar_row_structure_v2")
        self.assertEqual(messages[0]["avatar_alignment"]["role"], "customer")

    def test_right_avatar_row_overrides_left_leaning_text_geometry_as_self(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (900, 198, 946, 244))
        item = ocr_item("我们自己发的一条消息", 500, 210, 650, 238)

        messages = sidecar.parse_messages_from_ocr([item], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(messages[0]["sender_role_algorithm"], "wechat_avatar_row_structure_v2")
        self.assertEqual(messages[0]["avatar_alignment"]["role"], "self")

    def test_voice_duration_inherits_same_row_right_avatar_role(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (900, 298, 946, 344))
        duration = ocr_item('2"', 790, 310, 842, 338)

        messages = sidecar.parse_messages_from_ocr([duration], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "voice")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(messages[0]["avatar_alignment"]["role"], "self")

    def test_image_internal_duration_text_cannot_trigger_voice_flow(self) -> None:
        image = Image.new("RGB", (974, 853), (242, 242, 242))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (408, 330, 453, 375))
        for y in range(330, 550, 6):
            for x in range(470, 700, 6):
                tone = 35 if ((x + y) // 6) % 2 else 220
                draw.rectangle((x, y, x + 5, y + 5), fill=(tone, 150, 80))

        embedded_duration = ocr_item(":22", 500, 355, 516, 363)
        raw_messages = sidecar.parse_messages_from_ocr(
            [embedded_duration],
            image.size,
            target="CJR8S5K3",
            screenshot=image,
        )
        self.assertEqual(raw_messages[0]["type"], "voice")
        self.assertIn("untranscribed_voice_placeholder", raw_messages[0]["quality_flags"])

        messages = sidecar.parse_current_chat_frame_messages(
            [embedded_duration],
            image.size,
            target="CJR8S5K3",
            screenshot=image,
        )
        hint = sidecar.visible_untranscribed_voice_hint(
            image,
            [embedded_duration],
            image.size,
            parsed_messages=messages,
        )
        voice_observations = [
            observation
            for observation in sidecar.build_unified_voice_observations_v3(
                image,
                [embedded_duration],
                image.size,
                parsed_messages=messages,
            )
            if observation.get("message_type") == "voice"
        ]

        self.assertEqual([message["type"] for message in messages], ["image"])
        self.assertEqual(hint, {"detected": False})
        self.assertEqual(voice_observations, [])

    def test_real_voice_outside_image_remains_actionable(self) -> None:
        image = Image.new("RGB", (974, 853), (242, 242, 242))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (408, 330, 453, 375))
        for y in range(330, 550, 6):
            for x in range(470, 700, 6):
                tone = 35 if ((x + y) // 6) % 2 else 220
                draw.rectangle((x, y, x + 5, y + 5), fill=(tone, 150, 80))
        draw.rounded_rectangle((772, 610, 919, 654), radius=8, fill=(140, 226, 146))
        self.draw_avatar(draw, (900, 608, 946, 656))

        items = [
            ocr_item(":22", 500, 355, 516, 363),
            ocr_item('4"', 812, 620, 862, 645),
        ]
        messages = sidecar.parse_current_chat_frame_messages(
            items,
            image.size,
            target="CJR8S5K3",
            screenshot=image,
        )
        hint = sidecar.visible_untranscribed_voice_hint(
            image,
            items,
            image.size,
            parsed_messages=messages,
        )

        self.assertEqual([message["type"] for message in messages], ["image", "voice"])
        self.assertTrue(hint["detected"])
        self.assertEqual(hint["sender_role"], "self")
        self.assertGreater(float(hint["bubble_rect"][1]), 550.0)

    def test_v1685_right_voice_with_open_paren_ocr_noise_uses_unified_observation(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 860), fill=(238, 238, 238))
        self.draw_avatar(draw, (900, 608, 946, 656))
        duration = ocr_item('4" (c', 812, 620, 862, 645)

        messages = sidecar.parse_messages_from_ocr([duration], image.size, target="CJR8S5K3", screenshot=image)
        hint = sidecar.visible_untranscribed_voice_hint(
            image,
            [duration],
            image.size,
            parsed_messages=messages,
        )
        observations = sidecar.build_c2_observations_v3(messages, hint)
        payload = {"observation_schema_version": 3, "observations": observations}

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "voice")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertTrue(hint["detected"])
        self.assertGreaterEqual(len(_untranscribed_voice_observations(payload)), 1)
        self.assertEqual(observations[0]["row_kind"], "voice_bubble")
        self.assertEqual(observations[0]["voice_state"], "untranscribed")
        self.assertEqual(observations[0]["sender_role_source"], "same_row_avatar")

    def test_unified_voice_truth_does_not_reselect_transcribed_self_below_pending_customer(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        self.draw_avatar(draw, (398, 140, 444, 186))
        self.draw_avatar(draw, (916, 282, 962, 328))
        top_duration = ocr_item('8"', 489, 151, 535, 177)
        top_button = ocr_item("转文字", 670, 155, 719, 173)
        self_duration = ocr_item('6" (c', 812, 292, 861, 317)
        self_text = ocr_item("房间我已经退了，但是有点冷，你看一下什么时候过", 459, 343, 863, 366)
        self_tail = ocr_item("来。", 457, 365, 487, 390)
        top_pending = {
            "id": "customer-8-pending",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 8"',
            "voice_duration_text": '8"',
            "bubble_rect": [489, 151, 719, 177],
            "ocr_items": [top_duration, top_button],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        self_completed = {
            "id": "self-6-completed",
            "type": "voice",
            "sender_role": "self",
            "content": "房间我已经退了，但是有点冷，你看一下什么时候过\n来。",
            "content_raw_ocr": '6" (c\n房间我已经退了，但是有点冷，你看一下什么时候过\n来。',
            "voice_duration_text": '6" (c',
            "bubble_rect": [457, 292, 863, 390],
            "ocr_items": [self_duration, self_text, self_tail],
            "quality_flags": ["voice_duration_prefix_removed"],
        }
        items = [top_duration, top_button, self_duration, self_text, self_tail]

        observations = sidecar.build_unified_voice_observations_v3(
            image,
            items,
            image.size,
            parsed_messages=[top_pending, self_completed],
        )
        selected = sidecar.find_unified_untranscribed_voice_observation(
            image,
            items,
            image.size,
            parsed_messages=[top_pending, self_completed],
        )

        states = {item["source_message_id"]: item["voice_state"] for item in observations if item["source_message_id"]}
        self.assertEqual(states["self-6-completed"], "transcribed")
        self.assertEqual(states["customer-8-pending"], "untranscribed")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["source_message_id"], "customer-8-pending")
        self.assertEqual(selected["visible_button_target"]["item"]["text"], "转文字")

    def test_unified_voice_truth_merges_raw_ocr_and_visual_evidence_without_parser(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        self.draw_avatar(ImageDraw.Draw(image), (398, 290, 444, 336))
        duration = ocr_item('4"', 488, 300, 525, 326)
        visual_target = {
            "source": "visual_customer_voice_bubble_context_menu_anchor",
            "item": {
                "left": 462,
                "top": 287,
                "right": 590,
                "bottom": 340,
                "center_x": 526,
                "center_y": 313.5,
                "sender_role": "customer",
            },
            "click_bounds": [462, 287, 590, 340],
        }

        with (
            patch.object(sidecar, "find_visual_customer_voice_context_anchor_targets", return_value=[visual_target]),
            patch.object(sidecar, "find_visual_self_voice_context_anchor_targets", return_value=[]),
        ):
            observations = sidecar.build_unified_voice_observations_v3(
                image,
                [duration],
                image.size,
                parsed_messages=[],
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["voice_state"], "untranscribed")
        self.assertEqual(observations[0]["evidence_sources"], ["ocr_duration", "visual_customer_bubble"])

    def test_unified_voice_truth_keeps_all_visual_only_candidates_and_selects_bottom_first(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        self.draw_avatar(draw, (398, 182, 444, 228))
        self.draw_avatar(draw, (398, 422, 444, 468))

        def visual_target(top: int, bottom: int) -> dict:
            return {
                "source": "visual_customer_voice_bubble_context_menu_anchor",
                "item": {
                    "left": 462,
                    "top": top,
                    "right": 590,
                    "bottom": bottom,
                    "center_x": 526,
                    "center_y": (top + bottom) / 2,
                    "sender_role": "customer",
                },
                "click_bounds": [462, top, 590, bottom],
            }

        top_target = visual_target(180, 230)
        bottom_target = visual_target(420, 470)
        with (
            patch.object(sidecar, "find_visual_customer_voice_context_anchor_targets", return_value=[top_target, bottom_target]),
            patch.object(sidecar, "find_visual_self_voice_context_anchor_targets", return_value=[]),
        ):
            observations = sidecar.build_unified_voice_observations_v3(image, [], image.size, parsed_messages=[])
            selected = sidecar.find_unified_untranscribed_voice_observation(image, [], image.size, parsed_messages=[])

        self.assertEqual(len(observations), 2)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["bubble_rect"]["top"], 420.0)

    def test_multiline_self_voice_transcript_keeps_short_overlapping_tail(self) -> None:
        image = Image.new("RGB", (981, 860), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 860), fill=(238, 238, 238))
        self.draw_avatar(draw, (899, 280, 967, 328))
        items = [
            ocr_item('6" (c', 812, 292, 861, 317),
            ocr_item("房间我已经退了，但是有点冷，你看一下什么时候过", 459, 343, 863, 366),
            ocr_item("来。", 457, 365, 487, 390),
        ]

        messages = sidecar.parse_messages_from_ocr(items, image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "voice")
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(messages[0]["content"].replace("\n", ""), "房间我已经退了，但是有点冷，你看一下什么时候过来。")
        self.assertEqual([item["text"] for item in messages[0]["ocr_items"]], ['6" (c', "房间我已经退了，但是有点冷，你看一下什么时候过", "来。"])

    def test_customer_voice_transcript_inherits_parent_role_when_text_geometry_looks_self(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (398, 298, 444, 344))
        duration = ocr_item('6"', 470, 310, 530, 338)
        wide_transcript = ocr_item("好的，不着急，我身上还带了水果。", 470, 350, 900, 378)

        messages = sidecar.parse_messages_from_ocr(
            [duration, wide_transcript],
            image.size,
            target="CJR8S5K3",
            screenshot=image,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "voice")
        self.assertEqual(messages[0]["sender_role"], "customer")
        self.assertEqual(messages[0]["content"], "好的，不着急，我身上还带了水果。")
        self.assertIn("voice_duration_prefix_removed", messages[0]["quality_flags"])

    def test_text_row_without_avatar_or_parent_voice_is_rejected(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        transcript = ocr_item("语音转出的文字", 680, 410, 870, 438)

        messages = sidecar.parse_messages_from_ocr([transcript], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(messages, [])

    def test_phone_icon_is_not_a_customer_avatar(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((403, 100, 648, 141), radius=4, fill=(238, 238, 238))
        draw.arc((415, 108, 432, 127), 100, 260, fill=(0, 196, 112), width=4)
        banner = ocr_item("你正在其他设备进行切换", 415, 108, 637, 132)

        alignment = sidecar.message_row_avatar_role_details(
            image,
            [banner["left"], banner["top"], banner["right"], banner["bottom"]],
            image.size,
        )
        messages = sidecar.parse_messages_from_ocr([banner], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(alignment["role"], "")
        self.assertEqual(messages, [])

    def test_call_duration_bubble_is_typed_as_non_chat_call_event(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        self.draw_avatar(draw, (900, 610, 946, 656))
        draw.rounded_rectangle((695, 610, 878, 659), radius=6, fill=(149, 236, 151))
        event = ocr_item("通话时长 06:53 口", 708, 622, 859, 648)

        messages = sidecar.parse_messages_from_ocr([event], image.size, target="CJR8S5K3", screenshot=image)
        observations = sidecar.build_c2_observations_v3(messages)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "system")
        self.assertIn("non_chat_call_event", messages[0]["quality_flags"])
        self.assertEqual(observations[0]["row_kind"], "call_event")

    def test_top_edge_text_without_avatar_is_rejected_as_clipped_fragment(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        clipped_tail = ocr_item("的，也没有共享单车，好奇怪。", 485, 100, 720, 122)

        messages = sidecar.parse_messages_from_ocr([clipped_tail], image.size, target="CJR8S5K3", screenshot=image)

        self.assertEqual(messages, [])

    def test_voice_duration_text_like_handles_open_paren_noise(self) -> None:
        self.assertTrue(sidecar.voice_duration_text_like("9 ("))
        self.assertTrue(sidecar.voice_duration_text_like("9("))
        self.assertTrue(sidecar.voice_duration_text_like("9（"))

    def test_transcript_binding_requires_same_voice_neighborhood(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [472, 554, 590, 570],
            "item": {
                "text": '4"',
                "voice_duration_text": '4"',
                "sender_role": "customer",
                "parser_bubble_rect": [464, 549, 598, 575],
                "left": 494,
                "top": 553,
                "right": 528,
                "bottom": 575,
                "center_x": 511,
                "center_y": 564,
            },
        }
        nearby_transcript = {
            "type": "text",
            "sender": "self",
            "sender_role": "self",
            "content": "现在已经雨停了，外面又出太阳了。",
            "bubble_rect": [464, 556, 782, 588],
        }
        shifted_voice_after = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 4"',
            "voice_duration_text": '4"',
            "bubble_rect": [464, 505, 598, 531],
        }
        off_column_text = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "现在已经雨停了，外面又出太阳了。",
            "bubble_rect": [620, 556, 900, 588],
        }
        upper_duration_noise = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "9 (",
            "bubble_rect": [493, 104, 532, 129],
        }

        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(nearby_transcript, anchor, (965, 852))
        )
        self.assertTrue(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                nearby_transcript,
                anchor,
                (965, 852),
                after_messages=[shifted_voice_after, nearby_transcript],
            )
        )
        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                off_column_text,
                anchor,
                (965, 852),
                after_messages=[shifted_voice_after, off_column_text],
            )
        )
        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(upper_duration_noise, anchor, (965, 852))
        )

    def test_newly_visible_avatar_text_is_not_bound_to_old_voice_coordinates(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [496, 293, 528, 309],
            "item": {
                "text": ')3"',
                "voice_duration_text": '3"',
                "sender_role": "customer",
                "parser_bubble_rect": [488, 288, 536, 314],
            },
        }
        ordinary_text = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "海鲜",
            "bubble_rect": [483, 339, 527, 366],
            "avatar_alignment": {
                "role": "customer",
                "customer": {"present": True},
                "self": {"present": False},
            },
        }

        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                ordinary_text,
                anchor,
                (965, 852),
                after_messages=[ordinary_text],
            )
        )

    def test_avatar_text_is_rejected_even_when_matching_voice_remains_visible(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [496, 293, 528, 309],
            "item": {
                "text": '3"',
                "voice_duration_text": '3"',
                "sender_role": "customer",
                "parser_bubble_rect": [488, 288, 536, 314],
            },
        }
        shifted_voice_after = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 3"',
            "voice_duration_text": '3"',
            "bubble_rect": [488, 288, 536, 314],
        }
        ordinary_text = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "海鲜",
            "bubble_rect": [483, 339, 527, 366],
            "avatar_alignment": {
                "role": "customer",
                "customer": {"present": True},
                "self": {"present": False},
            },
        }

        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                ordinary_text,
                anchor,
                (965, 852),
                after_messages=[shifted_voice_after, ordinary_text],
            )
        )

    def test_voice_flow_refreshes_without_scrolling_or_binding_avatar_text(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "anchor_key": "voice-3",
            "anchor_stable_key": "voice-stable-3",
            "click_bounds": [496, 293, 528, 309],
            "item": {
                "text": '3"',
                "voice_duration_text": '3"',
                "sender_role": "customer",
                "parser_bubble_rect": [488, 288, 536, 314],
            },
        }
        before_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 3"',
            "voice_duration_text": '3"',
            "bubble_rect": [488, 288, 536, 314],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        ordinary_text = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "海鲜",
            "bubble_rect": [483, 339, 527, 366],
            "avatar_alignment": {
                "role": "customer",
                "customer": {"present": True},
                "self": {"present": False},
            },
        }
        recovered_voice = {
            **before_voice,
            "bubble_rect": [488, 220, 536, 246],
        }
        recovered_transcript = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "今天天气真不错",
            "bubble_rect": [488, 256, 680, 288],
            "avatar_alignment": {
                "role": "",
                "customer": {"present": False},
                "self": {"present": False},
            },
        }
        parsed_snapshots = iter(
            [
                [before_voice],
                [ordinary_text],
                [ordinary_text],
                [recovered_voice, recovered_transcript],
            ]
        )
        capture_paths = iter(["before.png", "after.png", "refresh.png", "recovered.png"])
        scroll_mock = Mock()

        with (
            patch.object(sidecar, "capture_wechat", side_effect=lambda *_args, **_kwargs: (image, next(capture_paths))),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(sidecar, "parse_messages_from_ocr", side_effect=lambda *_args, **_kwargs: next(parsed_snapshots)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(
                sidecar,
                "validate_active_send_target",
                side_effect=[
                    {"ok": True, "conversation_type": "private", "short_code_confirmed": True},
                    {"ok": False, "reason": "target_title_not_confirmed"},
                ],
            ) as validate_target,
            patch.object(sidecar, "c2_target_activation_confirmed", side_effect=lambda value: value.get("ok") is True),
            patch.object(sidecar, "find_unified_untranscribed_voice_observation", return_value=unified_voice_observation(anchor)),
            patch.object(sidecar, "has_remaining_voice_transcribe_candidate", return_value=False),
            patch.object(
                sidecar,
                "open_voice_transcribe_context_menu",
                return_value={"menu_state": "transcribe_available", "click_target": {"click_bounds": [525, 317, 649, 343]}},
            ),
            patch.object(
                sidecar,
                "click_voice_transcribe_context_menu_target",
                return_value={"ok": True, "planned_click_point": [587, 330], "click_jitter": {}},
            ),
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
            patch.object(sidecar, "scroll_chat_history", scroll_mock),
        ):
            result = sidecar.voice_transcribe_payload(
                1,
                {},
                target="CJR8S5K3",
                confirm_target="CJR8S5K3",
            )

        self.assertEqual(result["state"], "voice_transcribe_completed")
        self.assertEqual([item["content"] for item in result["transcribed_messages"]], ["今天天气真不错"])
        self.assertNotIn("海鲜", [item["content"] for item in result["transcribed_messages"]])
        self.assertEqual(result["attempts"][0]["reanchor_attempts"][-1]["transcribed_count"], 1)
        self.assertTrue(all(not item["scrolled_up"] for item in result["attempts"][0]["reanchor_attempts"]))
        self.assertEqual(result["timing"]["schema_version"], 1)
        self.assertGreaterEqual(result["timing"]["ocr_call_count"], 2)
        self.assertGreaterEqual(result["timing"]["capture_call_count"], 2)
        self.assertGreaterEqual(result["timing"]["wait_call_count"], 1)
        self.assertGreaterEqual(result["timing"]["total_duration_seconds"], 0)
        self.assertTrue(result["initial_target_confirmation"]["ok"])
        self.assertFalse(result["target_confirmation"]["ok"])
        self.assertFalse(result["final_frame_validation"]["target_confirmed"])
        self.assertFalse(result["final_frame_reusable"])
        self.assertEqual(validate_target.call_count, 2)
        self.assertEqual(validate_target.call_args_list[-1].kwargs["screenshot_path"], "recovered.png")
        scroll_mock.assert_not_called()

    def test_combined_voice_record_is_bound_to_clicked_row_with_duplicate_duration(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [790, 615, 860, 646],
            "item": {
                "text": '2"',
                "voice_duration_text": '2"',
                "sender_role": "self",
                "parser_bubble_rect": [772, 610, 878, 650],
            },
        }
        upper_same_duration = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": "上面那条语音的文字",
            "content_raw_ocr": '2"\n上面那条语音的文字',
            "content_clean": "上面那条语音的文字",
            "voice_duration_text": '2"',
            "bubble_rect": [700, 390, 878, 468],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "self", "self": {"present": True}},
        }
        clicked_voice = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": "啦啦啦。",
            "content_raw_ocr": '2"\n啦啦啦。',
            "content_clean": "啦啦啦。",
            "voice_duration_text": '2"',
            "bubble_rect": [760, 568, 878, 646],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "self", "self": {"present": True}},
        }
        after_messages = [upper_same_duration, clicked_voice]

        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                upper_same_duration,
                anchor,
                (965, 852),
                after_messages=after_messages,
            )
        )
        self.assertTrue(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                clicked_voice,
                anchor,
                (965, 852),
                after_messages=after_messages,
            )
        )

    def test_unique_same_duration_voice_far_from_clicked_row_is_rejected(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [790, 615, 860, 646],
            "item": {
                "text": '2"',
                "voice_duration_text": '2"',
                "sender_role": "self",
                "parser_bubble_rect": [772, 610, 878, 650],
            },
        }
        far_same_duration = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": "远处另一条语音的文字",
            "content_raw_ocr": '2"\n远处另一条语音的文字',
            "content_clean": "远处另一条语音的文字",
            "voice_duration_text": '2"',
            "bubble_rect": [760, 390, 878, 468],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "self", "self": {"present": True}},
        }

        evidence = sidecar.combined_voice_transcript_anchor_match_evidence(
            far_same_duration,
            anchor,
            (965, 852),
            after_messages=[far_same_duration],
        )

        self.assertFalse(evidence["accepted"])
        self.assertEqual(evidence["selected_candidate_count"], 0)
        self.assertEqual(evidence["reason"], "ambiguous_duration_match")

    def test_long_combined_voice_expansion_binds_when_original_row_is_covered(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [494, 357, 537, 375],
            "item": {
                "text": '23"',
                "voice_duration_text": '23"',
                "sender_role": "customer",
                "parser_bubble_rect": [486, 352, 545, 380],
            },
        }
        expanded_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": "然后，你看那个数字人直播这块儿，如果真要聊，有\n"
            "没有什么案例能展示的？就我们毕竟之前没做过。",
            "content_raw_ocr": '23"\n然后，你看那个数字人直播这块儿，如果真要聊，有\n'
            "没有什么案例能展示的？就我们毕竟之前没做过。",
            "content_clean": "然后，你看那个数字人直播这块儿，如果真要聊，有\n"
            "没有什么案例能展示的？就我们毕竟之前没做过。",
            "voice_duration_text": '23"',
            "bubble_rect": [484, 188, 891, 384],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }

        evidence = sidecar.combined_voice_transcript_anchor_match_evidence(
            dict(expanded_voice),
            anchor,
            (981, 860),
            after_messages=[expanded_voice],
        )

        self.assertTrue(evidence["accepted"])
        self.assertEqual(evidence["strategy"], "unique_duration_and_region")
        self.assertEqual(evidence["vertical_overlap"], 28.0)
        self.assertTrue(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                expanded_voice,
                anchor,
                (981, 860),
                after_messages=[expanded_voice],
            )
        )

    def test_duration_ocr_conflict_binds_only_unique_structural_voice(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [496, 620, 548, 646],
            "item": {
                "text": ')6"',
                "voice_duration_text": ')6"',
                "sender_role": "customer",
                "parser_bubble_rect": [488, 619, 568, 645],
            },
        }
        clicked_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": "现在到车上了，给张渊换了裤子，然后我们先去拿煎饼吧。",
            "content_raw_ocr": '9 (c\n现在到车上了，给张渊换了裤子，然后我们先去拿煎饼吧。',
            "content_clean": "现在到车上了，给张渊换了裤子，然后我们先去拿煎饼吧。",
            "voice_duration_text": "9 (c",
            "bubble_rect": [488, 570, 812, 645],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }

        evidence = sidecar.combined_voice_transcript_anchor_match_evidence(
            clicked_voice,
            anchor,
            (965, 852),
            after_messages=[clicked_voice],
        )

        self.assertTrue(evidence["accepted"])
        self.assertTrue(evidence["duration_conflict"])
        self.assertEqual(evidence["strategy"], "unique_structure_with_duration_conflict")
        self.assertEqual(evidence["structural_candidate_count"], 1)

    def test_duration_ocr_conflict_does_not_bind_ambiguous_structural_voices(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [496, 620, 548, 646],
            "item": {
                "text": ')6"',
                "voice_duration_text": ')6"',
                "sender_role": "customer",
                "parser_bubble_rect": [488, 619, 568, 645],
            },
        }
        clicked_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": "第一条候选文字",
            "content_raw_ocr": "9 (c\n第一条候选文字",
            "content_clean": "第一条候选文字",
            "voice_duration_text": "9 (c",
            "bubble_rect": [488, 570, 720, 645],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }
        competing_voice = {
            **clicked_voice,
            "content": "第二条候选文字",
            "content_raw_ocr": "8 (c\n第二条候选文字",
            "content_clean": "第二条候选文字",
            "voice_duration_text": "8 (c",
            "bubble_rect": [488, 535, 720, 610],
        }

        evidence = sidecar.combined_voice_transcript_anchor_match_evidence(
            clicked_voice,
            anchor,
            (965, 852),
            after_messages=[clicked_voice, competing_voice],
        )

        self.assertFalse(evidence["accepted"])
        self.assertEqual(evidence["reason"], "ambiguous_duration_conflict")
        self.assertEqual(evidence["structural_candidate_count"], 2)

    def test_failed_voice_anchor_is_skipped_and_next_voice_is_processed_without_scrolling(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))

        def make_anchor(name: str, top: int) -> dict:
            anchor = {
                "source": "parser_voice_message_context_menu_anchor",
                "click_bounds": [496, top + 5, 528, top + 25],
                "item": {
                    "text": '2"',
                    "voice_duration_text": '2"',
                    "sender_role": "customer",
                    "message_id": name,
                    "parser_bubble_rect": [488, top, 535, top + 26],
                    "center_x": 511.5,
                    "center_y": top + 13,
                },
            }
            return sidecar.mark_voice_context_anchor_keys(anchor, image.size)

        lower_anchor = make_anchor("lower", 620)
        upper_anchor = make_anchor("upper", 300)
        lower_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 2"',
            "content_raw_ocr": '2"',
            "voice_duration_text": '2"',
            "bubble_rect": [488, 620, 535, 646],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        upper_voice = {
            **lower_voice,
            "bubble_rect": [488, 300, 535, 326],
        }
        upper_transcribed = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": "上面这条已经转好",
            "content_clean": "上面这条已经转好",
            "content_raw_ocr": '2"\n上面这条已经转好',
            "voice_duration_text": '2"',
            "bubble_rect": [488, 300, 690, 374],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }
        lower_expanded_but_unbound = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "这条文字已经出现但没有可靠绑定",
            "bubble_rect": [488, 620, 760, 660],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }
        parsed_snapshots = iter(
            [
                [upper_voice, lower_voice],
                [upper_voice, lower_expanded_but_unbound],
                [upper_voice, lower_expanded_but_unbound],
                [upper_voice, lower_expanded_but_unbound],
                [upper_voice, lower_expanded_but_unbound],
                [upper_transcribed, lower_expanded_but_unbound],
            ]
        )
        capture_paths = iter(
            [
                "before.png",
                "after-1.png",
                "refresh-1.png",
                "refresh-2.png",
                "refresh-3.png",
                "after-2.png",
            ]
        )
        scroll_mock = Mock()

        def select_anchor(*_args, excluded_anchor_keys=None, **_kwargs):
            excluded = excluded_anchor_keys or set()
            if not sidecar.voice_context_anchor_is_excluded(lower_anchor, image.size, excluded):
                return lower_anchor
            if not sidecar.voice_context_anchor_is_excluded(upper_anchor, image.size, excluded):
                return upper_anchor
            return None

        with (
            patch.object(sidecar, "capture_wechat", side_effect=lambda *_args, **_kwargs: (image, next(capture_paths))),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(sidecar, "parse_messages_from_ocr", side_effect=lambda *_args, **_kwargs: next(parsed_snapshots)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(
                sidecar,
                "find_unified_untranscribed_voice_observation",
                side_effect=lambda *_args, **kwargs: unified_voice_observation(select_anchor(excluded_anchor_keys=kwargs.get("excluded_anchor_keys"))),
            ),
            patch.object(
                sidecar,
                "open_voice_transcribe_context_menu",
                return_value={"menu_state": "transcribe_available", "click_target": {"click_bounds": [525, 317, 649, 343]}},
            ),
            patch.object(
                sidecar,
                "click_voice_transcribe_context_menu_target",
                return_value={"ok": True, "planned_click_point": [587, 330], "click_jitter": {}},
            ) as click_mock,
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
            patch.object(sidecar, "scroll_chat_history", scroll_mock),
        ):
            result = sidecar.voice_transcribe_payload(1, {}, target="CJR8S5K3")

        self.assertEqual(result["state"], "voice_transcribe_partial")
        self.assertEqual(result["attempt_count"], 2)
        self.assertGreater(result["failed_voice_anchor_count"], 0)
        self.assertIn("voice_transcribe_anchor_failed", result["quality_flags"])
        self.assertFalse(result["attempts"][-1]["remaining_untranscribed_voice"])
        self.assertEqual([item["content"] for item in result["transcribed_messages"]], ["上面这条已经转好"])
        self.assertTrue(result["attempts"][0]["failed_anchor_keys"])
        self.assertEqual(result["attempts"][1]["context_anchor"]["item"]["message_id"], "upper")
        self.assertEqual(click_mock.call_count, 2)
        scroll_mock.assert_not_called()

    def test_each_voice_attempt_uses_fresh_baseline_and_does_not_rebind_prior_transcript(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))

        def make_anchor(name: str, role: str, duration: str, top: int, left: int, right: int) -> dict:
            anchor = {
                "source": "parser_voice_message_context_menu_anchor",
                "click_bounds": [left + 8, top + 5, right - 8, top + 21],
                "item": {
                    "text": duration,
                    "voice_duration_text": duration,
                    "sender_role": role,
                    "message_id": name,
                    "parser_bubble_rect": [left, top, right, top + 26],
                    "center_x": (left + right) / 2,
                    "center_y": top + 13,
                },
            }
            return sidecar.mark_voice_context_anchor_keys(anchor, image.size)

        customer_anchor = make_anchor("customer-5", "customer", '5"', 500, 488, 535)
        self_anchor = make_anchor("self-11", "self", '11"', 400, 803, 861)
        customer_voice = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 5"',
            "voice_duration_text": '5"',
            "bubble_rect": [488, 500, 535, 526],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        self_voice = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": '[语音] 11"',
            "voice_duration_text": '11"',
            "bubble_rect": [803, 400, 861, 426],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        customer_text = {
            "type": "text",
            "sender": "self",
            "sender_role": "self",
            "content": "客户五秒语音文字",
            "bubble_rect": [488, 536, 720, 570],
            "avatar_alignment": {"role": ""},
        }
        self_transcribed = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": "我们十一秒语音文字",
            "content_clean": "我们十一秒语音文字",
            "content_raw_ocr": '11"\n我们十一秒语音文字',
            "voice_duration_text": '11"',
            "bubble_rect": [620, 350, 861, 426],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "self", "self": {"present": True}},
        }
        parsed_snapshots = iter(
            [
                [self_voice, customer_voice],
                [self_voice, {**customer_voice, "bubble_rect": [488, 490, 535, 516]}, customer_text],
                [self_transcribed, {**customer_voice, "bubble_rect": [488, 490, 535, 516]}, customer_text],
            ]
        )
        capture_paths = iter(["before.png", "after-customer.png", "after-self.png"])

        def select_anchor(*_args, excluded_anchor_keys=None, **_kwargs):
            excluded = excluded_anchor_keys or set()
            if not sidecar.voice_context_anchor_is_excluded(customer_anchor, image.size, excluded):
                return customer_anchor
            if not sidecar.voice_context_anchor_is_excluded(self_anchor, image.size, excluded):
                return self_anchor
            return None

        with (
            patch.object(sidecar, "capture_wechat", side_effect=lambda *_args, **_kwargs: (image, next(capture_paths))),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(sidecar, "parse_messages_from_ocr", side_effect=lambda *_args, **_kwargs: next(parsed_snapshots)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(
                sidecar,
                "find_unified_untranscribed_voice_observation",
                side_effect=lambda *_args, **kwargs: unified_voice_observation(select_anchor(excluded_anchor_keys=kwargs.get("excluded_anchor_keys"))),
            ),
            patch.object(
                sidecar,
                "open_voice_transcribe_context_menu",
                return_value={"menu_state": "transcribe_available", "click_target": {"click_bounds": [525, 317, 649, 343]}},
            ),
            patch.object(
                sidecar,
                "click_voice_transcribe_context_menu_target",
                return_value={"ok": True, "planned_click_point": [587, 330], "click_jitter": {}},
            ),
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
        ):
            result = sidecar.voice_transcribe_payload(1, {}, target="CJR8S5K3")

        self.assertEqual(result["state"], "voice_transcribe_completed")
        self.assertEqual(
            [(item["content"], item["sender_role"]) for item in result["transcribed_messages"]],
            [("客户五秒语音文字", "customer"), ("我们十一秒语音文字", "self")],
        )
        self.assertEqual(result["attempts"][1]["transcribed_messages_count"], 1)

    def test_visible_button_waits_passively_without_second_physical_action(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        anchor = sidecar.mark_voice_context_anchor_keys(
            {
                "source": "parser_voice_message_context_menu_anchor",
                "click_bounds": [495, 225, 528, 243],
                "item": {
                    "text": '2"',
                    "voice_duration_text": '2"',
                    "sender_role": "customer",
                    "message_id": "customer-2",
                    "parser_bubble_rect": [487, 220, 536, 248],
                    "center_x": 511.5,
                    "center_y": 234,
                },
            },
            image.size,
        )
        direct_target = {
            "source": "ocr_transcribe_button",
            "click_bounds": [592, 211, 682, 257],
            "item": {"text": "转文字", "left": 610, "top": 223, "right": 664, "bottom": 245, "center_y": 234},
        }
        placeholder = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": '[语音] 2"',
            "voice_duration_text": '2"',
            "bubble_rect": [487, 220, 536, 248],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        completed = {
            "type": "voice",
            "sender": "customer",
            "sender_role": "customer",
            "content": "今天可以出门了",
            "content_clean": "今天可以出门了",
            "content_raw_ocr": '2"\n今天可以出门了',
            "voice_duration_text": '2"',
            "bubble_rect": [487, 220, 700, 294],
            "quality_flags": ["voice_duration_prefix_removed"],
            "avatar_alignment": {"role": "customer", "customer": {"present": True}},
        }
        parsed_snapshots = iter([[placeholder], [placeholder], [placeholder], [placeholder], [completed]])
        capture_paths = iter(["before.png", "after.png", "retry-1.png", "retry-2.png", "retry-3.png"])
        visible_clicks: list[tuple[int, int, list[int]]] = []

        def visible_click(_hwnd, x, y, *, bounds, **_kwargs):
            visible_clicks.append((x, y, bounds))
            return {"ok": True, "x": x, "y": y}

        with (
            patch.object(sidecar, "capture_wechat", side_effect=lambda *_args, **_kwargs: (image, next(capture_paths))),
            patch.object(sidecar, "run_ocr", return_value=[]),
            patch.object(sidecar, "parse_messages_from_ocr", side_effect=lambda *_args, **_kwargs: next(parsed_snapshots)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(
                sidecar,
                "find_unified_untranscribed_voice_observation",
                return_value=unified_voice_observation(anchor, direct_target),
            ),
            patch.object(sidecar, "has_remaining_voice_transcribe_candidate", return_value=False),
            patch.object(sidecar, "human_window_image_click_in_bounds", side_effect=visible_click),
            patch.object(
                sidecar,
                "open_voice_transcribe_context_menu",
                return_value={"menu_state": "transcribe_available", "click_target": {"click_bounds": [525, 317, 649, 343]}},
            ),
            patch.object(
                sidecar,
                "click_voice_transcribe_context_menu_target",
                return_value={"ok": True, "planned_click_point": [587, 330], "click_jitter": {}},
            ) as fallback_click,
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
        ):
            result = sidecar.voice_transcribe_payload(1, {}, target="CJR8S5K3")

        self.assertEqual(result["state"], "voice_transcribe_completed")
        self.assertEqual([item["content"] for item in result["transcribed_messages"]], ["今天可以出门了"])
        self.assertEqual(visible_clicks[0], (637, 234, [610, 223, 664, 245]))
        fallback_click.assert_not_called()
        self.assertNotIn("visible_button_fallback_context_menu", result["attempts"][0])

    def test_self_voice_transcript_binding_requires_right_aligned_layout(self) -> None:
        anchor = {
            "source": "parser_voice_message_context_menu_anchor",
            "click_bounds": [782, 520, 870, 552],
            "item": {
                "text": '2"',
                "voice_duration_text": '2"',
                "sender_role": "self",
                "parser_bubble_rect": [772, 520, 878, 556],
                "left": 800,
                "top": 528,
                "right": 850,
                "bottom": 548,
                "center_x": 825,
                "center_y": 538,
            },
        }
        shifted_voice_after = {
            "type": "voice",
            "sender": "self",
            "sender_role": "self",
            "content": '[语音] 2"',
            "voice_duration_text": '2"',
            "bubble_rect": [772, 520, 878, 556],
        }
        right_aligned_transcript = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "你中午回家吃饭不？",
            "bubble_rect": [681, 566, 878, 606],
        }
        left_column_text = {
            "type": "text",
            "sender": "customer",
            "sender_role": "customer",
            "content": "你中午回家吃饭不？",
            "bubble_rect": [464, 566, 661, 606],
        }

        self.assertTrue(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                right_aligned_transcript,
                anchor,
                (965, 852),
                after_messages=[shifted_voice_after, right_aligned_transcript],
            )
        )
        self.assertFalse(
            sidecar.message_is_plausible_voice_transcript_for_anchor(
                left_column_text,
                anchor,
                (965, 852),
                after_messages=[shifted_voice_after, left_column_text],
            )
        )

    def test_customer_visual_voice_anchor_when_duration_ocr_is_missing(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((458, 374, 660, 418), radius=8, fill=(232, 232, 234))
        self.draw_avatar(draw, (398, 372, 444, 420))

        items = [
            ocr_item('2"', 808, 510, 850, 535),
            ocr_item("你中午回家吃饭不？", 681, 545, 868, 589),
        ]

        anchor = sidecar.find_voice_context_menu_anchor_target(image, items, image.size)

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["source"], "visual_customer_voice_bubble_context_menu_anchor")
        self.assertLess(anchor["item"]["center_x"], 620)
        self.assertGreater(anchor["item"]["center_y"], 360)
        self.assertGreaterEqual(anchor["click_bounds"][0], 466)
        self.assertLessEqual(anchor["click_bounds"][2], 652)
        self.assertTrue(all(466 <= point[0] <= 652 for point in anchor["candidate_points"]))

    def test_transcribed_self_voice_does_not_block_customer_anchor(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((458, 374, 660, 418), radius=8, fill=(232, 232, 234))
        self.draw_avatar(draw, (398, 372, 444, 420))

        items = [
            ocr_item('2"', 808, 510, 850, 535),
            ocr_item("你中午回家吃饭不？", 681, 545, 868, 589),
        ]

        self.assertTrue(sidecar.voice_duration_has_transcribed_text_below(items[0], items, image.size))
        anchor = sidecar.find_voice_context_menu_anchor_target(image, items, image.size)

        self.assertEqual(anchor["source"], "visual_customer_voice_bubble_context_menu_anchor")

    def test_visual_customer_voice_anchor_rejects_plain_text_bubble(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((421, 396, 553, 440), radius=8, fill=(232, 232, 234))

        items = [
            ocr_item("不回了", 484, 407, 542, 432),
        ]

        self.assertIsNone(sidecar.find_visual_customer_voice_context_anchor_target(image, image.size, items))
        self.assertIsNone(sidecar.find_voice_context_menu_anchor_target(image, items, image.size))

    def test_visual_customer_voice_anchor_skips_transcribed_lower_bubble(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((464, 138, 622, 184), radius=8, fill=(232, 232, 234))
        draw.rounded_rectangle((464, 496, 598, 540), radius=8, fill=(232, 232, 234))
        draw.rounded_rectangle((464, 546, 782, 590), radius=8, fill=(232, 232, 234))

        items = [
            ocr_item('6"', 494, 150, 528, 174),
            ocr_item('4"', 494, 508, 528, 532),
            ocr_item("现在已经雨停了，外面又出太阳了。", 485, 556, 758, 579),
        ]

        anchor = sidecar.find_visual_customer_voice_context_anchor_target(image, image.size, items)

        self.assertIsNotNone(anchor)
        self.assertLess(anchor["item"]["center_y"], 220)

    def test_visual_customer_anchor_is_blocked_by_parser_confirmed_transcript(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((421, 444, 609, 488), radius=8, fill=(232, 232, 234))
        items = [
            ocr_item('5"', 489, 454, 535, 480),
            ocr_item("我看到这条路上还有车经过，车是可以开的。", 486, 506, 825, 529),
        ]
        voice = {
            "id": "customer-5",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 5"',
            "voice_duration_text": '5"',
            "bubble_rect": [489, 454, 535, 480],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        transcript = {
            "id": "customer-5-text",
            "type": "text",
            "sender_role": "self",
            "content": "我看到这条路上还有车经过，车是可以开的。",
            "bubble_rect": [486, 506, 825, 529],
        }

        self.assertTrue(sidecar.message_voice_has_transcribed_text_below(voice, [voice, transcript], image.size))
        self.assertIsNone(
            sidecar.find_visual_customer_voice_context_anchor_target(
                image,
                image.size,
                items,
                parsed_messages=[voice, transcript],
            )
        )

    def test_visual_self_voice_anchor_skips_transcribed_lower_bubble(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((772, 180, 878, 224), radius=8, fill=(140, 226, 146))
        draw.rounded_rectangle((772, 520, 878, 564), radius=8, fill=(140, 226, 146))
        draw.rounded_rectangle((681, 570, 878, 610), radius=8, fill=(232, 232, 234))

        items = [
            ocr_item('6"', 804, 190, 848, 214),
            ocr_item('2"', 804, 530, 848, 554),
            ocr_item("你中午回家吃饭不？", 700, 580, 870, 604),
        ]

        anchor = sidecar.find_visual_self_voice_context_anchor_target(image, image.size, items)

        self.assertIsNotNone(anchor)
        self.assertLess(anchor["item"]["center_y"], 260)

    def test_parser_voice_message_anchor_is_used(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        self.draw_avatar(ImageDraw.Draw(image), (398, 372, 444, 420))
        message = {
            "id": "voice-1",
            "type": "voice",
            "sender_role": "customer",
            "content": "[语音] 12\"",
            "bubble_rect": [458, 374, 660, 418],
            "ocr_items": [ocr_item('12"', 510, 384, 554, 408)],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }

        anchor = sidecar.find_voice_context_menu_anchor_target(image, [], image.size, parsed_messages=[message])

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["source"], "parser_voice_message_context_menu_anchor")
        self.assertEqual(anchor["item"]["message_id"], "voice-1")
        self.assertEqual(anchor["item"]["message_type"], "voice")
        self.assertGreater(anchor["item"]["center_y"], 370)

    def test_parser_voice_message_with_transcript_below_is_not_reselected(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        voice = {
            "id": "voice-done-placeholder",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 4"',
            "voice_duration_text": '4"',
            "bubble_rect": [464, 505, 598, 531],
            "ocr_items": [ocr_item('4"', 494, 508, 528, 530)],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        transcript = {
            "id": "voice-done-transcript",
            "type": "text",
            "sender_role": "self",
            "content": "现在已经雨停了，外面又出太阳了。",
            "bubble_rect": [464, 545, 782, 590],
        }

        self.assertTrue(sidecar.message_voice_has_transcribed_text_below(voice, [voice, transcript], image.size))
        anchor = sidecar.find_voice_context_menu_anchor_target(image, [], image.size, parsed_messages=[voice, transcript])

        self.assertIsNone(anchor)

    def test_parser_transcribed_voice_message_is_not_reselected(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        message = {
            "id": "voice-done-1",
            "type": "voice",
            "sender_role": "customer",
            "content": "现在已经雨停了，外面又出太阳了。",
            "bubble_rect": [458, 496, 782, 588],
            "ocr_items": [
                ocr_item('4"', 494, 508, 528, 532),
                ocr_item("现在已经雨停了，外面又出太阳了。", 480, 552, 770, 582),
            ],
            "quality_flags": ["voice_duration_prefix_removed"],
        }

        anchor = sidecar.find_voice_context_menu_anchor_target(image, [], image.size, parsed_messages=[message])

        self.assertIsNone(anchor)

    def test_processed_stable_key_skips_same_voice_after_text_expands(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        self.draw_avatar(draw, (398, 136, 444, 184))
        self.draw_avatar(draw, (398, 503, 444, 533))
        self.draw_avatar(draw, (398, 547, 444, 577))
        lower_before = {
            "id": "voice-lower-before",
            "type": "voice",
            "sender_role": "customer",
            "content": "[语音] 4\"",
            "bubble_rect": [464, 549, 598, 575],
            "ocr_items": [ocr_item('4"', 494, 553, 528, 575)],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        lower_anchor = sidecar.find_voice_context_menu_anchor_target(image, [], image.size, parsed_messages=[lower_before])
        self.assertIsNotNone(lower_anchor)
        excluded = sidecar.voice_context_anchor_exclusion_keys(lower_anchor, image.size)
        lower_after = {
            "id": "voice-lower-after",
            "type": "voice",
            "sender_role": "customer",
            "content": "[语音] 4\"",
            "bubble_rect": [464, 505, 598, 531],
            "ocr_items": [ocr_item('4"', 494, 508, 528, 530)],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }
        upper_pending = {
            "id": "voice-upper",
            "type": "voice",
            "sender_role": "customer",
            "content": "[语音] 6\"",
            "bubble_rect": [464, 138, 622, 184],
            "ocr_items": [ocr_item('6"', 494, 150, 528, 174)],
            "quality_flags": ["untranscribed_voice_placeholder"],
        }

        anchor = sidecar.find_voice_context_menu_anchor_target(
            image,
            [],
            image.size,
            parsed_messages=[lower_after, upper_pending],
            excluded_anchor_keys=excluded,
        )

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["item"]["message_id"], "voice-upper")
        self.assertGreater(anchor["item"]["center_y"], 145)
        self.assertLess(anchor["item"]["center_y"], 180)

    def test_visual_customer_voice_anchor_rejects_overlapping_parsed_text_message(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((421, 396, 553, 440), radius=8, fill=(232, 232, 234))
        parsed_messages = [
            {
                "id": "text-1",
                "type": "text",
                "sender_role": "customer",
                "content": "不回了",
                "bubble_rect": [421, 396, 553, 440],
            }
        ]

        anchor = sidecar.find_visual_customer_voice_context_anchor_target(
            image,
            image.size,
            [],
            parsed_messages=parsed_messages,
        )

        self.assertIsNone(anchor)

    def test_visual_self_voice_anchor_rejects_overlapping_parsed_text_message(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((772, 565, 878, 609), radius=8, fill=(140, 226, 146))
        parsed_messages = [
            {
                "id": "text-self-1",
                "type": "text",
                "sender_role": "self",
                "content": "在哪里",
                "bubble_rect": [772, 565, 878, 609],
            }
        ]

        anchor = sidecar.find_visual_self_voice_context_anchor_target(
            image,
            image.size,
            [],
            parsed_messages=parsed_messages,
        )

        self.assertIsNone(anchor)

    def test_read_only_visual_voice_hint_detects_self_voice_without_duration_ocr(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((772, 610, 919, 654), radius=8, fill=(140, 226, 146))
        self.draw_avatar(draw, (900, 608, 946, 656))

        hint = sidecar.visible_untranscribed_voice_hint(
            image,
            [],
            image.size,
            parsed_messages=[],
        )

        self.assertTrue(hint["detected"])
        self.assertEqual(hint["sender_role"], "self")
        self.assertEqual(hint["source"], "visual_self_voice_bubble_context_menu_anchor")

    def test_read_only_visual_voice_hint_rejects_plain_green_text_bubble(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((772, 565, 919, 609), radius=8, fill=(140, 226, 146))
        self.draw_avatar(draw, (900, 563, 946, 611))
        parsed_messages = [
            {
                "id": "text-self-1",
                "type": "text",
                "sender_role": "self",
                "content": "普通绿色文字",
                "bubble_rect": [772, 565, 919, 609],
            }
        ]

        hint = sidecar.visible_untranscribed_voice_hint(
            image,
            [],
            image.size,
            parsed_messages=parsed_messages,
        )

        self.assertEqual(hint, {"detected": False})

    def test_processed_customer_anchor_is_not_selected_again(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((458, 374, 660, 418), radius=8, fill=(232, 232, 234))
        self.draw_avatar(draw, (398, 372, 444, 420))

        anchor = sidecar.find_voice_context_menu_anchor_target(image, [], image.size)

        self.assertIsNotNone(anchor)
        excluded = {anchor["anchor_key"]}
        self.assertIsNone(sidecar.find_voice_context_menu_anchor_target(image, [], image.size, excluded_anchor_keys=excluded))
        self.assertFalse(sidecar.has_remaining_voice_transcribe_candidate(image, [], image.size, excluded_anchor_keys=excluded))

    def test_context_menu_anchor_uses_global_bottom_to_top_across_sources(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((766, 500, 874, 542), radius=8, fill=(140, 226, 146))
        draw.rounded_rectangle((772, 630, 878, 672), radius=8, fill=(140, 226, 146))
        self.draw_avatar(draw, (900, 498, 946, 544))
        self.draw_avatar(draw, (900, 628, 946, 674))

        items = [
            ocr_item('2"', 804, 508, 848, 532),
        ]

        anchor = sidecar.find_voice_context_menu_anchor_target(image, items, image.size)

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["source"], "visual_self_voice_bubble_context_menu_anchor")
        self.assertGreater(anchor["item"]["center_y"], 620)

    def test_visual_self_voice_anchor_stays_away_from_avatar_noise(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((772, 565, 878, 609), radius=8, fill=(140, 226, 146))
        draw.rectangle((900, 570, 919, 604), fill=(140, 226, 146))

        anchor = sidecar.find_visual_self_voice_context_anchor_target(image, image.size, [])

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["source"], "visual_self_voice_bubble_context_menu_anchor")
        self.assertEqual(anchor["item"]["right"], 919.0)
        self.assertLessEqual(anchor["click_bounds"][2], 861)
        self.assertGreaterEqual(anchor["click_bounds"][1], 570)
        self.assertLessEqual(anchor["click_bounds"][3], 604)
        self.assertTrue(all(point[0] <= 861 for point in anchor["candidate_points"]))
        self.assertTrue(all(570 <= point[1] <= 604 for point in anchor["candidate_points"]))

    def test_voice_transcribe_menu_texts_detects_open_menu(self) -> None:
        items = [
            ocr_item("语音转文字", 830, 642, 925, 670),
            ocr_item("收藏", 872, 694, 920, 720),
            ocr_item("收起文字", 830, 642, 925, 670),
        ]

        self.assertEqual(sidecar.voice_transcribe_menu_texts_from_items(items), ["语音转文字", "收起文字"])

    def test_context_menu_stable_wait_uses_shared_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "WECHAT_WIN32_OCR_CONTEXT_MENU_WAIT_MS": "1200",
                    "WECHAT_WIN32_OCR_VOICE_CONTEXT_MENU_WAIT_MS": "600",
                },
                clear=False,
            ),
            patch.object(sidecar, "humanized_action_sleep") as sleep,
        ):
            wait_ms = sidecar.wait_for_wechat_context_menu_stable()

        self.assertEqual(wait_ms, 1200)
        sleep.assert_called_once_with(950, 1650)

    def test_shared_context_menu_observer_captures_once_and_filters_near_anchor(self) -> None:
        image = Image.new("RGB", (1920, 1080), "white")
        near = ocr_item("复制", 20, 20, 90, 52)
        companion = ocr_item("转发", 20, 64, 90, 96)
        far = ocr_item("聊天正文", 600, 50, 720, 82)
        with (
            patch.object(
                sidecar,
                "resolve_wechat_context_menu_bounds",
                return_value={
                    "ok": True,
                    "reason": "context_menu_popup_window_confirmed",
                    "menu_panel_bounds": [410, 380, 520, 500],
                    "menu_hwnd": 2,
                    "menu_class_name": "WeChatMenuWnd",
                },
            ),
            patch.object(sidecar, "capture_visible_screen", return_value=(image, "menu.png")) as capture,
            patch.object(
                sidecar,
                "save_screenshot_artifact",
                return_value="menu_roi.png",
            ) as save_roi,
            patch.object(sidecar, "run_ocr", return_value=[near, companion, far]) as ocr,
        ):
            result = sidecar.observe_wechat_context_menu(
                1,
                anchor_screen=(520, 460),
                artifact_dir="evidence",
                label="shared_menu",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["text"] for item in result["local_ocr_items"]],
            ["复制", "转发"],
        )
        self.assertEqual(result["screenshot_path"], "menu.png")
        self.assertEqual(result["ocr_roi"], [410, 380, 520, 500])
        self.assertEqual(
            [item["text"] for item in result["menu_structure_evidence"]],
            ["复制", "转发"],
        )
        capture.assert_called_once_with(artifact_dir="evidence", label="shared_menu")
        self.assertEqual(result["roi_screenshot_path"], "menu_roi.png")
        self.assertEqual(
            result["menu_panel_bounds"], [410, 380, 520, 500]
        )
        save_roi.assert_called_once()
        self.assertEqual(ocr.call_args.args[0].size, (110, 120))
        image.close()

    def test_context_menu_bounds_use_distinct_same_process_popup(self) -> None:
        rects = {
            1: (100, 100, 1100, 900),
            2: (600, 300, 820, 690),
            3: (580, 280, 900, 760),
        }
        pids = {1: 700, 2: 700, 3: 999}
        gui = Mock()
        gui.GetWindowRect.side_effect = lambda hwnd: rects[hwnd]
        gui.IsWindowVisible.return_value = True
        gui.GetClassName.side_effect = lambda hwnd: f"WindowClass{hwnd}"
        gui.EnumWindows.side_effect = lambda callback, extra: [
            callback(candidate, extra) for candidate in (1, 2, 3)
        ]
        process = Mock()
        process.GetWindowThreadProcessId.side_effect = (
            lambda hwnd: (123, pids[hwnd])
        )

        with (
            patch.object(sidecar, "win32gui", gui),
            patch.object(sidecar, "win32process", process),
        ):
            result = sidecar.resolve_wechat_context_menu_bounds(
                1,
                anchor_screen=(610, 320),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["menu_panel_bounds"], [600, 300, 820, 690]
        )
        self.assertEqual(result["menu_hwnd"], 2)

    def test_context_menu_bounds_fail_closed_without_distinct_popup(self) -> None:
        gui = Mock()
        gui.GetWindowRect.return_value = (100, 100, 1100, 900)
        gui.IsWindowVisible.return_value = True
        gui.EnumWindows.side_effect = lambda callback, extra: callback(1, extra)
        process = Mock()
        process.GetWindowThreadProcessId.return_value = (123, 700)

        with (
            patch.object(sidecar, "win32gui", gui),
            patch.object(sidecar, "win32process", process),
        ):
            result = sidecar.resolve_wechat_context_menu_bounds(
                1,
                anchor_screen=(610, 320),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "context_menu_popup_window_not_found",
        )

    def test_context_menu_stable_wait_uses_longer_production_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "WECHAT_WIN32_OCR_CONTEXT_MENU_WAIT_MS": "",
                    "WECHAT_WIN32_OCR_VOICE_CONTEXT_MENU_WAIT_MS": "",
                },
                clear=False,
            ),
            patch.object(sidecar, "humanized_action_sleep") as sleep,
        ):
            wait_ms = sidecar.wait_for_wechat_context_menu_stable()

        self.assertEqual(wait_ms, 1800)
        sleep.assert_called_once_with(1550, 2250)

    def test_each_context_menu_action_waits_independently(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"WECHAT_WIN32_OCR_CONTEXT_MENU_WAIT_MS": "1800"},
                clear=False,
            ),
            patch.object(sidecar, "humanized_action_sleep") as sleep,
        ):
            first_wait_ms = sidecar.wait_for_wechat_context_menu_stable()
            second_wait_ms = sidecar.wait_for_wechat_context_menu_stable()

        self.assertEqual([first_wait_ms, second_wait_ms], [1800, 1800])
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(1550, 2250)

    def test_context_menu_prefers_nearby_collapse_over_far_inline_transcribe(self) -> None:
        image = Image.new("RGB", (1920, 1080), (247, 247, 247))
        anchor = {
            "source": "visual_customer_voice_bubble_context_menu_anchor",
            "click_bounds": [429, 449, 601, 483],
            "item": {"center_y": 466, "sender_role": "customer"},
        }
        # OCR now receives only the confirmed popup crop, so the far inline
        # chat label is structurally unavailable to menu classification.
        items = [ocr_item("收起文字", 22, 29, 102, 60)]
        with (
            patch.object(
                sidecar,
                "resolve_wechat_context_menu_bounds",
                return_value={
                    "ok": True,
                    "reason": "context_menu_popup_window_confirmed",
                    "menu_panel_bounds": [530, 430, 680, 530],
                    "menu_hwnd": 2,
                    "menu_class_name": "WeChatMenuWnd",
                },
            ),
            patch.object(sidecar, "human_window_image_right_click_in_bounds", return_value={"ok": True, "screen_y": 449}),
            patch.object(sidecar, "capture_visible_screen", return_value=(image, "menu.png")),
            patch.object(sidecar, "run_ocr", return_value=items),
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
        ):
            result = sidecar.open_voice_transcribe_context_menu(1, anchor, image_size=(965, 852))

        self.assertEqual(result["menu_state"], "already_transcribed")
        self.assertIsNone(result["click_target"])
        self.assertEqual(result["already_transcribed_target"]["item"]["text"], "收起文字")
        self.assertLess(result["already_transcribed_target"]["menu_distance_to_anchor"], result["menu_local_radius"])

    def test_context_menu_transcribe_target_stays_inside_ocr_text(self) -> None:
        item = ocr_item("语音转文字", 523, 508, 647, 535)

        target = sidecar.find_voice_transcribe_menu_item_target([item], (1920, 1080), anchor_screen_y=495)

        self.assertIsNotNone(target)
        self.assertGreaterEqual(target["click_bounds"][0], item["left"])
        self.assertLessEqual(target["click_bounds"][2], item["right"])
        self.assertGreaterEqual(target["click_bounds"][1], item["top"])
        self.assertLessEqual(target["click_bounds"][3], item["bottom"])

    def test_context_menu_click_uses_exact_ocr_text_center(self) -> None:
        item = ocr_item("语音转文字", 523, 508, 647, 535)
        target = sidecar.find_voice_transcribe_menu_item_target([item], (1920, 1080), anchor_screen_y=495)
        clicks: list[tuple[int, int]] = []

        def click(x: int, y: int, **_kwargs):
            clicks.append((x, y))
            return {"ok": True, "screen_x": x, "screen_y": y}

        with (
            patch.object(sidecar, "human_screen_click_in_bounds", side_effect=click),
            patch.object(sidecar, "verify_voice_transcribe_context_menu_closed", return_value={"ok": True, "reason": "menu_closed"}),
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
        ):
            result = sidecar.click_voice_transcribe_context_menu_target(
                1,
                target,
                geometry={"width": 965, "height": 852},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(clicks, [(int(item["center_x"]), int(item["center_y"]))])
        self.assertEqual(result["click_jitter"]["reason"], "click_exact_ocr_menu_text_center")

    def test_text_message_context_menu_text_is_detected(self) -> None:
        self.assertTrue(sidecar.text_message_context_menu_text_like("复制"))
        self.assertTrue(sidecar.text_message_context_menu_text_like("翻译"))
        self.assertTrue(sidecar.text_message_context_menu_text_like("搜一搜"))
        self.assertFalse(sidecar.text_message_context_menu_text_like("语音转文字"))

    def test_avatar_context_menu_text_is_detected(self) -> None:
        self.assertTrue(sidecar.avatar_context_menu_text_like("拍一拍"))
        self.assertFalse(sidecar.avatar_context_menu_text_like("语音转文字"))

    def test_context_menu_dismiss_uses_title_bar_click_not_escape(self) -> None:
        calls: list[str] = []
        clicks: list[dict] = []
        original_activate = sidecar.activate_window
        original_get_geometry = sidecar.get_window_geometry
        original_click = sidecar.human_window_image_click_in_bounds
        original_probe = sidecar.probe_wechat_windows
        original_probe_visible = sidecar.probe_has_usable_visible_main_window
        original_capture = sidecar.capture_wechat
        original_run_ocr = sidecar.run_ocr
        original_sleep = sidecar.humanized_action_sleep
        original_key_press = sidecar.key_press
        try:
            sidecar.activate_window = lambda _hwnd: calls.append("activate")
            sidecar.get_window_geometry = lambda _hwnd: {"width": 965, "height": 852}
            def fake_click(_hwnd, x, y, **kwargs):
                clicks.append({"x": x, "y": y, **kwargs})
                return {"ok": True}

            sidecar.human_window_image_click_in_bounds = fake_click
            sidecar.probe_wechat_windows = lambda: {"visible_main_windows": [{"hwnd": 1}], "windows": []}
            sidecar.probe_has_usable_visible_main_window = lambda _probe: True
            sidecar.capture_wechat = lambda _hwnd, **_kwargs: (Image.new("RGB", (965, 852), (255, 255, 255)), "")
            sidecar.run_ocr = lambda _image: []
            sidecar.humanized_action_sleep = lambda *_args, **_kwargs: None
            sidecar.key_press = lambda *_args, **_kwargs: calls.append("escape")

            result = sidecar.dismiss_voice_transcribe_context_menu(1)

            self.assertTrue(result["ok"])
            self.assertEqual(result["method"], "fresh_header_blank_click")
            self.assertLess(clicks[0]["y"], sidecar.chat_header_cutoff_y(852))
            self.assertNotIn("escape", calls)
        finally:
            sidecar.activate_window = original_activate
            sidecar.get_window_geometry = original_get_geometry
            sidecar.human_window_image_click_in_bounds = original_click
            sidecar.probe_wechat_windows = original_probe
            sidecar.probe_has_usable_visible_main_window = original_probe_visible
            sidecar.capture_wechat = original_capture
            sidecar.run_ocr = original_run_ocr
            sidecar.humanized_action_sleep = original_sleep
            sidecar.key_press = original_key_press

    def test_context_menu_dismiss_ignores_other_inline_transcribe_button(self) -> None:
        image = Image.new("RGB", (1920, 1080), (255, 255, 255))
        with (
            patch.object(sidecar, "activate_window", return_value=None),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 981, "height": 860}),
            patch.object(sidecar, "human_window_image_click_in_bounds", return_value={"ok": True}),
            patch.object(sidecar, "probe_wechat_windows", return_value={"visible_main_windows": [{"hwnd": 1}]}),
            patch.object(sidecar, "probe_has_usable_visible_main_window", return_value=True),
            patch.object(sidecar, "capture_wechat", return_value=(Image.new("RGB", (981, 860), (255, 255, 255)), "before.png")),
            patch.object(sidecar, "capture_visible_screen", return_value=(image, "dismissed.png")),
            patch.object(sidecar, "run_ocr", return_value=[ocr_item("转文字", 670, 155, 719, 173)]),
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
        ):
            result = sidecar.dismiss_voice_transcribe_context_menu(
                1,
                menu_bounds=[907, 322, 983, 346],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "menu_closed")
        self.assertEqual(result["visible_menu_texts"], [])

    def test_context_menu_close_verification_rejects_chat_info_panel(self) -> None:
        original_capture = sidecar.capture_visible_screen
        original_run_ocr = sidecar.run_ocr
        try:
            sidecar.capture_visible_screen = lambda **_kwargs: (Image.new("RGB", (965, 852), (255, 255, 255)), "")
            sidecar.run_ocr = lambda _image: [ocr_item("查找聊天内容", 668, 231, 765, 254)]

            result = sidecar.verify_voice_transcribe_context_menu_closed()

            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "menu_or_panel_still_visible")
            self.assertEqual(result["visible_panel_texts"], ["查找聊天内容"])
        finally:
            sidecar.capture_visible_screen = original_capture
            sidecar.run_ocr = original_run_ocr

    def test_context_menu_close_verification_ignores_inline_transcribe_button_outside_menu(self) -> None:
        original_capture = sidecar.capture_visible_screen
        original_run_ocr = sidecar.run_ocr
        try:
            sidecar.capture_visible_screen = lambda **_kwargs: (Image.new("RGB", (1920, 1080), (255, 255, 255)), "")
            sidecar.run_ocr = lambda _image: [ocr_item("转文字", 640, 454, 704, 480)]

            result = sidecar.verify_voice_transcribe_context_menu_closed(menu_bounds=[509, 630, 752, 681])

            self.assertTrue(result["ok"])
            self.assertEqual(result["visible_menu_texts"], [])
        finally:
            sidecar.capture_visible_screen = original_capture
            sidecar.run_ocr = original_run_ocr

    def test_context_menu_close_verification_detects_menu_item_inside_original_bounds(self) -> None:
        original_capture = sidecar.capture_visible_screen
        original_run_ocr = sidecar.run_ocr
        try:
            sidecar.capture_visible_screen = lambda **_kwargs: (Image.new("RGB", (1920, 1080), (255, 255, 255)), "")
            sidecar.run_ocr = lambda _image: [ocr_item("语音转文字", 533, 642, 656, 669)]

            result = sidecar.verify_voice_transcribe_context_menu_closed(menu_bounds=[509, 630, 752, 681])

            self.assertFalse(result["ok"])
            self.assertEqual(result["visible_menu_texts"], ["语音转文字"])
        finally:
            sidecar.capture_visible_screen = original_capture
            sidecar.run_ocr = original_run_ocr

    def test_avatar_role_is_stable_after_whole_row_vertical_translation(self) -> None:
        def detect(offset: int) -> str:
            image = Image.new("RGB", (965, 852), (247, 247, 247))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
            self.draw_avatar(draw, (398, 198 + offset, 444, 244 + offset))
            details = sidecar.message_row_avatar_role_details(
                image,
                [470, 210 + offset, 820, 238 + offset],
                image.size,
            )
            self.assertEqual(details["customer"]["position_source"], "bubble_relative_avatar_adjacency")
            return details["role"]

        self.assertEqual(detect(0), "customer")
        self.assertEqual(detect(54), "customer")

    def test_equal_duration_voice_structural_keys_survive_page_shift(self) -> None:
        def build(offset: int) -> dict[str, str]:
            image = Image.new("RGB", (965, 852), (247, 247, 247))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
            messages = []
            items = []
            for message_id, top in (("upper", 220 + offset), ("lower", 430 + offset)):
                self.draw_avatar(draw, (398, top - 2, 444, top + 44))
                duration = ocr_item('4"', 488, top + 8, 526, top + 32)
                items.append(duration)
                messages.append({
                    "id": message_id,
                    "type": "voice",
                    "sender_role": "customer",
                    "content": '[语音] 4"',
                    "voice_duration_text": '4"',
                    "bubble_rect": [462, top, 590, top + 44],
                    "ocr_items": [duration],
                    "quality_flags": ["untranscribed_voice_placeholder"],
                })
            observations = sidecar.build_unified_voice_observations_v3(
                image,
                items,
                image.size,
                parsed_messages=messages,
            )
            return {
                item["source_message_id"]: item["voice_anchor_structural_key"]
                for item in observations
            }

        baseline = build(0)
        shifted = build(48)
        self.assertNotEqual(baseline["upper"], baseline["lower"])
        self.assertEqual(baseline, shifted)

    def test_visual_voice_without_adjacent_avatar_is_not_actionable(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 376, 852), fill=(238, 238, 238))
        draw.rounded_rectangle((462, 350, 590, 394), radius=8, fill=(232, 232, 234))

        self.assertIsNone(sidecar.find_voice_context_menu_anchor_target(image, [], image.size))

    def test_visible_candidate_coordinates_are_never_clicked_without_fresh_rescan(self) -> None:
        stale_candidate = {
            "name": "CJR8S5K3 虾丸子大人",
            "session_key": "wx:rpa:v1:stale",
            "center_y": 612,
            "top": 590,
            "bottom": 634,
        }
        with (
            patch.object(
                sidecar,
                "validate_active_send_target",
                return_value={
                    "ok": True,
                    "online": True,
                    "confirmation_confidence": "active_title_strict",
                    "conversation_type": "private",
                    "conversation_type_evidence": {"short_code_confirmed": True},
                },
            ) as validate,
            patch.object(sidecar, "consume_recent_target_switch_validation", return_value=None),
            patch.object(sidecar, "activate_session_candidate") as activate_candidate,
            patch.object(sidecar, "open_chat", return_value=True) as open_chat,
            patch.object(sidecar, "humanized_action_sleep", return_value=None),
        ):
            result = sidecar.locate_chat_target_for_c2(
                1,
                target="CJR8S5K3 虾丸子大人",
                session_key="wx:rpa:v1:stale",
                remark_code="CJR8S5K3",
                target_mode="visible",
                visible_session_candidate=stale_candidate,
                exact=False,
                artifact_dir=None,
                sidecar_run_id="run-1",
                failure_state="failed",
                failure_error_code="FAILED",
            )

        self.assertTrue(result["ok"])
        validate.assert_called_once_with(1, "CJR8S5K3", exact=False, artifact_dir=None)
        activate_candidate.assert_not_called()
        open_chat.assert_called_once_with(
            1,
            "CJR8S5K3 虾丸子大人",
            exact=False,
            artifact_dir=None,
            session_key="wx:rpa:v1:stale",
            semantic_target="CJR8S5K3",
        )
        self.assertEqual(
            result["targeting"]["visible_session_candidate_activation"]["reason"],
            "fresh_semantic_reacquire_required",
        )
        self.assertEqual(
            result["targeting"]["visible_precheck"]["reason"],
            "merged_into_open_chat_fresh_rescan",
        )
        self.assertTrue(result["targeting"]["visible_postcheck"]["fallback_full_ocr"])

    def test_visible_locate_reuses_strict_validation_from_current_open_chat(self) -> None:
        strong_validation = {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "active_title_match": True,
            "confirmation_confidence": "active_title_strict",
            "conversation_type": "private",
            "conversation_type_evidence": {"short_code_confirmed": True},
            "geometry": {"left": 10, "top": 20, "width": 965, "height": 852},
        }
        previous_state = dict(sidecar._LAST_RPA_ACTION_STATE)

        def fake_open_chat(*_args, **_kwargs):
            sidecar._LAST_RPA_ACTION_STATE["active_session_key"] = "wx:rpa:v1:fresh"
            sidecar.remember_target_switch_validation(
                hwnd=1,
                target="CJR8S5K3",
                exact=False,
                session_key="wx:rpa:v1:fresh",
                validation=strong_validation,
                geometry=strong_validation["geometry"],
            )
            return True

        try:
            with (
                patch.object(sidecar, "open_chat", side_effect=fake_open_chat),
                patch.object(sidecar, "get_window_geometry", return_value=strong_validation["geometry"]),
                patch.object(sidecar, "validate_active_send_target") as validate,
                patch.object(sidecar, "humanized_action_sleep") as sleep,
            ):
                result = sidecar.locate_chat_target_for_c2(
                    1,
                    target="CJR8S5K3 虾丸子大人",
                    session_key="wx:rpa:v1:stale",
                    remark_code="CJR8S5K3",
                    target_mode="visible",
                    visible_session_candidate=None,
                    exact=False,
                    artifact_dir=None,
                    sidecar_run_id="run-2",
                    failure_state="failed",
                    failure_error_code="FAILED",
                )
        finally:
            sidecar._LAST_RPA_ACTION_STATE.clear()
            sidecar._LAST_RPA_ACTION_STATE.update(previous_state)

        self.assertTrue(result["ok"])
        validate.assert_not_called()
        sleep.assert_not_called()
        self.assertTrue(result["guard"]["target_ready_reused_switch_validation"])
        self.assertTrue(result["targeting"]["visible_postcheck"]["reused"])
        self.assertFalse(result["targeting"]["visible_postcheck"]["fallback_full_ocr"])

    def test_strict_switch_validation_reuse_rejects_old_or_wrong_session_proof(self) -> None:
        geometry = {"left": 10, "top": 20, "width": 965, "height": 852}
        validation = {
            "ok": True,
            "online": True,
            "confirmation_confidence": "active_title_strict",
            "geometry": geometry,
        }
        previous_state = dict(sidecar._LAST_RPA_ACTION_STATE)
        try:
            with patch.object(sidecar, "get_window_geometry", return_value=geometry):
                sidecar.remember_target_switch_validation(
                    hwnd=1,
                    target="CJR8S5K3",
                    exact=False,
                    session_key="wx:rpa:v1:fresh",
                    validation=validation,
                    geometry=geometry,
                )
                cached_at = float(sidecar._LAST_RPA_ACTION_STATE["target_ready_last_switch_validation"]["ts"])
                old_proof = sidecar.consume_recent_target_switch_validation(
                    hwnd=1,
                    target="CJR8S5K3",
                    exact=False,
                    session_key="wx:rpa:v1:fresh",
                    minimum_cached_at=cached_at + 0.001,
                    require_session_key_match=True,
                )
                wrong_session = sidecar.consume_recent_target_switch_validation(
                    hwnd=1,
                    target="CJR8S5K3",
                    exact=False,
                    session_key="wx:rpa:v1:other",
                    minimum_cached_at=cached_at,
                    require_session_key_match=True,
                )
        finally:
            sidecar._LAST_RPA_ACTION_STATE.clear()
            sidecar._LAST_RPA_ACTION_STATE.update(previous_state)

        self.assertIsNone(old_proof)
        self.assertIsNone(wrong_session)

    def test_open_chat_ignores_stale_key_and_uses_unique_remark_code_candidate(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        fresh_items = [ocr_item("CJR8S5K3虾丸..昨天12:02", 154, 112, 330, 136)]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, fresh_items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "active_chat_matches", return_value=[]),
            patch.object(sidecar, "activate_session_candidate", return_value=True) as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "CJR8S5K3 虾丸,..",
                exact=False,
                session_key="wx:rpa:v1:b5e62034c2b753afdae7",
                semantic_target="CJR8S5K3",
            )

        self.assertTrue(opened)
        activated = activate_candidate.call_args.args[1]
        self.assertEqual(activated["name"], "CJR8S5K3虾丸")
        self.assertEqual(activate_candidate.call_args.kwargs["target"], "CJR8S5K3")
        self.assertEqual(
            sidecar._LAST_OPEN_CHAT_TIMING["open_chat_semantic_candidate"]["matched_by"],
            "remark_code",
        )
        self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "semantic_candidate_activated")

    def test_open_chat_uses_unique_semantic_candidate_without_cached_session_key(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        fresh_items = [ocr_item("CJR8S5K3 虾丸子大人", 154, 112, 330, 136)]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, fresh_items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "active_chat_matches", return_value=[]),
            patch.object(sidecar, "activate_session_candidate", return_value=True) as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "虾丸子大人",
                exact=False,
                session_key="",
                semantic_target="CJR8S5K3",
            )

        self.assertTrue(opened)
        self.assertEqual(activate_candidate.call_args.kwargs["target"], "CJR8S5K3")
        self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "semantic_candidate_activated")

    def test_session_list_ocr_items_merges_sidebar_enhancement_once(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        base = [ocr_item("普通会话", 154, 190, 260, 214)]
        enhanced = ocr_item("CJR8S5K3 虾丸子", 154, 112, 330, 136)
        enhanced["ocr_source"] = "sidebar_visible_list_enhanced"
        with patch.object(sidecar, "sidebar_visible_list_enhanced_ocr_items", return_value=[enhanced]) as enhance:
            merged, enhanced_count = sidecar.session_list_ocr_items(image, base)
            merged_again, enhanced_count_again = sidecar.session_list_ocr_items(image, merged)

        self.assertEqual(enhanced_count, 1)
        self.assertEqual(enhanced_count_again, 1)
        self.assertEqual([item["text"] for item in merged_again], ["普通会话", "CJR8S5K3 虾丸子"])
        enhance.assert_called_once()

    def test_open_chat_does_not_click_ambiguous_semantic_candidates(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        fresh_items = [
            ocr_item("CJR8S5K3 虾丸子", 154, 112, 330, 136),
            ocr_item("CJR8S5K3 测试", 154, 190, 330, 214),
        ]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, fresh_items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "active_chat_matches", return_value=[]),
            patch.object(sidecar, "activate_session_candidate") as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "CJR8S5K3 虾丸子",
                exact=False,
                session_key="wx:rpa:v1:stale",
                semantic_target="CJR8S5K3",
            )

        self.assertFalse(opened)
        activate_candidate.assert_not_called()
        self.assertEqual(
            sidecar._LAST_OPEN_CHAT_TIMING["reason"],
            "semantic_candidate_ambiguous",
        )

    def test_open_chat_does_not_click_when_active_private_title_has_target_code(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        items = [
            ocr_item("CJR8S5K3 虾丸子", 154, 112, 330, 136),
            ocr_item("CJR8S5K3 虾丸子大人", 405, 52, 620, 80),
        ]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "activate_session_candidate") as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "旧显示名",
                exact=False,
                session_key="wx:rpa:v1:stale",
                semantic_target="CJR8S5K3",
            )

        self.assertTrue(opened)
        activate_candidate.assert_not_called()
        self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "active_private_remark_code_match")

    def test_open_chat_stops_on_active_group_before_sidebar_reacquire(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        for group_title in ("CJTEST01 (6)", "CJTEST01（6）"):
            items = [
                ocr_item("CJTEST01", 154, 112, 330, 136),
                ocr_item(group_title, 405, 52, 620, 80),
            ]
            with self.subTest(group_title=group_title):
                with (
                    patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
                    patch.object(sidecar, "ensure_main_session_list", return_value=(image, items)),
                    patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
                    patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
                    patch.object(sidecar, "parse_sessions_from_ocr") as parse_sessions,
                    patch.object(sidecar, "activate_session_candidate") as activate_candidate,
                ):
                    opened = sidecar.open_chat(
                        1,
                        "CJTEST01",
                        exact=False,
                        session_key="wx:rpa:v1:stale",
                        semantic_target="CJTEST01",
                    )

                self.assertFalse(opened)
                parse_sessions.assert_not_called()
                activate_candidate.assert_not_called()
                self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "active_group_remark_code_blocked")
                evidence = sidecar._LAST_OPEN_CHAT_TIMING["open_chat_initial_active_evidence"]
                self.assertEqual(evidence["conversation_type"], "group")
                self.assertTrue(evidence["short_code_confirmed"])

    def test_open_chat_stops_on_active_unknown_target_title(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        items = [
            ocr_item("CJTEST01", 154, 112, 330, 136),
            ocr_item("CJTEST01 (...)", 405, 52, 620, 80),
        ]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "parse_sessions_from_ocr") as parse_sessions,
            patch.object(sidecar, "activate_session_candidate") as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "CJTEST01",
                exact=False,
                session_key="wx:rpa:v1:stale",
                semantic_target="CJTEST01",
            )

        self.assertFalse(opened)
        parse_sessions.assert_not_called()
        activate_candidate.assert_not_called()
        self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "active_unknown_remark_code_blocked")

    def test_visible_locate_returns_terminal_group_without_second_ocr(self) -> None:
        previous_timing = dict(sidecar._LAST_OPEN_CHAT_TIMING)

        def fake_open_chat(*_args, **_kwargs):
            sidecar._LAST_OPEN_CHAT_TIMING.clear()
            sidecar._LAST_OPEN_CHAT_TIMING.update(
                {
                    "reason": "active_group_remark_code_blocked",
                    "open_chat_initial_active_evidence": {
                        "matched": True,
                        "short_code_confirmed": True,
                        "conversation_type": "group",
                        "admission_allowed": False,
                        "raw_title": "CJTEST01 (6)",
                    },
                }
            )
            return False

        try:
            with (
                patch.object(sidecar, "open_chat", side_effect=fake_open_chat),
                patch.object(sidecar, "validate_active_send_target") as validate,
            ):
                result = sidecar.locate_chat_target_for_c2(
                    1,
                    target="CJTEST01",
                    session_key="wx:rpa:v1:stale",
                    remark_code="CJTEST01",
                    target_mode="visible",
                    visible_session_candidate=None,
                    exact=False,
                    artifact_dir=None,
                    sidecar_run_id="run-group",
                    failure_state="failed",
                    failure_error_code="TARGET_NOT_CONFIRMED",
                )
        finally:
            sidecar._LAST_OPEN_CHAT_TIMING.clear()
            sidecar._LAST_OPEN_CHAT_TIMING.update(previous_timing)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C2_GROUP_CHAT_NOT_ALLOWED")
        self.assertEqual(result["guard"]["conversation_type"], "group")
        self.assertEqual(
            result["targeting"]["visible_postcheck"]["reason"],
            "terminal_active_title_admission_reused",
        )
        validate.assert_not_called()

    def test_open_chat_blocks_two_private_sessions_with_same_remark_code(self) -> None:
        image = Image.new("RGB", (965, 852), (247, 247, 247))
        items = [
            ocr_item("CJR8S5K3 张三", 154, 112, 330, 136),
            ocr_item("CJR8S5K3 李四", 154, 190, 330, 214),
            ocr_item("CJR8S5K3 张三", 405, 52, 620, 80),
        ]
        with (
            patch.object(sidecar, "consume_target_ready_prevalidation_ocr_seed", return_value=None),
            patch.object(sidecar, "ensure_main_session_list", return_value=(image, items)),
            patch.object(sidecar, "get_window_geometry", return_value={"width": 965, "height": 852}),
            patch.object(sidecar, "target_switch_surface_state", return_value={"ok": True}),
            patch.object(sidecar, "activate_session_candidate") as activate_candidate,
        ):
            opened = sidecar.open_chat(
                1,
                "CJR8S5K3 张三",
                exact=False,
                session_key="wx:rpa:v1:stale",
                semantic_target="CJR8S5K3",
            )

        self.assertFalse(opened)
        activate_candidate.assert_not_called()
        self.assertEqual(sidecar._LAST_OPEN_CHAT_TIMING["reason"], "active_private_remark_code_ambiguous")

    def test_wrong_active_title_is_lookup_failure_not_c2_unknown(self) -> None:
        error_code, _message = sidecar.c2_target_admission_error(
            {
                "conversation_type": "unknown",
                "conversation_type_evidence": {
                    "short_code_confirmed": False,
                    "conversation_type": "unknown",
                    "raw_title": "OTHER01 其他会话",
                },
            },
            "TARGET_NOT_CONFIRMED",
        )

        self.assertEqual(error_code, "TARGET_NOT_CONFIRMED")

    def test_target_code_unknown_admission_remains_terminal(self) -> None:
        error_code, _message = sidecar.c2_target_admission_error(
            {
                "conversation_type_evidence": {
                    "short_code_confirmed": True,
                    "conversation_type": "unknown",
                    "raw_title": "CJR8S5K3 (...)",
                },
            },
            "TARGET_NOT_CONFIRMED",
        )

        self.assertEqual(error_code, "C2_CONVERSATION_TYPE_UNKNOWN")

    def test_physical_session_key_can_distinguish_same_code_rows(self) -> None:
        first = sidecar.rpa_session_key(
            "CJR8S5K3 张三",
            row_fingerprint={"duplicate_discriminator": "0"},
        )
        second = sidecar.rpa_session_key(
            "CJR8S5K3 李四",
            row_fingerprint={"duplicate_discriminator": "1"},
        )

        self.assertNotEqual(first, second)

    def test_safe_header_target_avoids_ocr_title_bounds(self) -> None:
        target = sidecar.safe_window_header_blank_click_target(
            [ocr_item("CJR8S5K3 虾丸子大人", 440, 10, 590, 34)],
            (965, 852),
            geometry={"width": 965, "height": 852},
        )

        self.assertIsNotNone(target)
        x, y = target["point"]
        self.assertFalse(422 <= x <= 608 and 2 <= y <= 42)

    def test_connector_preserves_partial_state_without_restarting_whole_voice_flow(self) -> None:
        calls: list[list[str]] = []

        class StubConnector(wechat_connector.WeChatConnector):
            def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
                calls.append(list(args))
                return {
                    "ok": True,
                    "state": "voice_transcribe_partial",
                    "quality_flags": ["untranscribed_voice_remaining"],
                    "transcribed_messages": [{"content": "果然掉在更衣柜里了。"}],
                    "new_messages": [],
                }

        with patch.object(wechat_connector, "wechat_rpa_lock", return_value=nullcontext({"lock_id": "test"})):
            result = StubConnector().transcribe_voice_messages("CJR8S5K3", max_attempts=4)

        self.assertEqual(result["state"], "voice_transcribe_partial")
        self.assertEqual(result["quality_flags"], ["untranscribed_voice_remaining"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
