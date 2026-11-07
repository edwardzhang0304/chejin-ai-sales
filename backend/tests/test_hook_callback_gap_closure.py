"""Synthetic callback -> real runtime/history HTTP -> handoff -> fresh proof.

No real account, provider, model or notifications are used.
"""
from datetime import datetime
import json
import time
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent
from app.models.hook_native import HookHistoryReceipt
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from test_hook_business_http import clean
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_customer_refusal_closure import case, callback
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher


MISSING='9000000000000000088'


def missing_callback(c):
    scope=c.rt.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
        'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':MISSING,
            'msgtype':'3','fromtype':'1','fromid':scope.self_wxid,'toid':c.binding['peer_wxid'],
            'msg':'<msg><img/></msg>','time':'2026-01-01 10:00:00'}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)


@pytest.mark.parametrize('older_human', [False, True])
def test_missing_old_callback_bounded_handoff_restart_and_late_proof(case, older_human):
    c=case
    if older_human:
        time.sleep(1.05)  # This is an ordered post-binding row, not an ambiguous boundary second.
        created=int(time.time())
        c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
            (1,'9000000000000000007',1,1,created,created*1000,1,1,c.binding['peer_wxid'],'synthetic earlier assistance'))
    missing_callback(c)
    before={p:p.read_bytes() for p in c.rt.private_root.rglob('*.json')}
    first=c.rt.sync(c.binding)
    assert not first['history_ready'] and first['content_isolated'] and not first['message_batch']
    queries=[r for r in c.server.requests if r[0]=='/QueryDB' and 'CreateTime<' in r[1]['sql']]
    assert len(queries)==3
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)))
        assert len(events)==1 and events[0].conversation_id==c.binding['conversation_id']
        assert events[0].handoff_reason_code=='HOOK_HISTORY_CALLBACK_NOT_LANDED'
        assert db.get(Worker,c.w['id']).run_status=='running'
        assert db.get(WechatSessionBinding,c.binding['binding_id']).native_context['history_ready'] is False
        original=db.get(HookHistoryReceipt,first['history_receipt_id']).payload_json
        assert original['coverage']['missing_callback_ids']==[MISSING]
    inv=c.rt.client.call(c.rt.credentials,'POST',f"/workers/{c.w['id']}/wechat/native-work",c.rt.account)
    binding=next(b for b in inv['bindings'] if b['binding_id']==c.binding['binding_id'])
    assert binding['callback_gap_handoff_ids']==[MISSING]
    all_reads=len([r for r in c.server.requests if r[0]=='/QueryDB'])
    old=c.rt
    c.rt=HookFormalRuntime(old.client,old.credentials,old.gateway,old.prepared,old.private_root,wait=lambda _:None)
    dispatcher=HookBusinessDispatcher(c.rt.client,c.rt.private_root,None,None,automatic_transcription_disabled=lambda:True)
    dispatcher.runtime=c.rt
    for _ in range(10):
        result=dispatcher._read_history(binding)
        assert result['content_isolated'] and not result['history_ready']
    assert len([r for r in c.server.requests if r[0]=='/QueryDB' and 'CreateTime<' in r[1]['sql']])==3
    assert len([r for r in c.server.requests if r[0]=='/QueryDB'])==all_reads
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)
    assert all(p.read_bytes()==v for p,v in before.items())
    # The missing row later lands in the authorized range with native ordering.
    # This restores coverage, but cannot silently cancel the human handoff.
    created=int(time.time())
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
        (2,MISSING,3,1,created,created*1000+2,2,2,binding['peer_wxid'],'<msg><img/></msg>'))
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
        (3,'9000000000000000010',1,0,created,created*1000+3,3,3,binding['peer_wxid'],'synthetic new question'))
    callback(c)
    closed=c.rt.sync(binding)
    assert closed['history_ready'] and closed['message_batch'] is None
    with SessionLocal() as db:
        assert len(list(db.scalars(select(HandoffEvent))))==1
        assert db.get(Conversation,binding['conversation_id']).status=='waiting_sales_reply'
        assert db.get(HookHistoryReceipt,first['history_receipt_id']).payload_json==original
        assert db.get(Worker,c.w['id']).run_status=='running'
    # A verified later sales reply closes the handoff via the existing entry.
    # Closing it does not silently restore AI ownership or resend the old item.
    time.sleep(1.05)
    created=int(time.time()); human_id='9000000000000000011'
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
        (4,human_id,1,1,created,created*1000+4,4,4,binding['peer_wxid'],'synthetic sales assistance'))
    scope=c.rt.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
        'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':human_id,
            'msgtype':'1','fromtype':'1','fromid':scope.self_wxid,'toid':binding['peer_wxid'],
            'msg':'synthetic sales assistance','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)
    finished=c.rt.sync(binding)
    assert finished['history_ready'] and finished['message_batch'] is None
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent))
        assert event.closed_at is not None and event.status=='sales_replied'
        assert db.get(Conversation,binding['conversation_id']).status=='sales_replied_waiting_user'
        assert db.get(HookHistoryReceipt,first['history_receipt_id']).payload_json==original
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)


def test_restart_mid_retry_and_stale_inventory_do_not_renew_missing_id_budget(case):
    c=case;missing_callback(c)
    def crash(_):
        raise RuntimeError('synthetic interrupted retry')
    c.rt.wait=crash
    with pytest.raises(RuntimeError,match='synthetic interrupted retry'):
        c.rt.sync(c.binding)
    assert len([r for r in c.server.requests if r[0]=='/QueryDB' and 'CreateTime<' in r[1]['sql']])==1
    old=c.rt
    c.rt=HookFormalRuntime(old.client,old.credentials,old.gateway,old.prepared,old.private_root,wait=lambda _:None)
    assert c.rt.sync(c.binding)['content_isolated']
    assert len([r for r in c.server.requests if r[0]=='/QueryDB' and 'CreateTime<' in r[1]['sql']])==3
    # A fresh message invalidates only the terminal snapshot, not the missing
    # old ID's budget. Deliberately use stale inventory without backend gap IDs.
    created=int(time.time())
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
        (1,'9000000000000000010',1,0,created,created*1000+1,1,1,c.binding['peer_wxid'],'synthetic new question'))
    callback(c)
    assert c.rt.sync(c.binding)['content_isolated']
    assert len([r for r in c.server.requests if r[0]=='/QueryDB' and 'CreateTime<' in r[1]['sql']])==3
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)


def test_gap_hold_survives_other_history_reason_and_restart(case):
    c=case;missing_callback(c)
    boundary=int(c.binding['history_since_create_time'])
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
        (1,'9000000000000000007',1,0,boundary,boundary*1000,1,1,c.binding['peer_wxid'],'synthetic boundary message'))
    result=c.rt.sync(c.binding)
    assert result['reason_code']=='HOOK_HISTORY_BOUNDARY_SECOND_UNRESOLVED'
    assert result['callback_gap_isolated'] and not result['history_ready']
    reads=len([r for r in c.server.requests if r[0]=='/QueryDB'])
    old=c.rt
    c.rt=HookFormalRuntime(old.client,old.credentials,old.gateway,old.prepared,old.private_root,wait=lambda _:None)
    assert c.rt.sync(c.binding)['callback_gap_isolated']
    assert len([r for r in c.server.requests if r[0]=='/QueryDB'])==reads


def test_reliable_revoke_then_new_human_closes_gap(case):
    c=case
    missing_callback(c)
    first=c.rt.sync(c.binding)
    assert first['content_isolated'] and not first['history_ready']
    created=int(time.time())
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(2,'9000000000000000009',10002,0,created,created*1000+2,2,2,c.binding['peer_wxid'],f'<sysmsg type="revokemsg"><revokemsg><newmsgid>{MISSING}</newmsgid></revokemsg></sysmsg>'))
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(3,'9000000000000000010',1,0,created,created*1000+3,3,3,c.binding['peer_wxid'],'synthetic new question'))
    callback(c)
    recovered=c.rt.sync(c.binding)
    assert recovered['history_ready']
    time.sleep(1.05)
    created=int(time.time()); human='9000000000000000011'
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(4,human,1,1,created,created*1000+4,4,4,c.binding['peer_wxid'],'synthetic sales assistance'))
    scope=c.rt.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':human,'msgtype':'1','fromtype':'1','fromid':scope.self_wxid,'toid':c.binding['peer_wxid'],'msg':'synthetic sales assistance','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)
    final=c.rt.sync(c.binding)
    assert final['history_ready']
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));conv=db.get(Conversation,c.binding['conversation_id'])
        assert event.closed_at is not None, 'Verified revoke resolves missing callback, later sales reply must close handoff'
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)


def test_resolved_gap_reappears_does_not_fake_open_handoff(case):
    c=case
    test_missing_old_callback_bounded_handoff_restart_and_late_proof(c,False)
    c.db.execute('DELETE FROM MSG WHERE MsgSvrID=?',(MISSING,))
    time.sleep(1.05)
    created=int(time.time()); mid='9000000000000000012'
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(5,mid,1,0,created,created*1000+5,5,5,c.binding['peer_wxid'],'synthetic followup'))
    scope=c.rt.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':mid,'msgtype':'1','fromtype':'1','fromid':c.binding['peer_wxid'],'toid':scope.self_wxid,'msg':'synthetic followup','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)
    result=c.rt.sync(c.binding)
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)));conv=db.get(Conversation,c.binding['conversation_id'])
        assert result['callback_gap_isolated'] and len(events)==2
        assert sum(e.closed_at is None for e in events)==1
        assert conv.status=='waiting_sales_reply'
    reads=len(c.server.requests)
    for _ in range(4):
        assert c.rt.sync(c.binding)['callback_gap_isolated']
    # The outer account identity check remains mandatory on every sync.
    assert all(request[0]=='/IsLoginStatus' for request in c.server.requests[reads:])
    with SessionLocal() as db:
        assert len(list(db.scalars(select(HandoffEvent))))==2


def test_produced_gap_records_reach_formal_zip(case,tmp_path):
    from chejin_worker_client import storage
    from chejin_worker_client.diagnostic_bundle import export_bundle
    from zipfile import ZipFile
    c=case;missing_callback(c);result=c.rt.sync(c.binding)
    target=export_bundle(storage.APP_DIR,tmp_path/'actual-gap-diagnostic.zip')
    with ZipFile(target) as z:
        counters=json.loads(z.read('state/hook_callback_gap_reads.json'))['rows']
        holds=json.loads(z.read('state/hook_callback_gap_holds.json'))['rows']
        assert any(str(r['native_msg_id'])==MISSING and r['attempts']==3 for r in counters)
        assert any(result['history_receipt_id'] == r['result_json'].get('history_receipt_id') for r in holds)
        receipts=[n for n in z.namelist() if 'history-coverage/' in n]
        assert any(MISSING.encode() in z.read(n) for n in receipts)
