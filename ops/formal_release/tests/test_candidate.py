"""Failure-path coverage for retaining and retesting an immutable candidate."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import candidate
import select_artifact
import summarize_run
import validate_dispatch

ROOT = Path(__file__).resolve().parents[3]


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.build, self.retest = "a" * 40, "b" * 40
        self.stem = "chejin-worker-v0.9.70-windows-x64"
        self.archive = self.folder / (self.stem + ".zip")
        self.archive.write_bytes(b"synthetic immutable archive")
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.original = self.archive.read_bytes()
        self.write(".release.json", {"git_commit": self.build, "version": "0.9.70", "artifact_sha256": self.sha})
        self.write(".delivery.json", {"build_commit": self.build, "workflow_run_id": "123", "tests_status": "passed", "preflight_status": "passed"})
        (self.folder / (self.stem + ".sha256.txt")).write_text(self.sha)
        self.inputs = patch("candidate.source_inputs", return_value="c" * 64)
        self.inputs.start()
        self.addCleanup(self.inputs.stop)
        candidate.record(self.folder, self.build, "123")
        self.report = self.folder / "upgrade-result.json"
        flags = {key: True for key in (
            "real_settings_button_clicked", "original_worker_exited", "original_updater_used",
            "protected_data_preserved", "target_ui_confirmed", "actual_backend_download",
            "runtime_threads_alive", "immutable_handoff_baseline", "paused_intent_and_idle_gate_preserved")}
        self.result = {"status": "passed", "cases": [dict(flags, status="passed", current_version="0.9.69",
            target_version="0.9.70", target_commit=self.build, target_zip_sha256=self.sha,
            initial_run_status=state) for state in ("paused", "faulted")]}
        self.report.write_text(json.dumps(self.result))

    def write(self, suffix, payload):
        (self.folder / (self.stem + suffix)).write_text(json.dumps(payload), encoding="utf-8-sig")

    def test_failed_acceptance_preserves_candidate_then_retest_keeps_original_build(self):
        bad = copy.deepcopy(self.result)
        bad["cases"][0]["protected_data_preserved"] = False
        self.report.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "GUI_GATE_NOT_PASSED"):
            candidate.accept(self.folder, self.report, self.retest, "456", "0.9.69")
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertNotIn("acceptance_run_id", candidate.read(self.folder / (self.stem + ".delivery.json")))
        self.report.write_text(json.dumps(self.result))
        candidate.accept(self.folder, self.report, self.retest, "456", "0.9.69")
        data = candidate.read(self.folder / (self.stem + ".delivery.json"))
        self.assertEqual((data["build_commit"], data["acceptance_commit"], data["candidate_build_run_id"]),
                         (self.build, self.retest, "123"))
        self.assertEqual(candidate.delivery_source(self.folder, self.retest, "456"), self.build)
        import deliver
        metadata, _ = deliver.metadata(self.folder, "0.9.69", "456", self.build)
        self.assertEqual(data["workflow_run_id"], metadata["run_id"])
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertEqual(candidate.read(self.folder / (self.stem + ".release.json"))["git_commit"], self.build)

    def test_each_candidate_file_tamper_blocks_reuse(self):
        for suffix in candidate.SUFFIXES:
            path = self.folder / (self.stem + suffix)
            original = path.read_bytes()
            with self.subTest(suffix=suffix):
                path.write_bytes(original + b"tampered")
                with self.assertRaisesRegex(ValueError, "CANDIDATE_FILE_CHANGED"):
                    candidate.verify_candidate(self.folder, self.build, "123", self.retest)
                path.write_bytes(original)

    def test_wrong_source_run_or_build_input_blocks_retest(self):
        for commit, run in ((self.retest, "123"), (self.build, "999")):
            with self.assertRaisesRegex(ValueError, "CANDIDATE_PROVENANCE_MISMATCH"):
                candidate.verify_candidate(self.folder, commit, run, self.retest)
        with patch("candidate.source_inputs", side_effect=["c" * 64, "d" * 64]):
            with self.assertRaisesRegex(ValueError, "REBUILD_REQUIRED"):
                candidate.verify_candidate(self.folder, self.build, "123", self.retest)

    def test_wrong_package_or_incomplete_report_never_promoted(self):
        changes = [("status", "failed"), ("target_commit", self.retest), ("target_zip_sha256", "0" * 64),
                   ("current_version", "0.9.68"), ("target_version", "0.9.71"),
                   ("initial_run_status", "faulted"), ("original_updater_used", False)]
        for key, value in changes:
            with self.subTest(key=key):
                bad = copy.deepcopy(self.result)
                bad["cases"][0][key] = value
                self.report.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    candidate.accept(self.folder, self.report, self.retest, "456", "0.9.69")

    def test_wrong_acceptance_run_or_commit_not_deliverable(self):
        candidate.accept(self.folder, self.report, self.retest, "456", "0.9.69")
        for commit, run in ((self.build, "456"), (self.retest, "123")):
            with self.assertRaisesRegex(ValueError, "ACCEPTANCE_IDENTITY_MISMATCH"):
                candidate.delivery_source(self.folder, commit, run)

    def test_legacy_verified_artifact_still_requires_original_commit(self):
        self.assertEqual(candidate.delivery_source(self.folder, self.build, "123"), self.build)
        with self.assertRaisesRegex(ValueError, "LEGACY_BUILD_IDENTITY_MISMATCH"):
            candidate.delivery_source(self.folder, self.retest, "456")


class SourceInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.put(candidate.WORKFLOW, (ROOT / candidate.WORKFLOW).read_text())
        self.put("worker-client/chejin_worker_client/app.py", "application = 1\n")
        self.base = self.commit()
        self.fingerprint = candidate.source_inputs(self.base, self.root)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def put(self, path, value):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture", "--no-verify")
        return self.git("rev-parse", "HEAD")

    def test_gui_harness_and_docs_changes_allow_reuse(self):
        for path in candidate.RETEST_ONLY | candidate.DOCS:
            self.put(path, "revised harness or documentation\n")
        self.assertEqual(candidate.source_inputs(self.commit(), self.root), self.fingerprint)

    def test_application_dependencies_build_scripts_and_shared_tests_require_rebuild(self):
        for path in ("worker-client/chejin_worker_client/app.py", "worker-client/requirements.txt",
                     "worker-client/scripts/build-windows.ps1", ".github/actions/worker-release-checks/action.yml",
                     "backend/app/main.py", "worker-client/tests/test_post_update_health.py"):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.base)
                self.put(path, "changed build input\n")
                self.assertNotEqual(candidate.source_inputs(self.commit(), self.root), self.fingerprint)

    def test_acceptance_wiring_can_change_but_build_job_cannot(self):
        original = (self.root / candidate.WORKFLOW).read_text()
        self.put(candidate.WORKFLOW, original.replace("timeout-minutes: 25", "timeout-minutes: 24"))
        self.assertEqual(candidate.source_inputs(self.commit(), self.root), self.fingerprint)
        self.put(candidate.WORKFLOW, original.replace("timeout-minutes: 90", "timeout-minutes: 89"))
        self.assertNotEqual(candidate.source_inputs(self.commit(), self.root), self.fingerprint)

    def test_inherited_workflow_environment_requires_rebuild(self):
        path = self.root / candidate.WORKFLOW
        path.write_text(path.read_text().replace("permissions:\n", 'env:\n  BUILD_FLAG: changed\n\npermissions:\n', 1))
        self.assertNotEqual(candidate.source_inputs(self.commit(), self.root), self.fingerprint)


class SelectionAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.run = {"path": candidate.WORKFLOW, "event": "workflow_dispatch", "head_branch": "codex/gray-release-0.9.x",
                    "head_sha": "a" * 40, "id": 123, "run_attempt": 1}
        self.build_job = {"name": candidate.BUILD_JOB, "conclusion": "success"}
        self.artifact = {"id": 789, "name": "chejin-candidate-" + "a" * 40 + "-123-1", "expired": False}

    def test_failed_acceptance_can_select_candidate_but_cannot_select_release(self):
        jobs = [self.build_job, {"name": candidate.ACCEPT_JOB, "conclusion": "failure"}]
        self.assertEqual(candidate.select_candidate(self.run, jobs, [self.artifact])[0], 789)
        with self.assertRaisesRegex(ValueError, "ACCEPTANCE_NOT_PASSED"):
            select_artifact.select(self.run, jobs, [self.artifact])

    def test_rerun_failed_jobs_reuses_the_successful_build_attempt(self):
        run = {**self.run, "run_attempt": 2}
        jobs = [{**self.build_job, "run_attempt": 1}, {"name": candidate.ACCEPT_JOB, "conclusion": "failure"}]
        self.assertEqual(candidate.select_candidate(run, jobs, [self.artifact])[0], 789)

    def test_nonformal_expired_failed_build_wrong_attempt_or_ambiguous_candidate_denied(self):
        variants = [({**self.run, "path": ".github/workflows/worker-windows-fast-uat.yml"}, [self.build_job], [self.artifact]),
                    (self.run, [{**self.build_job, "conclusion": "failure"}], [self.artifact]),
                    (self.run, [self.build_job], [{**self.artifact, "expired": True}]),
                    ({**self.run, "run_attempt": 2}, [self.build_job], [self.artifact]),
                    (self.run, [self.build_job], [self.artifact, self.artifact])]
        for args in variants:
            with self.assertRaises(ValueError):
                candidate.select_candidate(*args)

    def test_skipped_cancelled_or_failed_acceptance_block_formal_selection(self):
        artifact = {**self.artifact, "name": "chejin-worker-v0.9.70-windows-x64-" + "a" * 40 + "-1"}
        for state in ("skipped", "cancelled", "failure", None):
            with self.assertRaisesRegex(ValueError, "ACCEPTANCE_NOT_PASSED"):
                select_artifact.select(self.run, [self.build_job, {"name": candidate.ACCEPT_JOB, "conclusion": state}], [artifact])
        self.assertEqual(select_artifact.select(self.run,
            [{"name": candidate.BUILD_JOB, "conclusion": "skipped"}, {"name": candidate.ACCEPT_JOB, "conclusion": "success"}], [artifact])[0], 789)

    def test_shared_tests_are_identical_to_prior_fast_uat_tests(self):
        # Baseline proves the extraction preserves existing tests, order and commands.
        baseline = subprocess.check_output(["git", "show", "e847470:.github/workflows/worker-windows-fast-uat.yml"], cwd=ROOT)
        old = yaml.load(baseline, Loader=yaml.BaseLoader)["jobs"]["fast-uat"]["steps"]
        first = next(i for i, step in enumerate(old) if step.get("name") == "Run credential security gate")
        end = next(i for i, step in enumerate(old) if step.get("name") == "Build reusable portable runtime base on cache miss")
        action = yaml.load((ROOT / ".github/actions/worker-release-checks/action.yml").read_text(), Loader=yaml.BaseLoader)
        steps = action["runs"]["steps"]
        self.assertEqual([{k:v for k,v in step.items() if k != "if"} for step in steps], old[first:end])
        expected = {
            "Run credential security gate": "github.workflow != 'Worker Windows package gate' || env.CHEJIN_SHARED_CREDENTIALS_REUSED != 'true'",
            "Run affected Worker and backend read-settlement tests": "github.workflow != 'Worker Windows package gate' || env.CHEJIN_SHARED_SETTLEMENT_REUSED != 'true'",
        }
        self.assertEqual({step["name"]:step["if"] for step in steps if "if" in step}, expected)
        new = yaml.load((ROOT / ".github/workflows/worker-windows-fast-uat.yml").read_text(), Loader=yaml.BaseLoader)["jobs"]["fast-uat"]["steps"]
        self.assertEqual(new[:first], old[:first])
        self.assertEqual(new[first+1:], old[end:])

    def test_retest_mode_cannot_execute_build_job_or_deliver_without_acceptance(self):
        jobs = yaml.load((ROOT / candidate.WORKFLOW).read_text(), Loader=yaml.BaseLoader)["jobs"]
        self.assertEqual(jobs["package"]["if"], "inputs.delivery_mode == 'build_and_stage'")
        self.assertEqual(jobs["deliver"]["needs"], ["prepare", "acceptance"])
        self.assertIn("needs.acceptance.result == 'success'", jobs["deliver"]["if"])
        self.assertFalse(any("build-windows.ps1" in step.get("run", "") for step in jobs["acceptance"]["steps"]))
        names = [step.get("name") for step in jobs["package"]["steps"]]
        self.assertLess(names.index("Fail fast on native Windows handoff checks"), names.index("Build and run packaged runtime probes"))

    def test_retest_requires_candidate_run_and_configured_old_client(self):
        env = {"GITHUB_REF": "refs/heads/codex/gray-release-0.9.x", "RELEASE_APPROVED": "true",
               "RELEASE_REASON": "fixture", "DELIVERY_MODE": "retest_candidate", "CURRENT_VERSION": "0.9.74",
               "FORMAL_SSH_KEY": "fixture", "FORMAL_SSH_HOST": "fixture", "FORMAL_SSH_PORT": "22",
               "FORMAL_KNOWN_HOSTS": "fixture"}
        with self.assertRaisesRegex(ValueError, "CANDIDATE_RUN_REQUIRED"):
            validate_dispatch.validate(env)
        validate_dispatch.validate({**env, "CANDIDATE_RUN_ID": "123"})
        with self.assertRaisesRegex(ValueError, "UPGRADE_START_FIXTURE_NOT_CONFIGURED"):
            validate_dispatch.validate({**env, "CANDIDATE_RUN_ID": "123", "CURRENT_VERSION": "0.9.69"})

    def test_summary_reports_failure_stage_without_claiming_publication(self):
        report = summarize_run.summarize([{"name": candidate.ACCEPT_JOB, "conclusion": "failure",
            "status": "completed", "started_at": "2026-09-08T00:00:00Z", "completed_at": "2026-09-08T00:01:26Z"}], "456", "123")
        self.assertEqual(report["stages"][0]["seconds"], 86)
        self.assertIn("retest_candidate", report["next_action"])


if __name__ == "__main__":
    unittest.main()
