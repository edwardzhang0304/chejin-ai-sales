from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class BuildSourceError(RuntimeError):
    pass


def resolve_contract_path(client_root: Path) -> Path:
    root = client_root.resolve()
    candidates = (
        root / "contracts" / "c2_contract_v3.json",
        root.parent / "contracts" / "c2_contract_v3.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildSourceError("C2_CONTRACT_NOT_FOUND")


def _git(client_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=client_root,
        capture_output=True,
        text=True,
        check=False,
    )


def verify_build_source(
    client_root: Path,
    *,
    development_build: bool,
) -> dict[str, object]:
    root = client_root.resolve()
    expected_repository_root = root.parent
    contract_path = resolve_contract_path(root)
    repository_result = _git(root, "rev-parse", "--show-toplevel")
    commit_result = _git(root, "rev-parse", "HEAD")
    status_result = _git(root, "status", "--porcelain")
    branch_result = _git(root, "branch", "--show-current")
    controlled_path = "worker-client/scripts/build_source.py"
    tracked_result = _git(
        root,
        "ls-files",
        "--error-unmatch",
        "--",
        f":(top){controlled_path}",
    )
    committed_result = _git(
        root,
        "cat-file",
        "-e",
        f"HEAD:{controlled_path}",
    )
    commit = (
        commit_result.stdout.strip()
        if commit_result.returncode == 0
        else ""
    )
    repository_root = (
        Path(repository_result.stdout.strip()).resolve()
        if repository_result.returncode == 0
        and repository_result.stdout.strip()
        else None
    )
    git_available = bool(
        COMMIT_PATTERN.fullmatch(commit)
        and repository_root == expected_repository_root
        and status_result.returncode == 0
        and branch_result.returncode == 0
        and tracked_result.returncode == 0
        and committed_result.returncode == 0
    )
    if not git_available and not development_build:
        raise BuildSourceError("OFFICIAL_BUILD_GIT_SOURCE_REQUIRED")
    branch = branch_result.stdout.strip() if git_available else ""
    detached = bool(git_available and not branch)
    return {
        "git_available": git_available,
        "git_commit": commit if git_available else "",
        "git_branch": "DETACHED" if detached else branch,
        "git_detached": detached,
        "git_repository_root": (
            str(repository_root) if git_available else ""
        ),
        "git_dirty": (
            bool(status_result.stdout.strip()) if git_available else True
        ),
        "contract_path": str(contract_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--development-build", action="store_true")
    args = parser.parse_args()
    try:
        result = verify_build_source(
            args.root,
            development_build=args.development_build,
        )
    except BuildSourceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
