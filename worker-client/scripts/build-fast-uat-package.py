from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

from build_source import BuildSourceError, verify_build_source
from client_delivery_policy import (
    is_client_forbidden_path,
    is_client_runtime_junk_path,
    load_client_exclude_paths,
)
from omniauto_tree import load_source_provenance, tree_manifest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
OMNIAUTO_ROOT = ROOT / "omniauto-rpa"
ALLOWED_DATA_PREFIXES = (
    "apps/wechat_ai_customer_service/data/tenants/chejin/product_master/",
    "apps/wechat_ai_customer_service/data/tenants/chejin/rag_index/",
)
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "build",
    "cache",
    "dist",
    "runtime",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "chejin_worker_client" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"])


def _copy_worker_app(destination: Path) -> None:
    source = ROOT / "chejin_worker_client"
    for path in source.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(source)
        target = destination / "chejin_worker_client" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _copy_omniauto(destination: Path) -> None:
    excludes = load_client_exclude_paths(OMNIAUTO_ROOT)
    for path in OMNIAUTO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(OMNIAUTO_ROOT)
        relative_name = relative.as_posix()
        if is_client_runtime_junk_path(relative_name):
            continue
        if is_client_forbidden_path(relative_name, excludes):
            continue
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if "data" in relative.parts and not any(
            relative_name.startswith(prefix) for prefix in ALLOWED_DATA_PREFIXES
        ):
            continue
        if path.suffix in {".pyc", ".pyo", ".zip", ".env"}:
            continue
        if path.name == ".env" or path.name.endswith(".local.env"):
            continue
        target = destination / "omniauto-rpa" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _tree_hash(root: Path, *, excluded_names: set[str] | None = None) -> tuple[str, int]:
    excluded = excluded_names or set()
    digest = hashlib.sha256()
    count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name not in excluded):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        file_hash = bytes.fromhex(_sha256(path))
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(file_hash)
        count += 1
    return digest.hexdigest(), count


def build(*, runtime_root: Path, output_dir: Path, git_commit: str, git_branch: str) -> dict[str, object]:
    if not runtime_root.is_dir() or not (runtime_root / "python.exe").is_file():
        raise SystemExit("FAST_UAT_RUNTIME_BASE_INVALID")
    if len(git_commit) != 40 or any(char not in "0123456789abcdefABCDEF" for char in git_commit):
        raise SystemExit("FAST_UAT_GIT_COMMIT_INVALID")
    try:
        build_source = verify_build_source(ROOT, development_build=False)
    except BuildSourceError as exc:
        raise SystemExit(str(exc)) from exc
    if str(build_source["git_commit"]).lower() != git_commit.lower():
        raise SystemExit("FAST_UAT_GIT_COMMIT_MISMATCH")
    if bool(build_source["git_dirty"]):
        raise SystemExit("FAST_UAT_GIT_DIRTY")
    vision_key = str(os.environ.get("CHEJIN_VISION_CLIENT_API_KEY") or "").strip()
    if not vision_key:
        raise SystemExit("FAST_UAT_VISION_KEY_REQUIRED")

    package_root = output_dir / "CheJinWorkerDebug"
    if package_root.exists():
        shutil.rmtree(package_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(runtime_root, package_root / "runtime")
    app_root = package_root / "app"
    app_root.mkdir(parents=True)
    _copy_worker_app(app_root)
    _copy_omniauto(app_root)
    shutil.copytree(PROJECT_ROOT / "contracts", app_root / "contracts")
    shutil.copy2(ROOT / "packaging" / "start-fast-uat.ps1", package_root / "start-fast-uat.ps1")
    shutil.copy2(ROOT / "packaging" / "collect-uat-evidence.ps1", package_root / "collect-uat-evidence.ps1")
    shutil.copy2(ROOT / "packaging" / "collect_uat_evidence.py", package_root / "collect_uat_evidence.py")

    provenance = load_source_provenance(OMNIAUTO_ROOT)
    omniauto_source = tree_manifest(OMNIAUTO_ROOT, client_delivery=True)
    identity = {
        "schema_version": 1,
        "version": _version(),
        "git_commit": git_commit,
        "git_branch": git_branch,
        "git_dirty": False,
        "build_kind": "debug_uat_locked",
        "formal_release": False,
        "distribution_channel": "windows_fast_debug_zip",
        "debug_uat": True,
        "vision_configuration_locked": True,
        "omniauto_upstream_base_commit": provenance["upstream_base_commit"],
        "omniauto_tree_sha256": omniauto_source["tree_sha256"],
    }
    (app_root / "runtime-build-identity.json").write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (app_root / "vision-runtime.json").write_text(
        json.dumps({"schema_version": 1, "vision_api_key": vision_key}, separators=(",", ":")),
        encoding="utf-8",
    )
    app_tree_sha256, app_file_count = _tree_hash(
        app_root,
        excluded_names={"vision-runtime.json"},
    )
    runtime_identity = json.loads(
        (runtime_root / "fast-uat-runtime-base.json").read_text(encoding="utf-8-sig")
    )
    manifest = {
        **identity,
        "not_for_customer_release": True,
        "runtime_base": runtime_identity,
        "app_tree_sha256_without_secret": app_tree_sha256,
        "app_file_count_without_secret": app_file_count,
        "omniauto_source": provenance,
        "tests_status": "passed",
    }
    manifest_path = package_root / "fast-uat-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if vision_key in manifest_path.read_text(encoding="utf-8"):
        raise SystemExit("FAST_UAT_MANIFEST_SECRET_LEAK")

    zip_path = output_dir / f"chejin-worker-fast-uat-v{_version()}-{git_commit[:12]}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(output_dir).as_posix())
    result = {
        "ok": True,
        "zip_path": str(zip_path.resolve()),
        "zip_sha256": _sha256(zip_path),
        "zip_bytes": zip_path.stat().st_size,
        "manifest_path": str(manifest_path.resolve()),
        "version": _version(),
        "git_commit": git_commit,
        "omniauto_tree_sha256": omniauto_source["tree_sha256"],
        "debug_uat": True,
        "formal_release": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--git-branch", required=True)
    args = parser.parse_args()
    build(
        runtime_root=args.runtime_root.resolve(),
        output_dir=args.output_dir.resolve(),
        git_commit=args.git_commit,
        git_branch=args.git_branch,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
