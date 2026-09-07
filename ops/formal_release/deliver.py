#!/usr/bin/env python3
"""Run in a GitHub runner. SSH pinned host key; resume verified chunks, never log leases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from verify import CHUNK, SUFFIXES, digest, identity, require


def metadata(folder, current_version, run_id, commit):
    descriptors = list(folder.glob("*.release.json"))
    require(len(descriptors) == 1, "EXPECTED_ONE_DESCRIPTOR")
    desc = json.loads(descriptors[0].read_text(encoding="utf-8-sig"))
    stem = f"chejin-worker-v{desc['version']}-windows-x64"
    meta = {"version": desc["version"], "current_version": current_version, "run_id": str(run_id),
            "commit": commit, "sha256": desc["artifact_sha256"], "files": {}}
    for suffix in SUFFIXES:
        path = folder / (stem + suffix)
        require(path.is_file() and not path.is_symlink(), "MISSING_ARTIFACT")
        meta["files"][path.name] = {"size": path.stat().st_size, "sha256": digest(path)}
    identity(meta)
    return meta, desc


class Remote:
    def __init__(self, role):
        self.directory = tempfile.TemporaryDirectory(prefix="formal-ssh-")
        root = Path(self.directory.name)
        key = root / "key"
        key.write_text(os.environ["FORMAL_SSH_KEY"].strip() + "\n")
        key.chmod(0o600)
        hosts = root / "known_hosts"
        hosts.write_text(os.environ["FORMAL_KNOWN_HOSTS"].strip() + "\n")
        host = os.environ["FORMAL_SSH_HOST"]
        port = os.environ["FORMAL_SSH_PORT"]
        require(host and not host.startswith("-") and port.isdigit(), "INVALID_SSH_CONFIG")
        self.command = ["ssh", "-T", "-i", str(key), "-p", port, "-o", "BatchMode=yes",
                        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                        "-o", f"UserKnownHostsFile={hosts}", "-o", "ConnectTimeout=15",
                        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                        f"chejin-release@{host}", role]

    def __call__(self, request, body=b""):
        payload = json.dumps(request).encode() + b"\n" + body
        for attempt in range(5):
            try:
                process = subprocess.run(self.command, input=payload, capture_output=True, timeout=600)
                if process.returncode == 255:
                    raise ConnectionError("SSH_TRANSPORT_FAILED")
                require(process.returncode in (0, 1), "REMOTE_PROCESS_FAILED")
                result = json.loads(process.stdout)
                require(result.get("ok") is True, "REMOTE_REJECTED_" + str(result.get("error", "UNKNOWN")))
                return result
            except (ConnectionError, subprocess.TimeoutExpired):
                if attempt == 4:
                    raise
                print(f"Transport interrupted; retry {attempt + 1}/4", flush=True)
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("RETRY_EXHAUSTED")

    def close(self):
        self.directory.cleanup()


def stage(folder, meta, desc, remote):
    token = remote({"operation": "init", "metadata": meta, "descriptor": desc})["stage_id"]
    status = remote({"operation": "status", "stage_id": token})
    sent = skipped = 0
    if not status["sealed"]:
        for name in meta["files"]:
            with (folder / name).open("rb") as source:
                index = 0
                while block := source.read(CHUNK):
                    sha = hashlib.sha256(block).hexdigest()
                    if status["chunks"].get(name + f".part{index}") == sha:
                        skipped += 1
                    else:
                        remote({"operation": "chunk", "stage_id": token, "name": name, "index": index, "sha256": sha}, block)
                        sent += 1
                        print(f"Transferred {name} chunk {index + 1}", flush=True)
                    index += 1
    result = remote({"operation": "seal", "stage_id": token})
    return {**result, "sent_chunks": sent, "reused_chunks": skipped, "transfer": "passed"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_external(result, current_version, api_origin, download_origin):
    """One complete external download; URL remains in memory and is never printed."""
    opener = build_opener(NoRedirect())
    require(urlparse(api_origin).scheme == "https" and urlparse(download_origin).scheme == "https", "HTTPS_REQUIRED")
    def fetch(url):
        with opener.open(url, timeout=45) as response:
            require(response.status == 200, "HTTP_CHECK_FAILED")
            payload = response.read(2 * 1024 * 1024 + 1)
            require(len(payload) <= 2 * 1024 * 1024, "RESPONSE_TOO_LARGE")
            return json.loads(payload)
    origin = api_origin.removesuffix("/api")
    fetch(origin + "/healthz")
    fetch(origin + "/readyz")
    def query(version):
        return fetch(api_origin + "/client-releases/latest?" + urlencode({"current_version": version, "platform": "windows-x64", "channel": "gray"}))["data"]
    latest = query(current_version)
    require(latest.get("update_available") is True and latest.get("latest_version") == result["version"], "OLD_CLIENT_UPDATE_NOT_AVAILABLE")
    require(latest.get("artifact_sha256") == result["sha256"] and latest.get("git_commit") == result["commit"], "LIVE_IDENTITY_MISMATCH")
    parsed = urlparse(latest["artifact_url"])
    expected = urlparse(download_origin)
    require(parsed.scheme == "https" and parsed.netloc == expected.netloc and not parsed.username and not parsed.password, "DOWNLOAD_ORIGIN_MISMATCH")
    size = result["size"]
    require(latest.get("artifact_size_bytes") == size, "LIVE_SIZE_MISMATCH")
    ranges = []
    for start, end in ((0, 31), (size - 32, size - 1)):
        with opener.open(Request(latest["artifact_url"], headers={"Range": f"bytes={start}-{end}"}), timeout=45) as response:
            part = response.read(33)
            require(response.status == 206 and response.headers.get("Content-Range") == f"bytes {start}-{end}/{size}"
                    and len(part) == 32, "RANGE_FAILED")
            ranges.append(part)
    h = hashlib.sha256()
    length = 0
    first = last = b""
    with opener.open(latest["artifact_url"], timeout=60) as response:
        require(response.status == 200 and int(response.headers.get("Content-Length", -1)) == size, "FULL_DOWNLOAD_SIZE_MISMATCH")
        while block := response.read(CHUNK):
            length += len(block)
            require(length <= size, "DOWNLOAD_TOO_LARGE")
            h.update(block)
            first = (first + block)[:32]
            last = (last + block)[-32:]
    require(ranges == [first, last], "RANGE_BYTES_MISMATCH")
    require(length == size and h.hexdigest() == result["sha256"], "FULL_DOWNLOAD_HASH_MISMATCH")
    same = query(result["version"])
    require(same.get("update_available") is False and same.get("latest_version") == result["version"], "CURRENT_VERSION_QUERY_FAILED")
    return {**result, "backend": "passed", "old_client_discovery": "passed", "external_download": "passed"}


def save_summary(result, output):
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as file:
            file.write("### 正式包交付结果\n\n| 检查项 | 结果 |\n|---|---|\n")
            for key, value in result.items():
                # Receiver returns only fixed status/identity fields; never echo raw exceptions.
                file.write(f"| {key} | {value} |\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("preflight", "stage", "check", "publish", "verify-live"))
    parser.add_argument("--role", choices=("stage", "promote"), default="stage")
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--commit")
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--stage-id")
    parser.add_argument("--workers-drained", action="store_true")
    parser.add_argument("--result", type=Path, default=Path("formal-result.json"))
    args = parser.parse_args()
    remote = None
    result = {"operation": args.operation, "package": "not_run", "transfer": "not_run",
              "backend": "not_run", "publication": "not_run", "old_client_discovery": "not_run",
              "external_download": "not_run", "windows_upgrade": "pending", "business_uat": "pending"}
    try:
        if args.operation == "verify-live":
            result.update(json.loads(args.result.read_text()))
        else:
            remote = Remote(args.role if args.operation == "preflight" else "stage" if args.operation == "stage" else "promote")
            if args.operation == "preflight":
                result.update(remote({"operation": "preflight", "current_version": args.current_version}))
            elif args.operation == "stage":
                meta, desc = metadata(args.folder, args.current_version, args.run_id, args.commit)
                result.update(stage(args.folder, meta, desc, remote))
            else:
                require(args.workers_drained, "LOCAL_QUEUE_CONFIRMATION_REQUIRED")
                result.update(remote({"operation": args.operation, "stage_id": args.stage_id, "workers_drained": True, "current_version": args.current_version}))
        if args.operation in {"publish", "verify-live"}:
            result["external_download"] = "failed"
            result.update(verify_external(result, args.current_version, os.environ["FORMAL_API_ORIGIN"], os.environ["FORMAL_DOWNLOAD_ORIGIN"]))
        result["outcome"] = "passed"
    except Exception as exc:
        result["outcome"] = "failed"
        result["error_type"] = type(exc).__name__
        # URLs can contain leases; subprocess output may contain sensitive diagnostics.
        if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum():
            result["error_code"] = str(exc)
        raise SystemExit(1) from None
    finally:
        if remote:
            remote.close()
        save_summary(result, args.result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
