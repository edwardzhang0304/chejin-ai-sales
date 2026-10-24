"""Customer relationship review and explicit operator-authorized new invitation.

A missing contact never proves deletion. A new invitation never overwrites the
old binding; original sends retain their own reconciliation identity.
"""
from sqlalchemy import select
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.services.followup_eligibility import lock_leads, require_followup

REMARK_CHANGED = 'HOOK_CONTACT_REMARK_CHANGED'
REINVITE_REASON = 'operator_reinvite'


def observe(db, authenticated, instance, payload):
    from app.services.hook_business_service import account
    from app.services import c3_service
    hint=db.get(WechatSessionBinding,payload.binding_id)
    lock_leads(db,[hint.lead_id] if hint else [])
    w=account(db,authenticated,instance,payload)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==payload.binding_id)
                      .with_for_update().execution_options(populate_existing=True))
    ctx=dict(binding.native_context or {}) if binding else {}
    if (binding is None or binding.deleted_at or binding.worker_id!=w.id
            or binding.native_self_wxid!=payload.self_wxid or ctx.get('account_epoch')!=payload.account_epoch
            or binding.authorization_revision!=payload.binding_revision or binding.replacement_binding_id):
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED','客户关系核验身份已变化',409)
    proof=db.get(TaskEvidence,ctx.get('invite_evidence_id'))
    if proof is None or (proof.extra_metadata or {}).get('backend_validated') is not True:
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED','缺少原系统邀请凭证',409)
    rules=shared_adapter('hook_contract_rules')
    if rules.digest(payload.friend_list)!=payload.raw_evidence_digest:
        raise AppError('HOOK_FRIEND_EVIDENCE_MISMATCH','联系人证据摘要不一致',409)
    try:
        state=shared_adapter('hook_contact_rules').inspect_bound_contact(payload.friend_list,
            binding.remark_code,payload.self_wxid,binding.native_peer_wxid)
    except ValueError:
        state='unconfirmed'
    ctx['relationship_observation']={'state':state,'response_digest':payload.raw_evidence_digest,
        'observed_at':utcnow().isoformat(),'process_generation':payload.process_generation}
    # Disabled/retired records can be observed, never re-enabled by a read.
    active=(binding.bind_status=='bound' and binding.allow_listening and ctx.get('business_enabled') is True)
    if state=='remark_changed' and active and not ctx.get('relationship_review_required'):
        require_followup(db,binding.lead_id)
        conversation=db.get(Conversation,binding.conversation_id)
        if conversation and conversation.status not in {'closed','rejected'}:
            evidence=TaskEvidence(task_id=ctx['source_task_id'],worker_id=w.id,
                evidence_type='hook_relationship_review',extra_metadata={
                    'binding_id':binding.id,'binding_revision':binding.authorization_revision,
                    'self_wxid':payload.self_wxid,'account_epoch':payload.account_epoch,
                    **ctx['relationship_observation']})
            db.add(evidence);db.flush()
            event,_=c3_service._create_or_reuse_open_handoff(db,conversation=conversation,batch_id=None,
                handoff_reason_code=REMARK_CHANGED,reason_detail='客户备注识别信息已变化，暂停该客户自动回复，请人工核对。',
                trigger_message_event_ids=[],risk_flags=['contact_identity_unconfirmed'],
                evidence_refs=['task_evidence:'+evidence.id],ai_payload={'source':'hook_relationship_review'})
            event.risk_flags=list(dict.fromkeys([*(event.risk_flags or []),'contact_identity_unconfirmed']))
            conversation.handoff_reason_code=event.handoff_reason_code
            conversation.handoff_at=event.created_at or utcnow()
            conversation.status='waiting_sales_reply'
            conversation.ai_enabled=False
            conversation.recall_cycle_id=conversation.recall_origin_status=conversation.next_recall_at=None
            c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,reason=REMARK_CHANGED)
            ctx['relationship_review_required']=True
    binding.native_context=ctx
    db.flush()
    return {'binding_id':binding.id,'state':state,'review_required':bool(ctx.get('relationship_review_required')),
            'authorization_changed':False,'deletion_confirmed':False}


def reinvite(db, lead_id, payload, actor):
    from app.services import task_service, wechat_service, c3_service
    from app.services.audit_service import write_log
    leads=lock_leads(db,[lead_id]);require_followup(db,lead_id)
    lead=leads.get(lead_id)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==payload.binding_id)
                      .with_for_update().execution_options(populate_existing=True))
    ctx=dict(binding.native_context or {}) if binding else {}
    if (binding is None or binding.deleted_at or binding.lead_id!=lead_id or not binding.native_self_wxid
            or not binding.native_peer_wxid or binding.sales_id!=lead.sales_id):
        raise AppError('HOOK_REINVITE_IDENTITY_MISMATCH','原客户托管身份已变化，请刷新后核对',409)
    prior=ctx.get('reinvite_request')
    if prior and prior.get('original_revision')==payload.binding_revision:
        task=db.get(Task,prior['task_id'])
        if task and task.lead_id==lead_id and prior['reason']==payload.reason.strip():
            return {'created':False,'task':task_service.task_to_detail(task),'previous_binding_id':binding.id}
        raise AppError('HOOK_REINVITE_REQUEST_CONFLICT','原重新邀请记录不可覆盖',409)
    repeat = (binding.bind_status == 'disabled' and binding.disable_reason == REINVITE_REASON
              and ctx.get('business_enabled') is False and prior)
    if (not payload.confirm_end_previous or binding.authorization_revision!=payload.binding_revision
            or binding.replacement_binding_id or not payload.reason.strip()
            or not (repeat or (binding.bind_status=='bound' and ctx.get('business_enabled') is True))):
        raise AppError('HOOK_REINVITE_NOT_AUTHORIZED','旧托管状态已变化，不能自动覆盖或恢复',409)
    proof=db.get(TaskEvidence,ctx.get('invite_evidence_id'))
    if proof is None or (proof.extra_metadata or {}).get('backend_validated') is not True:
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED','缺少原系统邀请凭证',409)
    if task_service._active_add_friend_task(db,lead_id):
        raise AppError('HOOK_REINVITE_TASK_PENDING','已有加好友任务待处理，请先核对任务结果',409)
    if repeat:
        previous_task = db.get(Task, prior['task_id'])
        if previous_task is None or previous_task.status not in {'completed','failed','cancelled','canceled'}:
            raise AppError('HOOK_REINVITE_TASK_PENDING','原重新邀请尚未收尾',409)
    conversation = db.get(Conversation, binding.conversation_id)
    if conversation and conversation.status == 'rejected':
        raise AppError('HOOK_REINVITE_NOT_AUTHORIZED','客户已拒绝跟进，不能重新邀请',409)
    original_revision=binding.authorization_revision
    result=task_service.create_add_friend_task(db,lead_id=lead_id,actor=actor,
        sales_id=binding.sales_id,worker_id=binding.worker_id,remark='运营确认结束旧托管后重新邀请：'+payload.reason.strip())
    if not result.get('created'):raise AppError('HOOK_REINVITE_TASK_PENDING','已有加好友任务待处理',409)
    task_id=result['task']['id']
    retire_binding(db,binding,task_id,payload.reason.strip(),actor,repeat=bool(repeat))
    db.add(TaskEvidence(task_id=task_id,worker_id=binding.worker_id,evidence_type='hook_reinvite_authorization',
        extra_metadata={'previous_binding_id':binding.id,'previous_revision':original_revision,
            'self_wxid':binding.native_self_wxid,'account_epoch':ctx.get('account_epoch'),
            'peer_wxid':binding.native_peer_wxid,'lead_id':lead_id,'sales_id':binding.sales_id}))
    write_log(db,actor,event_type='hook_customer_reinvite_authorized',module='wechat',
        target_type='wechat_session_binding',target_id=binding.id,lead_id=lead_id,
        before_data={'binding_revision':original_revision},after_data={'binding_revision':binding.authorization_revision,
            'task_id':task_id,'status':'disabled'},metadata={'reason':payload.reason.strip(),'original_receipts_preserved':True})
    db.flush()
    return {**result,'previous_binding_id':binding.id}


def retire_binding(db, binding, task_id, reason, actor, *, repeat=False):
    """One retirement boundary for reinvitation and operator lead succession."""
    from app.services import wechat_service, c3_service
    original_revision=binding.authorization_revision
    ctx=dict(binding.native_context or {})
    wechat_service._set_binding_state(binding,status='disabled',listen_status='disabled',allow_listening=False,
        error_code='HOOK_AUTHORIZATION_RETIRED',remark_code=binding.remark_code,preserve_lead=True)
    if repeat:
        binding.authorization_revision = original_revision + 1
    binding.disable_reason=REINVITE_REASON;binding.disabled_at=utcnow();binding.disabled_by=str(actor.operator_id)
    ctx.setdefault('retired_authorization_revision',original_revision)
    ctx['business_enabled']=False
    ctx['reinvite_request']={'task_id':task_id,'original_revision':original_revision,
        'reason':reason,'requested_at':utcnow().isoformat()}
    binding.native_context=ctx
    conversation=db.get(Conversation,binding.conversation_id)
    if conversation:
        conversation.ai_enabled=False;conversation.status='closed'
        conversation.recall_cycle_id=conversation.recall_origin_status=conversation.next_recall_at=None
        c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,reason='HOOK_AUTHORIZATION_RETIRED')


def replacement_predecessor(db,w,task,peer,existing):
    authorization=db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==task.id,
        TaskEvidence.evidence_type=='hook_reinvite_authorization'))
    proof=authorization.extra_metadata or {} if authorization else {}
    predecessor=next((b for b in existing if b.id==proof.get('previous_binding_id')),None)
    ctx=predecessor.native_context or {} if predecessor else {}
    from app.services.hook_binding_transfer import TRANSFER
    previous_lead_id=proof.get('previous_lead_id') if proof.get('mode')==TRANSFER else task.lead_id
    scope={'self_wxid':w.hook_self_wxid,'account_epoch':w.hook_account_epoch,
           'peer_wxid':peer,'lead_id':task.lead_id,'sales_id':task.sales_id}
    if (predecessor and authorization.worker_id==w.id and predecessor.worker_id==w.id
            and predecessor.lead_id==previous_lead_id and predecessor.sales_id==task.sales_id
            and predecessor.native_self_wxid==w.hook_self_wxid and predecessor.native_peer_wxid==peer
            and ctx.get('account_epoch')==w.hook_account_epoch
            and all(proof.get(k)==v for k,v in scope.items())
            and predecessor.disable_reason==REINVITE_REASON
            and (predecessor.replacement_binding_id or (ctx.get('reinvite_request') or {}).get('task_id')!=task.id)):
        raise AppError('HOOK_INVITE_AUTHORIZATION_RETIRED','本轮邀请授权已结束，以新邀请任务为准',409)
    if (predecessor is None or authorization.worker_id!=w.id or predecessor.bind_status!='disabled'
            or predecessor.disable_reason!=REINVITE_REASON or predecessor.replacement_binding_id
            or ctx.get('business_enabled') is not False
            or (ctx.get('reinvite_request') or {}).get('task_id')!=task.id
            or proof.get('previous_revision')!=(ctx.get('reinvite_request') or {}).get('original_revision')
            or predecessor.worker_id!=w.id or predecessor.lead_id!=previous_lead_id or predecessor.sales_id!=task.sales_id
            or predecessor.native_self_wxid!=w.hook_self_wxid or predecessor.native_peer_wxid!=peer
            or ctx.get('account_epoch')!=w.hook_account_epoch
            or any(proof.get(k)!=v for k,v in {'self_wxid':w.hook_self_wxid,'account_epoch':w.hook_account_epoch,
                'peer_wxid':peer,'lead_id':task.lead_id,'sales_id':task.sales_id}.items())):
        raise AppError('HOOK_BINDING_CONFLICT','新邀请没有已确认退出的原托管授权，不能覆盖',409)
    require_binding_chain(existing, predecessor)
    return predecessor


def require_binding_chain(existing, current):
    """Allow only this binding and its retained disabled predecessors."""
    allowed={current.id}
    by_id={b.id:b for b in existing}
    while True:
        added={b.id for b in existing if b.bind_status=='disabled' and b.replacement_binding_id in allowed
               and b.worker_id==current.worker_id and (b.lead_id==current.lead_id
                   or (b.native_context or {}).get('successor_lead_id')==by_id[b.replacement_binding_id].lead_id)
               and b.sales_id==current.sales_id and b.native_self_wxid==current.native_self_wxid
               and b.native_peer_wxid==current.native_peer_wxid}
        if added<=allowed:break
        allowed.update(added)
    if any(b.id not in allowed for b in existing):
        raise AppError('HOOK_BINDING_CONFLICT','仍有其他客户或账号绑定，不能覆盖',409)


def ensure_send_authorization(binding):
    """Customer-scoped refusal shared by permit claim and physical begin."""
    if (binding.native_context or {}).get('relationship_review_required'):
        raise AppError(REMARK_CHANGED,'客户识别信息待核对，不能自动发送',409,{'suggested_action':'do_not_send'})
    if binding.native_self_wxid and binding.disable_reason==REINVITE_REASON:
        raise AppError('HOOK_AUTHORIZATION_RETIRED','旧托管已结束，只能核对原发送结果',409,{'suggested_action':'do_not_send'})


def retired_evidence_scope(binding, worker_id, payload):
    """Exact former authorization may archive evidence, never admit business work."""
    if binding is None:return False
    ctx=binding.native_context or {}
    return (not binding.deleted_at and binding.worker_id==worker_id
        and binding.bind_status=='disabled' and binding.disable_reason==REINVITE_REASON
        and not binding.allow_listening and ctx.get('business_enabled') is False
        and type(payload.get('binding_revision')) is int
        and payload['binding_revision']==ctx.get('retired_authorization_revision')
        and binding.native_self_wxid==payload.get('self_wxid')
        and binding.native_peer_wxid==payload.get('peer_wxid')
        and ctx.get('account_epoch')==payload.get('account_epoch'))
