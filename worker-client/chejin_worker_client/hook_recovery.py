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
