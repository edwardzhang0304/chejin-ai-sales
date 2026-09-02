"""Register one signed, immutable Worker client release descriptor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.database import SessionLocal
from app.errors import AppError
from app.services.client_release_service import (
    register_signed_client_release,
    store_client_release_artifact,
    withdraw_client_release,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register a signed CheJin Worker client release"
    )
    parser.add_argument("--descriptor", type=Path)
    parser.add_argument("--public-keys", type=Path)
    parser.add_argument("--artifact-file", type=Path)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--publish",
        action="store_true",
        help="required acknowledgement before a published release is written",
    )
    operation.add_argument(
        "--withdraw-version",
        help="withdraw one exact gray windows-x64 version without changing its artifact",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.withdraw_version:
        try:
            with SessionLocal.begin() as db:
                release = withdraw_client_release(
                    db, version=str(args.withdraw_version)
                )
                result = {
                    "ok": True,
                    "release_id": release.id,
                    "channel": release.channel,
                    "platform": release.platform,
                    "version": release.version,
                    "status": release.status,
                }
        except AppError as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "code": exc.code,
                        "message": exc.message,
                        "data": exc.data,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if (
        args.descriptor is None
        or args.public_keys is None
        or args.artifact_file is None
    ):
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "CLIENT_RELEASE_PUBLISH_INPUT_REQUIRED",
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        raw = json.loads(args.descriptor.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("descriptor must be an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "CLIENT_RELEASE_DESCRIPTOR_INVALID",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        with SessionLocal.begin() as db:
            release = register_signed_client_release(
                db,
                raw,
                public_keys_path=Path(args.public_keys),
            )
            artifact_path = store_client_release_artifact(
                release,
                Path(args.artifact_file),
            )
            result = {
                "ok": True,
                "release_id": release.id,
                "channel": release.channel,
                "platform": release.platform,
                "version": release.version,
                "status": release.status,
                "artifact_storage_key": release.artifact_storage_key,
                "artifact_path": str(artifact_path),
            }
    except AppError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                    "data": exc.data,
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
