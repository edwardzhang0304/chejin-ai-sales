"""Original dispatcher consumes stale callbacks/invitations after operator retirement."""
from pathlib import Path
from types import SimpleNamespace
import json,threading,sqlite3,time
import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_hook_business_http import clean
from test_hook_history_business_http import bound
from test_hook_relationship_http import reinvite,finish_invite
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from app.core.database import SessionLocal
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from chejin_worker_client import storage
from chejin_worker_client.hook_backend import HookIdentityClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_inbox import NativeInbox

def make(w,lab,connection_factory,identity_client,tmp_path):
 manager,credentials,prepared=connection_factory(w)
 rt=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,lab[1],prepared,tmp_path/'runtime')
 runner=SimpleNamespace(binding=SimpleNamespace(run_status='running',worker_id=w['id'],client_instance_id=credentials.client_instance_id),stop_event=threading.Event(),api=SimpleNamespace(),
  _can_start_new_flow=lambda:False,can_pull_tasks=lambda:False,_worker_transaction_barrier_ready=lambda *a,**kw:True)
 services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
 d=HookBusinessDispatcher(rt.client,rt.private_root,lambda *a:None,lambda *a:None,automatic_transcription_disabled=lambda:True)
 d.runtime=rt;d._relationship_read_due=float('inf') # ordinary cycle before the next scheduled relationship read
 return manager,rt,runner,services,d

@pytest.mark.parametrize('arrival',['before_store','after_store','lost_before_store','lost_after_store'])
def test_retired_callback_is_customer_scoped_not_worker_fault(identity_client,lab,connection_factory,monkeypatch,tmp_path,arrival):
 w,t,p,b=bound();manager,rt,runner,services,d=make(w,lab,connection_factory,identity_client,tmp_path)
 table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
 table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
 while int(time.time())<=int(b['history_since_create_time']):time.sleep(.02)
 now=int(time.time())
 table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(9,'8200000000000000009',1,0,now,now*1000+9,0,9,b['peer_wxid'],'合成测试：请介绍车辆情况'))
 lab[0].catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'stale-audit'}]
 lab[0].query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
 event={'ServerPort':str(rt.scope.hook_api_port),'selfwxid':rt.scope.self_wxid,'sendorrecv':'2','msgnumber':'1',
  'msglist':[{'msgsvrid':'8200000000000000009','msgtype':'1','fromtype':'1','fromid':b['peer_wxid'],'toid':rt.scope.self_wxid,'msg':'合成测试：请介绍车辆情况'}]}
 NativeInbox(max_body_bytes=65536,max_inbox_bytes=131072).persist(json.dumps(event).encode(),rt.scope)
 original=HookIdentityClient.store_observations;fired=[]
 def retire():
  if not fired:
   fired.append(True);r=reinvite(b);assert r.status_code==200,r.text
 def store(*a,**kw):
  if arrival.endswith('before_store'):retire()
  if arrival=='lost_before_store' and not getattr(store,'lost',False):
   store.lost=True;raise OSError('synthetic delivery interruption')
  result=original(*a,**kw)
  if arrival.endswith('after_store'):retire()
  if arrival=='lost_after_store' and not getattr(store,'lost',False):
   store.lost=True;raise OSError('synthetic delivery interruption')
  return result
 monkeypatch.setattr(HookIdentityClient,'store_observations',store)
 error=None
 try:d.tick(runner,services)
 except Exception as exc:error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 if arrival.startswith('lost_'):
  assert error and error['type']=='OSError',error
  with storage.db_connection() as c:
   c.execute("UPDATE hook_delivery_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00'");c.commit()
  error=None
  try:d.tick(runner,services)
  except Exception as exc:error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 with storage.db_connection() as c:
  rows=c.execute('SELECT status,payload_json,receipt_json FROM hook_delivery_outbox').fetchall()
  assert len(rows)==1 and rows[0]['status']=='stored' and rows[0]['receipt_json'],error
 with SessionLocal() as db:
  from app.models.hook_native import HookNativeReceipt
  from app.models.wechat import MessageEvent
  assert len(list(db.scalars(select(HookNativeReceipt))))==1
  assert not list(db.scalars(select(MessageEvent)))

  data={'arrival':arrival,'error':error,'operator_confirmed':bool(fired),'binding_status':db.get(WechatSessionBinding,b['binding_id']).bind_status,'flow':db.get(Worker,w['id']).inflight_flow_state}
 with storage.db_connection() as c:
  data['raw_inbox_count']=c.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]
 data['physical_send_calls']=sum(path=='/SendTextMsg' for path,*_ in lab[0].requests)
 manager.close()
 assert fired
 assert error is None,data
 assert data['physical_send_calls']==0 and data['raw_inbox_count']>0

def test_superseded_invitation_discovery_is_customer_scoped(identity_client,lab,connection_factory,monkeypatch,tmp_path):
 w,t,p,b=bound();r=reinvite(b);assert r.status_code==200,r.text
 first=r.json()['data']['task'];prep=finish_invite(w,p,first)
 manager,rt,runner,services,d=make(w,lab,connection_factory,identity_client,tmp_path)
 fired=[]
 def on_request(path):
  lab[0].raw_override=None
  if path=='/GetFriendAndChatRoomList':
   with SessionLocal() as db:rev=db.get(WechatSessionBinding,b['binding_id']).authorization_revision
   r=reinvite({**b,'binding_revision':rev});assert r.status_code==200,r.text;fired.append(r.json()['data']['task']['id'])
   lab[0].raw_override=json.dumps({'code':0,'data':[{'wxid':b['peer_wxid'],'markname':prep['remark_name']}]}).encode()
 lab[0].on_request=on_request
 error=None
 try:d.tick(runner,services)
 except Exception as exc:error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 with SessionLocal() as db:
  old=db.get(WechatSessionBinding,b['binding_id'])
  data={'error':error,'new_rounds_created':len(fired),'old_invitation':first['id'],'current_invitation':old.native_context['reinvite_request']['task_id'],'old_status':old.bind_status,'replacement':old.replacement_binding_id,'worker_status':db.get(Worker,w['id']).run_status}
 manager.close()
 assert len(fired)==1
 assert error is None,data
 assert data['replacement'] is None

@pytest.mark.parametrize('lost_result',[False,'before','after'])
def test_voice_result_after_retirement_keeps_original_result_without_worker_fault(identity_client,lab,connection_factory,monkeypatch,tmp_path,lost_result):
 from test_hook_voice import persist,AUDIO,MID
 from app.models.hook_native import HookVoiceReceipt
 from app.models.wechat import MessageEvent
 w,t,p,b=bound();manager,rt,runner,services,d=make(w,lab,connection_factory,identity_client,tmp_path)
 table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
 table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
 while int(time.time())<=int(b['history_since_create_time']):time.sleep(.02)
 now=int(time.time());content=f'<msg><voicemsg length="{len(AUDIO)}"/></msg>'
 table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(9,MID,34,0,now,now*1000+9,0,9,b['peer_wxid'],content))
 lab[0].catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'voice-retire-audit'}]
 lab[0].query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
 persist(lab,metadata=True,scope=rt.scope);persist(lab,audio=True,scope=rt.scope)
 rt.sync(b)
 runner._can_start_new_flow=lambda:True;runner.can_pull_tasks=lambda:True
 original_voice=HookFormalClient.voice_result
 def voice_result(*args,**kwargs):
  if lost_result and not getattr(voice_result,'lost',False):
   voice_result.lost=True
   if lost_result=='after':original_voice(*args,**kwargs)
   raise OSError('synthetic result interruption')
  return original_voice(*args,**kwargs)
 monkeypatch.setattr(HookFormalClient,'voice_result',voice_result)
 fired=[];lab[0].voice_response={'status':1,'text':'合成转写内容'}
 def on_request(path):
  if path=='/Voice2Text':
   r=reinvite(b);assert r.status_code==200,r.text;fired.append(True)
   # This test covers original voice closure; new C1 UI is covered separately.
   runner.can_pull_tasks=lambda:False
 lab[0].on_request=on_request
 error=None
 try:d.tick(runner,services)
 except Exception as exc:error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 # A later ordinary poll must not re-transcribe the retired customer's audio.
 runner._can_start_new_flow=lambda:False;runner.can_pull_tasks=lambda:False
 next_error=None
 try:d.tick(runner,services)
 except Exception as exc:next_error={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc))}
 with SessionLocal() as db:
  parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
  data={'error':error,'next_error':next_error,'retired':bool(fired),'voice_calls':sum(path=='/Voice2Text' for path,*_ in lab[0].requests),'backend_voice_receipts':len(list(db.scalars(select(HookVoiceReceipt)))),'parent_state':parent.item_state,'local_voice_result':d.voice.saved_parent(rt.scope,b['peer_wxid'],MID),'flow':db.get(Worker,w['id']).inflight_flow_state}
 assert fired
 assert (error is not None and error['type']=='OSError' if lost_result else error is None) and next_error is None,data
 assert data['voice_calls']==1 and data['backend_voice_receipts']==1
 d.tick(runner,services)
 assert sum(path=='/Voice2Text' for path,*_ in lab[0].requests)==1
 assert list((rt.private_root/'voice-receipts').rglob('*.json'))
 manager.close();table.close()
