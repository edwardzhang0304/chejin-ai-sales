"""Zero native IDs remain uncertainty, never successful delivery or new send authority."""
from copy import deepcopy
import json
import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from test_hook_fault_recovery_http import clean, local_storage, retained_scenario
from test_hook_account_client import identity_client
from test_hook_history_business_http import bound, coverage, upload
from test_hook_friend_verification import append, notifications
from chejin_worker_client.shared_rules import hook_history_rules as rules


@pytest.mark.parametrize('invalidated', [False, True])
def test_original_zero_send_settles_unknown_and_releases_flow_without_changing_journals(identity_client, tmp_path, monkeypatch, invalidated):
    w,p,b,h,permit,value,d,runner,services = retained_scenario(
        identity_client,tmp_path,monkeypatch,invalidate=invalidated)
    for scan in value['scans']:
        scan['databases'][0]['pages'][0][-1]['MsgSvrID']='0'
    original={f: f.read_bytes() for f in (d.private_root/'sends').rglob('*.json')}
    with SessionLocal() as db:
        original_ai=deepcopy(db.get(ReplyAction,permit['reply_action_id']).ai_payload)
        worker=db.get(Worker,w['id']);worker.run_status='faulted';db.commit()
    runner.run_status='faulted'
    # Dispatcher settles the retained action first; the following tick closes
    # its original flow, even while new work remains faulted.
    for _ in range(3):d.tick(runner,services)
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id'])
        assert action.status=='unknown_send_result'
        assert action.ai_payload['native_send_identity']==original_ai['native_send_identity']
        assert action.ai_payload['native_send_attempt']==original_ai['native_send_attempt']
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id))
        assert ack.send_result=='unknown'
        assert not db.get(Worker,w['id']).inflight_flow_state
        assert db.get(Worker,w['id']).run_status=='faulted'  # Operator still explicitly resumes.
        assert not db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.sender_role=='self'))
        if not invalidated:
            assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
    assert all(f.read_bytes()==raw for f,raw in original.items())
    receipt=json.loads((d.private_root/'send-receipts'/f"{permit['reply_action_id']}.json").read_bytes())
    assert receipt['local_send_observed'] is False and receipt['automatic_resend_allowed'] is False
    # Recovery path has no sender/gateway. A physical action would fail this test.


def test_zero_outgoing_handoffs_only_customer_once_and_never_builds_ai_context(monkeypatch):
    w,t,p,b=bound();sent=notifications(w,monkeypatch)
    value=coverage(b);append(value,kind='1',sender='1',text='synthetic unresolved text')
    for scan in value['scans']:scan['databases'][0]['pages'][0][-1]['MsgSvrID']='0'
    for _ in range(3):
        r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
        result=r.json()['data']
        assert not result['history_ready'] and result['message_batch'] is None
        assert result['reason_code']==rules.OUTGOING_ID_PENDING
        assert not result['recovery_settlement_allowed']
    with SessionLocal() as db:
        assert db.get(Worker,w['id']).run_status=='running'
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        assert not db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.sender_role=='self'))
    assert len(sent)==1


@pytest.mark.parametrize('damage',['incoming_zero','voice_zero','duplicate','changed_scan','incomplete','other_peer'])
def test_zero_outgoing_does_not_bypass_other_history_guards(damage):
    value=coverage({'history_since_create_time':'1791543600','peer_wxid':'wxid_synthetic'})
    append(value,kind='1',sender='1',text='unconfirmed')
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0];rows[-1]['MsgSvrID']='0'
        if damage=='incoming_zero':rows[-1]['IsSender']='0'
        if damage=='voice_zero':rows[-1]['Type']='34'
        if damage=='duplicate':rows[1]['MsgSvrID']=rows[0]['MsgSvrID']
        if damage=='incomplete':scan['databases'][0]['row_count']=scan['databases'][0]['count_after']='4'
        if damage=='other_peer':rows[-1]['StrTalker']='wxid_other'
    if damage=='changed_scan':value['scans'][1]['databases'][0]['pages'][0][-1]['MsgSvrID']='7'
    with pytest.raises(ValueError):rules.validate_coverage(value,'wxid_synthetic',1791543600)


@pytest.mark.parametrize('field,value', [('reconciliation_only',False),('reconciliation_only','true'),
                                       ('recovery_settlement_allowed',False),('recovery_settlement_allowed',1),
                                       ('reason_code','HOOK_HISTORY_ORDER_UNRESOLVED')])
def test_unknown_receipt_permission_cannot_be_generalized(field,value):
    result={'history_ready':False,'reason_code':rules.OUTGOING_ID_PENDING,
            'reconciliation_only':True,'recovery_settlement_allowed':True}
    assert rules.retained_send_history_ready(result)
    result[field]=value
    assert not rules.retained_send_history_ready(result)


# Native process inspection, physical send and the model are fixtures; actual
# SQLite QueryDB -> gateway -> synchronizer -> runtime -> HTTP -> ACK/finish run.
from test_hook_gateway import lab
from test_hook_history import history_lab
from test_hook_callback_recovery import connection_factory


@pytest.mark.parametrize('invalidated',[False,True])
def test_zero_querydb_actual_runtime_through_deadline_unknown_ack_and_flow_finish(
        history_lab,identity_client,connection_factory,tmp_path,monkeypatch,invalidated):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from app.services import hook_send_service,c3_service
    from test_hook_business_http import post
    server,gateway,process,scope,delivery,catalog,db_native=history_lab
    w,p,b,h,permit,value,d,runner,services=retained_scenario(identity_client,tmp_path,monkeypatch,invalidate=invalidated)
    since=int(b['history_since_create_time'])
    native_rows=value['scans'][0]['databases'][0]['pages'][0]
    native_rows[-1]['MsgSvrID']='0'
    db_native.execute('DELETE FROM MSG')
    fields=['localId','MsgSvrID','Type','IsSender','CreateTime','Sequence','MsgServerSeq','MsgSequence','StrTalker','StrContent']
    db_native.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',[
        tuple(row[k] for k in fields) for row in native_rows])
    manager,credentials,prepared=connection_factory(w)
    assert prepared.registration.scope.account_epoch==p['account_epoch']
    d.runtime=HookFormalRuntime(d.client,credentials,gateway,prepared,d.private_root)
    d.runtime.clock.now=lambda:datetime.fromtimestamp(since+60,timezone.utc)
    services.connection=manager;services._credentials=credentials
    assert post(w,f"/workers/{w['id']}/run-status",{'run_status':'faulted','client_instance_id':w['instance']}).status_code==200
    runner.binding.run_status='faulted'
    monkeypatch.setattr(c3_service,'generate_for_batch',lambda *a,**kw:pytest.fail('Recovery started AI'))
    clock=[since+50]
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromtimestamp(clock[0],timezone.utc))
    originals={f:f.read_bytes() for f in (d.private_root/'sends').rglob('*.json')}
    # Before the original confirmation deadline: real observation, no settlement.
    d.tick(runner,services)
    with SessionLocal() as db:
        assert db.scalar(select(SentAck)) is None
        assert db.get(Worker,w['id']).inflight_flow_state
    assert not (d.private_root/'send-receipts'/f"{permit['reply_action_id']}.json").exists()
    assert any(path=='/QueryDB' for path,*_ in server.requests)
    clock[0]=since+600
    for _ in range(3):d.tick(runner,services)
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id'])
        assert action.status=='unknown_send_result'
        assert db.scalar(select(SentAck)).send_result=='unknown'
        assert not db.get(Worker,w['id']).inflight_flow_state
        assert db.get(Worker,w['id']).run_status=='faulted'
        assert not db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.sender_role=='self'))
    assert all(f.read_bytes()==raw for f,raw in originals.items())
    assert not any(path=='/SendTextMsg' for path,*_ in server.requests)
    receipts=[json.loads(f.read_bytes()) for f in (d.private_root/'history-receipts').glob('*.json')]
    assert receipts and all(r['recovery_settlement_allowed'] and not r['history_ready'] for r in receipts)
    assert list((d.private_root/'history-captures').glob('*.json'))
    # Lossless raw capture still has server ID 0, not a fabricated valid ID.
    captures=[json.loads(json.loads(f.read_bytes())['raw_utf8']) for f in (d.private_root/'history-captures').glob('*.json')]
    assert any(str(row.get('MsgSvrID'))=='0' for capture in captures for row in capture.get('data',[]) if isinstance(row,dict))


def test_restoring_old_zero_rejection_blocks_real_chain_before_any_ack(
        history_lab,identity_client,connection_factory,tmp_path,monkeypatch):
    monkeypatch.setattr(rules,'unresolved_outgoing',lambda row:False)
    with pytest.raises(ValueError,match='HOOK_HISTORY_MESSAGE_ID_UNRESOLVED'):
        test_zero_querydb_actual_runtime_through_deadline_unknown_ack_and_flow_finish(
            history_lab,identity_client,connection_factory,tmp_path,monkeypatch,False)
    with SessionLocal() as db:
        assert db.scalar(select(SentAck)) is None
        assert db.scalar(select(Worker)).inflight_flow_state
    assert not list((tmp_path/'retained/send-receipts').glob('*.json'))
    assert not any(path=='/SendTextMsg' for path,*_ in history_lab[0].requests)
