"""Independent cases: real OCR/Worker/HTTP/PG/SQLite; synthetic desktop/model.

Only desktop pixels and physical OS I/O are controlled. No outcome, receipt,
decision classifier or background generator is replaced. Historical messages
are explicit setup input, never a test-created reply or recovery result.
"""
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__import__("os").environ["CHEJIN_FIX_ROOT"])
sys.path.insert(0, str(ROOT/"worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests"))
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
import dynamic_composer_desktop as fixture
from test_customer_interrupt_http import PersistentDesktop, OLD_REPLY, NEW_REPLY, FRAME_FACTORY
import test_c3_api as api_fixture
import test_wechat_c2_api as c2
from app.services.ai_adapter import AIEngineDecision
from app.services import c3_service
from app.models.worker import Worker
from chejin_worker_client import storage, task_runner, omniauto_vision
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.models import Binding, Task
from chejin_worker_client.message_identity_commit import MessageCommitBasis
from chejin_worker_client.ui_lock import lock_summary


@pytest.mark.parametrize("moment", ["after_input", "before_input"])
@pytest.mark.parametrize("segmented", [False, True])
def test_customer_interruption_completes_new_reply(http_api, monkeypatch, async_generation, tmp_path, moment, segmented):
    monkeypatch.setattr(storage,"APP_DIR",tmp_path/"worker")
    monkeypatch.setattr(storage,"DB_FILE",tmp_path/"worker/worker_client.sqlite3")
    monkeypatch.setattr(api_fixture,"client",http_api)
    calibration,frames=FRAME_FACTORY(reply=OLD_REPLY,movement=0,reduction=0,final_customer=True,new_kind="customer")
    class Desktop(PersistentDesktop):
        def capture(self, hwnd, *, label="frame", **kwargs):
            if label=="send_baseline" and moment=="before_input":
                self.customer_arrived=True
            return super().capture(hwnd,label=label,**kwargs)
    desktop=Desktop(monkeypatch,tmp_path/"desktop",calibration,frames)
    brain_calls=[]
    class Model:
        def generate_reply_decision(self,**kw):
            brain_calls.append(kw)
            new="十五万元" in json.dumps(kw.get("message_batch"),ensure_ascii=False)
            desktop.reply=NEW_REPLY if new else OLD_REPLY
            parts=[OLD_REPLY, "车辆情况和库存需要销售核实，不能仅凭文字保证车况。"*4]
            long_initial=segmented and not new
            return AIEngineDecision(decision="send_reply",reply_text=" ".join(parts) if long_initial else desktop.reply,
                guard_result="pass",raw_payload={"adapter":"controlled_model", **(
                    {"omniauto_brain_result":{"brain_plan":{"reply_segments":parts}}} if long_initial else {})})
    monkeypatch.setattr(c3_service,"get_ai_engine_adapter",Model)
    worker=api_fixture._create_worker();api_fixture._create_sales(worker["id"])
    api_fixture._create_lead(remark_code="CJMKZUTH")
    session=api_fixture._scan(worker,remark_code="CJMKZUTH")
    with api_fixture.SessionLocal() as db:
        conv=db.get(api_fixture.Conversation,session["conversation_id"])
        conv.friend_state="friend_active";conv.status="waiting_user_reply"
        db.commit()
    client=WorkerApiClient(http_api.get("/healthz").url.removesuffix("/healthz")+"/api")
    binding=Binding(worker["id"],worker["worker_token"],"client-c3",run_status="running")
    storage.save_binding(binding);client.set_run_status(binding,"running")
    heartbeat=http_api.post(f"/api/workers/{worker['id']}/heartbeat",headers=api_fixture._worker_headers(worker),
        json={"client_instance_id":"client-c3","run_status":"running","running_status":"idle",
              "rpa_component_status":"ready","wechat_status":"logged_in",
              "local_lock_summary":{"capabilities":{"reply_sequence_version":1,"pre_send_read_recovery_version":1}}})
    assert heartbeat.status_code==200,heartbeat.text
    bridge=RpaBridge();bridge.mode="real"
    sends=[];wire=[];ocr=[];errors=[]
    native_ocr=fixture.sidecar.run_ocr
    def counted_ocr(image):
        rows=native_ocr(image);ocr.append({"size":list(image.size),"rows":len(rows)});return rows
    monkeypatch.setattr(fixture.sidecar,"run_ocr",counted_ocr)
    native_wire=client.session.send
    def exchange(request,**kw):
        response=native_wire(request,**kw)
        wire.append({"method":request.method,"path":request.url.removeprefix(client.base_url),"status":response.status_code})
        return response
    monkeypatch.setattr(client.session,"send",exchange)
    def io(args,**kw):
        def option(name,default=""):return args[args.index(name)+1] if name in args else default
        if args[0]=="send":
            assert lock_summary()["locked"]
            value=fixture.sidecar.send_payload(calibration["hwnd"],{},target=option("--target"),text=option("--text"),exact=True,
                skip_send_rate_guard=True,artifact_dir=str(tmp_path/"desktop"),
                expected_context_guard=json.loads(option("--expected-context-guard","{}")),action_journal_path=option("--action-journal"))
            sends.append(value)
        else:
            assert args[0] in {"messages","open-chat"}
            value=fixture.sidecar.messages_payload(calibration["hwnd"],{"ok":True},target="CJMKZUTH",history_load_times=0,max_scroll_steps=0,max_snapshots=1,
                confirm_target="CJMKZUTH",confirm_exact=True,chat_fact_roi_ocr=True)
            assert value["ok"],value
            if args[0]=="open-chat":value={"ok":True,"guard":value["target_confirmation"],"initial_messages_snapshot":value,"state":"chat_target_confirmed"}
        return json.loads(json.dumps(fixture.sidecar.sanitize_sidecar_contract_output(value)))
    monkeypatch.setattr(bridge,"_call_omniauto",io)
    monkeypatch.setattr(bridge,"prepare_startup_layout_for_new_transaction",lambda **kw:{"ok":True,"layout_snapshot":calibration})
    monkeypatch.setattr(omniauto_vision,"vision_configuration_status",lambda:{"ready":True})
    seed=io(["messages"])
    committed=[c2._committed_test_observation(item,worker_sequence=i+1,commit_basis=MessageCommitBasis.NEW_SUFFIX,
        proof={"alignment_status":"not_required","old_tail_fully_consumed":True,"new_suffix_observation_id":item["observation_id"]}) for i,item in enumerate(seed["observations"])]
    seeded=c2._production_worker_payload_for_test(binding={**session,"id":session.get("binding_id",session.get("id"))},remark_code="CJMKZUTH",
        read_run_id="historical-audit",observations=committed,read_reason="waiting_user_reply")
    r=client.session.post(f"{client.base_url}/workers/{worker['id']}/wechat/messages/ingest",json=seeded,headers=api_fixture._worker_headers(worker))
    assert r.status_code==200,r.text
    batch_id=r.json()["data"]["message_batch"]["batch_id"]
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        with api_fixture.SessionLocal() as db:
            action=db.query(api_fixture.ReplyAction).filter_by(batch_id=batch_id,current=True,status="queued",segment_index=1).first()
            if action:
                assert action.reply_text==OLD_REPLY
                assert action.segment_count==(2 if segmented else 1)
                action_id=action.id;break
        time.sleep(.02)
    else:raise AssertionError("No automatically generated initial reply")
    assert async_generation["counts"]["generated"]==1
    if __import__("os").environ.get("CHEJIN_P1_DISABLE_AUTOMATIC_GENERATION") == "1":
        # External mutation run must FAIL the unchanged business assertion.
        # The initial reply exists; suppress only its interrupted successor.
        async_generation["suppress"] = True
    runner=task_runner.TaskRunner(client,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,
        on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append)
    runner.binding=binding
    status=client.get_wechat_message_batch(binding,batch_id)
    try:
        runner._execute_task(binding,Task.from_api(status["task"]),"pending")
        with api_fixture.SessionLocal() as db:
            record={"moment":moment,"ocr_calls":len(ocr),"sidecar":sends,"http":wire,"captures":desktop.captures,
                "brain_calls":len(brain_calls),"async":async_generation["counts"],"sent":desktop.enter_texts,"draft":desktop.draft,
                "action":db.get(api_fixture.ReplyAction,action_id).status,"conversation":db.get(api_fixture.Conversation,session["conversation_id"]).status,
                "handoffs":[h.handoff_reason_code for h in db.query(api_fixture.HandoffEvent)],
                "ack":[{"result":a.send_result,"phase":a.action_phase,"error":a.error_code} for a in db.query(api_fixture.SentAck)],
                "backend_flow":db.get(Worker,worker["id"]).inflight_flow_state,"local_flow":storage.load_runtime_control().get("inflight_flow_id"),
                "pending_ack":storage.has_pending_reply_send_ack_outbox(),"lock":lock_summary(),"errors":errors}
        (tmp_path/"audit-send.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
        assert record["ocr_calls"]>0,record
        assert record["sent"]==([NEW_REPLY] if segmented else []),record
        assert record["ack"][0]["phase"]=="not_attempted",record
        assert not record["pending_ack"] and not record["local_flow"] and not record["backend_flow"] and not record["lock"]["locked"],record
        assert record["handoffs"]==[], f"Unexpected sales handoff: {record['handoffs']}"
        if not segmented:
            targets=runner._fetch_read_targets(binding)
            assert len(targets)==1,targets
            runner._read_state_target_queue(binding,targets=targets)
        with api_fixture.SessionLocal() as db:
            actions=list(db.query(api_fixture.ReplyAction))
            outcome={"segmented":segmented,"moment":moment,"sent":desktop.enter_texts,
                "old_actions":[{"index":a.segment_index,"status":a.status,"current":a.current,"text":a.reply_text} for a in actions if a.batch_id==batch_id],
                "new_actions":[{"status":a.status,"text":a.reply_text} for a in actions if a.batch_id!=batch_id],
                "handoffs":db.query(api_fixture.HandoffEvent).count(),"brain_calls":brain_calls,
                "async":async_generation["counts"],"http":wire,
                "flow":storage.load_runtime_control().get("inflight_flow_id"),
                "pending_ack":storage.has_pending_reply_send_ack_outbox()}
        (tmp_path/"full-continuation.json").write_text(json.dumps(outcome,ensure_ascii=False,indent=2,default=str))
        assert outcome["sent"]==[NEW_REPLY],outcome
        assert len(outcome["new_actions"])==1 and outcome["new_actions"][0]["status"]=="sent",outcome
        if segmented:
            # Existing segment settlement records the attempted segment as
            # failed; queued tail segments are superseded. Do not rewrite
            # physical receipt history into the single-reply terminal shape.
            assert [a["status"] for a in sorted(outcome["old_actions"],key=lambda a:a["index"])]==["failed","superseded"],outcome
        else:
            assert outcome["old_actions"][0]["status"]=="superseded" and not outcome["old_actions"][0]["current"],outcome
        assert not outcome["handoffs"] and not outcome["flow"] and not outcome["pending_ack"],outcome
        assert len(brain_calls)==2 and async_generation["counts"]=={"scheduled":2,"executed":2,"generated":2},outcome
        assert "十五万元" in json.dumps(brain_calls[-1]["message_batch"],ensure_ascii=False),outcome
    finally:
        runner._stop_task_lease_guard()
        runner.stop_for_update(timeout_seconds=5)
