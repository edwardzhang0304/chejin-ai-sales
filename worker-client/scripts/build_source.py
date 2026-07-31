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
    contract_path = resolve_contract_path(root)
    commit_result = _git(root, "rev-parse", "HEAD")
    status_result = _git(root, "status", "--porcelain")
    branch_result = _git(root, "branch", "--show-current")
    commit = (
        commit_result.stdout.strip()
        if commit_result.returncode == 0
        else ""
    )
    git_available = bool(
        COMMIT_PATTERN.fullmatch(commit)
        and status_result.returncode == 0
        and branch_result.returncode == 0
    )
    if not git_available and not development_build:
        raise BuildSourceError("OFFICIAL_BUILD_GIT_SOURCE_REQUIRED")
    return {
        "git_available": git_available,
        "git_commit": commit if git_available else "",
        "git_branch": (
            branch_result.stdout.strip() if git_available else ""
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
