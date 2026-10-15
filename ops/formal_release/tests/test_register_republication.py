"""A repair can reopen the same signed release, never replace its identity."""
import importlib.util
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests'), str(ROOT / 'worker-client')]
from app.core.database import Base
from app.models.client_release import WorkerClientRelease
from app.services.client_release_service import register_signed_client_release, withdraw_client_release
from app.errors import AppError
from test_client_release_registration import _signed_descriptor

spec = importlib.util.spec_from_file_location('ops_register_republication', ROOT / 'ops/formal_release/register.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('change_identity', [False, True])
def test_republication_is_identical_or_rolls_back(tmp_path, change_identity):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    descriptor, keys = _signed_descriptor(tmp_path, transport='hook')
    with Session(engine) as db:
        with db.begin():
            release = register_signed_client_release(db, descriptor, public_keys_path=keys)
            release_id = release.id
        with db.begin():
            withdraw_client_release(db, version=descriptor['version'], channel='gray-hook')
        if change_identity:
            with pytest.raises(AppError, match='同版本发布记录'):
                with db.begin():
                    module.register_descriptor(db, {**descriptor, 'release_notes': 'changed'}, keys, republish=True)
        else:
            with db.begin():
                assert module.register_descriptor(db, descriptor, keys, republish=True).id == release_id
        db.expire_all()
        row = db.scalar(select(WorkerClientRelease))
        assert row.status == ('withdrawn' if change_identity else 'published')
        assert row.artifact_sha256 == descriptor['artifact_sha256'] and row.release_notes == descriptor['release_notes']
