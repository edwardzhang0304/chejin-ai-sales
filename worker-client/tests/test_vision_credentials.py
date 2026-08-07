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


if __name__ == "__main__":
    unittest.main()
