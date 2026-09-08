"""Verify signed formal artifacts with an installed, previously shipped client verifier."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import zipfile

CHUNK = 8 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SUFFIXES = (".zip", ".release.json", ".delivery.json", ".sha256.txt")


def require(ok, code):
    if not ok:
        raise ValueError(code)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def identity(meta):
    require(isinstance(meta, dict), "INVALID_METADATA")
    for field, pattern in (("version", VERSION), ("current_version", VERSION),
                           ("commit", COMMIT), ("sha256", SHA)):
        require(isinstance(meta.get(field), str) and pattern.fullmatch(meta[field]), "INVALID_IDENTITY")
    require(str(meta.get("run_id", "")).isdigit(), "INVALID_RUN")
    stem = f"chejin-worker-v{meta['version']}-windows-x64"
    require(set(meta.get("files", {})) == {stem + suffix for suffix in SUFFIXES}, "INVALID_FILE_SET")
    total = 0
    for name, item in meta["files"].items():
        limit = 1024 ** 3 if name.endswith(".zip") else 128 * 1024
        require(type(item.get("size")) is int and 0 < item["size"] <= limit, "INVALID_SIZE")
        require(isinstance(item.get("sha256"), str) and SHA.fullmatch(item["sha256"]), "INVALID_HASH")
        total += item["size"]
    require(meta["files"][stem + ".zip"]["sha256"] == meta["sha256"], "ZIP_IDENTITY_MISMATCH")
    return stem, total


def client_api(config, current_version):
    # Paths and public keys are installed by operators, never supplied by CI artifacts.
    baseline = config["client_baselines"].get(current_version)
    require(baseline is not None, "OLD_CLIENT_BASELINE_NOT_INSTALLED")
    sys.path.insert(0, baseline)
    from chejin_worker_client import __version__ as baseline_version
    from chejin_worker_client.models import ClientRelease
    from chejin_worker_client import release_package_contract as contract
    require(baseline_version == current_version, "OLD_CLIENT_BASELINE_MISMATCH")
    return ClientRelease, contract


def check_descriptor(meta, desc, config):
    stem, _ = identity(meta)
    require(desc.get("git_commit") == meta["commit"] and desc.get("version") == meta["version"], "SOURCE_MISMATCH")
    require(desc.get("artifact_sha256") == meta["sha256"], "SIGNED_HASH_MISMATCH")
    require(desc.get("artifact_size_bytes") == meta["files"][stem + ".zip"]["size"], "SIGNED_SIZE_MISMATCH")
    require(desc.get("artifact_storage_key") == f"gray/windows-x64/{stem}.zip", "STORAGE_KEY_MISMATCH")
    require(desc.get("status") == "published", "INVALID_DESCRIPTOR_STATUS")
    model, contract = client_api(config, meta["current_version"])
    release = model.from_api({**desc, "update_available": True, "latest_version": desc["version"]})
    contract.validate_release_contract(release, current_version=meta["current_version"], require_download_url=False)
    contract.verify_release_signature(release, trusted_keys=contract.load_trusted_release_keys(Path(config["public_keys"])))
    return release, contract


def verify(folder, meta, config):
    stem, _ = identity(meta)
    for name, info in meta["files"].items():
        file = folder / name
        require(file.is_file() and not file.is_symlink(), "MISSING_ARTIFACT")
        require(file.stat().st_size == info["size"] and digest(file) == info["sha256"], "ARTIFACT_HASH_MISMATCH")
    desc = json.loads((folder / (stem + ".release.json")).read_text(encoding="utf-8-sig"))
    release, contract = check_descriptor(meta, desc, config)
    delivery = json.loads((folder / (stem + ".delivery.json")).read_text(encoding="utf-8-sig"))
    expected = {"version": meta["version"], "build_commit": meta["commit"],
                "default_api_base_url": config["api_origin"], "tests_status": "passed",
                "preflight_status": "passed", "vision_credential_embedded": False,
                "vision_credential_source": "worker_backend", "vision_configuration_locked": True,
                "vision_live_probe_check": "runtime_after_binding"}
    require(all(delivery.get(k) == v and type(delivery.get(k)) is type(v) for k, v in expected.items()), "DELIVERY_GATE_FAILED")
    require(delivery.get("upgrade_start_version") == meta["current_version"]
            and delivery.get("original_client_upgrade_check") == "passed"
            and isinstance(delivery.get("original_client_upgrade_report_sha256"), str)
            and SHA.fullmatch(delivery["original_client_upgrade_report_sha256"]), "ORIGINAL_CLIENT_UPGRADE_GATE_FAILED")
    require(str(delivery.get("workflow_run_id")) == str(meta["run_id"]), "RUN_ID_MISMATCH")
    require(str(delivery.get("zip_sha256", "")).lower() == meta["sha256"], "DELIVERY_HASH_MISMATCH")
    require((folder / (stem + ".sha256.txt")).read_text().strip().split() == [meta["sha256"], stem + ".zip"], "CHECKSUM_FILE_MISMATCH")
    marker = re.compile(rb"\bsk-[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    tls = {"CheJinWorkerClient/_internal/PySide6/Qt6Network.dll",
           "CheJinWorkerClient/_internal/PySide6/plugins/tls/qopensslbackend.dll",
           "CheJinWorkerClient/_internal/PySide6/plugins/tls/qschannelbackend.dll"}
    with tempfile.TemporaryDirectory(prefix="verify-", dir=folder) as temp, zipfile.ZipFile(folder / (stem + ".zip")) as archive:
        entries = archive.infolist()
        names = [i.filename.replace("\\", "/") for i in entries]
        require(len(names) == len(set(n.casefold() for n in names)), "DUPLICATE_ZIP_PATH")
        require(sum(i.file_size for i in entries) <= 3 * 1024 ** 3 and len(entries) <= 20000
                and all(i.file_size <= 512 * 1024 ** 2 for i in entries), "ZIP_LIMIT")
        for entry, name in zip(entries, names):
            path = PurePosixPath(name)
            require(path.parts and path.parts[0] == "CheJinWorkerClient" and ".." not in path.parts
                    and not path.is_absolute() and ":" not in name and not stat.S_ISLNK(entry.external_attr >> 16), "UNSAFE_ZIP_PATH")
            require(not any(p.rstrip(". ") != p for p in path.parts), "UNSAFE_WINDOWS_PATH")
            basename = path.name.lower()
            require(basename != "vision-runtime.json" and not basename.startswith(".env")
                    and not basename.endswith((".db", ".sqlite", ".sqlite3")), "PACKAGED_SECRET_OR_STATE")
            target = Path(temp).joinpath(*path.parts)
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, target.open("wb") as dest:
                shutil.copyfileobj(src, dest)
            content = target.read_bytes()
            for match in marker.finditer(content):
                require(name in tls and match.group().startswith(b"-----BEGIN")
                        and content[match.end():match.end() + 1] == b"\0", "PACKAGED_SECRET_SHAPE")
        package = Path(temp) / "CheJinWorkerClient"
        manifest = contract.verify_staged_package(release, package)
        for name, field in (("CheJinWorkerClient.exe", "exe_sha256"), ("CheJinUpdater.exe", "updater_exe_sha256")):
            require(digest(package / name) == delivery[field].lower(), "EXECUTABLE_HASH_MISMATCH")
        raw = (package / "_internal/contracts/c2_contract_v3.json").read_bytes()
        require(hashlib.sha256(raw).hexdigest() == delivery["c2_contract_sha256"].lower(), "CONTRACT_FILE_MISMATCH")
        require(json.loads(raw).get("contract_revision") == delivery.get("c2_contract_revision"), "CONTRACT_REVISION_MISMATCH")
        canonical = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        contract_sha = hashlib.sha256(canonical).hexdigest()
    return {"version": meta["version"], "commit": meta["commit"], "run_id": str(meta["run_id"]),
            "sha256": meta["sha256"], "size": release.artifact_size_bytes, "contract_sha256": contract_sha,
            "current_version": meta["current_version"], "contract_revision": delivery["c2_contract_revision"], "file_count": len(manifest["files"]), "package": "passed", "old_client_signature": "passed",
            "windows_upgrade": "pending", "business_uat": "pending"}
