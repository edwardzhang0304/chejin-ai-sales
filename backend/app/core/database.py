from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _disable_sqlite_legacy_transaction_control(dbapi_connection, _connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_sqlite_transaction(connection):
        connection.exec_driver_sql("BEGIN")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@event.listens_for(Session, "after_commit")
def _run_post_commit_effects(session: Session) -> None:
    # ``after_commit`` also fires when a SAVEPOINT is released.  Side-channel
    # observability writes use SAVEPOINTs, but their completion is not a
    # business commit and the newly created handoff is not visible to a
    # dispatcher session yet.  Consuming the queue here would lose the only
    # immediate delivery attempt.
    if session.in_nested_transaction():
        return
    from app.services.feishu_service import run_post_commit_effects

    run_post_commit_effects(session)


@event.listens_for(Session, "after_rollback")
def _clear_rolled_back_post_commit_effects(session: Session) -> None:
    # A telemetry SAVEPOINT rollback must not discard business effects queued
    # by the still-live outer transaction.
    if session.in_nested_transaction():
        return
    from app.services.feishu_service import clear_post_commit_effects

    clear_post_commit_effects(session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
