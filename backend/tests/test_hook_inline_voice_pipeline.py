"""Actual Worker/backend/send pipeline with synthetic native and model responses."""
import pytest
from types import SimpleNamespace
import json,sqlite3,threading,time
from pathlib import Path
from app.core.config import get_settings
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_partial_reply_recovery_provider import controlled_recovery_provider
from sqlalchemy import select,func
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch,ReplyAction,SentAck,HandoffEvent
from app.models.wechat import MessageEvent
from app.models.hook_native import HookVoiceReceipt
from test_hook_business_http import clean
from test_hook_history_business_http import bound
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_projection import create_projection_store


@pytest.mark.parametrize('delivery_mode', ['normal', 'request_lost', 'response_lost', 'failed_result'])
def test_inline_only_voice_then_text_reaches_one_reply_and_ack(identity_client,lab,connection_factory,tmp_path,monkeypatch,delivery_mode):
    w,_,_,binding=bound()
    server,gateway,*_=lab
    manager,credentials,prepared=connection_factory(w)
    scope=prepared.registration.scope
    client=HookFormalClient(identity_client.base_url,5)
    dispatcher=HookBusinessDispatcher(client,tmp_path/'runtime',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True)
    services=HookWorkerServices(manager,create_projection_store(),intake_interval_seconds=.02,
        projection_batch_size=1,dispatcher=dispatcher)
    services._credentials=credentials
    services.startup_window_ready_for=lambda _:True
    from chejin_worker_client import startup_check_state
    services._startup_check=startup_check_state.passed()
    services._startup_layout_ready=lambda:True
    errors=[]
    runner=TaskRunner(WorkerApiClient(identity_client.base_url),SimpleNamespace(sidecar_active=lambda:False),
        on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,
        on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append,hook_services=services)
    runner.binding=Binding(w['id'],w['worker_token'],w['instance'],'running')
    storage.save_binding(runner.binding)
    dispatcher._observe_clock(SimpleNamespace(clock=prepared.clock),prepared.account)
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'inline-voice'}]
    server.query_response=lambda body:{'data':[dict(r) for r in table.execute(body['sql']).fetchall()]}
    while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
    def append(kind,sender,text):
        n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1;now=int(time.time());mid=str(8100000000000000000+n)
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(n,mid,kind,sender,now,now*1000+n,0,n,binding['peer_wxid'],text))
        return mid
    audio=b'\x02#!SILK_V3'+b'synthetic-inline-voice'
    xml=f'<msg><voicemsg length="{len(audio)}"/></msg>'
    mid=append(34,0,xml)
    append(1,0,'可以贷款吗')
    event={'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msgnumber':'1',
        'msglist':[{'msgtype':'34','msgsvrid':mid,'fromtype':'1','fromid':binding['peer_wxid'],'toid':scope.self_wxid,
                    'msg':xml,'voice_hex':audio.hex(),'voice_len':str(len(audio))}]}
    NativeInbox(max_body_bytes=4096,max_inbox_bytes=65536).persist(json.dumps(event).encode(),scope)
    server.voice_response={'status':1,'text':'想看五万元左右的轿车'}
    if delivery_mode == 'failed_result':
        server.voice_response={'status':0,'text':''}
    def on_request(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            append(1,1,server.requests[-1][1]['msg'])
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    engine=str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',engine)
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',engine)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    requests=[]
    provider=controlled_recovery_provider(monkeypatch,'好的，先按五万元左右了解，您更在意油耗还是空间？',requests,catalog_claim_kind='non_inventory')
    try:
        with provider:
            if delivery_mode == 'normal':
                _drive(dispatcher,runner,services,server,event,scope,audio)
            else:
                _drain_voice_result(dispatcher,runner,services,server,event,scope,monkeypatch,delivery_mode)
                return
        assert [r['model'] for r in requests]==['recovery-author','recovery-reviewer']
    finally:table.close()


def _drive(dispatcher,runner,services,server,event,scope,audio):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        from chejin_worker_client.api import ApiError
        try:
            dispatcher.tick(runner,services)
        except ApiError as exc:
            if exc.code != 'HOOK_BACKEND_TEMPORARILY_UNAVAILABLE':raise
            time.sleep(.2)
            continue
        with SessionLocal() as db:
            if db.scalar(select(func.count()).select_from(SentAck))==1:break
        time.sleep(.05)
    else:raise AssertionError('inline voice did not reach send acknowledgement')
    # A later same-audio callback cannot start a second transcription/send.
    event['msglist'][0].update(msg='',fromid='',toid='',fromtype='',voice_hex='',voice_data=audio.hex())
    NativeInbox(max_body_bytes=4096,max_inbox_bytes=65536).persist(json.dumps(event).encode(),scope)
    for _ in range(2):dispatcher.tick(runner,services)
    assert [path for path,*_ in server.requests].count('/Voice2Text')==1
    assert [path for path,*_ in server.requests].count('/SendTextMsg')==1
    with SessionLocal() as db:
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        assert parent.item_state=='completed' and parent.content==server.voice_response['text']
        assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==1
        assert db.scalar(select(func.count()).select_from(MessageBatch))==1
        assert db.scalar(select(ReplyAction)).status=='sent'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0


def _drain_voice_result(dispatcher,runner,services,server,event,scope,monkeypatch,mode):
    from chejin_worker_client.api import ApiError
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    calls=[]; pending=threading.Event(); release=threading.Event(); errors=[]
    original=type(dispatcher.client).voice_result
    def upload(client,*args,**kwargs):
        calls.append(rules.digest(args[-1]))
        if len(calls)==1:
            runner.begin_update_check(lambda:{'update_request_id':'voice-drain-test'})
            if mode=='response_lost': original(client,*args,**kwargs)
            pending.set()
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic lost upload',0)
        if not release.is_set():
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic offline',0)
        return original(client,*args,**kwargs)
    monkeypatch.setattr(type(dispatcher.client),'voice_result',upload)
    dispatcher.interval_seconds=.02
    def consume():
        try:services.business_loop(runner,lambda _:None)
        except BaseException as exc:errors.append(repr(exc))
    thread=threading.Thread(target=consume);thread.start()
    try:
        assert pending.wait(10),errors
        runner._sync_pending_run_status(force=True)
        deadline=time.monotonic()+5
        while len(calls)<2 and time.monotonic()<deadline:time.sleep(.02)
        assert len(calls)>=2, 'recovery must continue without restarting Worker'
        before=runner.update_install_safety_snapshot()
        assert not before['safe'] and not before['settlement_complete']
        assert before['hook_pending_voice_results']==1
        release.set()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            after=runner.update_install_safety_snapshot()
            if after['safe']:break
            time.sleep(.05)
        assert after['safe'],after
        assert after['hook_pending_voice_results']==0
        assert len(set(calls))==1
        assert sum(r[0]=='/Voice2Text' for r in server.requests)==1
        assert not any(r[0]=='/SendTextMsg' for r in server.requests)
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==1
            assert db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice')).item_state == ('failed' if mode=='failed_result' else 'completed')
        assert not errors
    finally:
        runner.stop_event.set();release.set();thread.join(5)
    assert not thread.is_alive()
