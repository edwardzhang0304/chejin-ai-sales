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
from reply_sequence_heartbeat import live_test_worker_heartbeat
from chejin_worker_client.models import Binding
from chejin_worker_client.ui_lock import lock_summary
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from reply_sequence_media_desktop import (
    MediaDesktop, install_native_image_probe, install_native_voice_desktop, IMAGE_SUMMARY,
    record_checkpoint_comparisons,
)

PARTS = [FIXTURE_PARTS[0].replace("已知的", "具体"), *FIXTURE_PARTS[1:]]

NEW_QUESTION="我的预算改成十五万元了"
NEW_REPLY="好的，按您的新预算安排。"

class SequenceModel:
    calls=0
    expected_fact=NEW_QUESTION
    def generate_reply_decision(self, **kwargs):
        SequenceModel.calls += 1
        if self.expected_fact in json.dumps(kwargs, ensure_ascii=False, default=str):
            return AIEngineDecision(decision="send_reply",reply_text=NEW_REPLY,guard_result="pass",
                raw_payload={"omniauto_brain_result":{"brain_plan":{"reply_segments":[NEW_REPLY]}}})
        return AIEngineDecision(decision="send_reply",reply_text=" ".join(PARTS),guard_result="pass",
            raw_payload={"omniauto_brain_result":{"brain_plan":{"reply_segments":PARTS}}})


@pytest.mark.parametrize("reuse", [True, False])
@pytest.mark.parametrize("kind", ["text", "voice", "image"])
def test_customer_arrives_after_continuation_read(http_api, monkeypatch, async_generation, tmp_path, reuse, kind):
    SequenceModel.calls = 0
    SequenceModel.expected_fact = IMAGE_SUMMARY if kind == "image" else NEW_QUESTION
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
        voice_click = MediaDesktop.voice_click
        right_click = MediaDesktop.right_click
        observe_menu = MediaDesktop.observe_menu

        def key(self, key):
            if key == 13:
                assert self.draft == (PARTS[0] if not self.sent else NEW_REPLY)
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
            rows=[("customer", "请详细介绍看车安排")]
            for i,t in enumerate(self.sent):
                rows.append(("self",t))
                if i==0 and getattr(self,"customer_arrived",False):
                    rows.append((kind if kind != "text" else "customer",NEW_QUESTION))
            for role,text in rows:
                if role in {"voice", "image"}:
                    image.paste(original.crop((320,407,356,443)), (320,y))
                    if role == "image":
                        with Image.open(Path(__file__).resolve().parents[2]/"website/assets/vehicles/vehicle-02.jpg") as picture:
                            image.paste(picture.convert("RGB").resize((192,128)), (370,y))
                        y += 152
                    else:
                        draw.rounded_rectangle((370,y,538,y+40),radius=6,fill=(237,237,237))
                        for radius in (6,12,18):
                            draw.arc((380-radius,y+20-radius,380+radius,y+20+radius),start=-55,end=55,fill=(35,35,35),width=2)
                        draw.text((493,y+11),'5"',font=ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc",16),fill=(25,25,25))
                        if self.transcribed:
                            draw.rounded_rectangle((370,y+46,650,y+91),radius=6,fill=(237,237,237))
                            draw.text((380,y+58),NEW_QUESTION,font=font,fill=(25,25,25))
                        y += 115 if self.transcribed else 64
                    continue
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
    desktop.kind=kind; desktop.arrived=False; desktop.transcribed=False; desktop.media_clicks=[]; desktop.menu_open=False
    install_native_voice_desktop(monkeypatch, desktop)
    image_calls = install_native_image_probe(monkeypatch, desktop, tmp_path) if kind == "image" else []
    voice_calls = []
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
    fixtures._create_lead(remark_code="CJMKZUTH"); session = fixtures._scan(worker, remark_code="CJMKZUTH")
    with SessionLocal() as db:
        conv = db.get(Conversation,session["conversation_id"])
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
        if args[0] == "voice-transcribe":
            value=sidecar.run_sidecar_cli(args)
            voice_calls.append({"stage":option("--voice-action-stage"),"result":value})
        elif args[0] == "send":
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
    record_checkpoint_comparisons(monkeypatch, runner, tmp_path)
    ablation = os.environ.get('CHEJIN_SEQUENCE_AFTER_READ_ABLATION', '')
    if ablation:
        assert ablation == 'checkpoint_boundary' and kind == 'image' and not reuse
        original_continuity = task_runner._image_flow_action_slot_continuity
        def without_checkpoint_boundary(**kwargs):
            kwargs.pop('checkpoint_boundary_tokens', None)
            return original_continuity(**kwargs)
        monkeypatch.setattr(task_runner, '_image_flow_action_slot_continuity', without_checkpoint_boundary)
    original_read = runner._read_one_wechat_target
    def traced_read(*args, **kwargs):
        read_steps.append(kwargs.get("current_step", "initial_read"))
        result=original_read(*args, **kwargs)
        if kwargs.get("current_step")=="reply_sequence_read" and not getattr(desktop,"customer_arrived",False):
            assert result.get("ok") and len(desktop.sent)==1
            desktop.customer_arrived=True
            desktop.arrived=True
            (tmp_path/"arrival.json").write_text(json.dumps({"after":"real_reply_sequence_read_returned", "previous_sent":desktop.sent.copy(),"frame":(result.get("_reply_sequence_frame") or {}).get("frame_id")},ensure_ascii=False,indent=2))
        return result
    monkeypatch.setattr(runner,"_read_one_wechat_target",traced_read)
    target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==session["conversation_id"])
    try: result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)
    finally: runner._stop_task_lease_guard()
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        receipts=list(db.scalars(select(SentAck)))
        record={"reuse":reuse,"result":result,"ocr_calls":ocr_calls,"ocr_count":len(ocr_calls),"reads":reads,"read_steps":read_steps,"sends":sends,"wire":wire,"captures":desktop.captures,"enters":desktop.enter_count,"sent":desktop.sent,"actions":[{"text":a.reply_text,"status":a.status} for a in actions],"receipts":[a.send_result for a in receipts],"handoffs":len(list(db.scalars(select(HandoffEvent)))),"backend_flow":db.get(Worker,worker["id"]).inflight_flow_state,"runtime":storage.load_runtime_control(),"pending_ack":storage.has_pending_reply_send_ack_outbox(),"lock":lock_summary(),"errors":errors}
    record["audit"]={"customer_arrived":getattr(desktop,"customer_arrived",False),"model_calls":SequenceModel.calls,"question":NEW_QUESTION,
        "kind":kind,"ablation":ablation,"voice_calls":voice_calls,"voice_clicks":desktop.media_clicks,"image_calls":image_calls,"background":async_generation["counts"]}
    (tmp_path/"ocr-chain.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    assert record["audit"]["customer_arrived"]
    assert record["handoffs"]==0, {"reason":"ordinary new customer message must not transfer to sales", "sent":desktop.sent,"handoffs":record["handoffs"],"send_states":[s["result"].get("state") for s in sends],"model_calls":SequenceModel.calls}
    assert desktop.sent==[PARTS[0],NEW_REPLY] and desktop.enter_count==2
    assert SequenceModel.calls==2
    assert async_generation["counts"]=={"scheduled":2,"executed":2,"generated":2}
    assert not record["backend_flow"] and not record["runtime"]["inflight_flow_id"]
    assert not record["pending_ack"] and not record["lock"]["locked"]
    assert not record["runtime"]["pause_requested"] and runner.binding.run_status == "running"
    if kind == "voice":
        assert [v["stage"] for v in voice_calls]==["prepare","execute"] and len(desktop.media_clicks)==1
    if kind == "image":
        assert len(image_calls)==1 and json.loads((tmp_path/"image-continuity.json").read_text())
from fastapi import Request
from app.core.auth import require_admin_auth

@pytest.fixture(autouse=True)
def audit_admin(monkeypatch):
    def auth(request: Request):
        request.state.auth_actor={"operator_id":"00000000-0000-0000-0000-000000000001","operator_name":"Independent audit","actor_type":"admin_account","session_id":"test"}
    monkeypatch.setitem(fixtures.app.dependency_overrides,require_admin_auth,auth)
