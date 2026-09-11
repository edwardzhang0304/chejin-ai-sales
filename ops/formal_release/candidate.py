"""Keep immutable build candidates separate from successful acceptance artifacts.

Only trusted formal runs supply candidates. Retesting may change the GUI harness
or documentation, never application code, dependencies, shared tests or build steps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from verify import COMMIT, SUFFIXES, digest, require, manual_acceptance

WORKFLOW = ".github/workflows/worker-windows-package.yml"
BUILD_JOB = "Build signed formal Windows package"
ACCEPT_JOB = "Accept exact Windows candidate"
ROOT = Path(__file__).resolve().parents[2]
# Exact exceptions, deliberately not entire scripts/tests/directories.
RETEST_ONLY = {
    "ops/formal_release/summarize_run.py",
    "ops/formal_release/manual_publication.py", "ops/formal_release/manual_readiness.py",
    "ops/formal_release/deliver.py", "ops/formal_release/install.sh",
    "ops/formal_release/validate_dispatch.py", "ops/formal_release/workflow_scripts.py",
    "ops/formal_release/tests/test_manual_publication.py",
    "ops/formal_release/release_plan.py", "ops/formal_release/acceptance_cases.py",
    "ops/formal_release/tests/test_release_sop.py", "ops/formal_release/release-policy.json",

    "ops/formal_release/verify.py",
    "ops/formal_release/receiver.py",
    "ops/formal_release/tests/test_delivery.py",
    "worker-client/scripts/run-windows-updater-process-test.ps1",
    "worker-client/scripts/run-windows-pending-read-install.py",
    "ops/formal_release/tests/test_pending_read_install_gate.py",
    "worker-client/scripts/run-windows-client-upgrade-test.py",
    "worker-client/tests/test_windows_upgrade_gate_data.py",
    "ops/formal_release/candidate.py",
    "ops/formal_release/tests/test_candidate.py",
    "ops/formal_release/source_evidence.py",
    "ops/formal_release/run_evidence_checks.py",
    "ops/formal_release/tests/test_source_evidence.py",
    ".github/workflows/release-evidence.yml",
}
DOCS = {"deliverables/AI智能客服售前跟进系统_版本更新记录.md"}
DOCUMENT_PREFIXES = ("deliverables/", "docs/", "rules/")



def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def source_inputs(ref, root=ROOT, exclusions=None):
    """Hash Git objects, not a Windows checkout's CRLF or generated files."""
    import yaml
    require(COMMIT.fullmatch(ref), "INVALID_SOURCE_COMMIT")
    entries = subprocess.check_output(["git", "ls-tree", "-rz", ref], cwd=root)
    result = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        info, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if name in (RETEST_ONLY | DOCS if exclusions is None else exclusions) or name == "rules.md" or (exclusions is None and name.startswith(DOCUMENT_PREFIXES)) or name.startswith("rules/"):
            continue
        if name == WORKFLOW:
            raw = subprocess.check_output(["git", "show", f"{ref}:{name}"], cwd=root)
            workflow = yaml.load(raw, Loader=yaml.BaseLoader)
            # Acceptance wiring can evolve, but the complete build and prebuild
            # jobs, dependency setup and shared checks must remain identical.
            jobs = workflow["jobs"]
            build = {key: value for key, value in workflow.items() if key != "jobs"}
            build["jobs"] = {key: jobs[key] for key in ("prepare", "package")}
            result[name] = hashlib.sha256(json.dumps(build, sort_keys=True).encode()).hexdigest()
        else:
            result[name] = info.decode("ascii")  # Includes file mode and submodule object ID.
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recorded_input_exclusions(ref, root=ROOT):
    """Read literal classification from the immutable producer, never execute it.

    A tooling-classification correction must still authenticate the saved
    fingerprint under its original rules before comparing runtime inputs.
    """
    import ast
    raw = subprocess.check_output(["git", "show", f"{ref}:ops/formal_release/candidate.py"], cwd=root)
    tree = ast.parse(raw)
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"RETEST_ONLY", "DOCS"}}
    require(set(values) == {"RETEST_ONLY", "DOCS"}
            and all(isinstance(v, set) and all(isinstance(p, str) for p in v) for v in values.values()),
            "UNKNOWN_RECORDED_INPUT_CLASSIFICATION")
    return values["RETEST_ONLY"] | values["DOCS"]


def select_candidate(run, jobs, artifacts):
    require(run["path"] == WORKFLOW and run["event"] == "workflow_dispatch"
            and run["head_branch"] == "codex/gray-release-0.9.x", "NOT_FORMAL_CANDIDATE")
    builds = [j for j in jobs if j["name"] == BUILD_JOB and j["conclusion"] == "success"]
    require(len(builds) == 1, "CANDIDATE_BUILD_NOT_PASSED")
    # GitHub's 'rerun failed jobs' retains a successful build from its original
    # attempt. Select that exact build artifact, not a different/newest ZIP.
    attempt = builds[0].get("run_attempt", run["run_attempt"])
    name = f"chejin-candidate-{run['head_sha']}-{run['id']}-{attempt}"
    matches = [a for a in artifacts if a["name"] == name and not a["expired"]]
    require(len(matches) == 1, "CANDIDATE_NOT_UNIQUE_OR_EXPIRED")
    return matches[0]["id"], run["head_sha"]


def record(folder, commit, run_id):
    require(COMMIT.fullmatch(commit) and str(run_id).isdigit(), "INVALID_BUILD_IDENTITY")
    descriptors = list(folder.glob("*.release.json"))
    require(len(descriptors) == 1, "EXPECTED_ONE_DESCRIPTOR")
    desc = read(descriptors[0])
    require(desc["git_commit"] == commit, "CANDIDATE_SOURCE_MISMATCH")
    stem = f"chejin-worker-v{desc['version']}-windows-x64"
    files = {stem + suffix: digest(folder / (stem + suffix)) for suffix in SUFFIXES}
    require(files[stem + ".zip"] == desc["artifact_sha256"], "CANDIDATE_ZIP_MISMATCH")
    delivery = read(folder / (stem + ".delivery.json"))
    require(delivery["build_commit"] == commit and delivery["tests_status"] == "passed"
            and delivery["preflight_status"] == "passed", "CANDIDATE_BUILD_GATES_FAILED")
    payload = {"schema_version": 1, "build_commit": commit, "build_run_id": str(run_id),
               "version": desc["version"], "build_inputs_sha256": source_inputs(commit), "files": files}
    (folder / "candidate.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def validate_files(folder, proof):
    stem = f"chejin-worker-v{proof['version']}-windows-x64"
    require(set(proof["files"]) == {stem + suffix for suffix in SUFFIXES}, "INVALID_CANDIDATE_FILES")
    for name, sha in proof["files"].items():
        path = folder / name
        require(path.is_file() and not path.is_symlink() and digest(path) == sha,
                "CANDIDATE_FILE_CHANGED")
    desc = read(folder / (stem + ".release.json"))
    delivery = read(folder / (stem + ".delivery.json"))
    require(desc["git_commit"] == proof["build_commit"] == delivery["build_commit"],
            "CANDIDATE_SOURCE_MISMATCH")
    require(desc["artifact_sha256"] == proof["files"][stem + ".zip"], "CANDIDATE_ZIP_MISMATCH")
    return stem


def verify_candidate(folder, build_commit, build_run_id, acceptance_commit):
    proof = read(folder / "candidate.json")
    require(proof["schema_version"] == 1 and proof["build_commit"] == build_commit
            and proof["build_run_id"] == str(build_run_id), "CANDIDATE_PROVENANCE_MISMATCH")
    original = source_inputs(build_commit)
    if original != proof["build_inputs_sha256"]:
        require(source_inputs(build_commit, exclusions=recorded_input_exclusions(build_commit)) == proof["build_inputs_sha256"],
                "RECORDED_BUILD_INPUTS_MISMATCH")
    require(original == source_inputs(acceptance_commit), "BUILD_INPUTS_CHANGED_REBUILD_REQUIRED")
    return validate_files(folder, proof)


def accept(folder, report_path, commit, run_id, current_version):
    proof = read(folder / "candidate.json")
    stem = verify_candidate(folder, proof["build_commit"], proof["build_run_id"], commit)
    report = read(report_path)
    require(report.get("status") == "passed" and len(report.get("cases", [])) == 2,
            "GUI_ACCEPTANCE_INCOMPLETE")
    require({case.get("initial_run_status") for case in report["cases"]} == {"paused", "faulted"},
            "GUI_CASES_INCOMPLETE")
    for case in report["cases"]:
        require(case.get("status") == "passed" and case.get("current_version") == current_version
                and case.get("target_version") == proof["version"]
                and case.get("target_commit") == proof["build_commit"]
                and case.get("target_zip_sha256") == proof["files"][stem + ".zip"], "GUI_PACKAGE_MISMATCH")
        require(all(case.get(key) is True for key in (
            "real_settings_button_clicked", "original_worker_exited", "original_updater_used",
            "protected_data_preserved", "target_ui_confirmed", "actual_backend_download",
            "runtime_threads_alive", "immutable_handoff_baseline", "paused_intent_and_idle_gate_preserved",
        )), "GUI_GATE_NOT_PASSED")
    delivery_path = folder / (stem + ".delivery.json")
    delivery = read(delivery_path)
    delivery.update(upgrade_start_version=current_version, original_client_upgrade_check="passed",
                    original_client_upgrade_report_sha256=digest(report_path),
                    acceptance_commit=commit, acceptance_run_id=str(run_id), workflow_run_id=str(run_id),
                    candidate_build_run_id=proof["build_run_id"])
    delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")


def accept_manual(folder, report_path, pending_path, commit, run_id, current_version, recovery_required=True):
    proof = read(folder / "candidate.json")
    stem = verify_candidate(folder, proof["build_commit"], proof["build_run_id"], commit)
    report = read(report_path)
    pending = read(pending_path) if recovery_required else None
    require(report.get("status") == "passed" and len(report.get("cases", [])) == 2
            and {c.get("initial_run_status") for c in report["cases"]} == {"paused", "faulted"}, "MANUAL_CASES_INCOMPLETE")
    for c in [*report["cases"], *([pending] if pending is not None else [])]:
        require(c.get("status") == "passed" and c.get("current_version") == current_version
                and c.get("target_version") == proof["version"] and c.get("target_commit") == proof["build_commit"]
                and c.get("target_zip_sha256") == proof["files"][stem + ".zip"], "MANUAL_PACKAGE_MISMATCH")
    for c in report["cases"]:
        require(c.get("mode") == "preserve_data_manual_install" and c.get("real_settings_button_clicked") is False
                and c.get("original_updater_used") is False and all(c.get(k) is True for k in (
                    "original_worker_exited", "normal_close_used", "protected_data_preserved", "target_ui_confirmed",
                    "target_program_manifest_verified", "paused_intent_and_idle_gate_preserved", "original_data_directory_reused")), "MANUAL_DATA_GATE_FAILED")
    require(not recovery_required or (pending.get("mode") == "pending_read_preserve_data_install" and all(pending.get(k) is True for k in (
        "normal_close_used", "original_pending_flow_preserved_at_install", "original_data_directory_reused",
        "original_outbox_bytes_preserved", "original_flow_completed", "stopped_after_recovery",
        "target_ui_confirmed", "real_exe_recovery"))), "PENDING_READ_GATE_FAILED")
    path = folder / (stem + ".delivery.json")
    delivery = read(path)
    delivery.update(upgrade_start_version=current_version, installation_mode="preserve_data_manual_install",
        original_client_upgrade_check="not_applicable", automatic_update_allowed=False,
        manual_install_check="passed", pending_read_recovery_required=recovery_required,
        pending_read_install_check="passed" if recovery_required else "not_applicable",
        manual_install_report_sha256=digest(report_path), pending_read_install_report_sha256=digest(pending_path) if recovery_required else None,
        acceptance_commit=commit, acceptance_run_id=str(run_id), workflow_run_id=str(run_id),
        candidate_build_run_id=proof["build_run_id"])
    path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")


def delivery_source(folder, acceptance_commit, acceptance_run_id):
    """Resolve original signed build identity after selecting a passed acceptance run."""
    files = list(folder.glob("*.delivery.json"))
    require(len(files) == 1, "EXPECTED_ONE_DELIVERY")
    data = read(files[0])
    build = data["build_commit"]
    require(COMMIT.fullmatch(build), "INVALID_BUILD_IDENTITY")
    if "acceptance_commit" in data:
        require(data["acceptance_commit"] == acceptance_commit
                and data["acceptance_run_id"] == str(acceptance_run_id)
                and (data.get("original_client_upgrade_check") == "passed" or manual_acceptance(data)), "ACCEPTANCE_IDENTITY_MISMATCH")
        require(source_inputs(build) == source_inputs(acceptance_commit), "BUILD_INPUTS_CHANGED_REBUILD_REQUIRED")
    else:
        require(build == acceptance_commit, "LEGACY_BUILD_IDENTITY_MISMATCH")
    return build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["select", "record", "verify", "accept", "accept-manual", "delivery-source"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--pending-report", type=Path)
    args = parser.parse_args()
    env = os.environ
    if args.command == "select":
        from select_artifact import api
        run_id = env["CANDIDATE_RUN_ID"]
        require(run_id.isdigit(), "INVALID_CANDIDATE_RUN")
        prefix = f"repos/{env['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
        run = api(prefix)
        artifact, commit = select_candidate(run, api(prefix + "/jobs?per_page=100")["jobs"],
                                            api(prefix + "/artifacts?per_page=100")["artifacts"])
        with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"artifact_id={artifact}\nbuild_commit={commit}\n")
    elif args.command == "record":
        record(args.folder, env["GITHUB_SHA"], env["GITHUB_RUN_ID"])
    elif args.command == "verify":
        verify_candidate(args.folder, env["BUILD_COMMIT"], env["CANDIDATE_RUN_ID"], env["GITHUB_SHA"])
    elif args.command == "accept-manual":
        accept_manual(args.folder, args.report, args.pending_report, env["GITHUB_SHA"], env["GITHUB_RUN_ID"], env["CURRENT_VERSION"])
    elif args.command == "accept":
        accept(args.folder, args.report, env["GITHUB_SHA"], env["GITHUB_RUN_ID"], env["CURRENT_VERSION"])
    else:
        commit = delivery_source(args.folder, env["ACCEPTANCE_COMMIT"], env["FORMAL_RUN_ID"])
        with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"commit={commit}\n")


if __name__ == "__main__":
    main()
