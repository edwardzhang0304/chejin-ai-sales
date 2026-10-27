"""Validated SQLite history -> retained-send dispatcher -> shared wait boundary.

Backend receipt serialization is represented from validated coverage here;
real backend deadline/settlement coverage lives in customer_refusal_closure.
"""
from dataclasses import replace
from types import SimpleNamespace as NS
import json
import threading
import pytest
from test_hook_gateway import lab
from test_hook_history import history_lab
from test_hook_retained_send import retained
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.shared_rules import hook_history_rules


def service_once(call, runner=None):
    waits=[]; faults=[]; results=[]
    runner=runner or NS(binding=NS(run_status='running'),stop_event=threading.Event())
    def tick(*a):
        try:results.append(call())
        finally:runner.stop_event.set()
    svc=HookWorkerServices.__new__(HookWorkerServices)
    svc.dispatcher=NS(tick=tick,interval_seconds=.001,_report_wait=lambda code,**kw:waits.append(code))
    svc._fault=lambda runner,code,**kw:faults.append(code)
    svc.business_loop(runner,lambda _:None)
    return faults,waits,results


@pytest.mark.parametrize('kind',['empty_text','unsupported_media','order_pending'])
def test_incomplete_history_recovery_keeps_customer_wait(history_lab,retained,kind):
    server,gateway,process,scope,delivery,_,db=history_lab
    root,credentials,original_scope,binding,metadata,folder=retained
    private=root/'runtime';(private/'sends').mkdir(parents=True)
    (root/credentials.worker_id).rename(private/'sends'/credentials.worker_id)
    folder=private/'sends'/credentials.worker_id/original_scope.account_epoch/metadata['reply_action_id']
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    scope=replace(scope,account_epoch=original_scope.account_epoch)
    delivery=replace(delivery,account_epoch=scope.account_epoch)
    metadata={**metadata,'original_process_generation':original_scope.process_generation}
    db.execute('DELETE FROM MSG')
    db.execute('INSERT INTO MSG VALUES(9,10009,?,0,1791543697,?,1,9,?,?)',
        (43 if kind=='unsupported_media' else 1,0 if kind=='order_pending' else 1791543697000,
         delivery.peer_wxid,'' if kind=='empty_text' else 'synthetic'))
    checked=[]
    def sync(*a,**kw):
        _,coverage=HookHistorySynchronizer(gateway,lambda c:None,lambda:(0,0),media_enabled=True).capture(process,scope,delivery,'1791543600')
        proof=hook_history_rules.validate_coverage(coverage,delivery.peer_wxid,1791543600)
        checked.append(proof)
        return {'history_ready':proof['ready'],'reason_code':proof['reason_code']}
    d=HookBusinessDispatcher(NS(),private,lambda *a:None,lambda *a:None,automatic_transcription_disabled=lambda:True)
    d.runtime=NS(credentials=credentials,scope=scope,sync=sync,progress=lambda *a:None,active_task_id=metadata['task_id'])
    faults,waits,results=service_once(lambda:d._recover_send(binding,metadata))
    assert before=={p.name:p.read_bytes() for p in folder.iterdir()}
    assert not list((private/'send-receipts').glob('*'))
    assert not any(p=='/SendTextMsg' for p,*_ in server.requests)
    print(json.dumps({'case':kind,'reason':checked[0]['reason_code'],'faults':faults,'waits':waits}))
    assert not faults, f'Incomplete customer history became global fault: {faults}'
