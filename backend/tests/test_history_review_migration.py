"""Actual Alembic upgrade, re-entry and evidence-preserving rollback refusal."""
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, inspect
from app.core.config import get_settings


def test_history_review_migration_preserves_originals_and_refuses_evidence_loss(monkeypatch, tmp_path):
    url=f'sqlite:///{tmp_path}/review.db'
    monkeypatch.setattr(get_settings(),'database_url',url)
    engine=create_engine(url)
    with engine.begin() as db:
        db.execute(text('CREATE TABLE leads (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text('CREATE TABLE wechat_session_bindings (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text('CREATE TABLE original_send_receipts (id TEXT, payload TEXT)'))
        db.execute(text("INSERT INTO leads VALUES ('lead')"))
        db.execute(text("INSERT INTO wechat_session_bindings VALUES ('binding')"))
        db.execute(text("INSERT INTO original_send_receipts VALUES ('receipt','original-unknown')"))
    config=Config();config.set_main_option('script_location',str(Path(__file__).parents[1]/'alembic'))
    command.stamp(config,'20261022_0042')
    command.upgrade(config,'20261028_0043')
    with engine.begin() as db:
        db.execute(text("INSERT INTO history_reviews (id,lead_id,binding_id,boundary_key,reason,status,revision,evidence,message_ids,resolutions,created_at,updated_at) VALUES ('review','lead','binding','boundary','profile_succession','pending',1,'{}','[]','[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(config,'20261028_0043')
    with pytest.raises(RuntimeError,match='Cannot discard'):
        command.downgrade(config,'20261022_0042')
    with engine.connect() as db:
        assert 'history_reviews' in inspect(db).get_table_names()
        assert db.execute(text('SELECT COUNT(*) FROM history_reviews')).scalar_one()==1
        assert db.execute(text('SELECT payload FROM original_send_receipts')).scalar_one()=='original-unknown'
