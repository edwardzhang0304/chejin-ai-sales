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

from PIL import Image

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-vision-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.omniauto_vision import (
    DEFAULT_VISION_BASE_URL,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_PROVIDER,
    DEFAULT_VISION_REQUEST_STYLE,
    VisionCancelledError,
    _CancellableVisionProvider,
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
from apps.wechat_ai_customer_service.optional_plugins.vision.ports import VisionHostPorts


class C2VisionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vision_env_names = (
            "CUSTOMER_IMAGE_UNDERSTANDING_PROVIDER",
            "CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL",
            "CUSTOMER_IMAGE_UNDERSTANDING_MODEL",
            "CUSTOMER_IMAGE_UNDERSTANDING_REQUEST_STYLE",
            "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY",
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
    def image_observation(*, role: str = "customer", role_source: str = "same_row_avatar") -> dict:
        image_anchor = {
            "sender_role": role,
            "preceding_stable_message": "before-image",
            "following_stable_message": "after-image",
            "occurrence_index": 0,
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
                    return {"ok": True, "image": image.copy(), "ocr_items": [{"text": "复制"}]}
                self.candidate_image = image.copy()
                return {"ok": True, "image": self.candidate_image, "image_size": image.size, "messages": [], "time_markers": []}

        class Actions:
            def right_click(self, _x, _y):
                return None

            def click(self, _x, _y):
                return None

        class Clipboard:
            sequence = iter([10, 11])

            def sequence_number(self):
                return next(self.sequence)

            def read_current_bitmap(self):
                return image.copy()

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
            return_value=[{"bounds": [420, 180, 650, 320], "anchor": {"x": 500, "y": 240}}],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320},
        ), patch.object(
            transaction,
            "ephemeral_image_from_memory",
            return_value=SimpleNamespace(image_bytes=b"image", width=20, height=10),
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(frames.calls, 2)
        self.assertIn("candidate_frame", target.context)
        self.assertIs(target.context["candidate_frame"]["image"], frames.candidate_image)

    def test_copy_journal_is_persisted_before_physical_click(self):
        image = Image.new("RGB", (800, 600), "white")
        events: list[str] = []

        class Actions:
            def right_click(self, _x, _y):
                return None

            def click(self, _x, _y):
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
                }
            ),
            ui_action=Actions(),
            clipboard=SimpleNamespace(
                sequence_number=iter([10, 11]).__next__,
                read_current_bitmap=lambda: image.copy(),
            ),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[
                {
                    "bounds": [420, 180, 650, 320],
                    "anchor": {"x": 500, "y": 240},
                }
            ],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320},
        ), patch.object(
            transaction,
            "ephemeral_image_from_memory",
            return_value=SimpleNamespace(
                image_bytes=b"image",
                width=20,
                height=10,
            ),
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
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

    def test_copy_click_exception_always_dismisses_context_menu(self):
        image = Image.new("RGB", (800, 600), "white")

        class Actions:
            dismissed = 0

            def right_click(self, _x, _y):
                return None

            def click(self, _x, _y):
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
                }
            ),
            ui_action=actions,
            clipboard=SimpleNamespace(sequence_number=lambda: 10),
        )
        with patch.object(
            transaction,
            "detect_visual_image_bubbles",
            return_value=[{"bounds": [420, 180, 650, 320], "anchor": {"x": 500, "y": 240}}],
        ), patch.object(
            transaction,
            "find_copy_menu_item",
            return_value={"x": 620, "y": 320},
        ):
            result = transaction.acquire_current_image_via_ports(
                ports,
                {
                    "sender_role": "customer",
                    "bubble_rect": [420, 180, 650, 320],
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


if __name__ == "__main__":
    unittest.main()
