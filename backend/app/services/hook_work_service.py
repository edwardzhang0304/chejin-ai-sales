"""Read-only native work inventory; it grants no Task claim or physical action."""
from datetime import timezone
from sqlalchemy import select
from app.models.wechat import WechatSessionBinding,MessageEvent
from app.models.task import Task,TaskEvidence
from app.models.c3 import Conversation,MessageBatch,ReplyAction,HandoffEvent
from app.services.hook_business_service import account
from app.services.followup_eligibility import followup_block_reason
from app.services import task_service


def inventory(db,authenticated,instance,payload):
    from app.schemas.hook_business import HookAccountAction
    w=account(db,authenticated,instance,HookAccountAction(**{k:getattr(payload,k) for k in ('self_wxid','account_epoch','process_generation')}))
    rows=list(db.scalars(select(WechatSessionBinding).where(WechatSessionBinding.worker_id==w.id,
        WechatSessionBinding.deleted_at.is_(None),WechatSessionBinding.bind_status=='bound',
        WechatSessionBinding.allow_listening.is_(True),WechatSessionBinding.native_self_wxid==payload.self_wxid)
        .order_by(WechatSessionBinding.id)))
    bindings=[]
    source_tasks={str((b.native_context or {}).get('source_task_id') or '') for b in
        db.scalars(select(WechatSessionBinding).where(WechatSessionBinding.worker_id==w.id))}
    for binding in rows:
        ctx=binding.native_context or {}
        proof=db.get(TaskEvidence,ctx.get('invite_evidence_id')) if ctx.get('invite_evidence_id') else None
        if (ctx.get('business_enabled') is not True or ctx.get('account_epoch')!=payload.account_epoch
                or proof is None or (proof.extra_metadata or {}).get('backend_validated') is not True
                or followup_block_reason(db,binding.lead_id,lock=False)):
            continue
        source_tasks.add(ctx['source_task_id'])
        from app.services.hook_history_service import human_covered_voice_ids
        history_ids=ctx.get('history_message_event_ids',[])
        covered=human_covered_voice_ids(db,binding)
        voices=[];retained_voices=[]
        from app.services.reply_ownership import human_owned, state
        conversation=db.get(Conversation,binding.conversation_id)
        human=bool(conversation and human_owned(conversation))
        for message in db.scalars(select(MessageEvent).where(MessageEvent.binding_id==binding.id,
                MessageEvent.sender_role=='customer',MessageEvent.message_type=='voice',
                MessageEvent.item_state.in_({'metadata_observed','uncertain'}))):
            if message.id not in history_ids:continue
            # An admitted unknown result already handed to sales is terminal
            # for automation, not a transcription job to re-advertise forever.
            if message.item_state=='uncertain' and message.id in ctx.get('media_handoff_applied_ids',[]):continue
            (retained_voices if human or message.id in covered else voices).append({'native_msg_id':message.raw_payload['native_msg_id'],
                'voice_metadata_xml':(message.raw_payload or {}).get('native_media_xml'),
                'message_event_id':message.id,'item_state':message.item_state})
        batches=list(db.scalars(select(MessageBatch.id).where(MessageBatch.conversation_id==binding.conversation_id,
            MessageBatch.deleted_at.is_(None),MessageBatch.status.in_({'collecting','generating','retry_wait','reply_action_created'}))
            .order_by(MessageBatch.created_at,MessageBatch.id)))
        bindings.append({'binding_id':binding.id,'binding_revision':binding.authorization_revision,
            'conversation_id':binding.conversation_id,'peer_wxid':binding.native_peer_wxid,
            'history_since_create_time':ctx['history_since_create_time'],'source_task_id':ctx['source_task_id'],
            'history_ready':ctx.get('history_ready') is True,'voice_jobs':voices,'retained_voice_jobs':retained_voices,'batch_ids':batches,
            'reply_ownership':state(conversation) if conversation else {},
            'remark_code':binding.remark_code or ''})
    invitations=[]
    for task in db.scalars(select(Task).where(Task.worker_id==w.id,Task.deleted_at.is_(None),
            Task.task_type=='add_friend',Task.status=='completed',Task.result_code=='invite_sent').order_by(Task.created_at,Task.id)):
        if task.id in source_tasks or followup_block_reason(db,task.lead_id,lock=False):continue
        proof=db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==task.id,TaskEvidence.evidence_type=='hook_invite_submission'))
        data=proof.extra_metadata or {} if proof else {}
        grant=db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==task.id,
            TaskEvidence.evidence_type=='hook_reinvite_authorization'))
        if grant:
            previous=db.get(WechatSessionBinding,(grant.extra_metadata or {}).get('previous_binding_id'))
            if (previous is None or previous.replacement_binding_id or
                    ((previous.native_context or {}).get('reinvite_request') or {}).get('task_id')!=task.id):
                continue
        if (proof and data.get('backend_validated') is True and data.get('self_wxid')==payload.self_wxid
                and data.get('account_epoch')==payload.account_epoch):
            invitations.append({'source_task_id':task.id})
    bindings=with_open_handoffs(db,w.id,payload.self_wxid,payload.account_epoch,bindings)
    flow=dict(w.inflight_flow_state or {})
    current=db.get(Task,flow.get('flow_id')) if flow.get('flow_id') else None
    current_task=task_service.task_to_detail(current) if current and current.worker_id==w.id else None
    retained=[]
    for action in db.scalars(select(ReplyAction).join(Task,Task.reply_action_id==ReplyAction.id)
            .where(Task.worker_id==w.id,Task.deleted_at.is_(None),ReplyAction.status.in_({'sending','unknown_send_result'}))):
        attempt=(action.ai_payload or {}).get('native_send_attempt')
        native=(action.ai_payload or {}).get('native_send_identity')
        if not native or (action.ai_payload or {}).get('native_send_settlement') or (action.ai_payload or {}).get('native_no_trigger_settlement'):continue
        task=db.scalar(select(Task).where(Task.reply_action_id==action.id,Task.deleted_at.is_(None)))
        binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==action.conversation_id))
        if binding is None:continue
        context=binding.native_context or {}
        reconciliation_binding=None
        if (native.get('worker_id')==w.id and native.get('client_instance_id')==instance
                and native.get('binding_id')==binding.id and native.get('peer_wxid')==binding.native_peer_wxid
                and native.get('self_wxid')==payload.self_wxid==binding.native_self_wxid
                and native.get('account_epoch')==payload.account_epoch==context.get('account_epoch')):
            reconciliation_binding={'binding_id':binding.id,'binding_revision':native['binding_revision'],
                'conversation_id':binding.conversation_id,'peer_wxid':native['peer_wxid'],
                'history_since_create_time':context['history_since_create_time'],
                'remark_code':binding.remark_code or ''}
        retained.append({'binding_id':binding.id,'task_id':task.id,'reply_action_id':action.id,'batch_id':action.batch_id,
            'flow_id':native['flow_id'],'lease_fencing_token':native['lease_fencing_token'],
            'reconciliation_binding':reconciliation_binding,
            'journal_sha256':attempt['journal_sha256'] if attempt else None,'original_process_generation':native['process_generation'],
            'physical_begin_registered':bool(attempt)})
    retired_evidence=retired_evidence_inventory(db,w,payload)
    retained_task=db.get(Task,payload.retained_flow_id) if payload.retained_flow_id else None
    retained_flow_terminal=bool(retained_task and retained_task.worker_id==w.id
        and retained_task.status in {'completed','failed','cancelled','canceled'} and not flow.get('flow_id'))
    return {'retained_flow_terminal':retained_flow_terminal,'schema_version':'hook-native-work-v1',**payload.model_dump(),'run_status':w.run_status,
        'inflight_flow':flow,'inflight_task':current_task,
        'inflight_batch_id':(db.get(ReplyAction,current.reply_action_id).batch_id if current and current.reply_action_id and db.get(ReplyAction,current.reply_action_id) else None),'bindings':bindings,'invitations':invitations,
        'relationship_query_version':1,'relationship_bindings':[{k:b[k] for k in ('binding_id','binding_revision','peer_wxid','remark_code')} for b in bindings],
        'retired_evidence':retired_evidence,'retained_sends':retained,'can_send':False,'observation_query_version':1}


def with_open_handoffs(db,worker_id,self_wxid,account_epoch,bindings):
    """Enrich only already-authorized bindings; never grant generation/send."""
    result=[]
    for item in bindings:
        binding=db.get(WechatSessionBinding,item.get('binding_id'))
        ctx=binding.native_context or {} if binding else {}
        if (binding is None or binding.deleted_at is not None or binding.worker_id!=worker_id
                or binding.bind_status!='bound' or not binding.allow_listening
                or binding.native_self_wxid!=self_wxid
                or binding.native_peer_wxid!=item.get('peer_wxid')
                or binding.conversation_id!=item.get('conversation_id')
                or binding.authorization_revision!=item.get('binding_revision')
                or ctx.get('account_epoch')!=account_epoch or ctx.get('business_enabled') is not True):
            continue
        # No reason_detail, AI payload, message body, or client secrets leave
        # this view. closed_at is the original authoritative takeover gate.
        proof=db.get(TaskEvidence,ctx.get('invite_evidence_id')) if ctx.get('invite_evidence_id') else None
        if (proof is None or (proof.extra_metadata or {}).get('backend_validated') is not True
                or followup_block_reason(db,binding.lead_id,lock=False)):continue
        events=list(db.scalars(select(HandoffEvent).where(
            HandoffEvent.conversation_id==binding.conversation_id,
            HandoffEvent.closed_at.is_(None),HandoffEvent.deleted_at.is_(None))
            .order_by(HandoffEvent.created_at,HandoffEvent.id)))
        records=[{'id':e.id,'batch_id':e.batch_id,'handoff_reason_code':e.handoff_reason_code,
                  'created_at':(e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=timezone.utc)).isoformat(),
                  'progress_native_ids':[str((m.raw_payload or {}).get('native_msg_id')) for m in
                      db.scalars(select(MessageEvent).where(MessageEvent.binding_id==binding.id,
                          MessageEvent.id.in_(e.trigger_message_event_ids or []),MessageEvent.sender_role=='customer'))
                      if (m.raw_payload or {}).get('native_msg_id')]} for e in events]
        result.append({**item,'open_handoffs':records})
    return result


def retired_evidence_inventory(db,w,payload):
    """Advertise only archival retry identity, never a listening/transcription job."""
    from app.services.hook_relationship_service import retired_evidence_scope
    result=[]
    for binding in db.scalars(select(WechatSessionBinding).where(
            WechatSessionBinding.worker_id==w.id,WechatSessionBinding.bind_status=='disabled',
            WechatSessionBinding.disable_reason=='operator_reinvite')):
        ctx=binding.native_context or {}
        scope={'self_wxid':payload.self_wxid,'account_epoch':payload.account_epoch,
            'peer_wxid':binding.native_peer_wxid,'binding_revision':ctx.get('retired_authorization_revision')}
        proof=db.get(TaskEvidence,ctx.get('invite_evidence_id')) if ctx.get('invite_evidence_id') else None
        if (not retired_evidence_scope(binding,w.id,scope) or proof is None
                or (proof.extra_metadata or {}).get('backend_validated') is not True):continue
        voices=[]
        for parent in db.scalars(select(MessageEvent).where(MessageEvent.binding_id==binding.id,
                MessageEvent.message_type=='voice',MessageEvent.sender_role=='customer')):
            if parent.id not in ctx.get('history_message_event_ids',[]):continue
            voices.append({'native_msg_id':parent.raw_payload['native_msg_id']})
        result.append({'binding_id':binding.id,'binding_revision':scope['binding_revision'],
            'peer_wxid':binding.native_peer_wxid,'voice_jobs':voices})
    return result
