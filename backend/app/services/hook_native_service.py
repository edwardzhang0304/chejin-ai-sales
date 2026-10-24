"""Authorized transport intake; receipt does not claim domain dispatch is done."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.hook_native import HookNativeReceipt
from app.models.sales import Sales
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.services.followup_eligibility import lock_leads


def store_observations(db: Session, authenticated: Worker, payload: dict, instance_id: str):
    rules = shared_adapter('hook_contract_rules')
    rules.validate_envelope(payload)
    if payload['worker_id'] != authenticated.id or payload['client_instance_id'] != instance_id:
        raise AppError('HOOK_REQUEST_IDENTITY_MISMATCH', '消息上报身份不一致', 409)
    hint = db.get(WechatSessionBinding, payload['binding_id'])
    if hint is None or hint.worker_id != authenticated.id:
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED', '客户尚未完成绑定', 409)
    lock_leads(db, [hint.lead_id])
    binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id == hint.id)
        .with_for_update().execution_options(populate_existing=True))
    worker = db.scalar(select(Worker).where(Worker.id == authenticated.id, Worker.deleted_at.is_(None))
        .with_for_update().execution_options(populate_existing=True))
    if (worker is None or not worker.enabled or worker.client_binding_state != 'bound' or worker.client_instance_id != instance_id
            or worker.hook_self_wxid != payload['self_wxid'] or worker.hook_account_epoch != payload['account_epoch']
            or (worker.hook_runtime_context or {}).get('blocked')):
        raise AppError('HOOK_ACCOUNT_NOT_AUTHORIZED', '工作微信账号未通过检查', 409)
    old = db.scalar(select(HookNativeReceipt).where(HookNativeReceipt.worker_id == worker.id,
        HookNativeReceipt.event_uuid == payload['event_uuid']))
    if old:
        if old.payload_digest != rules.digest(payload):
            raise AppError('HOOK_DELIVERY_ID_CONFLICT', '同一上报记录的内容不一致', 409)
        return old.receipt_json  # No new work, even if contact authorization was subsequently revoked.
    sales = db.scalar(select(Sales).where(Sales.worker_id == worker.id, Sales.deleted_at.is_(None)))
    context = dict(binding.native_context or {})
    from app.services.hook_relationship_service import retired_evidence_scope
    retired=retired_evidence_scope(binding,worker.id,payload)
    if (binding.deleted_at or (not retired and (binding.bind_status != 'bound' or not binding.allow_listening
            or binding.listen_status not in {'listening', 'degraded'})) or not binding.remark_code or not binding.lead_id
            or binding.worker_id != worker.id or binding.native_self_wxid != payload['self_wxid']
            or binding.native_peer_wxid != payload['peer_wxid'] or context.get('account_epoch') != payload['account_epoch']
            or (not retired and int(binding.authorization_revision or 1) != payload['binding_revision'])
            or sales is None or not sales.enabled or sales.id != worker.hook_sales_id or binding.sales_id != sales.id):
        raise AppError('HOOK_BINDING_NOT_AUTHORIZED', '客户绑定或授权已失效', 409)
    from app.services.hook_binding_transfer import require_binding_origin, grant, TRANSFER
    authorization=grant(db,context.get('source_task_id'))
    if authorization and (authorization.extra_metadata or {}).get('mode')==TRANSFER:
        require_binding_origin(db,binding,worker)
    else:
        origin = db.get(Task, context.get('source_task_id')) if context.get('source_task_id') else None
        evidence = db.get(TaskEvidence, context.get('invite_evidence_id')) if context.get('invite_evidence_id') else None
        proof = dict(evidence.extra_metadata or {}) if evidence else {}
        if (origin is None or origin.deleted_at or origin.task_type != 'add_friend' or origin.status != 'completed'
                or origin.result_code != 'invite_sent' or origin.worker_id != worker.id or origin.sales_id != sales.id
                or origin.lead_id != binding.lead_id or evidence is None or evidence.task_id != origin.id
                or evidence.worker_id != worker.id or evidence.evidence_type != 'hook_invite_submission'
                or proof.get('schema_version') != 'hook-invite-proof-v1' or proof.get('shortcode') != binding.remark_code
                or proof.get('self_wxid') != payload['self_wxid'] or proof.get('account_epoch') != payload['account_epoch']
                or (retired and proof.get('backend_validated') is not True)
                or proof.get('shortcode_written') is not True or proof.get('invitation_submitted') is not True):
            raise AppError('HOOK_INVITE_ORIGIN_REQUIRED', '缺少系统加好友申请的绑定依据', 409)
    # Intake immediately invalidates AI/send admission, before asynchronous dispatch.
    if not retired:
        context.update(history_ready=False,history_reason_code='HOOK_HISTORY_NEW_FACTS_PENDING')
        binding.native_context=context
    response = rules.receipt(payload)
    db.add(HookNativeReceipt(worker_id=worker.id, client_instance_id=instance_id, self_wxid=payload['self_wxid'],
        account_epoch=payload['account_epoch'], binding_id=binding.id, binding_revision=str(payload['binding_revision']),
        event_uuid=payload['event_uuid'], payload_digest=rules.digest(payload), payload_json=payload,
        receipt_json=response, dispatch_status='authorization_retired' if retired else 'pending'))
    db.flush()
    return response
