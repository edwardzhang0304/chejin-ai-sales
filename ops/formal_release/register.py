"""Fixed operator-installed program executed inside the existing API container."""
import json
from pathlib import Path
import sys

from sqlalchemy import text, select
from app.contracts.c2 import contract_revision, contract_sha256
from app.core.database import SessionLocal
from app.services.release_readiness import assert_release_ready
from app.services.client_release_service import register_signed_client_release, store_client_release_artifact
from app.services.client_release_service import validate_signed_release_descriptor
from app.models.client_release import WorkerClientRelease


def register_descriptor(db, descriptor, public_keys, *, republish=False):
    """Caller owns the transaction; failed immutable checks roll back reopening."""
    if republish:
        normalized = validate_signed_release_descriptor(descriptor, public_keys_path=public_keys)
        existing = db.scalar(select(WorkerClientRelease).where(
            WorkerClientRelease.channel == normalized['channel'],
            WorkerClientRelease.platform == normalized['platform'],
            WorkerClientRelease.version == normalized['version']).with_for_update())
        if existing is None or existing.status not in {'withdrawn', 'published'}:
            raise RuntimeError('REPUBLISH_EXISTING_SIGNED_RELEASE_REQUIRED')
        existing.status = 'published'
    # Reuse every original immutable-field check, even for a signed descriptor.
    # This function must not be called outside a transaction context manager.
    return register_signed_client_release(db, descriptor, public_keys_path=public_keys)


def main():
    folder, expected_contract_revision, expected_contract_sha, operation = sys.argv[1:]
    if operation not in {"check", "publish", "republish"}:
        raise RuntimeError("INVALID_OPERATION")
    if contract_revision() != expected_contract_revision or contract_sha256() != expected_contract_sha:
        raise RuntimeError("BACKEND_CONTRACT_MISMATCH")
    root = Path(folder)
    with SessionLocal.begin() as db:
        # Keep readiness and registration in one transaction; concurrent task writes wait.
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("LOCK TABLE workers, tasks IN SHARE MODE"))
        readiness = assert_release_ready(db)
        if operation in {"publish", "republish"}:
            db.execute(text("SELECT pg_advisory_xact_lock(724098681)"))
            descriptor = json.loads((root / "release.json").read_text())
            release = register_descriptor(db, descriptor, root / "public-keys.json", republish=operation == 'republish')
            store_client_release_artifact(release, root / "artifact.zip")
    print(json.dumps({"ok": True, "operation": operation,
        "publication": "published" if operation != "check" else "not_run", "readiness": readiness}))


if __name__ == '__main__':
    main()
