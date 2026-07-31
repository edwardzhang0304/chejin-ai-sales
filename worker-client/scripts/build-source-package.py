from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from build_policy import validate_build_policy
from build_source import verify_build_source
from client_delivery_policy import (
    is_client_forbidden_path,
    load_client_exclude_paths,
)
from omniauto_tree import load_source_provenance, tree_manifest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
DELIVERABLES = PROJECT_ROOT / "deliverables"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".visual-venv",
    ".visual-venv312",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "build",
    "cache",
    "dist",
    "runtime",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip"}
EXCLUDE_NAMES = {
    ".DS_Store",
    "add_friend_menu_entry_after_click_window_annotated.png",
}
EXCLUDE_PATTERNS = {
    ".env",
    "*.local.env",
}
ALLOWED_DATA_PREFIXES = (
    "omniauto-rpa/apps/wechat_ai_customer_service/data/tenants/chejin/product_master/",
    "omniauto-rpa/apps/wechat_ai_customer_service/data/tenants/chejin/rag_index/",
)
OMNIAUTO_ROOT = ROOT / "omniauto-rpa"
OMNIAUTO_CLIENT_EXCLUDES = load_client_exclude_paths(OMNIAUTO_ROOT)


def _version_label() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "chejin_worker_client" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"]).rsplit(".", 1)[0]


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    rel_name = rel.as_posix()
    if rel_name.startswith("omniauto-rpa/") and is_client_forbidden_path(
        rel_name[len("omniauto-rpa/") :],
        OMNIAUTO_CLIENT_EXCLUDES,
    ):
        return True
    if "data" in parts and not any(rel_name.startswith(prefix) for prefix in ALLOWED_DATA_PREFIXES):
        return True
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    if path.name in EXCLUDE_NAMES:
        return True
    if any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDE_PATTERNS):
        return True
    if path.suffix == ".env":
        return True
    return False


def _iter_files() -> list[Path]:
    return sorted((item for item in ROOT.rglob("*") if item.is_file() and not _is_excluded(item)), key=lambda item: item.relative_to(ROOT).as_posix())


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_contract() -> tuple[dict[str, object], str]:
    payload = json.loads((PROJECT_ROOT / "contracts" / "c2_contract_v3.json").read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload, _sha256_bytes(canonical)


def _zip_member_sha256(zip_path: Path, member_name: str) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        return _sha256_bytes(archive.read(member_name))


def _forbidden_entries(names: list[str]) -> list[str]:
    forbidden: list[str] = []
    for name in names:
        rel = Path(name)
        if name.startswith("worker-client/"):
            rel = Path(name).relative_to("worker-client")
        parts = rel.parts
        base = parts[-1] if parts else ""
        rel_name = rel.as_posix()
        if rel_name.startswith("omniauto-rpa/") and is_client_forbidden_path(
            rel_name[len("omniauto-rpa/") :],
            OMNIAUTO_CLIENT_EXCLUDES,
        ):
            forbidden.append(name)
            continue
        if any(part in EXCLUDE_DIRS for part in parts):
            forbidden.append(name)
            continue
        if any(fnmatch.fnmatch(base, pattern) for pattern in EXCLUDE_PATTERNS):
            forbidden.append(name)
            continue
        if Path(base).suffix in EXCLUDE_SUFFIXES:
            forbidden.append(name)
            continue
        if Path(base).suffix == ".env":
            forbidden.append(name)
            continue
    return forbidden


def _zip_omniauto_manifest(zip_path: Path) -> dict[str, object]:
    prefix = "worker-client/omniauto-rpa/"
    entries: list[dict[str, object]] = []
    digest = hashlib.sha256()
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(
            item.filename
            for item in archive.infolist()
            if not item.is_dir() and item.filename.startswith(prefix)
        ):
            relative = name[len(prefix) :]
            payload = archive.read(name)
            file_hash = hashlib.sha256(payload).hexdigest()
            entries.append({"path": relative, "sha256": file_hash, "bytes": len(payload)})
            encoded_path = relative.encode("utf-8")
            digest.update(len(encoded_path).to_bytes(4, "big"))
            digest.update(encoded_path)
            digest.update(bytes.fromhex(file_hash))
    return {
        "tree_sha256": digest.hexdigest(),
        "file_count": len(entries),
        "files": entries,
    }


def build(
    *,
    version: str,
    date: str,
    tests_status: str = "not_run",
    preflight_status: str = "not_run",
    development_build: bool = False,
) -> dict[str, object]:
    DELIVERABLES.mkdir(parents=True, exist_ok=True)
    build_source = verify_build_source(
        ROOT,
        development_build=development_build,
    )
    git_dirty = bool(build_source["git_dirty"])
    validate_build_policy(
        git_dirty=git_dirty,
        skip_tests=tests_status != "passed",
        skip_preflight=preflight_status != "passed",
        development_build=development_build,
    )
    zip_path = DELIVERABLES / f"chejin-worker-client-{date}-v{version}.zip"
    manifest_path = DELIVERABLES / f"chejin-worker-client-{date}-v{version}.manifest.json"
    files = _iter_files()
    generated_check = subprocess.run(
        ["python3", "scripts/generate-c2-observation-schema.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if generated_check.returncode:
        raise SystemExit(generated_check.stdout + generated_check.stderr)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, "worker-client/" + path.relative_to(ROOT).as_posix())
        archive.write(
            PROJECT_ROOT / "contracts" / "c2_contract_v3.json",
            "worker-client/contracts/c2_contract_v3.json",
        )
        archive.write(
            PROJECT_ROOT
            / "contracts"
            / "examples"
            / "c2_v3_mixed_roundtrip.json",
            "worker-client/contracts/examples/c2_v3_mixed_roundtrip.json",
        )
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    forbidden = _forbidden_entries(names)
    contract, contract_fingerprint = _canonical_contract()
    source_contract_path = Path(str(build_source["contract_path"]))
    source_contract_sha256 = _sha256_bytes(source_contract_path.read_bytes())
    packaged_contract_path = "worker-client/contracts/c2_contract_v3.json"
    packaged_contract_sha256 = _zip_member_sha256(zip_path, packaged_contract_path)
    if source_contract_sha256 != packaged_contract_sha256:
        raise SystemExit("C2_CONTRACT_FILE_MISMATCH")
    omniauto_root = OMNIAUTO_ROOT
    omniauto_source_tree = tree_manifest(omniauto_root, client_delivery=True)
    omniauto_packaged_tree = _zip_omniauto_manifest(zip_path)
    if omniauto_source_tree["tree_sha256"] != omniauto_packaged_tree["tree_sha256"]:
        raise SystemExit("OMNIAUTO_TREE_MISMATCH")
    omniauto_provenance = load_source_provenance(omniauto_root)
    generated_schema = (
        ROOT
        / "omniauto-rpa"
        / "apps"
        / "wechat_ai_customer_service"
        / "adapters"
        / "chejin_c2_observation_schema.generated.json"
    )
    manifest = {
        "ok": not forbidden,
        "version": version,
        "zip_path": str(zip_path.resolve()),
        "sha256": sha256,
        "bytes": zip_path.stat().st_size,
        "file_count": len(names),
        "top_level": sorted({name.split("/", 1)[0] for name in names if name}),
        "bad_top_level_entries": sorted({name for name in names if not name.startswith("worker-client/")}),
        "forbidden_entries": forbidden,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "git_commit": str(build_source["git_commit"]),
            "git_branch": str(build_source["git_branch"]),
            "git_dirty": git_dirty,
            "build_kind": "development" if development_build else "official",
            "formal_release": not development_build,
            "omniauto_upstream_base_commit": omniauto_provenance[
                "upstream_base_commit"
            ],
            "omniauto_selective_integrations": omniauto_provenance[
                "selective_integrations"
            ],
            "omniauto_chejin_integration_commit": omniauto_provenance[
                "chejin_integration_commit"
            ],
            "omniauto_tree_sha256": omniauto_source_tree["tree_sha256"],
            "omniauto_file_count": omniauto_source_tree["file_count"],
            "packaged_omniauto_tree_sha256": omniauto_packaged_tree["tree_sha256"],
        },
        "contract": {
            "contract_version": int(contract.get("contract_version") or 0),
            "contract_revision": str(contract.get("contract_revision") or ""),
            # contract_sha256 is always the exact packaged file digest so an
            # operator can verify it directly after extracting the ZIP.
            "contract_sha256": packaged_contract_sha256,
            "contract_path": packaged_contract_path,
            "source_contract_sha256": source_contract_sha256,
            "canonical_contract_sha256": contract_fingerprint,
            "generated_observation_schema_sha256": hashlib.sha256(
                generated_schema.read_bytes()
            ).hexdigest(),
        },
        "verification": {
            "generated_schema_check": "passed",
            "tests_status": tests_status,
            "preflight_status": preflight_status,
            "omniauto_tree_check": "passed",
            "contract_file_check": "passed",
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if forbidden or manifest["bad_top_level_entries"]:
        raise SystemExit(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=_version_label())
    parser.add_argument("--date", default=dt.datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--tests-status", choices=("passed", "not_run"), default="not_run")
    parser.add_argument("--preflight-status", choices=("passed", "not_run"), default="not_run")
    parser.add_argument("--development-build", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                version=args.version,
                date=args.date,
                tests_status=args.tests_status,
                preflight_status=args.preflight_status,
                development_build=args.development_build,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
