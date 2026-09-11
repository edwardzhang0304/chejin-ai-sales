"""Read-only release readiness: keep compatible queued work, reject execution risk."""
from sqlalchemy import text

TERMINAL = {'completed', 'failed', 'cancelled'}
LEASE_FIELDS = ('lease_owner_worker_id', 'lease_owner_client_instance_id', 'lease_expires_at', 'lease_last_renewed_at')

def task_release_blocker(task):
    if any(task.get(k) is not None for k in LEASE_FIELDS):
        return 'TASK_LEASE_REMAINS'
    if task.get('status') in TERMINAL:
        return None
    if task.get('task_type') != 'add_friend' or task.get('status') not in {'pending', 'blocked'}:
        return 'TASK_EXECUTING_OR_PAYLOAD_NOT_ACCEPTED'
    if task.get('status') == 'pending' and task.get('block_code') is not None:
        return 'TASK_BLOCK_REASON_NOT_ACCEPTED'
    if task.get('status') == 'blocked' and task.get('block_code') != 'SALES_WORKER_NOT_BOUND':
        return 'TASK_BLOCK_REASON_NOT_ACCEPTED'
    if task.get('claimed_at') is not None or task.get('lease_fencing_token') != 0:
        return 'TASK_EXECUTION_HISTORY'
    if any(task.get(k) is not None for k in ('current_step','reply_action_id','original_task_id','completed_at','failed_at','cancelled_at','failure_step','result_code','error_code')):
        return 'TASK_EXECUTION_HISTORY'
    if task.get('has_execution_event') or task.get('has_evidence'):
        return 'TASK_EXECUTION_HISTORY'
    if not task.get('lead_id'):
        return 'TASK_PAYLOAD_INVALID'
    return None


def release_readiness(db):
    workers = db.execute(text('select run_status,running_status,current_task,local_lock_summary,inflight_flow_state from workers')).mappings().all()
    worker_blockers = sum(not (w['run_status'] in {'paused','faulted'} and w['running_status']=='idle' and w['current_task'] is None and not (w['local_lock_summary'] or {}).get('locked') and not w['inflight_flow_state']) for w in workers)
    tasks = db.execute(text("""select t.*, exists(select 1 from task_events e where e.task_id=t.id and e.event_type not in ('created','blocked')) as has_execution_event,
      exists(select 1 from task_evidences e where e.task_id=t.id) as has_evidence from tasks t""")).mappings().all()
    reasons = {}
    preserved = 0
    for row in tasks:
        reason = task_release_blocker(row)
        if reason: reasons[reason] = reasons.get(reason, 0) + 1
        elif row['status'] not in TERMINAL: preserved += 1
    pending = db.scalar(text("""select exists(select 1 from message_batches where active=true and status in ('collecting','generating','retry_wait'))
      or exists(select 1 from handoff_events where notify_status in ('pending','sending'))
      or exists(select 1 from reply_actions where status in ('sending','unknown_send_result'))"""))
    return {'ready': not worker_blockers and not reasons and not pending, 'worker_blockers':worker_blockers,
            'task_blockers':reasons,'pending_messages_or_send':bool(pending),'preserved_queued_tasks':preserved}


def assert_release_ready(db):
    result = release_readiness(db)
    if not result['ready']:
        raise RuntimeError('WORKERS_NOT_DRAINED')
    return result
