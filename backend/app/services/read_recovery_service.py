"""Admission for original pending facts, never for fresh reads or sends."""
from sqlalchemy import or_, select

from app.contracts.c2 import contract_revision
from app.contracts.read_recovery import LEGACY_REVISION, LEGACY_SHA256, legacy_read_contract, read_recovery_capability
from app.errors import AppError
from app.models.base import utcnow
from app.models.task import Task
from app.services.worker_service import has_unsettled_worker_send


def recovery_capability_for_worker(db, worker) -> dict:
    flow = dict(worker.inflight_flow_state or {})
    ready = bool(
        worker.run_status in {'paused', 'faulted'}
        and worker.client_binding_state == 'bound'
        and worker.client_instance_id
        and flow.get('flow_id') and flow.get('conversation_id')
        and flow.get('flow_kind') == 'c2_read'
        and flow.get('status') in {'active', 'draining'}
        and flow.get('registered_at')
        and not worker.current_task
        and not db.scalar(select(Task.id).where(
            or_(Task.worker_id == worker.id, Task.lease_owner_worker_id == worker.id),
            or_(Task.status == 'running', Task.lease_expires_at > utcnow()),
        ).limit(1))
        and not has_unsettled_worker_send(db, worker)
    )
    try:
        capability = read_recovery_capability()
    except (RuntimeError, OSError, ValueError):
        # Missing/incompatible recovery resources disable this narrow path;
        # ordinary heartbeats must still report the stopped client's status.
        capability = {'protocol_version': 1, 'contracts': []}
        ready = False
    registered = flow.get('contract_revision')
    # Missing registration fields identify pre-protocol flows. Every new Flow
    # now records its contract at creation, so it cannot acquire this exception.
    expected_sha = next((c['sha256'] for c in capability['contracts'] if c['revision'] == registered), None)
    if registered is not None and (not expected_sha or flow.get('contract_sha256') != expected_sha):
        ready = False
    return {**capability, 'ready': ready, 'flow_id': flow.get('flow_id'),
            'conversation_id': flow.get('conversation_id'), 'worker_id': worker.id,
            'client_instance_id': worker.client_instance_id}


def select_settlement_contract(db, worker, payload):
    if payload.contract_revision == contract_revision():
        return None
    if payload.contract_revision != LEGACY_REVISION or payload.contract_sha256 != LEGACY_SHA256:
        raise AppError('MESSAGE_CONTRACT_REVISION_MISMATCH', '消息合同不属于已验证恢复范围', 409)
    capability = recovery_capability_for_worker(db, worker)
    flow = dict(worker.inflight_flow_state or {})
    if not (
        capability['ready'] and payload.read_run_id == flow.get('flow_id')
        and payload.conversation_id == flow.get('conversation_id')
        and flow.get('contract_revision') in {None, LEGACY_REVISION}
        and flow.get('unread_generation') == payload.unread_generation
        and payload.authorization_scope != 'fact_settlement'
    ):
        raise AppError('MESSAGE_CONTRACT_REVISION_MISMATCH', '旧合同只允许原停止流程的事实结算', 409)
    # The original body/evidence are untouched. This selection only chooses the
    # frozen validator input; normal auth, identity, media and idempotency follow.
    return legacy_read_contract()
