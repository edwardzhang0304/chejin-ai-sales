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
    require(any(j["name"] == "Build signed formal Windows package" and j["conclusion"] == "success"
                or j["name"] == "package" and j["conclusion"] == "success" for j in jobs), "PACKAGE_GATE_NOT_PASSED")
    eligible = [a for a in artifacts if a["name"].startswith("chejin-worker-v")
                and a["name"].endswith("-windows-x64-" + run["head_sha"]) and not a["expired"]]
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
