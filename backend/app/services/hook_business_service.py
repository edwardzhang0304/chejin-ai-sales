"""Hook C1 proof and native binding use the original Task and Lead authority.

Only this service writes backend_validated Hook invitation proofs. A shortcode
match by itself, a laboratory run and an existing friend never grant admission.
"""
import hashlib
from datetime import timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.services import task_service, worker_service
from app.services.followup_eligibility import lock_leads, require_followup
from app.enums import TaskResultCode


def text_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def account(db, authenticated, instance, payload, *, lock=True):
    stmt = select(Worker).where(Worker.id == authenticated.id, Worker.deleted_at.is_(None))
    w = db.scalar((stmt.with_for_update() if lock else stmt).execution_options(populate_existing=True))
    runtime = dict(w.hook_runtime_context or {}) if w else {}
    if (w is None or not w.enabled or w.client_binding_state != 'bound' or w.client_instance_id != instance
            or w.hook_self_wxid != payload.self_wxid or w.hook_account_epoch != payload.account_epoch
            or runtime.get('process_generation') != payload.process_generation or runtime.get('blocked')
            or w.wechat_status != 'logged_in'):
        raise AppError('HOOK_ACCOUNT_NOT_AUTHORIZED', '工作微信账号或进程已变化', 409)
    sales = db.get(Sales, w.hook_sales_id) if w.hook_sales_id else None
    if sales is None or sales.deleted_at or not sales.enabled or sales.worker_id != w.id:
        raise AppError('HOOK_SALES_BINDING_REQUIRED', '销售与设备关联已失效', 409)
    return w


def _task(db, task_id, w):
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    if (task is None or task.deleted_at or task.task_type != 'add_friend' or task.worker_id != w.id
            or task.sales_id != w.hook_sales_id or not task.lead_id):
        raise AppError('HOOK_INVITE_TASK_MISMATCH', '正式加好友任务归属不一致', 409)
    lead = db.get(Lead, task.lead_id)
    if lead is None or lead.sales_id != task.sales_id:
        raise AppError('HOOK_INVITE_TASK_MISMATCH', '客户归属已经变化', 409)
    return task


def prepare_invite(db: Session, authenticated, instance, task_id, payload, flow_id, token):
    lock_leads(db, [db.scalar(select(Task.lead_id).where(Task.id == task_id))])
    w = account(db, authenticated, instance, payload)
    task = _task(db, task_id, w)
    require_followup(db, task.lead_id)
    worker_service.validate_inflight_continuation(w, flow_id)
    if flow_id != task.id or (w.inflight_flow_state or {}).get('flow_kind') != 'task' or w.run_status not in {'running','paused'}:
        raise AppError('HOOK_INVITE_FLOW_MISMATCH', '缺少正式在途任务', 409)
    task_service.validate_task_lease(task, worker_id=w.id, client_instance_id=instance, lease_fencing_token=token)
    fields = task_service._worker_add_friend_formal_fields(task)
    value = {'schema_version': 'hook-invite-preparation-v1', 'task_id': task.id, 'worker_id': w.id,
        'client_instance_id': instance, 'lease_fencing_token': task.lease_fencing_token,
        **payload.model_dump(), 'shortcode': fields['remark_code'],
        'greeting_sha256': text_digest(fields['verify_message']), 'remark_sha256': text_digest(fields['remark_name'])}
    evidence = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id,
        TaskEvidence.evidence_type == 'hook_invite_preparation').order_by(TaskEvidence.created_at.desc()))
    if evidence:
        if evidence.extra_metadata != value:
            raise AppError('HOOK_INVITE_PREPARATION_CHANGED', '原申请上下文已变化，不得自动重放', 409)
    else:
        evidence = TaskEvidence(task_id=task.id, worker_id=w.id, evidence_type='hook_invite_preparation', extra_metadata=value)
        db.add(evidence); db.flush()
    return {'preparation_id': evidence.id, 'task_id': task.id, **fields, **task_service._worker_execution_contact(task),
        'account_epoch': w.hook_account_epoch, 'self_wxid': w.hook_self_wxid,
        'process_generation': payload.process_generation, 'lease_fencing_token': task.lease_fencing_token}


def submit_invite(db, authenticated, instance, task_id, payload, flow_id, token, actor):
    lock_leads(db, [db.scalar(select(Task.lead_id).where(Task.id == task_id))])
    # Settlement keeps the original execution identity; it grants no new UI action.
    w = worker_service._lock_worker(db, authenticated.id)
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    prep = db.get(TaskEvidence, payload.preparation_id)
    prior = dict(prep.extra_metadata or {}) if prep else {}
    expected = {key: prior.get(key) for key in ('self_wxid', 'account_epoch', 'process_generation', 'greeting_sha256', 'remark_sha256')}
    if (task is None or task.worker_id != w.id or task.task_type != 'add_friend'
            or prep is None or prep.task_id != task_id or prep.worker_id != w.id
            or prep.evidence_type != 'hook_invite_preparation' or prior.get('schema_version') != 'hook-invite-preparation-v1'
            or prior.get('client_instance_id') != instance or flow_id != task_id
            or type(token) is not int or token <= 0 or prior.get('lease_fencing_token') != token
            or any(getattr(payload, key) != value for key, value in expected.items())):
        raise AppError('HOOK_INVITE_PROOF_MISMATCH', '提交记录与原正式任务不一致', 409)
    try:
        attempted = shared_adapter('hook_history_rules').utc(payload.attempted_at)
        prepared = prep.created_at.replace(tzinfo=timezone.utc) if prep.created_at.tzinfo is None else prep.created_at
        if attempted < prepared or attempted > utcnow():
            raise ValueError('HOOK_INVITE_CLOCK_INVALID')
    except (ValueError, TypeError):
        raise AppError('HOOK_INVITE_CLOCK_INVALID', '申请时间与后台任务不一致；保留原提交记录并核对时钟', 409) from None
    proof = {'schema_version': 'hook-invite-proof-v1', 'backend_validated': True,
        'shortcode': prior['shortcode'], 'shortcode_written': True, 'invitation_submitted': True,
        'task_id': task.id, 'worker_id': w.id, 'client_instance_id': instance,
        'lease_fencing_token': token, **payload.model_dump(exclude_none=True)}
    old = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id, TaskEvidence.evidence_type == 'hook_invite_submission'))
    if old is not None and old.extra_metadata != proof:
        raise AppError('HOOK_INVITE_PROOF_CONFLICT', '原提交记录不可覆盖', 409)
    if old is None:
        old = TaskEvidence(task_id=task.id, worker_id=w.id, evidence_type='hook_invite_submission', extra_metadata=proof)
        db.add(old); db.flush()
    data = task_service.settle_add_friend_success(db, task.id, w.id, instance, flow_id, token,
        TaskResultCode.invite_sent, '已按原申请文案和短码提交好友申请', actor)
    return {'task': data, 'invite_evidence_id': old.id, 'customer_bound': False, 'ai_admission': False}


def bind_friend(db, authenticated, instance, payload):
    lock_leads(db, [db.scalar(select(Task.lead_id).where(Task.id == payload.source_task_id))])
    w = account(db, authenticated, instance, payload)
    task = _task(db, payload.source_task_id, w)
    require_followup(db, task.lead_id)
    proof_row = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id,
        TaskEvidence.evidence_type == 'hook_invite_submission'))
    proof = dict(proof_row.extra_metadata or {}) if proof_row else {}
    if (task.status != 'completed' or task.result_code != 'invite_sent' or proof.get('backend_validated') is not True
            or proof.get('schema_version') != 'hook-invite-proof-v1' or proof.get('self_wxid') != w.hook_self_wxid
            or proof.get('account_epoch') != w.hook_account_epoch or proof_row.worker_id != w.id
            or proof.get('invitation_submitted') is not True or proof.get('shortcode_written') is not True):
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '缺少系统正式申请的绑定依据', 409)
    rules = shared_adapter('hook_contract_rules')
    if rules.digest(payload.friend_list) != payload.raw_evidence_digest:
        raise AppError('HOOK_FRIEND_EVIDENCE_MISMATCH', '好友列表证据摘要不一致', 409)
    try:
        candidate = shared_adapter('hook_contact_rules').resolve_friend_shortcode(payload.friend_list, proof['shortcode'], w.hook_self_wxid)
    except ValueError:
        raise AppError('HOOK_FRIEND_CANDIDATE_UNRESOLVED', '好友列表格式不正确，不能绑定', 409) from None
    if candidate.matching_objects != 1 or candidate.candidate_wxid is None:
        raise AppError('HOOK_FRIEND_CANDIDATE_UNRESOLVED', '未找到唯一短码好友，等待接受或人工核对', 409)
    peer = candidate.candidate_wxid
    if len(peer) > 64:
        raise AppError('HOOK_FRIEND_CANDIDATE_UNRESOLVED', '好友身份格式不正确', 409)
    existing = list(db.scalars(select(WechatSessionBinding).where(WechatSessionBinding.deleted_at.is_(None),
        (WechatSessionBinding.lead_id == task.lead_id) |
        ((WechatSessionBinding.native_self_wxid == w.hook_self_wxid) & (WechatSessionBinding.native_peer_wxid == peer)) |
        (WechatSessionBinding.remark_code == proof['shortcode'])).with_for_update()))
    current=[b for b in existing if (b.native_context or {}).get('source_task_id')==task.id]
    predecessor=None
    if current:
        from app.services.hook_relationship_service import require_binding_chain
        if len(current)==1:
            require_binding_chain(existing,current[0])
        existing=current
        if (len(existing) != 1 or existing[0].bind_status != 'bound' or existing[0].worker_id != w.id
                or existing[0].lead_id != task.lead_id or existing[0].native_peer_wxid != peer
                or existing[0].native_self_wxid != w.hook_self_wxid
                or (existing[0].native_context or {}).get('account_epoch') != w.hook_account_epoch
                or (existing[0].native_context or {}).get('source_task_id') != task.id):
            raise AppError('HOOK_BINDING_CONFLICT', '客户或微信身份已有其他绑定，不自动覆盖', 409)
        binding = existing[0]
        if not binding.allow_listening or binding.listen_status not in {'listening', 'degraded'}:
            raise AppError('HOOK_BINDING_NOT_AUTHORIZED', '原客户绑定已暂停，不自动恢复', 409)
    else:
        if existing or db.scalar(select(TaskEvidence.id).where(TaskEvidence.task_id==task.id,
                TaskEvidence.evidence_type=='hook_reinvite_authorization')):
            from app.services.hook_relationship_service import replacement_predecessor
            predecessor=replacement_predecessor(db,w,task,peer,existing)
        binding = WechatSessionBinding(lead_id=task.lead_id, sales_id=task.sales_id, worker_id=w.id,
            display_name=proof['shortcode'], remark_code=proof['shortcode'],
            native_self_wxid=w.hook_self_wxid, native_peer_wxid=peer,
            bind_status='bound', listen_status='listening', allow_listening=True,
            native_context={'account_epoch': w.hook_account_epoch, 'source_task_id': task.id,
                'invite_evidence_id': proof_row.id, 'business_enabled': True,
                'history_since_create_time': str(int(shared_adapter('hook_history_rules').utc(proof['attempted_at']).timestamp())),
                'history_ready': False,
                'binding_evidence_digest': payload.raw_evidence_digest, 'bound_process_generation': payload.process_generation})
        db.add(binding); db.flush()
        if predecessor is not None:
            predecessor.replacement_binding_id=binding.id
            binding.native_context={**binding.native_context,'previous_binding_id':predecessor.id}
        db.add(Conversation(conversation_id=binding.conversation_id, lead_id=task.lead_id,
            sales_id=task.sales_id, worker_id=w.id, friend_state='friend_active', status='ai_active', ai_enabled=True))
        db.flush()
    return {'binding_id': binding.id, 'conversation_id': binding.conversation_id, 'lead_id': binding.lead_id,
        'worker_id': w.id, 'self_wxid': w.hook_self_wxid, 'account_epoch': w.hook_account_epoch,
        'peer_wxid': binding.native_peer_wxid, 'binding_revision': int(binding.authorization_revision),
        'source_task_id': task.id, 'remark_code': binding.remark_code,
        'history_since_create_time': (binding.native_context or {})['history_since_create_time'],
        'can_ingest_messages': True}
