"""Native message facts are independent of OCR frames and AI dispatch readiness.

Transport persistence and business facts have separate durable acknowledgements.
Historical coverage/order admission is a subsequent explicit gate; neither this
receipt nor raw callback arrival claims that the full AI context is complete.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib
from sqlalchemy import select
from app.errors import AppError
from app.models.base import utcnow
from app.models.hook_native import HookNativeReceipt
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.task import TaskEvidence
from app.services.followup_eligibility import lock_leads, require_followup
from app.services.hook_business_service import account
from app.contracts.shared_rules import shared_adapter


def message_key(self_wxid, peer_wxid, mid):
    return 'hook-msg:'+hashlib.sha256(f'{self_wxid}|{peer_wxid}|{mid}'.encode()).hexdigest()


def dispatch_facts(db, authenticated, instance, event_uuid, *, current_account=None):
    receipt=db.scalar(select(HookNativeReceipt).where(HookNativeReceipt.worker_id==authenticated.id,
        HookNativeReceipt.event_uuid==event_uuid))
    if receipt is None:raise AppError('HOOK_NATIVE_RECEIPT_NOT_FOUND','消息尚未可靠保存',404)
    payload=receipt.payload_json
    if payload['client_instance_id']!=instance:
        raise AppError('HOOK_REQUEST_IDENTITY_MISMATCH','消息不是由本客户端上报',409)
    hint=db.get(WechatSessionBinding,receipt.binding_id)
    lock_leads(db,[hint.lead_id] if hint else [])
    original_account=SimpleNamespace(**{key:payload[key]
        for key in ('self_wxid','account_epoch','process_generation')})
    if current_account and any(getattr(current_account,k)!=payload[k] for k in ('self_wxid','account_epoch')):
        raise AppError('HOOK_REQUEST_IDENTITY_MISMATCH','原消息与当前账号身份不一致',409)
    w=account(db,authenticated,instance,current_account or original_account)
    binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id==receipt.binding_id)
        .with_for_update().execution_options(populate_existing=True))
    receipt=db.scalar(select(HookNativeReceipt).where(HookNativeReceipt.id==receipt.id)
        .with_for_update().execution_options(populate_existing=True))
    ctx=dict(binding.native_context or {}) if binding else {}
    proof=db.get(TaskEvidence,ctx.get('invite_evidence_id')) if ctx.get('invite_evidence_id') else None
    from app.services.hook_relationship_service import retired_evidence_scope
    retired=retired_evidence_scope(db,binding,w.id,payload)
    if not retired:
        # Active business admission retains its original process-generation fence.
        account(db,authenticated,instance,original_account)
    if not retired:
        from app.services.hook_relationship_service import require_current_binding_read
        require_current_binding_read(binding,w.id,payload)
    if (binding is None or binding.deleted_at or binding.worker_id!=w.id or (not retired and (binding.bind_status!='bound'
            or not binding.allow_listening or binding.listen_status not in {'listening','degraded'}))
            or binding.native_self_wxid!=payload['self_wxid'] or binding.native_peer_wxid!=payload['peer_wxid']
            or ctx.get('account_epoch')!=payload['account_epoch']
            or (not retired and (int(binding.authorization_revision or 1)!=payload['binding_revision']
            or ctx.get('business_enabled') is not True)) or proof is None
            or (proof.extra_metadata or {}).get('backend_validated') is not True):
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED','客户正式绑定或消息授权已失效',409)
    if retired:
        # A durable transport receipt is not permission to create customer input.
        # Preserve any previously stored facts and immutable original envelope.
        if receipt.dispatch_status!='facts_stored':receipt.dispatch_status='authorization_retired'
        db.flush()
        return {'event_uuid':event_uuid,'dispatch_status':'authorization_retired',
            'message_event_ids':[],'new_message_event_ids':[],
            'ai_dispatch':'authorization_retired','can_send':False}
    require_followup(db,binding.lead_id)
    # Validate every existing ID before inserting any member of this envelope.
    try:
        messages=shared_adapter('hook_contract_rules').unique_message_facts(payload)
    except ValueError as exc:
        raise AppError('HOOK_MESSAGE_ID_CONFLICT','同一原生消息 ID 的事实不一致',409) from exc
    pairs=[]
    for message in messages:
        key=message_key(payload['self_wxid'],payload['peer_wxid'],message['native_msg_id'])
        old=db.scalar(select(MessageEvent).where(MessageEvent.conversation_id==binding.conversation_id,
            MessageEvent.dedupe_key==key))
        role='customer' if message['direction']=='incoming' else 'self'
        if old and (old.sender_role!=role or old.message_type!=message['message_type'] or (message['message_type']=='text' and old.content!=message['content'])):
            raise AppError('HOOK_MESSAGE_ID_CONFLICT','同一原生消息 ID 的事实不一致，停止处理',409)
        pairs.append((message,key,role,old))
    ids=[]; inserted=[]
    observed=datetime.fromisoformat(payload['observed_at'].replace('Z','+00:00'))
    for message,key,role,old in pairs:
        if old:
            ids.append(old.id);continue
        event=MessageEvent(conversation_id=binding.conversation_id,binding_id=binding.id,
            lead_id=binding.lead_id,sales_id=binding.sales_id,worker_id=w.id,
            rpa_session_key='hook:'+binding.id,read_run_id=payload['source_event_uuid'],
            contract_version=1,source_message_key=key,dedupe_key=key,sender_role=role,
            message_type=message['message_type'],content=message['content'],
            raw_payload={'transport':'hook','native_msg_id':message['native_msg_id'],
                'self_wxid':payload['self_wxid'],'peer_wxid':payload['peer_wxid'],
                'account_epoch':payload['account_epoch'],'native_source':message['source'],
                'native_direction':message['direction'],'sender_source':'unresolved' if role=='self' else 'customer',
                'media_stage':message['media_stage'],'source_time_raw':message['source_time_raw'],
                'ordering_fields':message['ordering_fields'],'database':message['database'],
                'database_generation':message['database_generation']},
            evidence={'native_receipt_id':receipt.id,'raw_evidence_digest':payload['raw_evidence_digest'],
                'source_revision':message['source_revision'],'source_entry_position':message['entry_position']},
            item_state='text_ready' if message['message_type']=='text' else 'metadata_observed',
            occurred_at=None,observed_at=observed,observation_order=None)
        # Provider time is retained verbatim. It never becomes a global ordering
        # key and receipt sequence/entry index is never disguised as screen order.
        db.add(event);db.flush();ids.append(event.id);inserted.append(event.id)
    if inserted:
        ctx['history_ready']=False
        ctx['history_reason_code']='HOOK_HISTORY_NEW_FACTS_PENDING'
        binding.native_context=ctx
    receipt.dispatch_status='facts_stored'
    db.flush()
    from app.services.observability_service import process_run_id_for_key, record_server_stage_best_effort
    raw_process = process_run_id_for_key('hook_raw', f"{w.id}:{payload['account_epoch']}:{payload['source_event_uuid']}")
    record_server_stage_best_effort(db, process_run_id=raw_process, worker_id=w.id,
        stage_name='hook.event_registered', component='backend',
        stable_key=payload['source_event_uuid'], trace_id=payload['source_event_uuid'])
    return {'raw_process_run_id':raw_process,'raw_source_event_uuid':payload['source_event_uuid'],'event_uuid':event_uuid,'dispatch_status':receipt.dispatch_status,'message_event_ids':ids,
        'new_message_event_ids':inserted,'ai_dispatch':'history_coverage_required','can_send':False}
