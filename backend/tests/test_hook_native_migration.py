import importlib.util
from pathlib import Path
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.exc import IntegrityError


def migration():
    path=Path(__file__).parents[1]/'alembic/versions/20261009_0038_hook_native_receipt.py'
    spec=importlib.util.spec_from_file_location('native_receipt_migration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_native_upgrade_keeps_ocr_binding_and_downgrade_requires_empty_native_evidence(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/migration.db')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE workers (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text("INSERT INTO workers (id) VALUES ('worker-A')"))
        db.execute(text("CREATE TABLE wechat_session_bindings (id VARCHAR(36) PRIMARY KEY,worker_id VARCHAR(36) REFERENCES workers(id),rpa_session_key VARCHAR(255) NOT NULL,row_fingerprint VARCHAR(255) NOT NULL,deleted_at DATETIME,bind_status VARCHAR(32) NOT NULL)"))
        db.execute(text("INSERT INTO wechat_session_bindings (id,worker_id,rpa_session_key,row_fingerprint,bind_status) VALUES ('old','worker-A','original locator','original fingerprint','bound')"))
        with Operations.context(MigrationContext.configure(db)):
            migration().upgrade()
            row=db.execute(text("SELECT * FROM wechat_session_bindings WHERE id='old'")).mappings().one()
            assert row['rpa_session_key']=='original locator' and row['row_fingerprint']=='original fingerprint'
            assert row['native_self_wxid'] is None and row['native_context']=='{}'
            db.execute(text("INSERT INTO wechat_session_bindings (id,worker_id,bind_status,native_self_wxid,native_peer_wxid) VALUES ('native','worker-A','bound','wxid_sales_A','wxid_customer_A')"))
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(text("INSERT INTO wechat_session_bindings (id,worker_id,bind_status,native_self_wxid,native_peer_wxid) VALUES ('duplicate','worker-A','bound','wxid_sales_A','wxid_customer_A')"))
            with pytest.raises(RuntimeError,match='Cannot discard'):
                migration().downgrade()
            assert 'hook_native_receipts' in inspect(db).get_table_names()
            db.execute(text("DELETE FROM wechat_session_bindings WHERE id='native'"))
            migration().downgrade()
            assert 'hook_native_receipts' not in inspect(db).get_table_names()
            assert 'native_self_wxid' not in {c['name'] for c in inspect(db).get_columns('wechat_session_bindings')}
            assert db.execute(text('SELECT rpa_session_key FROM wechat_session_bindings')).scalar_one()=='original locator'
