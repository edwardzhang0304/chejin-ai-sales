from pathlib import Path
import importlib.util
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine,inspect,text
import pytest


def test_coverage_migration_preserves_existing_tables_and_nonempty_evidence(tmp_path):
    p=Path(__file__).parents[1]/'alembic/versions/20261011_0039_hook_history_receipt.py'
    spec=importlib.util.spec_from_file_location('history_receipt_migration',p);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    engine=create_engine(f'sqlite:///{tmp_path}/history-migration.sqlite3')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE workers (id VARCHAR(36) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE wechat_session_bindings (id VARCHAR(36) PRIMARY KEY)'))
        conn.execute(text("INSERT INTO workers VALUES ('retained-worker')"))
        conn.execute(text("INSERT INTO wechat_session_bindings VALUES ('retained-binding')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert conn.execute(text('SELECT id FROM workers')).scalar_one()=='retained-worker'
            conn.execute(text("INSERT INTO hook_history_receipts VALUES ('r','retained-worker','retained-binding','capture','digest','{}','{}','2026-10-11')"))
            with pytest.raises(RuntimeError,match='Cannot discard'):module.downgrade()
            assert 'hook_history_receipts' in inspect(conn).get_table_names()
            conn.execute(text('DELETE FROM hook_history_receipts'));module.downgrade()
            assert 'hook_history_receipts' not in inspect(conn).get_table_names()
            assert conn.execute(text('SELECT id FROM wechat_session_bindings')).scalar_one()=='retained-binding'
