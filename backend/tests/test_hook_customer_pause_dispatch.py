"""Real isolated backend HTTP rejection reaches actual dispatcher/service boundary."""
from types import SimpleNamespace as NS
import threading
import uuid
import pytest
from test_hook_business_http import clean
from test_hook_history_business_http import bound, coverage
from test_hook_account_client import identity_client
from app.core.database import SessionLocal
from app.models.wechat import WechatSessionBinding
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_backend import HookWorkerCredentials


@pytest.mark.parametrize('entry',['history','friend_discovery'])
def test_customer_pause_during_inventory_does_not_fault_worker(identity_client,tmp_path,entry):
    w,t,account,binding=bound()
    creds=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    formal=HookFormalClient(identity_client.base_url,timeout_seconds=5,max_response_bytes=65536)
    # Simulate an operator pausing one customer's authorization after inventory.
    with SessionLocal() as db:
        db.get(WechatSessionBinding,binding['binding_id']).allow_listening=False
        db.commit()
    rt=HookFormalRuntime.__new__(HookFormalRuntime)
    rt.account=account;rt.scope=NS(**account);rt.credentials=creds;rt.private_root=tmp_path/'private'
    rt.process=NS(generation=account['process_generation']);rt.clock=NS(observe=lambda _:None)
    rt.active_flow_id=t['id'];rt.progress=lambda *a:None
    friend={'code':0,'data':[{'wxid':binding['peer_wxid'],'markname':binding['remark_code']}]}
    rt.gateway=NS(friend_list=lambda *a:NS(payload=friend));rt.client=formal
    rt.barrier=lambda *a:(0,0)
    rt.sync=lambda current:formal.history_coverage(creds,account,current,str(uuid.uuid4()),coverage(binding))
    b={**binding,'voice_jobs':[],'batch_ids':[]}
    inventory={**account,'schema_version':'hook-native-work-v1','observation_query_version':1,
        'run_status':'running','bindings':[b],'retained_sends':[],
        'invitations':[{'source_task_id':t['id']}] if entry=='friend_discovery' else [],'inflight_task':None,'inflight_flow':{}}
    class InventoryClient:
        def call(self,*args,**kwargs):
            assert args[2].endswith('/native-work')
            return inventory
    d=HookBusinessDispatcher(InventoryClient(),rt.private_root,lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True)
    d.runtime=rt;d._adopt_flow=lambda *a:None
    runner=NS(binding=NS(run_status='running'),stop_event=threading.Event(),api=NS())
    services=NS(connection=NS(prepared=NS(process=rt.process,registration=NS(scope=rt.scope),account={})),
        startup_window_ready_for=lambda _:True)
    faults=[];waits=[]
    def tick(*a):
        try:d.tick(runner,services)
        finally:runner.stop_event.set()
    svc=HookWorkerServices.__new__(HookWorkerServices)
    svc.dispatcher=NS(tick=tick,interval_seconds=.001,_report_wait=lambda code,**kw:waits.append(code))
    svc._fault=lambda runner,code,**kw:faults.append(code)
    svc.business_loop(runner,lambda _:None)
    print({'entry':entry,'faults':faults,'waits':waits})
    assert faults==[], f'Paused customer escalated globally: {faults}'
