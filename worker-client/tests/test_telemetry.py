from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import chejin_worker_client.telemetry as telemetry
from chejin_worker_client.models import Binding
from chejin_worker_client.telemetry import (
    StageTimer,
    abandon_buffered_running_stages,
    enqueue_c2_flow_timing_stages,
    enqueue_existing_duration,
    flush_stage_events,
    load_process_run,
    pending_stage_events,
    remember_process_run,
)


class TelemetryConnectionLifecycleTest(unittest.TestCase):
    def test_connection_context_closes_sqlite_handle(self):
        with tempfile.TemporaryDirectory(
            prefix="chejin-telemetry-close-"
        ) as temporary_directory:
            path = Path(temporary_directory) / "telemetry.sqlite3"
            with telemetry._connect(path) as connection:
                connection.execute("SELECT 1").fetchone()

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


def _process_run_id() -> str:
    return str(uuid.uuid4())


def test_stage_timer_replaces_running_with_one_terminal_event(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    timer = StageTimer(
        process_run_id=_process_run_id(),
        conversation_id="conversation-1",
        stage_name="c2.message_read",
        component="worker",
        db_path=db_path,
    )
    assert pending_stage_events(db_path=db_path)[0]["status"] == "running"

    timer.finish(status="succeeded")

    events = pending_stage_events(db_path=db_path)
    assert len(events) == 1
    assert events[0]["stage_run_id"] == timer.stage_run_id
    assert events[0]["status"] == "succeeded"
    assert events[0]["execution_duration_ms"] >= 0


def test_process_run_link_survives_outbox_retry_without_business_outbox_fields(
    tmp_path,
):
    db_path = tmp_path / "telemetry.sqlite3"
    process_run_id = _process_run_id()
    assert remember_process_run(
        "read-local-1",
        process_run_id,
        conversation_id="conversation-1",
        db_path=db_path,
    )
    assert load_process_run("read-local-1", db_path=db_path) == process_run_id

    # A late attempt cannot replace the original business-processing identity.
    assert not remember_process_run(
        "read-local-1",
        _process_run_id(),
        conversation_id="conversation-1",
        db_path=db_path,
    )
    assert load_process_run("read-local-1", db_path=db_path) == process_run_id


def test_restart_marks_open_stage_abandoned_without_inventing_duration(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    StageTimer(
        process_run_id=_process_run_id(),
        conversation_id=None,
        stage_name="c1.add_friend_execute",
        component="worker",
        db_path=db_path,
    )

    assert abandon_buffered_running_stages(db_path=db_path) == 1
    event = pending_stage_events(db_path=db_path)[0]
    assert event["status"] == "abandoned"
    assert event["execution_duration_ms"] is None
    assert event["error_code"] == "PROCESS_RESTARTED_DURING_STAGE"


class _FailingApi:
    def post_observability_stage_events(self, *_args, **_kwargs):
        raise TimeoutError("telemetry endpoint unavailable")


class _SuccessApi:
    def __init__(self) -> None:
        self.events = []

    def post_observability_stage_events(self, _binding, events, **_kwargs):
        self.events.extend(events)
        return {"accepted_count": len(events)}


def test_upload_failure_keeps_buffer_and_never_raises_into_business(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    enqueue_existing_duration(
        process_run_id=_process_run_id(),
        conversation_id=None,
        stage_name="c2.scan",
        component="worker",
        execution_duration_ms=123,
        status="succeeded",
        db_path=db_path,
    )
    binding = Binding("worker-1", "token", "client-1")

    assert flush_stage_events(_FailingApi(), binding, db_path=db_path) == 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT upload_attempt_count, next_attempt_at FROM telemetry_stage_events"
        ).fetchone()
    assert row is not None and row[0] == 1 and row[1] > time.time()


def test_duplicate_upload_is_one_stage_and_success_deletes_buffer(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    stage_run_id = str(uuid.uuid4())
    kwargs = {
        "process_run_id": _process_run_id(),
        "conversation_id": None,
        "stage_name": "c2.scan",
        "component": "worker",
        "execution_duration_ms": 20,
        "status": "succeeded",
        "stage_run_id": stage_run_id,
        "db_path": db_path,
    }
    enqueue_existing_duration(**kwargs)
    enqueue_existing_duration(**kwargs)
    api = _SuccessApi()

    assert flush_stage_events(
        api,
        Binding("worker-1", "token", "client-1"),
        db_path=db_path,
    ) == 1
    assert len(api.events) == 1
    assert pending_stage_events(db_path=db_path) == []


def test_local_telemetry_storage_failure_never_escapes_business_code(
    monkeypatch,
    tmp_path,
):
    def fail_connect(_path):
        raise OSError("telemetry disk unavailable")

    monkeypatch.setattr(telemetry, "_connect", fail_connect)
    timer = StageTimer(
        process_run_id=_process_run_id(),
        conversation_id="conversation-1",
        stage_name="c2.message_read",
        component="worker",
        db_path=tmp_path / "unavailable.sqlite3",
    )
    event = timer.finish(status="succeeded")

    assert event["status"] == "succeeded"
    assert pending_stage_events(db_path=tmp_path / "unavailable.sqlite3") == []


def test_observability_switch_disables_new_worker_events(monkeypatch, tmp_path):
    monkeypatch.setattr(
        telemetry,
        "CONFIG",
        SimpleNamespace(
            observability_enabled=False,
            app_dir=tmp_path,
            observability_upload_batch_size=100,
            observability_upload_timeout_seconds=1.0,
        ),
    )
    timer = StageTimer(
        process_run_id=_process_run_id(),
        conversation_id=None,
        stage_name="c2.scan",
        component="worker",
        db_path=tmp_path / "disabled.sqlite3",
    )
    timer.finish(status="succeeded")

    assert not (tmp_path / "disabled.sqlite3").exists()


def test_existing_c2_timing_maps_one_to_one_without_changing_duration(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    events = enqueue_c2_flow_timing_stages(
        process_run_id=_process_run_id(),
        conversation_id="conversation-1",
        read_run_id="read-1",
        trace_id="sidecar-1",
        db_path=db_path,
        flow_timing={
            "phases": [
                {
                    "name": "target_chat_locate",
                    "duration_seconds": 1.234,
                    "completed": True,
                },
                {
                    "name": "voice_transcribe",
                    "duration_seconds": 2.5,
                    "completed": False,
                    "error_code": "VOICE_FAILED",
                },
                {"name": "diagnostic_only", "duration_seconds": 99},
            ]
        },
    )

    assert [item["stage_name"] for item in events] == [
        "c2.target_locate",
        "c2.voice_transcription",
    ]
    assert [item["execution_duration_ms"] for item in events] == [1234, 2500]
    assert [item["status"] for item in events] == ["succeeded", "failed"]
    assert len(pending_stage_events(db_path=db_path)) == 2


def test_c2_voice_failure_code_cannot_be_reported_as_succeeded(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    events = enqueue_c2_flow_timing_stages(
        process_run_id=_process_run_id(),
        conversation_id="conversation-1",
        read_run_id="read-voice-ambiguous",
        trace_id="voice-sidecar-1",
        db_path=db_path,
        flow_timing={
            "phases": [
                {
                    "name": "voice_transcribe",
                    "duration_seconds": 7.5,
                    "completed": True,
                    "failure_code": "C2_VOICE_RESULT_AMBIGUOUS",
                }
            ]
        },
    )

    assert events[0]["stage_name"] == "c2.voice_transcription"
    assert events[0]["status"] == "failed"
    assert events[0]["error_code"] == "C2_VOICE_RESULT_AMBIGUOUS"


def test_c2_image_failure_count_cannot_be_reported_as_succeeded(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    events = enqueue_c2_flow_timing_stages(
        process_run_id=_process_run_id(),
        conversation_id="conversation-1",
        read_run_id="read-image-failed",
        trace_id="image-sidecar-1",
        db_path=db_path,
        flow_timing={
            "phases": [
                {
                    "name": "image_understanding",
                    "duration_seconds": 3.5,
                    "completed": 0,
                    "failed": 1,
                    "failed_source_keys": ["image-source-1"],
                }
            ]
        },
    )

    assert events[0]["stage_name"] == "c2.image_vision"
    assert events[0]["status"] == "failed"
    assert events[0]["error_code"] == "C2_STAGE_FAILED"


def test_retries_get_new_stage_id_and_monotonic_attempt_number(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    process_run_id = _process_run_id()
    first = StageTimer(
        process_run_id=process_run_id,
        conversation_id="conversation-1",
        stage_name="c3.pre_send_refresh",
        component="worker",
        db_path=db_path,
    )
    first.finish(status="failed", error_code="FIRST_FAILED")
    assert flush_stage_events(
        _SuccessApi(),
        Binding("worker-1", "token", "client-1"),
        db_path=db_path,
    ) == 1
    second = StageTimer(
        process_run_id=process_run_id,
        conversation_id="conversation-1",
        stage_name="c3.pre_send_refresh",
        component="worker",
        db_path=db_path,
    )
    second.finish(status="succeeded")

    events = pending_stage_events(db_path=db_path)
    assert first.stage_run_id != second.stage_run_id
    assert [item["attempt"] for item in events] == [2]


def test_repeated_existing_c2_phase_persists_each_attempt_number(tmp_path):
    db_path = tmp_path / "telemetry.sqlite3"
    process_run_id = _process_run_id()

    first_read = enqueue_c2_flow_timing_stages(
        process_run_id=process_run_id,
        conversation_id="conversation-1",
        read_run_id="read-1",
        trace_id="sidecar-1",
        db_path=db_path,
        flow_timing={
            "phases": [
                {
                    "name": "voice_transcribe",
                    "duration_seconds": 1.0,
                    "completed": False,
                },
                {
                    "name": "voice_transcribe",
                    "duration_seconds": 2.0,
                    "completed": True,
                },
            ]
        },
    )
    second_read = enqueue_c2_flow_timing_stages(
        process_run_id=process_run_id,
        conversation_id="conversation-1",
        read_run_id="read-2",
        trace_id="sidecar-2",
        db_path=db_path,
        flow_timing={
            "phases": [
                {
                    "name": "voice_transcribe",
                    "duration_seconds": 3.0,
                    "completed": True,
                }
            ]
        },
    )

    assert [item["attempt"] for item in first_read + second_read] == [1, 2, 3]
    assert len(
        {item["stage_run_id"] for item in first_read + second_read}
    ) == 3
