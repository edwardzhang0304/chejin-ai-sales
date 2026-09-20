"""Scoped DI-07: real Worker/Sidecar/OCR, socket HTTP, PostgreSQL and SQLite.

Physical Windows input/capture and Brain generation are controlled. Historical
committed messages/approved reply are fixture preconditions. No test manually
settles a task, sends a receipt, clears a flow, or repairs production data.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[2]
SHARED=ROOT/"worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests"
sys.path.insert(0,str(SHARED))
from test_lead_followup_eligibility import http_api


@pytest.mark.parametrize("scenario",["sent","failed","unknown","response_loss"])
def test_worker_composer_http_settlement(tmp_path,request,monkeypatch,scenario):
    if os.environ.get("CHEJIN_COMPOSER_HTTP_CHILD") != "1":
        env={**os.environ,"CHEJIN_COMPOSER_HTTP_CHILD":"1",
             "CHEJIN_WORKER_HOME":str(tmp_path/"worker"),
             "CHEJIN_COMPOSER_WORKER_SOURCE":str(ROOT),
             "CHEJIN_C2_ENABLED":"false","CHEJIN_OBSERVABILITY_ENABLED":"false",
             "PYTHONDONTWRITEBYTECODE":"1",
             "PYTHONPATH":os.pathsep.join(str(ROOT/p) for p in ("backend","backend/tests","worker-client","worker-client/omniauto-rpa"))}
        result=subprocess.run([sys.executable,"-m","pytest",f"{__file__}::test_worker_composer_http_settlement[{scenario}]",
                               "-xq","--tb=short",f"--basetemp={tmp_path/'child'}"],
                              env=env,cwd=ROOT,text=True,capture_output=True,timeout=240)
        (tmp_path/"child.stdout").write_text(result.stdout)
        (tmp_path/"child.stderr").write_text(result.stderr)
        assert result.returncode == 0,result.stdout+result.stderr
        return

    _drive_composer_http(tmp_path, request, monkeypatch, scenario)


def controlled_send_process_started(args):
    """Mirror the real Popen boundary; RpaBridge itself records its completion."""
    from apps.wechat_ai_customer_service.adapters import send_launch_journal
    path = args[args.index('--action-journal') + 1]
    journal, _ = send_launch_journal.read(path)
    attempt = journal['send_launch_attempts'][-1]
    send_launch_journal.update(path, attempt['launch_attempt_id'],
                              allowed={'prepared'}, process_state='creating')


def _drive_composer_http(tmp_path, request, monkeypatch, scenario, *, expect_pending_ack=False, extra_sidecar_io=None, expected_send_calls=1):
    """Shared real-I/O harness; each caller asserts its business outcome."""
    import test_c3_api as backend
    import test_wechat_c2_api as c2
    from dynamic_composer_desktop import Desktop,derived_frames,REPLY,sidecar,send_context_from_args
    from chejin_worker_client import storage,omniauto_vision
    from chejin_worker_client.api import WorkerApiClient
    from chejin_worker_client.models import Binding,Task
    from chejin_worker_client.rpa_bridge import RpaBridge
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.message_identity_commit import MessageCommitBasis
    from chejin_worker_client.ui_lock import lock_summary
    from app.models.worker import Worker
    from sqlalchemy.schema import CreateSchema
    assert backend.engine.dialect.name == "postgresql"
    assert str(backend.engine.url.database) == "composer_test"
    assert Path(storage.DB_FILE).resolve().is_relative_to(Path("/private/tmp"))
    with backend.engine.begin() as connection:
        for schema in {t.schema for t in backend.Base.metadata.tables.values() if t.schema}:
            connection.execute(CreateSchema(schema,if_not_exists=True))
    backend.setup_function()
    http=request.getfixturevalue("http_api")
    base=http.get("/healthz").url.removesuffix("/healthz")+"/api"
    worker=backend._create_worker();backend._create_sales(worker["id"])
    backend._create_lead(remark_code="CJMKZUTH")
    session=backend._scan(worker,remark_code="CJMKZUTH")
    conversation_id=session["conversation_id"]
    with backend.SessionLocal() as db:
        conversation=db.get(backend.Conversation,conversation_id)
        conversation.friend_state="friend_active";conversation.status="waiting_user_reply";db.commit()
    calibration,frames=derived_frames(final_customer=True,new_kind="customer" if scenario=="failed" else "")
    desktop=Desktop(monkeypatch,tmp_path/"desktop",calibration,frames,unknown=scenario=="unknown")
    api=WorkerApiClient(base)
    binding=Binding(worker["id"],worker["worker_token"],"client-c3",run_status="running")
    api.set_run_status(binding,"running");storage.save_binding(binding)
    bridge=RpaBridge(sidecar_script=ROOT/"worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py")
    bridge.mode="real"
    errors=[];results=[];actions=[];events=[];loss=[];sends=[]
    original_send=api.session.send
    def wire(prepared,**kwargs):
        if scenario=="response_loss" and loss and prepared.url.endswith("/sent-ack"):
            raise __import__("requests").ConnectionError("controlled outage until restart")
        response=original_send(prepared,**kwargs)
        events.append({"method":prepared.method,"path":prepared.url.removeprefix(base),"status":response.status_code})
        if scenario=="response_loss" and prepared.url.endswith("/sent-ack") and response.status_code==200:
            loss.append("response lost after server commit")
            raise __import__("requests").ConnectionError("controlled response loss")
        return response
    monkeypatch.setattr(api.session,"send",wire)
    runner=TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,
                      on_task=lambda _:None,on_result=results.append,on_error=errors.append)
    runner.binding=binding
    def process_io(args,**kwargs):
        action=args[0];actions.append(action)
        def option(name,default=""):
            return args[args.index(name)+1] if name in args else default
        if action=="send":
            assert lock_summary()["locked"]
            controlled_send_process_started(args)
            payload=sidecar.send_payload(calibration["hwnd"],{},target=option("--target"),text=option("--text"),exact=True,
                skip_send_rate_guard=True,artifact_dir=str(tmp_path/"desktop"),
                expected_context_guard=send_context_from_args(args),
                action_journal_path=option("--action-journal"))
            sends.append(payload)
        elif extra_sidecar_io is not None and action not in {"messages", "open-chat"}:
            payload = extra_sidecar_io(args)
        else:
            assert action in {"messages","open-chat"},args
            payload=sidecar.messages_payload(calibration["hwnd"],{"ok":True},target="CJMKZUTH",history_load_times=0,
                max_scroll_steps=0,max_snapshots=1,confirm_target="CJMKZUTH",confirm_exact=True,chat_fact_roi_ocr=True)
            assert payload["ok"],payload
            if action=="open-chat":
                payload={"ok":True,"guard":payload["target_confirmation"],"initial_messages_snapshot":payload,"state":"chat_target_confirmed"}
        return json.loads(json.dumps(sidecar.sanitize_sidecar_contract_output(payload)))
    monkeypatch.setattr(bridge,"_call_omniauto",process_io)
    # Startup calibration / health are desktop environmental preconditions;
    # every business frame is still registered and measured by production code.
    monkeypatch.setattr(bridge,"prepare_startup_layout_for_new_transaction",lambda **kw:{"ok":True,"layout_snapshot":calibration})
    monkeypatch.setattr(omniauto_vision,"vision_configuration_status",lambda:{"ready":True})
    monkeypatch.setattr(bridge,"probe",lambda:("ready","logged_in"))
    # Normal startup heartbeat declares the real client's recovery capabilities.
    # Hold task admission until the approved-reply fixture below is ready.
    runner.can_pull_tasks=lambda:False
    runner.tick_once()
    assert not errors,errors
    runner.can_pull_tasks=lambda:True
    seed=process_io(["messages"])
    committed=[c2._committed_test_observation(item,worker_sequence=i+1,commit_basis=MessageCommitBasis.NEW_SUFFIX,
                  proof={"alignment_status":"not_required","old_tail_fully_consumed":True,"new_suffix_observation_id":item["observation_id"]})
               for i,item in enumerate(seed["observations"])]
    seed_binding={**session,"id":session["binding_id"]} if "binding_id" in session else session
    seeded=c2._production_worker_payload_for_test(binding=seed_binding,remark_code="CJMKZUTH",read_run_id="historical-composer",
                                                 observations=committed,read_reason="waiting_user_reply")
    response=api.session.post(f"{base}/workers/{worker['id']}/wechat/messages/ingest",json=seeded,headers=backend._worker_headers(worker))
    assert response.status_code==200,response.text
    # Real BackgroundTasks generates after returning HTTP. Wait only for the
    # initial approved-reply fixture; never invoke generation from the test.
    initial_batch_id=response.json()["data"]["message_batch"]["batch_id"]
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        with backend.SessionLocal() as db:
            if db.query(backend.ReplyAction).filter_by(batch_id=initial_batch_id, current=True, status="queued").count():
                break
        time.sleep(.02)
    else:
        raise AssertionError("Initial actual generation did not produce an approved reply")
    with backend.SessionLocal() as db:
        action=db.query(backend.ReplyAction).filter_by(conversation_id=conversation_id,current=True).one()
        # Approved Brain output is a setup fixture, never a settlement shortcut.
        action.reply_text=REPLY;action.reply_text_hash=backend.reply_text_hash(REPLY)
        action_id,batch_id=action.id,action.batch_id
        task_id=db.query(backend.Task).filter_by(reply_action_id=action_id).one().id
        db.commit()
    status=api.get_wechat_message_batch(binding,batch_id)
    runner._execute_task(binding,Task.from_api(status["task"]),"pending")
    initial={"pending_ack":storage.has_pending_reply_send_ack_outbox(),"flow":storage.load_runtime_control().get("inflight_flow_id"),
             "physical_enters":desktop.enter_count,"actions":actions,"errors":errors}
    record={"scenario":scenario,"boundary":"Controlled Windows I/O + Brain; real production send chain, RapidOCR, HTTP socket, PG, Worker SQLite",
            "initial":initial,"http":events,"sidecar_send":sends,"captures":desktop.captures}
    (tmp_path/"evidence.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    assert actions.count("send")==expected_send_calls,record
    assert desktop.enter_count==(0 if scenario=="failed" else 1),record
    if scenario=="response_loss":
        assert initial["pending_ack"] and len(loss)==1,record
        runner.stop_for_update(timeout_seconds=5)
        script=tmp_path/"restart.py"
        script.write_text('''
import json,sys,time
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client import storage
from chejin_worker_client.ui_lock import lock_summary
api=WorkerApiClient(sys.argv[1]);binding=storage.load_binding();calls=[]
class Desktop(RpaBridge):
    def probe(self):return 'ready','logged_in'
    def _call_omniauto(self,args,**kwargs):
        calls.append(args[0]);raise AssertionError('restart must not repeat physical send')
bridge=Desktop();bridge.mode='real'
runner=TaskRunner(api,bridge,on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=lambda _:None)
runner.start(binding)
deadline=time.monotonic()+15
while time.monotonic()<deadline:
    if not storage.has_pending_reply_send_ack_outbox() and not storage.load_runtime_control().get('inflight_flow_id'):break
    time.sleep(.1)
runner.stop_for_update(timeout_seconds=5)
print(json.dumps({'calls':calls,'pending_ack':storage.has_pending_reply_send_ack_outbox(),'flow':storage.load_runtime_control().get('inflight_flow_id'),'lock':lock_summary()},default=str))
''')
        restarted=subprocess.run([sys.executable,str(script),base],env=os.environ.copy(),cwd=ROOT,text=True,capture_output=True,timeout=30)
        (tmp_path/"restart.stdout").write_text(restarted.stdout);(tmp_path/"restart.stderr").write_text(restarted.stderr)
        assert restarted.returncode==0,restarted.stderr
        record["restart"]=json.loads(restarted.stdout.splitlines()[-1])
        assert record["restart"]["calls"]==[],record
        assert not record["restart"]["pending_ack"] and not record["restart"]["flow"],record
    elif expect_pending_ack:
        assert storage.has_pending_reply_send_ack_outbox(), record
    else:
        assert not storage.has_pending_reply_send_ack_outbox(),record
        assert not storage.load_runtime_control().get("inflight_flow_id"),record
    assert not lock_summary()["locked"],record
    with backend.SessionLocal() as db:
        receipts=db.query(backend.SentAck).filter_by(reply_action_id=action_id).all()
        record["backend"]={"receipts":[a.send_result for a in receipts],"task":db.get(backend.Task,task_id).status,
                            "action":db.get(backend.ReplyAction,action_id).status,"flow":db.get(Worker,worker["id"]).inflight_flow_state}
        assert [a.send_result for a in receipts]==["sent" if scenario=="response_loss" else scenario],record
        if not expect_pending_ack:
            assert not (record["backend"]["flow"] or {}).get("flow_id"),record
    (tmp_path/"evidence.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    return runner, desktop, record
