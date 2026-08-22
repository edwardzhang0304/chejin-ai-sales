from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRY_PATH = ROOT / "packaging" / "chejin_worker_client_entry.py"


def load_entry_module():
    spec = importlib.util.spec_from_file_location("chejin_packaging_entry_test", ENTRY_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("packaging entry module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingEntryDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entry_module()

    @staticmethod
    def _identity_file(root: Path) -> Path:
        path = root / "runtime-build-identity.json"
        path.write_text(
            json.dumps(
                {
                    "version": "0.9.27",
                    "git_commit": "a" * 40,
                    "git_branch": "codex/gray-release-0.9.x",
                }
            ),
            encoding="utf-8",
        )
        return path

    def _environment(self, root: Path):
        environment = {
            "LOCALAPPDATA": str(root),
            "CHEJIN_BUILD_IDENTITY_PATH": str(self._identity_file(root)),
        }
        context = mock.patch.dict(os.environ, environment, clear=False)
        return context

    def test_main_import_failure_uses_default_local_app_data_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self._environment(root):
                os.environ.pop("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", None)
                with mock.patch.object(
                    self.entry,
                    "_load_main",
                    side_effect=ImportError("worker_token=must-not-leak"),
                ):
                    with self.assertRaises(ImportError):
                        self.entry.run()

            path = root / "CheJinWorker" / "diagnostics" / "startup-crash.jsonl"
            payload = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["version"], "0.9.27")
            self.assertEqual(payload["build_commit"], "a" * 40)
            self.assertEqual(payload["exception_type"], "ImportError")
            self.assertTrue(payload["timestamp"])
            self.assertTrue(payload["windows_version"])
            self.assertIn("[REDACTED]", payload["traceback"])
            self.assertNotIn("must-not-leak", payload["traceback"])

    def test_main_startup_failure_leaves_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def crashing_main():
                raise RuntimeError("Qt platform plugin unavailable")

            with self._environment(root):
                os.environ.pop("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", None)
                with mock.patch.object(
                    self.entry,
                    "_load_main",
                    return_value=crashing_main,
                ):
                    with self.assertRaises(RuntimeError):
                        self.entry.run()

            path = root / "CheJinWorker" / "diagnostics" / "startup-crash.jsonl"
            payload = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["exception_type"], "RuntimeError")
            self.assertIn("Qt platform plugin unavailable", payload["traceback"])

    def test_normal_startup_does_not_create_false_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self._environment(root):
                os.environ.pop("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", None)
                with mock.patch.object(
                    self.entry,
                    "_load_main",
                    return_value=lambda: 0,
                ):
                    self.assertEqual(self.entry.run(), 0)

            path = root / "CheJinWorker" / "diagnostics" / "startup-crash.jsonl"
            self.assertFalse(path.exists())

    def test_frozen_startup_redaction_covers_embedded_vision_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            embedded_key = "embedded-startup-unit-key-never-export"
            (root / "vision-runtime.json").write_text(
                json.dumps(
                    {"schema_version": 1, "vision_api_key": embedded_key}
                ),
                encoding="utf-8",
            )
            with mock.patch.object(self.entry.sys, "frozen", True, create=True), mock.patch.object(
                self.entry.sys,
                "_MEIPASS",
                str(root),
                create=True,
            ):
                redacted = self.entry._redact_text(
                    f"startup provider error: {embedded_key}"
                )

        self.assertNotIn(embedded_key, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_frozen_startup_failure_logs_and_exits_without_error_dialog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def crashing_main():
                raise OSError("Qt DLL load failed")

            with self._environment(root):
                os.environ.pop("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", None)
                with mock.patch.object(self.entry.sys, "frozen", True, create=True):
                    with mock.patch.object(
                        self.entry,
                        "_load_main",
                        return_value=crashing_main,
                    ):
                        self.assertEqual(self.entry.run(), 1)

            path = root / "CheJinWorker" / "diagnostics" / "startup-crash.jsonl"
            payload = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["exception_type"], "OSError")
            self.assertIn("Qt DLL load failed", payload["traceback"])


if __name__ == "__main__":
    unittest.main()
