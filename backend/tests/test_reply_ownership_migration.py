"""Actual Alembic upgrade/re-entry; original synthetic receipts survive."""
import json
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,text,inspect
from app.core.config import get_settings


def test_upgrade_twice_preserves_old_receipts_and_does_not_backfill(monkeypatch,tmp_path):
    url=f'sqlite:///{tmp_path}/ownership.db'
    monkeypatch.setattr(get_settings(),'database_url',url)
    engine=create_engine(url)
    with engine.begin() as db:
        db.execute(text('CREATE TABLE workers (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text('CREATE TABLE conversations (conversation_id VARCHAR(36) PRIMARY KEY, status TEXT, ai_enabled BOOL)'))
        db.execute(text('CREATE TABLE message_batches (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(text('CREATE TABLE original_send_receipts (id TEXT, payload TEXT)'))
        db.execute(text("INSERT INTO workers VALUES ('worker')"))
        db.execute(text("INSERT INTO conversations VALUES ('conversation','waiting_sales_reply',0)"))
        db.execute(text("INSERT INTO message_batches VALUES ('batch')"))
        db.execute(text("INSERT INTO original_send_receipts VALUES ('receipt','original-unknown')"))
    config=Config();config.set_main_option('script_location',str(Path(__file__).parents[1]/'alembic'))
    command.stamp(config,'20261012_0041')
    command.upgrade(config,'20261022_0042')
    with engine.begin() as db:
        before=db.execute(text('SELECT reply_ownership FROM conversations')).scalar_one()
        assert json.loads(before)['owner']=='legacy'
        assert db.execute(text('SELECT COUNT(*) FROM human_reply_reminders')).scalar_one()==0
        assert db.execute(text('SELECT status,ai_enabled FROM conversations')).one()==('waiting_sales_reply',0)
    command.upgrade(config,'20261022_0042')
    with engine.begin() as db:
        assert db.execute(text('SELECT reply_ownership FROM conversations')).scalar_one()==before
        assert db.execute(text('SELECT payload FROM original_send_receipts')).scalar_one()=='original-unknown'
        assert db.execute(text('SELECT ownership_revision FROM message_batches')).scalar_one() is None
        db.execute(text("INSERT INTO human_reply_reminders (id,conversation_id,first_message_id,started_at,next_attempt_at,status,attempt_count,attempts,created_at,updated_at) VALUES ('r','conversation','m',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'unknown',1,'[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    with pytest.raises(RuntimeError,match='Cannot discard'):command.downgrade(config,'20261012_0041')
    with engine.connect() as db:
        assert 'human_reply_reminders' in inspect(db).get_table_names()
        assert db.execute(text('SELECT payload FROM original_send_receipts')).scalar_one()=='original-unknown'
