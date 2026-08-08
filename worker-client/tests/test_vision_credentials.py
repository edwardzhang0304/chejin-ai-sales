from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chejin_worker_client.vision_credentials import (
    OFFICIAL_VISION_BASE_URL,
    OFFICIAL_VISION_MODEL,
    OFFICIAL_VISION_PROVIDER,
    OFFICIAL_VISION_REQUEST_STYLE,
    resolve_vision_api_key,
    resolve_vision_runtime_settings,
    probe_official_vision_provider,
    vision_credential_status,
)


class VisionCredentialsTest(unittest.TestCase):
    def test_development_build_allows_dedicated_environment_override(self):
        with patch.dict(
            os.environ,
            {
                "CHEJIN_BUILD_KIND": "development",
                "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY": "dev-unit-key",
                "CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL": "https://dev.example/v1",
            },
            clear=False,
        ):
            self.assertEqual(resolve_vision_api_key(), "dev-unit-key")
            self.assertEqual(
                resolve_vision_runtime_settings()["base_url"],
                "https://dev.example/v1",
            )

    def test_official_build_uses_embedded_key_and_locks_provider_tuple(self):
        embedded_key = "official-unit-key-never-log"
        with tempfile.TemporaryDirectory() as temp:
            credential_path = Path(temp) / "vision-runtime.json"
            credential_path.write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CHEJIN_BUILD_KIND": "official",
                    "CHEJIN_VISION_CREDENTIAL_PATH": str(credential_path),
                    "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY": "attacker-key",
                    "CUSTOMER_IMAGE_UNDERSTANDING_PROVIDER": "attacker-provider",
                    "CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL": "https://attacker.invalid/v1",
                    "CUSTOMER_IMAGE_UNDERSTANDING_MODEL": "attacker-model",
                    "CUSTOMER_IMAGE_UNDERSTANDING_REQUEST_STYLE": "attacker-style",
                },
                clear=False,
            ):
                self.assertEqual(resolve_vision_api_key(), embedded_key)
                self.assertEqual(
                    resolve_vision_runtime_settings(),
                    {
                        "provider": OFFICIAL_VISION_PROVIDER,
                        "base_url": OFFICIAL_VISION_BASE_URL,
                        "model": OFFICIAL_VISION_MODEL,
                        "request_style": OFFICIAL_VISION_REQUEST_STYLE,
                    },
                )

    def test_fast_uat_build_uses_same_locked_vision_configuration(self):
        embedded_key = "fast-uat-unit-key-never-log"
        with tempfile.TemporaryDirectory() as temp:
            credential_path = Path(temp) / "vision-runtime.json"
            credential_path.write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CHEJIN_BUILD_KIND": "debug_uat_locked",
                    "CHEJIN_VISION_CREDENTIAL_PATH": str(credential_path),
                    "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY": "attacker-key",
                    "CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL": "https://attacker.invalid/v1",
                },
                clear=False,
            ):
                self.assertEqual(resolve_vision_api_key(), embedded_key)
                self.assertEqual(
                    resolve_vision_runtime_settings(),
                    {
                        "provider": OFFICIAL_VISION_PROVIDER,
                        "base_url": OFFICIAL_VISION_BASE_URL,
                        "model": OFFICIAL_VISION_MODEL,
                        "request_style": OFFICIAL_VISION_REQUEST_STYLE,
                    },
                )
                self.assertTrue(vision_credential_status()["configuration_locked"])

    def test_status_never_contains_key(self):
        embedded_key = "official-unit-key-never-export"
        with tempfile.TemporaryDirectory() as temp:
            credential_path = Path(temp) / "vision-runtime.json"
            credential_path.write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CHEJIN_BUILD_KIND": "official",
                    "CHEJIN_VISION_CREDENTIAL_PATH": str(credential_path),
                },
                clear=False,
            ):
                payload = vision_credential_status()

        self.assertTrue(payload["configured"])
        self.assertTrue(payload["configuration_locked"])
        self.assertEqual(payload["credential_source"], "embedded")
        self.assertNotIn(embedded_key, json.dumps(payload))

    def test_official_live_probe_returns_only_safe_operational_facts(self):
        embedded_key = "official-live-probe-unit-key-never-export"
        with tempfile.TemporaryDirectory() as temp:
            credential_path = Path(temp) / "vision-runtime.json"
            credential_path.write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CHEJIN_BUILD_KIND": "official",
                    "CHEJIN_VISION_CREDENTIAL_PATH": str(credential_path),
                },
                clear=False,
            ), patch(
                "chejin_worker_client.vision_credentials._run_vision_provider_probe_request",
                return_value={"ok": True, "status": 200, "response_text": embedded_key},
            ):
                payload = probe_official_vision_provider()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], 200)
        self.assertNotIn(embedded_key, json.dumps(payload))

    def test_official_live_probe_fails_closed_without_exposing_error(self):
        with patch.dict(
            os.environ,
            {"CHEJIN_BUILD_KIND": "official"},
            clear=False,
        ):
            os.environ.pop("CHEJIN_VISION_CREDENTIAL_PATH", None)
            payload = probe_official_vision_provider()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_reason"], "vision_credential_unavailable")


if __name__ == "__main__":
    unittest.main()
