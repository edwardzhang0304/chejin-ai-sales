"""Contract equivalence and admission for original pending read facts."""
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.contracts.c2 import contract_revision, contract_sha256
from app.contracts.shared_rules import shared_adapter
from app.contracts.read_recovery import compatible_read_contract, read_recovery_capability
from app.errors import AppError
from app.models.base import utcnow
from app.models.task import Task
from app.models.audit import OperationLog
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.schemas.wechat import WechatMessageIngestRequest
from app.services.worker_service import has_unsettled_worker_send

TERMINAL_EVENT = 'worker_read_business_settled'


@dataclass(frozen=True)
class CancelledRead:
    binding: WechatSessionBinding
    revocation_id: str
    original_finish_id: str | None


def cancelled_read_admission(db: Session, worker: Worker,
                             payload: WechatMessageIngestRequest) -> CancelledRead | None:
    """Prove original ownership and revoked authority, never current permission.

    Both the route and the locked ingest service use this decision. A historical
    finish plus the binding's revocation bridges the released-client case where
    revocation happened after the Flow was already removed from current state.
    """
    if (payload.authorization_scope == 'fact_settlement' or not payload.messages
            or worker.client_binding_state != 'bound' or not worker.bound_at
            or not worker.client_instance_id
            or compatible_read_contract(payload.contract_revision, payload.contract_sha256) is None):
        return None
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.worker_id == worker.id,
        WechatSessionBinding.conversation_id == payload.conversation_id,
        WechatSessionBinding.deleted_at.is_(None)))
    if not binding or binding.bind_status != 'bound' or not binding.lead_id:
        return None
    prior = db.scalar(select(OperationLog).where(
        OperationLog.event_type == TERMINAL_EVENT, OperationLog.operator_id == worker.id,
        OperationLog.target_id == payload.read_run_id).order_by(OperationLog.created_at).limit(1))
    if prior:
        recorded = (prior.after_data or {}).get('response', {}).get('recovery_settlement', {})
        metadata = prior.extra_metadata or {}
        if (recorded.get('conversation_id') == payload.conversation_id
                and recorded.get('authorization_revision') == payload.authorization_revision
                and recorded.get('client_instance_id') == worker.client_instance_id
                and recorded.get('bound_at') == _utc(worker.bound_at).isoformat()
                and metadata.get('binding_id') == binding.id and metadata.get('revocation_id')):
            # The locked service still checks the frozen full batch and body.
            # Later unread generations cannot undo an already committed proof.
            return CancelledRead(binding, metadata['revocation_id'], metadata.get('original_finish_id'))
        return None
    return revoked_read_owner(db, worker, binding, flow_id=payload.read_run_id,
        authorization_revision=payload.authorization_revision, unread_generation=payload.unread_generation,
        observed_at=payload.evidence.finished_at)


def _original_read_owner(db: Session, worker: Worker, binding: WechatSessionBinding, *,
                         flow_id: str | None, authorization_revision: str,
                         unread_generation: int | None = None,
                         observed_at: datetime | None = None) -> tuple[str, OperationLog | None] | None:
    """Prove the original owner independently of current business eligibility.

    Released media clients omit the ended Flow header. Only a unique matching
    finished Flow on this server binding can fill that missing identity.
    """
    if (worker.client_binding_state != 'bound' or not worker.bound_at or not worker.client_instance_id
            or binding.worker_id != worker.id or not binding.lead_id or binding.deleted_at):
        return None
    flow = dict(worker.inflight_flow_state or {})
    if not flow_id:
        candidates = list(db.scalars(select(OperationLog).where(
            OperationLog.event_type == 'worker_inflight_finished', OperationLog.target_type == 'worker_flow',
            OperationLog.operator_id == worker.id,
            OperationLog.after_data['conversation_id'].as_string() == binding.conversation_id,
            OperationLog.after_data['client_instance_id'].as_string() == worker.client_instance_id,
            OperationLog.after_data['bound_at'].as_string() == _utc(worker.bound_at).isoformat())))
        candidates = [item for item in candidates if (item.after_data or {}).get('terminal_kind')
                      in {'technical_failed', 'read_cancelled', 'read_confirmed'}]
        candidate_ids = {item.target_id for item in candidates}
        if len(candidate_ids) != 1:
            return None
        flow_id = next(iter(candidate_ids))
    active = (flow.get('flow_id') == flow_id and flow.get('flow_kind') == 'c2_read'
              and flow.get('status') in {'active', 'draining'}
              and flow.get('conversation_id') == binding.conversation_id
              and flow.get('authorization_revision') == authorization_revision
              and (unread_generation is None or flow.get('unread_generation') == unread_generation))
    finish = None
    if not active:
        finish = db.scalar(select(OperationLog).where(
            OperationLog.event_type == 'worker_inflight_finished',
            OperationLog.target_type == 'worker_flow', OperationLog.target_id == flow_id,
            OperationLog.operator_id == worker.id).order_by(OperationLog.created_at.desc()).limit(1))
        proof = (finish.after_data or {}) if finish else {}
        if (not finish or proof.get('terminal_kind') not in {'technical_failed', 'read_cancelled', 'read_confirmed'}
                or proof.get('flow_id') != flow_id
                or proof.get('conversation_id') != binding.conversation_id
                or proof.get('client_instance_id') != worker.client_instance_id
                or proof.get('bound_at') != _utc(worker.bound_at).isoformat()
                or (observed_at is not None and _utc(observed_at) > _utc(finish.created_at))):
            return None
    return flow_id, finish


def revoked_read_owner(db: Session, worker: Worker, binding: WechatSessionBinding, *,
                       flow_id: str | None, authorization_revision: str,
                       unread_generation: int | None = None,
                       observed_at: datetime | None = None) -> CancelledRead | None:
    owner = _original_read_owner(db, worker, binding, flow_id=flow_id,
        authorization_revision=authorization_revision, unread_generation=unread_generation,
        observed_at=observed_at)
    if owner is None:
        return None
    flow_id, finish = owner
    # The old permit may be invalid now, including after a later restore. Only
    # the exact historical revocation can terminate work under that permit.
    for log in db.scalars(select(OperationLog).where(
            OperationLog.event_type == 'lead_followup_revoked', OperationLog.lead_id == binding.lead_id)
            .order_by(OperationLog.created_at.asc())):
        for item in (log.extra_metadata or {}).get('bindings', []):
            if (item.get('binding_id') != binding.id or item.get('worker_id') != worker.id
                    or item.get('conversation_id') != binding.conversation_id
                    or item.get('old_token') != authorization_revision
                    or _utc(log.created_at) < _utc(worker.bound_at)):
                continue
            exact_active = item.get('flow_id') == flow_id and (
                unread_generation is None or item.get('unread_generation') == unread_generation)
            closed_before_revoke = (finish is not None and not item.get('flow_id')
                                    and _utc(finish.created_at) <= _utc(log.created_at)
                                    and (unread_generation is None or binding.unread_generation == unread_generation))
            if exact_active or closed_before_revoke:
                return CancelledRead(binding, log.id, finish.id if finish else None)
    return None


def validate_media_recovery_continuation(db: Session, worker: Worker, *, conversation_id: str,
        original_revision: str | None, presented_flow_id: str | None,
        recovery_transaction_id: str | None, action_kind: str | None,
        source_message_key_digest: str | None, original_flow_id: str | None = None) -> None:
    from app.services.worker_service import validate_inflight_continuation
    from app.services.followup_eligibility import followup_block_reason, token_for_revision
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.worker_id == worker.id, WechatSessionBinding.conversation_id == conversation_id))
    recovery_requested = all((original_revision, recovery_transaction_id, action_kind, source_message_key_digest))
    revoked = binding is not None and recovery_requested and (
            followup_block_reason(db, binding.lead_id)
            or (binding.followup_invalidated_revision is not None
                and original_revision != token_for_revision(binding.id, int(binding.authorization_revision or 1))))
    historical_owner = None
    if revoked:
        historical_owner = revoked_read_owner(db, worker, binding, flow_id=original_flow_id or presented_flow_id,
                                             authorization_revision=original_revision)
        if historical_owner is None:
            raise AppError('MESSAGE_AUTHORIZATION_REVISION_EXPIRED',
                           '缺少原动作的归属与撤销凭证，不能取得结算许可', 409)
    try:
        validate_inflight_continuation(worker, presented_flow_id)
    except AppError:
        if (binding is None or not all((original_revision, recovery_transaction_id, source_message_key_digest))
                or action_kind not in {'voice', 'image'}
                or (original_flow_id and presented_flow_id not in {None, original_flow_id})):
            raise
        if historical_owner is not None:
            return
        # A valid original permit needs no revocation log. This still grants
        # only the existing fact-settlement scope, with a proved original Flow;
        # it cannot resume reading, media actions, or normal work.
        if (binding.bind_status != 'bound'
                or original_revision != token_for_revision(binding.id, int(binding.authorization_revision or 1))
                or _original_read_owner(db, worker, binding,
                    flow_id=original_flow_id or presented_flow_id, authorization_revision=original_revision) is None):
            raise


def settle_cancelled_read(db: Session, worker: Worker, payload: WechatMessageIngestRequest,
                         admission: CancelledRead, raw_payload: dict) -> dict:
    """Called only under the original lead → binding → Worker transaction locks."""
    from app.services.wechat_service import _validate_v3_request_contract, _raise_message_identity_collision
    selected = compatible_read_contract(payload.contract_revision, payload.contract_sha256)
    _validate_v3_request_contract(payload, contract=selected)
    rules = shared_adapter('read_settlement')
    try:
        identity = rules.partition_identity(raw_payload)
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError('C2_RECOVERY_PARTITION_INVALID', '原消息分片身份不完整', 409) from exc
    # Earlier valid recovery already froze this batch. Revocation must not
    # turn its cancellation endpoint into permission to append invented keys.
    accepted_identity = _recovery_identity(payload)
    for record in db.scalars(select(OperationLog).where(
            OperationLog.event_type == 'worker_closed_read_messages_recovered',
            OperationLog.operator_id == worker.id, OperationLog.target_id == payload.read_run_id)):
        prior = record.after_data or {}
        if (any(prior.get(key) != accepted_identity[key] for key in (
                'contract_revision', 'contract_sha256', 'expected_source_keys', 'partition_count'))
                or (prior.get('partition_index') == accepted_identity['partition_index'] and prior != accepted_identity)):
            raise AppError('C2_RECOVERY_BATCH_CONFLICT', '原接受批次已冻结，不能更换消息或分片', 409)
    records = list(db.scalars(select(OperationLog).where(
        OperationLog.event_type == TERMINAL_EVENT, OperationLog.operator_id == worker.id,
        OperationLog.target_id == payload.read_run_id).order_by(OperationLog.created_at)))
    for record in records:
        saved = record.after_data or {}
        previous = saved.get('identity') or {}
        if (any(previous.get(key) != identity[key] for key in (
                'flow_id', 'conversation_id', 'authorization_revision', 'contract_revision',
                'contract_sha256', 'expected_source_message_keys', 'partition_count'))
                or (previous.get('partition_index') == identity['partition_index'] and previous != identity)
                or (previous.get('partition_index') != identity['partition_index']
                    and set(previous.get('source_message_keys') or []) & set(identity['source_message_keys']))):
            raise AppError('C2_RECOVERY_BATCH_CONFLICT', '原结算批次已冻结，不能更换消息或分片', 409)
        if previous == identity:
            response = saved['response']
            proof = response['recovery_settlement']
            if (proof['client_instance_id'] != worker.client_instance_id
                    or proof['bound_at'] != _utc(worker.bound_at).isoformat()):
                raise AppError('C2_RECOVERY_OWNER_MISMATCH', '原结算归属已变化', 409)
            return response
    accepted, cancelled, results = [], [], []
    for item in payload.messages:
        event = db.scalar(select(MessageEvent).where(
            MessageEvent.conversation_id == payload.conversation_id,
            or_(MessageEvent.dedupe_key == item.dedupe_key,
                (MessageEvent.read_run_id == payload.read_run_id)
                & (MessageEvent.source_message_key == item.source_message_key))))
        if event:
            if event.worker_id != worker.id or event.read_run_id != payload.read_run_id:
                raise AppError('C2_RECOVERY_OWNER_MISMATCH', '已接收消息不属于原读取', 409)
            _raise_message_identity_collision(db, existing=event, incoming_sender_role=item.sender_role_hint,
                incoming_message_type=item.message_type, incoming_content=item.content,
                incoming_raw_payload=item.raw_payload, source_message_key=item.source_message_key,
                dedupe_key=item.dedupe_key)
            accepted.append(item.source_message_key)
            results.append({'source_message_key': item.source_message_key, 'dedupe_key': item.dedupe_key,
                            'ingest_result': 'duplicated', 'message_id': event.id, 'message_event_id': event.id})
        else:
            # Physical media and send outcomes keep their dedicated settlement
            # owners. Business revocation cannot claim those actions never ran.
            if item.message_type not in {'text', 'system'} or item.sender_role_hint == 'ai':
                raise AppError('C2_FACT_SETTLEMENT_REQUIRED', '原动作结果须经专用无界面结算', 409)
            cancelled.append(item.source_message_key)
    proof_id, settled_at = str(uuid.uuid4()), utcnow().isoformat()
    response = {'recovery_action': 'conversation_terminated', 'accepted_source_message_keys': sorted(accepted),
                'results': results, 'ingested_count': 0, 'duplicated_count': len(accepted),
                'recovery_settlement': {
                    **{key: value for key, value in identity.items() if key != 'expected_source_message_keys'},
                    'protocol_version': 1, 'proof_id': proof_id, 'disposition': 'business_cancelled',
                    'reason_code': 'LEAD_INVALID', 'worker_id': worker.id,
                    'client_instance_id': worker.client_instance_id, 'bound_at': _utc(worker.bound_at).isoformat(),
                    'source_message_keys': sorted(cancelled), 'settled_at': settled_at}}
    db.add(OperationLog(id=proof_id, event_type=TERMINAL_EVENT, module='wechat',
        target_type='worker_flow', target_id=payload.read_run_id, operator_id=worker.id,
        after_data={'identity': identity, 'response': response},
        extra_metadata={'original_finish_id': admission.original_finish_id,
                        'revocation_id': admission.revocation_id, 'binding_id': admission.binding.id}))
    db.flush()
    return response


def business_settlement_complete(db: Session, worker: Worker, flow_id: str, conversation_id: str) -> bool | None:
    """Proofs must cover every original partition before a read may finish."""
    records = list(db.scalars(select(OperationLog).where(
        OperationLog.event_type == TERMINAL_EVENT, OperationLog.operator_id == worker.id,
        OperationLog.target_id == flow_id)))
    if not records:
        return None
    identities, covered, partitions = [], set(), {}
    for record in records:
        data = record.after_data or {}
        identity = data.get('identity') or {}
        response = data.get('response') or {}
        proof = response.get('recovery_settlement') or {}
        if (identity.get('conversation_id') != conversation_id or proof.get('worker_id') != worker.id
                or proof.get('client_instance_id') != worker.client_instance_id
                or proof.get('bound_at') != _utc(worker.bound_at).isoformat()):
            return False
        identities.append(identity)
        covered.update(identity['source_message_keys'])
        partitions.setdefault(identity['partition_index'], set()).update(identity['source_message_keys'])
    expected = identities[0]['expected_source_message_keys']
    count = identities[0]['partition_count']
    if not all(item['expected_source_message_keys'] == expected and item['partition_count'] == count
               for item in identities):
        return False
    # Confirmed Outbox rows are deliberately never sent again. Their original
    # MessageEvents already contain the frozen batch and partition evidence;
    # use those accepted facts alongside cancellation receipts, not a new POST.
    for event in db.scalars(select(MessageEvent).where(
            MessageEvent.worker_id == worker.id, MessageEvent.conversation_id == conversation_id,
            MessageEvent.read_run_id == flow_id)):
        partition = (event.evidence or {}).get('ingest_partition') or {}
        if not partition and count == 1 and event.source_message_key in expected:
            partitions.setdefault(1, set()).add(event.source_message_key)
            covered.add(event.source_message_key)
            continue
        if (partition.get('group_id') != flow_id or partition.get('count') != count
                or sorted(partition.get('expected_source_message_keys') or []) != expected
                or event.source_message_key not in expected):
            return False
        index = partition.get('index')
        if not isinstance(index, int) or not 1 <= index <= count:
            return False
        partitions.setdefault(index, set()).add(event.source_message_key)
        covered.add(event.source_message_key)
    return (set(partitions) == set(range(1, count + 1)) and covered == set(expected)
            and sum(len(keys) for keys in partitions.values()) == len(covered))


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
    return {**capability, 'terminal_settlement_protocol_version': 1,
            'bound_at': _utc(worker.bound_at).isoformat() if worker.bound_at else None,
            'ready': ready, 'flow_id': flow.get('flow_id'),
            'conversation_id': flow.get('conversation_id'), 'worker_id': worker.id,
            'client_instance_id': worker.client_instance_id}


def _matches_registered_read_contract(selected: dict, registration: dict) -> bool:
    # Historical settlement compatibility is not equivalence between two
    # independently accepted contracts. Only a release label may differ here.
    return isinstance(registration, dict) and shared_adapter('contract_rules').equivalent_contract(
        selected, registration.get('contract_revision'), registration.get('contract_sha256')) is not None


def select_settlement_contract(
    db: Session, worker: Worker, payload: WechatMessageIngestRequest,
) -> dict | None:
    if payload.contract_revision == contract_revision() and payload.contract_sha256 == contract_sha256():
        return None
    selected = compatible_read_contract(payload.contract_revision, payload.contract_sha256)
    if selected is None:
        raise AppError('MESSAGE_CONTRACT_REVISION_MISMATCH', '消息规则与当前合同不兼容，原始消息已保留', 409)
    flow = dict(worker.inflight_flow_state or {})
    registered = flow.get('contract_revision')
    registered_compatible = _matches_registered_read_contract(selected, flow)
    # A stopped pre-registration read retains its existing recovery scope.
    # Newer registered reads may continue while running when all rules match.
    legacy_stopped = (registered is None and flow.get('contract_sha256') is None
                      and recovery_capability_for_worker(db, worker)['ready'])
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
    selected = compatible_read_contract(payload.contract_revision, payload.contract_sha256)
    if (worker.run_status not in {'faulted', 'paused'} or (worker.inflight_flow_state or {}).get('flow_id')
            or worker.current_task or worker.running_status != 'idle'
            or (worker.local_lock_summary or {}).get('locked')
            or payload.authorization_scope == 'fact_settlement' or not payload.messages
            or selected is None):
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
    metadata = finish.extra_metadata or {}
    if ('registered_read_contract' in metadata
            and not _matches_registered_read_contract(selected, metadata['registered_read_contract'])):
        return None
    # Published finishes without this metadata retain the existing bounded
    # original-failure proof below. New registered Flows always record it.
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
) -> OperationLog | CancelledRead | None:
    from app.services.worker_service import validate_inflight_continuation
    if presented_flow_id in (None, payload.read_run_id):
        cancelled = cancelled_read_admission(db, worker, payload)
        if cancelled is not None:
            return cancelled
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
