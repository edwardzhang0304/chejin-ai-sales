"""Real HTTP, PostgreSQL, production Worker + SQLite; model and WeChat I/O controlled.

No test calls generate/claim/ack/finish for the Worker. The synthetic transport
adds a visible bubble only when Worker asks it to send.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import SequenceModel, PARTS
import test_c3_api as fixtures
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction, SentAck
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from app.models.task import Task
from app.services import c3_service


WORKER = r'''
import json,sys,copy,shutil,time,requests,os,inspect
from pathlib import Path
from test_task_runner import FakeBridge,TaskRunnerTest
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.storage import save_binding,load_binding,load_runtime_control,has_pending_reply_send_ack_outbox,list_reply_send_ack_outbox,has_pending_c2_outbox
from chejin_worker_client.ui_lock import lock_summary
base,w,conversation_id=sys.argv[1],json.loads(sys.argv[2]),sys.argv[3]
mode=sys.argv[4]
api=WorkerApiClient(base+'/api')
binding=load_binding() if mode=='resume' else Binding(w['id'],w['worker_token'],'client-c3',run_status='running')
if mode!='resume': save_binding(binding)
api.set_run_status(binding,'running') if mode!='resume' else None
exchanges=[]
injected_status_losses=0
original=api.session.send
def send(request,**kwargs):
 global injected_status_losses
 if mode=='resume' and os.environ.get('INJECT_SEQUENCE_STATUS_LOSS') and not injected_status_losses and '/message-batches/' in request.url:
  if any(frame.function=='resume_reply_sequence' for frame in inspect.stack()):
   injected_status_losses+=1
   raise requests.ConnectionError('one interrupted continuation-status request')
 if mode.startswith('tamper_') and request.url.endswith('/wechat/messages/ingest') and bridge.sent_replies:
  body=json.loads(request.body)
  for slot in body['evidence'].get('slot_ledger_states',[]):
   if slot.get('fact_scope')=='current_read_run' and slot.get('delivery_state')=='backend_confirmed':
    if mode=='tamper_origin':
     # Keep the HTTP Flow envelope valid, but corrupt the stored origin.
     # The new omission rule must check the database, not trust this envelope.
     from app.core.database import SessionLocal
     from app.models.wechat import MessageEvent
     from sqlalchemy import select
     with SessionLocal() as db:
      event=db.scalar(select(MessageEvent).where(MessageEvent.source_message_key==slot['source_message_key']))
      assert event is not None
      event.read_run_id='read-forged'
      db.commit()
    else:
     observation=next(o for o in body['evidence']['observations'] if o['observation_id']==slot['observation_id'])
     observation['content_clean']='这一条不是原来的内容'
    request.prepare_body(data=None,files=None,json=body)
    break
 if request.url.endswith('/sent-ack') and mode in {'lost_request','typing_request_loss'}:
  raise requests.ConnectionError('injected before request reaches server')
 image_ingest_loss=(mode=='image_full' and os.environ.get('SEQUENCE_IMAGE_INGEST_LOSS')
  and request.url.endswith('/messages/ingest') and any(m.get('message_type')=='image' for m in json.loads(request.body).get('messages',[])))
 if image_ingest_loss and os.environ['SEQUENCE_IMAGE_INGEST_LOSS']=='request':
  raise requests.ConnectionError('image fact request not delivered')
 response=original(request,**kwargs)
 if image_ingest_loss and os.environ['SEQUENCE_IMAGE_INGEST_LOSS']=='response':
  raise requests.ConnectionError('image fact committed but response lost')
 if request.url.endswith('/sent-ack') and mode in {'lost_response','typing_response_loss'}:
  raise requests.ConnectionError('injected after server committed receipt')
 exchanges.append({'path':request.url.split('/api')[-1],'status':response.status_code,'response':response.json()})
 return response
api.session.send=send
class Wechat(FakeBridge):
 def __init__(self):
  super().__init__(RpaResult(ok=True,result_code='unused'))
  picture=Path(__file__).with_name('visible.json')
  self.messages=json.loads(picture.read_text()) if mode in {'resume','recall','settle_read'} else [{'id':'customer-1','sender_role':'customer','type':'text','content':'请详细介绍看车安排'}]
 def get_messages(self,**kwargs):
  payload={'messages':copy.deepcopy(self.messages),'tail_complete':True}
  if any(m.get('type')=='image' for m in self.messages):
   payload=self._contractual_message_payload(payload)
   for index, row in enumerate(payload['observations']):
    row['bubble_rect']=[100,100+150*index,500,140+150*index]
   observation=next(o for o in payload['observations'] if o['observation_id']=='customer-interruption')
   observation.update(row_kind='image_bubble',message_type='image',item_state='discovered',
                      frame_visual_id='synthetic-new-image',bubble_rect=[100,500,300,650],
                      image_physical_anchor={'sender_role':'customer','bubble_visual_fingerprint':'b'*64,
                       'preceding_stable_message':'','following_stable_message':'','occurrence_index':0,'occurrence_count':1})
   observation.pop('content_clean',None)
   payload.pop('send_context_guard',None)
   payload.pop('image_frame_action_bindings',None)
  self.get_messages_payloads=[payload]
  return super().get_messages(**kwargs)
 def send_reply(self,**kwargs):
  assert lock_summary()['locked']
  pending=[r for r in list_reply_send_ack_outbox() if r['status'] in {'intent','waiting'}]
  assert len(pending)==1 and pending[0]['reply_action_id']==kwargs['reply_action_id'] and pending[0]['status']=='intent',pending
  assert len(kwargs['text'])<=108
  assert not kwargs['cancel_check']()
  if mode.startswith('typing_') and not getattr(self,'interrupted_while_typing',False) and (mode!='typing_second' or len(self.sent_replies)==1):
   # Controlled physical I/O: a customer arrives after typing, before Enter;
   # the existing Sidecar has cleared only its own draft. The real shared
   # continuity comparator supplies the sequence decision, not a success flag.
   from chejin_worker_client.message_viewport_projection import compare_business_viewport_continuity
   self.interrupted_while_typing=True
   self.messages.append({'id':'customer-interruption','sender_role':'customer','type':'text','content':'先不要介绍，改天再联系'})
   Path(__file__).with_name('visible.json').write_text(json.dumps(self.messages))
   frame=self._contractual_message_payload({'messages':copy.deepcopy(self.messages),'tail_complete':True})
   current=frame['send_context_guard'];expected=kwargs['expected_context_guard']
   decision=compare_business_viewport_continuity(expected['sequence'],current['sequence'],old_top_boundary_complete=True,new_top_boundary_complete=True)
   assert decision['relation']=='unique_tail_append',decision
   snapshot={'ok':True,'send_context_guard':current,'frame_observation':{'frame_id':'controlled-typing-frame:'+kwargs['reply_action_id']},
    'message_sequence':[{'observation_id':o['observation_id'],'sender_role':o['sender_role']} for o in frame['observations']]}
   value={'ok':False,'state':'send_input_not_ready','error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','action_phase':'not_attempted','physical_send_triggered':False,
    'guard':{'ok':True,'visual':{'physical_send_triggered':False,'draft_clear':{'ok':True,'cleared':True,'reason':'confirmed_program_draft_cleared','focus_check':{'ok':True}},
     'context_check':{'ok':False,'error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','continuity_relation':decision['relation'],
      'worker_continuity_decision':decision,'snapshot':snapshot,'frame_observation':snapshot['frame_observation'],
      'expected_sequence_sha256':expected['sequence_sha256'],'current_sequence_sha256':current['sequence_sha256']}}}}
   Path(__file__).with_name('typing-interruption.json').write_text(json.dumps(value,ensure_ascii=False))
   return value
  self.messages.append({'id':'sent-'+str(len(self.messages)+1),'sender_role':'self','type':'text','content':kwargs['text']})
  frame=self._contractual_message_payload({'messages':copy.deepcopy(self.messages)})
  self.send_payload.update(TaskRunnerTest._confirmed_send_sidecar_result(
   observations=frame['observations'],confirmed_observation_id=self.messages[-1]['id'],run_id='controlled-send-'+str(len(self.messages))))
  Path(__file__).with_name('visible.json').write_text(json.dumps(self.messages))
  result=super().send_reply(**kwargs)
  if mode in {'pause','pause_network'} and len(self.sent_replies)==1:
   assert runner.set_run_status('paused')
  if mode in {'inbound','voice','image','voice_full','image_full'} and len(self.sent_replies)==1:
   self.messages.append({'id':'customer-interruption','sender_role':'customer','type':mode.split('_')[0] if mode!='inbound' else 'text','voice_duration':5,
                         'content':{'voice':'[语音]','image':'[图片]'}.get(mode.split('_')[0],'先不要介绍，改天再联系')})
   Path(__file__).with_name('visible.json').write_text(json.dumps(self.messages))
  return result
 def prepare_voice_action(self,**kwargs):
  group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))
  assert group['reply_sequence']['terminal'] is True,group
  assert len(self.sent_replies)==1
  if mode!='voice_full':
   raise RuntimeError('CONTROLLED_STOP_AFTER_PROVING_CANCEL_BEFORE_VOICE_ACTION')
  return super().prepare_voice_action(**kwargs)
 def execute_voice_action(self,**kwargs):
  voice=next(m for m in self.messages if m['id']=='customer-interruption')
  voice.update(content='先不要介绍，改天再联系',voice_duration=5,voice_anchor_stable_key='customer-interruption')
  self.voice_payload={'ok':True,'state':'voice_transcribe_completed','sidecar_run_id':'controlled-voice-complete',
   'action_phase':'confirmed','business_state':'completed','business_result_confirmed':True,'ui_action_performed':True,
   'processed_voice_anchor_keys':['customer-interruption'],'failed_voice_anchor_keys':[],
   'transcribed_messages':[{'content':voice['content'],'sender_role':'customer','voice_anchor_stable_key':'customer-interruption'}],
   'item_action_outcomes':[{'action_phase':'confirmed','business_state':'completed','business_result_confirmed':True,'physical_anchor_keys':['customer-interruption']}]}
  self.get_messages_payloads=[{'messages':copy.deepcopy(self.messages),'tail_complete':True}]
  return super().execute_voice_action(**kwargs)
bridge=Wechat()
image_io_calls=[]
if mode=='image_full' or (mode=='resume' and Path(__file__).with_name('native-process-result.json').exists()):
 from chejin_worker_client import omniauto_vision
 from chejin_worker_client.action_journal import read_action_journal
 from apps.wechat_ai_customer_service.optional_plugins.vision.plugin import BuiltinVisionPlugin
 from test_c2_vision_integration import C2VisionIntegrationTests
 native_process_image_slot=omniauto_vision.process_image_slot
 omniauto_vision.vision_configuration_status=lambda:{'ready':True,'config':{'customer_image_understanding':{'enabled':True}}}
 def image_plugin_io(self,context):
  assert mode!='resume','recovery must not copy or recognize the same image again'
  group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))
  assert group['reply_sequence']['terminal'] is True
  image_io_calls.append(context['message_id'])
  observations=bridge.get_messages(display_name='C3TEST01',rpa_session_key='')['observations']
  order=context['expected_business_screen_order']
  summary='客户发来一张车辆外观图片'
  understanding={
   'schema_version':1,'enabled':True,'applied':True,'adoptable':True,'reason':'vision_ready',
   'provider':omniauto_vision.DEFAULT_VISION_BASE_URL,'request_style':omniauto_vision.DEFAULT_VISION_REQUEST_STYLE,
   'model':omniauto_vision.DEFAULT_VISION_MODEL,
   **C2VisionIntegrationTests.strict_provider_payload(summary),
   'audit':{'latency_ms':1,'used_fallback':False,'provider_error':'','retry_error':'','retry_after_non_json':False,'catalog_identity_candidate_count':0}}
  result={'applied':True,'reason':'vision_ready','customer_image_understanding':understanding,
   'visual_bridge_input':{'schema_version':1,'present':True,'vision_summary':summary,
    'classification':{'is_vehicle':False,'vehicle_confidence':0.0,'unknown':True},
    'catalog_assist':{'normalized_vehicle_query':'','candidate_names':[],'exact_candidate_name':''},
    'intent_hints':{'wants_catalog_match':False,'wants_similar_recommendation':False,'needs_clarification':True},
    'vehicle_image_retrieval':{'matched':False,'candidate_names':[]},'source_message_ids':[context['message_id']]},
   'clipboard_transaction':{
    'action_phase':'confirmed','ui_action_performed':True,'current_frame_target_selected':True,
    'physical_identity_inherited_from_prepare':False,
    'current_frame_selection_evidence':{'selection_policy':'worker_approved_current_business_occurrence',
     'physical_identity_inherited_from_prepare':False,'current_business_screen_order':order},
    'trigger_observation_id':context['message_id'],'trigger_business_screen_order':order,
    'action_frame_observations':observations,'action_frame_layout_snapshot_id':'layout:'+context['message_id'],
    'image_sha256':'a'*64,'right_click_ok':True,'menu_opened':True,'copy_click_ok':True,
    'clipboard_content_read':True,'clipboard_image_valid':True}}
  Path(__file__).with_name('controlled-plugin-result.json').write_text(json.dumps(result,ensure_ascii=False))
  return result
 BuiltinVisionPlugin.run=image_plugin_io
 def observed_native_process(**kwargs):
  # Supply controlled Windows I/O context; retain the real wrapper, receipt,
  # Journal and pending-continuity state. Never manufacture business completion.
  kwargs['window_context']=C2VisionIntegrationTests.window_context()
  result=native_process_image_slot(**kwargs)
  Path(__file__).with_name('native-process-result.json').write_text(json.dumps(result,ensure_ascii=False))
  Path(__file__).with_name('native-process-journal.json').write_text(json.dumps(read_action_journal(kwargs['action_journal_path']),ensure_ascii=False))
  return result
 omniauto_vision.process_image_slot=observed_native_process
 import chejin_worker_client.task_runner as observed_runner
 original_continuity=observed_runner._image_action_frame_to_reread_continuity
 def observed_image_continuity(*args,**kwargs):
  result=original_continuity(*args,**kwargs)
  Path(__file__).with_name('image-continuity.json').write_text(json.dumps(result,ensure_ascii=False,default=str))
  return result
 observed_runner._image_action_frame_to_reread_continuity=observed_image_continuity
errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append)
runner.binding=binding
read_trace=[]
original_read=runner._read_one_wechat_target
def traced_read(*args,**kwargs):
 entry={'step':kwargs.get('current_step','initial_read'),'phase':kwargs.get('operation_phase')}
 read_trace.append(entry)
 value=original_read(*args,**kwargs)
 entry.update(ok=value.get('ok'),new_self=value.get('new_self_message_count'),new_customer=value.get('new_customer_message_count'))
 if kwargs.get('current_step')=='reply_sequence_read' and os.environ.get('SEQUENCE_DISABLE_READ_REUSE'):
  value.pop('_reply_sequence_frame',None)
 return value
runner._read_one_wechat_target=traced_read
if mode=='image':
 original_images=runner._process_final_image_slots
 def stop_at_image_boundary(*args,**kwargs):
  if bridge.sent_replies:
   group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))
   assert group['reply_sequence']['terminal'] is True
   raise RuntimeError('CONTROLLED_STOP_AT_IMAGE_ENTRY_AFTER_CANCEL')
  return original_images(*args,**kwargs)
 runner._process_final_image_slots=stop_at_image_boundary
stage=runner._stage_payload_ledger
def observe_stage(payload):
 try:return stage(payload)
 except Exception:
  Path(__file__).with_name('failed-ledger-payload.json').write_text(json.dumps(payload,ensure_ascii=False,default=str))
  raise
runner._stage_payload_ledger=observe_stage
try:
 if mode=='resume':
  runner.start(binding)
  started=runner.set_run_status('running')
  assert started, {'errors':errors,'exchanges':exchanges}
  for _ in range(200):
   if not load_runtime_control()['inflight_flow_id']: break
   time.sleep(.1)
  runner.stop_event.set()
  for thread in (runner.thread, runner.c2_thread, runner.thread_monitor):
   if thread: thread.join(timeout=5)
  result={'production_threads_resumed':True}
 else:
  target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==conversation_id)
  result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
 out={'result':result,'sent':[r['text'] for r in bridge.sent_replies], 'exchanges':exchanges,'runtime':load_runtime_control(),
      'pending_ack':has_pending_reply_send_ack_outbox(),'locked':lock_summary()['locked'],'errors':errors,'injected_status_losses':injected_status_losses,'image_io_calls':image_io_calls,'pending_c2_outbox':has_pending_c2_outbox(),'read_trace':read_trace}
 print(json.dumps(out,ensure_ascii=False,default=str))
finally:
 runner._stop_task_lease_guard()
 shutil.rmtree(bridge.send_journal_dir)
'''


@pytest.mark.parametrize("mode", ["normal", "handoff", "recall", "inbound", "pause", "pause_network", "lost_request", "lost_response", "voice", "image", "voice_full", "image_full", "typing_first", "typing_second", "typing_request_loss", "typing_response_loss", "tamper_origin", "tamper_content"])
def test_worker_sends_and_settles_three_segments_without_test_intervention(http_api, monkeypatch, async_generation, tmp_path, mode):
    monkeypatch.setattr(fixtures, "client", http_api)
    class Model(SequenceModel):
        calls = 0
        requests = []
        def generate_reply_decision(self, **kwargs):
            type(self).requests.append(kwargs)
            from app.services.ai_adapter import AIEngineDecision
            type(self).calls += 1
            if type(self).calls > 1 and mode != "recall":
                return AIEngineDecision(decision="send_reply", reply_text="好的，之后再联系。", guard_result="pass")
            decision = super().generate_reply_decision(**kwargs)
            if mode == "handoff":
                from dataclasses import replace
                decision = replace(decision, decision="reply_then_handoff", handoff_reason_code="CUSTOMER_HIGH_INTENT")
            return decision
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", Model)
    worker, binding = fixtures._setup_bound_conversation()
    with SessionLocal() as db:
        db.get(Conversation, binding["conversation_id"]).status = "waiting_user_reply"
        db.get(Worker, worker["id"]).local_lock_summary = {"capabilities": {"reply_sequence_version": 1}}
        db.commit()
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "CHEJIN_WORKER_HOME": str(tmp_path / "worker"), "CHEJIN_RPA_MODE": "mock"}
    base = http_api.get("/healthz").url.removesuffix("/healthz")
    process = subprocess.run([sys.executable, str(script), base, json.dumps(worker), binding["conversation_id"], "normal" if mode == "recall" else mode],
                             env=env, text=True, capture_output=True, timeout=45)
    (tmp_path / "worker.stdout").write_text(process.stdout)
    (tmp_path / "worker.stderr").write_text(process.stderr)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    (tmp_path / "worker-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (tmp_path / "automatic-model-inputs.json").write_text(json.dumps(Model.requests,ensure_ascii=False,indent=2,default=str))
    if mode == "image_full":
        raw = json.loads((tmp_path / "native-process-result.json").read_text())
        assert raw["state"] == "completed", raw
        assert raw["business_state"] == "confirmed_result_pending_continuity", raw
        assert raw["business_result_confirmed"] is False, raw
        receipt = raw["_confirmed_image_action_receipt"]
        assert receipt["binding_confirmed"] is True and receipt["image_sha256"] == "a" * 64, raw
        assert result["image_io_calls"] == ["customer-interruption"], result
        with SessionLocal() as db:
            diagnosis = {
                "native_receipt_valid": True, "business_state": raw["business_state"],
                "worker_result": result["result"], "sent": result["sent"], "model_calls": Model.calls,
                "image_facts": [m.content for m in db.scalars(select(MessageEvent).where(MessageEvent.message_type == "image"))],
                "sent_ack_count": len(list(db.scalars(select(SentAck)))),
                "handoffs": [h.handoff_reason_code for h in db.scalars(select(HandoffEvent))],
                "continuity_called": (tmp_path / "image-continuity.json").exists(),
                "actions": [{"status": a.status, "segment_index": a.segment_index} for a in db.scalars(select(ReplyAction))],
            }
        (tmp_path / "native-boundary-diagnosis.json").write_text(json.dumps(diagnosis,ensure_ascii=False,indent=2))
    if mode == "recall":
        assert result["sent"] == PARTS and not result["runtime"]["inflight_flow_id"], result
        # Only advance the scheduler's clock inputs; the second subprocess must
        # discover the recall target, read, generate, send and finish by itself.
        from datetime import timedelta
        from app.models.base import utcnow
        from app.models.wechat import WechatSessionBinding
        from app.core.config import get_settings
        monkeypatch.setattr(get_settings(), "c3_recall_quiet_start_hour", 0)
        monkeypatch.setattr(get_settings(), "c3_recall_quiet_end_hour", 0)
        with SessionLocal() as db:
            session = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == binding["conversation_id"]))
            session.next_read_due_at = utcnow()-timedelta(seconds=1)
            db.commit()
        settled = subprocess.run([sys.executable, str(script), base, json.dumps(worker), binding["conversation_id"], "settle_read"],
                                 env=env, text=True, capture_output=True, timeout=45)
        (tmp_path / "settle-read.stdout").write_text(settled.stdout)
        (tmp_path / "settle-read.stderr").write_text(settled.stderr)
        assert settled.returncode == 0, settled.stderr
        assert json.loads(settled.stdout.strip().splitlines()[-1])["sent"] == []
        with SessionLocal() as db:
            db.get(Conversation, binding["conversation_id"]).next_recall_at = utcnow()-timedelta(seconds=1)
            session = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == binding["conversation_id"]))
            session.next_read_due_at = utcnow()-timedelta(seconds=1)
            db.commit()
        recalled = subprocess.run([sys.executable, str(script), base, json.dumps(worker), binding["conversation_id"], "recall"],
                                  env=env, text=True, capture_output=True, timeout=45)
        (tmp_path / "recall.stdout").write_text(recalled.stdout)
        (tmp_path / "recall.stderr").write_text(recalled.stderr)
        assert recalled.returncode == 0, recalled.stderr
        recalled_result = json.loads(recalled.stdout.strip().splitlines()[-1])
        assert recalled_result["sent"] == PARTS, recalled_result
        assert not recalled_result["runtime"]["inflight_flow_id"] and not recalled_result["pending_ack"]
        with SessionLocal() as db:
            assert len(list(db.scalars(select(SentAck)))) == 6
            conversation = db.get(Conversation, binding["conversation_id"])
            assert conversation.reply_count == 6 and conversation.recall_count == conversation.recall_daily_count == 1
            assert conversation.status == "recalled_waiting_user"
            assert len(list(db.scalars(select(MessageBatch).where(MessageBatch.trigger_type == "recall")))) == 1
        return
    if mode.startswith("tamper_"):
        assert result["sent"] == PARTS[:1], result
        rejected = [e for e in result["exchanges"] if e["path"].endswith("/messages/ingest") and e["status"] == 409]
        assert rejected, result
        assert all(e["response"]["code"] not in {"MESSAGE_CONTRACT_REVISION_MISMATCH", "WORKER_INFLIGHT_FLOW_MISMATCH"} for e in rejected)
        return
    if mode in {"voice", "image"}:
        assert result["sent"] == PARTS[:1], result
        interruption = [e for e in result["exchanges"] if e["path"].endswith("/interrupt-reply-sequence")]
        assert interruption and interruption[0]["status"] == 200, result
        assert interruption[0]["response"]["data"]["reply_sequence"]["terminal"]
        with SessionLocal() as db:
            assert all(a.status == "superseded" for a in db.scalars(select(ReplyAction).where(ReplyAction.segment_index > 1)))
        return  # Controlled media stop verifies cancellation, not media completion/Windows.
    image_loss = mode == "image_full" and os.environ.get("SEQUENCE_IMAGE_INGEST_LOSS")
    if mode in {"pause", "pause_network", "lost_request", "lost_response", "typing_request_loss", "typing_response_loss"} or image_loss:
        typing_loss = mode.startswith("typing_")
        assert result["sent"] == ([] if typing_loss else PARTS[:1]) and result["runtime"]["inflight_flow_id"], result
        if mode == "pause_network":
            env["INJECT_SEQUENCE_STATUS_LOSS"] = "1"
        resumed = subprocess.run([sys.executable, str(script), base, json.dumps(worker), binding["conversation_id"], "resume"],
                                 env=env, text=True, capture_output=True, timeout=45)
        (tmp_path / "resume.stdout").write_text(resumed.stdout)
        (tmp_path / "resume.stderr").write_text(resumed.stderr)
        assert resumed.returncode == 0, resumed.stderr
        resumed_result = json.loads(resumed.stdout.strip().splitlines()[-1])
        assert resumed_result["sent"] == (["好的，之后再联系。"] if typing_loss or image_loss else PARTS[1:]), resumed_result
        if mode == "pause_network":
            assert resumed_result["injected_status_losses"] == 1, resumed_result
        if image_loss:
            assert not resumed_result["image_io_calls"], resumed_result
        result = {**resumed_result, "sent": result["sent"] + resumed_result["sent"], "image_io_calls": result["image_io_calls"] + resumed_result["image_io_calls"]}
    (tmp_path / "automatic-model-inputs.json").write_text(json.dumps(Model.requests,ensure_ascii=False,indent=2,default=str))
    typing = mode.startswith("typing_")
    interrupted = mode in {"inbound", "voice_full", "image_full"} or typing
    expected_sent = ([PARTS[0]] if mode == "typing_second" else []) + ["好的，之后再联系。"] if typing else (PARTS if not interrupted else [PARTS[0], "好的，之后再联系。"])
    assert result["sent"] == expected_sent, result
    assert not result["pending_ack"] and not result["locked"]
    assert not result["runtime"]["inflight_flow_id"], result
    with SessionLocal() as db:
        actions = list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        sent = [a for a in actions if a.status == "sent"]
        assert len(sent) == len(expected_sent)
        receipts = list(db.scalars(select(SentAck)))
        assert len(receipts) == len(sent) + int(typing)
        if typing:
            failed = [ack for ack in receipts if ack.send_result != "sent"]
            assert len(failed) == 1 and failed[0].action_phase == "not_attempted"
            assert failed[0].error_code == "C3_CONTEXT_CHANGED_BEFORE_SEND"
            assert not list(db.scalars(select(HandoffEvent)))
            assert Model.calls == 2
            assert "先不要介绍，改天再联系" in json.dumps(Model.requests[-1],ensure_ascii=False)
        if not interrupted:
            assert all(a.pre_send_fact_checkpoint for a in actions if a.segment_index > 1)
        else:
            assert all(a.status in ({"failed", "superseded", "sent"} if typing else {"superseded"}) for a in actions if a.segment_index > 1)
        assert db.get(Conversation, binding["conversation_id"]).reply_count == len(sent)
        assert not db.get(Worker, worker["id"]).inflight_flow_state
        if mode in {"voice_full", "image_full"}:
            media = "voice" if mode == "voice_full" else "image"
            facts = list(db.scalars(select(MessageEvent).where(MessageEvent.message_type == media)))
            assert len(facts) == 1 and facts[0].content
            assert not list(db.scalars(select(HandoffEvent)))
            assert Model.calls == 2
            assert not result["pending_c2_outbox"]
            expected_content = "客户发来一张车辆外观图片" if media == "image" else "先不要介绍，改天再联系"
            assert expected_content in json.dumps(Model.requests[-1],ensure_ascii=False,default=str)
            if media == "image":
                assert result["image_io_calls"] == ["customer-interruption"]
        if mode == "handoff":
            assert len(list(db.scalars(select(HandoffEvent)))) == 1
            assert db.get(Conversation, binding["conversation_id"]).status == "waiting_sales_reply"
    if mode == "normal":
        steps = [entry["step"] for entry in result["read_trace"]]
        assert steps.count("reply_sequence_read") == 2, steps
        assert steps.count("pre_send_refresh") == 1, ("duplicate_pre_send_refresh", steps)


def test_missing_reusable_frame_keeps_sending_protected_but_fails_ocr_optimization(
    http_api, monkeypatch, async_generation, tmp_path,
):
    """Same business assertions pass first; removing reuse restores both reads."""
    monkeypatch.setenv("SEQUENCE_DISABLE_READ_REUSE", "1")
    with pytest.raises(AssertionError, match="duplicate_pre_send_refresh"):
        test_worker_sends_and_settles_three_segments_without_test_intervention(
            http_api, monkeypatch, async_generation, tmp_path, "normal",
        )


@pytest.mark.parametrize("mode,removed", [
    ("image_full", "continuity"), ("image_full", "generation"),
    ("typing_first", "generation"), ("typing_first", "server_interruption"),
])
def test_missing_required_step_breaks_complete_continuation(http_api, monkeypatch, async_generation, tmp_path, mode, removed):
    """The positive must fail itself; the test never manufactures replacement work."""
    if removed == "continuity":
        altered = WORKER.replace(
            "result=original_continuity(*args,**kwargs)",
            "result={'ok':False,'relation':'business_sequence_not_continuous','reason':'test_removed_continuity','matched_pairs':[],'new_suffix_indexes':[]}",
        )
        monkeypatch.setattr(sys.modules[__name__], "WORKER", altered)
    suppressed = []
    if removed == "generation":
        from starlette.background import BackgroundTasks
        add_task = BackgroundTasks.add_task
        def schedule(self, func, *args, **kwargs):
            if func.__name__ == "observed_generate" and async_generation["counts"]["generated"] >= 1:
                suppressed.append(args)
                return None
            return add_task(self, func, *args, **kwargs)
        monkeypatch.setattr(BackgroundTasks, "add_task", schedule)
        monkeypatch.setenv("CHEJIN_C3_BRAIN_NO_PROGRESS_WATCHDOG_SECONDS", "2")
        monkeypatch.setenv("CHEJIN_C3_BRAIN_POLL_INTERVAL_SECONDS", "0.1")
    if removed == "server_interruption":
        from app.services import reply_sequence_service
        monkeypatch.setattr(reply_sequence_service, "settle_customer_interrupted_segment", lambda *args: False)
    with pytest.raises(AssertionError):
        test_worker_sends_and_settles_three_segments_without_test_intervention(
            http_api, monkeypatch, async_generation, tmp_path, mode)
    result = json.loads((tmp_path / "worker-result.json").read_text())
    models = json.loads((tmp_path / "automatic-model-inputs.json").read_text())
    assert result["sent"] == (PARTS[:1] if mode == "image_full" else [])
    assert len(models) == 1
    if removed == "generation":
        assert suppressed
    with SessionLocal() as db:
        if removed == "continuity":
            assert not list(db.scalars(select(MessageEvent).where(MessageEvent.message_type == "image")))
            assert result["runtime"]["pause_requested"]
        if removed == "server_interruption":
            assert len(list(db.scalars(select(HandoffEvent)))) == 1
        else:
            assert not list(db.scalars(select(HandoffEvent)))
    (tmp_path / "negative-result.json").write_text(json.dumps({"removed":removed,"positive_failed":True,"model_calls":len(models),"sent":result["sent"],"suppressed_generation":len(suppressed)},ensure_ascii=False,indent=2))


@pytest.mark.parametrize("loss", ["request", "response"])
def test_native_image_fact_recovers_same_sqlite_without_second_physical_action(http_api, monkeypatch, async_generation, tmp_path, loss):
    from app.services import reply_sequence_service
    original_tail = reply_sequence_service.interrupted_customer_tail
    observations = []
    def observe_tail(db, **kwargs):
        result = original_tail(db, **kwargs)
        batch = db.get(MessageBatch, kwargs["batch_id"])
        observations.append({"input": kwargs, "result": result, "interruption": (batch.ai_response_snapshot or {}).get("reply_sequence_interrupt")})
        (tmp_path / "recovered-tail-calls.json").write_text(json.dumps(observations,ensure_ascii=False,indent=2))
        return result
    monkeypatch.setattr(reply_sequence_service, "interrupted_customer_tail", observe_tail)
    monkeypatch.setenv("SEQUENCE_IMAGE_INGEST_LOSS", loss)
    test_worker_sends_and_settles_three_segments_without_test_intervention(
        http_api, monkeypatch, async_generation, tmp_path, "image_full")


def test_removing_recovered_tail_collection_breaks_image_restart_continuation(http_api, monkeypatch, async_generation, tmp_path):
    from app.services import reply_sequence_service
    monkeypatch.setattr(reply_sequence_service, "interrupted_customer_tail", lambda *args, **kwargs: [])
    monkeypatch.setenv("SEQUENCE_IMAGE_INGEST_LOSS", "response")
    with pytest.raises(AssertionError):
        test_worker_sends_and_settles_three_segments_without_test_intervention(
            http_api, monkeypatch, async_generation, tmp_path, "image_full")
    resumed = json.loads((tmp_path / "resume.stdout").read_text().splitlines()[-1])
    assert resumed["sent"] == [] and not resumed["image_io_calls"]
    with SessionLocal() as db:
        assert len(list(db.scalars(select(MessageEvent).where(MessageEvent.message_type == "image")))) == 1
        assert len(list(db.scalars(select(SentAck)))) == 1
        assert not list(db.scalars(select(HandoffEvent)))
        # The response was lost after the backend generated the replacement.
        # Removing reconciliation strands that existing task, not generation.
        assert len(list(db.scalars(select(ReplyAction).where(ReplyAction.status == "queued")))) == 1
    assert async_generation["counts"]["generated"] == 2
    (tmp_path / "negative-result.json").write_text(json.dumps({
        "removed": "recovered_tail_collection", "positive_failed": True,
        "image_fact_count": 1, "generated_count": 2, "resumed_sent": resumed["sent"],
        "repeated_image_io": resumed["image_io_calls"],
    }, ensure_ascii=False, indent=2))
