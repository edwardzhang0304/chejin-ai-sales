"""Server-owned history range, original message batches and Brain context.

This module admits verified native text history. Physical native sending has a
separate permit, journal and reconciliation boundary; a model result cannot by
itself authorize a Hook call.
"""
from datetime import datetime, timezone, timedelta
from fastapi.encoders import jsonable_encoder
from types import SimpleNamespace
from sqlalchemy import select
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction
from app.models.hook_native import HookHistoryReceipt, HookNativeReceipt
from app.models.task import TaskEvidence
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services.followup_eligibility import lock_leads, require_followup
from app.services.hook_business_service import account
from app.services.hook_message_service import message_key


def ensure_history_context(binding):
    """Validate identity/generation independently of content readiness."""
    if not binding.native_self_wxid:return
    ctx=binding.native_context or {}; worker=binding.worker
    runtime=worker.hook_runtime_context or {} if worker else {}
    if (ctx.get('coverage_process_generation')!=runtime.get('process_generation')
            or not worker or not worker.enabled or runtime.get('blocked') or worker.wechat_status!='logged_in'
            or worker.hook_self_wxid!=binding.native_self_wxid or worker.hook_account_epoch!=ctx.get('account_epoch')):
        raise AppError('HOOK_HISTORY_COVERAGE_REQUIRED','申请后的原生历史尚未完整同步或顺序尚未确认',409)


def ensure_coverage(binding):
    ensure_history_context(binding)
    if binding.native_self_wxid and (binding.native_context or {}).get('history_ready') is not True:
        raise AppError('HOOK_HISTORY_COVERAGE_REQUIRED','申请后的原生历史尚未完整同步或顺序尚未确认',409)


def ordered_messages(db,binding):
    ensure_coverage(binding)
    ids=(binding.native_context or {}).get('history_message_event_ids',[])
    rows={m.id:m for m in db.scalars(select(MessageEvent).where(MessageEvent.id.in_(ids),
        MessageEvent.conversation_id==binding.conversation_id))}
    if len(rows)!=len(ids):raise AppError('HOOK_HISTORY_FACTS_MISSING','历史事实尚未可靠入库',409)
    return [rows[mid] for mid in ids]


def _apply_history_actors(db, binding, conv, rows, ctx, checked):
    """Apply verified sender/order facts independently of media transcription.

    The caller must have complete ordered history and no unresolved send actor.
    This settles human business replies only; it never completes a native call.
    """
    from app.services import c3_service
    applied=set(ctx.get('actor_applied_message_ids',[]))
    from app.services.hook_customer_notice_service import handoff_history_orders
    positions={r['MsgSvrID']:int(r['Sequence']) for r in checked['semantic_rows']}
    native_orders={m.id:positions[(m.raw_payload or {})['native_msg_id']] for m in rows}
    handoff_orders=handoff_history_orders(db,binding,checked,ctx)
    for message in rows:
        if message.id in applied:continue
        at=c3_service._aware_datetime(message.occurred_at)
        if message.sender_role=='customer':
            conv.last_inbound_at=max(filter(None,[c3_service._aware_datetime(conv.last_inbound_at),at]))
            conv.recall_cycle_id=conv.recall_origin_status=conv.next_recall_at=None
        else:
            raw=dict(message.raw_payload or {})
            conv.last_outbound_at=max(filter(None,[c3_service._aware_datetime(conv.last_outbound_at),at]))
            if raw.get('sender_source') in {'unresolved','human'}:
                raw['sender_source']='human';message.raw_payload=raw
                resolution=c3_service.close_open_handoffs_for_human_sales(db,
                    conversation_id=binding.conversation_id,sales_message=message,native_history_orders=native_orders,
                    native_handoff_orders=handoff_orders)
                if resolution['remaining_open_count']:
                    conv.status='waiting_sales_reply'
                else:
                    if resolution['resume_ai']:conv.ai_enabled=True
                    conv.last_sales_reply_at=max(filter(None,[c3_service._aware_datetime(conv.last_sales_reply_at),at]))
                    conv.sales_first_reply_at=conv.sales_first_reply_at or at
                    conv.status='sales_replied_waiting_user';conv.handoff_reason_code=None
                    from app.core.config import get_settings
                    conv.next_recall_at=at+timedelta(hours=get_settings().c3_recall_after_hours)
                    c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,
                        reason='销售已人工回复，取消未发送 AI 回复')
        applied.add(message.id)
    ctx['actor_applied_message_ids']=[m.id for m in rows if m.id in applied]
    ctx['actor_history_receipt_id']=ctx['active_history_receipt_id']
    binding.native_context=dict(ctx)


def human_covered_voice_ids(db, binding, *, verified_rows=None):
    """One trusted human/order boundary for history, inventory and late receipts.

    The history producer can reuse its validated rows. Other callers revalidate
    the saved coverage (not transcript readiness): an arbitrary/ambiguous self
    row must never exempt a voice. Original media/call states remain untouched.
    """
    ctx = binding.native_context or {}
    rows = verified_rows
    if rows is None:
        receipt_id = ctx.get('actor_history_receipt_id') or ctx.get('active_history_receipt_id')
        receipt = db.get(HookHistoryReceipt, receipt_id) if receipt_id else None
        if (receipt is None or receipt.binding_id != binding.id or receipt.worker_id != binding.worker_id
                or receipt.payload_json.get('self_wxid') != binding.native_self_wxid
                or receipt.payload_json.get('account_epoch') != ctx.get('account_epoch')):
            return set()
        try:
            checked = shared_adapter('hook_history_rules').validate_coverage(
                receipt.payload_json['coverage'], binding.native_peer_wxid, int(ctx['history_since_create_time']))
        except (ValueError, TypeError, KeyError):
            return set()
        if not checked['ready']:
            return set()
        ids = receipt.result_json.get('message_event_ids', [])
        saved = {m.id:m for m in db.scalars(select(MessageEvent).where(
            MessageEvent.binding_id == binding.id, MessageEvent.conversation_id == binding.conversation_id,
            MessageEvent.id.in_(ids)))}
        rows = [saved[mid] for mid in ids if mid in saved]
        if (len(rows) != len(ids) or [m.raw_payload.get('native_msg_id') for m in rows]
                != [r['MsgSvrID'] for r in checked['semantic_rows']]):
            return set()
    applied = set(ctx.get('actor_applied_message_ids', []))
    last_human = max((i for i, message in enumerate(rows)
        if message.id in applied and message.sender_role == 'self'
        and (message.raw_payload or {}).get('sender_source') == 'human'), default=-1)
    return {message.id for message in rows[:last_human + 1]
        if message.sender_role == 'customer' and message.message_type == 'voice'}


def _reply_history_state(message, covered):
    # Only a result received AFTER proved human handling has a saved projection.
    # Preserve that exact checkpoint, not a fabricated success or transcript.
    # If the human/order proof no longer holds, use the real state as usual.
    prior=((message.raw_payload or {}).get('native_voice_result') or {}).get('human_covered_history_state')
    if message.id in covered and prior is not None:
        return [prior['content'],prior['item_state']]
    return [message.content,message.item_state]


def accept_coverage(db,authenticated,instance,payload):
    hint=db.get(WechatSessionBinding,payload.binding_id)
    lock_leads(db,[hint.lead_id] if hint else [])
    w=account(db,authenticated,instance,payload)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==payload.binding_id)
        .with_for_update().execution_options(populate_existing=True))
    ctx=dict(binding.native_context or {}) if binding else {}
    reconciliation=payload.reconciliation
    if (reconciliation is None and binding and not binding.deleted_at
            and binding.worker_id==w.id and binding.native_self_wxid==payload.self_wxid
            and ctx.get('account_epoch')==payload.account_epoch and binding.disable_reason=='operator_reinvite'
            and binding.bind_status=='disabled' and payload.binding_revision < binding.authorization_revision):
        raise AppError('HOOK_AUTHORIZATION_RETIRED','旧托管已结束，仅保留原发送核对',409)
    if reconciliation is None and (binding is None or binding.deleted_at or binding.worker_id!=w.id or binding.bind_status!='bound'
            or not binding.allow_listening or binding.listen_status not in {'listening','degraded'}
            or binding.authorization_revision!=payload.binding_revision or ctx.get('business_enabled') is not True
            or ctx.get('account_epoch')!=payload.account_epoch or binding.native_self_wxid!=payload.self_wxid):
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED','客户绑定或历史权限已变化',409)
    if reconciliation is None:
        require_followup(db,binding.lead_id)
    else:
        from app.services.hook_send_service import original_send
        _,original_binding,reconciled_action,_=original_send(db,w,instance,reconciliation.reply_action_id,
            reconciliation,reconciliation.flow_id,reconciliation.lease_fencing_token)
        native=reconciled_action.ai_payload['native_send_identity']
        if (binding is None or original_binding.id!=binding.id
                or payload.binding_revision!=native.get('binding_revision')
                or payload.self_wxid!=native.get('self_wxid') or payload.account_epoch!=native.get('account_epoch')
                or binding.native_self_wxid!=payload.self_wxid or ctx.get('account_epoch')!=payload.account_epoch):
            raise AppError('HOOK_SEND_SETTLEMENT_MISMATCH','历史核对必须属于原发送账号和客户',409)

    proof=db.get(TaskEvidence,ctx.get('invite_evidence_id'))
    if proof is None or (proof.extra_metadata or {}).get('backend_validated') is not True:
        raise AppError('HOOK_INVITE_ORIGIN_REQUIRED','缺少正式系统申请证据',409)
    rules=shared_adapter('hook_contract_rules'); history=shared_adapter('hook_history_rules')
    if rules.digest(payload.coverage)!=payload.coverage_digest:
        raise AppError('HOOK_HISTORY_DIGEST_MISMATCH','历史证据摘要不一致',409)
    value=payload.model_dump(exclude_none=True); digest=rules.digest(value)
    old=db.scalar(select(HookHistoryReceipt).where(HookHistoryReceipt.worker_id==w.id,
        HookHistoryReceipt.capture_id==payload.capture_id))
    if old:
        if old.payload_digest!=digest:raise AppError('HOOK_HISTORY_CAPTURE_CONFLICT','原历史回执不能覆盖',409)
        return old.result_json
    try:
        checked=history.validate_coverage(payload.coverage,binding.native_peer_wxid,int(ctx['history_since_create_time']))
        if history.utc(checked['observed_at'])>utcnow():raise ValueError('HOOK_HISTORY_CLOCK_INVALID')
    except (ValueError,TypeError,KeyError):
        raise AppError('HOOK_HISTORY_COVERAGE_INVALID','历史范围、分页或重复读取核验未通过',409) from None
    receipt=HookHistoryReceipt(worker_id=w.id,binding_id=binding.id,capture_id=payload.capture_id,
        payload_digest=digest,payload_json=value,result_json={})
    db.add(receipt);db.flush()
    ids=[];new_ids=[];seen_native=set(); reason=checked['reason_code']
    # A verified restriction can stop this customer's automation even while
    # outgoing server IDs are pending. It never admits history for new work.
    notice_ready = checked['ready'] or reason == history.OUTGOING_ID_PENDING
    for row in checked['semantic_rows']:
        role='customer' if row['IsSender']=='0' else 'self'
        kind={'1':'text','49':'text','34':'voice','3':'image'}[row['Type']]
        content=(checked['quote_links'][row['MsgSvrID']]['new_text'] if row['Type']=='49' else row['StrContent']) if kind=='text' else None
        key=message_key(payload.self_wxid,binding.native_peer_wxid,row['MsgSvrID'])
        event=db.scalar(select(MessageEvent).where(MessageEvent.conversation_id==binding.conversation_id,
            MessageEvent.dedupe_key==key))
        if event and (event.sender_role!=role or event.message_type!=kind or (kind=='text' and event.content!=content)):
            raise AppError('HOOK_MESSAGE_ID_CONFLICT','历史与已有原生消息事实不一致',409)
        at=datetime.fromtimestamp(int(row['CreateTime']),timezone.utc)
        if event is None:
            event=MessageEvent(worker_id=w.id,binding_id=binding.id,conversation_id=binding.conversation_id,
                lead_id=binding.lead_id,sales_id=binding.sales_id,
                rpa_session_key='hook:'+binding.id,read_run_id=payload.capture_id,source_message_key=key,dedupe_key=key,
                sender_role=role,message_type=kind,content=content,raw_payload={},evidence={},
                item_state='text_ready' if kind=='text' else 'metadata_observed',flow_state='stored',occurred_at=at,observed_at=history.utc(checked['observed_at']))
            db.add(event);db.flush();new_ids.append(event.id)
        raw=dict(event.raw_payload or {})
        raw.update({'transport':'hook','native_msg_id':row['MsgSvrID'],'self_wxid':payload.self_wxid,
            'peer_wxid':binding.native_peer_wxid,'account_epoch':payload.account_epoch,'native_source':'native_history',
            'direction':'incoming' if role=='customer' else 'outgoing',
            'sender_source':'customer' if role=='customer' else raw.get('sender_source','unresolved'),
            'ordering_fields':{k:row[k] for k in ('localId','CreateTime','Sequence','MsgServerSeq','MsgSequence')},
            'database':row['database'],'database_generation':row['database_generation']})
        if row['Type']=='49':raw['native_quote']=checked['quote_links'][row['MsgSvrID']]
        if kind!='text':raw['native_media_xml']=row['StrContent']
        event.raw_payload=raw
        # This time is provider CreateTime, never callback arrival or OCR position.
        event.occurred_at=at
        event.evidence={**(event.evidence or {}),'native_history_receipt_id':receipt.id}
        ids.append(event.id);seen_native.add(row['MsgSvrID'])
    # The sync barrier also includes every already accepted callback observation.
    for native in db.scalars(select(HookNativeReceipt).where(HookNativeReceipt.binding_id==binding.id)):
        for item in native.payload_json['messages']:
            if item['native_msg_id'] not in seen_native and item['native_msg_id'] not in checked['excluded_native_ids'] and item['native_msg_id'] not in checked['revoked_native_ids'] and item['native_msg_id'] not in checked['system_notice_native_ids']:
                reason=reason or 'HOOK_HISTORY_CALLBACK_NOT_COVERED'
                notice_ready = False
    withdrawn=set(checked['revoked_native_ids'])|set(checked['system_notice_native_ids'])
    for message in db.scalars(select(MessageEvent).where(MessageEvent.binding_id==binding.id)):
        if (message.raw_payload or {}).get('native_msg_id') in withdrawn and message.id not in ids:
            message.item_state='withdrawn'
            message.raw_payload={**message.raw_payload,'native_withdrawn':True,
                **({'native_system_notice':True} if (message.raw_payload or {}).get('native_msg_id') in checked['system_notice_native_ids'] else {})}
            message.evidence={**(message.evidence or {}),'native_withdrawn_history_receipt_id':receipt.id}
    # A late unique row may classify the actor of an already settled unknown
    # send, but cannot turn its immutable old receipt into a delivery guarantee.
    unresolved=db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==binding.conversation_id,
        ReplyAction.deleted_at.is_(None),ReplyAction.status.in_({'sending','unknown_send_result'}))).all()
    if reconciliation is not None:unresolved=[reconciled_action]
    binding.native_context={**ctx,'active_history_receipt_id':receipt.id}
    db.flush()
    from app.services.hook_send_service import candidate_events
    for action in unresolved:
        saved=(action.ai_payload or {}).get('native_send_settlement')
        if (action.status=='unknown_send_result' and saved
                and not (action.ai_payload or {}).get('native_actor_resolution')):
            candidates=candidate_events(db,action,binding,saved['request']['completed_at'])
            if len(candidates)==1:
                message=candidates[0]
                native_id=message.raw_payload['native_msg_id']
                action.ai_payload={**action.ai_payload,'native_actor_resolution':{
                    'native_msg_id':native_id,'history_receipt_id':receipt.id,
                    'observed_at':checked['observed_at'],'automatic_resend_allowed':False,
                    'original_settlement_unchanged':True}}
                message.raw_payload={**message.raw_payload,'sender_source':'ai',
                    'ai_reply_action_id':action.id,'native_association_method':'late_body_time_unique_history'}
    if any((a.ai_payload or {}).get('native_send_attempt') and not (a.ai_payload or {}).get('native_actor_resolution') for a in unresolved):
        reason=reason or 'HOOK_NATIVE_SEND_ACTOR_PENDING'
    rows_by_id={m.id:m for m in db.scalars(select(MessageEvent).where(MessageEvent.id.in_(ids)))}
    last_self=max((i for i,mid in enumerate(ids) if rows_by_id[mid].sender_role=='self'),default=-1)
    tail=[rows_by_id[mid] for mid in ids[last_self+1:] if rows_by_id[mid].sender_role=='customer']
    # An unclassified/partially acknowledged outgoing actor cannot close a handoff.
    if any(m.sender_role=='self' and (m.raw_payload or {}).get('sender_source')
            not in {'unresolved','human','ai'} for m in rows_by_id.values()):
        reason=reason or 'HOOK_NATIVE_SEND_ACTOR_PENDING'
    facts_ready=checked['ready'] and reason is None
    covered=human_covered_voice_ids(db,binding,verified_rows=[rows_by_id[mid] for mid in ids]) if facts_ready else set()
    from app.services.hook_customer_notice_service import notice_key
    notice_keys=sorted(notice_key(row,ctx) for row in checked['friend_verification_notices'])
    state_digest=rules.digest([payload.account_epoch,payload.process_generation,payload.binding_revision,
        checked['revoked_native_ids'],checked['system_notice_native_ids'],[[r['MsgSvrID'],r['Type'],r['IsSender'],r['StrContent'],r['Sequence'],r['MsgSequence'],
          *_reply_history_state(rows_by_id[ids[i]],covered)] for i,r in enumerate(checked['semantic_rows'])]]
          + ([checked['unresolved_outgoing_rows']] if checked['unresolved_outgoing_rows'] else [])
          + ([['friend_verification_notices',notice_keys]] if notice_keys else []))
    changed=ctx.get('history_state_digest')!=state_digest
    if reconciliation is None:
        ctx.update({'history_ready':False,'history_reason_code':reason,'active_history_receipt_id':receipt.id,
            'coverage_process_generation':payload.process_generation,'history_message_event_ids':ids,
            'history_state_digest':state_digest,'history_observed_at':checked['observed_at']})
        binding.native_context=ctx
        from app.services import c3_service
        previous_ids=set(ctx.get('previous_history_message_ids',[]))
        additions=[db.get(MessageEvent,mid) for mid in ids if mid not in previous_ids]
        native_attempts=[a for a in unresolved if (a.ai_payload or {}).get('native_send_attempt')]
        only_own_outgoing=bool(additions) and previous_ids.issubset(ids) and all(
            m.sender_role=='self' and ((m.raw_payload or {}).get('sender_source')=='ai' or native_attempts)
            for m in additions)
        ctx['previous_history_message_ids']=list(ids);binding.native_context=dict(ctx)
        if changed and not only_own_outgoing:
            c3_service.cancel_active_batches_for_conversation_change(db,binding.conversation_id,reason='原生历史事实或顺序已更新')
            c3_service.supersede_open_reply_actions_for_new_inbound(db,binding.conversation_id)
    rows=[rows_by_id[mid] for mid in ids]
    if facts_ready and reconciliation is None:
        conv=db.get(Conversation,binding.conversation_id)
        if conv and conv.status not in {'closed','rejected'}:
            _apply_history_actors(db,binding,conv,rows,ctx,checked)
    covered=human_covered_voice_ids(db,binding,verified_rows=rows) if facts_ready else set()
    if any(m.sender_role=='customer' and m.message_type=='voice'
            and m.item_state not in {'completed','failed'} and m.id not in covered for m in rows):
        reason=reason or 'HOOK_VOICE_PENDING'
        # Media content readiness cannot postpone a verified stop restriction.
    ready=checked['ready'] and reason is None
    if reconciliation is None:
        ctx.update(history_ready=ready,history_reason_code=reason)
        binding.native_context=dict(ctx)
    from app.services.observability_service import process_run_id_for_key, record_server_stage_best_effort
    process_run_id = process_run_id_for_key('hook_history', payload.capture_id)
    record_server_stage_best_effort(
        db, process_run_id=process_run_id, worker_id=w.id,
        conversation_id=binding.conversation_id, stage_name='hook.history_verified',
        component='backend', stable_key=payload.capture_id, trace_id=payload.capture_id,
    )
    # Customer business restrictions do not become Worker connection faults.
    # Keep any original send pending until its separate history settlement.
    if notice_ready and reason in {None, 'HOOK_NATIVE_SEND_ACTOR_PENDING', 'HOOK_VOICE_PENDING', history.OUTGOING_ID_PENDING}:
        from app.services.hook_customer_notice_service import apply_friend_verification_notice
        apply_friend_verification_notice(db, binding, checked, receipt, ctx)
    if checked['reason_code'] == history.OUTGOING_ID_PENDING and reconciliation is None:
        from app.services.hook_customer_notice_service import apply_unresolved_outgoing_notice
        apply_unresolved_outgoing_notice(db, binding, checked, receipt, ctx)
    batch=None
    if ready and reconciliation is None:
        conv=db.get(Conversation,binding.conversation_id)
        if conv and conv.status not in {'closed','rejected'}:
            rows=ordered_messages(db,binding)
            # Native successors get a distinct checkpoint; never fabricate a C2 frame.
            for next_action in db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==binding.conversation_id,
                    ReplyAction.current.is_(True),ReplyAction.status=='queued',ReplyAction.segment_index>1)):
                predecessor=next_action.predecessor_reply_action_id
                if any((m.raw_payload or {}).get('ai_reply_action_id')==predecessor
                        and (m.raw_payload or {}).get('sender_source')=='ai' for m in rows):
                    next_action.ai_payload={**(next_action.ai_payload or {}),'native_pre_send_checkpoint':{
                        'history_state_digest':state_digest,'history_receipt_id':receipt.id}}
            from app.services.hook_media_service import handoff_native_media
            media_handled=set(ctx.get('media_handoff_applied_ids',[]))
            for message in tail:
                if message.id in media_handled:continue
                reason_code=('HOOK_CUSTOMER_IMAGE_HANDOFF' if message.message_type=='image' else
                    'C2_VOICE_TRANSCRIBE_FAILED' if message.message_type=='voice' and message.item_state=='failed' else None)
                if reason_code:
                    handoff_native_media(db,binding,message,reason_code)
                    media_handled.add(message.id)
            ctx['media_handoff_applied_ids']=sorted(media_handled);binding.native_context=dict(ctx)
            open_handoffs=c3_service.enforce_open_handoff_gate(db,conv,for_update=True)
            last_self=max((i for i,m in enumerate(rows) if m.sender_role=='self'),default=-1)
            inbound=[m.id for m in rows[last_self+1:] if m.sender_role=='customer']
            if inbound and conv.ai_enabled and not open_handoffs:
                conv.status='ai_active'
                batch=c3_service.collect_customer_message_batch(db,conversation_id=binding.conversation_id,message_event_ids=inbound,trace_id=payload.capture_id)
            elif rows and conv.ai_enabled and not open_handoffs and w.run_status=='running':
                from app.services.wechat_service import _prepare_due_recall
                _prepare_due_recall(conv,binding=binding,worker=w,db=db)
                if conv.status=='recall_precheck' and conv.recall_cycle_id:
                    cycle=conv.recall_cycle_id
                    ctx['native_recall_precheck']={'cycle_id':cycle,'history_receipt_id':receipt.id,
                        'history_state_digest':state_digest,'observed_at':checked['observed_at'],
                        'callback_complete':True,'voice_complete':True}
                    binding.native_context=dict(ctx)
                    batch=c3_service.create_control_message_batch(db,conversation_id=binding.conversation_id,
                        trigger_type='recall',trigger_key=cycle,recall_cycle_id=cycle)
                    conv.status='ai_active';conv.next_recall_at=None
            elif not rows and conv.ai_enabled and not open_handoffs:
                batch=c3_service.create_control_message_batch(db,conversation_id=binding.conversation_id,
                    trigger_type='friend_welcome',trigger_key='hook-invite:'+ctx['source_task_id'])
    result={'process_run_id':process_run_id,'conversation_id':binding.conversation_id,'capture_id':payload.capture_id,'history_receipt_id':receipt.id,'binding_id':binding.id,
        'history_ready':ready,'reason_code':reason,'message_event_ids':ids,'new_message_event_ids':new_ids,
        'history_state_digest':state_digest,'coverage_digest':payload.coverage_digest,'message_batch':batch,'native_send_ready':False,
        'reconciliation_only':reconciliation is not None,
        # This admits only original unknown-result settlement, never new work
        # or a positive send proof. The raw two-pass snapshot is retained.
        'recovery_settlement_allowed':reconciliation is not None and checked['reason_code']==history.OUTGOING_ID_PENDING}
    result=jsonable_encoder(result)
    receipt.result_json=result;db.flush()
    return result
