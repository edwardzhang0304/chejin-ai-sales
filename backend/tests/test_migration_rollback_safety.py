from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(filename: str):
    path = VERSIONS_DIR / filename
    spec = spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("filename", "error_code"),
    [
        (
            "20260804_0019_omniauto_product_knowledge.py",
            "IRREVERSIBLE_MIGRATION_20260804_0019",
        ),
        (
            "20260805_0020_admin_cookie_sessions.py",
            "IRREVERSIBLE_MIGRATION_20260805_0020",
        ),
        (
            "20260806_0021_reply_action_vehicle_facts.py",
            "DOWNGRADE_CHAIN_BLOCKED_BY_DATA_SAFETY",
        ),
        (
            "20260807_0022_vehicle_file_cleanup_outbox.py",
            "IRREVERSIBLE_MIGRATION_20260807_0022",
        ),
        (
            "20260809_0023_c2_identity_read_recovery.py",
            "IRREVERSIBLE_MIGRATION_20260809_0023",
        ),
        (
            "20260809_0024_review_inconsistent_disabled_bindings.py",
            "IRREVERSIBLE_MIGRATION_20260809_0024",
        ),
        (
            "20260811_0026_feishu_handoff_notifications.py",
            "IRREVERSIBLE_MIGRATION_20260811_0026",
        ),
    ],
)
def test_data_bearing_migrations_refuse_automatic_downgrade(
    monkeypatch,
    filename,
    error_code,
):
    migration = _load_migration(filename)
    destructive_calls = []

    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda *args, **kwargs: destructive_calls.append(("execute", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda *args, **kwargs: destructive_calls.append(("drop_table", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: destructive_calls.append(("drop_index", args, kwargs)),
    )

    with pytest.raises(RuntimeError, match=error_code) as exc:
        migration.downgrade()

    assert "backup" in str(exc.value).lower()
    assert "forward migration" in str(exc.value).lower()
    assert destructive_calls == []


def test_legacy_disabled_paused_repair_is_idempotent_and_preserves_terminals():
    migration = _load_migration(
        "20260809_0023_c2_identity_read_recovery.py"
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                ai_enabled BOOLEAN NOT NULL,
                close_reason TEXT,
                deleted_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE wechat_session_bindings (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                bind_status TEXT NOT NULL,
                listen_status TEXT NOT NULL,
                allow_listening BOOLEAN NOT NULL,
                authorization_revision INTEGER NOT NULL,
                error_code TEXT,
                disable_reason TEXT,
                replacement_binding_id TEXT,
                deleted_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO conversations
                    (conversation_id, status, ai_enabled, close_reason, deleted_at)
                VALUES
                    ('valid', 'waiting_user_reply', true, NULL, NULL),
                    ('closed', 'closed', true, 'closed', NULL),
                    ('rejected', 'rejected', false, NULL, NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO wechat_session_bindings
                    (id, conversation_id, bind_status, listen_status,
                     allow_listening, authorization_revision, error_code,
                     disable_reason, replacement_binding_id, deleted_at, updated_at)
                VALUES
                    ('repair', 'valid', 'disabled', 'paused', false, 2, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP),
                    ('permanent', 'valid', 'disabled', 'paused', false, 2, NULL, 'admin_disabled', NULL, NULL, CURRENT_TIMESTAMP),
                    ('history', 'valid', 'disabled', 'paused', false, 2, NULL, NULL, 'repair', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('closed-binding', 'closed', 'disabled', 'paused', false, 2, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP),
                    ('rejected-binding', 'rejected', 'disabled', 'paused', false, 2, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP)
                """
            )
        )

        first_count = migration.repair_legacy_bindings(connection)
        second_count = migration.repair_legacy_bindings(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, bind_status, listen_status, allow_listening,
                       authorization_revision
                  FROM wechat_session_bindings
                 ORDER BY id
                """
            )
        ).mappings().all()

    assert first_count == 1
    assert second_count == 0
    by_id = {row["id"]: row for row in rows}
    assert dict(by_id["repair"]) == {
        "id": "repair",
        "bind_status": "bound",
        "listen_status": "paused",
        "allow_listening": False,
        "authorization_revision": 3,
    }
    for protected_id in (
        "permanent",
        "history",
        "closed-binding",
        "rejected-binding",
    ):
        assert by_id[protected_id]["bind_status"] == "disabled"
        assert by_id[protected_id]["authorization_revision"] == 2


def test_feishu_handoff_migration_is_idempotent_and_preserves_one_open_event():
    migration = _load_migration(
        "20260811_0026_feishu_handoff_notifications.py"
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE sales (
                id TEXT PRIMARY KEY,
                phone TEXT,
                feishu_user_id TEXT,
                deleted_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE handoff_events (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                handoff_reason_code TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP,
                deleted_at TIMESTAMP
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO sales (id, phone, feishu_user_id, deleted_at)
                VALUES
                    ('active', '13900000001', 'legacy-id', NULL),
                    ('deleted', '13900000002', 'legacy-deleted-id', CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO handoff_events
                    (id, conversation_id, status, handoff_reason_code,
                     created_at, updated_at, closed_at, deleted_at)
                VALUES
                    ('hard', 'conversation-one', 'created', 'CUSTOMER_HIGH_INTENT',
                     '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, NULL),
                    ('recoverable', 'conversation-one', 'created', 'C2_MESSAGE_HISTORY_GAP',
                     '2025-01-01 00:00:00', '2025-01-01 00:00:00', NULL, NULL),
                    ('other', 'conversation-two', 'created', 'HANDOFF_REQUIRED',
                     '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, NULL)
                """
            )
        )

        migration._normalize_and_validate_sales_phones(connection)
        migration._close_duplicate_open_handoffs(connection)
        migration._close_duplicate_open_handoffs(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, status, closed_at
                  FROM handoff_events
                 ORDER BY id
                """
            )
        ).mappings().all()

    by_id = {row["id"]: row for row in rows}
    assert by_id["hard"]["closed_at"] is None
    assert by_id["recoverable"]["status"] == "closed_duplicate_migration"
    assert by_id["recoverable"]["closed_at"] is not None
    assert by_id["other"]["closed_at"] is None


def test_feishu_handoff_migration_rejects_missing_phone_on_deleted_sales():
    migration = _load_migration(
        "20260811_0026_feishu_handoff_notifications.py"
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE sales (
                id TEXT PRIMARY KEY,
                phone TEXT,
                deleted_at TIMESTAMP
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO sales (id, phone, deleted_at)
                VALUES ('deleted', NULL, CURRENT_TIMESTAMP)
                """
            )
        )
        with pytest.raises(RuntimeError, match="SALES_PHONE_BACKFILL_REQUIRED"):
            migration._normalize_and_validate_sales_phones(connection)


def test_inconsistent_disabled_repair_is_idempotent_and_preserves_evidence():
    migration = _load_migration(
        "20260809_0024_review_inconsistent_disabled_bindings.py"
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                ai_enabled BOOLEAN NOT NULL,
                close_reason TEXT,
                deleted_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE wechat_session_bindings (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                bind_status TEXT NOT NULL,
                listen_status TEXT NOT NULL,
                allow_listening BOOLEAN NOT NULL,
                authorization_revision INTEGER NOT NULL,
                error_code TEXT,
                disable_reason TEXT,
                disabled_at TIMESTAMP,
                disabled_by TEXT,
                replacement_binding_id TEXT,
                deleted_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO conversations
                    (conversation_id, status, ai_enabled, close_reason, deleted_at)
                VALUES
                    ('valid', 'waiting_user_reply', true, NULL, NULL),
                    ('closed', 'closed', true, 'closed', NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO wechat_session_bindings
                    (id, conversation_id, bind_status, listen_status,
                     allow_listening, authorization_revision, error_code,
                     disable_reason, disabled_at, disabled_by,
                     replacement_binding_id, deleted_at, updated_at)
                VALUES
                    ('paused', 'valid', 'disabled', 'paused', false, 2, NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP),
                    ('unknown', 'valid', 'disabled', 'disabled', false, 4, NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP),
                    ('partial', 'valid', 'disabled', 'disabled', false, 6, NULL, 'admin_disabled', NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP),
                    ('permanent', 'valid', 'disabled', 'disabled', false, 8, NULL, 'admin_disabled', CURRENT_TIMESTAMP, 'operator:1', NULL, NULL, CURRENT_TIMESTAMP),
                    ('history', 'valid', 'disabled', 'disabled', false, 10, NULL, NULL, NULL, NULL, 'permanent', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('closed-binding', 'closed', 'disabled', 'disabled', false, 12, NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP)
                """
            )
        )

        first_count = migration.repair_inconsistent_disabled_bindings(
            connection
        )
        second_count = migration.repair_inconsistent_disabled_bindings(
            connection
        )
        rows = connection.execute(
            text(
                """
                SELECT id, bind_status, listen_status, allow_listening,
                       authorization_revision, error_code
                  FROM wechat_session_bindings
                 ORDER BY id
                """
            )
        ).mappings().all()

    assert first_count == 3
    assert second_count == 0
    by_id = {row["id"]: row for row in rows}
    assert dict(by_id["paused"]) == {
        "id": "paused",
        "bind_status": "bound",
        "listen_status": "paused",
        "allow_listening": False,
        "authorization_revision": 3,
        "error_code": "SESSION_BINDING_MIGRATED_TO_PAUSED",
    }
    for review_id, revision in (("unknown", 5), ("partial", 7)):
        assert dict(by_id[review_id]) == {
            "id": review_id,
            "bind_status": "needs_review",
            "listen_status": "paused",
            "allow_listening": False,
            "authorization_revision": revision,
            "error_code": "SESSION_BINDING_STATE_INCONSISTENT",
        }
    for protected_id, revision in (
        ("permanent", 8),
        ("history", 10),
        ("closed-binding", 12),
    ):
        assert by_id[protected_id]["bind_status"] == "disabled"
        assert by_id[protected_id]["authorization_revision"] == revision
