"""Real HTTP, PostgreSQL and Worker SQLite; only desktop/model boundaries are controlled.

The first production Worker read creates history. The second reads the same
bubble with OCR whitespace drift plus a new customer question. Nothing calls
generate_for_batch or finishes a Flow on the Worker's behalf.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import func, select

from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services import c3_service
from app.services import wechat_service
from app.services.ai_adapter import AIEngineDecision
from app.api.routes import wechat as wechat_routes
from app.schemas.wechat import WechatMessageIngestRequest
from app.errors import AppError

ROOT = Path(__file__).resolve().parents[2]
OLD_TEXT = "测试车辆ZX 2026款，售价12.8万。\n请问您的预算是多少？"
NEW_QUESTION = "请问有混动车型吗？"

WORKER = r'''
import json, sys, time, os, hashlib
from pathlib import Path
from unittest.mock import patch
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, RpaResult
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client import task_runner as module
from chejin_worker_client.storage import save_binding, load_binding, load_runtime_control, db_connection
from chejin_worker_client.ui_lock import lock_summary
base, worker, conv, frame_path, mode = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5]
api = WorkerApiClient(base + '/api')
binding = load_binding() or Binding(worker['id'], worker['worker_token'], 'followup-test', run_status='running')
save_binding(binding)
bridge = FakeBridge(RpaResult(ok=True, result_code='unused'))
bridge.get_messages_payloads = [json.loads(frame_path.read_text())]
errors, exchanges, injections = [], [], []
runner = TaskRunner(api, bridge, on_profile=lambda x: None, on_status=lambda x: None,
    on_step=lambda x: None, on_task=lambda x: None, on_result=lambda x: None, on_error=errors.append)
runner.binding = binding
if mode == 'legacy' or os.environ.get('CHEJIN_CJF9_USE_OLD_FINISH') == '1':
    namespace = dict(vars(module))
    legacy = Path(os.environ['CHEJIN_CJF9_LEGACY_METHOD'])
    exec(compile(legacy.read_text(), str(legacy), 'exec'), namespace)
    TaskRunner._finish_inflight_flow_locked = namespace['_finish_inflight_flow_locked']
send = api.session.send
def observe(request, **kwargs):
    body = json.loads(request.body) if request.body else None
    if mode.startswith('rejected') and request.url.endswith('/messages/ingest') and body and body.get('messages'):
        # Controlled transport corruption exercises a REAL backend rejection.
        # Worker identity logic and durable Outbox are not replaced.
        historical = next(s for s in body['evidence']['slot_ledger_states'] if s['fact_scope'] == 'historical')
        item = next(o for o in body['evidence']['observations'] if o['observation_id'] == historical['observation_id'])
        item['content_clean'] = '完全不同的历史正文'
        request.body = json.dumps(body, ensure_ascii=False).encode()
        request.headers['Content-Length'] = str(len(request.body))
    if mode == 'rejected_status_timeout' and request.url.endswith('/run-status') and body['run_status'] == 'faulted' and not injections:
        injections.append('fault_status_transport_timeout')
        raise TimeoutError('controlled fault-status transport interruption')
    response = send(request, **kwargs)
    if mode == 'rejected_status_lost_response' and request.url.endswith('/run-status') and body['run_status'] == 'faulted' and not injections:
        injections.append('accepted_fault_status_response_lost')
        raise TimeoutError('controlled loss after backend persisted fault state')
    if mode == 'restart_no_completion' and request.url.endswith('/messages/ingest') and response.status_code == 200:
        modified = response.json()
        modified['data'].pop('read_completion', None)
        response._content = json.dumps(modified).encode()
        injections.append('removed_read_completion_proof')
    if any(p in request.url for p in ('/messages/ingest', '/inflight-flow/', '/run-status', '/read-targets')):
        exchanges.append({'url': request.url, 'status': response.status_code, 'request': body, 'response': response.json()})
    return response
api.session.send = observe
original_save = module.save_binding
def fail_binding_once(value):
    if mode in {'rejected_save_failure', 'rejected_save_after'} and value.run_status == 'faulted' and not injections:
        injections.append('fault_status_save_failure')
        if mode == 'rejected_save_after':
            original_save(value)
        raise OSError('controlled SQLite save interruption')
    return original_save(value)
with patch.object(module, 'save_binding', side_effect=fail_binding_once):
    if mode.startswith('restart'):
        from dataclasses import replace
        with patch.object(module, 'CONFIG', replace(module.CONFIG, c2_enabled=False)):
            before_retry = {'runtime': load_runtime_control(), 'status': binding.run_status}
            runner.start(binding)
            deadline = time.monotonic() + 15
            while load_runtime_control()['inflight_flow_id'] and time.monotonic() < deadline:
                time.sleep(.1)
            runner.stop_for_update(timeout_seconds=5)
        result = {'restart': True}
    else:
        target = next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id == conv)
        try:
            result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=False)
        except Exception as exc:
            result = {'raised': type(exc).__name__, 'message': str(exc)}
        before_retry = {'runtime': load_runtime_control(), 'status': runner.binding.run_status,
                       'can_start': runner._can_start_new_flow(), 'finish_count': sum(e['url'].endswith('/inflight-flow/finish') for e in exchanges)}
        deadline = time.monotonic() + 6
        while runner._pending_flow_finish and time.monotonic() < deadline:
            runner._retry_pending_flow_finish(binding)
            time.sleep(.1)
with db_connection() as conn:
    outbox = []
    for r in conn.execute('SELECT outbox_id,status,last_error,read_run_id,payload_json FROM c2_ingest_outbox'):
        item = dict(r)
        item['payload_sha256'] = hashlib.sha256(item.pop('payload_json').encode()).hexdigest()
        outbox.append(item)
print(json.dumps({'result': result, 'before_retry': before_retry, 'runtime': load_runtime_control(),
    'status': runner.binding.run_status, 'saved_status': load_binding().run_status,
    'can_start': runner._can_start_new_flow(), 'locked': lock_summary().get('locked'),
    'outbox': outbox,
    'errors': errors, 'exchanges': exchanges, 'injections': injections,
    'physical_sends': len(bridge.sent_replies), 'reads': len(bridge.message_reads)}, ensure_ascii=False, default=str))
'''


CASES = [(mode, "synthetic") for mode in (
    "whitespace", "rejected", "rejected_status_timeout", "rejected_save_failure",
    "rejected_save_after", "rejected_status_lost_response", "legacy_restart", "legacy_no_completion")]
if os.environ.get("CHEJIN_CJF9_INCIDENT_EVIDENCE"):
    # Optional private evidence is never required/copied into the public repo.
    CASES += [(mode, "incident") for mode in ("whitespace", "legacy_restart")]


@pytest.mark.parametrize("mode,frame_source", CASES)
def test_historical_ocr_read_and_technical_finish(http_api, tmp_path, monkeypatch, mode, frame_source):
    worker, rows = fixture_rows()
    row = rows[0]
    conv = row["conversation_id"]
    calls = []
    scheduled = []
    add_task = BackgroundTasks.add_task

    def record_schedule(tasks, function, *args, **kwargs):
        if function is wechat_routes._generate_message_batch:
            scheduled.append("automatic_batch")
            if os.environ.get("CHEJIN_CJF9_DISABLE_AUTO_GENERATION") == "1":
                return  # Test-only ablation must make the positive fail.
        return add_task(tasks, function, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", record_schedule)
    if os.environ.get("CHEJIN_CJF9_USE_OLD_VALIDATOR") == "1":
        path = Path(__file__).parent / "fixtures/cjf9_20260911/backend_validate_before.py"
        namespace = dict(vars(wechat_service))
        exec(compile(path.read_text(), str(path), "exec"), namespace)
        monkeypatch.setattr(wechat_service, "_validate_non_delivered_frame_observations", namespace["_validate_non_delivered_frame_observations"])

    class ControlledModel:
        def generate_reply_decision(self, **kwargs):
            calls.append("provider")
            return AIEngineDecision(decision="send_reply", reply_text="您好，请问您的预算是多少？",
                confidence=.95, guard_result="pass", evidence_refs=[], risk_flags=[],
                raw_payload={"adapter": "controlled test model"})

    monkeypatch.setattr(get_settings(), "c3_ai_adapter_mode", "real")
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", ControlledModel)
    with SessionLocal() as db:
        sales = Sales(sales_name="Synthetic salesperson", phone="13800009992", worker_id=worker["id"], enabled=True)
        db.add(sales)
        db.flush()
        db.get(Conversation, conv).sales_id = sales.id
        db.get(WechatSessionBinding, row["binding_id"]).sales_id = sales.id
        db.commit()
    url = http_api.get("/healthz").url.removesuffix("/healthz")
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "CHEJIN_WORKER_HOME": str(tmp_path / "worker-data"),
           "CHEJIN_RPA_MODE": "mock", "CHEJIN_UI_LOCK_LEASE_SECONDS": "1",
           "CHEJIN_CJF9_LEGACY_METHOD": str(Path(__file__).parent / "fixtures/cjf9_20260911/worker_finish_before.py"),
           "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
               ("worker-client", "worker-client/tests", "worker-client/omniauto-rpa"))}

    def run(phase, frame, run_mode):
        path = tmp_path / (phase + ".json")
        path.write_text(json.dumps(frame, ensure_ascii=False))
        proc = subprocess.run([sys.executable, str(script), url, json.dumps(worker), conv, str(path), run_mode],
            env=env, capture_output=True, text=True, timeout=40)
        (tmp_path / (phase + ".stdout")).write_text(proc.stdout)
        (tmp_path / (phase + ".stderr")).write_text(proc.stderr)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        (tmp_path / (phase + "-evidence.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    history = [{"id": "history-self", "type": "text", "sender_role": "self", "content": OLD_TEXT}]
    seed = {"messages": history, "frame_id": "seed"}
    current = {"frame_id": "current", "messages": [
        {**history[0], "id": "current-self", "content": OLD_TEXT.replace("ZX 2026", "ZX2026")},
        {"id": "new-question", "type": "text", "sender_role": "customer", "content": NEW_QUESTION}]}
    old_text, new_question, old_count = OLD_TEXT, NEW_QUESTION, 1
    if frame_source == "incident":
        folder = Path(os.environ["CHEJIN_CJF9_INCIDENT_EVIDENCE"])
        def saved_frame(filename):
            data = json.loads((folder / filename).read_text())
            observations = next(e["result"]["observations"] for e in data["events"] if "observations" in e.get("result", {}))
            # Frame-review JSON is a diagnostic projection, not a complete
            # transport object. Preserve captured OCR text/role/order/bounds;
            # the controlled desktop adapter builds the normal V3 envelope.
            return {"messages": [{"id": o["observation_id"], "sender_role": o["sender_role"],
                "type": o["message_type"], "content": o.get("content_clean", ""),
                "bubble_rect": o.get("bubble_rect")} for o in observations], "frame_id": filename}
        seed = saved_frame("019-wechat_messages_frame_review.json")
        current = saved_frame("004-wechat_messages_frame_review.json")
        old_count = len(seed["messages"])
        old_text = seed["messages"][-1]["content"]
        new_question = current["messages"][-1]["content"]
    before = run("before", seed, "seed")
    assert before["result"]["ok"], before
    with SessionLocal() as db:
        old_rows = list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id == conv)))
        assert len(old_rows) == old_count
        old_reply = next(e for e in old_rows if e.content == old_text)
        original_id, original_source = old_reply.id, old_reply.source_message_key
        # Test setup permits the next customer turn; no message/Flow/receipt is changed.
        for handoff in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id == conv)):
            handoff.deleted_at = utcnow()
        db.get(Conversation, conv).status = "waiting_user_reply"
        binding = db.get(WechatSessionBinding, row["binding_id"])
        binding.last_read_conversation_status = "waiting_user_reply"
        binding.next_read_due_at = utcnow() - timedelta(seconds=1)
        db.commit()
    if mode.startswith("legacy"):
        path = Path(__file__).parent / "fixtures/cjf9_20260911/backend_validate_before.py"
        namespace = dict(vars(wechat_service))
        exec(compile(path.read_text(), str(path), "exec"), namespace)
        with monkeypatch.context() as legacy_backend:
            legacy_backend.setattr(wechat_service, "_validate_non_delivered_frame_observations", namespace["_validate_non_delivered_frame_observations"])
            stuck = run("legacy", current, "legacy")
        assert stuck["runtime"]["inflight_flow_id"] and stuck["status"] == "running", stuck
        assert any(e["status"] == 409 for e in stuck["exchanges"] if e["url"].endswith("/inflight-flow/finish")), stuck
        assert not calls
        after = run("restart", current, "restart_no_completion" if mode == "legacy_no_completion" else "restart")
        assert after["before_retry"]["runtime"]["inflight_flow_id"] == stuck["runtime"]["inflight_flow_id"]
        assert {r["outbox_id"]: r["payload_sha256"] for r in after["outbox"]} == {r["outbox_id"]: r["payload_sha256"] for r in stuck["outbox"]}
    else:
        after = run("after", current, mode)
    finishes = [e for e in after["exchanges"] if e["url"].endswith("/inflight-flow/finish")]
    with SessionLocal() as db:
        owner = db.get(Worker, worker["id"])
        events = list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id == conv)))
        original = db.get(MessageEvent, original_id)
        assert original.content == old_text and original.source_message_key == original_source
        state = {"worker_status": owner.run_status, "flow": owner.inflight_flow_state,
                 "message_count": len(events), "provider_calls": len(calls), "scheduled": scheduled}
        (tmp_path / "backend-evidence.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        if mode == "whitespace" or mode.startswith("legacy"):
            if mode == "whitespace":
                assert after["result"]["ok"], after
                assert owner.run_status == after["status"] == "running"
            assert len(events) == old_count + 1 and sum(e.content == new_question for e in events) == 1
            if mode == "legacy_no_completion":
                assert owner.run_status == after["status"] == after["saved_status"] == "faulted", after
                assert not after["can_start"] and after["injections"] == ["removed_read_completion_proof"]
            # Check AUTOMATIC generation before any separate idempotency call.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                db.expire_all()
                if db.scalar(select(func.count()).select_from(ReplyAction).where(ReplyAction.conversation_id == conv)):
                    break
                time.sleep(.05)
            assert len(calls) == 1, "Automatic provider callback did not run exactly once"
            assert scheduled == ["automatic_batch"]
            assert db.scalar(select(func.count()).select_from(MessageBatch).where(MessageBatch.conversation_id == conv)) == 1
            assert db.scalar(select(func.count()).select_from(ReplyAction).where(ReplyAction.conversation_id == conv)) == 1
            assert db.scalar(select(func.count()).select_from(Task).where(Task.worker_id == worker["id"])) == 1
            assert all(r["status"] == "confirmed" for r in after["outbox"])
            # Focused validator checks against the real persisted rows. These
            # supplement (and are not counted as) the HTTP flow above.
            request = next(e["request"] for e in after["exchanges"] if e["url"].endswith("/messages/ingest"))
            protected = []
            mutations = ("other_text", "price_digit", "decimal_point", "role", "source_key", "conversation") if frame_source == "synthetic" else ("other_text", "role", "source_key", "conversation")
            for mutation in mutations:
                parsed = WechatMessageIngestRequest.model_validate(request)
                slot = next(s for s in parsed.evidence.slot_ledger_states if s.fact_scope == "historical")
                observation = next(o for o in parsed.evidence.observations if o["observation_id"] == slot.observation_id)
                if mutation == "other_text": observation["content_clean"] = "完全不同的历史正文"
                if mutation == "price_digit": observation["content_clean"] = observation["content_clean"].replace("12.8", "22.8")
                if mutation == "decimal_point": observation["content_clean"] = observation["content_clean"].replace("12.8", "128")
                if mutation == "role": observation["sender_role"] = "customer"
                if mutation == "source_key": slot.source_message_key = "source:never-persisted"
                if mutation == "conversation": parsed.conversation_id = rows[1]["conversation_id"]
                with pytest.raises(AppError) as rejected:
                    wechat_service._validate_non_delivered_frame_observations(db, parsed)
                assert rejected.value.code == "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
                protected.append({"mutation": mutation, "code": rejected.value.code})
            (tmp_path / "protected-history-validator.json").write_text(json.dumps(protected, indent=2))
        else:
            assert owner.run_status == after["status"] == after["saved_status"] == "faulted", after
            assert len(events) == old_count and not calls
            assert not after["can_start"] and any(r["status"] == "capability_paused" for r in after["outbox"])
            assert any(e["status"] == 409 and e["response"]["code"] == "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
                       for e in after["exchanges"] if e["url"].endswith("/messages/ingest")), after
            if mode != "rejected":
                assert len(after["injections"]) == 1 and after["before_retry"]["finish_count"] == 0, after
                assert not after["before_retry"]["can_start"], after
        assert not owner.inflight_flow_state, after
    assert len(finishes) == 1 and finishes[0]["status"] == 200, after
    assert finishes[0]["request"]["terminal_kind"] == ("technical_failed" if mode.startswith("rejected") or mode == "legacy_no_completion" else "read_confirmed")
    assert not after["runtime"]["inflight_flow_id"] and not after["locked"], after
    assert after["physical_sends"] == 0 and after["reads"] == (0 if mode.startswith("legacy") else 1)
