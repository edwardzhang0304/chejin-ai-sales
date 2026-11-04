"""Active-capture ownership round; actual Worker subprocess and backend.

Only physical Hook, model replies and Feishu delivery are synthetic. Original
history, ownership, reminder, Brain/reviewer, send and ACK consumers run.
"""
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.request import Request, urlopen

from sqlalchemy import select
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck, HumanReplyReminder
from app.models.sales import Sales
from app.models.task import Task
from app.services import c3_service, hook_history_service, human_reminder_service, feishu_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_memory_business_process import clean, Actor, finalize_evidence, second_bound
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_history_business_http import bound
from test_partial_reply_recovery_provider import controlled_recovery_provider
from test_human_reply_ownership import Adapter, daytime_after
from test_c3_recovery_isolation import run_rounds
from chejin_worker_client import memory_capture as mc


def test_active_capture_human_recall_old_voice_retake_and_each_round_reminder(identity_client,lab,monkeypatch,tmp_path):
    settings=get_settings()
    original_recall_hours=settings.c3_recall_after_hours
    assert original_recall_hours == 72
    engine=str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',engine)
    monkeypatch.setattr(settings,'c3_omniauto_root',engine)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    w,_,account,binding=bound()
    other=second_bound(w,account)
    server,gateway,process,scope,receiver,receiver_thread=lab
    receiver.shutdown();receiver_thread.join(2);receiver.server_close()
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'ownership-memory-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    wall_offset = [timedelta(0)]
    def wall_now():
        return datetime.now(timezone.utc) + wall_offset[0]
    def append(kind,sender,text,peer=None):
        n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1; now=int(wall_now().timestamp())
        mid=str(8100000000000000000+n)
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(n,mid,kind,sender,now,now*1000+n,0,n,peer or binding['peer_wxid'],text))
        return mid
    def on_request(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            append(1,1,server.requests[-1][1]['msg'],server.requests[-1][1]['toid'])
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    folder=tmp_path/'worker';folder.mkdir(); actor=Actor(folder)
    report={};evidence={'original_recall_hours':original_recall_hours,'controlled_clocks':[]}
    requests=[];exchanges=[];results=[];request_thread=None;armed=None
    def advance(stamp):
        # Only test wall clocks advance. Production due/lease/receipt policies
        # stay unchanged; monotonic capture and sender waits remain real.
        from app.models import base
        class ControlledWallClock(datetime):
            @classmethod
            def now(cls, tz=None):
                current = wall_now()
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)
        # Patch the source clock, not SQLAlchemy's already-cached default
        # wrappers: both service timestamps and INSERT defaults call utcnow.
        monkeypatch.setattr(base, 'datetime', ControlledWallClock)
        wall_offset[0] = stamp - datetime.now(timezone.utc)
        actor.call('clock', now=stamp.isoformat())
        evidence['controlled_clocks'].append(stamp.isoformat())
    adapter=Adapter()
    monkeypatch.setattr(feishu_service,'get_feishu_adapter',lambda:adapter)
    monkeypatch.setattr(human_reminder_service,'get_feishu_adapter',lambda:adapter)
    with SessionLocal() as db:
        sales=db.scalar(select(Sales));sales.feishu_user_id='ou_synthetic_memory_owner';db.commit()
    try:
        init=actor.call('init',deployment=asdict(gateway.deployment),process=asdict(process),
            credentials={'worker_id':w['id'],'worker_token':w['worker_token'],'client_instance_id':w['instance']},
            base_url=identity_client.base_url)
        armed=actor.call('arm')
        request_thread=threading.Thread(target=lambda:results.append(mc.request_capture(folder)))
        request_thread.start();assert actor.call('await_capture')['tracing']
        audio=b'\x02#!SILK_V3'+bytes(range(32))
        parent=append(34,0,f'<msg><voicemsg length="{len(audio)}"/></msg>')
        assert actor.call('sync',binding=binding)['reason_code']=='HOOK_VOICE_PENDING'
        callback={'ServerPort':str(gateway.deployment.hook_api_port),'selfwxid':scope.self_wxid,
            'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':parent,'msgtype':'34','fromtype':'1',
            'fromid':binding['peer_wxid'],'toid':scope.self_wxid,'msg':f'<msg><voicemsg length="{len(audio)}"/></msg>',
            'voice_data':audio.hex(),'voice_len':str(len(audio))}]}
        with urlopen(Request(init['callback_url'],data=json.dumps(callback).encode(),headers={'Content-Type':'application/json'}),timeout=5) as response:assert response.status==200
        server.voice_response={'status':1,'text':'之前的问题已经由销售处理'}
        saved=actor.call('voice_save',binding=binding,native_id=parent)
        assert saved['state']=='transcribed'
        append(3,0,'<msg><img md5="synthetic"/></msg>')
        append(1,1,'我是销售，之前的问题已经处理，请问还有什么需要？')
        held=actor.call('sync',binding=binding);assert held['message_batch'] is None
        with SessionLocal() as db:
            conv=db.get(Conversation,binding['conversation_id'])
            assert conv.reply_ownership['owner']=='human'
            due=conv.next_recall_at.replace(tzinfo=timezone.utc)
        from app.services.hook_business_time import aware
        assert due - aware(conv.last_sales_reply_at) == timedelta(hours=72)
        advance(due-timedelta(seconds=5))
        assert actor.call('sync',binding=binding)['message_batch'] is None
        future=(due+timedelta(days=1)).replace(hour=2,minute=0,second=0,microsecond=0)
        advance(future)
        with controlled_recovery_provider(monkeypatch,'之前聊过选车的事情，您最近还有什么想了解的吗？',requests,catalog_claim_kind='non_inventory',exchange_observer=exchanges.append):
            recall=actor.call('reply',binding=binding)
        assert len(recall['sends'])==1 and recall['sends'][0]['status']=='sent'
        with SessionLocal() as db:
            conv=db.get(Conversation,binding['conversation_id']);assert conv.reply_ownership['owner']=='ai'
            assert db.get(MessageBatch,recall['batch_id']).trigger_type=='recall'
            action=db.scalar(select(ReplyAction).where(ReplyAction.batch_id==recall['batch_id']))
            task=db.get(Task,action.claimed_task_id)
            replay={'action_id':action.id,'task_id':task.id,'token':task.lease_fencing_token,
                'flow_id':action.ai_payload['native_send_identity']['flow_id'],
                'request':action.ai_payload['native_send_settlement']['request']}
        append(1,0,'我主要是市区接送孩子，想先了解空间方面怎么选。')
        new=actor.call('sync',binding=binding);assert new['message_batch']['next_step']=='generate'
        late=actor.call('voice_publish',binding=binding,native_id=parent)
        assert not late['history_coverage_required']
        with controlled_recovery_provider(monkeypatch,'好的，接送孩子可以先看看后排空间和安全座椅安装位置，您通常几个人乘坐？',requests,catalog_claim_kind='non_inventory',exchange_observer=exchanges.append):
            reply=actor.call('reply',binding=binding)
        assert len(reply['sends'])==1 and reply['sends'][0]['status']=='sent'
        append(1,1,'我接着给您介绍。');actor.call('sync',binding=binding)
        ack=actor.call('replay_settlement',**replay)
        evidence.update(recall=recall,new_reply=reply,late_voice=late,replayed_ack=ack)
        with SessionLocal() as db:
            assert db.get(Conversation,binding['conversation_id']).reply_ownership['owner']=='human'
            acks=list(db.scalars(select(SentAck)))
            assert len(acks)==2 and all(a.send_result=='sent' for a in acks)
        # A human-owned customer must not stop a different customer's AI.
        append(1,0,'我想了解通勤用车，平时一家三口乘坐。',other['peer_wxid'])
        with controlled_recovery_provider(monkeypatch,'好的，一家三口通勤可以先考虑乘坐空间，您平时停车方便吗？',requests,catalog_claim_kind='non_inventory',exchange_observer=exchanges.append):
            independent=actor.call('reply',binding=other)
        assert len(independent['sends'])==1 and independent['sends'][0]['status']=='sent'
        with SessionLocal() as db:
            assert db.get(Conversation,binding['conversation_id']).reply_ownership['owner']=='human'
            assert db.get(Conversation,other['conversation_id']).reply_ownership['owner']=='ai'
            independent_acks=list(db.scalars(select(SentAck).join(ReplyAction,SentAck.reply_action_id==ReplyAction.id).where(ReplyAction.conversation_id==other['conversation_id'])))
            assert len(independent_acks)==1 and independent_acks[0].send_result=='sent'
        evidence.update(other_binding=other,other_reply=independent)
        # Actual history consumers create each reminder; no pending state seeded.
        reminder_ids=[]; clocks=[]
        for round_index in range(2):
            if round_index:
                append(1,1,'上个问题已经说明，我继续帮您处理。')
                actor.call('sync',binding=binding)
            append(1,0,'还有一个使用上的问题，麻烦您再说明一下。')
            assert actor.call('sync',binding=binding)['message_batch'] is None
            with SessionLocal() as db:
                reminder=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.closed_at.is_(None)))
                start=human_reminder_service.aware(reminder.started_at)
                assert (human_reminder_service.aware(reminder.next_attempt_at)-start).total_seconds()==3600
                reminder_ids.append(reminder.id)
            future=daytime_after(start)
            advance(future)
            assert actor.call('sync',binding=binding)['history_ready']
            run_rounds(2)
            with SessionLocal() as db:
                reminder=db.get(HumanReplyReminder,reminder_ids[-1])
                assert reminder.status=='succeeded' and reminder.attempt_count==1
            clocks.append(future.isoformat())
        assert len(set(reminder_ids))==2
        assert len(adapter.calls)==2
        assert len([r for r in server.requests if r[0]=='/Voice2Text'])==1
        assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==3
        evidence.update(reminder_ids=reminder_ids,controlled_reminder_clocks=clocks,notifications=adapter.calls,saved_voice=saved)
        report.update(mode='on',scenario='ownership',send_count=3)
    finally:
        dest=Path(os.environ.get('PE02_D2_OUTPUT',tmp_path/'evidence'))/'on-ownership'
        finalize_evidence(actor,table,folder,dest,report,evidence,request_thread,results,armed,w['id'],exchanges,server.requests)
    assert results[0]['status']=='finished',results
    assert report['qt_max_gap']<2
    active=[s for s in report['spans'] if s['tracing_at_start'] and s['tracing_at_end']]
    assert sum(s['operation']=='HookNativeSender.execute' for s in active)==3
    assert sum(s['operation']=='HookFormalClient.native_send_settle' for s in active)==3
    assert len([s for s in active if s['operation']=='HookFormalRuntime.sync'])>=8
