"""Exercise the real Hook migration against a pre-Hook SQLite schema."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/20261009_0037_hook_account.py"
    spec = importlib.util.spec_from_file_location("hook_account_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preserves_old_worker_and_prevents_duplicate_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE workers (id VARCHAR(36) PRIMARY KEY, worker_name TEXT NOT NULL, deleted_at DATETIME)"))
        db.execute(text("INSERT INTO workers (id, worker_name) VALUES ('old', 'old OCR worker')"))
        with Operations.context(MigrationContext.configure(db)):
            migration().upgrade()
        row = db.execute(text("SELECT * FROM workers WHERE id='old'")).mappings().one()
        assert row["worker_name"] == "old OCR worker"
        assert row["hook_self_wxid"] is None and row["hook_runtime_context"] == "{}"
        db.execute(text("UPDATE workers SET hook_self_wxid='wxid_test' WHERE id='old'"))
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.execute(text("INSERT INTO workers (id, worker_name, hook_self_wxid) VALUES ('other', 'other', 'wxid_test')"))
        assert db.execute(text("SELECT count(*) FROM workers")).scalar_one() == 1
        db.execute(text("UPDATE workers SET deleted_at=CURRENT_TIMESTAMP WHERE id='old'"))
        db.execute(text("INSERT INTO workers (id, worker_name, hook_self_wxid) VALUES ('other', 'other', 'wxid_test')"))


@pytest.mark.parametrize("field,value", [("hook_self_wxid", "wxid_test"), ("hook_runtime_context", '{"blocked":true}')])
def test_downgrade_refuses_to_discard_identity_or_recovery(tmp_path, field, value):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE workers (id VARCHAR(36) PRIMARY KEY, worker_name TEXT NOT NULL, deleted_at DATETIME)"))
        db.execute(text("INSERT INTO workers (id, worker_name) VALUES ('old', 'old OCR worker')"))
        with Operations.context(MigrationContext.configure(db)):
            migration().upgrade()
            db.execute(text(f"UPDATE workers SET {field}=:value WHERE id='old'"), {"value": value})
            with pytest.raises(RuntimeError, match="Cannot discard"):
                migration().downgrade()
        assert "hook_self_wxid" in {column["name"] for column in inspect(db).get_columns("workers")}
        db.execute(text("UPDATE workers SET hook_self_wxid=NULL, hook_runtime_context='{}' WHERE id='old'"))
        with Operations.context(MigrationContext.configure(db)):
            migration().downgrade()
        assert "hook_self_wxid" not in {column["name"] for column in inspect(db).get_columns("workers")}
        assert db.execute(text("SELECT worker_name FROM workers")).scalar_one() == "old OCR worker"
