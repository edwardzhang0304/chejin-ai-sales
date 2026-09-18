"""Real HTTP/PostgreSQL + Worker subprocess/SQLite; OCR and sends are I/O fixtures.

The first read has an OCR omission. After segment one the same visible message
is read correctly. Do not repair history or call generation/ack/finish in tests.
"""
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import SequenceModel, PARTS
from test_reply_sequence_worker import WORKER
import test_c3_api as fixtures
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, SentAck
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from app.services import c3_service


@pytest.mark.parametrize("transport", ["normal", "request_lost", "response_lost"])
def test_segment_identity_failure_reports_after_prior_facts(http_api, monkeypatch, async_generation, tmp_path, transport):
    monkeypatch.setattr(fixtures, "client", http_api)
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", SequenceModel)
    worker, target = fixtures._setup_bound_conversation()
    with SessionLocal() as db:
        db.get(Conversation, target["conversation_id"]).status = "waiting_user_reply"
        db.get(Worker, worker["id"]).local_lock_summary = {"capabilities": {"reply_sequence_version": 1}}
        db.commit()
    # Vary only what the physical read boundary returns, never the comparator.
    needle = "  payload={'messages':copy.deepcopy(self.messages),'tail_complete':True}"
    assert WORKER.count(needle) == 1
    script_text = WORKER.replace(needle,
        "  if self.sent_replies: self.messages[0]['content']='请详细介绍看车安排及费用'\n" + needle +
        "\n  if self.sent_replies: payload['top_message_fragment']={'state':'partial','reason':'controlled_old_top_fragment'}")
    script_text = script_text.replace(
        "'status':response.status_code,'response':response.json()}",
        "'status':response.status_code,'response':response.json(),'request':json.loads(request.body) if request.body else None}")
    needle = " response=original(request,**kwargs)"
    assert script_text.count(needle) == 1
    script_text = script_text.replace(needle,
        " gate_only=request.url.endswith('/messages/ingest') and not json.loads(request.body).get('messages') and bool(json.loads(request.body).get('evidence',{}).get('flow_gate_errors'))\n"
        " if gate_only and os.environ.get('GATE_TRANSPORT')=='request_lost': raise requests.ConnectionError('gate request lost')\n" + needle +
        "\n if gate_only and os.environ.get('GATE_TRANSPORT')=='response_lost': raise requests.ConnectionError('gate response lost after commit')")
    script = tmp_path / "worker.py"
    script.write_text(script_text)
    env = {**os.environ, "CHEJIN_WORKER_HOME": str(tmp_path / "worker"), "CHEJIN_RPA_MODE": "mock", "GATE_TRANSPORT": transport}
    base = http_api.get("/healthz").url.removesuffix("/healthz")
    process = subprocess.run([sys.executable, str(script), base, json.dumps(worker), target["conversation_id"], "normal"],
                             env=env, text=True, capture_output=True, timeout=45)
    (tmp_path / "worker.stdout").write_text(process.stdout)
    (tmp_path / "worker.stderr").write_text(process.stderr)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    (tmp_path / "worker-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["sent"] == PARTS[:1], result
    if transport != "normal":
        assert result["pending_c2_outbox"], result
        recovery = tmp_path / "recover.py"
        recovery.write_text(RECOVER)
        recovered = subprocess.run([sys.executable, str(recovery), base], env=env, text=True, capture_output=True, timeout=35)
        (tmp_path / "recovery.stdout").write_text(recovered.stdout)
        (tmp_path / "recovery.stderr").write_text(recovered.stderr)
        assert recovered.returncode == 0, recovered.stderr
        settled = json.loads(recovered.stdout.strip().splitlines()[-1])
        result["exchanges"].extend(settled["exchanges"])
        result.update({key: settled[key] for key in ("runtime", "pending_c2_outbox", "pending_ack", "locked")})
    ingests = [item for item in result["exchanges"] if item["path"].endswith("/messages/ingest")]
    gates = [item for item in ingests if item["request"]["evidence"].get("flow_gate_errors")]
    assert gates and all(item["status"] == 200 for item in gates), result
    gate = gates[-1]
    assert gate["request"]["messages"] == []
    assert any(item["request"]["messages"] and item["request"]["read_run_id"] == gate["request"]["read_run_id"]
               for item in ingests), ingests
    assert not result["pending_c2_outbox"] and not result["pending_ack"]
    assert not result["runtime"]["inflight_flow_id"] and not result["locked"], result
    with SessionLocal() as db:
        state = db.get(Worker, worker["id"]).inflight_flow_state
        assert state.get("status") not in {"active", "draining"}, state
        facts = list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id == target["conversation_id"])))
        assert any(item.content == "请详细介绍看车安排" for item in facts)
        assert not any(item.content == "请详细介绍看车安排及费用" for item in facts)
        assert len(list(db.scalars(select(SentAck)))) == 1
        assert len([item for item in db.scalars(select(ReplyAction)) if item.status == "sent"]) == 1


RECOVER = r'''
import json,sys,time
from test_task_runner import FakeBridge
from chejin_worker_client.models import RpaResult
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import load_binding,load_runtime_control,has_pending_c2_outbox,has_pending_reply_send_ack_outbox
from chejin_worker_client.ui_lock import lock_summary
api=WorkerApiClient(sys.argv[1]+'/api');binding=load_binding();assert binding.run_status=='faulted'
wire=[];native=api.session.send
def send(request,**kwargs):
 response=native(request,**kwargs)
 wire.append({'path':request.url.split('/api')[-1],'status':response.status_code,'response':response.json(),'request':json.loads(request.body) if request.body else None})
 return response
api.session.send=send
bridge=FakeBridge(RpaResult(ok=True,result_code='unused'))
errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append)
runner.start(binding)
deadline=time.monotonic()+20
while time.monotonic()<deadline:
 if not has_pending_c2_outbox() and not load_runtime_control()['inflight_flow_id']:break
 time.sleep(.1)
runner.stop_event.set()
for thread in (runner.thread,runner.c2_thread,runner.thread_monitor):
 if thread:thread.join(5)
assert not bridge.sent_replies and not bridge.message_reads, (bridge.sent_replies,bridge.message_reads)
assert load_binding().run_status=='faulted'
print(json.dumps({'exchanges':wire,'runtime':load_runtime_control(),'pending_c2_outbox':has_pending_c2_outbox(),'pending_ack':has_pending_reply_send_ack_outbox(),'locked':lock_summary()['locked'],'errors':errors},default=str))
'''
