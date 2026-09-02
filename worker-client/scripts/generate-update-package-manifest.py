from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MANIFEST_NAME = "update-package-manifest.json"


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.package_root.resolve(strict=True)
    if not VERSION_RE.fullmatch(args.version.strip()):
        raise SystemExit("version must be exact major.minor.patch")
    if not GIT_COMMIT_RE.fullmatch(args.git_commit.strip().lower()):
        raise SystemExit("git commit must be a full SHA-1")
    for required in ("CheJinWorkerClient.exe", "CheJinUpdater.exe"):
        if not (root / required).is_file():
            raise SystemExit(f"required executable missing: {required}")
    files = {
        path.relative_to(root).as_posix(): hash_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if not files or any(not SHA256_RE.fullmatch(value) for value in files.values()):
        raise SystemExit("package file hash inventory is invalid")
    payload = {
        "schema_version": 1,
        "version": args.version.strip(),
        "platform": "windows-x64",
        "git_commit": args.git_commit.strip().lower(),
        "rollback_safe": True,
        "files": files,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, args.output)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(args.output),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "file_count": len(files),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
