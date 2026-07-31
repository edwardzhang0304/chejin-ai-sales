from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


CLIENT_MANIFEST_RELATIVE_PATH = PurePosixPath(
    "apps/wechat_ai_customer_service/deploy/client_source_manifest.json"
)


class ClientDeliveryPolicyError(RuntimeError):
    pass


def _normalize_relative_path(value: str | PurePosixPath) -> str:
    raw = str(value).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/"):
        raise ClientDeliveryPolicyError("CLIENT_DELIVERY_PATH_INVALID")
    normalized = PurePosixPath(raw).as_posix()
    if not normalized or normalized == "." or ".." in PurePosixPath(normalized).parts:
        raise ClientDeliveryPolicyError("CLIENT_DELIVERY_PATH_INVALID")
    return normalized


def load_client_exclude_paths(omniauto_root: Path) -> tuple[str, ...]:
    manifest_path = omniauto_root / Path(CLIENT_MANIFEST_RELATIVE_PATH)
    if not manifest_path.is_file():
        raise ClientDeliveryPolicyError("CLIENT_SOURCE_MANIFEST_NOT_FOUND")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    values = payload.get("exclude_paths")
    if not isinstance(values, list) or not values:
        raise ClientDeliveryPolicyError("CLIENT_SOURCE_EXCLUDES_REQUIRED")
    normalized = tuple(sorted({_normalize_relative_path(str(value)) for value in values}))
    if not normalized:
        raise ClientDeliveryPolicyError("CLIENT_SOURCE_EXCLUDES_REQUIRED")
    return normalized


def is_client_forbidden_path(
    relative_path: str | PurePosixPath,
    exclude_paths: tuple[str, ...],
) -> bool:
    relative = _normalize_relative_path(relative_path)
    for excluded in exclude_paths:
        prefix = excluded.rstrip("/")
        if relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def forbidden_tree_entries(
    root: Path,
    exclude_paths: tuple[str, ...],
) -> list[str]:
    resolved = root.resolve()
    return sorted(
        path.relative_to(resolved).as_posix()
        for path in resolved.rglob("*")
        if path.is_file()
        and is_client_forbidden_path(path.relative_to(resolved).as_posix(), exclude_paths)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--omniauto-root", type=Path, required=True)
    parser.add_argument("--scan-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        exclude_paths = load_client_exclude_paths(args.omniauto_root)
        forbidden = forbidden_tree_entries(args.scan_root, exclude_paths)
    except (ClientDeliveryPolicyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": not forbidden,
                "forbidden_entries": forbidden,
                "exclude_count": len(exclude_paths),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not forbidden else 1


if __name__ == "__main__":
    raise SystemExit(main())
