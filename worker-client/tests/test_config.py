from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chejin_worker_client.config import ClientConfig, DEFAULT_API_BASE_URL


class ClientConfigTest(unittest.TestCase):
    def test_official_default_targets_production_api(self):
        self.assertEqual(DEFAULT_API_BASE_URL, "https://jiangsuchejin.com/api")
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                os.environ,
                {"CHEJIN_WORKER_HOME": temp},
                clear=True,
            ):
                config = ClientConfig.from_env()

        self.assertEqual(config.api_base_url, "https://jiangsuchejin.com/api")
        self.assertEqual(config.app_dir, Path(temp))

    def test_environment_override_remains_available_for_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                os.environ,
                {
                    "CHEJIN_WORKER_HOME": temp,
                    "CHEJIN_API_BASE_URL": "https://uat.example.test/api/",
                },
                clear=True,
            ):
                config = ClientConfig.from_env()

        self.assertEqual(config.api_base_url, "https://uat.example.test/api")


if __name__ == "__main__":
    unittest.main()
