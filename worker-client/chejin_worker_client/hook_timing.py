"""Best-effort native durations through the existing bounded telemetry queue.

The context must come from an authenticated backend response. Timing is never
an execution gate, and cannot turn an HTTP return into confirmed delivery.
"""
from datetime import datetime, timezone
import re
import time
import uuid
from . import telemetry


def _error_code(error):
    try:
        candidate = getattr(error, 'code', None) or str(error)
        namespaces = ('HOOK_', 'C1_', 'C2_', 'C3_', 'C4_', 'RUNTIME_', 'API_', 'WORKER_', 'UI_LOCK_', 'REPLY_', 'MESSAGE_', 'LEAD_')
        if isinstance(candidate, str) and candidate.startswith(namespaces) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', candidate):
            return candidate
    except Exception:
        pass
    return 'HOOK_OPERATION_FAILED'


def _record(stage, started_at, elapsed, context, status, error_code):
    try:
        process_id = str(context.get('process_run_id') or '')
        uuid.UUID(process_id)
        ended_at = max(started_at, datetime.now(timezone.utc))
        attempt, stage_id = telemetry.allocate_stage_attempt(process_id, stage, str(uuid.uuid4()))
        telemetry.enqueue_stage_event({
            'process_run_id': process_id, 'stage_run_id': stage_id,
            'conversation_id': context.get('conversation_id'), 'stage_name': stage,
            'component': 'worker', 'attempt': attempt,
            'started_at': started_at.isoformat(), 'ended_at': ended_at.isoformat(),
            'execution_duration_ms': max(0, round(elapsed * 1000)),
            'status': status, 'error_code': error_code,
        })
        api, binding = context.get('api'), context.get('binding')
        if api is not None and binding is not None:
            telemetry.schedule_stage_event_upload(api, binding)
    except Exception:
        # The original queue/exporter owns diagnostics. A failed measurement
        # must not cancel, replay, or change a durable physical operation.
        pass


def timed_call(stage, operation, *, context=None, result_context=False):
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        result = operation()
    except Exception as error:
        _record(stage, started_at, time.perf_counter() - started, context or {}, 'failed', _error_code(error))
        raise
    try:
        measured_context = result_context(result) if callable(result_context) else result if result_context and isinstance(result, dict) else context or {}
    except Exception:
        measured_context = {}
    # A returned transcript can explicitly report failure without raising.
    error = None
    if isinstance(result, dict):
        outcome = result.get('outcome') if isinstance(result.get('outcome'), dict) else {}
        if outcome.get('error_code'):
            error = outcome['error_code']
        elif result.get('state') in {'failed', 'uncertain'}:
            error = result.get('error_code') or 'HOOK_VOICE_RESULT_UNCONFIRMED'
        elif stage == 'hook.send_reconcile' and result.get('local_send_observed') is False:
            error = 'HOOK_SEND_RESULT_UNCONFIRMED'
    _record(stage, started_at, time.perf_counter() - started, measured_context,
            'failed' if error else 'succeeded', _error_code(RuntimeError(error)) if error else None)
    return result


def record_raw_delivery(context):
    """Bind the original callback measurement after backend admission.

    Multi-customer delivery retries share this one scope-free raw measurement;
    no client fabricates an issued process id or sums it into each customer's
    end-to-end time. The original logs and callback bytes remain unchanged.
    """
    try:
        from . import storage
        import json
        process = str(context.get('raw_process_run_id') or '')
        uuid.UUID(process)
        source = str(context.get('raw_source_event_uuid') or '')
        uuid.UUID(source)
        with storage.db_connection() as db:
            row = db.execute("SELECT metadata FROM local_logs WHERE event='hook_callback_saved' AND json_extract(metadata, '$.event_uuid')=? ORDER BY created_at DESC LIMIT 1", (source,)).fetchone()
        if row is None:
            return
        milliseconds = json.loads(row['metadata'])['execution_duration_ms']
        if type(milliseconds) is not int or milliseconds < 0:
            return
        telemetry.enqueue_existing_duration(
            process_run_id=process, conversation_id=None, stage_name='hook.raw_persist',
            component='worker', execution_duration_ms=milliseconds, status='succeeded',
            trace_id=source, stage_run_id=str(uuid.uuid5(uuid.UUID(process), 'hook.raw_persist')))
    except Exception:
        pass
