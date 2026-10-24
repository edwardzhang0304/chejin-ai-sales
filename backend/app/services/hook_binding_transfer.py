"""Explicit cross-lead succession of an existing, proven Hook relationship.

Phone matches only hold new invitations and offer an operator choice. They
never authorize a binding: the native binder still verifies the exact contact.
"""
from sqlalchemy import select

from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation
from app.models.lead import Lead, LeadContact
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.services.followup_eligibility import lock_leads, require_followup

TRANSFER = 'operator_lead_transfer'
BLOCK = 'HOOK_BINDING_TRANSFER_REQUIRED'


def candidates(db, lead_id):
    hashes = select(LeadContact.contact_hash).where(
        LeadContact.lead_id == lead_id, LeadContact.contact_type == 'phone')
    related = select(LeadContact.lead_id).where(
        LeadContact.contact_type == 'phone', LeadContact.contact_hash.in_(hashes))
    return list(db.scalars(select(WechatSessionBinding).where(
        WechatSessionBinding.lead_id.in_(related), WechatSessionBinding.lead_id != lead_id,
        WechatSessionBinding.deleted_at.is_(None), WechatSessionBinding.native_self_wxid.is_not(None), WechatSessionBinding.native_self_wxid != "",
        WechatSessionBinding.replacement_binding_id.is_(None)).order_by(WechatSessionBinding.id)))


def invitation_needs_review(db, task):
    return task.task_type == 'add_friend' and bool(candidates(db, task.lead_id))


def grant(db, task_id):
    return db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task_id,
        TaskEvidence.evidence_type == 'hook_reinvite_authorization'))


def preview(db, lead_id):
    lead = db.get(Lead, lead_id)
    if lead is None or lead.deleted_at:
        raise AppError('LEAD_NOT_FOUND', '客户不存在', 404)
    rows = candidates(db, lead_id)
    tasks = list(db.scalars(select(Task).where(Task.lead_id == lead_id,
        Task.task_type == 'add_friend', Task.deleted_at.is_(None)).order_by(Task.created_at.desc(), Task.id.desc())))
    task = tasks[0] if tasks else None
    # Never silently pick among multiple historical identities.
    if len(rows) != 1 or task is None:
        return {'candidate': None, 'needs_review': bool(rows)}
    binding = rows[0]
    authorization=grant(db,task.id)
    if (authorization and (authorization.extra_metadata or {}).get('mode')==TRANSFER
            and (authorization.extra_metadata or {}).get('previous_binding_id')==binding.id
            and ((binding.native_context or {}).get('reinvite_request') or {}).get('task_id')==task.id):
        return {'candidate':None,'needs_review':True,'pending_verification':True}
    old = db.get(Lead, binding.lead_id)
    return {'needs_review': True, 'candidate': {
        'binding_id': binding.id, 'binding_revision': binding.authorization_revision,
        'source_task_id': task.id, 'previous_lead_id': binding.lead_id,
        'previous_name': old.customer_name, 'previous_remark_code': binding.remark_code,
        'target_name': lead.customer_name,
    }}


def authorize(db, lead_id, payload, actor):
    from app.services import task_service
    from app.services.audit_service import write_log
    from app.services.hook_relationship_service import retire_binding
    from app.services.task_ownership import lock_sales, lock_task_executors, can_reassign_add_friend, has_unsettled_task_flow

    hint = db.get(WechatSessionBinding, payload.binding_id)
    leads = lock_leads(db, [lead_id, hint.lead_id if hint else None])
    require_followup(db, lead_id)
    lead = leads[lead_id]
    lock_sales(db, [lead.sales_id, hint.sales_id if hint else None])
    task = db.scalar(select(Task).where(Task.id == payload.source_task_id)
        .with_for_update().execution_options(populate_existing=True))
    if task is None or task.deleted_at or task.lead_id != lead_id or task.task_type != 'add_friend':
        raise AppError(BLOCK, '目标任务已变化，请刷新客户档案', 409)
    executors = lock_task_executors(db, [task])
    binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id == payload.binding_id)
        .with_for_update().execution_options(populate_existing=True))
    ctx = dict(binding.native_context or {}) if binding else {}
    prior = grant(db, task.id)
    expected = {'mode': TRANSFER, 'previous_binding_id': payload.binding_id,
        'previous_revision': payload.binding_revision, 'lead_id': lead_id, 'reason': payload.reason.strip()}
    if prior:
        if all((prior.extra_metadata or {}).get(k) == v for k, v in expected.items()):
            return {'authorized': True, 'created': False, 'source_task_id': task.id}
        raise AppError(BLOCK, '已有其他接续授权，不可覆盖', 409)
    old = leads.get(binding.lead_id) if binding else None
    worker = executors.get(task.worker_id)
    related = candidates(db, lead_id)
    if (not payload.confirm_end_previous or not payload.reason.strip() or len(related) != 1
            or binding is None or related[0].id != binding.id or old is None or old.status != 'invalid'
            or old.deleted_at or binding.deleted_at or binding.replacement_binding_id
            or binding.authorization_revision != payload.binding_revision
            or binding.bind_status != 'bound' or ctx.get('business_enabled') is not True
            or binding.sales_id != lead.sales_id or task.sales_id != lead.sales_id
            or binding.worker_id != task.worker_id or worker is None or worker.deleted_at
            or not worker.enabled or worker.hook_self_wxid != binding.native_self_wxid
            or worker.hook_account_epoch != ctx.get('account_epoch')
            or worker.hook_sales_id != lead.sales_id):
        raise AppError(BLOCK, '原档案、销售或微信身份已变化，不能接续', 409)
    # A queued task must have no lease, preparation, registered Flow or side
    # effect. A completed invitation reuses its real submission, never resends.
    if has_unsettled_task_flow(task, executors):
        raise AppError(BLOCK, '原加好友流程尚未收尾，请先核对原结果', 409)
    fresh = can_reassign_add_friend(task, executors)
    if fresh and db.scalar(select(TaskEvidence.id).where(TaskEvidence.task_id == task.id,
            TaskEvidence.evidence_type.in_(['hook_invite_preparation', 'hook_invite_submission']))):
        fresh = False
    if not fresh and not (task.status == 'completed' and task.result_code == 'invite_sent'):
        raise AppError(BLOCK, '原加好友动作尚未结清，不能重新申请或覆盖', 409)
    conversation=db.get(Conversation,binding.conversation_id)
    if conversation and conversation.status=='rejected':
        raise AppError(BLOCK, '原客户已拒绝跟进，不能通过新档案恢复自动回复', 409)
    old_proof = db.get(TaskEvidence, ctx.get('invite_evidence_id'))
    proof = old_proof if fresh else db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id,
        TaskEvidence.evidence_type == 'hook_invite_submission'))
    validate_origin(old_proof, worker)
    validate_origin(proof, worker)
    if fresh:
        task_service.cancel_task(db, task.id, TRANSFER, actor)
    retire_binding(db, binding, task.id, payload.reason.strip(), actor)
    binding.native_context={**binding.native_context,'successor_lead_id':lead_id}
    authorization = {**expected, 'previous_lead_id': old.id, 'sales_id': lead.sales_id,
        'self_wxid': binding.native_self_wxid, 'peer_wxid': binding.native_peer_wxid,
        'account_epoch': ctx['account_epoch'], 'invite_evidence_id': proof.id,
        'reuse_previous_invitation': fresh, 'authorized_at': utcnow().isoformat()}
    db.add(TaskEvidence(task_id=task.id, worker_id=worker.id,
        evidence_type='hook_reinvite_authorization', extra_metadata=authorization))
    write_log(db, actor, event_type='hook_customer_transfer_authorized', module='wechat',
        target_type='wechat_session_binding', target_id=binding.id, lead_id=lead_id,
        before_data={'lead_id': old.id, 'binding_revision': payload.binding_revision},
        after_data={'lead_id': lead_id, 'source_task_id': task.id},
        metadata={'reason': payload.reason.strip(), 'original_receipts_preserved': True})
    db.flush()
    return {'authorized': True, 'created': True, 'source_task_id': task.id}


def validate_origin(row, worker):
    data = row.extra_metadata or {} if row else {}
    if (row is None or row.worker_id != worker.id or row.evidence_type != 'hook_invite_submission'
            or data.get('backend_validated') is not True or data.get('schema_version') != 'hook-invite-proof-v1'
            or data.get('self_wxid') != worker.hook_self_wxid or data.get('account_epoch') != worker.hook_account_epoch
            or data.get('invitation_submitted') is not True or data.get('shortcode_written') is not True):
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '缺少系统正式申请的绑定依据', 409)
    return data


def binding_origin(db, task, worker):
    authorization = grant(db, task.id)
    data = authorization.extra_metadata or {} if authorization else {}
    reuse = data.get('mode') == TRANSFER and data.get('reuse_previous_invitation') is True
    if reuse:
        if task.status != 'cancelled' or task.cancel_reason != TRANSFER or authorization.worker_id != worker.id:
            raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '原邀请接续状态不一致', 409)
        row = db.get(TaskEvidence, data.get('invite_evidence_id'))
    else:
        if task.status != 'completed' or task.result_code != 'invite_sent':
            raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '邀请尚未完成', 409)
        row = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id,
            TaskEvidence.evidence_type == 'hook_invite_submission'))
    return row, validate_origin(row, worker), data


def require_binding_origin(db, binding, worker):
    """Intake and transfer share the same real invitation provenance."""
    ctx = binding.native_context or {}
    origin = db.get(Task, ctx.get('source_task_id'))
    if (origin is None or origin.deleted_at or origin.task_type != 'add_friend'
            or origin.lead_id != binding.lead_id or origin.sales_id != binding.sales_id
            or origin.worker_id != worker.id):
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '原邀请任务归属不一致', 409)
    row, proof, authorization = binding_origin(db, origin, worker)
    if row.id != ctx.get('invite_evidence_id') or proof.get('shortcode') != binding.remark_code:
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '原邀请凭证不一致', 409)
    if authorization.get('mode') == TRANSFER:
        previous = db.get(WechatSessionBinding, authorization.get('previous_binding_id'))
        if (previous is None or previous.replacement_binding_id != binding.id
                or ctx.get('previous_binding_id') != previous.id
                or any(authorization.get(k) != v for k, v in {
                    'lead_id':binding.lead_id, 'sales_id':binding.sales_id,
                    'self_wxid':binding.native_self_wxid, 'peer_wxid':binding.native_peer_wxid,
                    'account_epoch':ctx.get('account_epoch')}.items())):
            raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '档案接续凭证不一致', 409)
