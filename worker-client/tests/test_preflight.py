from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-preflight-test-"))
os.environ["CHEJIN_RPA_MODE"] = "mock"

from chejin_worker_client.preflight import (
    PreflightCheck,
    backend_readyz_url,
    checks_to_dict,
    format_text,
    has_blocking_failures,
    run_preflight,
    write_report,
)


class PreflightTest(unittest.TestCase):
    def test_backend_readyz_url_is_derived_from_api_base(self):
        self.assertEqual(backend_readyz_url("http://127.0.0.1:8000/api"), "http://127.0.0.1:8000/readyz")
        self.assertEqual(backend_readyz_url("https://example.com/admin/api"), "https://example.com/admin/readyz")

    def test_blocking_failures_ignore_warning(self):
        checks = [
            PreflightCheck("binding", False, "warning", "未绑定"),
            PreflightCheck("sidecar", True, "error", "存在"),
        ]

        self.assertFalse(has_blocking_failures(checks))
        self.assertTrue(checks_to_dict(checks)["ok"])
        self.assertIn("[WARN] binding", format_text(checks))

    def test_blocking_failures_detect_error(self):
        checks = [PreflightCheck("backend", False, "error", "不可达")]

        self.assertTrue(has_blocking_failures(checks))
        self.assertFalse(checks_to_dict(checks)["ok"])

    def test_write_report_outputs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            written = write_report([PreflightCheck("sidecar", True, "error", "存在")], path)
            data = json.loads(written.read_text(encoding="utf-8"))

        self.assertTrue(data["ok"])
        self.assertEqual(data["checks"][0]["name"], "sidecar")

    def test_run_preflight_can_skip_external_checks(self):
        with patch("chejin_worker_client.preflight.binding_check", return_value=PreflightCheck("binding", True, "error", "已绑定")):
            checks = run_preflight(check_backend=False, check_wechat=False)

        names = {item.name for item in checks}
        self.assertIn("sidecar", names)
        self.assertNotIn("backend", names)
        self.assertNotIn("wechat", names)


if __name__ == "__main__":
    unittest.main()
