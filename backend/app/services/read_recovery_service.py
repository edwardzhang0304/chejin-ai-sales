"""Contract equivalence and admission for original pending read facts."""
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.contracts.c2 import contract_revision
from app.contracts.read_recovery import compatible_read_contract, read_recovery_capability
from app.errors import AppError
from app.models.base import utcnow
from app.models.task import Task
from app.models.audit import OperationLog
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import WechatMessageIngestRequest
from app.services.worker_service import has_unsettled_worker_send


def recovery_capability_for_worker(db: Session, worker: Worker) -> dict:
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
    if registered is not None and compatible_read_contract(registered, flow.get('contract_sha256')) is None:
        ready = False
    return {**capability, 'ready': ready, 'flow_id': flow.get('flow_id'),
            'conversation_id': flow.get('conversation_id'), 'worker_id': worker.id,
            'client_instance_id': worker.client_instance_id}


def select_settlement_contract(
    db: Session, worker: Worker, payload: WechatMessageIngestRequest,
) -> dict | None:
    if payload.contract_revision == contract_revision():
        return None
    selected = compatible_read_contract(payload.contract_revision, payload.contract_sha256)
    if selected is None:
        raise AppError('MESSAGE_CONTRACT_REVISION_MISMATCH', '消息规则与当前合同不兼容，原始消息已保留', 409)
    flow = dict(worker.inflight_flow_state or {})
    registered = flow.get('contract_revision')
    registered_compatible = (registered is not None
                            and compatible_read_contract(registered, flow.get('contract_sha256')) is not None)
    # A stopped pre-registration read retains its existing recovery scope.
    # Newer registered reads may continue while running when all rules match.
    legacy_stopped = registered is None and recovery_capability_for_worker(db, worker)['ready']
    closed = not flow.get('flow_id') and closed_read_recovery(db, worker, payload) is not None
    active_original = (
        (registered_compatible or legacy_stopped)
        and flow.get('status') in {'active', 'draining'}
        and payload.read_run_id == flow.get('flow_id')
        and payload.conversation_id == flow.get('conversation_id')
        and flow.get('flow_kind') == 'c2_read'
        and flow.get('unread_generation') == payload.unread_generation
        and payload.authorization_scope != 'fact_settlement'
    )
    if not (closed or active_original):
        raise AppError('MESSAGE_CONTRACT_REVISION_MISMATCH', '旧合同消息必须属于原读取流程', 409)
    return selected


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _recovery_identity(payload: WechatMessageIngestRequest) -> dict:
    partition = payload.evidence.model_dump(mode='json').get('ingest_partition') or {}
    sources = sorted(message.source_message_key for message in payload.messages)
    return {
        'contract_revision': payload.contract_revision,
        'contract_sha256': payload.contract_sha256,
        'expected_source_keys': sorted(partition.get('expected_source_message_keys') or sources),
        'partition_index': partition.get('index', 1),
        'partition_count': partition.get('count', 1),
        'payload_sha256': hashlib.sha256(json.dumps(payload.model_dump(mode='json'), ensure_ascii=False,
                                                   sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    }


def closed_read_recovery(
    db: Session, worker: Worker, payload: WechatMessageIngestRequest,
) -> OperationLog | None:
    """Prove a failed original read, without reopening a Flow or authorizing UI.

    A version-only rejection used to close the Flow before its Outbox reached
    the server. Only that recorded failure on the same binding can be repaired.
    Changed authorization, a later read, another active task or send must wait
    for their own recovery path. This is not general admission for ended flows.
    """
    if (worker.run_status not in {'faulted', 'paused'} or (worker.inflight_flow_state or {}).get('flow_id')
            or worker.current_task or worker.running_status != 'idle'
            or (worker.local_lock_summary or {}).get('locked')
            or payload.authorization_scope == 'fact_settlement' or not payload.messages
            or compatible_read_contract(payload.contract_revision, payload.contract_sha256) is None):
        return None
    finish = db.scalar(select(OperationLog).where(
        OperationLog.event_type == 'worker_inflight_finished',
        OperationLog.target_type == 'worker_flow', OperationLog.target_id == payload.read_run_id,
        OperationLog.operator_id == worker.id,
    ).order_by(OperationLog.created_at.desc(), OperationLog.id.desc()).limit(1))
    proof = (finish.after_data or {}) if finish else {}
    bound_at = _utc(worker.bound_at).isoformat() if worker.bound_at else None
    if (not finish or proof.get('terminal_kind') != 'technical_failed'
            or proof.get('error_code') != 'MESSAGE_CONTRACT_REVISION_MISMATCH'
            or proof.get('flow_id') != payload.read_run_id or proof.get('conversation_id') != payload.conversation_id
            or proof.get('client_instance_id') != worker.client_instance_id or proof.get('bound_at') != bound_at
            or _utc(payload.evidence.finished_at) > _utc(finish.created_at)):
        return None
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.worker_id == worker.id,
        WechatSessionBinding.conversation_id == payload.conversation_id,
        WechatSessionBinding.deleted_at.is_(None),
    ))
    from app.services.followup_eligibility import token_for_revision
    if (not binding or binding.bind_status != 'bound' or not binding.allow_listening
            or payload.authorization_revision != token_for_revision(binding.id, int(binding.authorization_revision or 1))
            or payload.unread_generation != binding.unread_generation
            or (binding.last_read_completed_at and _utc(binding.last_read_completed_at) > _utc(finish.created_at)
                and binding.last_read_run_id != payload.read_run_id)):
        return None
    if db.scalar(select(Task.id).where(
        or_(Task.worker_id == worker.id, Task.lease_owner_worker_id == worker.id),
        or_(Task.status == 'running', Task.lease_expires_at > utcnow()),
    ).limit(1)) or has_unsettled_worker_send(db, worker):
        return None
    identity = _recovery_identity(payload)
    # Freeze the first accepted batch/partition identities. A retry can neither
    # add another message nor change the original payload after acceptance.
    for record in db.scalars(select(OperationLog).where(
        OperationLog.event_type == 'worker_closed_read_messages_recovered',
        OperationLog.target_type == 'worker_flow', OperationLog.target_id == payload.read_run_id,
        OperationLog.operator_id == worker.id,
    )):
        prior = record.after_data or {}
        if (any(prior.get(key) != identity[key] for key in (
                'contract_revision', 'contract_sha256', 'expected_source_keys', 'partition_count'))
                or (prior.get('partition_index') == identity['partition_index'] and prior != identity)):
            return None
    return finish


def validate_message_continuation(
    db: Session, worker: Worker, payload: WechatMessageIngestRequest,
    presented_flow_id: str | None,
) -> OperationLog | None:
    from app.services.worker_service import validate_inflight_continuation
    try:
        validate_inflight_continuation(worker, presented_flow_id)
    except AppError:
        # Released clients clear their process-wide Flow header after finish,
        # but retain the original read_run_id in the immutable Outbox body.
        if presented_flow_id in (None, payload.read_run_id):
            proof = closed_read_recovery(db, worker, payload)
            if proof is not None:
                return proof
        raise
    return None


def record_closed_read_recovery(
    db: Session, worker: Worker, payload: WechatMessageIngestRequest, proof: OperationLog,
) -> None:
    """Called under the ingest Worker lock; failed validation rolls it back."""
    identity = _recovery_identity(payload)
    existing = db.scalar(select(OperationLog.id).where(
        OperationLog.event_type == 'worker_closed_read_messages_recovered',
        OperationLog.target_id == payload.read_run_id, OperationLog.operator_id == worker.id,
        OperationLog.after_data['payload_sha256'].as_string() == identity['payload_sha256'],
    ).limit(1))
    if existing is None:
        # No request bodies or customer text are copied into the audit trail.
        db.add(OperationLog(event_type='worker_closed_read_messages_recovered', module='wechat',
                            target_type='worker_flow', target_id=payload.read_run_id, operator_id=worker.id,
                            after_data=identity, extra_metadata={'original_finish_id': proof.id,
                                                               'conversation_id': payload.conversation_id}))
