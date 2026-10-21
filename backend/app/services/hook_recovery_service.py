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
    w=account(db,authenticated,instance,payload)
    if payload.kind=='send_claim':
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
    if payload.kind=='flow_start':
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
