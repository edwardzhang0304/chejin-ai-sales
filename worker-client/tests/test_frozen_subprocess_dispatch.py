from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ROOT = ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import rpa_operator_guard
from apps.wechat_ai_customer_service.adapters import wechat_connector
from apps.wechat_ai_customer_service.optional_plugins.vision.integrations import (
    wechat_current,
)


class FrozenSubprocessDispatchTest(unittest.TestCase):
    def test_operator_guard_uses_frozen_dispatch_in_packaged_client(self):
        with (
            mock.patch.object(rpa_operator_guard.sys, "frozen", True, create=True),
            mock.patch.object(rpa_operator_guard.sys, "executable", r"C:\CheJin\worker.exe"),
        ):
            command = rpa_operator_guard.operator_guard_command(["--tenant-id", "default"])

        self.assertEqual(
            command,
            [r"C:\CheJin\worker.exe", "--rpa-operator-guard", "--tenant-id", "default"],
        )
        self.assertFalse(any(item.lower().endswith(".py") for item in command))

    def test_operator_guard_keeps_python_script_command_in_source_mode(self):
        with (
            mock.patch.object(rpa_operator_guard.sys, "frozen", False, create=True),
            mock.patch.object(rpa_operator_guard.sys, "executable", "/usr/bin/python3"),
        ):
            command = rpa_operator_guard.operator_guard_command(["--tenant-id", "default"])

        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertTrue(command[1].endswith("run_rpa_operator_guard.py"))

    def test_win32_ocr_compat_uses_frozen_sidecar_dispatch(self):
        with (
            mock.patch.object(wechat_connector.sys, "frozen", True, create=True),
            mock.patch.object(wechat_connector.sys, "executable", r"C:\CheJin\worker.exe"),
        ):
            command = wechat_connector.compat_sidecar_command(
                Path(r"C:\missing\python.exe"),
                Path(r"C:\bundle\wechat_win32_ocr_sidecar.py"),
                ["status"],
            )

        self.assertEqual(
            command,
            [r"C:\CheJin\worker.exe", "--omniauto-sidecar", "status"],
        )
        self.assertFalse(any(item.lower().endswith(".py") for item in command))

    def test_win32_ocr_daemon_uses_frozen_sidecar_dispatch(self):
        with (
            mock.patch.object(wechat_connector.sys, "frozen", True, create=True),
            mock.patch.object(wechat_connector.sys, "executable", r"C:\CheJin\worker.exe"),
        ):
            command = wechat_connector.compat_sidecar_command(
                Path(r"C:\missing\python.exe"),
                Path(r"C:\bundle\wechat_win32_ocr_sidecar.py"),
                ["--daemon"],
            )

        self.assertEqual(
            command,
            [r"C:\CheJin\worker.exe", "--omniauto-sidecar", "--daemon"],
        )
        self.assertFalse(any(item.lower().endswith(".py") for item in command))

    def test_vision_wechat_worker_uses_frozen_dispatch(self):
        with (
            mock.patch.object(wechat_current.sys, "frozen", True, create=True),
            mock.patch.object(wechat_current.sys, "executable", r"C:\CheJin\worker.exe"),
        ):
            command = wechat_current._worker_command(
                object(),
                ["observe-current-surface", "--target", "CJ123456"],
            )

        self.assertEqual(
            command,
            [
                r"C:\CheJin\worker.exe",
                "--omniauto-vision-wechat-worker",
                "observe-current-surface",
                "--target",
                "CJ123456",
            ],
        )
        self.assertNotIn("-m", command)


if __name__ == "__main__":
    unittest.main()
