from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, ReplyAction
from app.services import c3_service
from app.services.recovery_logging import recovery_log, recovery_failure
from app.services.feishu_service import (
    recover_pending_handoff_notifications_once,
)
from app.services.human_reminder_service import recover_reminders_once


logger = logging.getLogger(__name__)


def _run_recovery_stage(stage: str, recover: Callable[[], dict[str, int]]) -> None:
    try:
        result = recover()
        # These are only existing service counters for safe telemetry. Do not
        # stringify an unexpected return value or treat it as a successful pass.
        counters = {"examined", "claimed", "generated", "failed", "recovered", "attempted"}
        if (type(result) is not dict or not result
                or any(type(k) is not str or k not in counters
                       or type(v) is not int or v < 0 for k, v in result.items())):
            recovery_log(logger, logging.ERROR, "C3 recovery invalid result stage=%s", stage)
            return
        recovery_log(logger, logging.INFO, "C3 recovery completed stage=%s counters=%s", stage, result)
    except Exception as exc:
        recovery_failure(logger, stage, exc)


def recover_due_message_batches_once(*, limit: int = 20) -> dict[str, int]:
    """Recover persisted Brain work without depending on a Worker GET poll."""

    with SessionLocal() as db:
        batch_rows = list(
            db.execute(
                select(MessageBatch.id, MessageBatch.status)
                .where(
                    MessageBatch.deleted_at.is_(None),
                    MessageBatch.active.is_(True),
                    MessageBatch.status.in_(("collecting", "generating", "retry_wait")),
                )
                .order_by(MessageBatch.updated_at.asc())
                .limit(max(1, int(limit)))
            )
        )

    result = {"examined": len(batch_rows), "claimed": 0, "generated": 0, "failed": 0}
    for batch_id, batch_status in batch_rows:
        attempt: int | None = None
        try:
            with SessionLocal() as db:
                claim = c3_service.claim_message_batch_generation(
                    db,
                    batch_id=batch_id,
                    stale_only=batch_status != "collecting",
                )
                db.commit()
                if claim.get("run"):
                    attempt = int(claim["attempt"])
                    result["claimed"] += 1
            if attempt is None:
                continue
            with SessionLocal() as db:
                generated=c3_service.generate_for_batch(
                    db,
                    batch_id=batch_id,
                    expected_generation_attempt=attempt,
                )
                db.commit()
                if generated.get("decision") != "queued":
                    result["generated"] += 1
        except Exception as exc:
            result["failed"] += 1
            recovery_failure(logger, "message_batches", exc, batch_id=batch_id, generation_attempt=attempt)
    return result


def recover_stale_reply_sends_once(*, limit: int = 20) -> dict[str, int]:
    with SessionLocal() as db:
        action_ids = list(
            db.scalars(
                select(ReplyAction.id)
                .where(
                    ReplyAction.deleted_at.is_(None),
                    ReplyAction.status == "sending",
                )
                .order_by(ReplyAction.sending_claimed_at.asc())
                .limit(max(1, int(limit)))
            )
        )
    result = {"examined": len(action_ids), "recovered": 0, "failed": 0}
    for action_id in action_ids:
        try:
            with SessionLocal() as db:
                recovered = c3_service.recover_stale_sending_reply_action(
                    db,
                    reply_action_id=action_id,
                )
                db.commit()
                if recovered:
                    result["recovered"] += 1
        except Exception as exc:
            result["failed"] += 1
            recovery_failure(logger, "reply_sends", exc, reply_action_id=action_id)
    return result


class C3BatchRecoveryLoop:
    def __init__(self, *, poll_seconds: float | None = None) -> None:
        configured = get_settings().c3_batch_recovery_poll_seconds
        self.poll_seconds = max(0.0, float(configured if poll_seconds is None else poll_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.poll_seconds <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._run,
            name="c3-batch-recovery",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(5.0, self.poll_seconds + 0.5))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Preserve priority and each service's own sessions/transactions.
            # A failed stage is retried only by the existing next poll.
            for stage, recover in (
                ("handoff_notifications", recover_pending_handoff_notifications_once),
                ("human_reminders", recover_reminders_once),
                ("message_batches", recover_due_message_batches_once),
                ("reply_sends", recover_stale_reply_sends_once),
            ):
                _run_recovery_stage(stage, recover)
            self._stop_event.wait(self.poll_seconds)
