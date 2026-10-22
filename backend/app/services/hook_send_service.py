"""Native text permits reuse original ReplyAction, task lease and settlement.

A Hook success flag is only submission evidence. Confirmation requires the
single matching new native history record within the recorded call interval.
Ambiguous/unknown results never authorize another physical send.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from sqlalchemy import select
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import ReplyAction, MessageBatch
from app.models.task import Task
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.hook_native import HookHistoryReceipt
from app.services import c3_service, task_service, worker_service
from app.services.hook_business_service import account
from app.services.hook_history_service import ensure_coverage, ordered_messages
from app.services.followup_eligibility import lock_leads


def identity(db,worker,instance,action_id,payload,flow,token,*,current=True):
    hint=db.get(ReplyAction,action_id)
    binding_hint=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==hint.conversation_id)) if hint else None
    lock_leads(db,[binding_hint.lead_id] if binding_hint else [])
    if current:c3_service._lock_vehicle_facts_for_action(db,action_id)
    if current:w=account(db,worker,instance,payload)
    else:w=worker_service._lock_worker(db,worker.id)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==binding_hint.id)
        .with_for_update().execution_options(populate_existing=True)) if binding_hint else None

    action=db.scalar(select(ReplyAction).where(ReplyAction.id==action_id).with_for_update().execution_options(populate_existing=True))
    task=db.scalar(select(Task).where(Task.id==payload.task_id).with_for_update().execution_options(populate_existing=True))
    if (binding is None or action is None or task is None or binding.worker_id!=w.id or not binding.native_self_wxid
            or task.worker_id!=w.id or task.reply_action_id!=action.id or task.task_type!='chat_reply'
            or w.client_instance_id!=instance):
        raise AppError('HOOK_SEND_IDENTITY_MISMATCH','原生发送动作与任务身份不一致',409)
    if current:
        worker_service.validate_inflight_continuation(w,flow)
        state=w.inflight_flow_state or {}
        if state.get('flow_kind')!='chat_reply' or state.get('conversation_id')!=action.conversation_id or w.run_status not in {'running','paused'}:
            raise AppError('HOOK_SEND_FLOW_REQUIRED','发送需持有本任务的在途流程',409)
        origin=db.get(Task,flow)
        origin_action=db.get(ReplyAction,origin.reply_action_id) if origin else None
        if origin is None or origin.worker_id!=w.id or origin_action is None or origin_action.batch_id!=action.batch_id:
            raise AppError('HOOK_SEND_FLOW_REQUIRED','分段发送必须属于同一批次的原在途流程',409)
        task_service.validate_task_lease(task,worker_id=w.id,client_instance_id=instance,lease_fencing_token=token)
        ensure_coverage(binding)
    return w,binding,action,task


def claim_native(db,worker,instance,action_id,payload,flow,token):
    w,binding,action,task=identity(db,worker,instance,action_id,payload,flow,token)
    ctx=binding.native_context or {};batch=db.get(MessageBatch,action.batch_id)
    if (payload.history_state_digest!=ctx.get('history_state_digest')
            or ((action.ai_payload or {}).get('native_pre_send_checkpoint',{}) if action.segment_index>1 else (batch.ai_request_snapshot or {}).get('native_sync_checkpoint',{})).get('history_state_digest')!=payload.history_state_digest):
        raise AppError('HOOK_SEND_CONTEXT_STALE','发送前历史与生成回复时的上下文不一致',409)
    if (action.ai_payload or {}).get('native_send_attempt'):
        raise AppError('HOOK_SEND_ALREADY_ATTEMPTED','已存在发送尝试，只能核对原回执',409)
    data=c3_service.claim_send(db,reply_action_id=action.id,task_id=task.id,worker_id=w.id,
        client_instance_id=instance,lease_fencing_token=token,native_authorized=True)
    identity_value={**payload.model_dump(),'worker_id':w.id,'client_instance_id':instance,
        'binding_id':binding.id,'binding_revision':binding.authorization_revision,'peer_wxid':binding.native_peer_wxid,
        'reply_action_id':action.id,'flow_id':flow,'lease_fencing_token':token,
        'baseline_native_ids':[(m.raw_payload or {})['native_msg_id'] for m in ordered_messages(db,binding)]}
    previous=(action.ai_payload or {}).get('native_send_identity')
    if previous and previous!=identity_value:raise AppError('HOOK_SEND_PERMIT_CONFLICT','原发送许可不可替换',409)
    action.ai_payload={**(action.ai_payload or {}),'native_send_identity':identity_value}
    db.flush()
    data['expire_at']=(action.expire_at.replace(tzinfo=timezone.utc) if action.expire_at.tzinfo is None else action.expire_at).isoformat()
    return {**data,**identity_value,'schema_version':'hook-native-send-v1','native_send_ready':True}


def begin_native(db,worker,instance,action_id,payload,flow,token):
    w,binding,action,task=identity(db,worker,instance,action_id,payload,flow,token)
    saved=(action.ai_payload or {}).get('native_send_identity',{})
    expected={k:saved.get(k) for k in ('self_wxid','account_epoch','process_generation','task_id','history_state_digest')}
    if (not saved or saved.get('flow_id')!=flow or saved.get('lease_fencing_token')!=token or any(getattr(payload,k)!=v for k,v in expected.items()) or action.send_token!=payload.send_token
            or action.status!='sending'):
        raise AppError('HOOK_SEND_PERMIT_CONFLICT','发送许可或同步上下文已经变化',409)
    conv=c3_service._conversation_for_binding(db,binding)
    c3_service._ensure_reply_action_send_eligible(db,binding=binding,conversation=conv,action=action,native_authorized=True)
    if (binding.native_context or {}).get('history_state_digest')!=payload.history_state_digest:
        raise AppError('HOOK_SEND_PERMIT_CONFLICT','发送许可或同步上下文已经变化',409)
    if c3_service._is_past(action.expire_at):raise AppError('REPLY_ACTION_EXPIRED','回复动作已过期',409)
    stale=c3_service._stale_action_vehicle_ids(db,action,locked_vehicles=c3_service._lock_vehicle_facts_for_action(db,action.id))
    if stale:raise AppError(c3_service.VEHICLE_FACT_STALE_CODE,'车辆事实已经变化',409)
    attempted=shared_adapter('hook_history_rules').utc(payload.attempted_at)
    claimed=action.sending_claimed_at.replace(tzinfo=timezone.utc) if action.sending_claimed_at.tzinfo is None else action.sending_claimed_at
    if attempted<claimed or attempted>utcnow():raise AppError('HOOK_SEND_CLOCK_INVALID','发送时钟核验未通过',409)
    value=payload.model_dump();old=(action.ai_payload or {}).get('native_send_attempt')
    if old:
        if old!=value:raise AppError('HOOK_SEND_ALREADY_ATTEMPTED','原发送尝试不能覆盖',409)
        return {'attempt_registered':True,'duplicated':True,'may_call_hook':False}
    action.ai_payload={**(action.ai_payload or {}),'native_send_attempt':value}
    db.flush()
    return {'attempt_registered':True,'duplicated':False,'may_call_hook':True}


def candidate_events(db,action,binding,completed_at):
    native=(action.ai_payload or {}).get('native_send_identity',{});attempt=(action.ai_payload or {}).get('native_send_attempt',{})
    if not native or not attempt:return []
    rules=shared_adapter('hook_history_rules')
    lower=int(rules.utc(attempt['attempted_at']).timestamp());upper=int(rules.utc(completed_at).timestamp())+1
    receipt=db.get(HookHistoryReceipt,(binding.native_context or {}).get('active_history_receipt_id'))
    if (receipt is None or receipt.binding_id!=binding.id or receipt.worker_id!=native.get('worker_id')
            or any(receipt.payload_json.get(k)!=native.get(k) for k in
                ('self_wxid','account_epoch','binding_revision'))):return []
    runtime=(binding.worker.hook_runtime_context or {}) if binding.worker else {}
    if (receipt.payload_json.get('process_generation')!=runtime.get('process_generation')
            or binding.worker.hook_account_epoch!=native.get('account_epoch')
            or binding.worker.hook_self_wxid!=native.get('self_wxid')):return []
    # Read only backend-validated, complete bounded scans. Readiness may be held
    # for pending actor reconciliation; that is not a fabricated missing page.
    data=rules.validate_coverage(receipt.payload_json['coverage'],binding.native_peer_wxid,
        int((binding.native_context or {})['history_since_create_time']))
    if not data['ready']:return []
    ids=[]
    for row in data['text_rows']:
        if (row['IsSender']=='1' and row['StrContent']==action.reply_text
                and row['MsgSvrID'] not in native['baseline_native_ids']
                and lower<=int(row['CreateTime'])<=upper):
            ids.append(row['MsgSvrID'])
    return [m for m in db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==action.conversation_id,
        MessageEvent.sender_role=='self')) if (m.raw_payload or {}).get('native_msg_id') in ids]


def original_send(db,worker,instance,action_id,payload,flow,token):
    """Validate original send evidence, independently of new-service eligibility."""
    w,binding,action,task=identity(db,worker,instance,action_id,payload,flow,token,current=False)
    native=(action.ai_payload or {}).get('native_send_identity',{})
    attempt=(action.ai_payload or {}).get('native_send_attempt',{})
    if (not native or not attempt or native.get('worker_id')!=w.id
            or native.get('client_instance_id')!=instance or native.get('task_id')!=task.id
            or native.get('binding_id')!=binding.id or native.get('peer_wxid')!=binding.native_peer_wxid
            or native.get('lease_fencing_token')!=token or native.get('flow_id')!=flow
            or payload.send_token!=action.send_token or payload.journal_sha256!=attempt.get('journal_sha256')
            or any(getattr(payload,k)!=native.get(k) for k in ('self_wxid','account_epoch','process_generation'))):
        raise AppError('HOOK_SEND_SETTLEMENT_MISMATCH','回执与原发送尝试不一致',409)
    return w,binding,action,task


def reconcile_native(db,worker,instance,action_id,payload,flow,token):
    w,binding,action,task=original_send(db,worker,instance,action_id,payload,flow,token)
    attempt=(action.ai_payload or {}).get('native_send_attempt',{})
    completed=shared_adapter('hook_history_rules').utc(payload.completed_at)
    if completed<shared_adapter('hook_history_rules').utc(attempt['attempted_at']) or completed>utcnow():
        raise AppError('HOOK_SEND_CLOCK_INVALID','调用完成时钟核验未通过',409)
    old=(action.ai_payload or {}).get('native_send_settlement')
    if old:
        if old['request']!=payload.model_dump():raise AppError('HOOK_SEND_SETTLEMENT_CONFLICT','已结算回执不可覆盖',409)
        return old['result']
    candidates=candidate_events(db,action,binding,payload.completed_at)
    confirmed=len(candidates)==1
    event=candidates[0] if confirmed else None
    if event:
        event.raw_payload={**event.raw_payload,'sender_source':'ai_pending_ack','ai_reply_action_id':action.id,
            'native_association_method':'body_time_unique_history'}
    report=SimpleNamespace(task_id=task.id,worker_id=w.id,client_instance_id=instance,send_token=payload.send_token,
        send_result='sent' if confirmed else 'unknown',action_phase='confirmed' if confirmed else 'trigger_attempted',
        reply_text_hash=action.reply_text_hash,sidecar_run_id=None,sent_at=event.occurred_at if event else None,
        error_code=None if confirmed else 'HOOK_NATIVE_SEND_UNCONFIRMED',remark='原生历史唯一正文与时间关联' if confirmed else '保留原发送尝试，不自动重发',
        evidence={'transport':'hook','journal_sha256':payload.journal_sha256,
            'native_msg_id':(event.raw_payload or {}).get('native_msg_id') if event else None,
            'candidate_count':len(candidates),'recipient_delivery_confirmed':False})
    if confirmed and (binding.native_context or {}).get('history_reason_code')=='HOOK_NATIVE_SEND_ACTOR_PENDING':
        runtime=w.hook_runtime_context or {}
        if (w.hook_account_epoch==payload.account_epoch and w.hook_self_wxid==payload.self_wxid
                and not runtime.get('blocked')):
            binding.native_context={**binding.native_context,'history_ready':True,'history_reason_code':None}
    # Original settlement reacquires binding/Worker locks with populate_existing.
    # Flush the validated coverage restoration and actor proof before that refresh.
    db.flush()
    result=c3_service.sent_ack(db,reply_action_id=action.id,payload=report,native_verified=True)
    # Settling a local send does not remove a customer-level handoff (e.g.
    # WeChat requires friend verification). The original ACK remains immutable.
    conversation=c3_service._conversation_for_binding(db,binding)
    if c3_service.enforce_open_handoff_gate(db,conversation):
        conversation.next_recall_at=None
    from fastapi.encoders import jsonable_encoder
    result=jsonable_encoder({**result,'native_msg_id':report.evidence['native_msg_id'],'local_send_observed':confirmed,
        'recipient_delivery_confirmed':False,'automatic_resend_allowed':False})
    action.ai_payload={**(action.ai_payload or {}),'native_send_settlement':{'request':payload.model_dump(),'result':result}}
    db.flush();return result


def observe_original_send(db,worker,instance,action_id,payload,flow,token):
    w,binding,action,task=original_send(db,worker,instance,action_id,payload,flow,token)
    attempt=(action.ai_payload or {}).get('native_send_attempt',{})
    completed=shared_adapter('hook_history_rules').utc(payload.completed_at)
    if completed<shared_adapter('hook_history_rules').utc(attempt['attempted_at']) or completed>utcnow():
        raise AppError('HOOK_SEND_CLOCK_INVALID','原发送观察时钟不正确',409)
    candidates=candidate_events(db,action,binding,payload.completed_at)
    from app.core.config import get_settings
    from datetime import timedelta
    deadline=shared_adapter('hook_history_rules').utc(attempt['attempted_at'])+timedelta(seconds=get_settings().c3_send_ack_stale_after_seconds)
    return {'candidate_count':len(candidates),'unique_local_send_observed':len(candidates)==1,
        'confirmation_deadline_utc':deadline.isoformat(),'deadline_elapsed':utcnow()>=deadline,
        'settlement':(action.ai_payload or {}).get('native_send_settlement'),'may_call_hook':False}


def settle_no_trigger(db,worker,instance,action_id,payload,flow,token):
    w,binding,action,task=identity(db,worker,instance,action_id,payload,flow,token,current=False)
    native=(action.ai_payload or {}).get('native_send_identity',{})
    attempt=(action.ai_payload or {}).get('native_send_attempt')
    rules=shared_adapter('hook_contract_rules');proof=payload.proof
    if (native.get('client_instance_id')!=instance or native.get('flow_id')!=flow
            or native.get('lease_fencing_token')!=token or payload.send_token!=action.send_token
            or any(payload.model_dump()[k]!=native.get(k) for k in ('self_wxid','account_epoch','process_generation'))
            or set(proof)!={'reply_action_id','task_id','phase','reason_code','intent_sha256','observed_at'}
            or proof.get('reply_action_id')!=action_id or proof.get('task_id')!=task.id or proof.get('phase')!='not_triggered'
            or proof.get('reason_code') not in {'HOOK_SEND_LATEST_CONTEXT_STALE','HOOK_RUNTIME_NOT_RUNNING','HOOK_SEND_SYNC_PENDING',
                'HOOK_SEND_PERMIT_EXPIRED_OR_CHANGED','HOOK_ORIGINAL_FLOW_CONTINUATION_BLOCKED'}
            or rules.digest(proof)!=payload.proof_digest
            or (attempt and proof.get('intent_sha256')!=attempt.get('journal_sha256'))):
        raise AppError('HOOK_SEND_NO_TRIGGER_PROOF_INVALID','未触发发送的原日志核验未通过',409)
    checked=shared_adapter('hook_history_rules').utc(proof['observed_at'])
    if checked>utcnow():raise AppError('HOOK_SEND_CLOCK_INVALID','未触发日志的时钟不正确',409)
    old=(action.ai_payload or {}).get('native_no_trigger_settlement')
    if old:
        if old['request']!=payload.model_dump():raise AppError('HOOK_SEND_SETTLEMENT_CONFLICT','原未触发回执不可覆盖',409)
        return old['result']
    report=SimpleNamespace(task_id=task.id,worker_id=w.id,client_instance_id=instance,send_token=payload.send_token,
        send_result='failed',action_phase='not_attempted',reply_text_hash=action.reply_text_hash,sidecar_run_id=None,
        sent_at=None,error_code='C3_CONTEXT_CHANGED_BEFORE_SEND',remark='原生发送前资格或上下文变化，未调用微信发送',
        evidence={'transport':'hook','native_no_trigger_verified':True,'proof_digest':payload.proof_digest})
    result=c3_service.sent_ack(db,reply_action_id=action.id,payload=report,native_verified=True)
    from fastapi.encoders import jsonable_encoder
    result=jsonable_encoder({**result,'physical_send_attempted':False,'automatic_resend_allowed':False})
    action.ai_payload={**(action.ai_payload or {}),'native_no_trigger_settlement':{'request':payload.model_dump(),'result':result}}
    db.flush();return result
