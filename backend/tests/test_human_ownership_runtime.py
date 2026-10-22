"""Real Worker/backend with simulated Hook HTTP, mock AI, virtual due check."""
from test_hook_formal_runtime import (
    replace, SimpleNamespace, json, sqlite3, time, Path, pytest, select,
    SessionLocal, Task, MessageEvent, clean, claimed, post, identity_client, lab,
    HookWorkerCredentials, HookFormalClient, HookFormalRuntime, ReplayUI,
    calibrated_clock, drive_reply,
)
from test_partial_reply_recovery_provider import recovery_provider_factory
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services import c3_service
from app.core.config import get_settings
from app.models.c3 import ReplyAction, SentAck

from datetime import datetime,timedelta,timezone
from app.models.c3 import Conversation,MessageBatch,HumanReplyReminder
from app.models.sales import Sales
from app.services import human_reminder_service as reminders,wechat_service
from test_human_reply_ownership import Adapter

@pytest.fixture
def human_runtime(identity_client,lab,tmp_path,monkeypatch):

    # Shorten only the recall delay for physical-loop tests; the API timing
    # tests separately verify the production 72-hour and quiet-hour settings.
    monkeypatch.setattr(get_settings(),"c3_recall_after_hours",0.001)
    monkeypatch.setattr(get_settings(),"c3_recall_quiet_start_hour",0)
    monkeypatch.setattr(get_settings(),"c3_recall_quiet_end_hour",0)
    server,gateway,process,scope,*_=lab
    w,t,account=claimed(process_generation=process.generation,pending=True)
    scope=replace(scope,account_epoch=account['account_epoch'])
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),
        clock=calibrated_clock(identity_client,credentials,gateway,process,scope))
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),
        credentials,gateway,prepared,tmp_path/'coordinator')
    uis=[]
    def factory(prep,guard):
        class ClipboardReplay(ReplayUI):
            def __init__(self):super().__init__();self.clipboard_field_events=[]
            def replace(self,frame,text):
                guard();super().replace(frame,text)
                if frame.name.startswith('fields'):
                    self.clipboard_field_events.append({'copy_calls':1,'paste_calls':1,'clipboard_match':True,'completion_guard_passed':True})
            def capture(self,name):
                guard();frame=super().capture(name)
                if name=='lookup':
                    for row in frame.rows:row['text']=row['text'].replace('13800000003',prep['primary_phone'])
                return frame
        ui=ClipboardReplay();uis.append(ui);return ui
    outcome=runtime.invite(t,factory)
    assert 'task' in outcome.get('settlement',{}),outcome
    assert outcome['settlement']['task']['result_code']=='invite_sent'
    code=outcome['preparation']['remark_code']
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'runtime-test-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    def on_request(path):
        server.raw_override=None
        if path=='/GetFriendAndChatRoomList':server.raw_override=json.dumps({'code':0,'data':[{'wxid':'wxid_customer_A','markname':code}]}).encode()
        if path=='/SendTextMsg':
            text=server.requests[-1][1]['msg'];created=int(time.time());seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
            before=getattr(server,'before_send',None)
            if before:before()
            seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,1,created,created*1000+seq,0,seq,'wxid_customer_A',text))
            after=getattr(server,'after_send',None)
            if after:after()
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    binding=runtime.discover(t['id'])
    # Clear the invite's ambiguous boundary second without changing production guards.
    time.sleep(1.05)
    return runtime,binding,table,server,w


def add_message(table,*,sender,text):
    seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
    created=int(time.time())
    table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,sender,created,created*1000+seq,0,seq,'wxid_customer_A',text))


def test_human_recall_ai_reply_human_again_and_one_reminder(human_runtime,monkeypatch,recovery_provider_factory):
    runtime,binding,table,server,w=human_runtime
    add_message(table,sender=0,text='请帮我看看车辆')
    add_message(table,sender=1,text='我来帮您确认，后续我负责')
    first=runtime.sync(binding);assert first['history_ready'] and first['message_batch'] is None, json.dumps(first,ensure_ascii=False)
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        assert conv.reply_ownership['owner']=='human'
        due=conv.next_recall_at.replace(tzinfo=timezone.utc)
    time.sleep(max(0,(due-datetime.now(timezone.utc)).total_seconds())+.05)
    recall=runtime.sync(binding);assert recall['message_batch'],recall
    bid=recall['message_batch']['batch_id']
    from app.models.base import utcnow
    monkeypatch.setattr(wechat_service,'utcnow',utcnow)
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    recovery_provider_factory('之前聊过购车的事情，您最近还有想了解的吗？可以按您的节奏慢慢聊。')
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    result=drive_reply(runtime,binding,bid,max_wait_seconds=30)
    assert len(result['sends'])==1 and result['sends'][0]['local_send_observed'],result
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        assert conv.reply_ownership['owner']=='ai' and conv.reply_ownership['recall_streak']==1
        assert conv.recall_count==1
    # A real new customer fact (not replay or transcript replacement) resets
    # the silence streak, while the confirmed daily usage remains recorded.
    from app.models.base import utcnow
    from app.services.ai_adapter import MockOmniAutoAIEngineAdapter
    monkeypatch.setattr(wechat_service,"utcnow",utcnow)
    monkeypatch.setattr(c3_service,"get_ai_engine_adapter",MockOmniAutoAIEngineAdapter)
    add_message(table,sender=0,text='现在想看看五万元以内的车')
    next_turn=runtime.sync(binding);assert next_turn['message_batch'],next_turn
    reply=drive_reply(runtime,binding,next_turn['message_batch']['batch_id'],max_wait_seconds=30)
    assert len(reply['sends'])>=1 and all(x['local_send_observed'] for x in reply['sends']),reply
    add_message(table,sender=1,text='我接着处理，您先看我的介绍')
    runtime.sync(binding)
    add_message(table,sender=0,text='那具体情况请你再说一下')
    waiting=runtime.sync(binding);assert waiting['message_batch'] is None
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id']);row=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.closed_at.is_(None)))
        assert conv.reply_ownership['owner']=='human' and conv.reply_ownership['recall_streak']==0
        assert conv.recall_daily_count==1
        rid=row.id
        # Move only the round due time for this consumer check. The producer
        # timestamp and complete hour/night arithmetic have separate API tests.
        row.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        db.get(Sales,conv.sales_id).feishu_user_id='ou_synthetic_runtime'
        db.commit()
    runtime.sync(binding)
    adapter=Adapter()
    monkeypatch.setattr(reminders,'next_daytime',lambda now:now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='succeeded'
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='not_due'
    assert len(adapter.calls)==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1+len(reply['sends'])


@pytest.mark.parametrize('interruption',['human_after','customer_before','customer_after','none'])
def test_first_recall_segment_ownership_uses_native_order_and_keeps_receipts(
        human_runtime,monkeypatch,recovery_provider_factory,interruption):
    runtime,binding,table,server,w=human_runtime
    add_message(table,sender=0,text='先了解一下')
    add_message(table,sender=1,text='销售先为您介绍')
    assert runtime.sync(binding)['history_ready']
    with SessionLocal() as db:
        due=db.get(Conversation,binding['conversation_id']).next_recall_at.replace(tzinfo=timezone.utc)
    time.sleep(max(0,(due-datetime.now(timezone.utc)).total_seconds())+.05)
    recall=runtime.sync(binding);bid=recall['message_batch']['batch_id']
    from app.models.base import utcnow
    monkeypatch.setattr(wechat_service,'utcnow',utcnow)
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    recovery_provider_factory(['之前聊过购车的事情，您最近还有想了解的吗？可以先告诉我您打算怎么用车，我再帮您梳理下一步要看的信息，也可以先了解一下您比较关注的用车需求。','如果目前还在考虑也没关系，可以按您的节奏慢慢聊。您有具体问题时随时告诉我，我会先确认情况再继续介绍，也会把目前能确认的情况和需要核实的信息分开说明。'])
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    def interrupt():
        server.before_send=server.after_send=None
        add_message(table,sender=1 if interruption=='human_after' else 0,text='这是一条新的人工回复' if interruption=='human_after' else '客户还有新问题')
    if interruption=='customer_before':server.before_send=interrupt
    elif interruption!='none':server.after_send=interrupt
    # Stop after the first physically sent segment to inspect the true cutover,
    # before a later normal poll applies the newly observed actors.
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        view=runtime.batch_view(bid)
        if not view.get('processing'):break
        runtime.wait(.05)
    assert view['reply_action']['status']=='queued',view
    history=runtime.sync(binding,show_progress=False)
    first=runtime.reply_once(binding,{**view,'batch_id':bid},history=history)
    assert first['status']=='sent',first
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        assert conv.reply_ownership['owner']==('ai' if interruption in {'none','customer_after'} else 'human')
        assert conv.recall_count==1
        assert db.scalar(select(SentAck)).send_result=='sent'
    runtime.sync(binding,show_progress=False)
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        actions=list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==bid).order_by(ReplyAction.segment_index)))
        assert len(actions)==2 and actions[0].status=='sent'
        if interruption!='none':assert actions[1].status in {'cancelled','superseded'},actions[1].status
        else:assert actions[1].status=='queued'
        assert conv.reply_ownership['owner']==('human' if interruption in {'human_after','customer_before'} else 'ai')
        if interruption=='customer_before':assert conv.reply_ownership['round_id']
        if interruption=='customer_after':assert conv.reply_ownership['recall_streak']==0
        assert len(list(db.scalars(select(SentAck))))==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
