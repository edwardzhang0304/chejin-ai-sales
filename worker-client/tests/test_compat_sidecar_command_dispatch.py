from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OMNIAUTO_ROOT = ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_connector


class CompletedProcess:
    stdout = '{"ok": true, "online": true}'
    stderr = ""
    returncode = 0


class DaemonProcess:
    stdin = None
    stdout = None
    stderr = None

    def poll(self):
        return None


class CompatSidecarCommandDispatchTest(unittest.TestCase):
    def tearDown(self) -> None:
        wechat_connector._compat_daemon_proc = None

    def test_frozen_oneshot_caller_uses_executable_dispatch(self):
        connector = wechat_connector.WeChatConnector(
            compat_sidecar_python=Path(r"C:\missing\python.exe"),
            compat_sidecar_script=Path(r"C:\bundle\wechat_win32_ocr_sidecar.py"),
            root=Path(r"C:\bundle"),
        )
        with (
            mock.patch.object(wechat_connector.sys, "frozen", True, create=True),
            mock.patch.object(wechat_connector.sys, "executable", r"C:\CheJin\worker.exe"),
            mock.patch.object(wechat_connector.subprocess, "run", return_value=CompletedProcess()) as run,
        ):
            payload = connector._call_compat_oneshot(
                ["status"], allow_failure=False, env_overrides=None
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(
            run.call_args.args[0],
            [r"C:\CheJin\worker.exe", "--omniauto-sidecar", "status"],
        )

    def test_source_oneshot_caller_uses_python_and_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python.exe"
            script = root / "wechat_win32_ocr_sidecar.py"
            python.touch()
            script.touch()
            connector = wechat_connector.WeChatConnector(
                compat_sidecar_python=python,
                compat_sidecar_script=script,
                root=root,
            )
            with (
                mock.patch.object(wechat_connector.sys, "frozen", False, create=True),
                mock.patch.object(wechat_connector.subprocess, "run", return_value=CompletedProcess()) as run,
            ):
                payload = connector._call_compat_oneshot(
                    ["sessions"], allow_failure=False, env_overrides=None
                )
            self.assertTrue(payload["ok"])
            self.assertEqual(run.call_args.args[0], [str(python), str(script), "sessions"])

    def test_frozen_daemon_caller_uses_executable_dispatch(self):
        with (
            mock.patch.object(wechat_connector.sys, "frozen", True, create=True),
            mock.patch.object(wechat_connector.sys, "executable", r"C:\CheJin\worker.exe"),
            mock.patch.object(wechat_connector.subprocess, "Popen", return_value=DaemonProcess()) as popen,
        ):
            wechat_connector._ensure_compat_daemon(
                Path(r"C:\missing\python.exe"),
                Path(r"C:\bundle\wechat_win32_ocr_sidecar.py"),
                Path(r"C:\bundle"),
            )
        self.assertEqual(
            popen.call_args.args[0],
            [r"C:\CheJin\worker.exe", "--omniauto-sidecar", "--daemon"],
        )

    def test_source_daemon_caller_uses_python_and_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python.exe"
            script = root / "wechat_win32_ocr_sidecar.py"
            python.touch()
            script.touch()
            with (
                mock.patch.object(wechat_connector.sys, "frozen", False, create=True),
                mock.patch.object(wechat_connector.subprocess, "Popen", return_value=DaemonProcess()) as popen,
            ):
                wechat_connector._ensure_compat_daemon(python, script, root)
            self.assertEqual(
                popen.call_args.args[0],
                [str(python), str(script), "--daemon"],
            )


if __name__ == "__main__":
    unittest.main()
