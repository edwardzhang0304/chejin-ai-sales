"""Real HTTP/PostgreSQL + production Worker read/identity/SQLite/settlement.

Only the desktop boundary and model provider are controlled. Historical facts
are created by a preceding Worker read, never by hand-written projections.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from test_lead_followup_eligibility import http_api, isolated_db, fixture_rows
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.models.c3 import Conversation, MessageBatch, ReplyAction, HandoffEvent
from app.models.task import Task

ROOT = Path(__file__).resolve().parents[2]
WORKER = r'''
import json,sys,os
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.task_runner import TaskRunner
if os.environ.get('CHEJIN_TEST_DISABLE_HISTORY_SUFFIX')=='1':
 import chejin_worker_client.task_runner as runner_module
 original_compare=runner_module.compare_business_viewport_continuity
 def feature_disabled(*args,**kwargs):
  kwargs['allow_history_suffix']=False
  return original_compare(*args,**kwargs)
 runner_module.compare_business_viewport_continuity=feature_disabled
from chejin_worker_client.storage import save_binding,load_runtime_control,has_pending_c2_outbox
from chejin_worker_client.ui_lock import lock_summary
base,worker,conv,path=sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],Path(sys.argv[4])
api=WorkerApiClient(base+'/api')
binding=Binding(worker['id'],worker['worker_token'],'followup-test',run_status='running')
save_binding(binding)
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
bridge.get_messages_payloads=[json.loads(path.read_text())]
errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
runner.binding=binding
exchanges=[]
send=api.session.send
def observe(request,**kwargs):
 response=send(request,**kwargs)
 if any(x in request.url for x in ('/messages/ingest','/inflight-flow/','/read-authorization','/read-targets')):
  exchanges.append({'url':request.url,'status':response.status_code,'response':response.json(),'request':json.loads(request.body) if request.body else None})
 return response
api.session.send=observe
target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==conv)
result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False)
print(json.dumps({'result':result,'runtime':load_runtime_control(),'pending':has_pending_c2_outbox(),'locked':lock_summary().get('locked'),'status':runner.binding.run_status,'errors':errors,'exchanges':exchanges,'physical_sends':len(bridge.sent_replies),'locates':len(bridge.locate_chats),'reads':len(bridge.message_reads)},ensure_ascii=False,default=str))
'''


@pytest.mark.parametrize('mode', ['unchanged', 'new_tail', 'mismatch'])
def test_history_suffix_read_settles_without_false_fault(http_api, tmp_path, mode):
    worker,rows=fixture_rows(); row=rows[0]; conv=row['conversation_id']
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    messages=[{'id':f'old-{i}','type':'text','sender_role':'self' if i%2==0 else 'customer',
               'content':f'历史对话第{i+1}条，已确认的不同内容。'} for i in range(11)]
    seed={'messages':messages,'frame_id':'seed-full-history'}
    current={'messages':[dict(m,id='current-'+m['id']) for m in messages[-5:]],'frame_id':'current-history-suffix',
             'top_message_fragment':[{'bounds':[100,80,110,87],'reason':'object_clipped_by_viewport_top'}]}
    if mode=='new_tail':current['messages'].append({'id':'new-question','type':'text','sender_role':'customer','content':'请问有新能源车吗？'})
    if mode=='mismatch':current['messages'][2]['content']='和历史完全不同的内容'
    script=tmp_path/'worker.py';script.write_text(WORKER)
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker-data'),'CHEJIN_RPA_MODE':'mock',
         'CHEJIN_UI_LOCK_LEASE_SECONDS':'1','PYTHONPATH':os.pathsep.join([str(ROOT/'worker-client'),str(ROOT/'worker-client/tests'),str(ROOT/'worker-client/omniauto-rpa')])}
    def run(phase,frame):
        path=tmp_path/(phase+'.json');path.write_text(json.dumps(frame,ensure_ascii=False))
        p=subprocess.run([sys.executable,str(script),base,json.dumps(worker),conv,str(path)],env=env,capture_output=True,text=True,timeout=45)
        (tmp_path/(phase+'.stdout')).write_text(p.stdout);(tmp_path/(phase+'.stderr')).write_text(p.stderr)
        assert p.returncode==0,p.stderr
        result=json.loads(p.stdout.strip().splitlines()[-1])
        (tmp_path/(phase+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        return result
    before=run('seed',seed)
    assert before['result']['ok'],before
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==conv))==11
        # Test preparation: let an already-read customer return to AI listening.
        # No Flow, outbox, message identity, or settlement is modified here.
        for h in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==conv)):h.deleted_at=utcnow()
        db.get(Conversation,conv).status='waiting_user_reply'
        b=db.get(WechatSessionBinding,row['binding_id']);b.next_read_due_at=utcnow()-timedelta(seconds=1);b.last_read_conversation_status='waiting_user_reply'
        db.commit()
    after=run('current',current)
    with SessionLocal() as db:
        events=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==conv)))
        assert len(events)==(12 if mode=='new_tail' else 11)
        if mode=='new_tail':assert sum(e.content=='请问有新能源车吗？' for e in events)==1
        if mode=='unchanged':
            assert db.scalar(select(func.count()).select_from(MessageBatch).where(MessageBatch.conversation_id==conv))==0
            assert db.scalar(select(func.count()).select_from(ReplyAction).where(ReplyAction.conversation_id==conv))==0
            assert db.scalar(select(func.count()).select_from(Task).where(Task.worker_id==worker['id']))==0
        if mode=='new_tail':
            assert db.scalar(select(func.count()).select_from(MessageBatch).where(MessageBatch.conversation_id==conv))==1
        assert not db.get(Worker,worker['id']).inflight_flow_state
    assert after['status']==('faulted' if mode=='mismatch' else 'running'),after
    assert not after['runtime']['inflight_flow_id'] and not after['pending'] and not after['locked'],after
    assert after['physical_sends']==0
    finishes=[e for e in after['exchanges'] if e['url'].endswith('/inflight-flow/finish')]
    assert len(finishes)==1 and finishes[0]['status']==200,after
    if mode=='unchanged':
        assert after['result']['ok'],after
        assert 'read_confirmed' in json.dumps(finishes[0])
        assert after['result']['result']['read_completion']['result']=='no_change'
        with SessionLocal() as db:
            db.get(WechatSessionBinding,row['binding_id']).next_read_due_at=utcnow()-timedelta(seconds=1)
            db.commit()
        again=run('repeat',current)
        assert again['result']['ok'] and again['status']=='running',again
        assert again['result']['result']['ingested_count']==0
        assert not again['runtime']['inflight_flow_id'] and not again['pending'] and not again['locked']
    if mode=='mismatch':assert after['result']['error_code']=='MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'
