from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
from pathlib import Path
import zipfile


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
    "data",
    "dist",
    "runtime",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip"}
EXCLUDE_NAMES = {".DS_Store"}
EXCLUDE_PATTERNS = {
    ".env",
    "*.local.env",
}


def _version_label() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "chejin_worker_client" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"]).rsplit(".", 1)[0]


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    if path.name in EXCLUDE_NAMES:
        return True
    if any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDE_PATTERNS):
        return True
    if path.suffix == ".env" and not path.name.endswith(".example.env"):
        return True
    return False


def _iter_files() -> list[Path]:
    return sorted((item for item in ROOT.rglob("*") if item.is_file() and not _is_excluded(item)), key=lambda item: item.relative_to(ROOT).as_posix())


def _forbidden_entries(names: list[str]) -> list[str]:
    forbidden: list[str] = []
    for name in names:
        rel = Path(name)
        if name.startswith("worker-client/"):
            rel = Path(name).relative_to("worker-client")
        parts = rel.parts
        base = parts[-1] if parts else ""
        if any(part in EXCLUDE_DIRS for part in parts):
            forbidden.append(name)
            continue
        if any(fnmatch.fnmatch(base, pattern) for pattern in EXCLUDE_PATTERNS):
            forbidden.append(name)
            continue
        if Path(base).suffix in EXCLUDE_SUFFIXES:
            forbidden.append(name)
            continue
        if Path(base).suffix == ".env" and not base.endswith(".example.env"):
            forbidden.append(name)
            continue
    return forbidden


def build(*, version: str, date: str) -> dict[str, object]:
    DELIVERABLES.mkdir(parents=True, exist_ok=True)
    zip_path = DELIVERABLES / f"chejin-worker-client-{date}-v{version}.zip"
    manifest_path = DELIVERABLES / f"chejin-worker-client-{date}-v{version}.manifest.json"
    files = _iter_files()
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, "worker-client/" + path.relative_to(ROOT).as_posix())
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    forbidden = _forbidden_entries(names)
    manifest = {
        "ok": not forbidden,
        "zip_path": str(zip_path.resolve()),
        "sha256": sha256,
        "bytes": zip_path.stat().st_size,
        "file_count": len(names),
        "top_level": sorted({name.split("/", 1)[0] for name in names if name}),
        "bad_top_level_entries": sorted({name for name in names if not name.startswith("worker-client/")}),
        "forbidden_entries": forbidden,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if forbidden or manifest["bad_top_level_entries"]:
        raise SystemExit(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=_version_label())
    parser.add_argument("--date", default=dt.datetime.now().strftime("%Y%m%d"))
    args = parser.parse_args()
    print(json.dumps(build(version=args.version, date=args.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
