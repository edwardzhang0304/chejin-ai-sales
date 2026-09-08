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

from verify import COMMIT, SUFFIXES, digest, require

WORKFLOW = ".github/workflows/worker-windows-package.yml"
BUILD_JOB = "Build signed formal Windows package"
ACCEPT_JOB = "Accept exact Windows candidate"
ROOT = Path(__file__).resolve().parents[2]
# Exact exceptions, deliberately not entire scripts/tests/directories.
RETEST_ONLY = {
    "worker-client/scripts/run-windows-client-upgrade-test.py",
    "worker-client/tests/test_windows_upgrade_gate_data.py",
    "ops/formal_release/candidate.py",
    "ops/formal_release/tests/test_candidate.py",
}
DOCS = {
    "deliverables/AI智能客服售前跟进系统_技术方案手册_v0.9.68.md",
    "deliverables/AI智能客服售前跟进系统_PRD_运营后台统一版_v0.9.68.md",
    "deliverables/AI智能客服售前跟进系统_全流程图_v0.9.68.puml",
    "deliverables/AI智能客服售前跟进系统_版本更新记录.md",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def source_inputs(ref, root=ROOT):
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
        if name in RETEST_ONLY | DOCS or name == "rules.md" or name.startswith("rules/"):
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
    require(source_inputs(build_commit) == proof["build_inputs_sha256"] == source_inputs(acceptance_commit),
            "BUILD_INPUTS_CHANGED_REBUILD_REQUIRED")
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
                and data["original_client_upgrade_check"] == "passed", "ACCEPTANCE_IDENTITY_MISMATCH")
        require(source_inputs(build) == source_inputs(acceptance_commit), "BUILD_INPUTS_CHANGED_REBUILD_REQUIRED")
    else:
        require(build == acceptance_commit, "LEGACY_BUILD_IDENTITY_MISMATCH")
    return build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["select", "record", "verify", "accept", "delivery-source"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--report", type=Path)
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
    elif args.command == "accept":
        accept(args.folder, args.report, env["GITHUB_SHA"], env["GITHUB_RUN_ID"], env["CURRENT_VERSION"])
    else:
        commit = delivery_source(args.folder, env["ACCEPTANCE_COMMIT"], env["FORMAL_RUN_ID"])
        with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"commit={commit}\n")


if __name__ == "__main__":
    main()
