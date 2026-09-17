"""Real RapidOCR/Sidecar/Worker/HTTP/PG; synthetic desktop pixels and model.

The private desktop fixture and original calibration stay outside the repo.
Set CHEJIN_SEQUENCE_DESKTOP_FIXTURE and CHEJIN_COMPOSER_INCIDENT explicitly.
This counts actual OCR calls; a mocked get_messages call is not OCR evidence.
"""
import importlib
import json
import os
from pathlib import Path
import sys
import time

import pytest
from conftest import authenticated_admin_dependency
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import PARTS as FIXTURE_PARTS
from test_partial_reply_recovery_provider import recovery_provider_factory
from app.services.ai_adapter import AIEngineDecision, RealOmniAutoAIEngineAdapter
import test_c3_api as fixtures
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, SentAck, HandoffEvent
from app.models.worker import Worker
from app.services import c3_service
from chejin_worker_client import task_runner, storage, rpa_bridge, omniauto_vision, reply_sequence_runtime
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.ui_lock import lock_summary
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar

PARTS = [FIXTURE_PARTS[0].replace("已知的", "具体"), *FIXTURE_PARTS[1:]]

RESUMED = "接着可以根据您的时间确认看车安排，车辆情况以现场核验为准。"

class SequenceModel:
    parts = PARTS

    def generate_reply_decision(self, **kwargs):
        return AIEngineDecision(decision="send_reply",reply_text=" ".join(self.parts),guard_result="pass",
            raw_payload={"omniauto_brain_result":{"brain_plan":{"reply_segments":self.parts}}})


@pytest.mark.parametrize("failure_after,question,resumed,initial_parts", [
    (0, "请详细介绍看车安排", RESUMED, PARTS),
    (1, "请详细介绍看车安排", RESUMED, PARTS),
    # Retain the separate r8 control and both unchanged original cases. r9's
    # real-successor acceptance uses case "1", including the original "核实";
    # this neutral-word control is not evidence that the intent bug is fixed.
    (1, "买二手车需要注意哪些事情？", "接着还可以了解车辆的保养维修记录，查看手续是否齐全，有疑问的地方再逐项核实。",
     [PARTS[0].replace("核实", "说明"), *PARTS[1:]]),
], ids=["0", "1", "provider_context"])
def test_unfinished_reply_remains_eligible_after_fresh_all_old_read(http_api, monkeypatch, async_generation, tmp_path, request, failure_after, question, resumed, initial_parts):
    reuse = True
    real_successor = os.environ.get("CHEJIN_RECOVERY_REAL_SUCCESSOR") == "1"
    if real_successor:
        request.getfixturevalue("recovery_provider_factory")(resumed)
    fixture_dir = os.environ.get("CHEJIN_SEQUENCE_DESKTOP_FIXTURE")
    if not fixture_dir:
        pytest.skip("requires explicitly supplied private desktop fixture")
    monkeypatch.setattr(storage, "APP_DIR", tmp_path / "worker")
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker/worker_client.sqlite3")
    monkeypatch.syspath_prepend(fixture_dir)
    desktop_fixture = importlib.import_module("dynamic_composer_desktop")
    assert desktop_fixture.sidecar is sidecar
    assert Path(sidecar.__file__).is_relative_to(Path(task_runner.__file__).parents[1] / "omniauto-rpa")
    calibration, paths = desktop_fixture.incident_inputs()
    original = Image.open(paths["before_input"]).convert("RGB")
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 14)

    class Desktop(desktop_fixture.Desktop):
        sent = []
        faults_enabled = True
        capture_errors = 0

        def key(self, key):
            if key == 13:
                assert self.draft == (initial_parts[len(self.sent)] if self.faults_enabled else resumed)
                self.sent.append(self.draft)
                self.reply = self.draft
            super().key(key)

        def capture(self, hwnd, *, label="frame", **kwargs):
            if self.faults_enabled and label == "send_baseline" and len(self.sent) == failure_after:
                self.capture_errors += 1
                raise OSError("audit: temporary screenshot failure before this segment is typed")
            image = original.copy()
            draw = ImageDraw.Draw(image)
            draw.rectangle((301,81,778,800), fill=(250,250,250))
            draw.rounded_rectangle((304,700,775,830), radius=9, fill=(250,250,250), outline=(224,224,224), width=2)
            image.paste(original.crop((305,801,774,829)), (305,801))
            y = 140
            for role, text in [("customer", question), *[("self", t) for t in self.sent]]:
                lines, line = [], ""
                for char in text:
                    if font.getlength(line + char) > 285:
                        lines.append(line); line = ""
                    line += char
                if line: lines.append(line)
                height = max(44, len(lines)*20+22)
                x = 390 if role == "self" else 370
                draw.rounded_rectangle((x,y,x+316,y+height), radius=5, fill=(157,242,155) if role == "self" else (237,237,237))
                image.paste(original.crop((722,130,758,166) if role == "self" else (320,407,356,443)), (722 if role == "self" else 320,y))
                for i, line in enumerate(lines): draw.text((x+12,y+10+i*20), line, font=font, fill=(25,25,25))
                y += height + 24
            assert y < 700
            if self.draft:
                # The simulated typing buffer supplies pixels, never OCR boxes.
                lines, line = [], ""
                for char in self.draft:
                    if font.getlength(line+char) > 425:
                        lines.append(line); line = ""
                    line += char
                if line: lines.append(line)
                for i, line in enumerate(lines): draw.text((320,710+20*i), line, font=font, fill=(25,25,25))
            path = self.directory / f"{len(self.captures):03d}-{label}.png"
            image.save(path)
            desktop_fixture.register(image, calibration, path)
            self.captures.append({"label":label,"path":str(path),"sent_count":len(self.sent),"draft":self.draft})
            return image, str(path)

    desktop = Desktop(monkeypatch, tmp_path / "desktop", calibration, {})
    desktop.sent = []
    ocr_calls, reads, sends, wire = [], [], [], []
    raw_ocr = sidecar.run_ocr
    def counted_ocr(image):
        rows = raw_ocr(image)
        ocr_calls.append({"capture":len(desktop.captures)-1,"size":list(image.size),"rows":len(rows),"sent_count":len(desktop.sent)})
        return rows
    monkeypatch.setattr(sidecar, "run_ocr", counted_ocr)
    monkeypatch.setattr(fixtures, "client", http_api)
    brain_inputs = []
    class RecoveryModel(SequenceModel):
        parts = initial_parts

        def generate_reply_decision(self, **kwargs):
            brain_inputs.append(kwargs)
            if len(brain_inputs) == 1:
                return super().generate_reply_decision(**kwargs)
            snapshot = kwargs["conversation_context"]["brain_context_snapshot"]
            from apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge import build_chejin_brain_context
            bridged = build_chejin_brain_context(brain_context_snapshot=snapshot,
                current_batch=kwargs["message_batch"]["messages"],
                expected_conversation_id=kwargs["conversation_context"]["conversation_id"])
            if failure_after:
                recovery = bridged["conversation_context"]["partial_reply_recovery"]
                assert len(recovery["confirmed_prefix"]) == failure_after
                assert "".join(recovery["confirmed_prefix"][0]["text"].split()) == "".join(initial_parts[0].split())
            if real_successor:
                # Executed by the real C2 BackgroundTasks generation callback,
                # never by test code after the automatic flow has finished.
                from dataclasses import asdict
                decision = RealOmniAutoAIEngineAdapter().generate_reply_decision(**kwargs)
                (tmp_path / "real-brain-result.json").write_text(json.dumps(asdict(decision), ensure_ascii=False, indent=2, default=str))
                return decision
            return AIEngineDecision(decision="send_reply", reply_text=resumed, guard_result="pass")
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", RecoveryModel)
    worker = fixtures._create_worker(); fixtures._create_sales(worker["id"])
    fixtures._create_lead(remark_code="CJMKZUTH"); session = fixtures._scan(worker, remark_code="CJMKZUTH")
    # Complete the historical C1 setup through its real API before the fault.
    from app.models.task import Task as BackendTask
    with SessionLocal() as db:
        friend_task = db.query(BackendTask).filter_by(task_type="add_friend").one().id
    friend_claim = http_api.post(f"/api/tasks/{friend_task}/claim", headers=fixtures._worker_headers(worker), json={"worker_id":worker["id"]})
    assert friend_claim.status_code == 200, friend_claim.text
    done = http_api.post(f"/api/tasks/{friend_task}/already-friend", headers=fixtures._task_lease_headers(worker, friend_claim), json={"remark":"Historical setup; already friends"})
    assert done.status_code == 200, done.text
    with SessionLocal() as db:
        conv = db.get(Conversation,session["conversation_id"])
        conv.friend_state = "friend_active"; conv.status = "waiting_user_reply"
        db.get(Worker,worker["id"]).local_lock_summary = {"capabilities":{"reply_sequence_version":1,"pre_send_read_recovery_version":1}}
        db.commit()
    api = WorkerApiClient(http_api.get("/healthz").url.removesuffix("/healthz") + "/api")
    binding = Binding(worker["id"],worker["worker_token"],"client-c3",run_status="running")
    storage.save_binding(binding); api.set_run_status(binding,"running")
    original_wire = api.session.send
    def record_wire(request, **kwargs):
        response = original_wire(request, **kwargs)
        wire.append({"path":request.url.split("/api")[-1],"status":response.status_code})
        return response
    monkeypatch.setattr(api.session, "send", record_wire)
    bridge = rpa_bridge.RpaBridge(); bridge.mode = "real"
    def physical_io(args, **kwargs):
        def option(name, default=""): return args[args.index(name)+1] if name in args else default
        start = len(ocr_calls)
        if args[0] == "send":
            assert lock_summary()["locked"]
            value = sidecar.send_payload(calibration["hwnd"],{},target=option("--target"),text=option("--text"),exact=True,skip_send_rate_guard=True,artifact_dir=str(tmp_path/"desktop"),expected_context_guard=json.loads(option("--expected-context-guard","{}")),action_journal_path=option("--action-journal"))
            sends.append({"result":value,"ocr_count":len(ocr_calls)-start})
        else:
            assert args[0] in {"messages","open-chat"}
            value = sidecar.messages_payload(calibration["hwnd"],{"ok":True},target="CJMKZUTH",history_load_times=0,max_scroll_steps=0,max_snapshots=1,confirm_target="CJMKZUTH",confirm_exact=True,chat_fact_roi_ocr="--chat-fact-roi-ocr" in args,expected_confirmed_self_text=option("--expected-confirmed-self-text"))
            (tmp_path/f"read-{len(reads)}.json").write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
            reads.append({"operation":args[0],"ocr_count":len(ocr_calls)-start,"sent_count":len(desktop.sent)})
            if args[0] == "open-chat": value={"ok":True,"guard":value["target_confirmation"],"initial_messages_snapshot":value,"state":"chat_target_confirmed"}
        return json.loads(json.dumps(sidecar.sanitize_sidecar_contract_output(value)))
    monkeypatch.setattr(bridge,"_call_omniauto",physical_io)
    monkeypatch.setattr(bridge,"prepare_startup_layout_for_new_transaction",lambda **kw:{"ok":True,"layout_snapshot":calibration})
    monkeypatch.setattr(omniauto_vision,"vision_configuration_status",lambda:{"ready":True})
    if not reuse:
        monkeypatch.setattr(reply_sequence_runtime,"reuse_continuation_read",lambda *a,**kw:None)
    errors=[]; read_steps=[]
    runner=task_runner.TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append)
    runner.binding=binding
    original_read = runner._read_one_wechat_target
    def traced_read(*args, **kwargs):
        read_steps.append(kwargs.get("current_step", "initial_read"))
        return original_read(*args, **kwargs)
    monkeypatch.setattr(runner,"_read_one_wechat_target",traced_read)
    target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==session["conversation_id"])
    try: result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
    finally: runner._stop_task_lease_guard()
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        receipts=list(db.scalars(select(SentAck)))
        record={"reuse":reuse,"result":result,"ocr_calls":ocr_calls,"ocr_count":len(ocr_calls),"reads":reads,"read_steps":read_steps,"sends":sends,"wire":wire,"captures":desktop.captures,"enters":desktop.enter_count,"sent":desktop.sent,"actions":[{"text":a.reply_text,"status":a.status} for a in actions],"receipts":[a.send_result for a in receipts],"handoffs":len(list(db.scalars(select(HandoffEvent)))),"backend_flow":db.get(Worker,worker["id"]).inflight_flow_state,"runtime":storage.load_runtime_control(),"pending_ack":storage.has_pending_reply_send_ack_outbox(),"lock":lock_summary(),"errors":errors}
    (tmp_path/"ocr-chain.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    assert desktop.sent == initial_parts[:failure_after] and desktop.capture_errors == 2, record
    assert record["receipts"] == ["sent"]*failure_after + ["failed"] and record["handoffs"] == 0, record
    assert not record["backend_flow"] and not record["runtime"]["inflight_flow_id"] and not record["pending_ack"] and not record["lock"]["locked"], record
    assert binding.run_status == "faulted", record
    if os.environ.get("CHEJIN_RECOVERY_DISABLE_SUCCESSOR") == "1":
        # Mutation check: leave initial work intact, suppress only automatic
        # recovery generation. The unchanged success assertions must fail.
        async_generation["suppress"] = True
    desktop.faults_enabled = False
    monkeypatch.setattr(bridge, "probe", lambda:("ready", "logged_in"))
    runner.start(binding)
    try:
        deadline = time.monotonic()+25
        while time.monotonic()<deadline and not runner.fault_recovery_state().get("ready"):
            time.sleep(.1)
        assert runner.fault_recovery_state().get("ready"), {"state":runner.fault_recovery_state(),"errors":errors}
        assert runner.set_run_status("running")
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and binding.run_status != "running": time.sleep(.05)
        assert binding.run_status == "running", errors
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and (runner.task_lock.locked() or lock_summary()["locked"] or storage.load_runtime_control().get("inflight_flow_id")):
            time.sleep(.05)
        targets = runner._fetch_read_targets(binding)
        target = next(t for t in targets if t.conversation_id == session["conversation_id"])
        # Production fresh reader and automatic backend scheduling, no manual
        # collection/generation/repair of either old batch or pending metadata.
        fresh_attempts = []
        deadline = time.monotonic() + 20
        while True:
            fresh = runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
            fresh_attempts.append({"ok": fresh.get("ok"), "error_code": fresh.get("error_code")})
            if fresh.get("error_code") != "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION" or time.monotonic() >= deadline:
                break
            # The real task thread may win admission immediately after the
            # snapshot above. Drive the next ordinary read turn, never bypass
            # its lock, suppress the thread, or retry an actual reading error.
            time.sleep(.1)
        from app.models.c3 import MessageBatch
        from app.models.wechat import WechatSessionBinding, MessageEvent
        with SessionLocal() as db:
            batches = [{"id":b.id,"status":b.status,"active":b.active} for b in db.query(MessageBatch)]
            saved = db.query(WechatSessionBinding).filter_by(conversation_id=session["conversation_id"]).one()
            evidence={"failure_after":failure_after,"question":question,"resumed":resumed,"real_successor":real_successor,"fresh":fresh,"batches":batches,
                "pending":saved.last_scan_snapshot.get("pre_send_read_pending"),"generation":async_generation["counts"],
                "messages":[{"role":m.sender_role,"content":m.content,"raw":m.raw_payload} for m in db.query(MessageEvent)],
                "sent":desktop.sent,"captures":desktop.captures,"wire":wire,"errors":errors,
                "brain_inputs":brain_inputs,"fresh_attempts":fresh_attempts,
                "backend_flow":db.get(Worker,worker["id"]).inflight_flow_state,
                "local_flow":storage.load_runtime_control().get("inflight_flow_id"),
                "pending_ack":storage.has_pending_reply_send_ack_outbox(),"lock":lock_summary(),
                "actions":[{"id":a.id,"batch_id":a.batch_id,"status":a.status,"text":a.reply_text} for a in db.query(ReplyAction)],
                "handoffs":db.query(HandoffEvent).count()}
        (tmp_path/"partial-recovery.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
        assert fresh.get("ok"), evidence
        assert len(batches) == 2, "Original partially/un-sent demand did not create a fresh batch: " + json.dumps(evidence,ensure_ascii=False,default=str)
        assert desktop.sent == initial_parts[:failure_after] + [resumed], evidence
        assert async_generation["counts"] == {"scheduled":2,"executed":2,"generated":2}, evidence
        assert evidence["handoffs"] == 0 and not evidence["backend_flow"] and not evidence["local_flow"], evidence
        assert not evidence["pending_ack"] and not evidence["lock"]["locked"], evidence
        assert len([a for a in evidence["actions"] if a["text"]==resumed and a["status"]=="sent"])==1, evidence
    finally:
        runner._stop_task_lease_guard()
        runner.stop_for_update(timeout_seconds=5)
