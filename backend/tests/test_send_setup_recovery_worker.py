"""Real Worker subprocess/HTTP/PG/same SQLite. Desktop/model are controlled."""
import json,os,subprocess,sys,time,sqlite3,hashlib
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation,ReplyAction,SentAck,HandoffEvent
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from test_lead_followup_eligibility import isolated_db,http_api,fixture_rows
from test_pre_send_checkpoint_order import async_generation

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('mode',['normal','stop_offline','ack_offline','ack_response_lost','crash_before_stop','segmented','crash_creating','crash_finished_unknown',
                                'upgrade_ack_offline','upgrade_ack_response_lost'])
def test_original_task_settles_then_explicit_start_automatically_answers_a_and_b(http_api,tmp_path,monkeypatch,async_generation,mode):
    upgrade = mode.startswith('upgrade_')
    mode = mode.removeprefix('upgrade_')
    calls=[]
    class Model:
        def generate_reply_decision(self,**kw):
            calls.append(kw)
            if mode=='segmented' and len(calls)==1:
                from test_reply_sequence_http import PARTS
                return AIEngineDecision(decision='send_reply',reply_text=' '.join(PARTS),guard_result='pass',
                    raw_payload={'omniauto_brain_result':{'brain_plan':{'reply_segments':PARTS}}})
            return AIEngineDecision(decision='send_reply',reply_text='好的，我可以继续帮您介绍车辆。',guard_result='pass')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    if os.environ.get('SP_ABLATION')=='settlement':
        from app.services import pre_send_read_recovery
        from app.errors import AppError
        original_settle=pre_send_read_recovery.settle
        def disabled(*args,**kwargs):
            if 'pre_send_setup_failure' in json.dumps(kwargs,default=str):
                raise AppError('CONTROLLED_SETUP_SETTLEMENT_DISABLED','Controlled negative test',409)
            return original_settle(*args,**kwargs)
        monkeypatch.setattr(pre_send_read_recovery,'settle',disabled)
    w,rows=fixture_rows(eligible=True)
    with SessionLocal() as db:
        for row in rows:db.get(Conversation,row['conversation_id']).friend_state='friend_active'
        db.commit()
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock','CHEJIN_OBSERVABILITY_ENABLED':'false',
         # Let the unchanged lease expire naturally after an abrupt exit.
         # The production default is 90s; 35s is not evidence of a deadlock.
         'CHEJIN_UI_LOCK_LEASE_SECONDS':'5',
         'CHEJIN_C2_MESSAGE_READ_INTERVAL_SECONDS':'3600','CHEJIN_C2_SESSION_SCAN_INTERVAL_SECONDS':'3600'}
    def run(phase):
        command=[sys.executable,str(ROOT/'backend/tests/send_setup_worker_process.py'),base,json.dumps(w),json.dumps(rows),phase,mode]
        value=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=85)
        (tmp_path/(phase+'.stdout')).write_text(value.stdout);(tmp_path/(phase+'.stderr')).write_text(value.stderr)
        if phase=='initial' and mode in ('ack_response_lost','crash_before_stop','crash_creating','crash_finished_unknown'):
            assert value.returncode==17,value.stdout+value.stderr
            return None
        assert value.returncode==0,value.stdout[-7000:]+value.stderr[-7000:]
        return json.loads((tmp_path/(phase+'-result.json')).read_text())
    first=run('initial')
    if upgrade:
        from app.contracts import c2
        upgraded = {**c2.c2_contract_v3(), 'contract_revision': '0.9.91'}
        monkeypatch.setattr(c2, 'c2_contract_v3', lambda: upgraded)
    if mode not in ('normal','segmented'):
        settled=run('settle')
        assert settled['run_status']=='faulted' and settled['physical']==[],settled
        assert not settled['pending_ack'] and not settled['runtime'].get('inflight_flow_id'),settled
    else:
        assert first['run_status']=='faulted' and len(first['physical'])==(1 if mode=='segmented' else 0),first
        assert not first['pending_ack'] and not first['runtime'].get('inflight_flow_id'),first
    # These are real server-confirmed receipts, before the original journal
    # is deleted. Verify ownership reached the same SQLite, including unknown.
    with sqlite3.connect(tmp_path/'worker'/'worker_client.sqlite3') as local:
        local.row_factory=sqlite3.Row
        saved_acks=list(local.execute("SELECT * FROM reply_send_ack_outbox"))
    assert saved_acks
    for local_ack in saved_acks:
        assert local_ack['status']=='confirmed'
        saved_payload=json.loads(local_ack['ack_payload_json'])
        refs=saved_payload['evidence']['send_request_files']
        assert refs
        for ref in refs:
            assert hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()==ref['sha256']
    (tmp_path/'settled-file-references.json').write_text(json.dumps([dict(a) for a in saved_acks],indent=2))
    if mode in ('crash_creating','crash_finished_unknown'):
        with SessionLocal() as db:
            ack=db.scalar(select(SentAck))
            assert ack.send_result=='unknown' and ack.action_phase=='trigger_attempted'
            assert 'pre_send_setup_failure' not in ack.evidence
            assert len(calls)==1 and db.query(ReplyAction).count()==1
            assert db.query(HandoffEvent).count()==1
            assert db.get(Worker,w['id']).run_status=='faulted'
        return
    with SessionLocal() as db:
        a=db.scalar(select(ReplyAction).where(ReplyAction.status=='failed'));assert a is not None
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==a.id));assert ack.send_result=='failed'
        assert ack.evidence['pre_send_setup_failure']['process_state']=='create_failed'
        assert not db.query(HandoffEvent).count()
        saved=db.get(WechatSessionBinding,rows[0]['binding_id'])
        assert saved.last_scan_snapshot['pre_send_read_pending']['status']=='pending'
    assert len(calls)==1 and async_generation['counts']['generated']==1
    if upgrade:
        # This case covers backend upgrade with the old client's frozen
        # receipt; it does not claim a newly packaged Windows upgrade.
        assert settled['physical']==[]
        return
    if os.environ.get('SP_ABLATION')=='automatic':
        from fastapi import BackgroundTasks
        suppressed=[]
        monkeypatch.setattr(BackgroundTasks,'add_task',lambda self,*a,**kw:suppressed.append(str(a[0])))
    second=run('resume')
    with SessionLocal() as db:
        a_actions=list(db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==rows[0]['conversation_id'])))
        b_actions=list(db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==rows[1]['conversation_id'])))
        evidence={'first':first,'second':second,'a_actions':[{'id':a.id,'status':a.status} for a in a_actions],
            'b_actions':[{'id':a.id,'status':a.status} for a in b_actions],'model_calls':len(calls),'async':async_generation['counts']}
        (tmp_path/'end-to-end.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
        assert len(a_actions)==(4 if mode=='segmented' else 2) and len(b_actions)==1,{'automatic_reply_missing':evidence}
        assert sorted(a.status for a in a_actions)==(['cancelled','failed','sent','sent'] if mode=='segmented' else ['failed','sent']) and b_actions[0].status=='sent',evidence
        if mode=='segmented':
            original=db.get(ReplyAction,first['physical'][0]['action'])
            prefix_ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==original.id))
            assert original.status=='sent' and prefix_ack.send_result=='sent' and prefix_ack.action_phase=='confirmed'
            assert len([x for x in second['physical'] if x['text']==original.reply_text])==0
        assert len(second['physical'])==2 and len(calls)==3 and async_generation['counts']['generated']==3,evidence
        assert not db.query(HandoffEvent).count() and not db.get(Worker,w['id']).inflight_flow_state
