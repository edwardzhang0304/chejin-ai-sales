from __future__ import annotations

import os
import io
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-vision-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.omniauto_vision import (
    DEFAULT_VISION_BASE_URL,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_PROVIDER,
    DEFAULT_VISION_REQUEST_STYLE,
    DEFAULT_VISION_TIMEOUT_SECONDS,
    VisionCancelledError,
    _CancellableVisionProvider,
    _UiAction,
    _VisionHostState,
    _WindowFrame,
    _frame_fingerprint,
    _menu_ocr_evidence,
    explicit_vision_config,
    process_image_slot,
    vision_configuration_status,
)
from chejin_worker_client.action_journal import (
    initialize_action_journal,
    read_action_journal,
)
from chejin_worker_client.wechat_c2 import apply_image_terminal_result
from apps.wechat_ai_customer_service.optional_plugins.vision.capture import transaction
from apps.wechat_ai_customer_service.optional_plugins.vision.capture import (
    visual_fingerprint,
)
from apps.wechat_ai_customer_service.optional_plugins.vision.capture.wechat import (
    attach_image_physical_anchors,
    detect_visual_image_bubbles,
    find_copy_menu_item,
)
from apps.wechat_ai_customer_service.optional_plugins.vision.ports import VisionHostPorts
from apps.wechat_ai_customer_service.optional_plugins.vision.understanding.service import (
    effective_customer_image_understanding_settings,
)
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar


class C2VisionIntegrationTests(unittest.TestCase):
    def test_c2_role_remains_authoritative_when_visual_side_conflicts(self):
        screenshot = Image.new("RGB", (800, 700), "white")
        expected_anchor = {
            "sender_role": "customer",
            "preceding_stable_message": "before-message",
            "following_stable_message": "after-message",
            "bubble_visual_fingerprint": "dhash64:0000000000000000",
            "occurrence_index": 0,
            "occurrence_count": 1,
        }
        current_candidate = {
            "bounds": [430, 220, 630, 360],
            "side": "self",
            "anchor": {"x": 530, "y": 290},
            "image_physical_anchor": {
                "sender_role": "self",
                "preceding_stable_message": "before-message",
                "following_stable_message": "after-message",
                "bubble_visual_fingerprint": "dhash64:0000000000000000",
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
        }
        with patch.object(
            transaction,
            "attach_image_physical_anchors",
            return_value=[current_candidate],
        ):
            matched = transaction._matching_bubble(
                screenshot,
                [current_candidate],
                [],
                expected_anchor=expected_anchor,
                expected_role="customer",
            )

        self.assertTrue(matched)
        evidence = matched["identity_match_evidence"]
        self.assertEqual(evidence["c2_sender_role"], "customer")
        self.assertEqual(evidence["visual_side"], "self")
        self.assertFalse(evidence["visual_side_consistent"])
        screenshot.close()
    def setUp(self) -> None:
        self.vision_env_names = (
            "CUSTOMER_IMAGE_UNDERSTANDING_PROVIDER",
            "CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL",
            "CUSTOMER_IMAGE_UNDERSTANDING_MODEL",
            "CUSTOMER_IMAGE_UNDERSTANDING_REQUEST_STYLE",
            "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY",
            "CUSTOMER_IMAGE_UNDERSTANDING_TIMEOUT_SECONDS",
            "ANTHROPIC_AUTH_TOKEN",
        )
        self.original_env = {name: os.environ.get(name) for name in self.vision_env_names}
        for name in self.vision_env_names:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def image_anchor(
        *,
        role: str = "customer",
        occurrence_index: int = 0,
        occurrence_count: int = 1,
    ) -> dict:
        return {
            "sender_role": role,
            "preceding_stable_message": "",
            "following_stable_message": "",
            "bubble_visual_fingerprint": "dhash64:0000000000000000",
            "occurrence_index": occurrence_index,
            "occurrence_count": occurrence_count,
        }

    @staticmethod
    def image_observation(*, role: str = "customer", role_source: str = "same_row_avatar") -> dict:
        image_anchor = {
            "sender_role": role,
            "preceding_stable_message": "before-image",
            "following_stable_message": "after-image",
            "bubble_visual_fingerprint": "dhash64:0000000000000000",
            "occurrence_index": 0,
            "occurrence_count": 1,
        }
        return {
            "schema_version": 3,
            "observation_id": "canonical_visual_image_1",
            "row_kind": "image_bubble",
            "sender_role": role,
            "sender_role_source": role_source,
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": image_anchor,
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": "canonical_visual_image_1",
                "type": "image",
                "image_physical_anchor": image_anchor,
            },
        }

    @staticmethod
    def window_context() -> dict:
        return {
            "schema_version": 1,
            "hwnd": 31415,
            "pid": 2718,
            "class_name": "WeChatMainWndForPC",
            "source": "sidecar_selected_main_window",
        }

    def test_missing_api_key_stops_before_plugin(self):
        result = process_image_slot(
            observation=self.image_observation(),
            remark_code="CJTEST01",
            session_key="wx-row-1",
        )

        self.assertEqual(result["state"], "capability_paused")
        self.assertEqual(result["reason"], "vision_configuration_incomplete")
        self.assertEqual(
            result["missing_configuration"],
            ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY_OR_ANTHROPIC_AUTH_TOKEN"],
        )
        self.assertFalse(result["diagnostics"]["image_persisted"])

    def test_image_candidate_frame_is_reused_for_target_confirmation(self):
        image = Image.new("RGB", (800, 600), "white")

        class Target:
            context = {}

            def confirm_target(self, context):
                self.context = context
                return {"ok": True}

        class Frames:
            calls = 0
            candidate_image = None

            def capture_frame(self, context):
                self.calls += 1
                if context.get("phase") == "image_context_menu":
                    return {
                        "ok": True,
                        "image": image.copy(),
                        "ocr_items": [{"text": "复制"}],
                        "screen_origin": [0, 0],
                    }
                frame_image = image.copy()
                if context.get("phase") == "image_candidate":
                    self.candidate_image = frame_image
                return {"ok": True, "image": frame_image, "image_size": image.size, "messages": [], "time_markers": []}

        class Actions:
            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                return {"screen_x": 500, "screen_y": 240}

            def click_screen(self, _x, _y, *, bounds):
                self.bounds = bounds
                return None

        class Clipboard:
            sequence = iter([10, 11, 11])

            def sequence_number(self):
                return next(self.sequence)

            def read_current_bitmap(self):
                return image.crop((420, 180, 650, 320))

        target = Target()
        frames = Frames()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(lease=lambda *_args, **_kwargs: nullcontext()),
            conversation_target=target,
            window_frame=frames,
            ui_action=Actions(),
            clipboard=Clipboard(),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[{"bounds": [420, 180, 650, 320], "side": "customer", "anchor": {"x": 500, "y": 240}}],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320, "bounds": [600, 300, 650, 340]},
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
                    "image_physical_anchor": self.image_anchor(),
                },
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(frames.calls, 2)
        self.assertIn("candidate_frame", target.context)
        self.assertIs(target.context["candidate_frame"]["image"], frames.candidate_image)
        result["_ephemeral_clipboard_image"].release()

    def test_copy_journal_is_persisted_before_physical_click(self):
        image = Image.new("RGB", (800, 600), "white")
        events: list[str] = []

        class Actions:
            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                return {"screen_x": 500, "screen_y": 240}

            def click_screen(self, _x, _y, *, bounds):
                self.bounds = bounds
                events.append("click")

        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda context: {
                    "ok": True,
                    "image": image.copy(),
                    "image_size": image.size,
                    "messages": [],
                    "time_markers": [],
                    "ocr_items": (
                        [{"text": "复制"}]
                        if context.get("phase") == "image_context_menu"
                        else []
                    ),
                    "screen_origin": [0, 0],
                }
            ),
            ui_action=Actions(),
            clipboard=SimpleNamespace(
                sequence_number=iter([10, 11, 11]).__next__,
                read_current_bitmap=lambda: image.crop(
                    (420, 180, 650, 320)
                ),
            ),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": [420, 180, 650, 320],
                    "side": "customer",
                    "anchor": {"x": 500, "y": 240},
                }
            ],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320, "bounds": [600, 300, 650, 340]},
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
                    "image_physical_anchor": self.image_anchor(),
                    "action_journal_update": lambda **kwargs: events.append(
                        str(kwargs["action_phase"])
                    ),
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            ["trigger_attempted", "click", "confirmed"],
        )
        result["_ephemeral_clipboard_image"].release()

    def test_copy_click_exception_always_dismisses_context_menu(self):
        image = Image.new("RGB", (800, 600), "white")

        class Actions:
            dismissed = 0

            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                return {"screen_x": 500, "screen_y": 240}

            def click_screen(self, _x, _y, *, bounds):
                self.bounds = bounds
                raise RuntimeError("click failed")

            def dismiss_menu_safely(self):
                self.dismissed += 1

        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(lease=lambda *_args, **_kwargs: nullcontext()),
            conversation_target=SimpleNamespace(confirm_target=lambda _context: {"ok": True}),
            window_frame=SimpleNamespace(
                capture_frame=lambda context: {
                    "ok": True,
                    "image": image.copy(),
                    "image_size": image.size,
                    "messages": [],
                    "time_markers": [],
                    "ocr_items": [{"text": "复制"}] if context.get("phase") == "image_context_menu" else [],
                    "screen_origin": [0, 0],
                }
            ),
            ui_action=actions,
            clipboard=SimpleNamespace(sequence_number=lambda: 10),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[{"bounds": [420, 180, 650, 320], "side": "customer", "anchor": {"x": 500, "y": 240}}],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320, "bounds": [600, 300, 650, 340]},
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
                    "image_physical_anchor": self.image_anchor(),
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "vision_port_transaction_exception")
        self.assertEqual(actions.dismissed, 1)

    def test_non_secret_defaults_and_anthropic_token_alias_build_formal_config(self):
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "unit-only"

        config, missing = explicit_vision_config()

        self.assertEqual(missing, [])
        settings = config["customer_image_understanding"]
        self.assertEqual(settings["provider"], DEFAULT_VISION_PROVIDER)
        self.assertEqual(settings["base_url"], DEFAULT_VISION_BASE_URL)
        self.assertEqual(settings["model"], DEFAULT_VISION_MODEL)
        self.assertEqual(settings["request_style"], DEFAULT_VISION_REQUEST_STYLE)
        self.assertEqual(settings["api_key_env"], "ANTHROPIC_AUTH_TOKEN")
        self.assertEqual(
            settings["timeout_seconds"],
            DEFAULT_VISION_TIMEOUT_SECONDS,
        )

    def test_vision_timeout_env_is_shared_by_parent_and_provider_settings(self):
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "unit-only"
        os.environ["CUSTOMER_IMAGE_UNDERSTANDING_TIMEOUT_SECONDS"] = "75"

        config, missing = explicit_vision_config()
        provider_settings = effective_customer_image_understanding_settings(config)

        self.assertEqual(missing, [])
        self.assertEqual(
            config["customer_image_understanding"]["timeout_seconds"],
            75.0,
        )
        self.assertEqual(provider_settings["timeout_seconds"], 75)

    def test_copy_menu_requires_local_menu_cluster_and_ignores_chat_copy_text(self):
        ocr_items = [
            {
                "text": "请复制这段文字给我",
                "left": 430,
                "top": 120,
                "right": 620,
                "bottom": 150,
                "confidence": 0.99,
            },
            {
                "text": "复制",
                "left": 585,
                "top": 285,
                "right": 635,
                "bottom": 315,
                "confidence": 0.96,
            },
            {
                "text": "转发",
                "left": 585,
                "top": 325,
                "right": 635,
                "bottom": 355,
                "confidence": 0.95,
            },
            {
                "text": "收藏",
                "left": 585,
                "top": 365,
                "right": 635,
                "bottom": 395,
                "confidence": 0.94,
            },
        ]

        copy_item = find_copy_menu_item(
            ocr_items,
            (1200, 900),
            anchor=(520, 260),
            require_menu_cluster=True,
        )
        chat_only = find_copy_menu_item(
            ocr_items[:1],
            (1200, 900),
            anchor=(520, 260),
            require_menu_cluster=True,
        )

        self.assertEqual(copy_item["text"], "复制")
        self.assertEqual(len(copy_item["menu_evidence"]), 3)
        self.assertIsNone(chat_only)

    def test_menu_frame_ocr_is_cropped_around_anchor_and_skips_chat_parser(self):
        full_screen = Image.new("RGB", (1200, 900), "white")

        class State:
            window_context = {"hwnd": 31415}
            window_context_validated = True
            events = []

            class Host:
                @staticmethod
                def capture_c2_window_context(_context, *, phase, label):
                    self.assertEqual(phase, "image_context_menu")
                    self.assertEqual(label, "vision_image_context_menu")
                    return {
                        "ok": True,
                        "image": full_screen.copy(),
                        "hwnd": 31415,
                        "capture_mode": "visible_screen",
                        "screen_origin": [0, 0],
                        "validation": {"reason": "window_context_confirmed"},
                    }

                @staticmethod
                def run_ocr(image):
                    self.assertLess(image.size[0], full_screen.size[0])
                    self.assertLess(image.size[1], full_screen.size[1])
                    return [{"text": "复制"}]

                @staticmethod
                def parse_messages_from_ocr(*_args, **_kwargs):
                    raise AssertionError("menu ROI must not parse chat messages")

            host = Host()

            def record(self, *_args, **_kwargs):
                return None

        result = _WindowFrame(State()).capture_frame(
            {
                "phase": "image_context_menu",
                "menu_anchor_screen": [900, 500],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["screen_origin"], [520, 80])
        self.assertEqual(result["messages"], [])
        result["image"].close()
        full_screen.close()

    def test_menu_click_uses_screen_port_without_window_reactivation(self):
        calls = []

        class State:
            class Host:
                @staticmethod
                def human_screen_click_in_bounds(x, y, *, bounds, action_name):
                    calls.append((x, y, list(bounds), action_name))
                    return {"ok": True}

                @staticmethod
                def human_window_image_click_in_bounds(*_args, **_kwargs):
                    raise AssertionError("menu click must not reactivate the window")

                @staticmethod
                def humanized_action_sleep(*_args):
                    return None

            host = Host()

            @staticmethod
            def ensure_window():
                return 31415

            @staticmethod
            def record(*_args, **_kwargs):
                return None

        _UiAction(State()).click_screen(
            710,
            420,
            bounds=[680, 400, 740, 440],
        )

        self.assertEqual(
            calls,
            [
                (
                    710,
                    420,
                    [680, 400, 740, 440],
                    "c2_vision_image_copy_menu_click",
                )
            ],
        )

    def test_delayed_clipboard_update_is_polled_after_one_copy_click(self):
        image = Image.new("RGB", (800, 600), "white")

        class Actions:
            right_click_count = 0
            copy_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                self.right_click_count += 1
                return {"screen_x": 500, "screen_y": 240}

            def click_screen(self, _x, _y, *, bounds):
                self.copy_click_count += 1
                self.bounds = bounds

        class Clipboard:
            values = [10, 10, 10, 11, 11]
            calls = 0

            def sequence_number(self):
                self.calls += 1
                return self.values.pop(0) if self.values else 11

            def read_current_bitmap(self):
                return image.crop((420, 180, 650, 320))

        actions = Actions()
        clipboard = Clipboard()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda context: {
                    "ok": True,
                    "image": image.copy(),
                    "image_size": image.size,
                    "messages": [],
                    "time_markers": [],
                    "ocr_items": [],
                    "screen_origin": [0, 0],
                }
            ),
            ui_action=actions,
            clipboard=clipboard,
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": [420, 180, 650, 320],
                    "side": "customer",
                    "anchor": {"x": 500, "y": 240},
                }
            ],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={
                "x": 620,
                "y": 320,
                "bounds": [600, 300, 650, 340],
            },
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
                    "image_physical_anchor": self.image_anchor(),
                    "clipboard_wait_timeout_seconds": 0.5,
                    "clipboard_poll_interval_seconds": 0.02,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(actions.right_click_count, 1)
        self.assertEqual(actions.copy_click_count, 1)
        self.assertGreaterEqual(clipboard.calls, 5)
        result["_ephemeral_clipboard_image"].release()
        image.close()

    def test_two_visible_images_are_reconfirmed_and_copied_by_requested_slot(self):
        image = Image.new("RGB", (800, 700), "white")

        class Actions:
            right_click_points = []
            right_click_bounds = []
            copy_click_count = 0

            def right_click(self, x, y, *, bounds):
                self.right_click_points.append((x, y))
                self.right_click_bounds.append(list(bounds))
                return {"screen_x": x, "screen_y": y}

            def click_screen(self, _x, _y, *, bounds):
                self.copy_click_count += 1
                self.bounds = bounds
                clipboard.sequence += 1

        class Clipboard:
            sequence = 20

            def sequence_number(self):
                return self.sequence

            def read_current_bitmap(self):
                return image.crop(
                    tuple(actions.right_click_bounds[-1])
                )

        clipboard = Clipboard()
        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda _context: {
                    "ok": True,
                    "image": image.copy(),
                    "image_size": image.size,
                    "messages": [],
                    "time_markers": [],
                    "ocr_items": [],
                    "screen_origin": [0, 0],
                }
            ),
            ui_action=actions,
            clipboard=clipboard,
        )
        bubbles = [
            {
                "bounds": [420, 140, 650, 280],
                "side": "customer",
                "anchor": {"x": 520, "y": 210},
            },
            {
                "bounds": [420, 390, 650, 560],
                "side": "customer",
                "anchor": {"x": 520, "y": 475},
            },
        ]
        results = []
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=bubbles,
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={
                "x": 620,
                "y": 320,
                "bounds": [600, 300, 650, 340],
            },
        ):
            for occurrence_index, bubble in enumerate(bubbles):
                results.append(
                    transaction.acquire_current_image_via_ports(
                        ports,
                        {
                            "sender_role": "customer",
                            "bubble_rect": bubble["bounds"],
                            "image_physical_anchor": self.image_anchor(
                                occurrence_index=occurrence_index,
                                occurrence_count=2,
                            ),
                        },
                    )
                )

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(actions.right_click_points, [(520, 210), (520, 475)])
        self.assertEqual(actions.copy_click_count, 2)
        for result in results:
            result["_ephemeral_clipboard_image"].release()
        image.close()

    def test_shifted_second_image_is_matched_by_identity_not_old_coordinates(self):
        initial = Image.new("RGB", (800, 700), "white")
        current = Image.new("RGB", (800, 700), "white")
        target_pattern = Image.new("RGB", (200, 150), "white")
        target_draw = ImageDraw.Draw(target_pattern)
        for offset in range(200):
            target_draw.line(
                [(offset, 0), (offset, 149)],
                fill=(offset, offset, offset),
            )
        replacement_pattern = Image.new("RGB", (200, 150), "white")
        replacement_draw = ImageDraw.Draw(replacement_pattern)
        for offset in range(200):
            value = 255 - offset
            replacement_draw.line(
                [(offset, 0), (offset, 149)],
                fill=(value, value, value),
            )
        old_bounds = [430, 400, 630, 550]
        shifted_bounds = [430, 220, 630, 370]
        initial.paste(target_pattern, (old_bounds[0], old_bounds[1]))
        current.paste(target_pattern, (shifted_bounds[0], shifted_bounds[1]))
        current.paste(replacement_pattern, (old_bounds[0], old_bounds[1]))
        initial_messages = [
            {
                "id": "text-before",
                "type": "text",
                "sender_role": "customer",
                "content": "原来的上文",
                "bubble_rect": [430, 330, 600, 370],
            },
            {
                "id": "text-after",
                "type": "text",
                "sender_role": "customer",
                "content": "原来的下文",
                "bubble_rect": [430, 570, 600, 610],
            },
        ]
        current_messages = [
            {
                "id": "text-before",
                "type": "text",
                "sender_role": "customer",
                "content": "原来的上文",
                "bubble_rect": [430, 150, 600, 190],
            },
            {
                "id": "new-text",
                "type": "text",
                "sender_role": "customer",
                "content": "处理期间新增消息",
                "bubble_rect": [430, 380, 620, 410],
            },
        ]
        expected = attach_image_physical_anchors(
            initial,
            [{"bounds": old_bounds, "side": "customer"}],
            initial_messages,
        )[0]["image_physical_anchor"]

        class Clipboard:
            sequence = 30

            def sequence_number(self):
                return self.sequence

            def read_current_bitmap(self):
                return target_pattern.copy()

        clipboard = Clipboard()
        right_click_calls: list[dict] = []
        click_screen_calls: list[dict] = []

        def right_click_host(hwnd, x, y, *, bounds, action_name):
            right_click_calls.append(
                {
                    "hwnd": hwnd,
                    "x": x,
                    "y": y,
                    "bounds": list(bounds),
                    "action_name": action_name,
                }
            )
            return {"ok": True, "screen_x": x, "screen_y": y}

        def click_screen_host(x, y, *, bounds, action_name):
            click_screen_calls.append(
                {
                    "x": x,
                    "y": y,
                    "bounds": list(bounds),
                    "action_name": action_name,
                }
            )
            clipboard.sequence += 1
            return {"ok": True}

        action_state = SimpleNamespace(
            ensure_window=lambda: 31415,
            host=SimpleNamespace(
                human_window_image_right_click_in_bounds=right_click_host,
                human_screen_click_in_bounds=click_screen_host,
                humanized_action_sleep=lambda *_args: None,
                dismiss_voice_transcribe_context_menu=lambda *_args, **_kwargs: {
                    "ok": True
                },
            ),
            record=lambda *_args, **_kwargs: None,
        )
        actions = _UiAction(action_state)
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda _context: {
                    "ok": True,
                    "image": current.copy(),
                    "image_size": current.size,
                    "messages": current_messages,
                    "time_markers": [],
                    "ocr_items": [],
                    "screen_origin": [0, 0],
                }
            ),
            ui_action=actions,
            clipboard=clipboard,
        )
        current_bubbles = [
            {
                "bounds": shifted_bounds,
                "side": "customer",
                "anchor": {"x": 530, "y": 295},
            },
            {
                "bounds": old_bounds,
                "side": "customer",
                "anchor": {"x": 530, "y": 475},
            },
        ]
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=current_bubbles,
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={
                "x": 620,
                "y": 320,
                "bounds": [600, 300, 650, 340],
            },
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": old_bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            right_click_calls,
            [
                {
                    "hwnd": 31415,
                    "x": 530,
                    "y": 295,
                    "bounds": shifted_bounds,
                    "action_name": "c2_vision_image_slot_context_right_click",
                }
            ],
        )
        self.assertEqual(len(click_screen_calls), 1)
        self.assertTrue(result["transaction"]["slot_identity_confirmed"])
        self.assertEqual(
            result["transaction"]["current_bubble_rect"],
            shifted_bounds,
        )
        result["_ephemeral_clipboard_image"].release()
        target_pattern.close()
        replacement_pattern.close()
        initial.close()
        current.close()

    def test_image_at_old_coordinates_is_not_clicked_when_identity_differs(self):
        initial = Image.new("RGB", (800, 700), "white")
        current = Image.new("RGB", (800, 700), "white")
        target_pattern = Image.new("RGB", (200, 150), "white")
        target_draw = ImageDraw.Draw(target_pattern)
        target_draw.polygon(
            [(0, 0), (199, 0), (0, 149)],
            fill=(20, 150, 220),
        )
        replacement_pattern = Image.new("RGB", (200, 150), "white")
        replacement_draw = ImageDraw.Draw(replacement_pattern)
        replacement_draw.polygon(
            [(199, 149), (199, 0), (0, 149)],
            fill=(220, 70, 30),
        )
        old_bounds = [430, 400, 630, 550]
        initial.paste(target_pattern, (old_bounds[0], old_bounds[1]))
        current.paste(replacement_pattern, (old_bounds[0], old_bounds[1]))
        expected = attach_image_physical_anchors(
            initial,
            [{"bounds": old_bounds, "side": "customer"}],
            [],
        )[0]["image_physical_anchor"]

        class Actions:
            right_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                self.right_click_count += 1
                return {}

        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda _context: {
                    "ok": True,
                    "image": current.copy(),
                    "image_size": current.size,
                    "messages": [],
                    "time_markers": [],
                }
            ),
            ui_action=actions,
            clipboard=SimpleNamespace(sequence_number=lambda: 40),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": old_bounds,
                    "side": "customer",
                    "anchor": {"x": 530, "y": 475},
                }
            ],
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": old_bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "image_bubble_slot_not_reconfirmed")
        self.assertEqual(result["action_phase"], "not_attempted")
        self.assertEqual(actions.right_click_count, 0)
        target_pattern.close()
        replacement_pattern.close()
        initial.close()
        current.close()

    def test_clipboard_fingerprint_retry_reanchors_once_then_succeeds(self):
        frame_image = Image.new("RGB", (800, 600), "white")
        bubble_image = Image.new("RGB", (200, 140), (30, 120, 210))
        draw = ImageDraw.Draw(bubble_image)
        draw.rectangle([35, 25, 160, 110], fill=(235, 175, 35))
        bounds = [430, 180, 630, 320]
        frame_image.paste(bubble_image, (bounds[0], bounds[1]))
        expected = attach_image_physical_anchors(
            frame_image,
            [{"bounds": bounds, "side": "customer"}],
            [],
        )[0]["image_physical_anchor"]
        wrong_image = Image.new("RGB", (200, 140), (210, 40, 60))

        class Frames:
            candidate_count = 0

            def capture_frame(self, context):
                if context.get("phase") == "image_candidate":
                    self.candidate_count += 1
                    return {
                        "ok": True,
                        "image": frame_image.copy(),
                        "image_size": frame_image.size,
                        "messages": [],
                        "time_markers": [],
                    }
                return {
                    "ok": True,
                    "image": frame_image.copy(),
                    "image_size": frame_image.size,
                    "ocr_items": [{"text": "复制"}],
                    "screen_origin": [0, 0],
                }

        class Actions:
            right_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.right_click_count += 1
                return {"screen_x": 530, "screen_y": 250}

            def click_screen(self, _x, _y, *, bounds):
                return None

        class Clipboard:
            sequences = iter([10, 11, 11, 20, 21, 21])
            reads = 0

            def sequence_number(self):
                return next(self.sequences)

            def read_current_bitmap(self):
                self.reads += 1
                return (
                    wrong_image.copy()
                    if self.reads == 1
                    else bubble_image.copy()
                )

        frames = Frames()
        actions = Actions()
        clipboard = Clipboard()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=frames,
            ui_action=actions,
            clipboard=clipboard,
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": bounds,
                    "side": "customer",
                    "anchor": {"x": 530, "y": 250},
                }
            ],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={
                "x": 620,
                "y": 320,
                "bounds": [600, 300, 650, 340],
            },
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(frames.candidate_count, 2)
        self.assertEqual(actions.right_click_count, 2)
        self.assertEqual(clipboard.reads, 2)
        self.assertTrue(
            result["transaction"]["clipboard_image_matches_target"]
        )
        self.assertEqual(
            result["transaction"]["clipboard_fingerprint_retry_count"],
            1,
        )
        self.assertTrue(
            result["transaction"][
                "clipboard_fingerprint_first_attempt_mismatch"
            ]
        )
        result["_ephemeral_clipboard_image"].release()
        wrong_image.close()
        bubble_image.close()
        frame_image.close()

    def test_clipboard_fingerprint_mismatch_twice_never_reaches_vision(self):
        frame_image = Image.new("RGB", (800, 600), "white")
        bubble_image = Image.new("RGB", (200, 140), (30, 120, 210))
        bounds = [430, 180, 630, 320]
        frame_image.paste(bubble_image, (bounds[0], bounds[1]))
        expected = attach_image_physical_anchors(
            frame_image,
            [{"bounds": bounds, "side": "customer"}],
            [],
        )[0]["image_physical_anchor"]
        wrong_image = Image.new("RGB", (200, 140), (210, 40, 60))

        class Frames:
            def capture_frame(self, context):
                return {
                    "ok": True,
                    "image": frame_image.copy(),
                    "image_size": frame_image.size,
                    "messages": [],
                    "time_markers": [],
                    "ocr_items": (
                        [{"text": "复制"}]
                        if context.get("phase") == "image_context_menu"
                        else []
                    ),
                    "screen_origin": [0, 0],
                }

        class Actions:
            right_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.right_click_count += 1
                return {"screen_x": 530, "screen_y": 250}

            def click_screen(self, _x, _y, *, bounds):
                return None

        class Clipboard:
            sequences = iter([10, 11, 11, 20, 21, 21])

            def sequence_number(self):
                return next(self.sequences)

            def read_current_bitmap(self):
                return wrong_image.copy()

        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=Frames(),
            ui_action=actions,
            clipboard=Clipboard(),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": bounds,
                    "side": "customer",
                    "anchor": {"x": 530, "y": 250},
                }
            ],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={
                "x": 620,
                "y": 320,
                "bounds": [600, 300, 650, 340],
            },
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "clipboard_image_fingerprint_mismatch",
        )
        self.assertEqual(result["action_phase"], "trigger_attempted")
        self.assertEqual(actions.right_click_count, 2)
        self.assertFalse(
            result["transaction"]["clipboard_image_matches_target"]
        )
        self.assertNotIn("_ephemeral_clipboard_image", result)
        wrong_image.close()
        bubble_image.close()
        frame_image.close()

    def test_upstream_clipboard_fingerprint_allows_scaled_compression(self):
        source = Image.new("RGB", (160, 120), (20, 120, 210))
        draw = ImageDraw.Draw(source)
        draw.rectangle([35, 25, 120, 95], fill=(230, 180, 40))
        buffer = io.BytesIO()
        source.resize((320, 240), Image.Resampling.LANCZOS).save(
            buffer,
            format="JPEG",
            quality=82,
        )
        buffer.seek(0)
        with Image.open(buffer) as compressed:
            compressed.load()
            self.assertTrue(
                visual_fingerprint.fingerprints_match(
                    visual_fingerprint.image_fingerprint(source),
                    visual_fingerprint.image_fingerprint(compressed),
                )
            )
        source.close()

    def test_identical_image_is_not_clicked_when_duplicate_group_shrinks(self):
        initial = Image.new("RGB", (800, 700), "white")
        current = Image.new("RGB", (800, 700), "white")
        first_bounds = [430, 180, 630, 320]
        second_bounds = [430, 390, 630, 530]
        initial_candidates = attach_image_physical_anchors(
            initial,
            [
                {"bounds": first_bounds, "side": "customer"},
                {"bounds": second_bounds, "side": "customer"},
            ],
            [],
        )
        expected = initial_candidates[0]["image_physical_anchor"]
        self.assertEqual(expected["occurrence_index"], 0)
        self.assertEqual(expected["occurrence_count"], 2)

        class Actions:
            right_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                self.right_click_count += 1
                return {}

        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda _context: {
                    "ok": True,
                    "image": current.copy(),
                    "image_size": current.size,
                    "messages": [],
                    "time_markers": [],
                }
            ),
            ui_action=actions,
            clipboard=SimpleNamespace(sequence_number=lambda: 50),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": second_bounds,
                    "side": "customer",
                    "anchor": {"x": 530, "y": 460},
                }
            ],
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": first_bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "image_bubble_slot_not_reconfirmed")
        self.assertEqual(result["action_phase"], "not_attempted")
        self.assertEqual(actions.right_click_count, 0)
        initial.close()
        current.close()

    def test_single_fingerprint_match_without_neighbor_match_is_not_clicked(self):
        current = Image.new("RGB", (800, 700), "white")
        bounds = [430, 220, 630, 360]
        expected = {
            "sender_role": "customer",
            "preceding_stable_message": "message_semantic_expected",
            "following_stable_message": "",
            "bubble_visual_fingerprint": "dhash64:0000000000000000",
            "occurrence_index": 0,
            "occurrence_count": 1,
        }
        current_candidate = {
            "bounds": bounds,
            "side": "customer",
            "anchor": {"x": 530, "y": 290},
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": "message_semantic_other",
                "following_stable_message": "",
                "bubble_visual_fingerprint": "dhash64:0000000000000000",
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
        }

        class Actions:
            right_click_count = 0

            def right_click(self, _x, _y, *, bounds):
                self.bounds = bounds
                self.right_click_count += 1
                return {}

        actions = Actions()
        ports = VisionHostPorts(
            rpa_lease=SimpleNamespace(
                lease=lambda *_args, **_kwargs: nullcontext()
            ),
            conversation_target=SimpleNamespace(
                confirm_target=lambda _context: {"ok": True}
            ),
            window_frame=SimpleNamespace(
                capture_frame=lambda _context: {
                    "ok": True,
                    "image": current.copy(),
                    "image_size": current.size,
                    "messages": [],
                    "time_markers": [],
                }
            ),
            ui_action=actions,
            clipboard=SimpleNamespace(sequence_number=lambda: 60),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[current_candidate],
        ), patch.object(
            transaction,
            "attach_image_physical_anchors",
            return_value=[current_candidate],
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": bounds,
                    "image_physical_anchor": expected,
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "image_bubble_slot_not_reconfirmed")
        self.assertEqual(result["action_phase"], "not_attempted")
        self.assertEqual(actions.right_click_count, 0)
        current.close()

    def test_capability_preflight_reports_missing_key_without_touching_plugin(self):
        status = vision_configuration_status()

        self.assertFalse(status["ready"])
        self.assertEqual(
            status["missing_configuration"],
            ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY_OR_ANTHROPIC_AUTH_TOKEN"],
        )
        self.assertIsNone(status["config"])

    def test_non_same_row_role_is_ignored_without_vision(self):
        result = process_image_slot(
            observation=self.image_observation(role_source="unknown"),
            remark_code="CJTEST01",
            session_key="wx-row-1",
            config={"customer_image_understanding": {"enabled": True}},
        )

        self.assertEqual(result["state"], "ignored")
        self.assertEqual(result["reason"], "image_same_row_avatar_unconfirmed")
        self.assertFalse(result["diagnostics"]["image_persisted"])

    def test_blocking_provider_process_is_killed_when_authorization_is_cancelled(self):
        released = threading.Event()

        class FakeProcess:
            returncode = None
            killed = False

            def communicate(self, *, input):
                self.input = input
                released.wait(timeout=3)
                self.returncode = -9 if self.killed else 0
                return "", ""

            def kill(self):
                self.killed = True
                released.set()

        process = FakeProcess()
        checks = {"count": 0}

        def cancelled():
            checks["count"] += 1
            return checks["count"] >= 2

        image = SimpleNamespace(
            image_bytes=b"memory-only-image",
            mime_type="image/png",
            width=20,
            height=10,
        )
        provider = _CancellableVisionProvider(cancelled)
        with patch(
            "chejin_worker_client.omniauto_vision.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaisesRegex(
                VisionCancelledError,
                "vision_cancelled_during_provider",
            ):
                provider.understand(
                    {
                        "image": image,
                        "config": {
                            "customer_image_understanding": {
                                "enabled": True,
                                "timeout_seconds": 30,
                            }
                        },
                        "customer_text": "图片",
                        "message_id": "image-1",
                    }
                )

        self.assertTrue(process.killed)
        self.assertTrue(released.is_set())

    def test_cancel_after_copy_never_returns_not_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "image-action.json"
            initialize_action_journal(
                journal_path,
                action_kind="image",
                transaction_id="image-cancel-after-copy",
                conversation_id="conversation-image-cancel",
                items=[
                    {
                        "source_message_key": "image-source-1",
                        "physical_anchor_keys": ["image-anchor-1"],
                    }
                ],
            )

            class FakePlugin:
                def __init__(self, *, ports, config):
                    pass

                def run(self, context):
                    callback = context["action_journal_update"]
                    callback(
                        action_phase="trigger_attempted",
                        business_state=None,
                        business_result_confirmed=False,
                    )
                    callback(
                        action_phase="confirmed",
                        business_state="clipboard_confirmed",
                        business_result_confirmed=False,
                    )
                    raise VisionCancelledError(
                        "vision_cancelled_during_provider"
                    )

            with patch(
                "apps.wechat_ai_customer_service.optional_plugins."
                "vision.plugin.BuiltinVisionPlugin",
                FakePlugin,
            ):
                result = process_image_slot(
                    observation=self.image_observation(),
                    remark_code="CJTEST01",
                    session_key="wx-row-1",
                    window_context=self.window_context(),
                    config={
                        "customer_image_understanding": {
                            "enabled": True
                        }
                    },
                    action_journal_path=journal_path,
                    source_message_key="image-source-1",
                )

            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(result["action_phase"], "confirmed")
            item = read_action_journal(journal_path)["items"][
                "image-source-1"
            ]
            self.assertEqual(item["action_phase"], "confirmed")
            self.assertEqual(item["business_state"], "failed")
            self.assertFalse(item["business_result_confirmed"])

    def test_worker_adapter_calls_official_plugin_once_with_omniauto_role(self):
        calls: list[dict] = []

        class FakePlugin:
            def __init__(self, *, ports, config):
                self.ports = ports
                self.config = config

            def run(self, context):
                calls.append(context)
                return {
                    "applied": True,
                    "reason": "vision_ready",
                    "customer_image_understanding": {
                        "schema_version": 1,
                        "vision_summary": "客户发来一张车辆外观图",
                    },
                    "visual_bridge_input": {"summary": "车辆外观图"},
                    "clipboard_transaction": {"image_sha256": "a" * 64},
                }

        with patch(
            "apps.wechat_ai_customer_service.optional_plugins.vision.plugin.BuiltinVisionPlugin",
            FakePlugin,
        ):
            result = process_image_slot(
                observation=self.image_observation(role="customer"),
                remark_code="CJTEST01",
                session_key="wx-row-1",
                window_context=self.window_context(),
                config={"customer_image_understanding": {"enabled": True, "api_key": "unit-only"}},
            )

        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sender_role"], "customer")
        self.assertEqual(calls[0]["bubble_rect"], [420, 180, 650, 320])
        self.assertFalse(result["diagnostics"]["image_persisted"])
        self.assertEqual(
            [item["stage"] for item in result["diagnostics"]["events"]],
            ["vision_preflight", "vision_provider"],
        )

    def test_vision_uses_sidecar_window_context_without_selecting_window(self):
        state = _VisionHostState(
            "vision-window-context",
            window_context=self.window_context(),
        )
        with patch.object(
            state.host,
            "validate_c2_window_context",
            return_value={
                "ok": True,
                "reason": "window_context_confirmed",
                "hwnd": 31415,
            },
        ) as validator, patch.object(
            state.host,
            "ensure_visible_wechat_window",
            side_effect=AssertionError("Vision must not search for a window"),
        ) as search, patch.object(
            state.host,
            "select_primary_visible_main_window",
            side_effect=AssertionError("Vision must not select a window"),
        ) as select:
            self.assertEqual(state.ensure_window(), 31415)
            self.assertEqual(state.ensure_window(), 31415)

        validator.assert_called_once_with(self.window_context())
        search.assert_not_called()
        select.assert_not_called()

    def test_vision_frame_uses_only_sidecar_context_capture_entry(self):
        state = _VisionHostState(
            "vision-window-capture",
            window_context=self.window_context(),
        )
        frame = _WindowFrame(state)
        image = Image.new("RGB", (800, 600), "white")
        capture_result = {
            "ok": True,
            "image": image,
            "hwnd": 31415,
            "capture_mode": "wechat_window_exact_hwnd",
            "validation": {
                "ok": True,
                "reason": "window_context_confirmed",
                "hwnd": 31415,
            },
        }
        with patch.object(
            state.host,
            "capture_c2_window_context",
            return_value=capture_result,
        ) as capture, patch.object(
            state.host,
            "capture_wechat",
            side_effect=AssertionError("Vision must not call raw capture"),
        ) as raw_capture, patch.object(
            state.host,
            "capture_wechat_window_visible_screen",
            side_effect=AssertionError("Vision must not call fallback capture"),
        ) as fallback_capture, patch.object(
            state.host,
            "run_ocr",
            return_value=[],
        ), patch.object(
            state.host,
            "parse_messages_from_ocr",
            return_value=[],
        ):
            result = frame.capture_frame(
                {
                    "phase": "image_candidate",
                    "remark_code": "CJTEST01",
                }
            )

        self.assertTrue(result["ok"])
        capture.assert_called_once_with(
            self.window_context(),
            phase="image_candidate",
            label="vision_image_candidate",
        )
        raw_capture.assert_not_called()
        fallback_capture.assert_not_called()
        image.close()

    def test_sidecar_window_context_binds_exact_selected_hwnd(self):
        probe = {
            "selected_main_window": {
                "hwnd": 31415,
                "pid": 2718,
                "class_name": "WeChatMainWndForPC",
            }
        }
        with patch.object(
            wechat_win32_ocr_sidecar,
            "get_window_geometry",
            return_value={
                "left": 10,
                "top": 20,
                "right": 1010,
                "bottom": 820,
                "width": 1000,
                "height": 800,
            },
        ), patch.object(
            wechat_win32_ocr_sidecar,
            "win32gui",
            SimpleNamespace(
                IsWindow=lambda _hwnd: True,
                IsWindowVisible=lambda _hwnd: True,
            ),
        ), patch.object(
            wechat_win32_ocr_sidecar,
            "probe_wechat_windows",
            return_value={
                "windows": [
                    {
                        "hwnd": 31415,
                        "pid": 2718,
                        "class_name": "WeChatMainWndForPC",
                    }
                ]
            },
        ):
            context = wechat_win32_ocr_sidecar.build_c2_window_context(
                31415,
                probe,
            )
            validation = (
                wechat_win32_ocr_sidecar.validate_c2_window_context(
                    context
                )
            )

        self.assertEqual(context["hwnd"], 31415)
        self.assertEqual(context["source"], "sidecar_selected_main_window")
        self.assertTrue(validation["ok"])
        self.assertEqual(validation["hwnd"], 31415)

    def test_sidecar_context_capture_never_selects_another_window(self):
        image = Image.new("RGB", (800, 600), "white")
        with patch.object(
            wechat_win32_ocr_sidecar,
            "validate_c2_window_context",
            return_value={
                "ok": True,
                "reason": "window_context_confirmed",
                "hwnd": 31415,
            },
        ), patch.object(
            wechat_win32_ocr_sidecar,
            "capture_wechat",
            return_value=(image, None),
        ) as capture, patch.object(
            wechat_win32_ocr_sidecar,
            "win32gui",
            SimpleNamespace(GetWindowRect=lambda _hwnd: (100, 200, 900, 800)),
        ), patch.object(
            wechat_win32_ocr_sidecar,
            "ensure_visible_wechat_window",
            side_effect=AssertionError("must not search for another window"),
        ) as search, patch.object(
            wechat_win32_ocr_sidecar,
            "select_primary_visible_main_window",
            side_effect=AssertionError("must not select another window"),
        ) as select:
            result = wechat_win32_ocr_sidecar.capture_c2_window_context(
                self.window_context(),
                phase="image_candidate",
                label="vision_image_candidate",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["hwnd"], 31415)
        capture.assert_called_once_with(
            31415,
            artifact_dir=None,
            label="vision_image_candidate",
        )
        search.assert_not_called()
        select.assert_not_called()
        image.close()

    def test_missing_sidecar_window_context_pauses_before_plugin(self):
        class ForbiddenPlugin:
            def __init__(self, **_kwargs):
                raise AssertionError("plugin must not start without C2 window context")

        with patch(
            "apps.wechat_ai_customer_service.optional_plugins."
            "vision.plugin.BuiltinVisionPlugin",
            ForbiddenPlugin,
        ):
            result = process_image_slot(
                observation=self.image_observation(),
                remark_code="CJTEST01",
                session_key="wx-row-1",
                config={
                    "customer_image_understanding": {
                        "enabled": True,
                        "api_key": "unit-only",
                    }
                },
            )

        self.assertEqual(result["state"], "capability_paused")
        self.assertEqual(result["reason"], "vision_window_context_missing")
        self.assertEqual(result["action_phase"], "not_attempted")

    def test_strict_adapter_disables_all_legacy_vision_entries(self):
        from apps.wechat_ai_customer_service.optional_plugins.vision.plugin import (
            BuiltinVisionPlugin,
        )

        plugin = BuiltinVisionPlugin(config={"_chejin_c2_strict_adapter": True})

        for call in (
            lambda: plugin.observe_current_surface({}),
            lambda: plugin.capture_self_context({}),
            lambda: plugin.invoke("image-save", {}),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "CHEJIN_C2_LEGACY_VISION_ENTRY_DISABLED",
            ):
                call()

    def test_frame_fingerprint_is_stable_and_never_needs_a_path(self):
        first = Image.new("RGB", (160, 90), "white")
        same = Image.new("RGB", (160, 90), "white")
        different = Image.new("RGB", (160, 90), "black")
        try:
            self.assertEqual(_frame_fingerprint(first), _frame_fingerprint(same))
            self.assertNotEqual(_frame_fingerprint(first), _frame_fingerprint(different))
        finally:
            first.close()
            same.close()
            different.close()

    def test_encoded_memory_image_is_validated_resized_and_normalized_in_memory(self):
        from apps.wechat_ai_customer_service.optional_plugins.vision.clipboard_payload import (
            ephemeral_image_from_memory,
        )

        source = Image.new("RGB", (4096, 512), "white")
        encoded = io.BytesIO()
        try:
            source.save(encoded, format="JPEG", quality=90)
            payload = ephemeral_image_from_memory(encoded.getvalue(), mime_type="image/jpeg")
        finally:
            source.close()
            encoded.close()

        self.assertIsNotNone(payload)
        try:
            self.assertEqual(payload.mime_type, "image/png")
            self.assertLessEqual(max(payload.width, payload.height), 2048)
            self.assertTrue(bytes(payload.image_bytes).startswith(b"\x89PNG\r\n\x1a\n"))
        finally:
            payload.release()

    def test_encoded_memory_image_rejects_non_allowlisted_format(self):
        from apps.wechat_ai_customer_service.optional_plugins.vision.clipboard_payload import (
            ephemeral_image_from_memory,
        )

        source = Image.new("RGB", (32, 32), "white")
        encoded = io.BytesIO()
        try:
            source.save(encoded, format="GIF")
            payload = ephemeral_image_from_memory(encoded.getvalue(), mime_type="image/gif")
        finally:
            source.close()
            encoded.close()

        self.assertIsNone(payload)

    def test_menu_ocr_evidence_drops_chat_text_and_keeps_allowlisted_menu_tokens(self):
        evidence = _menu_ocr_evidence(
            [
                {"text": "复制", "bounds": [600, 220, 660, 250]},
                {"text": "客户身份证号码和聊天正文", "bounds": [420, 180, 710, 210]},
            ]
        )

        self.assertEqual(evidence, [{"token": "复制", "bounds": [600, 220, 660, 250]}])

    def test_persisted_projection_drops_runtime_image_fields(self):
        projected = apply_image_terminal_result(
            self.image_observation(),
            {
                "state": "completed",
                "customer_image_understanding": {
                    "schema_version": 1,
                    "vision_summary": "车辆外观图",
                    "image_local_path": "C:\\temp\\raw.png",
                    "image_bytes": "not-allowed",
                    "source_messages": [{"bubble_rect": [1, 2, 3, 4]}],
                    "local_visual_profile": {"crop": "not-allowed"},
                    "audit": {
                        "latency_ms": 15,
                        "provider_response_text": "raw-provider-output",
                        "retry_response_diagnostics": {"body": "raw-provider-output"},
                    },
                },
                "visual_bridge_input": {
                    "present": True,
                    "vision_summary": "车辆外观图",
                    "asset_id": "not-allowed",
                    "vehicle_image_retrieval": {
                        "matched": True,
                        "candidates": [{"product_name": "测试车", "picture_ref": "C:\\raw.png"}],
                    },
                },
                "transaction": {"image_sha256": "b" * 64},
            },
        )

        understanding = projected["customer_image_understanding"]
        self.assertNotIn("image_local_path", understanding)
        self.assertNotIn("image_bytes", understanding)
        self.assertNotIn("source_messages", understanding)
        self.assertNotIn("local_visual_profile", understanding)
        self.assertNotIn("provider_response_text", understanding["audit"])
        self.assertNotIn("retry_response_diagnostics", understanding["audit"])
        self.assertNotIn("asset_id", projected["visual_bridge_input"])
        self.assertNotIn("picture_ref", str(projected["visual_bridge_input"]))
        self.assertEqual(understanding["audit"]["image_sha256"], "b" * 64)

    def test_structural_image_identity_does_not_depend_on_screen_coordinates(self):
        from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
        from apps.wechat_ai_customer_service.optional_plugins.vision.capture import surface

        screenshot = Image.new("RGB", (1000, 800), "white")

        def envelope(bounds):
            return [{
                "id": "vision-side-temporary",
                "message_id": "vision-side-temporary",
                "type": "image",
                "sender": "self",
                "sender_role": "self",
                "time": "10:10",
                "bubble_rect": bounds,
                "_vision_preceding_text_id": "text-before",
                "_vision_following_text_id": "text-after",
            }]

        try:
            with patch.object(surface, "visual_image_messages_from_current_surface", return_value=envelope([410, 180, 650, 320])), patch.object(
                sidecar,
                "message_row_avatar_role_details",
                return_value={"role": "customer", "source": "same_row_avatar"},
            ):
                first = sidecar.merge_structural_image_messages(screenshot, [], [], target="CJTEST01")[0]
            with patch.object(surface, "visual_image_messages_from_current_surface", return_value=envelope([410, 230, 650, 370])), patch.object(
                sidecar,
                "message_row_avatar_role_details",
                return_value={"role": "customer", "source": "same_row_avatar"},
            ):
                second = sidecar.merge_structural_image_messages(screenshot, [], [], target="CJTEST01")[0]
        finally:
            screenshot.close()

        self.assertEqual(first["sender_role"], "customer")
        self.assertEqual(first["canonical_visual_id"], second["canonical_visual_id"])
        self.assertNotEqual(first["bubble_rect"], second["bubble_rect"])

    def test_structural_image_bounds_exclude_avatar_before_shared_role_resolution(self):
        screenshot = Image.new("RGB", (974, 853), (242, 242, 242))
        draw = ImageDraw.Draw(screenshot)

        # Reproduce the Windows frame shape: a textured customer avatar is
        # visually joined to the adjacent image by a narrow antialiased bridge.
        for y in range(391, 436, 5):
            for x in range(408, 453, 5):
                tone = 50 if ((x + y) // 5) % 2 else 205
                draw.rectangle((x, y, x + 4, y + 4), fill=(tone, 110, 170))
        for y in range(390, 654, 8):
            for x in range(470, 670, 8):
                tone = 35 if ((x + y) // 8) % 2 else 220
                draw.rectangle((x, y, x + 7, y + 7), fill=(tone, 150, 80))
        draw.rectangle((451, 402, 472, 414), fill=(70, 120, 170))

        try:
            candidates = detect_visual_image_bubbles(
                screenshot,
                messages=[],
                max_images=8,
                side_filter="all",
            )
            self.assertTrue(candidates)
            candidate = candidates[0]
            self.assertEqual(candidate["side"], "customer")
            self.assertGreaterEqual(candidate["bounds"][0], 463)
            self.assertIn(
                "avatar_column_excluded_from_media_bounds",
                candidate["structure_evidence"],
            )

            messages = wechat_win32_ocr_sidecar.merge_structural_image_messages(
                screenshot,
                [],
                [],
                target="CJTEST01",
            )
        finally:
            screenshot.close()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_role"], "customer")
        self.assertEqual(messages[0]["avatar_alignment"]["role"], "customer")
        observations = wechat_win32_ocr_sidecar.build_message_observations_v3(messages)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["sender_role"], "customer")
        self.assertEqual(observations[0]["sender_role_source"], "same_row_avatar")
        self.assertNotIn("contract_errors", observations[0])

    def test_structural_self_image_bounds_exclude_avatar_before_shared_role_resolution(self):
        screenshot = Image.new("RGB", (974, 853), (242, 242, 242))
        draw = ImageDraw.Draw(screenshot)

        for y in range(391, 436, 5):
            for x in range(895, 940, 5):
                tone = 50 if ((x + y) // 5) % 2 else 205
                draw.rectangle((x, y, x + 4, y + 4), fill=(tone, 110, 170))
        for y in range(390, 654, 8):
            for x in range(680, 875, 8):
                tone = 35 if ((x + y) // 8) % 2 else 220
                draw.rectangle((x, y, x + 7, y + 7), fill=(tone, 150, 80))
        draw.rectangle((872, 402, 897, 414), fill=(70, 120, 170))

        try:
            candidates = detect_visual_image_bubbles(
                screenshot,
                messages=[],
                max_images=8,
                side_filter="all",
            )
            self.assertTrue(candidates)
            candidate = candidates[0]
            self.assertEqual(candidate["side"], "self")
            self.assertLessEqual(candidate["bounds"][2], 885)
            self.assertIn(
                "avatar_column_excluded_from_media_bounds",
                candidate["structure_evidence"],
            )

            messages = wechat_win32_ocr_sidecar.merge_structural_image_messages(
                screenshot,
                [],
                [],
                target="CJTEST01",
            )
        finally:
            screenshot.close()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_role"], "self")
        self.assertEqual(messages[0]["avatar_alignment"]["role"], "self")
        observations = wechat_win32_ocr_sidecar.build_message_observations_v3(messages)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["sender_role"], "self")
        self.assertEqual(observations[0]["sender_role_source"], "same_row_avatar")
        self.assertNotIn("contract_errors", observations[0])


if __name__ == "__main__":
    unittest.main()
