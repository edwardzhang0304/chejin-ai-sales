"""Real notification producers/transactions; only provider and DB failures are fake.

Run against a disposable SQLite database or a dedicated PostgreSQL database
named chejin_u1_u4_test. PostgreSQL-only tests prove row-lock exclusion.
"""
from contextlib import contextmanager
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateSchema
from app.core.database import Base, SessionLocal, engine
from app.models.c3 import Conversation, HandoffEvent, HumanReplyReminder
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services import reply_ownership
from app.services import feishu_service as handoffs, human_reminder_service as reminders
from app.services.c3_recovery import _run_recovery_stage
from app.services.feishu_adapter import FeishuAdapterError
from test_feishu_handoff import _seed_conversation, _create_handoff, FakeFeishuAdapter
from test_human_reply_ownership import unanswered, daytime_after, send_history, Adapter


@pytest.fixture(autouse=True)
def isolated_database():
    assert (engine.url.database == 'chejin_u1_u4_test' or
            '/private/tmp/chejin-hook-formal-20261011/test-' in str(engine.url))
    if engine.dialect.name == 'postgresql':
        with engine.begin() as db:
            for schema in {t.schema for t in Base.metadata.tables.values() if t.schema}:
                db.execute(CreateSchema(schema, if_not_exists=True))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def database_fault(predicate):
    def fail(conn, cursor, statement, parameters, context, executemany):
        values = tuple(parameters.values()) if isinstance(parameters, dict) else parameters
        if predicate(statement.lower(), values):
            raise OperationalError('synthetic DB boundary', {}, Exception('private-token-must-not-log'))
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        yield
    finally:
        event.remove(engine, 'before_cursor_execute', fail)


def pending_handoffs(monkeypatch, count=3):
    adapter = FakeFeishuAdapter()
    monkeypatch.setattr(handoffs, 'get_feishu_adapter', lambda: adapter)
    # Real producer commits and after_commit runs. A claim DB outage leaves the
    # durable queue pending; no manually synthesized notification row.
    with database_fault(lambda sql, p: sql.startswith('update handoff_events') and 'sending' in p):
        ids = [_create_handoff(_seed_conversation()[0]) for _ in range(count)]
    assert not adapter.send_calls
    return ids, adapter


@pytest.mark.parametrize('bad_index', [0, 1])
@pytest.mark.parametrize('boundary', ['read', 'claim', 'settlement'])
def test_pending_batch_isolates_database_failures(monkeypatch, caplog, bad_index, boundary):
    ids, adapter = pending_handoffs(monkeypatch)
    bad = ids[bad_index]
    target = 'sending' if boundary == 'claim' else 'succeeded'
    with database_fault(lambda sql, p: bad in p and 'handoff_events' in sql and
                        (sql.startswith('select') if boundary == 'read' else
                         sql.startswith('update') and target in p)):
        assert not _run_recovery_stage('handoff_notifications', lambda:
            handoffs.recover_pending_handoff_notifications_once(adapter=adapter))
    with SessionLocal() as db:
        assert all(db.get(HandoffEvent, mid).notify_status == 'succeeded' for mid in ids if mid != bad)
        assert db.get(HandoffEvent, bad).notify_status == ('pending' if boundary == 'claim' else 'sending')
    assert bad in caplog.text and 'private-token-must-not-log' not in caplog.text
    later = handoffs.utcnow()+timedelta(hours=1)
    monkeypatch.setattr(handoffs, 'utcnow', lambda: later)
    handoffs.recover_pending_handoff_notifications_once(adapter=adapter)
    handoffs.recover_handoff_notifications(adapter=adapter)
    assert len(adapter.send_calls) == (2 if boundary == 'read' else 3)
    with SessionLocal() as db:
        row = db.get(HandoffEvent, bad)
        assert row.notify_status == ('succeeded' if boundary == 'claim' else 'failed')
        if boundary != 'claim':
            assert row.notify_error_code == 'FEISHU_NOTIFY_RESULT_UNKNOWN'


@pytest.mark.parametrize('result', ['succeeded', 'failed', 'unknown'])
def test_after_commit_settlement_failure_closes_without_restart_or_resend(monkeypatch, result):
    adapter = FakeFeishuAdapter()
    if result != 'succeeded':
        adapter.send_error = FeishuAdapterError('SYNTHETIC_FAILURE', 'synthetic', result_unknown=result=='unknown')
    monkeypatch.setattr(handoffs, 'get_feishu_adapter', lambda: adapter)
    with database_fault(lambda sql, p: sql.startswith('update handoff_events') and
                        ('succeeded' in p or 'failed' in p)):
        mid = _create_handoff(_seed_conversation()[0])
    with SessionLocal() as db:
        row = db.get(HandoffEvent, mid)
        assert row.notify_status == 'sending'
        attempted_at, original_reason = row.notify_attempted_at, row.handoff_reason_code
    assert len(adapter.send_calls) == 1
    assert handoffs.recover_pending_handoff_notifications_once(adapter=adapter)['recovered'] == 0
    later = handoffs.utcnow()+timedelta(hours=1)
    monkeypatch.setattr(handoffs, 'utcnow', lambda: later)
    assert handoffs.recover_pending_handoff_notifications_once(adapter=adapter)['recovered'] == 1
    assert handoffs.recover_pending_handoff_notifications_once(adapter=adapter)['recovered'] == 0
    assert handoffs.recover_handoff_notifications(adapter=adapter)['unknown_settled'] == 0
    with SessionLocal() as db:
        row = db.get(HandoffEvent, mid)
        assert (row.notify_status, row.notify_error_code) == ('failed', 'FEISHU_NOTIFY_RESULT_UNKNOWN')
        assert row.notify_attempted_at == attempted_at and row.handoff_reason_code == original_reason
    assert len(adapter.send_calls) == 1


def test_global_discovery_failure_is_bounded_and_reported(monkeypatch):
    _, adapter = pending_handoffs(monkeypatch)
    with database_fault(lambda sql, p: sql.startswith('select') and 'handoff_events' in sql):
        assert not _run_recovery_stage('handoff_notifications', lambda:
            handoffs.recover_pending_handoff_notifications_once(adapter=adapter))
    assert not adapter.send_calls
    assert _run_recovery_stage('handoff_notifications', lambda:
        handoffs.recover_pending_handoff_notifications_once(adapter=adapter))
    assert len(adapter.send_calls) == 3


def ready_reminder(monkeypatch):
    w, p, b, value, rid, start = unanswered(monkeypatch)
    now = daytime_after(start)
    monkeypatch.setattr(reminders, 'utcnow', lambda: now)
    send_history(w, p, b, value, monkeypatch, now)
    return rid, now


def produce_reminder_round(now):
    # Verified message facts are synthetic input to the real ownership producer.
    # No reminder rows/status/attempts are manufactured by the test.
    cid, _, _ = _seed_conversation()
    with SessionLocal() as db:
        conv = db.get(Conversation, cid)
        conv.reply_ownership = {'owner': 'ai', 'revision': 0}
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == cid))
        worker = db.get(Worker, binding.worker_id)
        worker.online_status, worker.last_heartbeat_at = 'online', now
        binding.last_read_result, binding.last_read_completed_at = 'new_facts', now
        for offset, role in [(2, 'sales'), (1, 'customer')]:
            message = MessageEvent(conversation_id=cid, binding_id=binding.id, worker_id=worker.id,
                rpa_session_key=binding.rpa_session_key, read_run_id=str(uuid.uuid4()), dedupe_key=str(uuid.uuid4()),
                sender_role=role, message_type='text', content='synthetic input',
                raw_payload={'sender_source': 'human' if role == 'sales' else 'customer'},
                occurred_at=now-timedelta(hours=offset))
            db.add(message)
            db.flush()
            assert reply_ownership.apply_message(db, conv, message)
        rid = conv.reply_ownership['round_id']
        db.commit()
        return rid


@pytest.mark.parametrize('boundary', ['read', 'claim', 'settlement'])
@pytest.mark.parametrize('bad_index', [0, 1])
def test_reminder_item_failure_does_not_block_other_round(monkeypatch, caplog, boundary, bad_index):
    now = daytime_after(handoffs.utcnow())
    monkeypatch.setattr(reminders, 'utcnow', lambda: now)
    ids = [produce_reminder_round(now) for _ in range(3)]
    with SessionLocal() as db:
        ids = list(db.scalars(select(HumanReplyReminder.id).order_by(HumanReplyReminder.id)))
    bad = ids[bad_index]
    adapter = Adapter()
    def broken(sql, p):
        if bad not in p or 'human_reply_reminders' not in sql:
            return False
        return (sql.startswith('select') if boundary == 'read' else
                sql.startswith('update') and ('sending' if boundary == 'claim' else 'succeeded') in p)
    with database_fault(broken):
        result = reminders.recover_reminders_once(adapter=adapter)
    assert result['failed'] == 1 and result['attempted'] == 2
    with SessionLocal() as db:
        assert all(db.get(HumanReplyReminder, mid).status == 'succeeded' for mid in ids if mid != bad)
    assert bad in caplog.text and 'private-token-must-not-log' not in caplog.text
    if boundary == 'settlement':
        monkeypatch.setattr(reminders, 'utcnow', lambda: now+timedelta(hours=1))
        assert reminders.recover_reminders_once(adapter=adapter)['recovered'] == 1
        reminders.recover_reminders_once(adapter=adapter)
        assert len(adapter.calls) == 3


@pytest.mark.parametrize('kind', ['handoff', 'reminder'])
def test_postgres_live_send_excluded_from_periodic_and_startup_recovery(monkeypatch, kind):
    if engine.dialect.name != 'postgresql':
        pytest.skip('requires dedicated PostgreSQL: SQLite cannot prove row locks')
    entered, release = threading.Event(), threading.Event()
    calls = []
    class BlockedProvider:
        def send_text_message(self, *args):
            calls.append(1)
            entered.set()
            assert release.wait(10)
    if kind == 'handoff':
        ids, _ = pending_handoffs(monkeypatch, count=1)
        dispatch = lambda: handoffs.dispatch_handoff_notification(ids[0], adapter=BlockedProvider())
        now = handoffs.utcnow()
        recover = handoffs.recover_pending_handoff_notifications_once
        service = handoffs
    else:
        rid, now = ready_reminder(monkeypatch)
        dispatch = lambda: reminders.dispatch_reminder(rid, adapter=BlockedProvider())
        recover = reminders.recover_reminders_once
        service = reminders
    with ThreadPoolExecutor(max_workers=2) as pool:
        sender = pool.submit(dispatch)
        try:
            assert entered.wait(5)
            monkeypatch.setattr(service, 'utcnow', lambda: now+timedelta(hours=1))
            # Separate DB sessions, no process-local mutex. Even aged requests
            # must stay in flight while the provider owner holds the row lock.
            assert recover()['recovered'] == 0
            if kind == 'handoff':
                assert handoffs.recover_handoff_notifications()['unknown_settled'] == 0
        finally:
            release.set()
        assert sender.result(timeout=5) == 'succeeded'
    assert recover()['recovered'] == 0 and calls == [1]


@pytest.mark.parametrize('kind', ['handoff', 'reminder'])
@pytest.mark.parametrize('bad_index', [0, 1])
def test_stale_settlement_isolated_and_global_failure_bounded(monkeypatch, kind, bad_index):
    now = daytime_after(handoffs.utcnow())
    if kind == 'handoff':
        ids, adapter = pending_handoffs(monkeypatch)
        service, model, table = handoffs, HandoffEvent, 'handoff_events'
        dispatch = handoffs.dispatch_handoff_notification
        recover = handoffs.recover_pending_handoff_notifications_once
        terminal, calls = 'failed', adapter.send_calls
    else:
        monkeypatch.setattr(reminders, 'utcnow', lambda: now)
        ids = [produce_reminder_round(now) for _ in range(3)]
        adapter = Adapter()
        service, model, table = reminders, HumanReplyReminder, 'human_reply_reminders'
        dispatch, recover = reminders.dispatch_reminder, reminders.recover_reminders_once
        terminal, calls = 'unknown', adapter.calls
    with database_fault(lambda sql, p: sql.startswith('update '+table) and 'succeeded' in p):
        for mid in ids:
            with pytest.raises(OperationalError):
                dispatch(mid, adapter=adapter)
    assert len(calls) == 3
    monkeypatch.setattr(service, 'utcnow', lambda: now+timedelta(hours=1))
    with database_fault(lambda sql, p: sql.startswith('select') and table in sql):
        assert not _run_recovery_stage(kind, lambda: recover(adapter=adapter))
    with SessionLocal() as db:
        time_col = model.notify_attempted_at if kind == 'handoff' else model.attempted_at
        ordered = list(db.scalars(select(model.id).order_by(time_col, model.id)))
    bad = ordered[bad_index]
    with database_fault(lambda sql, p: sql.startswith('update '+table) and bad in p):
        result = recover(adapter=adapter)
    assert result['failed'] == 1 and result['recovered'] == 2
    assert recover(adapter=adapter)['recovered'] == 1
    assert recover(adapter=adapter)['recovered'] == 0
    with SessionLocal() as db:
        for mid in ids:
            row = db.get(model, mid)
            assert (row.notify_status if kind == 'handoff' else row.status) == terminal
            assert (row.notify_attempted_at if kind == 'handoff' else row.attempted_at)
    assert len(calls) == 3


@pytest.mark.parametrize('kind', ['handoff', 'reminder'])
def test_postgres_concurrent_claimers_only_call_once(monkeypatch, kind):
    if engine.dialect.name != 'postgresql':
        pytest.skip('requires dedicated PostgreSQL')
    if kind == 'handoff':
        ids, adapter = pending_handoffs(monkeypatch, 1)
        dispatch = lambda: handoffs.dispatch_handoff_notification(ids[0], adapter=adapter)
        calls = adapter.send_calls
    else:
        rid, _ = ready_reminder(monkeypatch)
        adapter = Adapter()
        dispatch = lambda: reminders.dispatch_reminder(rid, adapter=adapter)
        calls = adapter.calls
    barrier = threading.Barrier(2)
    def concurrent():
        barrier.wait(5)
        return dispatch()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(concurrent), pool.submit(concurrent)
        results = [a.result(10), b.result(10)]
    assert results.count('succeeded') == 1 and len(calls) == 1


def test_late_settlement_cannot_overwrite_recovered_original_attempt(monkeypatch):
    ids, adapter = pending_handoffs(monkeypatch, 1)
    mid = ids[0]
    with database_fault(lambda sql, p: sql.startswith('update handoff_events') and 'succeeded' in p):
        with pytest.raises(OperationalError):
            handoffs.dispatch_handoff_notification(mid, adapter=adapter)
    with SessionLocal() as db:
        original = db.get(HandoffEvent, mid).notify_attempted_at
    later = handoffs.utcnow()+timedelta(hours=1)
    monkeypatch.setattr(handoffs, 'utcnow', lambda: later)
    assert handoffs.recover_pending_handoff_notifications_once()['recovered'] == 1
    for status in ['succeeded', 'failed']:
        with SessionLocal() as db:
            handoffs._settle_notification(mid, db=db, attempted_at=original,
                status=status, error_code=None, error_summary=None, duration_ms=1)
        with SessionLocal() as db:
            row = db.get(HandoffEvent, mid)
            assert row.notify_error_code == 'FEISHU_NOTIFY_RESULT_UNKNOWN'
            assert row.notify_attempted_at == original
    assert len(adapter.send_calls) == 1
