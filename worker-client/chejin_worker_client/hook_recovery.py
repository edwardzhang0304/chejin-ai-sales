"""One durable request/receipt path for lost backend acknowledgements.

This is terminal-only authority: it cannot reconstruct a send permit or call
WeChat. Original flows and sends remain untouched for audit/history recovery.
"""
from .shared_rules import hook_contract_rules as rules
from .hook_formal_c1 import _save


def reconcile(runtime, kind, intent, *, evidence):
    request={**runtime.account,'kind':kind,**intent,'evidence_digest':rules.digest(evidence)}
    root=runtime.private_root/'recovery'/kind/intent['task_id']
    path=root/'request.json'
    if path.exists():
        saved=rules.loads(path.read_bytes())
        if any(saved.get(k)!=v for k,v in request.items() if k!='process_generation'):
            raise ValueError('HOOK_RECOVERY_INTENT_CONFLICT')
        request={**saved,'process_generation':runtime.account['process_generation']}
    else:_save(path,request)
    result=runtime.client.call(runtime.credentials,'POST',
        f'/workers/{runtime.credentials.worker_id}/wechat/native-recovery',request)
    if (result.get('may_call_hook') is not False or result.get('flow_id')!=intent['flow_id']
            or result.get('status') not in ({'registered','not_started'} if kind=='flow_start' else {'not_triggered'})):
        raise ValueError('HOOK_RECOVERY_RECEIPT_INVALID')
    if result['status']!='registered':
        if result.get('task_id')!=intent['task_id'] or result.get('automatic_resend_allowed') is not False:
            raise ValueError('HOOK_RECOVERY_RECEIPT_INVALID')
        receipt=root/'receipt.json'
        if receipt.exists():
            if rules.loads(receipt.read_bytes())!=result:raise ValueError('HOOK_RECOVERY_RECEIPT_CONFLICT')
        else:_save(receipt,result)
    return result


def settle_untriggered(runtime, permit, root, reason):
    """Same immutable negative proof for live interruption and restart.

    A trigger or outcome forbids this path, even when a later business API
    refuses the customer. No physical operation is available here.
    """
    import hashlib
    if (root/'trigger.json').exists() or (root/'outcome.json').exists():
        raise ValueError('HOOK_SEND_PREVIOUS_ATTEMPT_REQUIRES_SETTLEMENT')
    intent=root/'intent.json'
    digest=hashlib.sha256(intent.read_bytes()).hexdigest() if intent.exists() else None
    if intent.exists():
        saved=rules.loads(intent.read_bytes())
        if any(saved.get(k)!=getattr(permit,k) for k in
               ('reply_action_id','task_id','self_wxid','account_epoch','process_generation','send_token','peer_wxid')):
            raise ValueError('HOOK_SEND_PERMIT_JOURNAL_CONFLICT')
    proof={'reply_action_id':permit.reply_action_id,'task_id':permit.task_id,'phase':'not_triggered',
        'reason_code':reason,'intent_sha256':digest,'observed_at':runtime.clock.now().isoformat()}
    path=root/'not-triggered.json'
    if path.exists():
        saved=rules.loads(path.read_bytes())
        if any(saved.get(k)!=proof[k] for k in ('reply_action_id','task_id','phase','intent_sha256')):
            raise ValueError('HOOK_SEND_NO_TRIGGER_PROOF_INVALID')
        proof=saved
    else:_save(path,proof)
    result=runtime.client.call(runtime.credentials,'POST',
        f'/workers/{runtime.credentials.worker_id}/wechat/native-replies/{permit.reply_action_id}/not-triggered',
        {**permit.account(),'task_id':permit.task_id,'send_token':permit.send_token,
            'proof':proof,'proof_digest':rules.digest(proof)},
        task_id=permit.task_id,token=permit.lease_fencing_token,flow_id=permit.flow_id)
    if result.get('physical_send_attempted') is not False or result.get('automatic_resend_allowed') is not False:
        raise ValueError('HOOK_SEND_NO_TRIGGER_RECEIPT_INVALID')
    receipt=runtime.private_root/'send-receipts'/f'{permit.reply_action_id}.json'
    if receipt.exists():
        if rules.loads(receipt.read_bytes())!=result:raise ValueError('HOOK_SEND_SETTLEMENT_CONFLICT')
    else:_save(receipt,result)
    return result
