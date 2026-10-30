"""Run an explicit engineering test selection, or the independent release-tool suite."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from source_evidence import ROOT, fingerprint, identity, require


def selected_nodes(cwd, nodes):
    require(cwd in {"worker-client", "backend", "."}, "INVALID_TEST_DIRECTORY")
    require(isinstance(nodes, list) and nodes, "EXPLICIT_TEST_NODES_REQUIRED")
    unique = []
    for node in nodes:
        require(isinstance(node, str) and not node.startswith("-") and "\\" not in node, "INVALID_TEST_NODE")
        file = node.split("::", 1)[0]
        require(file.endswith(".py") and ".." not in Path(file).parts and not Path(file).is_absolute()
                and Path(file).name.startswith("test_") and (ROOT / cwd / file).is_file(), "TEST_FILE_REQUIRED")
        if node not in unique:
            unique.append(node)
    # A file/class selector already includes its individual children.
    return [n for n in unique if not any(n.startswith(other + "::") for other in unique if other != n)]


def junit(path):
    cases = ET.parse(path).getroot().findall(".//testcase")
    failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    return {"passed": len(cases) - failed - skipped, "failed": failed, "skipped": skipped}


def execute(command, cwd, path, environment=None):
    with path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT / cwd, env={**os.environ, **(environment or {}), "PYTHONUTF8": "1",
                                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": os.pathsep.join([str(ROOT / cwd),str(ROOT / "backend"),str(ROOT / "backend/tests"),str(ROOT / "worker-client"),str(ROOT / "worker-client/tests"),str(ROOT / "worker-client/omniauto-rpa")])},
                                stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(path.read_text(encoding="utf-8")[-12000:])
    return result.returncode


def main():
    kind = os.environ["CHECK_KIND"]
    output = Path(os.environ["EVIDENCE_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    current = identity()
    tooling_reuse = None
    if kind == "tooling" and os.environ.get("TOOLING_REUSE_REQUIRED") == "true":
        require(bool(os.environ.get("TOOLING_HANDOFF")), "TOOLING_RESUME_HANDOFF_REQUIRED")
    if kind == "tooling" and os.environ.get("TOOLING_HANDOFF"):
        from tooling_handoff import validate as validate_tooling
        tooling_reuse = validate_tooling(os.environ["TOOLING_HANDOFF"])
        plan = {"reason": "Reviewed tool delta; preserve original check identities",
                "not_tested": "Unchanged business behavior; Windows component is a separate job",
                "pytest": tooling_reuse["pytest"]}
    elif kind == "tooling":
        plan = {"reason": "Release tools changed; validate only release tooling", "not_tested": "Business behavior and Windows EXE acceptance",
                "pytest": [{"cwd": ".", "nodes": [str(p.relative_to(ROOT)) for p in sorted((ROOT / "ops/formal_release/tests").glob("test_*.py"))]}]}
        plan['pytest'].append({'cwd':'worker-client','nodes':[
            'tests/test_packaging_scripts.py::PackagingScriptsTest::'+name for name in (
                'test_windows_ci_publishes_a_verified_portable_zip',
                'test_formal_update_package_contains_independent_updater_and_real_process_gate',
                'test_formal_exe_workflow_is_manual_and_reuses_evidence_without_fast_uat_package',
                'test_windows_package_ci_builds_and_probes_the_frozen_executable')]})
    else:
        require(kind == "source", "UNKNOWN_CHECK_KIND")
        plan = json.loads(os.environ.get("SOURCE_TEST_PLAN", "{}"))
        require(plan.get("reason") and plan.get("not_tested") and (plan.get("pytest") or plan.get("reviewed_handoff")), "REVIEWED_SOURCE_TEST_PLAN_REQUIRED")
        require(set(plan) <= {"reason", "not_tested", "pytest", "reviewed_handoff"}, "UNSUPPORTED_TEST_PLAN_FIELD")
    reused = None
    from release_profiles import selected_transport,validate_native_source_plan
    if selected_transport()=='hook' and kind=='source':
        validate_native_source_plan(plan,ROOT)
    if plan.get("reviewed_handoff"):
        from reviewed_handoff import validate
        reused = validate(plan["reviewed_handoff"])
    scope, results = [], []
    if tooling_reuse:
        scope.append({"check": "reviewed_unaffected_tool_checks", "original": tooling_reuse["original"],
                      "cases": tooling_reuse["cases"], "mode": "reused"})
        results.append({"exit_code": 0, "passed": tooling_reuse["count"], "failed": 0, "skipped": 0,
                        "type": "original_ci_evidence_reuse", "tests_executed": 0})
    if reused:
        scope.append({"check": "reviewed_source_identity_and_release_version", "business_tests_executed": 0})
        results.append({"exit_code": 0, "passed": 1, "failed": 0, "skipped": 0,
                        "type": "evidence_and_version_verification", "business_tests_executed": 0})
    grouped = {}
    for group in plan.get("pytest", []):
        require(set(group) == {"cwd", "nodes"}, "INVALID_TEST_GROUP")
        grouped.setdefault(group["cwd"], []).extend(group["nodes"])
    selections=[(cwd,selected_nodes(cwd,nodes)) for cwd,nodes in grouped.items()]
    if selected_transport()=='hook' and kind=='source':
        from native_test_environment import execution_groups
        executions=execution_groups(selections)
    else:
        executions=[{'cwd':cwd,'nodes':nodes,'env':{}} for cwd,nodes in selections]
    for index, group in enumerate(executions):
        cwd,nodes=group['cwd'],group['nodes']
        report = output / f"pytest-{index}.xml"
        code = execute([sys.executable, "-m", "pytest", *nodes, "-q", "--junitxml", str(report)], cwd, output / f"pytest-{index}.log", group["env"])
        require(report.is_file(), "TEST_REPORT_MISSING")
        counts = junit(report)
        scope.append({"cwd": cwd, "nodes": nodes, "report_xml": report.name,
                      **({"environment_profile":group["profile"]} if "profile" in group else {})})
        results.append({"exit_code": code, **counts})
        require(code == 0 and counts["passed"] > 0 and counts["failed"] == 0 and counts["skipped"] == 0,
                "TESTS_FAILED_EMPTY_OR_SKIPPED: select the applicable tests; no completion evidence written")
    evidence = {"schema_version": 2, **current, "kind": kind, "status": "passed",
                "fingerprint": fingerprint("HEAD", kind), "scope": scope, "results": results,
                "reason": plan["reason"], "not_tested": plan["not_tested"], "python": sys.version,
                "reviewed_handoff": reused, "tooling_reuse": tooling_reuse}
    if reused:
        evidence["scope"].append({"reused_engineering_checks": reused})
    (output / "evidence.json").write_text(json.dumps(evidence, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"kind": kind, "results": results, "scope": scope}))


if __name__ == "__main__":
    main()
