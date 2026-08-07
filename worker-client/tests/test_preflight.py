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
    omniauto_vision_ocr_check,
    run_preflight,
    vision_credential_check,
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

    def test_vision_ocr_preflight_uses_production_subprocess_probe(self):
        with patch(
            "chejin_worker_client.omniauto_ocr_client.probe_omniauto_ocr_subprocess",
            return_value={"ok": False, "reason": "rapidocr_onnxruntime_unavailable"},
        ):
            check = omniauto_vision_ocr_check()

        self.assertFalse(check.ok)
        self.assertEqual(check.name, "vision_ocr_subprocess")
        self.assertEqual(
            check.detail["reason"],
            "rapidocr_onnxruntime_unavailable",
        )

    def test_official_vision_preflight_is_secret_free(self):
        secret = "official-unit-key-not-for-report"
        with patch(
            "chejin_worker_client.vision_credentials.is_official_vision_runtime",
            return_value=True,
        ), patch(
            "chejin_worker_client.vision_credentials.vision_credential_status",
            return_value={
                "configured": True,
                "credential_source": "embedded",
                "configuration_locked": True,
                "provider": "anthropic_compatible",
                "base_url": "https://aiself.vip/v1",
                "model": "doubao-seed-2-0-lite-260428",
                "request_style": "anthropic_messages_vision",
            },
        ), patch(
            "chejin_worker_client.vision_credentials.probe_official_vision_provider",
            return_value={
                "ok": True,
                "status": 200,
                "failure_reason": "",
                "model": "doubao-seed-2-0-lite-260428",
                "request_style": "anthropic_messages_vision",
            },
        ):
            check = vision_credential_check()

        self.assertTrue(check.ok)
        self.assertEqual(check.message, "内置 Vision 能力可用。")
        self.assertNotIn(secret, json.dumps(check.detail, ensure_ascii=False))

    def test_official_vision_preflight_blocks_when_live_probe_fails(self):
        with patch(
            "chejin_worker_client.vision_credentials.is_official_vision_runtime",
            return_value=True,
        ), patch(
            "chejin_worker_client.vision_credentials.vision_credential_status",
            return_value={
                "configured": True,
                "credential_source": "embedded",
                "configuration_locked": True,
                "provider": "anthropic_compatible",
                "base_url": "https://aiself.vip/v1",
                "model": "doubao-seed-2-0-lite-260428",
                "request_style": "anthropic_messages_vision",
            },
        ), patch(
            "chejin_worker_client.vision_credentials.probe_official_vision_provider",
            return_value={
                "ok": False,
                "status": 401,
                "failure_reason": "vision_provider_probe_failed",
                "model": "doubao-seed-2-0-lite-260428",
                "request_style": "anthropic_messages_vision",
            },
        ):
            check = vision_credential_check()

        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "error")
        self.assertEqual(check.message, "内置 Vision 能力不可用。")
        self.assertEqual(check.detail["live_probe"]["status"], 401)


if __name__ == "__main__":
    unittest.main()
