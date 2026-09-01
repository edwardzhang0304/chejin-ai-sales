from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.core.database import Base, SessionLocal, engine
from app.errors import AppError
from app.models.observability import ProcessStageRun
from app.models.c3 import HandoffEvent, MessageBatch
from app.models.audit import OperationLog
from app.models.lead import Lead, LeadAssignment
from app.models.task import Task
from app.models.worker import Worker
from app.schemas.observability import ProcessStageEventIn
from app.services.observability_service import (
    abandon_open_server_stages_after_restart,
    get_process_run,
    ingest_worker_stage_events,
    process_run_id_for_batch,
    process_run_id_for_handoff_event,
    process_run_id_for_key,
    record_server_stage_best_effort,
)
import app.services.observability_service as observability_service
from app.api.routes.wechat import _ingest_telemetry_terminal
from app.api.routes import observability as observability_route
from app.main import app
import app.main as app_main
from app.services import lead_service


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


def test_observability_toggle_keeps_c0_front_middle_back_identical(
    monkeypatch,
):
    sales_payload = {
        "sales_name": "张伟",
        "phone": "13900000001",
        "enabled": True,
        "sort_order": 10,
    }
    lead_payload = {
        "customer_name": "王先生",
        "phones": ["13896676678"],
        "remark": "预算 10 万",
    }
    headers = {
        "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
        "X-Operator-Name": "Ops Tester",
        "X-Operator-Role": "admin",
    }
    original_assign = lead_service.assign_lead_round_robin

    def run(enabled: bool) -> dict[str, object]:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(
            observability_service,
            "get_settings",
            lambda: SimpleNamespace(observability_enabled=enabled),
        )
        middle_calls: list[str] = []

        def assign_with_trace(*args, **kwargs):
            middle_calls.append("assign_lead_round_robin")
            return original_assign(*args, **kwargs)

        monkeypatch.setattr(
            lead_service,
            "assign_lead_round_robin",
            assign_with_trace,
        )
        api = TestClient(app)
        sales_response = api.post(
            "/api/sales",
            json=sales_payload,
            headers=headers,
        )
        lead_response = api.post(
            "/api/leads",
            json=lead_payload,
            headers=headers,
        )
        assert sales_response.status_code == 200
        assert lead_response.status_code == 200

        with SessionLocal() as db:
            lead = db.scalar(select(Lead))
            assignment = db.scalar(select(LeadAssignment))
            tasks = list(db.scalars(select(Task).order_by(Task.created_at)))
            logs = list(
                db.scalars(
                    select(OperationLog).order_by(
                        OperationLog.created_at,
                        OperationLog.id,
                    )
                )
            )
            stages = list(
                db.scalars(
                    select(ProcessStageRun).order_by(
                        ProcessStageRun.created_at,
                        ProcessStageRun.stage_name,
                    )
                )
            )
            assert lead is not None and assignment is not None
            business = {
                "front": {
                    "sales": dict(sales_payload),
                    "lead": dict(lead_payload),
                },
                "middle": {
                    "api_statuses": [
                        sales_response.status_code,
                        lead_response.status_code,
                    ],
                    "calls": list(middle_calls),
                    "audit_events": [
                        (row.module, row.event_type) for row in logs
                    ],
                },
                "back": {
                    "lead_status": lead.status,
                    "assign_status": lead.assign_status,
                    "sales_name": lead_response.json()["data"]["sales_name"],
                    "assignment_type": assignment.assignment_type,
                    "assignment_status": assignment.assignment_status,
                    "tasks": [
                        (task.task_type, task.status, task.block_code)
                        for task in tasks
                    ],
                },
            }
            telemetry = [
                (row.stage_name, row.status) for row in stages
            ]
        return {"business": business, "telemetry": telemetry}

    disabled = run(False)
    enabled = run(True)

    assert disabled["business"] == enabled["business"]
    assert disabled["telemetry"] == []
    assert enabled["telemetry"] == [
        ("c0.lead_received", "succeeded"),
        ("c0.lead_assigned", "succeeded"),
    ]
    # This comparison deliberately commits through the formal HTTP routes.
    # Restore the shared SQLite test database so later rollback/isolation
    # tests never observe C0's committed telemetry rows.
    Base.metadata.drop_all(bind=engine)


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


def test_schema_rejects_inconsistent_terminal_status_and_error_code():
    with pytest.raises(ValueError):
        _event(status="succeeded", error_code="C2_VOICE_RESULT_AMBIGUOUS")
    with pytest.raises(ValueError):
        _event(status="failed", error_code=None)


def test_late_failed_terminal_corrects_prior_succeeded_stage():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        worker = _worker()
        db.add(worker)
        db.flush()
        succeeded = _event()
        record_server_stage_best_effort(
            db,
            process_run_id=succeeded.process_run_id,
            stage_name="c1.add_friend_queued",
            component="backend",
            worker_id=worker.id,
            duration_ms=0,
        )
        ingest_worker_stage_events(db, worker=worker, events=[succeeded])
        failed = succeeded.model_copy(
            update={
                "status": "failed",
                "error_code": "C2_VOICE_RESULT_AMBIGUOUS",
            }
        )

        result = ingest_worker_stage_events(
            db,
            worker=worker,
            events=[failed],
        )

        row = db.get(ProcessStageRun, succeeded.stage_run_id)
        assert result["updated_count"] == 1
        assert row is not None and row.status == "failed"
        assert row.error_code == "C2_VOICE_RESULT_AMBIGUOUS"
        db.rollback()


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


def test_real_observability_savepoint_rollback_preserves_business_transaction():
    Base.metadata.create_all(bind=engine)

    def fail_observability_insert(*_args, **_kwargs):
        raise OSError("process_stage_runs unavailable")

    with SessionLocal() as db:
        worker = _worker()
        worker_id = worker.id
        db.add(worker)
        db.flush()
        event.listen(ProcessStageRun, "before_insert", fail_observability_insert)
        try:
            stage_run_id = record_server_stage_best_effort(
                db,
                process_run_id=str(uuid.uuid4()),
                stage_name="c0.lead_received",
                component="backend",
                worker_id=worker_id,
                duration_ms=1,
            )
        finally:
            event.remove(
                ProcessStageRun,
                "before_insert",
                fail_observability_insert,
            )
        worker.running_status = "business_committed"
        db.commit()

    with SessionLocal() as db:
        persisted_worker = db.get(Worker, worker_id)
        stage_count = len(list(db.scalars(select(ProcessStageRun))))

    assert stage_run_id is None
    assert persisted_worker is not None
    assert (
        persisted_worker.running_status == "business_committed"
    )
    assert stage_count == 0


def test_backend_restart_abandons_only_open_backend_stages_without_duration():
    Base.metadata.create_all(bind=engine)
    process_run_id = str(uuid.uuid4())
    with SessionLocal() as db:
        backend_stage_id = record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            stage_name="c3.brain_generate",
            component="backend",
            status="running",
            stable_key="backend-open",
        )
        worker_stage = _event(
            process_run_id=process_run_id,
            stage_name="c2.message_read",
            component="worker",
            status="running",
            ended_at=None,
            execution_duration_ms=None,
            error_code=None,
        )
        db.add(ProcessStageRun(**worker_stage.model_dump()))
        db.commit()

    with SessionLocal() as db:
        abandoned = abandon_open_server_stages_after_restart(db)
        db.commit()

    with SessionLocal() as db:
        backend_stage = db.get(ProcessStageRun, backend_stage_id)
        worker_stage_row = db.get(ProcessStageRun, worker_stage.stage_run_id)

    assert abandoned == 1
    assert backend_stage is not None
    assert backend_stage.status == "abandoned"
    assert backend_stage.execution_duration_ms is None
    assert backend_stage.error_code == "BACKEND_RESTARTED_DURING_STAGE"
    assert worker_stage_row is not None
    assert worker_stage_row.status == "running"


def test_observability_startup_commit_failure_does_not_block_business_startup(
    monkeypatch,
):
    class BrokenSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            raise RuntimeError("telemetry database unavailable")

    monkeypatch.setattr(app_main, "SessionLocal", BrokenSession)
    monkeypatch.setattr(
        app_main,
        "abandon_open_server_stages_after_restart",
        lambda _db: 1,
    )

    assert app_main._recover_observability_on_startup_best_effort() == 0


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
            "authority_snapshots": [],
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


def test_real_worker_sqlite_upload_route_database_and_aggregate_query(
    monkeypatch,
    tmp_path,
):
    worker_root = Path(__file__).resolve().parents[2] / "worker-client"
    sys.path.insert(0, str(worker_root))
    try:
        from chejin_worker_client import telemetry as worker_telemetry
        from chejin_worker_client.models import Binding as WorkerBinding
    finally:
        sys.path.remove(str(worker_root))

    Base.metadata.create_all(bind=engine)
    process_run_id = str(uuid.uuid4())
    with SessionLocal() as db:
        worker = _worker()
        worker_id = worker.id
        db.add(worker)
        db.flush()
        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id,
            stage_name="c1.add_friend_queued",
            component="backend",
            worker_id=worker_id,
            duration_ms=0,
            stable_key="queue-1",
        )
        db.commit()

    monkeypatch.setattr(
        observability_route.worker_service,
        "authenticate_worker_client",
        lambda db, resolved_worker_id, *_args: db.get(
            Worker, resolved_worker_id
        ),
    )
    telemetry_path = tmp_path / "worker_telemetry.sqlite3"
    stage_run_id = str(uuid.uuid4())
    worker_telemetry.enqueue_existing_duration(
        process_run_id=process_run_id,
        conversation_id=None,
        stage_name="c1.add_friend_execute",
        component="worker",
        execution_duration_ms=4321,
        status="succeeded",
        stage_run_id=stage_run_id,
        db_path=telemetry_path,
    )
    original_event = worker_telemetry.pending_stage_events(
        db_path=telemetry_path
    )[0]
    client = TestClient(app)

    class RouteApi:
        def post_observability_stage_events(
            self,
            binding,
            events,
            **_kwargs,
        ):
            response = client.post(
                (
                    f"/api/workers/{binding.worker_id}"
                    "/observability/stage-events"
                ),
                headers={
                    "X-Worker-Token": binding.worker_token,
                    "X-Client-Instance-Id": binding.client_instance_id,
                },
                json={"events": events},
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]

    uploaded = worker_telemetry.flush_stage_events(
        RouteApi(),
        WorkerBinding(worker_id, "token", "client-instance"),
        db_path=telemetry_path,
    )
    duplicate_result = RouteApi().post_observability_stage_events(
        WorkerBinding(worker_id, "token", "client-instance"),
        [original_event],
    )
    report_response = client.get(
        f"/api/observability/process-runs/{process_run_id}"
    )

    assert uploaded == 1
    assert duplicate_result["accepted_count"] == 1
    assert duplicate_result["inserted_count"] == 0
    assert worker_telemetry.pending_stage_events(db_path=telemetry_path) == []
    cached_reports = worker_telemetry.authority_snapshots(
        db_path=telemetry_path
    )
    assert len(cached_reports) == 1
    assert cached_reports[0]["process_run_id"] == process_run_id
    evidence_zip = tmp_path / "uat-evidence.zip"
    collector = (
        worker_root / "packaging" / "collect_uat_evidence.py"
    )
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    collected = subprocess.run(
        [
            sys.executable,
            str(collector),
            "--app-dir",
            str(tmp_path),
            "--package-dir",
            str(package_dir),
            "--from-iso",
            "2000-01-01T00:00:00Z",
            "--to-iso",
            "2100-01-01T00:00:00Z",
            "--output",
            str(evidence_zip),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    with zipfile.ZipFile(evidence_zip) as archive:
        exported_reports = json.loads(
            archive.read(
                "telemetry/backend_authority_snapshots.json"
            )
        )
        exported_pending = json.loads(
            archive.read("telemetry/pending_stage_uploads.json")
        )
    assert exported_pending == []
    assert len(exported_reports) == 1
    assert exported_reports[0]["process_run_id"] == process_run_id
    assert exported_reports[0]["summary"]["c1.add_friend_execute"][
        "execution_duration_ms"
    ] == 4321
    assert report_response.status_code == 200
    report = report_response.json()["data"]
    matching = [
        item
        for item in report["stages"]
        if item["stage_run_id"] == stage_run_id
    ]
    assert len(matching) == 1
    assert matching[0]["execution_duration_ms"] == 4321
    assert report["summary"]["c1.add_friend_execute"] == {
        "attempt_count": 1,
        "queue_duration_ms": None,
        "execution_duration_ms": 4321,
        "terminal_status": "succeeded",
    }


def test_identity_handoff_ingest_is_not_reported_as_success():
    assert _ingest_telemetry_terminal(
        {
            "message_batch": {
                "batch_status": "handoff_created",
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            }
        }
    ) == ("failed", "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS")
    assert _ingest_telemetry_terminal(
        {"message_batch": {"batch_status": "collecting"}}
    ) == ("succeeded", None)
    assert _ingest_telemetry_terminal(
        {
            "message_batch": {
                "batch_status": "recoverable_hold",
                "reason_codes": [
                    "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                ],
            }
        }
    ) == ("failed", "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS")
