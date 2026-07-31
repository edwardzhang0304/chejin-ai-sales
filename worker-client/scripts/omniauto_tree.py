from __future__ import annotations

import hashlib
import json
from pathlib import Path

from client_delivery_policy import (
    is_client_forbidden_path,
    load_client_exclude_paths,
)


EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "cache",
    "runtime",
    "build",
    "dist",
}
ALLOWED_DATA_PREFIXES = (
    "apps/wechat_ai_customer_service/data/tenants/chejin/product_master/",
    "apps/wechat_ai_customer_service/data/tenants/chejin/rag_index/",
)


def include_file(
    root: Path,
    path: Path,
    *,
    client_exclude_paths: tuple[str, ...] = (),
) -> bool:
    rel = path.relative_to(root)
    rel_name = rel.as_posix()
    if client_exclude_paths and is_client_forbidden_path(
        rel_name,
        client_exclude_paths,
    ):
        return False
    if any(part in EXCLUDED_PARTS for part in rel.parts):
        return False
    if "data" in rel.parts and not any(
        rel_name.startswith(prefix) for prefix in ALLOWED_DATA_PREFIXES
    ):
        return False
    if path.suffix in {".pyc", ".pyo", ".zip", ".env"}:
        return False
    if path.name == ".env" or path.name.endswith(".local.env"):
        return False
    return True


def tree_manifest(
    root: Path,
    *,
    client_delivery: bool = False,
) -> dict[str, object]:
    resolved = root.resolve()
    client_exclude_paths = (
        load_client_exclude_paths(resolved) if client_delivery else ()
    )
    files = sorted(
        (
            path
            for path in resolved.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and include_file(
                resolved,
                path,
                client_exclude_paths=client_exclude_paths,
            )
        ),
        key=lambda path: path.relative_to(resolved).as_posix(),
    )
    entries: list[dict[str, object]] = []
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(resolved).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        entries.append({"path": relative, "sha256": file_hash, "bytes": size})
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(file_hash))
    return {
        "tree_sha256": digest.hexdigest(),
        "file_count": len(entries),
        "files": entries,
    }


def load_source_provenance(root: Path) -> dict[str, object]:
    path = root / ".chejin-source.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    base_commit = str(payload.get("upstream_base_commit") or "").strip()
    if len(base_commit) != 40:
        raise ValueError("OMNIAUTO_UPSTREAM_BASE_COMMIT_INVALID")
    integration_commit = str(
        payload.get("chejin_integration_commit") or ""
    ).strip()
    if len(integration_commit) != 40:
        raise ValueError("OMNIAUTO_CHEJIN_INTEGRATION_COMMIT_INVALID")
    integrations = payload.get("selective_integrations")
    if not isinstance(integrations, list) or not integrations:
        raise ValueError("OMNIAUTO_SELECTIVE_INTEGRATIONS_REQUIRED")
    for item in integrations:
        if not isinstance(item, dict):
            raise ValueError("OMNIAUTO_SELECTIVE_INTEGRATION_INVALID")
        source_commit = str(item.get("source_commit") or "").strip()
        scope = item.get("scope")
        if len(source_commit) != 40:
            raise ValueError(
                "OMNIAUTO_SELECTIVE_SOURCE_COMMIT_INVALID"
            )
        if not isinstance(scope, list) or not all(
            isinstance(value, str) and value.strip()
            for value in scope
        ):
            raise ValueError("OMNIAUTO_SELECTIVE_SCOPE_INVALID")
    return payload


def verify_same_tree(source_root: Path, packaged_root: Path) -> dict[str, object]:
    source = tree_manifest(source_root, client_delivery=True)
    packaged = tree_manifest(packaged_root, client_delivery=True)
    if source["tree_sha256"] != packaged["tree_sha256"]:
        source_files = {
            str(item["path"]): str(item["sha256"])
            for item in source["files"]
            if isinstance(item, dict)
        }
        packaged_files = {
            str(item["path"]): str(item["sha256"])
            for item in packaged["files"]
            if isinstance(item, dict)
        }
        raise ValueError(
            json.dumps(
                {
                    "error_code": "OMNIAUTO_TREE_MISMATCH",
                    "missing": sorted(set(source_files) - set(packaged_files)),
                    "unexpected": sorted(set(packaged_files) - set(source_files)),
                    "changed": sorted(
                        key
                        for key in set(source_files) & set(packaged_files)
                        if source_files[key] != packaged_files[key]
                    ),
                },
                ensure_ascii=False,
            )
        )
    return {
        "source": source,
        "packaged": packaged,
        "provenance": load_source_provenance(source_root),
    }
