"""HC through a real Worker process, SQLite, socket HTTP and PostgreSQL.

Only captured frames, physical sends and the external model are controlled.
These constructed frames are separate from the original PNG replay.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
import test_c3_api as api
from test_pre_send_checkpoint_order import async_generation
from test_lead_followup_eligibility import http_api, isolated_db
from test_reply_sequence_worker import WORKER
from test_reply_sequence_http import SequenceModel, PARTS
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision


FRAME_IO = r'''
 def sidecar_active(self):
  return False
 def get_messages(self,**kwargs):
  self.capture_count=getattr(self,'capture_count',0)+1
  frame_id=os.environ['HC_RUN']+'-physical-'+str(self.capture_count)
  payload=self._contractual_message_payload({'messages':copy.deepcopy(self.messages),'tail_complete':True})
  payload.update(sidecar_run_id=frame_id,frame_observation={'frame_id':frame_id})
  self.get_messages_payloads=[payload]
  return FakeBridge.get_messages(self,**kwargs)
'''

TYPING_IO = r'''
  if mode=='hc_typing' and os.environ['HC_RUN']=='continued' and not getattr(self,'hc_interrupted',False):
   from test_task_runner import production_sidecar_module
   self.hc_interrupted=True
   self.messages[1]['content']='这款600Pro适合日常通勤，具体信息可以再看看。'
   self.messages.append({'id':'customer-interruption','sender_role':'customer','type':'text','content':'改成下周再联系我'})
   frame=self.get_messages(display_name='C3TEST01',rpa_session_key='')
   old=kwargs['expected_context_guard'];current=frame['send_context_guard']
   check=production_sidecar_module().validate_send_context_guard(old,current,current_observations=frame['observations'])
   assert check['worker_continuity_decision']['relation']=='unique_tail_append',check
   validation={'ok':True,'confirmed_target':'C3TEST01','conversation_type':'private'}
   snapshot={'ok':True,'validation':validation,'observations':frame['observations'],
    'message_sequence':[{'observation_id':o['observation_id'],'sender_role':o['sender_role']} for o in frame['observations']],
    'send_context_guard':current,'frame_observation':frame['frame_observation']}
   check.update(snapshot=snapshot,frame_observation=snapshot['frame_observation'])
   visual={'physical_send_triggered':False,'error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','context_check':check,
    'draft_clear':{'ok':True,'clear_attempted':True,'method':'select_all_backspace',
     'reason':'confirmed_program_draft_clear_requested',
     'focus_check':{'ok':True,'expected_length':len(kwargs['text']),'observed_length':len(kwargs['text'])}}}
   return {'ok':False,'state':'send_input_not_ready','error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND',
    'action_phase':'not_attempted','physical_send_triggered':False,
    'guard':{'ok':True,'confirmed_target':'C3TEST01','conversation_type':'private',
     'send_baseline':{'send_context_guard':old},'visual':visual}}
'''


def worker_source():
    start = WORKER.index(' def get_messages(self,**kwargs):')
    end = WORKER.index(' def send_reply(self,**kwargs):',start)
    media_io = WORKER[start:end].replace('return super().get_messages(**kwargs)', 'return FakeBridge.get_messages(self,**kwargs)')
    media_io = media_io.replace("bubble_rect=[100,500,300,650]","bubble_rect=[100,100+150*(len(self.messages)-1),300,240+150*(len(self.messages)-1)]")
    media_io = media_io.replace("  self.get_messages_payloads=[payload]", "  self.capture_count=getattr(self,'capture_count',0)+1\n  payload.update(sidecar_run_id='media-frame-'+str(self.capture_count),frame_observation={'frame_id':'media-frame-'+str(self.capture_count)})\n  self.get_messages_payloads=[payload]")
    value = WORKER[:start]+FRAME_IO+WORKER[end:]
    if os.environ.get('HC_TEST_MEDIA'):
        value = WORKER[:start]+' def sidecar_active(self):return False\n'+media_io+WORKER[end:]

    value = value.replace('bridge=Wechat()', "bridge=Wechat()\nbridge.messages=json.loads(Path(os.environ['HC_VISIBLE']).read_text())")
    value = value.replace("  assert not kwargs['cancel_check']()", "  assert not kwargs['cancel_check']()\n"+TYPING_IO)
    value = value.replace('runner.binding=binding', '''runner.binding=binding
# Controlled optional-service readiness, not a real credential or model call.
# Recovery has this environment preflight; the original task/send gates remain.
from chejin_worker_client import omniauto_vision
omniauto_vision.vision_configuration_status=lambda:{'ready':True,'config':{}}
if os.environ.get('HC_DISABLE')=='1':
 from apps.wechat_ai_customer_service.adapters import historical_text_alignment
 historical_text_alignment.build_correspondence=lambda *args,**kwargs:None
''')
    value = value.replace(' response=original(request,**kwargs)', '''
 if os.environ.get('HC_TEST_MEDIA') and os.environ['HC_RUN']=='continued' and request.url.endswith('/messages/ingest'):
  body=json.loads(request.body)
  if any(m.get('message_type') in {'voice','image'} for m in body.get('messages',[])) and (body['evidence'].get('sequence_alignment_evidence') or {}).get('text_correspondence'):
   negatives=[]
   damages=('parent','receipt','state')+ (('new_text_omitted','new_text_identity','new_text_historical') if mode=='normal' else ())
   for damage in damages:
    damaged=copy.deepcopy(body)
    item=next(m for m in damaged['messages'] if m.get('message_type') in {'voice','image'})
    # Alter the original authoritative contract, not a duplicate diagnostic
    # field in message_identity_commit_record that the API does not consume.
    if damage.startswith('new_text_'):
     text=next(m for m in damaged['messages'] if m['message_type']=='text' and m['sender_role_hint']=='customer')
     oid=text['raw_payload']['observation']['observation_id']
     if damage=='new_text_omitted':damaged['messages'].remove(text)
     if damage=='new_text_identity':
      mapped=next(p for p in damaged['evidence']['sequence_alignment_evidence']['matched_pairs'] if p['post_observation_id']==oid)
      mapped['worker_stable_id']='worker-message-1'
     if damage=='new_text_historical':
      slot=next(p for p in damaged['evidence']['slot_ledger_states'] if p['observation_id']==oid)
      slot.update(fact_scope='historical',delivery_state='backend_confirmed')
    elif item['message_type']=='voice':
     voice=damaged['evidence']['voice_transcription']
     if damage=='parent':voice['reserved_worker_stable_id']='wrong-parent-id'
     if damage=='receipt':voice['action_result_receipt']['post_observation_id']='another-media'
     if damage=='state':voice['action_phase']='failed'
    else:
     if damage=='parent':item['raw_payload']['source_message_key']='another-image-source'
     if damage=='receipt':item['raw_payload']['customer_image_understanding']['adoptable']=False
     if damage=='state':item['item_state']='processing'
    changed=request.copy()
    changed.prepare_body(data=None,files=None,json=damaged)
    denied=original(changed,**kwargs)
    negatives.append({'damage':damage,'status':denied.status_code,'body':denied.json()})
    Path(__file__).with_name('media-proof-negatives.json').write_text(json.dumps(negatives))
    assert denied.status_code in (400,409,422),negatives
 response=original(request,**kwargs)
 if os.environ.get('HC_MEDIA_LOSS')=='1' and os.environ['HC_RUN']=='continued' and request.url.endswith('/messages/ingest') and any(m.get('message_type') in {'voice','image'} for m in json.loads(request.body).get('messages',[])):
  raise requests.ConnectionError('controlled loss after completed media was committed')
 if mode=='hc_loss' and os.environ['HC_RUN']=='continued' and request.url.endswith('/messages/ingest'):
  raise requests.ConnectionError('controlled loss after server committed HC ingest')
''')
    value = value.replace(" out={'result':result,'sent':", """
 if mode=='hc_typing' and result.get('brain_result',{}).get('customer_interrupted'):
  # A single reply resumes through the server's ordinary scheduled reader.
  # Do not create a task, call generation, or manufacture a replacement ACK.
  runner.start(binding)
  until=time.monotonic()+15
  while not runner._can_start_new_flow(binding) and time.monotonic()<until:time.sleep(.05)
  assert runner._can_start_new_flow(binding), {'runtime':load_runtime_control(),'errors':errors}
  scheduled=runner._fetch_read_targets(binding)
  assert any(t.conversation_id==conversation_id for t in scheduled)
  runner._read_state_target_queue(binding,targets=scheduled)
  until=time.monotonic()+20
  while time.monotonic()<until:
   if bridge.sent_replies and not load_runtime_control()['inflight_flow_id']:break
   time.sleep(.05)
  runner.stop_event.set()
  for thread in (runner.thread,runner.c2_thread,runner.thread_monitor):
   if thread:thread.join(5)
 out={'result':result,'sent':""")
    value = value.replace("  started=runner.set_run_status('running')", "  until=time.monotonic()+15\n  while os.environ.get('HC_MEDIA_LOSS') and (has_pending_c2_outbox() or load_runtime_control()['inflight_flow_id']) and time.monotonic()<until:time.sleep(.1)\n  started=runner.set_run_status('running')")
    value = value.replace("if not load_runtime_control()['inflight_flow_id']: break",
        "if bridge.sent_replies and not load_runtime_control()['inflight_flow_id']: break")
    # Receive physical confirmation for the same captured conversation. All
    # tickets, intents, claims, ACKs and Flow settlement remain production code.
    if os.environ.get('HC_TEST_MEDIA'):
        media=os.environ['HC_TEST_MEDIA']
        value=value.replace("{'id':'customer-interruption','sender_role':'customer','type':'text','content':'改成下周再联系我'}",
            repr({'id':'customer-interruption','sender_role':'customer','type':media,'content':'[语音]' if media=='voice' else '[图片]','voice_duration':5}))
        # These assertions belonged to the old segmented-only desktop fixture.
        # The real cancellation state is checked below for single AND segments.
        value=value.replace("  group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))\n  assert group['reply_sequence']['terminal'] is True,group\n  assert len(self.sent_replies)==1\n", '')
        value=value.replace("  if mode!='voice_full':", "  if mode not in {'voice_full','hc_typing','normal'}:")
        value=value.replace("if mode=='image_full' or", "if os.environ.get('HC_TEST_MEDIA')=='image' or mode=='image_full' or")
        value=value.replace("  group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))\n  assert group['reply_sequence']['terminal'] is True\n", '')
        value=value.replace("  self.voice_payload={'ok':True", "  Path(__file__).with_name('visible.json').write_text(json.dumps(self.messages))\n  self.voice_payload={'ok':True")
        value=value.replace(" def execute_voice_action(self,**kwargs):", " def execute_voice_action(self,**kwargs):\n  self.voice_execute_calls=getattr(self,'voice_execute_calls',0)+1")
        value=value.replace("'image_io_calls':image_io_calls", "'image_io_calls':image_io_calls,'voice_execute_calls':getattr(bridge,'voice_execute_calls',0)")
        value=value.replace("omniauto_vision.vision_configuration_status=lambda:{'ready':True,'config':{}}", "omniauto_vision.vision_configuration_status=lambda:{'ready':True,'config':{'customer_image_understanding':{'enabled':True}}}")
    # A disabled automatic callback must be observed as zero tasks, without
    # waiting out the production model timeout or generating a test reply.
    value = value.replace('wait_for_brain=True', "wait_for_brain=not (mode=='suppress_async' and os.environ['HC_RUN']=='continued')")
    return value.replace("  frame=self._contractual_message_payload({'messages':copy.deepcopy(self.messages)})",
                         "  frame=self.get_messages(**{'display_name':'CJTEST01','rpa_session_key':''})").replace('C3TEST01', 'CJTEST01')


@pytest.mark.parametrize('mode,segmented,media',[
    ('normal',False,None),('normal',True,None),('normal',False,'voice'),('normal',False,'image'),('hc_typing',False,None),('hc_typing',True,None),
    ('hc_loss',False,None),('disabled',False,None),('suppress_async',False,None),
    ('hc_typing',False,'voice'),('hc_typing',False,'image'),('voice_full',True,'voice'),('image_full',True,'image'),('voice_full',True,'voice-loss'),('image_full',True,'image-loss')])
def test_confidence_read_reaches_async_send_ack_and_flow(http_api,monkeypatch,async_generation,tmp_path,mode,segmented,media):
    media_loss=bool(media and media.endswith('-loss'))
    if media_loss:
        media=media.removesuffix('-loss')
        monkeypatch.setenv('HC_MEDIA_LOSS','1')
    if media:monkeypatch.setenv('HC_TEST_MEDIA',media)
    monkeypatch.setattr(api,'client',http_api)
    worker = api._create_worker()
    api._create_sales(worker['id'])
    api._create_lead(remark_code='CJTEST01')
    target = api._scan(worker, remark_code='CJTEST01')
    with SessionLocal() as db:
        conv=db.get(Conversation,target['conversation_id'])
        conv.status,conv.friend_state='waiting_user_reply','friend_active'
        db.get(Worker,worker['id']).local_lock_summary={'capabilities':{
            'text_correspondence_version':2,'reply_sequence_version':1}}
        # _setup_bound_conversation creates an add-friend task even though its
        # fixture already marks the friendship active. Exclude that unrelated
        # pending setup work before starting the real background task consumer.
        for task in db.scalars(select(Task).where(Task.task_type=='add_friend')):
            task.status='cancelled'
        db.commit()
    originals=['唯一开场','这款600Pro适合日常通勤，具体信息可以再看看。','唯一末句']
    class Model(SequenceModel):
        calls=0
        def generate_reply_decision(self,**kwargs):
            type(self).calls+=1
            if type(self).calls>2:
                return AIEngineDecision(decision='send_reply',reply_text='好的，下周再联系您。',guard_result='pass')
            return super().generate_reply_decision(**kwargs) if segmented and type(self).calls>1 else AIEngineDecision(
                decision='send_reply',reply_text='好的，我帮您查询看车安排。',guard_result='pass')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    visible=[{'id':'seed-'+str(i),'sender_role':'customer','type':'text','content':text} for i,text in enumerate(originals)]
    path=tmp_path/'visible-input.json';path.write_text(json.dumps(visible,ensure_ascii=False))
    script=tmp_path/'worker.py';script.write_text(worker_source())
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock','HC_VISIBLE':str(path)}
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    def run_worker(label):
        process_mode='resume' if label=='recovered' else mode if label=='continued' else 'normal'
        run=subprocess.run([sys.executable,str(script),base,json.dumps(worker),target['conversation_id'],process_mode],
            env={**env,'HC_RUN':label,'HC_DISABLE':'1' if label=='continued' and mode=='disabled' else '0'},capture_output=True,text=True,timeout=60)
        (tmp_path/(label+'.stdout')).write_text(run.stdout)
        (tmp_path/(label+'.stderr')).write_text(run.stderr)
        assert run.returncode==0,run.stderr
        return json.loads(run.stdout.strip().splitlines()[-1])
    seed=run_worker('seed')
    assert seed['sent']==['好的，我帮您查询看车安排。'],seed
    assert not seed['runtime']['inflight_flow_id']
    # All original IDs, facts, features and AI receipts above were created by
    # the production Worker. The only new input is a subsequent physical frame.
    visible=json.loads((tmp_path/'visible.json').read_text())
    visible[1]['content']='这款600Pr0适合日常通勤，具体信息可以再看看。'
    visible.append({'id':'new-customer-question','sender_role':'customer','type':'text','content':'请详细介绍看车安排'})
    if media and mode=='normal':
        visible.append({'id':'customer-interruption','sender_role':'customer','type':media,'voice_duration':5,'content':'[语音]' if media=='voice' else '[图片]'})
    path.write_text(json.dumps(visible,ensure_ascii=False))
    with SessionLocal() as db:
        session=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==target['conversation_id']))
        session.unread_hint=True
        session.unread_generation=(session.unread_generation or 0)+1
        session.next_read_due_at=None
        db.commit()
    async_generation['suppress']=mode=='suppress_async'
    result=run_worker('continued')
    if mode=='hc_loss':
        assert result['sent']==[] and result['pending_c2_outbox']
        result=run_worker('recovered')
    if media_loss:
        assert result['sent']==[PARTS[0]] and result['pending_c2_outbox'],result
        interrupted=result
        path.write_text((tmp_path/'visible.json').read_text())
        result=run_worker('recovered')
        assert result['image_io_calls']==[] and result.get('voice_execute_calls',0)==0
        assert result['sent']==['好的，下周再联系您。'],result
        result['sent']=interrupted['sent']+result['sent']
        result['image_io_calls']=interrupted['image_io_calls']
        result['voice_execute_calls']=interrupted.get('voice_execute_calls',0)

    (tmp_path/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    expected=PARTS if segmented else ['好的，我帮您查询看车安排。']
    if mode=='hc_typing':expected=['好的，下周再联系您。']
    if mode in {'voice_full','image_full'}:expected=[PARTS[0],'好的，下周再联系您。']
    if mode in {'disabled','suppress_async'}:
        with pytest.raises(AssertionError):assert result['sent']==expected
        assert result['sent']==[] and Model.calls==1
        with SessionLocal() as db:
            assert len(list(db.scalars(select(SentAck))))==1
            assert not list(db.scalars(select(HandoffEvent)))
        return
    assert result['sent']==expected,result
    assert Model.calls==async_generation['counts']['generated']==(3 if mode in {'hc_typing','voice_full','image_full'} else 2)
    assert not result['pending_ack'] and not result['pending_c2_outbox'] and not result['locked']
    assert not result['runtime']['inflight_flow_id']
    with SessionLocal() as db:
        events=list(db.scalars(select(MessageEvent).order_by(MessageEvent.ingested_at)))
        assert [e.content for e in events[:3]]==originals
        assert sum(e.content=='请详细介绍看车安排' for e in events)==1
        assert not list(db.scalars(select(HandoffEvent)))
        assert len(list(db.scalars(select(SentAck))))==1+len(expected)+int(mode=='hc_typing')
        if mode not in {'hc_typing','voice_full','image_full'}:assert all(a.status=='sent' for a in db.scalars(select(ReplyAction)))
        if media:
            facts=[e for e in events if e.message_type==media]
            assert len(facts)==1 and facts[0].sender_role=='customer'
            assert result.get('voice_execute_calls',0)==int(media=='voice')
            assert len(result['image_io_calls'])==int(media=='image')
            actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.created_at,ReplyAction.segment_index)))
            if mode!='normal':
                assert any(a.status in {'cancelled','interrupted','superseded'} or a.error_code=='C3_CONTEXT_CHANGED_BEFORE_SEND' for a in actions)
            assert actions[-1].status=='sent'
