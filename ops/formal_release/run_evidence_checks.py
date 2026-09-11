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


def execute(command, cwd, path):
    with path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT / cwd, env={**os.environ, "PYTHONUTF8": "1",
                                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT / cwd)},
                                stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(path.read_text(encoding="utf-8")[-12000:])
    return result.returncode


def main():
    kind = os.environ["CHECK_KIND"]
    output = Path(os.environ["EVIDENCE_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    current = identity()
    if kind == "tooling":
        plan = {"reason": "Release tools changed; validate only release tooling", "not_tested": "Business behavior and Windows EXE acceptance",
                "pytest": [{"cwd": ".", "nodes": [str(p.relative_to(ROOT)) for p in sorted((ROOT / "ops/formal_release/tests").glob("test_*.py"))]}]}
    else:
        require(kind == "source", "UNKNOWN_CHECK_KIND")
        plan = json.loads(os.environ.get("SOURCE_TEST_PLAN", "{}"))
        require(plan.get("reason") and plan.get("not_tested") and plan.get("pytest"), "REVIEWED_SOURCE_TEST_PLAN_REQUIRED")
        require(set(plan) <= {"reason", "not_tested", "pytest"}, "UNSUPPORTED_TEST_PLAN_FIELD")
    scope, results = [], []
    grouped = {}
    for group in plan["pytest"]:
        require(set(group) == {"cwd", "nodes"}, "INVALID_TEST_GROUP")
        grouped.setdefault(group["cwd"], []).extend(group["nodes"])
    for index, (cwd, nodes) in enumerate(grouped.items()):
        nodes = selected_nodes(cwd, nodes)
        report = output / f"pytest-{index}.xml"
        code = execute([sys.executable, "-m", "pytest", *nodes, "-q", "--junitxml", str(report)], cwd, output / f"pytest-{index}.log")
        require(report.is_file(), "TEST_REPORT_MISSING")
        counts = junit(report)
        scope.append({"cwd": cwd, "nodes": nodes})
        results.append({"exit_code": code, **counts})
        require(code == 0 and counts["passed"] > 0 and counts["failed"] == 0 and counts["skipped"] == 0,
                "TESTS_FAILED_EMPTY_OR_SKIPPED: select the applicable tests; no completion evidence written")
    evidence = {"schema_version": 2, **current, "kind": kind, "status": "passed",
                "fingerprint": fingerprint("HEAD", kind), "scope": scope, "results": results,
                "reason": plan["reason"], "not_tested": plan["not_tested"], "python": sys.version}
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"kind": kind, "results": results, "scope": scope}))


if __name__ == "__main__":
    main()
