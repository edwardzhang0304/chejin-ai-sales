"""Real recovery loop/services/database; synthetic Windows/Hook/model/Feishu only.

Native batches must still be admitted by Worker; the server loop is not allowed
 to generate them merely because an earlier notification stage failed.
"""
import json
import logging
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction, SentAck
from app.models.sales import Sales
from app.models.task import Task
from app.models.worker import Worker
from app.services import c3_recovery as recovery, c3_service, feishu_service, human_reminder_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_c3_recovery_isolation import run_rounds
from test_hook_business_http import clean, post
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_human_ownership_runtime import human_runtime, add_message
from test_hook_formal_runtime import drive_reply
from test_partial_reply_recovery_provider import recovery_provider_factory
from test_hook_native_send_http import beginning
from test_human_reply_ownership import Adapter, unanswered, send_history, daytime_after


def fail_stage_sessions(monkeypatch, module, *, query=False):
    """Fail at the DB boundary; real context manager rolls back before escaping."""
    sessions = []
    @contextmanager
    def broken():
        if not query:
            raise RuntimeError('token=secret init failure')
        with SessionLocal() as db:
            sessions.append(db)
            row = db.scalar(select(Sales))
            row.sales_name = 'uncommitted poison'
            db.flush()
            raise RuntimeError('phone=13800000000 query failure')
            yield db
    monkeypatch.setattr(module, 'SessionLocal', broken)
    return sessions


@pytest.mark.parametrize('stage', ['notifications', 'reminders'])
@pytest.mark.parametrize('query', [False, True])
def test_real_send_timeout_runs_after_earlier_db_failure(monkeypatch, tmp_path, caplog, stage, query):
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    w,p,b,h,permit,value,path,now,start = beginning(monkeypatch)
    with SessionLocal() as db:
        action = db.get(ReplyAction, permit['reply_action_id'])
        original = json.loads(json.dumps(action.ai_payload))
        claimed_at = action.sending_claimed_at
        sales = db.scalar(select(Sales)); sales_id, sales_name = sales.id, sales.sales_name
        sales.feishu_user_id = 'ou_synthetic_recovery'
        db.commit()
    adapter = Adapter()
    monkeypatch.setattr(feishu_service, 'get_feishu_adapter', lambda:adapter)
    target = feishu_service if stage == 'notifications' else human_reminder_service
    # Actual four implementations, only one stage's session boundary fails.
    fail_stage_sessions(monkeypatch, target, query=query)
    clock = claimed_at + timedelta(seconds=get_settings().c3_send_ack_stale_after_seconds + 1)
    monkeypatch.setattr(c3_service, 'utcnow', lambda: clock)
    run_rounds(2)  # Each call is a complete production _run pass.
    with SessionLocal() as db:
        action = db.get(ReplyAction, permit['reply_action_id'])
        task = db.get(Task, permit['task_id'])
        handoff = db.scalar(select(HandoffEvent).where(HandoffEvent.conversation_id == b['conversation_id']))
        assert action.status == 'unknown_send_result' and task.status == 'failed'
        assert action.ai_payload['native_send_identity'] == original['native_send_identity']
        assert action.ai_payload['native_send_attempt'] == original['native_send_attempt']
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id == action.id)) is None
        assert handoff and handoff.handoff_reason_code == 'SEND_ACK_TIMEOUT'
        assert db.get(Sales, sales_id).sales_name == sales_name
        assert db.get(Worker,w['id']).current_task is None
        evidence = {'action_id': action.id, 'task_id': task.id, 'conversation_id': b['conversation_id'],
                    'handoff_id': handoff.id, 'action_status': action.status, 'task_status':task.status,
                    'original_identity_preserved': True, 'session_fault': stage, 'query': query}
    monkeypatch.setattr(target, 'SessionLocal', SessionLocal)
    run_rounds(2)
    with SessionLocal() as db:
        assert db.get(HandoffEvent,evidence['handoff_id']).notify_status == 'succeeded'
        assert len(adapter.calls) == 1
        evidence['notification_after_fault_cleared'] = 'succeeded_once'
    assert 'error_type=RuntimeError' in caplog.text
    assert 'completed stage=reply_sends' in caplog.text
    assert 'token=secret' not in caplog.text and '13800000000' not in caplog.text
    (tmp_path/'timeout-chain.json').write_text(json.dumps(evidence, indent=2))
    (tmp_path/'server-recovery.log').write_text(caplog.text)


@pytest.mark.parametrize('mode', ['sent', 'unknown'])
def test_worker_history_brain_send_and_timeout_with_notification_fault(
        human_runtime, monkeypatch, recovery_provider_factory, tmp_path, caplog, mode):
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    runtime,binding,table,server,w = human_runtime
    data_root = tmp_path/'worker-data'
    runtime = type(runtime)(runtime.client,runtime.credentials,runtime.gateway,
                            runtime.prepared,data_root/'transactions/hook')
    root = str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT', root)
    monkeypatch.setattr(get_settings(), 'c3_omniauto_root', root)
    requests = recovery_provider_factory(['您好，先按您的预算了解车辆情况，您比较关注轿车还是SUV？我可以先帮您梳理关注的配置，再结合实际车源逐项核实，确认清楚之后再为您介绍。',
        '车辆的具体车况和价格需要以实际资料为准，您也可以先告诉我平时的用车场景，我会把能确认的情况和仍需核实的信息分开说明，方便您进行比较。'])
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', RealOmniAutoAIEngineAdapter)
    add_message(table, sender=0, text='想了解五万元以内的车，先看看不预约')
    synced = runtime.sync(binding)
    bid = synced['message_batch']['batch_id']
    fail_stage_sessions(monkeypatch, feishu_service)
    run_rounds()
    with SessionLocal() as db:
        batch = db.get(MessageBatch, bid)
        assert batch.generation_attempt_count == 0  # Server must not bypass Worker admission.
    if mode == 'unknown':
        original_request = server.on_request
        def request(path):
            if path == '/SendTextMsg':
                # Hook call accepted; no reliable outgoing record ever returned.
                server.raw_override = b'{"SendTextMsg":"1"}'
            else:
                original_request(path)
        server.on_request = request
    sent = drive_reply(runtime, binding, bid)
    with SessionLocal() as db:
        actions = list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id == bid).order_by(ReplyAction.segment_index)))
        first = actions[0]
        aid, tid = first.id, first.claimed_task_id
        original_identity = first.ai_payload['native_send_identity']
        claimed_at = first.sending_claimed_at
    before_calls = len([r for r in server.requests if r[0] == '/SendTextMsg'])
    if mode == 'unknown':
        assert before_calls == 1 and not sent['sends']
        run_rounds()  # Not expired yet; do not decide unknown early.
        with SessionLocal() as db:
            assert db.get(ReplyAction, aid).status == 'sending'
        clock = claimed_at + timedelta(seconds=get_settings().c3_send_ack_stale_after_seconds + 1)
        monkeypatch.setattr(c3_service, 'utcnow', lambda:clock)
    run_rounds(2)
    run_rounds()  # New loop instance represents scheduler restart, same durable rows.
    with SessionLocal() as db:
        first = db.get(ReplyAction, aid); task = db.get(Task, tid)
        states = [a.status for a in db.scalars(select(ReplyAction).where(ReplyAction.batch_id == bid).order_by(ReplyAction.segment_index))]
        assert first.ai_payload['native_send_identity'] == original_identity
        ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == aid))
        if mode == 'sent':
            assert all(s == 'sent' for s in states) and task.status == 'completed' and ack.send_result == 'sent'
            assert len(sent['sends']) == 2
        else:
            assert states == ['unknown_send_result', 'cancelled'] and task.status == 'failed' and ack is None
            assert db.get(Conversation, binding['conversation_id']).reply_ownership['owner'] == 'human'
        evidence = {'mode': mode, 'batch_id': bid, 'action_id': aid, 'task_id': tid,
            'conversation_id': binding['conversation_id'], 'states': states, 'task_status': task.status,
            'ack_id': ack.id if ack else None, 'send_calls': before_calls,
            'provider_models': [r['model'] for r in requests]}
    assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == before_calls
    assert [r['model'] for r in requests] == ['recovery-author', 'recovery-reviewer']
    intents = list((runtime.private_root/'sends').glob('*/*/*/intent.json'))
    assert intents
    assert all(.5 <= json.loads(p.read_text())['delay_seconds'] <= 3 for p in intents)
    assert 'completed stage=reply_sends' in caplog.text
    # Export only actual Worker-generated records, through the same collector
    # used by desktop diagnosis. Server exception evidence stays server-side.
    from chejin_worker_client.diagnostic_bundle import export_bundle
    import hashlib, zipfile
    before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime.private_root.rglob('*') if p.is_file()}
    bundle = export_bundle(data_root,tmp_path/'actual-worker-evidence.zip')
    with zipfile.ZipFile(bundle) as archive:
        members=archive.namelist()
        assert any('/sends/' in name and name.endswith('/intent.json') for name in members)
        assert any('/history-receipts/' in name for name in members)
        assert any(aid in name for name in members)
        assert w['worker_token'].encode() not in b''.join(archive.read(n) for n in members)
    assert before == {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime.private_root.rglob('*') if p.is_file()}
    evidence['export_members']=members
    (tmp_path/'worker-recovery-chain.json').write_text(json.dumps(evidence, indent=2))
    (tmp_path/'server-recovery.log').write_text(caplog.text)


def test_actual_reminder_recovers_despite_notification_query_failure(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    w,p,b,value,rid,start = unanswered(monkeypatch)
    now = daytime_after(start)
    monkeypatch.setattr(human_reminder_service, 'utcnow', lambda:now)
    send_history(w,p,b,value,monkeypatch,now)
    adapter = Adapter()
    monkeypatch.setattr(human_reminder_service, 'get_feishu_adapter', lambda:adapter)
    fail_stage_sessions(monkeypatch, feishu_service, query=True)
    run_rounds(2)
    from app.models.c3 import HumanReplyReminder
    with SessionLocal() as db:
        row = db.get(HumanReplyReminder,rid)
        assert row.status == 'succeeded' and row.attempt_count == 1
        assert len(adapter.calls) == 1
        assert (human_reminder_service.aware(row.next_attempt_at) - start).total_seconds() == 3600
        evidence = {'reminder_id':rid,'conversation_id':b['conversation_id'], 'status':row.status,
            'attempts':row.attempts,'scheduled_seconds':3600}
    (tmp_path/'reminder-chain.json').write_text(json.dumps(evidence,indent=2))
    (tmp_path/'server-recovery.log').write_text(caplog.text)


def test_human_recall_ai_then_human_reminder_under_stage_failure(
        human_runtime, monkeypatch, recovery_provider_factory, tmp_path, caplog):
    """Native producer/consumer chain; shorten recall clock, advance reminder clock.

    No pending/owner/task/reminder state is inserted or reset by this test.
    """
    import time
    from datetime import datetime, timezone
    from app.models.c3 import HumanReplyReminder
    from app.services import hook_history_service
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    runtime,binding,table,server,w = human_runtime
    root = str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',root)
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',root)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    provider = recovery_provider_factory('您好，可以先聊聊您比较关注的车型和用车需求，我再帮您逐项核实车辆资料。')
    fail_stage_sessions(monkeypatch, feishu_service)
    add_message(table,sender=0,text='请帮我看看车辆')
    add_message(table,sender=1,text='我来处理这个客户')
    assert runtime.sync(binding)['message_batch'] is None
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        assert conv.reply_ownership['owner']=='human'
        due=conv.next_recall_at.replace(tzinfo=timezone.utc)
    time.sleep(max(0,(due-datetime.now(timezone.utc)).total_seconds())+.05)
    recall=runtime.sync(binding);bid=recall['message_batch']['batch_id']
    run_rounds()
    result=drive_reply(runtime,binding,bid)
    assert len(result['sends'])==1 and result['sends'][0]['local_send_observed']
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        assert conv.reply_ownership['owner']=='ai' and conv.recall_count==1
    add_message(table,sender=0,text='我想先了解十五万以内的轿车')
    incoming=runtime.sync(binding);next_bid=incoming['message_batch']['batch_id']
    run_rounds()
    reply=drive_reply(runtime,binding,next_bid)
    assert len(reply['sends'])==1 and reply['sends'][0]['local_send_observed']
    add_message(table,sender=1,text='我接着给您介绍')
    runtime.sync(binding)
    add_message(table,sender=0,text='那请再详细说说')
    assert runtime.sync(binding)['message_batch'] is None
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        row=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.closed_at.is_(None)))
        assert conv.reply_ownership['owner']=='human'
        rid=row.id;start=human_reminder_service.aware(row.started_at)
        assert (human_reminder_service.aware(row.next_attempt_at)-start).total_seconds()==3600
        db.get(Sales,conv.sales_id).feishu_user_id='ou_synthetic_recovery_chain'
        db.commit()  # Test recipient configuration only, not a state transition.
    future=daytime_after(start)
    monkeypatch.setattr(runtime.clock,'now',lambda:future)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:future)
    monkeypatch.setattr(human_reminder_service,'utcnow',lambda:future)
    # Real synchronizer produces fresh complete coverage; no hand-built receipt.
    assert runtime.sync(binding)['history_ready']
    adapter=Adapter()
    monkeypatch.setattr(human_reminder_service,'get_feishu_adapter',lambda:adapter)
    run_rounds(2)
    with SessionLocal() as db:
        row=db.get(HumanReplyReminder,rid)
        assert row.status=='succeeded' and row.attempt_count==1 and len(adapter.calls)==1
        assert db.get(Conversation,binding['conversation_id']).reply_ownership['owner']=='human'
        evidence={'conversation_id':binding['conversation_id'],'recall_batch_id':bid,
            'customer_reply_batch_id':next_bid,'reminder_id':rid,'reminder_status':row.status,
            'provider_models':[r['model'] for r in provider],'send_calls':2,
            'recall_clock_seconds':get_settings().c3_recall_after_hours*3600,'reminder_seconds':3600}
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==2
    (tmp_path/'ownership-recovery-chain.json').write_text(json.dumps(evidence,indent=2))
    (tmp_path/'server-recovery.log').write_text(caplog.text)


@pytest.mark.parametrize('failed_stage', ['notifications','reminders'])
def test_persisted_provider_failure_recovers_via_real_loop(monkeypatch, recovery_provider_factory, tmp_path, caplog, failed_stage):
    """Legacy durable batch producer; no post-production state reset/seed."""
    from app.errors import AppError
    from test_c3_api import _setup_bound_conversation, _ingest, _collect, _generate
    caplog.set_level(logging.INFO,logger=recovery.__name__)
    class FailedProvider:
        def generate_reply_decision(self, **kwargs):
            raise AppError('AI_ENGINE_PROVIDER_FAILED','synthetic provider down',503)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',FailedProvider)
    w,b=_setup_bound_conversation()
    mid=_ingest(w,b['conversation_id'],'recovery-stage-input','请介绍一下适合家用的车')
    batch=_collect(b['conversation_id'],mid)
    with SessionLocal() as db:
        row=db.get(MessageBatch,batch['batch_id'])
        initial=row.status
    if initial=='collecting':
        _generate(batch['batch_id'])
    with SessionLocal() as db:
        row=db.get(MessageBatch,batch['batch_id'])
        assert row.status=='retry_wait' and row.retryable
        bid=row.id;attempt=row.generation_attempt_count
        due=row.generated_at+timedelta(seconds=get_settings().c3_batch_retry_delay_seconds+1)
    fail_stage_sessions(monkeypatch,feishu_service if failed_stage=='notifications' else human_reminder_service)
    root=str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',root)
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',root)
    provider=recovery_provider_factory('可以先了解您的用车需求和预算，再核实合适的车辆资料。')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    monkeypatch.setattr(c3_service,'utcnow',lambda:due)
    run_rounds(2)
    with SessionLocal() as db:
        row=db.get(MessageBatch,bid)
        action=db.scalar(select(ReplyAction).where(ReplyAction.batch_id==bid))
        assert row.status=='reply_action_created' and row.generation_attempt_count==attempt+1
        assert action.status=='queued'
        task=db.scalar(select(Task).where(Task.reply_action_id==action.id))
        assert task and task.status=='pending'
        evidence={'message_id':mid,'batch_id':bid,'action_id':action.id,'state':row.status,
            'task_id':task.id,'attempts_before':attempt,'attempts_after':row.generation_attempt_count,'stage_fault':failed_stage}
    assert [r['model'] for r in provider]==['recovery-author','recovery-reviewer']
    (tmp_path/'durable-batch-chain.json').write_text(json.dumps(evidence,indent=2))
    (tmp_path/'server-recovery.log').write_text(caplog.text)


def test_item_rollback_preserves_pending_batch_and_safe_id_evidence(monkeypatch,tmp_path,caplog):
    from test_hook_history_business_http import bound,coverage,upload
    caplog.set_level(logging.INFO,logger=recovery.__name__)
    w,t,p,b=bound();response,_=upload(w,p,b,coverage(b),monkeypatch)
    bid=response.json()['data']['message_batch']['batch_id']
    original_claim=c3_service.claim_message_batch_generation
    def broken_claim(db,*,batch_id,**kwargs):
        row=db.get(MessageBatch,batch_id)
        row.status='uncommitted poison'
        db.flush()
        raise RuntimeError('token=secret item failure')
    monkeypatch.setattr(c3_service,'claim_message_batch_generation',broken_claim)
    run_rounds()
    with SessionLocal() as db:
        row=db.get(MessageBatch,bid)
        assert row.status=='collecting' and row.generation_attempt_count==0
    assert bid in caplog.text and 'completed stage=reply_sends' in caplog.text
    assert 'token=secret' not in caplog.text
    monkeypatch.setattr(c3_service,'claim_message_batch_generation',original_claim)
    run_rounds()
    # Native admission must still wait for Worker after the DB transaction heals.
    with SessionLocal() as db:
        assert db.get(MessageBatch,bid).generation_attempt_count==0
    (tmp_path/'item-rollback.log').write_text(caplog.text)
