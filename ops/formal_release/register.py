"""Fixed operator-installed program executed inside the existing API container."""
import json
from pathlib import Path
import sys

from sqlalchemy import text
from app.contracts.c2 import contract_revision, contract_sha256
from app.core.database import SessionLocal
from app.services.release_readiness import assert_release_ready
from app.services.client_release_service import register_signed_client_release, store_client_release_artifact

folder, expected_contract_revision, expected_contract_sha, operation = sys.argv[1:]
if operation not in {"check", "publish"}:
    raise RuntimeError("INVALID_OPERATION")
if contract_revision() != expected_contract_revision or contract_sha256() != expected_contract_sha:
    raise RuntimeError("BACKEND_CONTRACT_MISMATCH")
root = Path(folder)
with SessionLocal.begin() as db:
    # Keep readiness and registration in one transaction; concurrent task writes wait.
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("LOCK TABLE workers, tasks IN SHARE MODE"))
    readiness = assert_release_ready(db)
    if operation == "publish":
        db.execute(text("SELECT pg_advisory_xact_lock(724098681)"))
        descriptor = json.loads((root / "release.json").read_text())
        release = register_signed_client_release(db, descriptor, public_keys_path=root / "public-keys.json")
        store_client_release_artifact(release, root / "artifact.zip")
print(json.dumps({"ok": True, "publication": "published" if operation == "publish" else "not_run", "readiness": readiness}))
