#!/usr/bin/env python3
"""Forced-command receiver. No shell, arbitrary paths, downloads, or artifact execution.

Install root-owned; sudoers permits only this entry point with a fixed role.
One bounded JSON line followed by an optional binary chunk is read from stdin.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from verify import CHUNK, SHA, check_descriptor, client_api, digest, identity, require, verify

CONFIG = Path("/etc/chejin-formal-release.json")


def write_json(path, payload):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as out:
        json.dump(payload, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def stage_id(meta):
    return hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_fixed(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=180, **kwargs)
    require(result.returncode == 0, "PRODUCTION_CHECK_OR_REGISTER_FAILED")
    return result.stdout


def require_publication_approval(folder, meta):
    # A reviewed operator receipt is local/root-owned, never taken from the artifact.
    from build_admin import validate_browser_receipt
    from maintenance import BLOCK, INCLUDE, SNIPPET, SITE, validate_ingress
    require(SNIPPET.exists() and SNIPPET.read_text() == BLOCK and INCLUDE in SITE.read_text(), "PERSISTENT_MAINTENANCE_REQUIRED")
    validate_ingress()
    approval = json.loads((folder / "production-approval.json").read_text())
    require(approval.get("version") == meta["version"] and approval.get("commit") == meta["commit"] and approval.get("sha256") == meta["sha256"], "PRODUCTION_APPROVAL_IDENTITY_MISMATCH")
    require(approval.get("button_upgrade_accepted") is True, "BUTTON_UPGRADE_NOT_ACCEPTED")
    require(approval.get("old_nginx_workers_drained") is True, "INGRESS_NOT_DRAINED")
    validate_browser_receipt(approval["admin_candidate"], approval["admin_browser_receipt"])


def publish(folder, meta, verified, config, check_only):
    require_publication_approval(folder, meta)
    require(run_fixed(["docker", "inspect", config["container"], "--format", "{{.State.Health.Status}}"], text=True).strip() == "healthy", "BACKEND_UNHEALTHY")
    stem, _ = identity(meta)
    # Only root-owned, verified files cross into the container. No script from the ZIP runs.
    target = "/tmp/chejin-formal-" + meta["sha256"]
    run_fixed(["docker", "exec", config["container"], "mkdir", "-p", target])
    for source, name in ((folder / (stem + ".release.json"), "release.json"),
                         (folder / (stem + ".zip"), "artifact.zip"),
                         (Path(config["public_keys"]), "public-keys.json")):
        run_fixed(["docker", "cp", str(source), config["container"] + ":" + target + "/" + name])
    try:
        output = run_fixed(["docker", "exec", "-i", config["container"], "python", "-",
                            target, verified["contract_revision"], verified["contract_sha256"],
                            "check" if check_only else "publish"],
                           input=Path(__file__).with_name("register.py").read_text(), text=True)
        result = json.loads(output)
        require(result.get("ok") is True, "REGISTRATION_FAILED")
        return {**verified, "backend": "passed", "publication": result["publication"],
                "workers_drained": "passed", "external_download": "not_run"}
    finally:
        run_fixed(["docker", "exec", config["container"], "rm", "-f",
                   target + "/release.json", target + "/artifact.zip", target + "/public-keys.json"])
        run_fixed(["docker", "exec", config["container"], "rmdir", target])


def handle(request, stream, role, config):
    operation = request.get("operation")
    allowed = {"init", "status", "chunk", "seal"} if role == "stage" else {"check", "publish"}
    if operation == "preflight":
        _, contract = client_api(config, request.get("current_version"))
        contract.load_trusted_release_keys(Path(config["public_keys"]))
        return {"receiver_version": 1, "old_client_baseline": "passed"}
    require(operation in allowed, "ROLE_DENIED")
    root = Path(config["staging_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (root / ".lock").open("a")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if operation == "init":
            meta = request["metadata"]
            _, total = identity(meta)
            check_descriptor(meta, request["descriptor"], config)
            token = stage_id(meta)
            folder = root / token
            if folder.exists():
                require(json.loads((folder / "metadata.json").read_text()) == meta, "IDENTITY_CONFLICT")
                return {"stage_id": token}
            used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            require(used + total <= config.get("staging_limit_bytes", 4 * 1024 ** 3), "STAGING_QUOTA")
            require(shutil.disk_usage(root).free > 4 * 1024 ** 3, "INSUFFICIENT_DISK")
            folder.mkdir(mode=0o700)
            write_json(folder / "metadata.json", meta)
            return {"stage_id": token}
        token = request.get("stage_id", "")
        require(isinstance(token, str) and SHA.fullmatch(token), "INVALID_STAGE_ID")
        folder = root / token
        meta = json.loads((folder / "metadata.json").read_text())
        stem, _ = identity(meta)
        require(token == stage_id(meta), "STAGE_IDENTITY_MISMATCH")
        if operation == "status":
            hashes = {}
            for name, info in meta["files"].items():
                for index in range((info["size"] + CHUNK - 1) // CHUNK):
                    path = folder / (name + f".part{index}")
                    if path.is_file():
                        hashes[name + f".part{index}"] = digest(path)
            return {"chunks": hashes, "sealed": (folder / "verified.json").is_file()}
        if operation == "chunk":
            require(not (folder / "verified.json").exists(), "STAGE_SEALED")
            name, index = request.get("name"), request.get("index")
            require(name in meta["files"] and type(index) is int and index >= 0, "INVALID_CHUNK")
            size = meta["files"][name]["size"]
            length = min(CHUNK, size - index * CHUNK)
            require(0 < length <= CHUNK, "INVALID_CHUNK_RANGE")
            data = stream.read(length + 1)
            require(len(data) == length and hashlib.sha256(data).hexdigest() == request.get("sha256"), "CHUNK_HASH_MISMATCH")
            require(shutil.disk_usage(root).free > 4 * 1024 ** 3, "INSUFFICIENT_DISK")
            used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            require(used + length <= config.get("staging_limit_bytes", 4 * 1024 ** 3), "STAGING_QUOTA")
            destination = folder / (name + f".part{index}")
            temp = destination.with_suffix(destination.suffix + ".tmp")
            with temp.open("wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, destination)
            return {"chunk": "stored"}
        if operation == "seal":
            require(shutil.disk_usage(root).free > meta["files"][stem + ".zip"]["size"] + 3 * 1024 ** 3, "INSUFFICIENT_DISK")
            if not (folder / "verified.json").exists():
                for name, info in meta["files"].items():
                    temp = folder / (name + ".assembling")
                    with temp.open("wb") as out:
                        for index in range((info["size"] + CHUNK - 1) // CHUNK):
                            with (folder / (name + f".part{index}")).open("rb") as block:
                                shutil.copyfileobj(block, out)
                        out.flush()
                        os.fsync(out.fileno())
                    require(temp.stat().st_size == info["size"] and digest(temp) == info["sha256"], "ASSEMBLY_HASH_MISMATCH")
                    os.replace(temp, folder / name)
            verified = verify(folder, meta, config)
            version_file = root / ("version-" + meta["version"] + ".json")
            if version_file.exists():
                require(json.loads(version_file.read_text())["sha256"] == meta["sha256"], "IMMUTABLE_VERSION_CONFLICT")
            write_json(version_file, {"sha256": meta["sha256"]})
            write_json(folder / "verified.json", verified)
            for path in folder.glob("*.part[0-9]*"):
                path.unlink()
            return {**verified, "stage_id": token, "publication": "not_run", "external_download": "not_run"}
        require((folder / "verified.json").exists(), "STAGE_NOT_VERIFIED")
        require(request.get("workers_drained") is True, "LOCAL_QUEUE_CONFIRMATION_REQUIRED")
        require(request.get("current_version") == meta["current_version"], "UPGRADE_START_MISMATCH")
        verified = verify(folder, meta, config)
        result = publish(folder, meta, verified, config, operation == "check")
        write_json(folder / "publication-result.json", result)
        return result


def main():
    os.umask(0o077)
    require(len(sys.argv) == 2 and sys.argv[1] in {"stage", "promote"}, "INVALID_ROLE")
    config = json.loads(CONFIG.read_text())
    request = json.loads(sys.stdin.buffer.readline(256 * 1024))
    result = handle(request, sys.stdin.buffer, sys.argv[1], config)
    with (Path(config["staging_root"]) / "audit.jsonl").open("a") as log:
        log.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "operation": request.get("operation"),
                              "stage_id": result.get("stage_id", request.get("stage_id")), "ok": True}) + "\n")
    print(json.dumps({"ok": True, **result}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not expose subprocess output, secrets, URLs, or business state.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z_]{1,100}", str(exc)) else type(exc).__name__
        try:
            config = json.loads(CONFIG.read_text())
            with (Path(config["staging_root"]) / "audit.jsonl").open("a") as log:
                log.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "ok": False, "error": code}) + "\n")
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": code}))
        sys.exit(1)
