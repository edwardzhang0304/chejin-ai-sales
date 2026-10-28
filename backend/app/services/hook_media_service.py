"""Complete the original voice parent; native media never creates a fake C2 frame."""
from types import SimpleNamespace
from sqlalchemy import select
from app.errors import AppError
from app.contracts.shared_rules import shared_adapter
from app.models.hook_native import HookVoiceReceipt
from app.models.wechat import WechatSessionBinding,MessageEvent
from app.models.task import TaskEvidence
from app.models.c3 import Conversation
from app.models.base import utcnow
from app.services.followup_eligibility import lock_leads,require_followup
from app.services.hook_business_service import account
from app.services.hook_message_service import message_key


def finish_voice(db,authenticated,instance,payload):
    hint=db.get(WechatSessionBinding,payload.binding_id)
    lock_leads(db,[hint.lead_id] if hint else [])
    w=account(db,authenticated,instance,payload)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==payload.binding_id)
        .with_for_update().execution_options(populate_existing=True))
    ctx=dict(binding.native_context or {}) if binding else {}
    proof=db.get(TaskEvidence,ctx.get('invite_evidence_id')) if ctx.get('invite_evidence_id') else None
    from app.services.hook_relationship_service import retired_evidence_scope
    retired=retired_evidence_scope(db,binding,w.id,payload.model_dump())
    if (binding is None or binding.deleted_at or binding.worker_id!=w.id or (not retired and (binding.bind_status!='bound'
            or not binding.allow_listening or binding.listen_status not in {'listening','degraded'}))
            or binding.native_self_wxid!=payload.self_wxid or binding.native_peer_wxid!=payload.peer_wxid
            or (not retired and binding.authorization_revision!=payload.binding_revision) or ctx.get('account_epoch')!=payload.account_epoch
            or (not retired and ctx.get('business_enabled') is not True) or proof is None
            or (proof.extra_metadata or {}).get('backend_validated') is not True):
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED','语音客户的原绑定已失效',409)
    if not retired:require_followup(db,binding.lead_id)
    key=message_key(payload.self_wxid,payload.peer_wxid,payload.native_msg_id)
    parent=db.scalar(select(MessageEvent).where(MessageEvent.conversation_id==binding.conversation_id,
        MessageEvent.binding_id==binding.id,MessageEvent.dedupe_key==key).with_for_update())
    if (parent is None or parent.message_type!='voice' or parent.sender_role!='customer' or parent.worker_id!=w.id
            or parent.id not in ctx.get('history_message_event_ids',[])
            or (parent.raw_payload or {}).get('account_epoch')!=payload.account_epoch):
        raise AppError('HOOK_VOICE_PARENT_NOT_FOUND','没有已核验的客户语音父消息',409)
    from app.services.hook_history_review import needs_review
    rules=shared_adapter('hook_contract_rules')
    saved={key:getattr(payload,key) for key in ('self_wxid','account_epoch','native_msg_id','peer_wxid',
        'process_generation','audio_sha256','audio_bytes','state','attempted_at','completed_at','response_sha256','text','error_code')}
    saved['process_generation']=payload.attempt_process_generation or payload.process_generation
    if rules.digest(saved)!=payload.attempt_digest:
        raise AppError('HOOK_VOICE_ATTEMPT_DIGEST_MISMATCH','原语音转写记录摘要不一致',409)
    value=payload.model_dump(exclude={'attempt_process_generation'});value['process_generation']=saved['process_generation']
    digest=rules.digest(value)
    old=db.scalar(select(HookVoiceReceipt).where(HookVoiceReceipt.worker_id==w.id,
        HookVoiceReceipt.message_event_id==parent.id))
    if old:
        if old.payload_digest!=digest:raise AppError('HOOK_VOICE_RESULT_CONFLICT','原语音结果不可覆盖',409)
        if not retired and not needs_review(parent) and parent.item_state=='uncertain':_handoff_uncertain_voice(db,binding,parent)
        return old.result_json
    from app.services.hook_history_service import human_covered_voice_ids
    archived = needs_review(parent)
    covered=parent.id in human_covered_voice_ids(db,binding)
    prior_history_state={'content':parent.content,'item_state':parent.item_state} if covered else None
    receipt=HookVoiceReceipt(worker_id=w.id,message_event_id=parent.id,payload_digest=digest,payload_json=value,result_json={})
    db.add(receipt);db.flush()
    parent.item_state={'transcribed':'completed','failed':'failed','uncertain':'uncertain'}[payload.state]
    parent.content=payload.text if payload.state=='transcribed' else None
    parent.error_code=payload.error_code
    parent.raw_payload={**(parent.raw_payload or {}),'native_voice_result':{
        'state':payload.state,'audio_sha256':payload.audio_sha256,'audio_bytes':payload.audio_bytes,
        'attempt_digest':payload.attempt_digest,'response_sha256':payload.response_sha256,
        **({'human_covered_history_state':prior_history_state} if covered else {})}}
    parent.evidence={**(parent.evidence or {}),'native_voice_receipt_id':receipt.id}
    if archived:
        from app.services.hook_history_review import record_archived_change
        record_archived_change(db,parent,evidence_key='latest_voice_receipt_id',receipt_id=receipt.id)
    # Original native order/ID/timestamps remain; transcript is not a new input.
    # A proven old voice changes archival facts, not the customer's later turn.
    # Keep its pre-result checkpoint projection so the next history read cannot
    # invalidate the same generation/send indirectly through the full digest.
    if not covered and not retired and not archived:
        ctx['history_ready']=False;ctx['history_reason_code']='HOOK_VOICE_RESULT_REQUIRES_HISTORY'
        binding.native_context=ctx
        from app.services import c3_service
        c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,reason='原语音消息的转写结果已更新')
        c3_service.supersede_open_reply_actions_for_new_inbound(db,binding.conversation_id)
    result={'native_msg_id':payload.native_msg_id,'message_event_id':parent.id,'voice_receipt_id':receipt.id,
        'state':payload.state,'history_coverage_required':not covered and not retired and not archived,'ai_dispatch':False,
        **({'disposition':'authorization_retired'} if retired else {})}
    receipt.result_json=result
    if not retired and not needs_review(parent) and parent.item_state=='uncertain':_handoff_uncertain_voice(db,binding,parent)
    db.flush();return result



def _handoff_uncertain_voice(db,binding,message):
    # Business disposition is separate from the immutable unknown DLL result.
    # Replaying an old receipt may establish the handoff once, never reopen a
    # handoff the salesperson has already handled.
    ctx=dict(binding.native_context or {})
    applied=set(ctx.get('media_handoff_applied_ids',[]))
    if message.id in applied:return
    from app.services.hook_history_service import human_covered_voice_ids
    if message.id in human_covered_voice_ids(db,binding):
        # A first late receipt and a legacy replay share this disposition.
        # Remember it like an already handled handoff; a later incomplete scan
        # must not reopen it. This is not completion of the unknown DLL call.
        applied.add(message.id)
        binding.native_context={**ctx,'media_handoff_applied_ids':sorted(applied)}
        return
    event_id=handoff_native_media(db,binding,message,'HOOK_VOICE_RESULT_UNCONFIRMED')
    if event_id:
        applied.add(message.id)
        binding.native_context={**ctx,'media_handoff_applied_ids':sorted(applied)}


def handoff_native_media(db,binding,message,reason):
    """Use the original one-open-handoff and post-commit notification pipeline."""
    from app.services import c3_service
    conversation=db.get(Conversation,binding.conversation_id)
    if conversation is None or conversation.status in {'closed','rejected'} or not conversation.ai_enabled:
        return None
    event,created=c3_service._create_or_reuse_open_handoff(db,conversation=conversation,batch_id=None,
        handoff_reason_code=reason,reason_detail=('客户语音转写结果未确认，需要销售人工处理；原语音不会自动重试' if reason=='HOOK_VOICE_RESULT_UNCONFIRMED'
            else '客户发来图片，需要销售人工处理' if message.message_type=='image' else '客户语音转写失败，需要销售处理'),
        trigger_message_event_ids=[message.id],risk_flags=[reason.lower()],
        evidence_refs=['native_message:'+str((message.raw_payload or {}).get('native_msg_id'))],
        ai_payload={'source':'hook_native_media','parent_message_event_id':message.id,'image_understanding_called':False})
    conversation.status='waiting_sales_reply';conversation.handoff_reason_code=reason;conversation.handoff_at=utcnow()
    conversation.recall_origin_status=conversation.recall_cycle_id=conversation.next_recall_at=None
    c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,reason='客户媒体交由销售处理')
    c3_service.supersede_open_reply_actions_for_new_inbound(db,binding.conversation_id)
    return event.id
