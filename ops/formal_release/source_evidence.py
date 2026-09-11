"""Formal builds consume CI evidence; this module never falls back to running tests."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
REPO = "edwardzhang0304/chejin-ai-sales"
BRANCH = "codex/gray-release-0.9.x"
WORKFLOW = ".github/workflows/release-evidence.yml"
FORMAL = ".github/workflows/worker-windows-package.yml"
JOBS = {"source": "Selected source checks", "tooling": "Release tools checks"}
TOOL_FILES = {
    "worker-client/scripts/build-windows.ps1", "worker-client/run_checks.py",
    "worker-client/tests/test_packaging_scripts.py", "worker-client/tests/test_release_gate_runner.py",
    "worker-client/scripts/run-windows-client-upgrade-test.py",
    "worker-client/tests/test_windows_upgrade_gate_data.py",
}
TOOL_DEPENDENCIES = {
    "worker-client/requirements-test.txt", "backend/requirements.txt",
    "backend/app/services/release_readiness.py", "worker-client/chejin_worker_client/__init__.py",
    "worker-client/chejin_worker_client/models.py", "worker-client/chejin_worker_client/release_package_contract.py",
}
EXCLUDED = ("deliverables/", "output/", "rules/", "docs/")


def require(ok, code):
    if not ok:
        raise ValueError(code)


def git(*args, root=ROOT):
    return subprocess.check_output(["git", *args], cwd=root)


def relevant(name, kind):
    tooling = name.startswith(("ops/formal_release/", ".github/")) or name in TOOL_FILES
    if kind == "tooling":
        # Toolkit dependencies are explicit and share no mutable business test state.
        return tooling or name in TOOL_DEPENDENCIES
    return not (tooling or name.startswith(EXCLUDED) or name == "rules.md")


def fingerprint(ref, kind, root=ROOT):
    rows = []
    for row in git("ls-tree", "-rz", ref, root=root).split(b"\0"):
        if row and relevant(row.split(b"\t", 1)[1].decode(), kind):
            rows.append(row.decode())
    require(bool(rows), "EMPTY_INPUT_FINGERPRINT")
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def identity():
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY") == REPO,
            "TRUSTED_CI_REQUIRED")
    return {"commit": git("rev-parse", "HEAD").decode().strip(),
            "run_id": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"]}


def validate_run(run, jobs, kind):
    require(run.get("head_branch") == BRANCH and run.get("event") in {"push", "workflow_dispatch"}
            and run.get("status") == "completed"
            and run.get("repository", {}).get("full_name") == REPO, "UNTRUSTED_EVIDENCE_RUN")
    legacy = kind == "source" and run.get("path") == FORMAL
    require(legacy or run.get("path") == WORKFLOW, "UNTRUSTED_EVIDENCE_WORKFLOW")
    expected = "Build signed formal Windows package" if legacy else JOBS[kind]
    matches = [j for j in jobs if j.get("name") == expected and j.get("conclusion") == "success"
               and j.get("run_attempt") == run.get("run_attempt")]
    require(len(matches) == 1, "EVIDENCE_JOB_NOT_PASSED")
    if kind == "tooling":
        require(any(j.get("name") == "Windows build script parser" and j.get("conclusion") == "success"
                    and j.get("run_attempt") == run.get("run_attempt") for j in jobs), "WINDOWS_PARSER_NOT_PASSED")
    return legacy


def validate_report(report, run, kind, legacy=False):
    sha = run["head_sha"]
    require(fingerprint(sha, kind) == fingerprint("HEAD", kind), "EVIDENCE_INPUT_MISMATCH")
    if legacy:
        from source_check_reuse import SUITES, fingerprint as legacy_fingerprint
        require(report.get("schema_version") == 1 and report.get("mode") == "same_run_complete"
                and report.get("current_commit") == sha
                and report.get("current_tree_sha256") == legacy_fingerprint(sha)
                and set(report.get("completed_suites", [])) == set(SUITES), "INCOMPLETE_LEGACY_SOURCE_EVIDENCE")
        attempt = report.get("run_attempt")
    else:
        require(git("show", sha + ":" + WORKFLOW) == git("show", "HEAD:" + WORKFLOW),
                "EVIDENCE_PRODUCER_CHANGED")
        require(report.get("schema_version") == 2 and report.get("kind") == kind
                and report.get("commit") == sha and report.get("status") == "passed"
                and report.get("fingerprint") == fingerprint(sha, kind)
                and report.get("scope") and report.get("results"), "INVALID_EVIDENCE_REPORT")
        for result in report["results"]:
            require(result.get("exit_code") == 0 and result.get("passed", 0) > 0
                    and result.get("failed") == 0 and result.get("skipped") == 0,
                    "FAILED_OR_SKIPPED_EVIDENCE")
        attempt = report.get("attempt")
    require(str(report.get("run_id")) == str(run["id"])
            and str(attempt) == str(run["run_attempt"]), "EVIDENCE_ATTEMPT_MISMATCH")


def api(path, binary=False):
    args = ["gh", "api", "repos/" + REPO + "/" + path]
    data = subprocess.check_output(args)
    return data if binary else json.loads(data)


def resolve_one(run_id, kind):
    require(re.fullmatch(r"[1-9][0-9]*", str(run_id)) is not None, "INVALID_RUN_ID")
    run = api("actions/runs/" + str(run_id))
    jobs = api(f"actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100")["jobs"]
    legacy = validate_run(run, jobs, kind)
    artifacts = api(f"actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    if legacy:
        name = f"windows-gate-reports-package-{run_id}-{run['run_attempt']}"
        candidates = [a for a in artifacts if a["name"] == name and not a["expired"]]
    else:
        name = f"{kind}-evidence-{run_id}-{run['run_attempt']}"
        candidates = [a for a in artifacts if a["name"] == name and not a["expired"]]
    require(len(candidates) == 1, "EVIDENCE_ARTIFACT_MISSING_OR_AMBIGUOUS")
    artifact = candidates[0]
    require(artifact["size_in_bytes"] < 20_000_000, "EVIDENCE_ARTIFACT_TOO_LARGE")
    raw = api(f"actions/artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        suffix = "source-checks-complete.json" if legacy else "evidence.json"
        files = [i for i in archive.infolist() if i.filename.split("/")[-1] == suffix]
        require(len(files) == 1 and files[0].file_size < 1_000_000, "EVIDENCE_REPORT_MISSING_OR_TOO_LARGE")
        report = json.loads(archive.read(files[0]))
    validate_report(report, run, kind, legacy)
    return {"run_id": str(run_id), "attempt": str(run["run_attempt"]), "commit": run["head_sha"],
            "artifact_id": artifact["id"], "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": report.get("scope", report.get("completed_suites")), "legacy": legacy}


def resolve(kind, requested=""):
    if requested:
        return resolve_one(requested, kind)
    runs = api(f"actions/workflows/release-evidence.yml/runs?branch={BRANCH}&per_page=30")["workflow_runs"]
    reasons = []
    for run in runs:
        if run["status"] != "completed":
            continue
        try:
            return resolve_one(str(run["id"]), kind)
        except ValueError as exc:
            reasons.append({"run": run["id"], "reason": str(exc)})
    raise ValueError(f"{kind.upper()}_EVIDENCE_MISSING: supply a passed run for unchanged inputs; no tests were started. {reasons[:3]}")


def issue(output, source_run=""):
    current = identity()
    # Check tooling first, before allocating a Windows builder or touching a receiver.
    tools = resolve("tooling")
    source = resolve("source", source_run)
    receipt = {"schema_version": 2, **current, "kind": "formal-source-gate", "source": source, "tooling": tools,
               "source_fingerprint": fingerprint("HEAD", "source"),
               "tooling_fingerprint": fingerprint("HEAD", "tooling")}
    Path(output).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"source_run": source["run_id"], "tooling_run": tools["run_id"], "tests_reexecuted": 0}))


def verify_local(path):
    current = identity()
    require(bool(path), "FORMAL_SOURCE_RECEIPT_REQUIRED")
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    require(receipt.get("schema_version") == 2 and receipt.get("kind") == "formal-source-gate", "INVALID_FORMAL_RECEIPT")
    require(all(receipt.get(k) == v for k, v in current.items()), "FORMAL_RECEIPT_IDENTITY_MISMATCH")
    for kind in ("source", "tooling"):
        require(receipt.get(kind + "_fingerprint") == fingerprint("HEAD", kind), "FORMAL_RECEIPT_INPUT_MISMATCH")
        require(receipt.get(kind, {}).get("artifact_id") and receipt[kind].get("scope"), "FORMAL_RECEIPT_EVIDENCE_MISSING")
    require(not git("status", "--porcelain", "--untracked-files=no").strip(), "FORMAL_SOURCE_DIRTY")
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["resolve", "verify", "tools"])
    parser.add_argument("--output")
    parser.add_argument("--source-run", default="")
    args = parser.parse_args()
    if args.command == "resolve":
        require(bool(args.output), "OUTPUT_REQUIRED")
        issue(args.output, args.source_run)
    elif args.command == "verify":
        verify_local(os.environ.get("CHEJIN_FORMAL_SOURCE_RECEIPT", ""))
        print("Verified source and tooling evidence; no source tests repeated")
    else:
        identity()
        print(json.dumps(resolve("tooling")))


if __name__ == "__main__":
    main()
