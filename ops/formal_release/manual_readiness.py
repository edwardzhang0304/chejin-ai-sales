"""Read-only recovery boundary, shared by manual publication and maintenance cutover.

This never deletes a flow or overrides an unsettled execution. Cutover is not
ordinary release readiness and does not authorize publication or new work.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from sqlalchemy import select, text
from app.contracts.c2 import contract_revision, contract_sha256
from app.core.database import SessionLocal
from app.services.release_readiness import release_readiness
from app.services.worker_service import has_unsettled_worker_send
from app.models.worker import Worker


def inspect_terminal_hook_flow(db, w):
    """Classify original server facts; never rewrite a stale heartbeat projection."""
    from app.models.task import Task
    from app.models.c3 import ReplyAction
    flow = w.inflight_flow_state or {}
    assert flow.get('status') == 'draining' and flow.get('flow_kind') == 'chat_reply', 'RECOVERY_CATEGORY_NOT_ACCEPTED'
    task = db.get(Task, flow.get('flow_id'))
    assert task and task.worker_id == w.id and task.status in ('completed', 'failed', 'cancelled'), 'ORIGINAL_TASK_NOT_TERMINAL'
    action = db.get(ReplyAction, task.reply_action_id)
    native = (action.ai_payload or {}).get('native_send_identity', {}) if action else {}
    expected = {'worker_id': w.id, 'client_instance_id': w.client_instance_id,
                'task_id': task.id, 'flow_id': flow['flow_id'], 'reply_action_id': task.reply_action_id,
                'lease_fencing_token': task.lease_fencing_token}
    assert all(native.get(k) == v for k, v in expected.items()), 'ORIGINAL_SEND_IDENTITY_MISMATCH'
    assert action.conversation_id == flow.get('conversation_id'), 'ORIGINAL_CONVERSATION_MISMATCH'


def inspect_stopped_workers(db, expected, *, allow_terminal_projection=False, exited_at=None):
    assert isinstance(expected, dict)
    readiness = release_readiness(db)
    assert not readiness['task_blockers'] and not readiness['pending_messages_or_send'], 'UNSETTLED_EXECUTION'
    seen = {}
    workers = db.query(Worker).filter(Worker.deleted_at.is_(None)).all()
    for w in workers:
        assert w.run_status in ('paused', 'faulted'), 'WORKER_NOT_STOPPED'
        if w.running_status != 'idle' or w.current_task:
            assert (allow_terminal_projection and w.running_status in ('idle', 'running')
                    and w.current_task and w.current_task == (w.inflight_flow_state or {}).get('flow_id')), 'WORKER_NOT_STOPPED'
            inspect_terminal_hook_flow(db, w)
        assert not (w.local_lock_summary or {}).get('locked') and not has_unsettled_worker_send(db, w), 'WORKER_SEND_OR_LOCK_PENDING'
        heartbeat = w.last_heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        assert heartbeat is None or (datetime.now(timezone.utc) - heartbeat).total_seconds() > 120, 'WORKER_RECONNECTED'
        if exited_at is not None:
            assert heartbeat is None or heartbeat <= exited_at, 'HEARTBEAT_AFTER_EXIT_CONFIRMATION'
        if w.inflight_flow_state:
            seen[w.id] = hashlib.sha256(json.dumps(w.inflight_flow_state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert seen == expected, 'UNREVIEWED_OR_CHANGED_RECOVERY_FLOW'
    assert readiness['worker_blockers'] == len(expected), 'OTHER_WORKER_BLOCKER'
    return readiness, workers


def inspect_cutover(db, workers):
    """A stopped terminal Hook send may retain its flow; live/unknown execution may not."""
    from app.models.c3 import HandoffEvent, MessageBatch
    from app.models.vehicle import VehicleFileCleanup
    from app.core.config import get_settings
    assert not get_settings().auto_create_tables, 'AUTOMATIC_SCHEMA_CREATION_ENABLED'
    assert not db.scalar(select(VehicleFileCleanup.id).where(VehicleFileCleanup.status == 'pending').limit(1)), 'STARTUP_FILE_CLEANUP_PENDING'
    assert not db.scalar(select(HandoffEvent.id).where(HandoffEvent.notify_status.in_(('pending', 'sending'))).limit(1)), 'STARTUP_NOTIFICATION_PENDING'
    assert not db.scalar(select(MessageBatch.id).where(MessageBatch.active.is_(True), MessageBatch.status.in_(('collecting', 'generating', 'retry_wait'))).limit(1)), 'STARTUP_GENERATION_PENDING'
    for w in workers:
        flow = w.inflight_flow_state or {}
        if not flow:
            continue
        inspect_terminal_hook_flow(db, w)
    # Hash only; do not output customers, chat bodies, credentials or original permits.
    protected = {}
    tables = ('workers', 'tasks', 'reply_actions', 'sent_acks', 'handoff_events',
              'admin_accounts', 'sales', 'leads', 'wechat_session_bindings',
              'conversations', 'message_events', 'message_batches', 'reply_action_vehicle_facts',
              'hook_native_receipts', 'hook_history_receipts', 'hook_voice_receipts',
              'wechat_recovery_settlements', 'wechat_scan_runs',
              'managed_knowledge_items', 'managed_knowledge_revisions', 'knowledge_releases',
              'current_knowledge_releases', 'wechat_ai_customer_service.knowledge_items',
              'wechat_ai_customer_service.vehicle_images')
    for table in tables:
        # Names are fixed source constants, never a request field.
        row = db.execute(text("SELECT count(*) AS count, encode(sha256(convert_to(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),''),'UTF8')),'hex') AS sha256 FROM " + table + ' t')).mappings().one()
        protected[table] = dict(row)
    return protected


def main(request):
    purpose = request.get('purpose', 'manual_publication')
    assert purpose in ('manual_publication', 'maintenance_cutover'), 'INVALID_READINESS_PURPOSE'
    assert contract_revision() == request['contract_revision'] and contract_sha256() == request['contract_sha256'], 'BACKEND_CONTRACT_MISMATCH'
    with SessionLocal() as db:
        dialect = db.get_bind().dialect.name
        if dialect == 'postgresql':
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
        elif dialect == 'sqlite' and purpose == 'manual_publication':
            db.execute(text('PRAGMA query_only = ON'))
        else:
            raise RuntimeError('READINESS_DATABASE_NOT_SUPPORTED')
        exited_at = None
        if purpose == 'maintenance_cutover':
            exited_at = datetime.fromisoformat(request['confirmed_exited_at'])
            assert exited_at.tzinfo is not None and exited_at <= datetime.now(timezone.utc), 'INVALID_EXIT_CONFIRMATION_TIME'
        readiness, workers = inspect_stopped_workers(db, request['approved_read_flows'],
            allow_terminal_projection=purpose == 'maintenance_cutover', exited_at=exited_at)
        result = {'read_only': True, 'original_flows_preserved': len(request['approved_read_flows'])}
        if purpose == 'maintenance_cutover':
            result.update(cutover_safe=True, ordinary_readiness=readiness,
                          protected=inspect_cutover(db, workers), authorization='maintenance_only_no_publication_no_resume')
        else:
            result['ready'] = True
        db.rollback()
    return result


if __name__ == '__main__':
    print(json.dumps(main(json.loads(sys.argv[1]))))
