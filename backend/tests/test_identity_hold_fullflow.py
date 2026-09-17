"""Actual HTTP/PG lifecycle, synthetic legal protocol input; no OCR or model claim."""
from datetime import timedelta
import json
import time

import pytest
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
import test_wechat_c2_api as c2
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent, ReplyAction
from app.models.wechat import WechatSessionBinding
from app.core.database import SessionLocal


@pytest.mark.parametrize("reason",["C2_MESSAGE_HISTORY_GAP","MESSAGE_IDENTITY_UNCONFIRMED","MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"])
def test_all_temporary_identity_handoffs_recover_after_complete_read(http_api,monkeypatch,async_generation,tmp_path,reason):
    monkeypatch.setattr(c2,"client",http_api)
    worker=c2._create_worker();c2._create_sales(worker["id"]);c2._create_lead("审计身份恢复","13896676543")
    code=c2._pull_remark_code(worker)
    scan=http_api.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result",json=c2._scan_payload(code),headers=c2._worker_headers(worker))
    assert scan.status_code==200,scan.text
    binding=scan.json()["data"]["bindings"][0]
    endpoint=f"/api/workers/{worker['id']}/wechat/messages/ingest"
    auth_url=f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/read-authorization"
    history=[]
    def authorize(payload):
        auth=http_api.get(auth_url,headers=c2._worker_headers(worker))
        assert auth.status_code==200,auth.text
        auth=auth.json()["data"]
        payload["authorization_revision"]=auth["authorization_revision"]
        payload["read_reason"]=auth["read_reason"]
        payload["evidence"].update(read_reason=auth["read_reason"],authorization_read_reason=auth["read_reason"])
        return payload
    for index,kind in enumerate(["checkpoint_merge","stable_reread","stable_reread"]):
        payload=c2._v3_ingest_payload(binding,code,read_run_id=f"audit-hold-{index}",messages=[])
        payload["evidence"].update(flow_gate_errors=[reason],flow_gate_identity_key="audit-stable-same-object",
            recovery_attempt_kind=kind,flow_gate_details=[{"error_code":reason,"position_source":"position_unavailable",
            "gate_scope":"reply_suffix","min_screen_order":0,"max_screen_order":0,"boundary_relation":"unknown"}])
        payload=authorize(payload)
        response=http_api.post(endpoint,json=payload,headers=c2._worker_headers(worker))
        assert response.status_code==200,response.text
        with SessionLocal() as db:
            hold=dict(db.get(WechatSessionBinding,binding["id"]).recovery_hold or {})
            events=list(db.query(HandoffEvent))
            history.append({"stage":kind,"response":response.json(),"hold":hold,"handoff_count":len(events)})
            assert hold["recovery_attempt_count"]==index,history
            assert len(events)==(1 if index==2 else 0),history
            if events:handoff_id=events[0].id
        repeated=http_api.post(endpoint,json=payload,headers=c2._worker_headers(worker))
        assert repeated.status_code==200,repeated.text
        with SessionLocal() as db:
            assert db.get(WechatSessionBinding,binding["id"]).recovery_hold["recovery_attempt_count"]==index
            assert db.query(HandoffEvent).count()==(1 if index==2 else 0)
    # Only advance the scheduler clock; no state, receipt or message is repaired.
    with SessionLocal() as db:
        db.get(WechatSessionBinding,binding["id"]).next_read_due_at=utcnow()-timedelta(seconds=1)
        db.commit()
    targets=http_api.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets",headers=c2._worker_headers(worker))
    assert targets.status_code==200,targets.text
    target=next(t for t in targets.json()["data"]["targets"] if t["conversation_id"]==binding["conversation_id"])
    advertised=target["recoverable_handoff_reason_codes"]
    clean=c2._v3_ingest_payload(binding,code,read_run_id="audit-clean-read",messages=[c2._v3_message(
        "audit-new-customer",role="customer",message_type="text",content="你好，请问明天可以看车吗",screen_order=1)])
    # Behave exactly like the consumer: no recovery declaration when server
    # advertises none. Successful read/message is unchanged across both codes.
    if advertised:
        clean["evidence"]["recoverable_handoff_resolution"]={"version":1,"status":"latest_unreplied_turn_complete",
            "reason_codes":advertised,"identity_confirmed":True,"history_confirmed":True,"automatic_reread_performed":True}
    response=http_api.post(endpoint,json=authorize(clean),headers=c2._worker_headers(worker))
    assert response.status_code==200,response.text
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        with SessionLocal() as db:
            replies=db.query(ReplyAction).count()
            if replies:break
        if not response.json()["data"].get("message_batch"):break
        time.sleep(.02)
    with SessionLocal() as db:
        event=db.get(HandoffEvent,handoff_id)
        record={"reason":reason,"history":history,"advertised_recoverable_reasons":advertised,"clean_response":response.json(),
            "handoff_status":event.status,"closed_at":str(event.closed_at),"conversation":db.get(Conversation,binding["conversation_id"]).status,
            "reply_count":db.query(ReplyAction).count(),"async":async_generation["counts"]}
    (tmp_path/"audit-hold.json").write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    assert record["handoff_status"]=="auto_recovered_clean_read",record
    assert record["reply_count"]==1 and record["async"]["generated"]==1,record
