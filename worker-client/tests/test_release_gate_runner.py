"""Check fail-fast orchestration without executing builds or the full suite."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ReleaseGateRunnerTest(unittest.TestCase):
    def setUp(self):
        # Unit-test the CLI in isolation from unittest/pytest argv and CI reuse state.
        args = patch.object(sys, "argv", ["run_checks.py"])
        args.start(); self.addCleanup(args.stop)
        env = patch.dict(os.environ, {"CHEJIN_SOURCE_CHECK_RECEIPT": ""})
        env.start(); self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("release_gate_runner", ROOT / "run_checks.py")
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        mkdir = patch.object(self.runner.tempfile, "mkdtemp", return_value=self.home.name)
        mkdir.start()
        self.addCleanup(mkdir.stop)

    def test_schema_failure_stops_before_security_or_other_tests(self):
        with patch.object(self.runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(self.runner.main(), 7)
        self.assertEqual(run.call_count, 1)
        self.assertIn("scripts/generate-c2-observation-schema.py", run.call_args.args[0])

    def test_any_security_failure_stops_before_full_tests(self):
        # Include pytest's no-tests-collected status; it must never count as pass.
        for code in (1, 2, 3, 4, 5):
            with self.subTest(exit_code=code):
                results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], code)]
                with patch.object(self.runner.subprocess, "run", side_effect=results) as run:
                    self.assertEqual(self.runner.main(), code)
                self.assertEqual(run.call_count, 2)
                self.assertIn("scripts/run-credential-security-checks.py", run.call_args.args[0])

    def test_full_suite_failure_still_stops_after_security_passes(self):
        results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 9)]
        with patch.object(self.runner.subprocess, "run", side_effect=results) as run:
            self.assertEqual(self.runner.main(), 9)
        self.assertEqual(run.call_count, 3)
        self.assertIn("unittest", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
