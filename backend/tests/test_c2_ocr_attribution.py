"""Shared OCR semantics through real HTTP/PostgreSQL and Worker/SQLite.

Desktop frames and the model response are controlled. The sent-action case
runs production generation, claim/send, sent_ack, receipt creation and read
attribution; it never inserts a sent action or manufactures a local receipt.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows, headers
from test_wechat_c2_api import _v3_message, _v3_ingest_payload, _simulate_worker_incremental_filter
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from app.services.message_contract import reply_text_hash
from app.api.routes import wechat as wechat_routes

ROOT = Path(__file__).resolve().parents[2]
OLD = '这台混动 车型售价12.8万，欢迎继续了解。'
OCR = '这台混动车型售价12.8万，欢迎继\n续了解。'
QUESTION = '请问还有白色的吗？'


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@pytest.mark.parametrize('change,accepted', [
    ('unchanged', True), ('space', True), ('newline', True),
    ('price', False), ('decimal', False), ('body', False),
    ('role', False), ('source', False), ('other_conversation', False),
    ('failed_saved_voice', False), ('failed_observation', False),
])
def test_historical_voice_ocr_and_protected_facts(http_api, tmp_path, change, accepted):
    worker, rows = fixture_rows(); row = rows[0]
    binding = {'id': row['binding_id'], 'conversation_id': row['conversation_id'], 'rpa_session_key': 'test-0'}
    endpoint = f"/api/workers/{worker['id']}/wechat/messages/ingest"
    def frame(run, text):
        messages = [_v3_message('history-voice', role='self', message_type='voice', content=text, screen_order=1)]
        if run == 'second':
            messages.append(_v3_message('new-question', role='customer', message_type='text', content=QUESTION, screen_order=2))
        return _v3_ingest_payload(binding, 'CJ3N95EU', read_run_id=run, messages=messages)
    seed = http_api.post(endpoint, json=frame('first', OLD), headers=headers(worker))
    assert seed.status_code == 200, seed.text
    text = {'space': OLD.replace('混动 车型', '混动车型'), 'newline': OCR,
            'price': OLD.replace('12.8', '13.8'), 'decimal': OLD.replace('12.8', '128'),
            'body': '完全不同的语音正文', 'failed_saved_voice': OCR, 'failed_observation': OCR}.get(change, OLD)
    payload = _simulate_worker_incremental_filter(frame('second', text), keep_source_keys={'new-question'})
    slot = payload['evidence']['slot_ledger_states'][0]
    slot['origin_read_run_id'] = 'first'
    observed = payload['evidence']['observations'][0]
    if change == 'role': observed['sender_role'] = 'customer'
    if change == 'source': slot['source_message_key'] = 'unknown-source'
    if change == 'failed_observation':
        observed.update(item_state='failed', error_code='VOICE_TRANSCRIBE_FAILED', reason_detail='controlled failure')
    with SessionLocal() as db:
        old = db.scalar(select(MessageEvent).where(MessageEvent.source_message_key == 'history-voice'))
        if change == 'other_conversation': old.conversation_id = rows[1]['conversation_id']
        if change == 'failed_saved_voice': old.item_state = 'failed'; old.error_code = 'VOICE_TRANSCRIBE_FAILED'; old.content = None
        db.commit()
    response = http_api.post(endpoint, json=payload, headers=headers(worker))
    with SessionLocal() as db:
        events = list(db.scalars(select(MessageEvent)))
        evidence = {'change': change, 'request': payload, 'status': response.status_code, 'response': response.json(),
                    'messages': [{'key': m.source_message_key, 'text': m.content, 'state': m.item_state} for m in events]}
    dump(tmp_path/'evidence.json', evidence)
    assert response.status_code == (200 if accepted else 409), evidence
    if accepted:
        assert [m['text'] for m in evidence['messages'] if m['key'] == 'history-voice'] == [OLD]
        assert [m['text'] for m in evidence['messages'] if m['key'] == 'new-question'] == [QUESTION]
    else:
        assert not any(m['key'] == 'new-question' for m in evidence['messages'])


@pytest.mark.parametrize('variant,expected', [
    ('exact', 'ai'), ('ocr', 'ai'), ('space', 'ai'),
    ('pending_ack', 'ai_pending_ack'), ('unreconciled', 'ai_unreconciled'),
    ('wrong_hash', 'ai_identity_unconfirmed_guard'), ('wrong_source', 'ai_identity_unconfirmed_guard'),
    ('missing_stable_id', 'ai_identity_unconfirmed_guard'), ('wrong_time', 'ai_identity_unconfirmed_guard'),
    ('wrong_action', 'ai_identity_unconfirmed_guard'), ('wrong_conversation', 'ai_identity_unconfirmed_guard'),
    ('price', 'ai_identity_unconfirmed_guard'), ('decimal', 'ai_identity_unconfirmed_guard'),
    ('corrupted_original', 'ai_identity_unconfirmed_guard'), ('non_text', 'ai_identity_unconfirmed_guard'),
    ('empty_receipt', 'ai_identity_unconfirmed_guard'), ('malformed_receipt', 'ai_identity_unconfirmed_guard'),
    ('human_no_action', 'human'), ('human_same_text', 'human'),
])
def test_verified_original_receipt_separate_from_ocr(http_api, tmp_path, variant, expected):
    # These are focused attribution guards with persisted send fixtures; the
    # production send/ack chain is independently exercised by the test below.
    worker, rows = fixture_rows(); row = rows[0]; now = utcnow()
    binding = {'id': row['binding_id'], 'conversation_id': row['conversation_id'], 'rpa_session_key': 'test-0'}
    open_handoff = variant in {'wrong_hash', 'wrong_source'}
    with SessionLocal() as db:
        conv = db.get(Conversation, row['conversation_id']); conv.status = 'waiting_sales_reply' if open_handoff else 'waiting_user_reply'
        if not open_handoff:
            for handoff in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id == row['conversation_id'])):
                handoff.deleted_at = now
        action = ReplyAction(batch_id='synthetic-attribution-batch', conversation_id=rows[1 if variant == 'wrong_conversation' else 0]['conversation_id'],
            status='sending' if variant == 'pending_ack' else 'unknown_send_result' if variant == 'unreconciled' else 'sent',
            current=False, generation_no=1, decision='send_reply', reply_text=OLD, reply_text_hash=reply_text_hash(OLD),
            sent_at=now, sending_claimed_at=now)
        if variant not in {'human_no_action', 'human_same_text'}:
            db.add(action); db.flush()
        action_id = action.id
        if variant == 'corrupted_original': action.reply_text = OCR  # Stored hash must still authenticate the original.
        db.commit()
    receipt = {'reply_action_id': action_id, 'reply_text_hash': reply_text_hash(OLD), 'worker_stable_id': 'worker-message-1',
               'source_message_key': 'self-key', 'confirmed_at': now.isoformat()}
    if variant == 'wrong_hash': receipt['reply_text_hash'] = 'f'*64
    if variant == 'wrong_source': receipt['source_message_key'] = 'other-key'
    if variant == 'missing_stable_id': receipt.pop('worker_stable_id')
    if variant == 'wrong_time': receipt['confirmed_at'] = (now-timedelta(days=1)).isoformat()
    if variant == 'wrong_action': receipt['reply_action_id'] = 'does-not-exist'
    if variant == 'unreconciled': receipt['reconciliation_state'] = 'ai_unreconciled'
    content = OLD if variant in {'exact', 'human_same_text'} else OLD.replace('混动 车型','混动车型') if variant == 'space' else OCR
    if variant == 'price': content = OLD.replace('12.8', '13.8')
    if variant == 'decimal': content = OLD.replace('12.8', '128')
    if variant == 'human_no_action': content = '我是销售，这次由我联系您。'
    raw = {} if variant.startswith('human_') else {'ai_reply_receipt': {} if variant == 'empty_receipt' else 'invalid' if variant == 'malformed_receipt' else receipt}
    message = _v3_message('self-key', role='self', message_type='voice' if variant == 'non_text' else 'text', content=content, screen_order=1, raw_extra=raw)
    response = http_api.post(f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(binding,'CJ3N95EU',read_run_id='attribution-read',messages=[message],
            read_reason='waiting_sales_reply' if open_handoff else 'waiting_user_reply'),headers=headers(worker))
    assert response.status_code == 200, response.text
    assert response.json()['data']['state_transition_applied'] is True, response.text
    with SessionLocal() as db:
        event = db.scalar(select(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id']))
        conv = db.get(Conversation, row['conversation_id'])
        handoff = db.scalar(select(HandoffEvent).where(HandoffEvent.conversation_id==row['conversation_id']))
        evidence = {'variant':variant,'source':event.raw_payload.get('sender_source'),'action_id':event.raw_payload.get('ai_reply_action_id'),
            'state':conv.status,'last_sales_reply_at':conv.last_sales_reply_at,'handoff_closed_at':handoff.closed_at,
            'validation':event.raw_payload.get('ai_reply_receipt_validation'),'content':event.content,'response':response.json()}
    dump(tmp_path/'evidence.json',evidence)
    assert evidence['source']==expected,evidence
    if expected != 'human':
        assert evidence['last_sales_reply_at'] is None and evidence['handoff_closed_at'] is None,evidence
        assert evidence['state']==('waiting_sales_reply' if open_handoff else 'waiting_user_reply'),evidence
    if expected=='ai_identity_unconfirmed_guard':
        assert evidence['action_id'] is None and evidence['validation']=='rejected',evidence
    if expected=='human':
        assert evidence['last_sales_reply_at'] is not None,evidence
        assert evidence['action_id'] is None,evidence


WORKER = r'''
import json,os,sys
from pathlib import Path
from test_task_runner import FakeBridge,TaskRunnerTest
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import save_binding,load_binding,load_c2_state,load_runtime_control
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import build_message_observations_v3
base,w,conv,frame,phase=sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],json.loads(Path(sys.argv[4]).read_text()),sys.argv[5]
api=WorkerApiClient(base+'/api');b=load_binding() or Binding(w['id'],w['worker_token'],'followup-test',run_status='running');save_binding(b)
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'));bridge.get_messages_payloads=[frame]*15
if phase=='voice-seed':
 first={**frame,'messages':[{**m,'content':'[语音]'} if m['type']=='voice' else m for m in frame['messages']]}
 bridge.get_messages_payloads=[first,frame]
 bridge.voice_payload={'ok':True,'adapter':'mock','state':'voice_transcribe_completed','action_phase':'confirmed','business_state':'completed',
  'business_result_confirmed':True,'ui_action_performed':True,'sidecar_run_id':'controlled-voice','attempt_count':1,'quality_flags':[],
  'transcribed_messages':[{'content':os.environ['TEST_REPLY'],'sender_role':'customer'}],
  'item_action_outcomes':[{'action_phase':'confirmed','business_state':'completed','business_result_confirmed':True,'physical_anchor_keys':['old-voice']}]}
if phase=='send':
 messages=frame['messages']+[{'id':'sent-ai','type':'text','sender_role':'self','content':os.environ['TEST_REPLY']}]
 observations=build_message_observations_v3(messages,{'detected':False})
 bridge.send_payload={**bridge.send_payload,**TaskRunnerTest._confirmed_send_sidecar_result(observations=observations,confirmed_observation_id=observations[-1]['observation_id'],run_id='controlled-send')}
errors=[];exchanges=[]
r=TaskRunner(api,bridge,on_profile=lambda x:None,on_status=lambda x:None,on_step=lambda x:None,on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append);r.binding=b
original=api.session.send
def send(request,**kwargs):
 response=original(request,**kwargs)
 if any(p in request.url for p in ('messages/ingest','sent-ack','inflight-flow','claim-send')):
  exchanges.append({'url':request.url,'request':json.loads(request.body) if request.body else None,'status':response.status_code,'response':response.json()})
 return response
api.session.send=send
target=next(t for t in api.get_wechat_read_targets(b) if t.conversation_id==conv)
result=r._read_one_wechat_target(b,target,enforce_read_targets=True,wait_for_brain=(phase=='send'))
print(json.dumps({'result':result,'runtime':load_runtime_control(),'identity':load_c2_state('message_identity:'+conv),'exchanges':exchanges,'errors':errors,'sends':len(bridge.sent_replies),'reads':len(bridge.message_reads),'transcribes':len(bridge.voice_transcribes)},ensure_ascii=False,default=str))
'''


@pytest.mark.parametrize('kind',['voice','ai_sent'])
def test_worker_read_and_sent_receipt_attribution_through_http(http_api,tmp_path,monkeypatch,kind):
    worker,rows=fixture_rows();row=rows[0];conv=row['conversation_id'];calls=[];scheduled=[]
    class ControlledModel:
        def generate_reply_decision(self,**kwargs):
            calls.append('provider')
            return AIEngineDecision(decision='send_reply',reply_text=OLD,confidence=.95,guard_result='pass',evidence_refs=[],risk_flags=[],raw_payload={'adapter':'controlled'})
    monkeypatch.setattr(get_settings(),'c3_ai_adapter_mode','real')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',ControlledModel)
    original_add=BackgroundTasks.add_task
    def add(tasks,function,*args,**kwargs):
        if function is wechat_routes._generate_message_batch: scheduled.append('automatic')
        return original_add(tasks,function,*args,**kwargs)
    monkeypatch.setattr(BackgroundTasks,'add_task',add)
    with SessionLocal() as db:
        sales=Sales(sales_name='Synthetic salesperson',phone='13800009992',worker_id=worker['id'],enabled=True);db.add(sales);db.flush()
        db.get(Lead,row['lead_id']).sales_id=sales.id;db.get(WechatSessionBinding,row['binding_id']).sales_id=sales.id
        c=db.get(Conversation,conv);c.sales_id=sales.id;c.friend_state='friend_active';db.commit()
    def ready():
        with SessionLocal() as db:
            for h in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==conv)):h.deleted_at=utcnow()
            c=db.get(Conversation,conv)
            if not c.last_ai_reply_at:c.status='ai_active'
            b=db.get(WechatSessionBinding,row['binding_id']);b.unread_generation+=1;b.unread_hint=True;b.next_read_due_at=utcnow()-timedelta(seconds=1)
            db.commit()
    script=tmp_path/'worker.py';script.write_text(WORKER)
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock','CHEJIN_UI_LOCK_LEASE_SECONDS':'1','TEST_REPLY':OLD,
         'PYTHONPATH':os.pathsep.join(str(ROOT/p) for p in ('worker-client','worker-client/tests','worker-client/omniauto-rpa'))}
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    def run(phase,messages):
        path=tmp_path/(phase+'.json');dump(path,{'messages':messages,'frame_id':phase})
        p=subprocess.run([sys.executable,str(script),base,json.dumps(worker),conv,str(path),phase],env=env,text=True,capture_output=True,timeout=50)
        (tmp_path/(phase+'.stdout')).write_text(p.stdout);(tmp_path/(phase+'.stderr')).write_text(p.stderr)
        assert p.returncode==0,p.stderr
        result=json.loads(p.stdout.splitlines()[-1]);dump(tmp_path/(phase+'-evidence.json'),result)
        assert result['result']['ok'] and not result['runtime']['inflight_flow_id'],result
        return result
    if kind=='voice':
        # Historical media transfer requires two-sided stable context. A lone
        # voice without that proof must still fail the existing identity guard.
        history=[{'id':'before-voice','type':'text','sender_role':'customer','content':'您好，我想了解车型。'},
            {'id':'old-voice','type':'voice','sender_role':'customer','content':OLD,'voice_state':'transcribed','voice_anchor_stable_key':'old-voice'},
            {'id':'after-voice','type':'text','sender_role':'self','content':'请您稍等，我看看资料。'}]
        seeded=run('voice-seed',history)
        assert seeded['transcribes']==1,seeded
        ready()
        result=run('new-question',[history[0],{**history[1],'id':'seen-voice','content':OCR},history[2],{'id':'customer','type':'text','sender_role':'customer','content':QUESTION}])
        expected_calls=1
        assert result['transcribes']==0,result
    else:
        ready();history=[{'id':'customer','type':'text','sender_role':'customer','content':'您好，我想买辆车。'}]
        sent=run('send',history)
        assert sent['sends']==1 and len(sent['identity']['ai_reply_receipts'])==1,sent
        receipt=sent['identity']['ai_reply_receipts'][0]
        assert receipt['reply_text_hash']==reply_text_hash(OLD),receipt
        ready();visible=history+[{'id':'observed-ai','type':'text','sender_role':'self','content':OCR}]
        seen=run('observe',visible)
        with SessionLocal() as db:
            event=db.scalar(select(MessageEvent).where(MessageEvent.conversation_id==conv,MessageEvent.sender_role=='self'))
            c=db.get(Conversation,conv);a=db.get(ReplyAction,receipt['reply_action_id'])
            proof={'source':event.raw_payload.get('sender_source'),'action':event.raw_payload.get('ai_reply_action_id'),'status':c.status,'last_sales_reply_at':c.last_sales_reply_at,
                   'action_status':a.status,'original':a.reply_text,'hash':a.reply_text_hash,'observed':event.content}
        dump(tmp_path/'attribution-proof.json',proof)
        assert proof['source']=='ai' and proof['action']==receipt['reply_action_id'],proof
        assert proof['status']=='waiting_user_reply' and proof['last_sales_reply_at'] is None,proof
        assert proof['action_status']=='sent' and proof['original']==OLD and proof['hash']==reply_text_hash(OLD),proof
        assert seen['sends']==0,seen
        ready();result=run('new-question',visible+[{'id':'new-question','type':'text','sender_role':'customer','content':QUESTION}]);expected_calls=2
    deadline=time.monotonic()+5
    while len(calls)<expected_calls and time.monotonic()<deadline:time.sleep(.05)
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==conv)))
        tasks=list(db.scalars(select(Task).where(Task.reply_action_id.in_([a.id for a in actions]))))
        evidence={'provider_calls':len(calls),'scheduled':len(scheduled),'actions':[a.id for a in actions],'tasks':[t.id for t in tasks],
                  'last_sales_reply_at':db.get(Conversation,conv).last_sales_reply_at,'messages':[{'text':m.content,'source':m.raw_payload.get('sender_source')} for m in db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==conv))]}
    dump(tmp_path/'automatic-evidence.json',evidence)
    assert len(calls)==len(scheduled)==len(actions)==len(tasks)==expected_calls,evidence
    assert result['sends']==0,result
    if kind=='ai_sent':assert evidence['last_sales_reply_at'] is None,evidence
