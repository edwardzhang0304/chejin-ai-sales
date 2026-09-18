"""Real Worker, socket HTTP, PostgreSQL, SQLite and async generation.

Only desktop observations/physical input and the external model are controlled.
The production confirmation comparator receives the actual ordered synthetic
observations. Tests never submit ACKs or generate replacement tasks themselves.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.wechat import MessageEvent
from app.models.worker import Worker
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
import test_c3_api as fixtures
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import PARTS
from test_reply_sequence_worker import WORKER

NEXT_REPLY = "好的，我按您新补充的信息重新安排。"


def worker_script():
    script = WORKER
    anchor = " def get_messages(self,**kwargs):"
    assert script.count(anchor) == 1
    script = script.replace(anchor, """
 def _contractual_message_payload(self,payload):
  value=super()._contractual_message_payload(payload)
  for index,row in enumerate(value['observations']):
   left=500 if row.get('sender_role')=='self' else 100
   row.setdefault('bubble_rect',[left,100+150*index,left+300,140+150*index])
  return value
""" + anchor)
    start = script.index("  self.messages.append({'id':'sent-'")
    end = script.index("  return result\n def prepare_voice_action", start)
    script = script[:start] + SEND_BOUNDARY + script[end:]
    # The receipt transaction cancels the old tail before the first media
    # action; observe its formal GET state rather than requiring an extra POST.
    old = "group=next(e['response']['data'] for e in exchanges if e['path'].endswith('/interrupt-reply-sequence'))"
    new = "group=api.get_wechat_message_batch(binding,next(e['response']['data']['batch_id'] for e in exchanges if '/message-batches/' in e['path']))"
    script = script.replace(old, new)
    script = script.replace("on_result=lambda _:None,on_error=errors.append)",
                            "on_result=lambda _:None,on_error=errors.append,can_pull_tasks=lambda:False)")
    # Continue through the normal production loop; no direct read/generate/ACK
    # calls after the original authorized read. Restart uses the same SQLite.
    anchor = " out={'result':result,'sent':"
    assert script.count(anchor) == 1
    script = script.replace(anchor, LOOP + anchor)
    script = script.replace("errors=[]\nrunner=", "from chejin_worker_client import omniauto_vision\nomniauto_vision.vision_configuration_status=lambda:{'ready':True,'config':{'customer_image_understanding':{'enabled':True}}}\nerrors=[]\nrunner=")
    return script.replace("C3TEST01", "CJTEST01")


SEND_BOUNDARY = r'''
  from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
  if os.environ.get('AUDIT_OLD_SEND_MATCHER_FILE'):
   exec(Path(os.environ['AUDIT_OLD_SEND_MATCHER_FILE']).read_text(),sidecar.__dict__)
  before=self._contractual_message_payload({'messages':copy.deepcopy(self.messages)})
  self.messages.append({'id':'sent-'+str(len(self.messages)+1),'sender_role':'self','type':'text','content':kwargs['text']})
  sent_id=self.messages[-1]['id']
  if not any(m['id']=='customer-interruption' for m in self.messages):
   kind=os.environ['POST_SEND_KIND']
   self.messages.append({'id':'customer-interruption','sender_role':'customer','type':kind,'voice_duration':5,
    'content':{'image':'[图片]','voice':'[语音]'}.get(kind,'先不要介绍，改天再联系')})
  frame=self._contractual_message_payload({'messages':copy.deepcopy(self.messages)})
  def snapshot(source,identity):
   seq=[{'observation_id':o['observation_id'],'sender_role':o['sender_role'],'row_kind':o['row_kind'],
         'content_normalized':o.get('content_clean') or o.get('content') or ''} for o in source['observations']]
   return {'ok':True,'validation':{'confirmed_target':'C3TEST01'},'input_region':{'has_visible_text':False},
           'frame_observation':{'frame_id':identity},'message_sequence':seq,'observations':source['observations']}
  baseline=snapshot(before,'pre:'+kwargs['reply_action_id'])
  current=snapshot(frame,'post:'+kwargs['reply_action_id'])
  confirmed=sidecar.confirm_reply_sent(1,target='C3TEST01',text=kwargs['text'],exact=True,baseline_match_count=0,
    baseline_message_sequence=baseline['message_sequence'],initial_snapshot=current,max_attempts=1)
  self.send_payload.update({'sidecar_run_id':'controlled-send:'+kwargs['reply_action_id'],
   'send_result':{'ok':bool(confirmed['ok']),'confirmed':bool(confirmed['ok']),
    'result':'sent' if confirmed['ok'] else 'unknown','send_baseline':baseline,'sent_confirmation':confirmed}})
  Path(__file__).with_name('visible.json').write_text(json.dumps(self.messages))
  result=super().send_reply(**kwargs)
'''

LOOP = r'''
 if mode not in {'lost_request','lost_response'}:
  runner.start(binding)
  until=time.monotonic()+12
  while time.monotonic()<until:
   if len(bridge.sent_replies)>=int(os.environ.get('POST_SEND_EXPECT_SENDS','2')) and not load_runtime_control()['inflight_flow_id']:break
   time.sleep(.05)
  runner.stop_event.set()
  for thread in (runner.thread,runner.c2_thread,runner.thread_monitor):
   if thread:thread.join(5)
'''


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("kind", ["text", "image", "voice"])
def test_post_send_customer_continues_via_formal_reader(http_api, monkeypatch, async_generation, tmp_path, segmented, kind):
    _run(http_api, monkeypatch, async_generation, tmp_path, segmented, kind)


@pytest.mark.parametrize("transport", ["lost_request", "lost_response"])
def test_confirmed_receipt_and_read_intent_survive_new_process(http_api, monkeypatch, async_generation, tmp_path, transport):
    _run(http_api, monkeypatch, async_generation, tmp_path, True, "text", transport)


def _run(http, monkeypatch, async_generation, tmp_path, segmented, kind, transport="normal"):
    monkeypatch.setattr(fixtures, "client", http)
    class Model:
        requests = []
        def generate_reply_decision(self, **kwargs):
            type(self).requests.append(kwargs)
            if len(type(self).requests) > 1:
                return AIEngineDecision(decision="send_reply", reply_text=NEXT_REPLY, guard_result="pass")
            parts = PARTS if segmented else ["好的，我来介绍一下看车安排。"]
            return AIEngineDecision(decision="send_reply", reply_text=" ".join(parts), guard_result="pass",
                                    raw_payload={"omniauto_brain_result": {"brain_plan": {"reply_segments": parts}}})
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", Model)
    if os.environ.get("AUDIT_DISABLE_POST_SEND_RULE") == "1":
        from app.services import post_send_customer_read
        monkeypatch.setattr(post_send_customer_read, "settle", lambda *a, **kw: False)
    if os.environ.get("AUDIT_DISABLE_POST_SEND_CALLBACK") == "1":
        from app.api.routes import wechat
        native = wechat._generate_message_batch
        def once(*a, **kw):
            if not Model.requests: return native(*a, **kw)
        monkeypatch.setattr(wechat, "_generate_message_batch", once)
    worker = fixtures._create_worker()
    fixtures._create_sales(worker["id"])
    fixtures._create_lead(remark_code="CJTEST01")
    target = fixtures._scan(worker, remark_code="CJTEST01")
    with SessionLocal() as db:
        conversation = db.get(Conversation, target["conversation_id"])
        conversation.status, conversation.friend_state = "waiting_user_reply", "friend_active"
        db.get(Worker, worker["id"]).local_lock_summary = {"capabilities": {"reply_sequence_version": 1}}
        db.commit()
    script = tmp_path / "worker.py"
    script.write_text(worker_script())
    env = {**os.environ, "CHEJIN_WORKER_HOME": str(tmp_path / "worker"), "CHEJIN_RPA_MODE": "mock", "POST_SEND_KIND": kind,
           "CHEJIN_C2_ENABLED": "true"}
    base = http.get("/healthz").url.removesuffix("/healthz")
    mode = transport if transport != "normal" else {"image": "image_full", "voice": "voice_full"}.get(kind, "normal")
    def execute(mode):
        process = subprocess.run([sys.executable, str(script), base, json.dumps(worker), target["conversation_id"], mode],
                                 env=env, text=True, capture_output=True, timeout=65)
        (tmp_path / (mode + ".stdout")).write_text(process.stdout)
        (tmp_path / (mode + ".stderr")).write_text(process.stderr)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout.strip().splitlines()[-1])
    result = execute(mode)
    results = [result]
    if transport != "normal":
        assert result["pending_ack"] and len(result["sent"]) == 1, result
        env["POST_SEND_EXPECT_SENDS"] = "1"
        results.append(execute("resume"))
    (tmp_path / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (tmp_path / "model-requests.json").write_text(json.dumps(Model.requests, ensure_ascii=False, indent=2, default=str))
    assert [text for r in results for text in r["sent"]] == [PARTS[0] if segmented else "好的，我来介绍一下看车安排。", NEXT_REPLY], results
    assert len(Model.requests) == 2
    if segmented:
        recovery = Model.requests[-1]["conversation_context"]["brain_context_snapshot"].get("partial_reply_recovery")
        assert recovery and [p["text"] for p in recovery["confirmed_prefix"]] == PARTS[:1]
    final = results[-1]
    assert not final["pending_ack"] and not final["pending_c2_outbox"] and not final["locked"]
    assert not final["runtime"]["inflight_flow_id"], final
    with SessionLocal() as db:
        acks = list(db.scalars(select(SentAck)))
        assert len(acks) == 2 and all(a.send_result == "sent" for a in acks)
        assert not list(db.scalars(select(HandoffEvent)))
        actions = list(db.scalars(select(ReplyAction)))
        assert len([a for a in actions if a.status == "sent"]) == 2
        if segmented: assert all(a.status in {"cancelled", "superseded"} for a in actions if a.segment_index > 1)
        facts = list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role == "customer")))
        assert len(facts) == 2 and len({a.source_message_key for a in facts}) == 2
        assert not db.get(Worker, worker["id"]).inflight_flow_state
