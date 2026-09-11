import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ops/formal_release"))
import source_evidence as evidence
import run_evidence_checks as runner
import yaml


class EvidenceTrustTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 42, "run_attempt": 1, "head_sha": "a" * 40, "head_branch": evidence.BRANCH,
                    "event": "workflow_dispatch", "path": evidence.WORKFLOW, "status": "completed",
                    "repository": {"full_name": evidence.REPO}}
        self.jobs = [{"name": evidence.JOBS["source"], "conclusion": "success", "run_attempt": 1}]
        self.report = {"schema_version": 2, "commit": "a" * 40, "run_id": "42", "attempt": "1",
                       "kind": "source", "status": "passed", "fingerprint": "f" * 64,
                       "scope": ["reviewed test"], "results": [{"exit_code": 0, "passed": 1, "failed": 0, "skipped": 0}]}

    def test_only_successful_current_attempt_and_trusted_repository_workflow_are_allowed(self):
        self.assertFalse(evidence.validate_run(self.run, self.jobs, "source"))
        for key, value in (("head_branch", "other"), ("path", "fast-uat.yml"), ("event", "pull_request"),
                           ("status", "in_progress"), ("repository", {"full_name": "untrusted/fork"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence.validate_run({**self.run, key: value}, self.jobs, "source")
        for job in ({**self.jobs[0], "conclusion": "failure"}, {**self.jobs[0], "run_attempt": 2}):
            with self.assertRaises(ValueError): evidence.validate_run(self.run, [job], "source")

    def test_report_binds_source_attempt_scope_and_no_skips(self):
        with patch.object(evidence, "fingerprint", return_value="f" * 64), patch.object(evidence, "git", return_value=b"workflow"):
            evidence.validate_report(self.report, self.run, "source")
            for key, value in (("commit", "b" * 40), ("run_id", "43"), ("attempt", "2"), ("scope", []), ("results", []), ("fingerprint", "e" * 64)):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    evidence.validate_report({**self.report, key: value}, self.run, "source")
            for key, value in (("exit_code", 1), ("failed", 1), ("skipped", 1), ("passed", 0)):
                report = copy.deepcopy(self.report); report["results"][0][key] = value
                with self.subTest(key=key), self.assertRaises(ValueError): evidence.validate_report(report, self.run, "source")

    def test_changed_inputs_or_producer_do_not_reuse_evidence(self):
        with patch.object(evidence, "fingerprint", side_effect=["old", "new"]):
            with self.assertRaisesRegex(ValueError, "INPUT_MISMATCH"):
                evidence.validate_report(self.report, self.run, "source")

        with patch.object(evidence, "fingerprint", return_value="f" * 64), patch.object(evidence, "git", side_effect=[b"old", b"new"]):
            with self.assertRaisesRegex(ValueError, "PRODUCER_CHANGED"):
                evidence.validate_report(self.report, self.run, "source")

    def test_tooling_requires_real_windows_parser_job(self):
        jobs = [{"name": evidence.JOBS["tooling"], "conclusion": "success", "run_attempt": 1}]
        with self.assertRaisesRegex(ValueError, "WINDOWS_PARSER_NOT_PASSED"):
            evidence.validate_run(self.run, jobs, "tooling")
        parser = {"name": "Windows build script parser", "conclusion": "success", "run_attempt": 1}
        evidence.validate_run(self.run, jobs + [parser], "tooling")
        for value in ("failure", "skipped", "cancelled"):
            with self.assertRaises(ValueError): evidence.validate_run(self.run, jobs + [{**parser, "conclusion": value}], "tooling")

    def test_missing_evidence_never_starts_tests(self):
        with patch.object(evidence, "api", return_value={"workflow_runs": []}), patch.object(evidence.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "SOURCE_EVIDENCE_MISSING"):
                evidence.resolve("source")
            run.assert_not_called()

    def test_local_receipt_cannot_cross_run_attempt_commit_or_changed_inputs(self):
        current = {"commit": "a" * 40, "run_id": "42", "attempt": "1"}
        receipt = {"schema_version": 2, "kind": "formal-source-gate", **current,
                   "source": {"artifact_id": 10, "scope": ["test"]}, "tooling": {"artifact_id": 11, "scope": ["tool"]},
                   "source_fingerprint": "f" * 64, "tooling_fingerprint": "f" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(evidence, "identity", return_value=current), patch.object(evidence, "fingerprint", return_value="f" * 64), patch.object(evidence, "git", return_value=b""):
            path = Path(directory) / "receipt.json"; path.write_text(json.dumps(receipt))
            evidence.verify_local(str(path))
            for key, value in (("commit", "b" * 40), ("run_id", "43"), ("attempt", "2"), ("source_fingerprint", "d" * 64), ("tooling", {})):
                path.write_text(json.dumps({**receipt, key: value}))
                with self.subTest(key=key), self.assertRaises(ValueError): evidence.verify_local(str(path))


class FingerprintAndSelectionTests(unittest.TestCase):
    def test_business_dependencies_and_tests_remain_protected_but_tools_and_docs_are_separate(self):
        for name in ("worker-client/requirements.txt", "worker-client/chejin_worker_client/update.py", "backend/app/main.py", "contracts/api.json", "worker-client/tests/test_update_long_paths.py", "worker-client/prompts/instruction.md"):
            self.assertTrue(evidence.relevant(name, "source"), name)
        for name in ("deliverables/current.md", "rules/operations.rules.md", "ops/formal_release/source_evidence.py", "worker-client/scripts/build-windows.ps1"):
            self.assertFalse(evidence.relevant(name, "source"), name)
        self.assertTrue(evidence.relevant("worker-client/scripts/build-windows.ps1", "tooling"))

    def test_fingerprint_changes_on_content_and_mode_but_not_docs(self):
        def row(mode, sha, name): return f"{mode} blob {sha}\t{name}".encode() + b"\0"
        app = row("100644", "a" * 40, "worker-client/app.py")
        with patch.object(evidence, "git", return_value=app): baseline = evidence.fingerprint("HEAD", "source")
        with patch.object(evidence, "git", return_value=app + row("100644", "b" * 40, "deliverables/doc.md")):
            self.assertEqual(baseline, evidence.fingerprint("HEAD", "source"))
        for changed in (app.replace(b"100644", b"100755"), app.replace(b"a" * 40, b"b" * 40)):
            with patch.object(evidence, "git", return_value=changed): self.assertNotEqual(baseline, evidence.fingerprint("HEAD", "source"))

    def test_selection_deduplicates_and_rejects_full_directory_or_flags(self):
        file = "tests/test_update_long_paths.py"
        self.assertEqual(runner.selected_nodes("worker-client", [file + "::test_a", file, file]), [file])
        for node in ("tests", "--collect-only", "../backend/test_fake.py", "/tmp/test_fake.py", "tests/no_file.py"):
            with self.subTest(node=node), self.assertRaises(ValueError): runner.selected_nodes("worker-client", [node])

    def test_junit_does_not_count_failures_or_skips_as_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.xml"
            path.write_text('<testsuites><testsuite><testcase/><testcase><skipped/></testcase><testcase><failure/></testcase></testsuite></testsuites>')
            self.assertEqual(runner.junit(path), {"passed": 1, "failed": 1, "skipped": 1})

    def test_formal_workflow_has_no_full_or_shared_source_test_fallback(self):
        text = (ROOT / evidence.FORMAL).read_text()
        for duplicate in ("run_checks.py", "quick_gate.py", "-m pytest", "unittest discover", "uses: ./.github/actions/worker-release-checks"):
            self.assertNotIn(duplicate, text)
        jobs = yaml.load(text, Loader=yaml.BaseLoader)["jobs"]
        self.assertEqual(jobs["package"]["needs"], "prepare")
        self.assertTrue(any("source_evidence.py resolve" in s.get("run", "") for s in jobs["prepare"]["steps"]))
        script = (ROOT / "worker-client/scripts/build-windows.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('if ($DevelopmentBuild -and -not $SkipTests) {\n  .\\.venv\\Scripts\\python.exe run_checks.py', script)
        self.assertIn("source_evidence.py verify", script)
        self.assertNotIn("quick_gate.py", script)

    def test_fast_uat_workflow_and_shared_checks_unchanged(self):
        for path in (".github/workflows/worker-windows-fast-uat.yml", ".github/actions/worker-release-checks/action.yml"):
            previous = subprocess.check_output(["git", "show", "cd8763ed38ec1df1ff8054e49c3381e8a5322f62:" + path], cwd=ROOT)
            self.assertEqual(previous.replace(b"\r\n", b"\n"), (ROOT / path).read_bytes().replace(b"\r\n", b"\n"))


if __name__ == "__main__":
    unittest.main()
