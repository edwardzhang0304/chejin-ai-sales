"""Sign one immutable, successful Hook build; never compile or change its ZIP."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

REPO = "edwardzhang0304/chejin-ai-sales"
ROOT = Path(__file__).resolve().parents[2]


def require(ok, code):
    if not ok:
        raise ValueError(code)


def api(path, binary=False):
    raw = subprocess.check_output(["gh", "api", f"repos/{REPO}/{path}"])
    return raw if binary else json.loads(raw)


def validate_build(run, artifacts, version):
    require(run.get("repository", {}).get("full_name") == REPO
            and run.get("head_branch") == "gray-release-0.10.x"
            and run.get("path") == ".github/workflows/hook-windows-build.yml"
            and run.get("conclusion") == "success"
            and run.get("status") == "completed", "UNTRUSTED_HOOK_BUILD")
    matches = [a for a in artifacts if a.get("name") == f"CheJinWorkerClient_V{version}"
               and not a.get("expired")]
    require(len(matches) == 1, "IMMUTABLE_ARTIFACT_REQUIRED")
    return matches[0]


def build_package_hash(run, artifacts, version):
    """Trusted small build artifact: signing needn't wait for server transfer."""
    matches = [a for a in artifacts if a.get('name') == 'hook-package-identity' and not a.get('expired')]
    require(len(matches) == 1 and matches[0]['size_in_bytes'] <= 100_000, 'BUILD_PACKAGE_IDENTITY_REQUIRED')
    item = matches[0]
    raw = api(f"actions/artifacts/{item['id']}/zip", binary=True)
    require(item.get('digest') == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'BUILD_IDENTITY_DIGEST_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        entries = z.infolist()
        require(len(entries) == 1 and entries[0].filename == 'package-identity.json'
                and entries[0].file_size <= 20_000, 'BUILD_IDENTITY_CONTENT_INVALID')
        identity = json.loads(z.read(entries[0]).decode('utf-8-sig'))
    require(identity.get('version') == version and identity.get('source_commit') == run['head_sha']
            and str(identity.get('build_run')) == str(run['id'])
            and re.fullmatch(r'[0-9a-f]{64}', identity.get('package_sha256', '')), 'BUILD_IDENTITY_MISMATCH')
    return identity['package_sha256']


def verify_package(raw, expected_sha, version, commit):
    require(hashlib.sha256(raw).hexdigest() == expected_sha, "PACKAGE_HASH_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest_bytes = archive.read("CheJinWorkerClient/update-package-manifest.json")
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        identity = json.loads(archive.read("CheJinWorkerClient/_internal/runtime-build-identity.json").decode("utf-8-sig"))
        keys_bytes = archive.read("CheJinWorkerClient/_internal/release-signing-public-keys.json")
    require(manifest.get("version") == version and manifest.get("git_commit") == commit
            and manifest.get("transport") == "hook"
            and identity.get("transport") == "hook"
            and identity.get("formal_release") is True
            and identity.get("git_commit") == commit, "PACKAGE_IDENTITY_MISMATCH")
    keys = json.loads(keys_bytes)
    key = {"key_id": os.environ["CHEJIN_RELEASE_SIGNING_KEY_ID"], "algorithm": "ed25519",
           "public_key_base64": os.environ["CHEJIN_RELEASE_SIGNING_PUBLIC_KEY_BASE64"]}
    require(key in keys.get("keys", []), "SIGNER_NOT_TRUSTED_BY_PACKAGE")
    # A mismatched Secret must fail before signing anything.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(
        os.environ["CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64"], validate=True))
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    require(base64.b64encode(public).decode() == key["public_key_base64"], "SIGNING_KEY_PAIR_MISMATCH")
    return manifest_bytes, keys_bytes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    require(re.fullmatch(r"[1-9][0-9]*", args.run), "INVALID_BUILD_RUN")
    require(re.fullmatch(r"\d+\.\d+\.\d+", args.version), "INVALID_VERSION")
    run = api(f"actions/runs/{args.run}")
    artifacts = api(f"actions/runs/{args.run}/artifacts?per_page=100")["artifacts"]
    artifact = validate_build(run, artifacts, args.version)
    if not args.sha256:
        args.sha256 = build_package_hash(run, artifacts, args.version)
    require(re.fullmatch(r"[0-9a-f]{64}", args.sha256), "INVALID_HASH")
    raw = api(f"actions/artifacts/{artifact['id']}/zip", binary=True)
    digest = hashlib.sha256(raw).hexdigest()
    require(artifact.get("digest") == "sha256:" + digest, "GITHUB_ARTIFACT_DIGEST_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(raw)) as outer:
        items = [i for i in outer.infolist() if i.filename == f"CheJinWorkerClient_V{args.version}.zip"]
        require(len(items) == 1, "INNER_PACKAGE_REQUIRED")
        package = outer.read(items[0])
    manifest, keys = verify_package(package, args.sha256, args.version, run["head_sha"])
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output.parent / "sealed-artifact.zip"
    archive.write_bytes(package)
    manifest_path = args.output / "update-package-manifest.json"
    manifest_path.write_bytes(manifest)
    (args.output / "public-keys.json").write_bytes(keys)
    subprocess.run([sys.executable, str(ROOT / "worker-client/scripts/sign-client-release.py"),
        "--archive", str(archive), "--package-manifest", str(manifest_path),
        "--version", args.version, "--git-commit", run["head_sha"], "--transport", "hook",
        "--artifact-storage-key", f"gray-hook/{args.version}/{args.sha256}.zip",
        "--published-at", datetime.now(timezone.utc).isoformat(),
        "--key-id", os.environ["CHEJIN_RELEASE_SIGNING_KEY_ID"],
        "--output", str(args.output / "release.json")], check=True)
    receipt = {"schema_version": 1, "operation": "sign_existing_hook", "build_run": args.run,
               "artifact_id": artifact["id"], "outer_sha256": digest, "package_sha256": args.sha256,
               "source_commit": run["head_sha"], "version": args.version, "rebuilt": False}
    (args.output / "publication-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
