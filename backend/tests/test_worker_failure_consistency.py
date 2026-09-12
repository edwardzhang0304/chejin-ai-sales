"""Regression tests promoted from the independent consistency audit.

C1 uses real loopback HTTP/PostgreSQL and a Worker subprocess/SQLite. Only
desktop observations are controlled. Gateway cases use a real HTTP listener
and production response encoding, client decoding and durable outbox handling.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from app.api.response import error_response
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.worker import Worker
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils

ROOT = Path(__file__).resolve().parents[2]

COMMON = r'''
import json, sys
from pathlib import Path
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import save_binding, load_binding, load_runtime_control
request=json.loads(Path(sys.argv[1]).read_text())
api=WorkerApiClient(request['base_url'])
binding=Binding(request['worker_id'],request['token'],'followup-test',run_status='running')
save_binding(binding)
events=[]
injected=[]
original_send=api.session.send
def send(prepared, **kwargs):
    interruption=request.get('interruption', '')
    should_drop=bool(interruption and not injected and prepared.url.endswith('/'+interruption.split(':')[0]))
    if should_drop:
        injected.append({'at':interruption,'memory':binding.run_status,'sqlite':load_binding().run_status})
        if interruption.endswith(':before'):
            raise ConnectionError('controlled request loss before backend')
    response=original_send(prepared, **kwargs)
    if should_drop:
        raise ConnectionError('controlled response loss after backend commit')
    events.append({'method':prepared.method,'url':prepared.url,'status':response.status_code})
    return response
api.session.send=send
from chejin_worker_client.rpa_bridge import RpaBridge
class Desktop(RpaBridge):
    calls=0
    def probe(self):return 'ready','logged_in'
    def _call_omniauto(self,args,timeout=30,cancel_check=None):
        self.calls+=1
        assert args[0]=='add-friend-entry-click-plan-windows',args
        return {'ok':False,'task_status':'failed','error_code':request['code'],
                'failure_step':'phone_search_finished' if request['code']=='PHONE_NOT_FOUND' else 'window_layout_calibration',
                'message':'controlled desktop boundary; no physical click'}
bridge=Desktop()
bridge.mode='real'
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,
    on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=lambda x:None)
runner.binding=binding
'''


def run_worker(tmp_path, program, request):
    script=tmp_path/'worker.py'; script.write_text(COMMON+program,encoding='utf-8')
    args=tmp_path/'input.json'; args.write_text(json.dumps(request),encoding='utf-8')
    env={**os.environ,'PYTHONPATH':str(ROOT/'worker-client'),
         'CHEJIN_WORKER_HOME':str(tmp_path/'worker-state'),'CHEJIN_C2_ENABLED':'false',
         'CHEJIN_OBSERVABILITY_ENABLED':'false','PYTHONDONTWRITEBYTECODE':'1'}
    process=subprocess.run([sys.executable,str(script),str(args)],env=env,cwd=ROOT,
                           text=True,capture_output=True,timeout=35)
    (tmp_path/'worker.stdout').write_text(process.stdout,encoding='utf-8')
    (tmp_path/'worker.stderr').write_text(process.stderr,encoding='utf-8')
    assert process.returncode==0,process.stderr
    return json.loads(process.stdout.splitlines()[-1])


@pytest.mark.parametrize('code,expected_status,expected_attempts,interruption',[
    ('WECHAT_UI_LAYOUT_UNRESOLVED','faulted',1,''),
    ('PHONE_NOT_FOUND','running',2,''),
    *[('WECHAT_UI_LAYOUT_UNRESOLVED','faulted',1,phase) for phase in ('fail:before','fail:after','run-status:before','run-status:after')],
])
def test_c1_technical_fault_must_not_consume_next_customer(http_api,tmp_path,code,expected_status,expected_attempts,interruption):
    worker,rows=fixture_rows()
    with SessionLocal() as db:
        for index,row in enumerate(rows):
            db.add(_contact_model(row['lead_id'],ContactType.phone,
                                  contact_utils.normalize_phone(f'1380000888{index}'),True))
            db.add(Task(lead_id=row['lead_id'],worker_id=worker['id'],task_type='add_friend',status='pending'))
        db.commit()
    # Get base URL from an actual response, without peeking into the fixture closure.
    base=http_api.get('/healthz').url.rsplit('/healthz',1)[0]+'/api'
    result=run_worker(tmp_path,r'''
runner.tick_once()
first={'status':binding.run_status,'runtime':load_runtime_control(),'calls':bridge.calls}
runner.tick_once()
if request['interruption'].startswith('fail:'):
    import time
    deadline = time.monotonic() + 8
    while load_runtime_control().get('inflight_flow_id') and time.monotonic() < deadline:
        runner.tick_once()
        time.sleep(.1)
print(json.dumps({'first':first,'status':binding.run_status,'saved_status':load_binding().run_status,
                  'runtime':load_runtime_control(),'calls':bridge.calls,'http':events,'injected':injected},default=str))
''',{'base_url':base,'worker_id':worker['id'],'token':worker['worker_token'],'code':code,'interruption':interruption})
    with SessionLocal() as db:
        state=db.get(Worker,worker['id'])
        result['backend']={'run_status':state.run_status,'flow':state.inflight_flow_state,
                           'tasks':[{'id':t.id,'status':t.status,'error_code':t.error_code}
                                    for t in db.scalars(select(Task).where(Task.worker_id==worker['id']))]}
    (tmp_path/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    assert any(e['url'].endswith('/claim') and e['status']==200 for e in result['http']),result
    assert result['status']==result['saved_status']==result['backend']['run_status']==expected_status,result
    assert result['calls']==expected_attempts,result
    if interruption:
        assert result['injected'] == [{'at':interruption,'memory':'faulted','sqlite':'faulted'}],result
    if interruption.startswith('fail:'):
        # First stop retains the original Flow; later real ticks must settle it.
        assert result['first']['runtime']['inflight_flow_id'], result
    assert not result['backend']['flow'].get('flow_id'), result
    assert not result['runtime'].get('inflight_flow_id'), result
    if expected_status == 'faulted':
        assert sum(t['status']=='failed' for t in result['backend']['tasks']) == 1,result
        assert sum(t['status']=='pending' for t in result['backend']['tasks']) == 1,result
        assert sum(e['url'].endswith('/claim') for e in result['http']) == 1,result
        if not interruption:
            paths=[e['url'].rsplit('/',1)[-1] for e in result['http']]
            assert paths.index('run-status') < paths.index('fail'),result


@pytest.mark.parametrize('status,encoding,code,explicit,expected',[
    (502,'html','SYNTHETIC_UPSTREAM_ERROR',None,'retry'),
    (503,'html','SYNTHETIC_UPSTREAM_ERROR',None,'retry'),
    (503,'json','SYNTHETIC_UPSTREAM_ERROR',None,'retry'),
    (409,'json','SYNTHETIC_UPSTREAM_ERROR',None,'capability_paused'),
    (200,'html','SYNTHETIC_UPSTREAM_ERROR',None,'capability_paused'),
    (503,'json_without_action','MESSAGE_IDENTITY_COLLISION',None,'identity_quarantined'),
    (503,'json_without_action','MESSAGE_CONTRACT_REVISION_MISMATCH',None,'capability_paused'),
    (503,'json','SYNTHETIC_UPSTREAM_ERROR','capability_paused','capability_paused'),
])
def test_proxy_and_backend_transient_failures_share_retry_policy(tmp_path,status,encoding,code,explicit,expected):
    # HTML 502/503 is the ordinary reverse-proxy response when API upstream is unavailable.
    response=error_response(status,code,'controlled upstream response')
    envelope=json.loads(response.body)
    if encoding=='json_without_action':
        envelope.get('data',{}).pop('recovery_action',None)
    if explicit:
        envelope.setdefault('data',{})['recovery_action']=explicit
    body=json.dumps(envelope).encode() if encoding.startswith('json') else b'<html><body>upstream temporarily unavailable</body></html>'
    calls=[]
    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            received=self.rfile.read(int(self.headers.get('Content-Length','0')))
            calls.append({'path':self.path,'body_bytes':len(received),'status':status})
            self.send_response(status)
            self.send_header('Content-Type','application/json' if encoding.startswith('json') else 'text/html')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Gateway)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=run_worker(tmp_path,r'''
from chejin_worker_client.c2_contract import contract_revision,contract_sha256
from chejin_worker_client.storage import enqueue_c2_outbox,load_c2_outbox_entry
payload={'contract_version':3,'contract_revision':contract_revision(),'contract_sha256':contract_sha256(),
         'conversation_id':'synthetic-conv','read_run_id':'synthetic-read',
         'authorization_revision':'synthetic-auth-rev','messages':[],
         'evidence':{'observations':[],'slot_ledger_states':[]}}
outbox=enqueue_c2_outbox(payload)
result=runner._attempt_c2_outbox_delivery(binding=binding,payload=payload,outbox_id=outbox,operation='audit')
entry=load_c2_outbox_entry(outbox)
print(json.dumps({'recovery_action':result.get('recovery_action'),'outbox_state':entry['status'],
 'status':binding.run_status,'saved_status':load_binding().run_status,'http':events,
 'error_code':result.get('error_code')},default=str))
''',{'base_url':f'http://127.0.0.1:{server.server_port}/api','worker_id':'synthetic-worker','token':'test-only'})
    finally:
        server.shutdown();server.server_close();thread.join(5)
    result['gateway']=calls
    (tmp_path/'evidence.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    assert len(calls)==1,result
    assert result['recovery_action']==expected,result
    assert result['outbox_state']==('retry_waiting' if expected=='retry' else expected),result
    assert result['status']==('running' if expected in {'retry','identity_quarantined'} else 'paused'),result
