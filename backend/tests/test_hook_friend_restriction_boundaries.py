"""Synthetic deletion boundaries through production Worker/HTTP/SQLite; no real sends."""
import json,sqlite3,time
from pathlib import Path
import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HandoffEvent,ReplyAction,MessageBatch
from app.models.task import Task
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload
from test_hook_native_media import media_coverage
from test_hook_friend_verification import append,NOTICE,REASON,notifications
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from test_hook_formal_runtime import drive_reply
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime

def test_pending_voice_keeps_own_state_but_cannot_defer_verified_friend_notice(monkeypatch):
 w,t,p,b=bound();sent=notifications(w,monkeypatch);value=media_coverage(b);append(value)
 outcomes=[]
 for _ in range(3):
  response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
  outcomes.append(response.json()['data']['reason_code'])
 with SessionLocal() as db:
  handoffs=list(db.scalars(select(HandoffEvent)))
  conv=db.get(Conversation,b['conversation_id'])
  assert outcomes==['HOOK_VOICE_PENDING']*3 and len(handoffs)==len(sent)==1
  assert handoffs[0].handoff_reason_code==REASON and conv.status=='waiting_sales_reply'
  assert db.get(Worker,w['id']).run_status=='running'


@pytest.mark.parametrize('arrival',['delay','begin'])
@pytest.mark.parametrize('native_notice_id',['positive','zero'])
@pytest.mark.parametrize('decision',['send_reply','reply_then_handoff'])
def test_notice_arrives_during_send_delay(identity_client,lab,connection_factory,monkeypatch,tmp_path,native_notice_id,decision,arrival):
 class Model:
  def generate_reply_decision(self,**kwargs):
   return AIEngineDecision(decision=decision,reply_text='好的，我们会结合您的实际需求核实车辆资料。',guard_result='pass',handoff_reason_code='CUSTOMER_HIGH_INTENT' if decision=='reply_then_handoff' else None)
 monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
 w,_,_,binding=bound();sent=notifications(w,monkeypatch);server,gateway,*_=lab
 manager,credentials,prepared=connection_factory(w)
 table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
 table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
 server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'deletion-audit'}]
 server.query_response=lambda body:{'data':[dict(r) for r in table.execute(body['sql']).fetchall()]}
 while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
 def add(kind,sender,text,zero=False):
  n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+9;now=int(time.time())
  table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(n,'0' if zero else str(8200000000000000000+n),kind,sender,now,now*1000+n,0,n,binding['peer_wxid'],text))
 add(1,0,'想了解二手车的选购方式，暂时不预约。')
 introduced=[]
 def wait(seconds):
  if seconds>=.5 and not introduced and arrival=='delay':
   introduced.append(True);add(10000,0,NOTICE,zero=native_notice_id=='zero')
  elif seconds<.5:time.sleep(seconds)
 def hook(path):
  server.raw_override=None
  if path=='/SendTextMsg':
   add(1,1,server.requests[-1][1]['msg']);server.raw_override=b'{"SendTextMsg":"1"}'
 server.on_request=hook
 runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,gateway,prepared,tmp_path/'runtime',wait=wait)
 if arrival in {'begin','identity_rejection','network_rejection'}:
  from copy import deepcopy
  import uuid
  histories=[];history_upload=HookFormalClient.history_coverage
  def capture(client,credentials,account,binding,capture_id,coverage,**kwargs):
   histories.append(deepcopy(coverage))
   return history_upload(client,credentials,account,binding,capture_id,coverage,**kwargs)
  monkeypatch.setattr(HookFormalClient,'history_coverage',capture)
  original=HookFormalClient.native_send_begin
  def begin(client,credentials,permit,payload):
   if arrival in {'identity_rejection','network_rejection'}:
    from chejin_worker_client.api import ApiError
    code='HOOK_SEND_IDENTITY_MISMATCH' if arrival=='identity_rejection' else 'HOOK_BACKEND_NETWORK_FAILED'
    raise ApiError(code,'synthetic denial',409 if arrival=='identity_rejection' else 0)
   assert not introduced
   introduced.append(True);add(10000,0,NOTICE,zero=native_notice_id=='zero')
   # Actual history API arrives between final refresh and begin; no mocked
   # eligibility error or forged ACK. The server must reject the original send.
   value=deepcopy(histories[-1]);append(value)
   if native_notice_id=='zero':
    for scan in value['scans']:scan['databases'][0]['pages'][0][-1]['MsgSvrID']='0'
   history_upload(client,credentials,runtime.account,binding,str(uuid.uuid4()),value)
   return original(client,credentials,permit,payload)
  monkeypatch.setattr(HookFormalClient,'native_send_begin',begin)
 first=runtime.sync(binding);error=None;result=None
 try:result=drive_reply(runtime,binding,first['message_batch']['batch_id'])
 except Exception as exc:error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 with SessionLocal() as db:
  action=db.scalar(select(ReplyAction));task=db.scalar(select(Task).where(Task.reply_action_id==action.id)) if action else None
  ev=db.scalar(select(HandoffEvent));worker=db.get(Worker,w['id'])
  if arrival in {'identity_rejection','network_rejection'}:
   expected='HOOK_SEND_IDENTITY_MISMATCH' if arrival=='identity_rejection' else 'HOOK_BACKEND_NETWORK_FAILED'
   assert error=={'type':'ApiError','code':expected}
   assert action.status=='sending' and task.status=='running'
   assert worker.inflight_flow_state and runtime.active_flow_id
   assert not list((runtime.private_root/'sends').rglob('not-triggered.json'))
   assert not list((runtime.private_root/'send-receipts').glob('*.json'))
   assert not any(path=='/SendTextMsg' for path,*_ in server.requests)
   manager.close();table.close();return
  assert error is None,error
  assert not any(path=='/SendTextMsg' for path,*_ in server.requests)
  assert action.status in {'superseded','cancelled'} and task.status=='cancelled'
  assert worker.run_status=='running' and not worker.inflight_flow_state and not runtime.active_flow_id
  assert ev and ev.closed_at is None and 'friend_verification_required' in ev.risk_flags
  assert len(sent)==1
  original_action=action.id
 from chejin_worker_client import storage
 assert storage.load_runtime_control().get('inflight_flow_id') is None
 folder=next((runtime.private_root/'sends').rglob('permit.json')).parent
 assert not (folder/'trigger.json').exists()
 assert (folder/'not-triggered.json').exists()
 receipt=json.loads((runtime.private_root/'send-receipts'/f'{original_action}.json').read_bytes())
 assert receipt['physical_send_attempted'] is False and receipt['automatic_resend_allowed'] is False
 before={f.name:f.read_bytes() for f in folder.iterdir()}
 for _ in range(2):runtime.sync(binding)
 assert before=={f.name:f.read_bytes() for f in folder.iterdir()}
 assert introduced
 manager.close();table.close()


@pytest.mark.parametrize('arrival',['identity_rejection','network_rejection'])
def test_other_denials_are_not_swallowed_as_friend_restriction(identity_client,lab,connection_factory,monkeypatch,tmp_path,arrival):
 test_notice_arrives_during_send_delay(identity_client,lab,connection_factory,monkeypatch,tmp_path,'zero','send_reply',arrival)


@pytest.mark.parametrize('decision',['send_reply','reply_then_handoff'])
@pytest.mark.parametrize('notice_id',['zero','positive'])
def test_pending_voice_and_restriction_at_begin_close_original_unsent_flow(identity_client,lab,connection_factory,monkeypatch,tmp_path,decision,notice_id):
 # The last refresh was ready; both facts arrive at the begin boundary.
 # Exercise the shared case assertions, including durable no-trigger receipt.
 import sys
 original=append
 def voice_then_notice(value,*args,**kwargs):
  original(value,kind='34',sender='0',text='<msg><voicemsg voicelength="1000"/></msg>')
  return original(value,*args,**kwargs)
 monkeypatch.setattr(sys.modules[__name__],'append',voice_then_notice)
 test_notice_arrives_during_send_delay(identity_client,lab,connection_factory,monkeypatch,tmp_path,notice_id,decision,'begin')


@pytest.mark.parametrize('damage',['binding_account','binding_epoch','history_generation','voice_without_restriction'])
def test_begin_preserves_history_identity_and_content_guards(monkeypatch,damage):
 from datetime import datetime,timezone
 from test_hook_native_send_http import permitted
 w,p,b,h,permit,value=permitted(monkeypatch)
 value['schema_version']='hook-history-coverage-v2'
 append(value,kind='34',sender='0',text='<msg><voicemsg voicelength="1000"/></msg>')
 if damage!='voice_without_restriction':append(value)
 response,_=upload(w,p,b,value,monkeypatch)
 assert response.status_code==200,response.text
 assert not response.json()['data']['history_ready']
 with SessionLocal() as db:
  binding=db.get(WechatSessionBinding,b['binding_id'])
  if damage=='binding_account':binding.native_self_wxid='wxid_different_account'
  elif damage=='binding_epoch':binding.native_context={**binding.native_context,'account_epoch':'different-epoch'}
  elif damage=='history_generation':binding.native_context={**binding.native_context,'coverage_process_generation':'different-generation'}
  db.commit()
 request={**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],
          'send_token':permit['send_token'],'journal_sha256':'d'*64,'attempted_at':datetime.now(timezone.utc).isoformat()}
 response=post(w,f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/begin",request,h)
 assert response.status_code==409 and response.json()['code']=='HOOK_HISTORY_COVERAGE_REQUIRED',response.text
 with SessionLocal() as db:
  action=db.get(ReplyAction,permit['reply_action_id'])
  assert not action.ai_payload.get('native_send_attempt')
  assert not action.ai_payload.get('native_no_trigger_settlement')
