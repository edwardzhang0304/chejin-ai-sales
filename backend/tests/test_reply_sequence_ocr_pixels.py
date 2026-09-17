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

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import PARTS as FIXTURE_PARTS
from app.services.ai_adapter import AIEngineDecision
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

class SequenceModel:
    def generate_reply_decision(self, **kwargs):
        return AIEngineDecision(decision="send_reply",reply_text=" ".join(PARTS),guard_result="pass",
            raw_payload={"omniauto_brain_result":{"brain_plan":{"reply_segments":PARTS}}})


@pytest.mark.parametrize("reuse,new_friend", [(True, False), (False, False), (True, True)])
def test_three_segments_real_ocr_call_count(http_api, monkeypatch, async_generation, tmp_path, reuse, new_friend):
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

        def key(self, key):
            if key == 13:
                assert self.draft == PARTS[len(self.sent)]
                self.sent.append(self.draft)
                self.reply = self.draft
            super().key(key)

        def capture(self, hwnd, *, label="frame", **kwargs):
            image = original.copy()
            draw = ImageDraw.Draw(image)
            draw.rectangle((301,81,778,800), fill=(250,250,250))
            draw.rounded_rectangle((304,700,775,830), radius=9, fill=(250,250,250), outline=(224,224,224), width=2)
            image.paste(original.crop((305,801,774,829)), (305,801))
            y = 140
            for role, text in [("customer", "请详细介绍看车安排"), *[("self", t) for t in self.sent]]:
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
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", SequenceModel)
    worker = fixtures._create_worker(); fixtures._create_sales(worker["id"])
    fixtures._create_lead(remark_code="CJMKZUTH")
    if new_friend:
        # Real C1 completion establishes the initial activation state. Do not
        # replace the production friend-confirm HTTP or seed it as activated.
        from app.models.task import Task
        with SessionLocal() as db:
            friend_task = db.scalar(select(Task).where(Task.task_type == "add_friend")).id
        claim = http_api.post(f"/api/tasks/{friend_task}/claim", headers=fixtures._worker_headers(worker),
                              json={"worker_id": worker["id"]})
        assert claim.status_code == 200, claim.text
        done = http_api.post(f"/api/tasks/{friend_task}/invite-sent", headers=fixtures._task_lease_headers(worker, claim),
                             json={"remark": "Synthetic historical invitation completed"})
        assert done.status_code == 200, done.text
    session = fixtures._scan(worker, remark_code="CJMKZUTH")
    with SessionLocal() as db:
        conv = db.get(Conversation,session["conversation_id"])
        if new_friend:
            assert conv.friend_state == conv.status == "friend_request_sent"
        else:
            conv.friend_state = "friend_active"; conv.status = "waiting_user_reply"
        db.get(Worker,worker["id"]).local_lock_summary = {"capabilities":{"reply_sequence_version":1}}
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
    assert desktop.sent == PARTS and desktop.enter_count == 3, record
    assert record["receipts"] == ["sent"]*3 and record["handoffs"] == 0, record
    assert not record["backend_flow"] and not record["runtime"]["inflight_flow_id"] and not record["pending_ack"] and not record["lock"]["locked"], record
    assert read_steps.count("reply_sequence_read") == 2, read_steps
    assert read_steps.count("pre_send_refresh") == (1 if reuse else 3), read_steps
    assert all(s["ocr_count"] > 0 for s in sends), sends
    if new_friend:
        activation_calls = [call for call in wire if call["path"].endswith("/activation-confirm")]
        assert len(activation_calls) == 1 and activation_calls[0]["status"] == 200, wire
