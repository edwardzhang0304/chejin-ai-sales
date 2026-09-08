"""Select only a completed formal package job, never a Fast UAT artifact."""
import json
import os
from pathlib import Path
import subprocess

from verify import require


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def select(run, jobs, artifacts):
    require(run["path"] == ".github/workflows/worker-windows-package.yml"
            and run["event"] == "workflow_dispatch" and run["head_branch"] == "codex/gray-release-0.9.x", "NOT_FORMAL_SOURCE")
    # New runs separate successful builds from acceptance. A saved candidate
    # must never become publishable merely because its build job succeeded.
    acceptance = [j for j in jobs if j["name"] == "Accept exact Windows candidate"]
    if acceptance:
        require(len(acceptance) == 1 and acceptance[0]["conclusion"] == "success", "ACCEPTANCE_NOT_PASSED")
    else:
        require(any(j["name"] in {"Build signed formal Windows package", "package"}
                    and j["conclusion"] == "success" for j in jobs), "PACKAGE_GATE_NOT_PASSED")
    suffix = "-windows-x64-" + run["head_sha"]
    if acceptance:
        suffix += "-" + str(run["run_attempt"])
    eligible = [a for a in artifacts if a["name"].startswith("chejin-worker-v")
                and a["name"].endswith(suffix) and not a["expired"]]
    require(len(eligible) == 1, "FORMAL_ARTIFACT_NOT_UNIQUE")
    return eligible[0]["id"], run["head_sha"]


def main():
    run_id = os.environ["FORMAL_RUN_ID"]
    require(run_id.isdigit(), "INVALID_RUN_ID")
    prefix = "repos/" + os.environ["GITHUB_REPOSITORY"] + "/actions/runs/" + run_id
    run = api(prefix)
    artifact_id, commit = select(run, api(prefix + "/jobs?per_page=100")["jobs"], api(prefix + "/artifacts?per_page=100")["artifacts"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"artifact_id={artifact_id}\ncommit={commit}\n")


if __name__ == "__main__":
    main()
