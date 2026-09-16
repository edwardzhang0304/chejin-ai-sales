"""Exercise the real Alembic migration on an isolated PostgreSQL schema/data."""
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app.core.database import SessionLocal, engine
from app.models.c3 import ReplyAction, SentAck
from test_lead_followup_eligibility import isolated_db
from test_migration_rollback_safety import _load_migration


def test_single_reply_migration_preserves_identity_and_refuses_lossy_downgrade(monkeypatch):
    assert engine.dialect.name == "postgresql"
    migration = _load_migration("20260916_0035_reply_sequence.py")
    with SessionLocal() as db:
        action = ReplyAction(batch_id="legacy-batch", conversation_id="legacy-conversation",
                             status="sent", reply_text="原回复", reply_text_hash="original-hash")
        db.add(action)
        db.flush()
        db.add(SentAck(reply_action_id=action.id, task_id="original-task", worker_id="original-worker",
                       send_token="original-token", send_result="sent", action_phase="confirmed"))
        db.commit()
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()  # Establish the actual previous columns/index, single rows only.
        before = dict(connection.execute(text("SELECT * FROM reply_actions")).mappings().one())
        receipt = dict(connection.execute(text("SELECT * FROM sent_acks")).mappings().one())
        assert "segment_index" not in before
        migration.upgrade()
        after = dict(connection.execute(text("SELECT * FROM reply_actions")).mappings().one())
        assert {key: after[key] for key in before} == before
        assert after["segment_index"] == after["segment_count"] == 1
        assert after["predecessor_reply_action_id"] is None and after["pre_send_fact_checkpoint"] is None
        assert dict(connection.execute(text("SELECT * FROM sent_acks")).mappings().one()) == receipt
        assert any(i["name"] == "uq_reply_actions_current_batch" and i["unique"] for i in inspect(connection).get_indexes("reply_actions"))
        connection.execute(text("UPDATE reply_actions SET segment_count=2"))
        with pytest.raises(RuntimeError, match="reply-sequence history exists"):
            migration.downgrade()
        assert connection.execute(text("SELECT segment_count FROM reply_actions")).scalar_one() == 2
        assert dict(connection.execute(text("SELECT * FROM sent_acks")).mappings().one()) == receipt
