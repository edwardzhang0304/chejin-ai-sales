from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import uuid

import pytest

from app.core.database import Base, SessionLocal, engine
from app.errors import AppError
from app.models.observability import ProcessStageRun
from app.models.c3 import HandoffEvent, MessageBatch
from app.models.worker import Worker
from app.schemas.observability import ProcessStageEventIn
from app.services.observability_service import (
    get_process_run,
    ingest_worker_stage_events,
    process_run_id_for_batch,
    process_run_id_for_handoff_event,
    process_run_id_for_key,
    record_server_stage_best_effort,
)
import app.services.observability_service as observability_service


def _event(**overrides):
    started = datetime.now(timezone.utc)
    values = {
        "process_run_id": str(uuid.uuid4()),
        "stage_run_id": str(uuid.uuid4()),
        "parent_stage_run_id": None,
        "conversation_id": None,
        "stage_name": "c1.add_friend_execute",
        "component": "worker",
        "attempt": 1,
        "queued_at": None,
        "started_at": started,
        "ended_at": started + timedelta(milliseconds=20),
        "queue_duration_ms": None,
        "execution_duration_ms": 20,
        "status": "succeeded",
        "error_code": None,
        "trace_id": "request-1",
    }
    values.update(overrides)
    return ProcessStageEventIn(**values)


def _worker() -> Worker:
    return Worker(
        id=str(uuid.uuid4()),
        worker_name="observability-test-worker",
        worker_token_hash="hash",
        worker_token_encrypted="encrypted",
    )


def test_stage_event_is_idempotent_and_terminal_does_not_regress():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()
        event = _event()
        record_server_stage_best_effort(
            db,
            process_run_id=event.process_run_id,
            stage_name="c1.add_friend_queued",
            component="backend",
            worker_id=worker.id,
            duration_ms=0,
        )

        first = ingest_worker_stage_events(db, worker=worker, events=[event])
        second = ingest_worker_stage_events(db, worker=worker, events=[event])
        stale_running = event.model_copy(
            update={"status": "running", "ended_at": None}
        )
        third = ingest_worker_stage_events(
            db,
            worker=worker,
            events=[stale_running],
        )

        assert first["inserted_count"] == 1
        assert second["ignored_terminal_regressions"] == 1
        assert third["ignored_terminal_regressions"] == 1
        row = db.get(ProcessStageRun, event.stage_run_id)
        assert row is not None and row.status == "succeeded"
        assert row.execution_duration_ms == 20
        report = get_process_run(db, event.process_run_id)
        assert report["stage_count"] == 2
        assert report["summary"]["c1.add_friend_execute"]["attempt_count"] == 1
        db.rollback()


def test_running_event_can_be_completed_once_without_duplicate_stage():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()
        terminal = _event()
        record_server_stage_best_effort(
            db,
            process_run_id=terminal.process_run_id,
            stage_name="c1.add_friend_queued",
            component="backend",
            worker_id=worker.id,
            duration_ms=0,
        )
        running = terminal.model_copy(
            update={
                "status": "running",
                "ended_at": None,
                "execution_duration_ms": None,
            }
        )

        ingest_worker_stage_events(db, worker=worker, events=[running])
        result = ingest_worker_stage_events(db, worker=worker, events=[terminal])

        assert result["updated_count"] == 1
        row = db.get(ProcessStageRun, terminal.stage_run_id)
        assert row is not None and row.status == "succeeded"
        assert row.execution_duration_ms == 20
        db.rollback()


def test_worker_cannot_forge_backend_component():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()
        process_run_id = str(uuid.uuid4())
        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            stage_name="c1.add_friend_queued",
            component="backend",
            worker_id=worker.id,
            duration_ms=0,
        )

        with pytest.raises(AppError) as error:
            ingest_worker_stage_events(
                db,
                worker=worker,
                events=[
                    _event(
                        process_run_id=process_run_id,
                        component="backend",
                    )
                ],
            )

        assert error.value.code == "OBSERVABILITY_COMPONENT_INVALID"
        db.rollback()


def test_schema_rejects_cross_clock_negative_wall_order():
    started = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        _event(
            started_at=started,
            ended_at=started - timedelta(milliseconds=1),
        )


def test_running_ingest_stage_links_following_batch_to_same_process_run():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        process_run_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        trace_id = "ingest-request-1"
        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            conversation_id=conversation_id,
            stage_name="c2.message_ingest",
            component="backend",
            status="running",
            trace_id=trace_id,
            stable_key="read-run-1",
        )
        batch = MessageBatch(
            conversation_id=conversation_id,
            status="collecting",
            active=True,
            trigger_type="customer_message",
            trigger_key="message-1",
            message_event_ids=[],
            message_count=0,
            trace_id=trace_id,
        )
        db.add(batch)
        db.flush()

        assert process_run_id_for_batch(db, batch) == process_run_id
        db.rollback()


def test_server_telemetry_storage_failure_is_ignored(monkeypatch):
    class _BrokenSavepointSession:
        def begin_nested(self):
            raise OSError("telemetry storage unavailable")

    # The helper is deliberately best-effort: an unavailable observability
    # table must never surface into C0-C4 business code.
    assert (
        record_server_stage_best_effort(
            _BrokenSavepointSession(),
            process_run_id=str(uuid.uuid4()),
            stage_name="c0.lead_received",
            component="backend",
            duration_ms=1,
        )
        is None
    )


def test_worker_cannot_invent_process_run_id():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()

        with pytest.raises(AppError) as error:
            ingest_worker_stage_events(db, worker=worker, events=[_event()])

        assert error.value.code == "OBSERVABILITY_PROCESS_RUN_NOT_ISSUED"
        db.rollback()


def test_observability_switch_stops_new_backend_and_worker_events(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        observability_service,
        "get_settings",
        lambda: SimpleNamespace(observability_enabled=False),
    )
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()

        assert (
            record_server_stage_best_effort(
                db,
                process_run_id=str(uuid.uuid4()),
                stage_name="c0.lead_received",
                component="backend",
                duration_ms=1,
            )
            is None
        )
        result = ingest_worker_stage_events(
            db,
            worker=worker,
            events=[_event()],
        )

        assert result == {
            "accepted_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "ignored_terminal_regressions": 0,
            "observability_enabled": False,
        }
        db.rollback()


def test_recall_handoff_is_horizontal_branch_of_same_process_run():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        conversation_id = str(uuid.uuid4())
        cycle_id = "recall-cycle-1"
        batch = MessageBatch(
            conversation_id=conversation_id,
            status="handoff_created",
            active=False,
            trigger_type="recall",
            trigger_key=cycle_id,
            recall_cycle_id=cycle_id,
            message_event_ids=[],
            message_count=0,
        )
        db.add(batch)
        db.flush()
        event = HandoffEvent(
            conversation_id=conversation_id,
            batch_id=batch.id,
            handoff_reason_code="CUSTOMER_HIGH_INTENT",
        )
        db.add(event)
        db.flush()

        expected = process_run_id_for_key("c4", cycle_id)
        assert process_run_id_for_batch(db, batch) == expected
        assert process_run_id_for_handoff_event(db, event) == expected
        db.rollback()


def test_unknown_duration_stays_null_instead_of_becoming_zero():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        process_run_id = str(uuid.uuid4())
        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            stage_name="handoff.wait_sales",
            component="backend",
            status="running",
            duration_ms=None,
            stable_key="handoff-1",
        )

        summary = get_process_run(db, process_run_id)["summary"]
        assert summary["handoff.wait_sales"]["queue_duration_ms"] is None
        assert summary["handoff.wait_sales"]["execution_duration_ms"] is None
        db.rollback()


def test_backend_and_worker_use_exact_same_standard_stage_catalog():
    worker_root = Path(__file__).resolve().parents[2] / "worker-client"
    sys.path.insert(0, str(worker_root))
    try:
        from chejin_worker_client.telemetry import (
            STANDARD_STAGE_NAMES as WORKER_STAGE_NAMES,
        )
    finally:
        sys.path.remove(str(worker_root))

    assert WORKER_STAGE_NAMES == observability_service.STANDARD_STAGE_NAMES
