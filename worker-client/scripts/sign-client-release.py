from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def canonical_utc_timestamp(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("published timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SystemExit("published timestamp must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(payload: dict[str, object]) -> bytes:
    signed = {
        "schema_version": 1,
        "version": payload["version"],
        "channel": payload["channel"],
        "platform": payload["platform"],
        "artifact_size_bytes": payload["artifact_size_bytes"],
        "artifact_sha256": payload["artifact_sha256"],
        "git_commit": payload["git_commit"],
        "package_manifest_sha256": payload["package_manifest_sha256"],
        "published_at": canonical_utc_timestamp(payload["published_at"]),
        "minimum_updater_version": payload["minimum_updater_version"],
        "rollback_safe": payload["rollback_safe"],
    }
    return json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--package-manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--artifact-storage-key", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key-env", default="CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64")
    args = parser.parse_args()

    version = args.version.strip()
    commit = args.git_commit.strip().lower()
    if not VERSION_RE.fullmatch(version) or not GIT_COMMIT_RE.fullmatch(commit):
        raise SystemExit("version or git commit is invalid")
    storage_key = str(args.artifact_storage_key or "").strip().replace("\\", "/")
    if (
        not storage_key
        or storage_key.startswith("/")
        or ".." in Path(storage_key).parts
        or not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in storage_key.split("/"))
    ):
        raise SystemExit("artifact storage key is invalid")
    try:
        private_bytes = base64.b64decode(
            str(os.environ.get(args.private_key_env) or ""), validate=True
        )
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except (TypeError, ValueError) as exc:
        raise SystemExit("release signing private key is missing or invalid") from exc

    payload: dict[str, object] = {
        "schema_version": 1,
        "channel": "gray",
        "platform": "windows-x64",
        "version": version,
        "status": "published",
        "artifact_storage_key": storage_key,
        "artifact_size_bytes": args.archive.stat().st_size,
        "artifact_sha256": hash_file(args.archive),
        "git_commit": commit,
        "package_manifest_sha256": hash_file(args.package_manifest),
        "published_at": canonical_utc_timestamp(args.published_at),
        "release_notes": "",
        "minimum_updater_version": "0.9.59",
        "rollback_safe": True,
        "signature_key_id": args.key_id.strip(),
    }
    if not SHA256_RE.fullmatch(str(payload["artifact_sha256"])) or not SHA256_RE.fullmatch(str(payload["package_manifest_sha256"])):
        raise SystemExit("release hashes are invalid")
    payload["manifest_signature"] = base64.b64encode(
        private_key.sign(canonical(payload))
    ).decode("ascii")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
