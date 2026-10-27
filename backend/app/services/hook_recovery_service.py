"""Reconcile lost acknowledgements under original ownership, without issuing work.

Both outcomes are durable terminal receipts. Worker/Task locks serialize them
with registration/claim/begin, so a delayed request cannot resurrect the work.
"""
from uuid import UUID
from sqlalchemy import select
from app.errors import AppError
from app.core.request_context import ActorContext
from app.models.task import Task, TaskEvidence
from app.models.c3 import ReplyAction
from app.services import task_service, c3_service
from app.services.hook_business_service import account
from app.services.followup_eligibility import lock_leads

EVIDENCE_TYPE = 'hook_recovery_terminal'

def reconcile(db, authenticated, instance, payload):
    hint=db.get(Task,payload.task_id)
    lock_leads(db,[hint.lead_id] if hint else [])
    # Match each existing operation's lock order, including its root lead lock.
    from app.models.wechat import WechatSessionBinding
    if payload.kind=='flow_start' and payload.conversation_id:
        db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==payload.conversation_id,
            WechatSessionBinding.worker_id==authenticated.id).with_for_update())
    if payload.kind=='send_unpermitted':
        c3_service._lock_vehicle_facts_for_action(db,payload.reply_action_id)
    w=account(db,authenticated,instance,payload)
    if payload.kind in {'send_claim','send_unpermitted'}:
        from app.services.hook_send_service import identity
        identity(db,w,instance,payload.reply_action_id,payload,payload.flow_id,payload.lease_fencing_token,current=False)
    task=db.scalar(select(Task).where(Task.id==payload.task_id).with_for_update().execution_options(populate_existing=True))
    if task is None or task.worker_id!=w.id or task.deleted_at:
        raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原任务不属于当前客户端',409)
    request=payload.model_dump(exclude={'process_generation'})
    request['client_instance_id']=instance
    prior=db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==task.id,
        TaskEvidence.worker_id==w.id,TaskEvidence.evidence_type==EVIDENCE_TYPE))
    if prior:
        if prior.extra_metadata.get('request')!=request:
            raise AppError('HOOK_RECOVERY_RECEIPT_CONFLICT','原恢复凭据不可替换',409)
        return prior.extra_metadata['result']
    action=db.get(ReplyAction,task.reply_action_id) if task.reply_action_id else None
    conversation=action.conversation_id if action else None
    if (payload.flow_kind!=('chat_reply' if action else 'task') or payload.conversation_id!=conversation
            or task.task_type not in {'add_friend','chat_reply'}):
        raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原流程范围不一致',409)
    if payload.kind=='send_unpermitted':
        result=settle_unpermitted(db,w,instance,payload,task,action)
    elif payload.kind=='flow_start':
        if payload.flow_id!=task.id or payload.reply_action_id is not None or payload.lease_fencing_token is not None:
            raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原启动意图不一致',409)
        flow=w.inflight_flow_state or {}
        if flow.get('flow_id'):
            if (flow.get('flow_id')!=task.id or flow.get('flow_kind')!=payload.flow_kind
                    or flow.get('conversation_id')!=payload.conversation_id):
                raise AppError('HOOK_RECOVERY_FLOW_CONFLICT','后台存在其他流程',409)
            return {'status':'registered','flow_id':task.id,'may_call_hook':False}
        # Absence alone is insufficient: reject a claimed/started task, any
        # issued native authority, or retained invitation preparation.
        if (task.status not in {'pending','blocked'} or task.lease_owner_worker_id
                or task.lease_owner_client_instance_id or task.lease_fencing_token
                or task.claimed_at or (action and (action.send_token or action.status!='queued'))
                or db.scalar(select(TaskEvidence.id).where(TaskEvidence.task_id==task.id,
                    TaskEvidence.evidence_type.in_({'hook_invite_preparation','hook_invite_submission'})))):
            raise AppError('HOOK_RECOVERY_START_UNCONFIRMED','原任务已有执行证据，不能认定未开始',409)
        actor=ActorContext(UUID(w.id),w.worker_name,'worker',None,None,'hook-recovery')
        task_service.cancel_task(db,task.id,'HOOK_FLOW_START_NOT_REGISTERED',actor)
        if action:
            c3_service.handoff_unsent_reply_recovery_failure(db,reply_action_id=action.id,error_code='HOOK_FLOW_START_NOT_REGISTERED')
            if action.status=='queued':
                action.status='cancelled';action.current=False
                from app.services.reply_sequence_service import cancel_remaining
                cancel_remaining(db,action,'HOOK_FLOW_START_NOT_REGISTERED')
        result={'status':'not_started','flow_id':payload.flow_id,'task_id':task.id,
            'may_call_hook':False,'automatic_resend_allowed':False}
    else:
        from app.services.hook_send_service import settle_no_trigger
        native=(action.ai_payload or {}).get('native_send_identity',{}) if action else {}
        if (not action or payload.reply_action_id!=action.id
                or native.get('worker_id')!=w.id or native.get('client_instance_id')!=instance
                or native.get('task_id')!=task.id or native.get('flow_id')!=payload.flow_id
                or native.get('lease_fencing_token')!=payload.lease_fencing_token
                or native.get('self_wxid')!=payload.self_wxid or native.get('account_epoch')!=payload.account_epoch
                or native.get('process_generation')!=payload.original_process_generation):
            raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原发送授权身份不一致',409)
        # This uses the original send locks too. No server token/permit leaves
        # this endpoint, and physical-begin evidence always vetoes this route.
        if (action.ai_payload or {}).get('native_send_attempt'):
            raise AppError('HOOK_RECOVERY_SEND_JOURNAL_REQUIRED','已登记发送尝试，必须核对原发送凭证',409)
        from app.schemas.hook_business import HookNativeNoTrigger
        from app.contracts.shared_rules import shared_adapter
        from app.models.base import utcnow
        proof={'reply_action_id':action.id,'task_id':task.id,'phase':'not_triggered',
            'reason_code':'HOOK_RUNTIME_NOT_RUNNING','intent_sha256':None,'observed_at':utcnow().isoformat()}
        data=HookNativeNoTrigger(**{k:native[k] for k in ('self_wxid','account_epoch','process_generation')},
            task_id=task.id,send_token=action.send_token,proof=proof,proof_digest=shared_adapter('hook_contract_rules').digest(proof))
        settled=settle_no_trigger(db,w,instance,action.id,data,payload.flow_id,payload.lease_fencing_token)
        # Negative physical proof must not become an automatic replacement
        # reply. Reuse the established one-open-handoff disposition.
        from app.models.c3 import Conversation
        conv=db.get(Conversation,action.conversation_id)
        c3_service._create_send_failure_handoff(db,action=action,conversation=conv,
            error_code='HOOK_SEND_CLAIM_INTERRUPTED',send_result='failed')
        action.suggested_action='handoff'
        task.cancel_reason='HOOK_SEND_CLAIM_INTERRUPTED'
        result={'status':'not_triggered','flow_id':payload.flow_id,'task_id':task.id,
            'reply_action_id':action.id,'may_call_hook':False,'automatic_resend_allowed':False,'settlement':settled}
    db.add(TaskEvidence(task_id=task.id,worker_id=w.id,evidence_type=EVIDENCE_TYPE,
        extra_metadata={'request':request,'result':result}))
    db.flush()
    return result


def settle_unpermitted(db,w,instance,payload,task,action):
    """Close a refused original task only if no send authority was ever issued.

    Re-evaluate authoritative business rules under the original flow/lease.
    A client error code alone is never evidence that a task is safe to close.
    """
    from app.models.c3 import MessageBatch
    from app.models.wechat import WechatSessionBinding
    from app.services.followup_eligibility import require_followup, followup_block_reason
    from app.services.hook_history_service import ensure_history_context
    from app.services.message_effective_text import require_current_context
    flow=w.inflight_flow_state or {}
    origin=db.get(Task,payload.flow_id)
    origin_action=db.get(ReplyAction,origin.reply_action_id) if origin else None
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==action.conversation_id))
    if (payload.reply_action_id!=action.id or binding is None or binding.worker_id!=w.id
            or binding.native_self_wxid!=payload.self_wxid
            or (binding.native_context or {}).get('account_epoch')!=payload.account_epoch
            or payload.original_process_generation!=payload.process_generation
            or flow.get('flow_id')!=payload.flow_id or flow.get('flow_kind')!='chat_reply'
            or flow.get('conversation_id')!=action.conversation_id
            or not origin_action or origin.worker_id!=w.id or origin_action.batch_id!=action.batch_id):
        raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原未发送任务身份不一致',409)
    if (action.send_token or action.status in {'sending','sent','unknown_send_result'}
            or any((action.ai_payload or {}).get(k) for k in
                   ('native_send_identity','native_send_attempt','native_send_settlement','native_no_trigger_settlement'))):
        raise AppError('HOOK_RECOVERY_SEND_JOURNAL_REQUIRED','已有发送许可，必须核对原凭证',409)
    if (task.lease_fencing_token or None)!=payload.lease_fencing_token:
        raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原任务租约代次不一致',409)
    terminal=task.status in {'completed','failed','cancelled'}
    if not terminal:
        if task.status=='running':
            task_service.validate_task_lease(task,worker_id=w.id,client_instance_id=instance,
                lease_fencing_token=payload.lease_fencing_token)
        elif task.status not in {'pending','blocked'} or payload.lease_fencing_token is not None:
            raise AppError('HOOK_RECOVERY_IDENTITY_MISMATCH','原未发送任务状态不一致',409)
        # Identity/generation failures are never a customer refusal.
        ensure_history_context(binding)
        reason=None
        try:
            require_followup(db,binding.lead_id)
            conv=c3_service._conversation_for_binding(db,binding)
            c3_service._ensure_reply_action_send_eligible(db,binding=binding,conversation=conv,action=action,native_authorized=True)
            batch=db.get(MessageBatch,action.batch_id)
            require_current_context(db,batch)
            checkpoint=((action.ai_payload or {}).get('native_pre_send_checkpoint',{}) if action.segment_index>1
                        else (batch.ai_request_snapshot or {}).get('native_sync_checkpoint',{}))
            if checkpoint.get('history_state_digest')!=(binding.native_context or {}).get('history_state_digest'):
                reason='HOOK_SEND_CONTEXT_STALE'
            elif c3_service._is_past(action.expire_at):reason='REPLY_ACTION_EXPIRED'
            elif c3_service._stale_action_vehicle_ids(db,action,
                    locked_vehicles=c3_service._lock_vehicle_facts_for_action(db,action.id)):
                reason=c3_service.VEHICLE_FACT_STALE_CODE
            elif action.status in {'cancelled','expired','superseded','failed'}:reason='REPLY_ACTION_NOT_SENDABLE'
        except AppError as exc:
            if exc.code not in {'HUMAN_REPLY_OWNERSHIP_REQUIRED','CONVERSATION_NOT_ELIGIBLE',
                    'REPLY_THEN_HANDOFF_NOT_ELIGIBLE','HOOK_HISTORY_COVERAGE_REQUIRED',
                    'HOOK_FRIEND_VERIFICATION_REQUIRED','HOOK_CONTACT_REMARK_CHANGED','HOOK_AUTHORIZATION_RETIRED',
                    'HISTORICAL_TEXT_CONTEXT_STALE','LEAD_INVALID'}:raise
            reason=exc.code
        if reason is None:
            raise AppError('HOOK_RECOVERY_REFUSAL_UNCONFIRMED','任务仍可执行，不能猜测取消原因',409)
        conv=c3_service._conversation_for_binding(db,binding)
        c3_service._settle_customer_interruption(db,action=action,binding=binding,conversation=conv,
            followup_cancelled=bool(followup_block_reason(db,binding.lead_id)),cancellation_reason=reason)
        from app.services.reply_sequence_service import cancel_remaining
        cancel_remaining(db,action,reason)
    return {'status':'not_triggered','flow_id':payload.flow_id,'task_id':task.id,
        'reply_action_id':action.id,'may_call_hook':False,'physical_send_attempted':False,
        'automatic_resend_allowed':False,'already_terminal':terminal}


def unpermitted_inventory(db,worker,payload,flow,root_task):
    """Read-only identities from the original flow's batch, including stopped customers.

    This grants neither a lease nor closure: reconcile rechecks every request.
    Segment task IDs differ from the root Flow ID and must come from the server.
    """
    from app.models.wechat import WechatSessionBinding
    if (not root_task or root_task.worker_id!=worker.id or root_task.deleted_at
            or root_task.task_type!='chat_reply' or flow.get('flow_kind')!='chat_reply'):
        return []
    root=db.get(ReplyAction,root_task.reply_action_id)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==root.conversation_id)) if root else None
    if (not binding or binding.deleted_at or binding.worker_id!=worker.id
            or binding.native_self_wxid!=payload.self_wxid
            or (binding.native_context or {}).get('account_epoch')!=payload.account_epoch
            or flow.get('conversation_id')!=root.conversation_id):return []
    rows=db.execute(select(Task,ReplyAction).join(ReplyAction,Task.reply_action_id==ReplyAction.id).where(
        Task.worker_id==worker.id,Task.deleted_at.is_(None),Task.task_type=='chat_reply',
        ReplyAction.batch_id==root.batch_id,ReplyAction.generation_no==root.generation_no,
        ReplyAction.conversation_id==root.conversation_id,ReplyAction.deleted_at.is_(None),
        ReplyAction.send_token.is_(None)).order_by(ReplyAction.segment_index))
    return [{'task_id':task.id,'reply_action_id':action.id,'flow_id':flow['flow_id'],
        'batch_id':action.batch_id,'conversation_id':action.conversation_id,
        'lease_fencing_token':task.lease_fencing_token or None} for task,action in rows]
