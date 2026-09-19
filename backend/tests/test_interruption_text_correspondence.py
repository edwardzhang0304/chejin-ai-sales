"""Independent compound scenario: real rules + HTTP/PG; OCR observations are fixtures.

No production-code patch. Desktop/cleanup evidence is supplied, not Windows-tested.
The failure expectation is business behavior, not a test-created reply or handoff.
"""
import copy
import json

import pytest

from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import isolated_db, http_api
from test_historical_alignment_flow import setup_case, frozen_checkpoint
from test_customer_interrupt_settlement import claimed_reply
from test_send_interruption import receipt
import test_task_runner as worker_fixtures
import test_c3_api as backend
from chejin_worker_client.task_runner import _bind_worker_continuity_contract_to_send_guard
from chejin_worker_client.pre_send_checkpoint import compare_checkpoint_to_observations
from chejin_worker_client.transaction_outcomes import classify_action_result
from chejin_worker_client.shared_rules import send_interruption
from apps.wechat_ai_customer_service.adapters import reply_sequence


def compound_receipt(*, target, ocr_difference, appended, stage='before_trigger'):
    checkpoint, before, with_ocr_error, _ = setup_case()
    frozen = frozen_checkpoint(checkpoint)
    compare = compare_checkpoint_to_observations(frozen, before,
        before_frame_id="before", after_frame_id="baseline", current_tail_complete=True,
        historical_checkpoint=checkpoint)
    assert compare["comparison_result"] == "checkpoint_equal", compare
    baseline = _bind_worker_continuity_contract_to_send_guard(
        worker_fixtures.production_send_context_guard(before, layout_ok=True), before,
        checkpoint=frozen, checkpoint_comparison=compare, empty_welcome_baseline=False,
        historical_checkpoint=checkpoint)
    current = copy.deepcopy(with_ocr_error if ocr_difference else before)
    if appended:
        row = worker_fixtures.TaskRunnerTest._ai_send_observation(
            "customer-new-question", sender_role="customer", content="那周末出游够用吗")
        row.update(bubble_rect=[100, 510, 450, 550], contract_errors=[])
        current.append(row)
    current_guard = worker_fixtures.production_send_context_guard(current, layout_ok=True)
    sidecar = worker_fixtures.production_sidecar_module()
    decision = sidecar.validate_send_context_guard(baseline, current_guard, current_observations=current)
    evidence = receipt()["evidence"]
    guard = evidence["guard"]
    guard["confirmed_target"] = target
    guard["send_baseline"] = {"send_context_guard": baseline}
    snapshot = {"ok": True,
        "validation": {"ok": True, "confirmed_target": target, "conversation_type": "private"},
        "send_context_guard": current_guard,
        "message_sequence": current,
        "frame_observation": {"frame_id": "current-frame"}}
    guard["visual"]["context_check"] = {**decision, "snapshot": snapshot,
        "frame_observation": {"frame_id": "current-frame"}}
    guard["visual"]["draft_clear"]["reason"] = "confirmed_program_draft_cleared"
    if stage == 'before_input':
        snapshot.update(screenshot_path='/fixture/current.png', input_region={'has_visible_text':False})
        evidence = {'state':'send_context_changed_before_input',
            'guard':{'ok':True,'confirmed_target':target,'conversation_type':'private',
                     'screenshot_path':snapshot['screenshot_path']},
            'action_journal':{'ok':True,'action_phase':'not_attempted'},
            'context_validation':{**decision,'expected_context_guard':baseline},
            'send_baseline':snapshot}
    return decision, evidence


@pytest.mark.parametrize("ocr_difference", [False, True])
def test_no_new_customer_message_still_passes_same_guard(tmp_path, ocr_difference):
    decision, _ = compound_receipt(target="C3TEST01", ocr_difference=ocr_difference, appended=False)
    (tmp_path / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2))
    assert decision["ok"], decision


@pytest.mark.parametrize("ocr_difference", [False, True])
@pytest.mark.parametrize('stage', ['before_input', 'before_trigger'])
def test_validated_customer_append_must_not_become_handoff(http_api, monkeypatch, tmp_path, ocr_difference, stage):
    monkeypatch.setattr(backend, "client", http_api)
    worker, binding, action_id, task_id, payload = claimed_reply()
    decision, evidence = compound_receipt(target=binding["remark_code"],
        ocr_difference=ocr_difference, appended=True, stage=stage)
    assert not decision["ok"]
    assert decision["continuity_relation"] == "unique_tail_append", decision
    payload["evidence"] = evidence
    classified = classify_action_result("send", {
        "action_phase": "not_attempted", "error_code": payload["error_code"], "evidence": evidence})
    worker_decision = send_interruption.confirmed_customer_interruption(
        send_result=classified["result"], action_phase=classified["action_phase"],
        error_code=classified["error_code"], evidence=evidence, target=binding["remark_code"])
    segmented_decision = reply_sequence.confirmed_customer_interruption(
        error_code=classified["error_code"], action_phase=classified["action_phase"], evidence=evidence)
    response = http_api.post(f"/api/reply-actions/{action_id}/sent-ack", json=payload,
                            headers=backend._worker_headers(worker))
    assert response.status_code == 200, response.text
    with backend.SessionLocal() as db:
        result = {"ocr_difference": ocr_difference, "stage":stage, "guard": decision,
            "worker_single_interruption": worker_decision,
            "worker_segmented_interruption": segmented_decision,
            "backend_task_status": db.get(backend.Task, task_id).status,
            "backend_action_status": db.get(backend.ReplyAction, action_id).status,
            "backend_conversation_status": db.get(backend.Conversation, binding["conversation_id"]).status,
            "backend_handoff_count": db.query(backend.HandoffEvent).count(),
            "limits": "Constructed OCR observations and clean-draft evidence; actual rules, HTTP and PostgreSQL. No physical send/model claim."}
    (tmp_path / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["backend_handoff_count"] == 0, result
    assert result["backend_task_status"] == "cancelled", result
    assert worker_decision and segmented_decision, result


@pytest.mark.parametrize('damage',['proof','observation','old_sequence','decision','customer_role','draft','target'])
def test_correspondence_claim_cannot_override_changed_or_unsafe_evidence(damage):
    _,evidence=compound_receipt(target='C3TEST01',ocr_difference=True,appended=True)
    guard=evidence['guard'];visual=guard['visual'];check=visual['context_check']
    if damage=='proof': check['worker_continuity_decision']['text_correspondence']['current']={}
    if damage=='observation': check['snapshot']['message_sequence'][1]['content_clean']='可以肯定装下'
    if damage=='old_sequence': guard['send_baseline']['send_context_guard']['sequence'][0]['normalized_content_signature']='changed'
    if damage=='decision': check['worker_continuity_decision']['matched_pairs'][0]['new_index']=1
    if damage=='customer_role': check['snapshot']['message_sequence'][-1]['sender_role']='sales'
    if damage=='draft': visual['draft_clear']['cleared']=False
    if damage=='target': guard['confirmed_target']='different'
    assert not send_interruption.confirmed_customer_interruption(send_result='failed',action_phase='not_attempted',
        error_code='C3_CONTEXT_CHANGED_BEFORE_SEND',evidence=evidence,target='C3TEST01')


from test_pre_send_checkpoint_order import async_generation


@pytest.mark.parametrize('segmented',[False,True])
def test_correspondence_interruption_reaches_automatic_new_reply(http_api,monkeypatch,async_generation,segmented):
    """Model/receipt boundaries controlled; real async generation and HTTP settlement."""
    import time
    from sqlalchemy import select
    from app.services import c3_service
    from app.services.ai_adapter import AIEngineDecision
    from app.models.worker import Worker
    from test_reply_sequence_http import PARTS, first_claim, ack
    calls=[]
    class Model:
        def generate_reply_decision(self,**kwargs):
            calls.append(kwargs)
            if len(calls)>1:
                return AIEngineDecision(decision='send_reply',reply_text='可以先核实具体车型的后备箱情况。',guard_result='pass')
            return AIEngineDecision(decision='send_reply',reply_text=' '.join(PARTS) if segmented else '我帮您核实看车安排。',
                guard_result='pass',raw_payload={'omniauto_brain_result':{'brain_plan':{'reply_segments':PARTS}}} if segmented else {})
    monkeypatch.setattr(backend,'client',http_api)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    worker,binding=backend._setup_bound_conversation()
    with backend.SessionLocal() as db:
        db.get(Worker,worker['id']).local_lock_summary={'capabilities':{'reply_sequence_version':1}}
        db.get(backend.Conversation,binding['conversation_id']).status='waiting_user_reply'
        db.commit()
    backend._ingest(worker,binding['conversation_id'],'old-question','请介绍看车安排')
    def actions():
        for _ in range(100):
            with backend.SessionLocal() as db:
                rows=list(db.scalars(select(backend.ReplyAction).where(backend.ReplyAction.status=='queued').order_by(backend.ReplyAction.segment_index)))
                if rows:return rows
            time.sleep(.02)
        raise AssertionError('No automatically generated reply')
    old=actions()
    assert len(old)==(3 if segmented else 1)
    flow='read-customer-appended'
    if segmented:
        with backend.SessionLocal() as db:
            generation=db.query(backend.WechatSessionBinding).filter_by(conversation_id=binding['conversation_id']).one().unread_generation
        started=http_api.post(f"/api/workers/{worker['id']}/inflight-flow/start",headers=backend._worker_headers(worker),
            json={'flow_id':flow,'flow_kind':'c2_read','conversation_id':binding['conversation_id'],'unread_generation':generation})
        assert started.status_code==200,started.text
        original_headers=backend._worker_headers
        monkeypatch.setattr(backend,'_worker_headers',lambda owner:{**original_headers(owner),'X-Inflight-Flow-Id':flow})
    claim=first_claim(http_api,worker,binding,old[0].id)
    _,evidence=compound_receipt(target=binding['remark_code'],ocr_difference=True,appended=True)
    r=http_api.post(f"/api/reply-actions/{old[0].id}/sent-ack",headers=backend._worker_headers(worker),json={
        'worker_id':worker['id'],'client_instance_id':'client-c3','task_id':claim['task_id'],
        'send_token':claim['send_token'],'reply_text_hash':claim['reply_text_hash'],
        'send_result':'failed','action_phase':'not_attempted','error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','evidence':evidence})
    assert r.status_code==200,r.text
    with backend.SessionLocal() as db:
        assert db.query(backend.HandoffEvent).count()==0
        assert db.get(backend.ReplyAction,old[0].id).status==('failed' if segmented else 'superseded')
        assert all(db.get(backend.ReplyAction,a.id).status in ('superseded','cancelled') for a in old[1:])
    if segmented:
        class SameFlowHTTP:
            def get(self,path,**kwargs):
                kwargs['headers']={**kwargs.get('headers',{}),'X-Inflight-Flow-Id':flow}
                return http_api.get(path,**kwargs)
            def post(self,path,**kwargs):
                kwargs['headers']={**kwargs.get('headers',{}),'X-Inflight-Flow-Id':flow}
                return http_api.post(path,**kwargs)
        monkeypatch.setattr(backend,'client',SameFlowHTTP())
    backend._ingest(worker,binding['conversation_id'],'customer-appended','那周末出游够用吗')
    new=actions()
    assert len(new)==1 and new[0].id not in {a.id for a in old}
    assert len(calls)==2 and '那周末出游够用吗' in json.dumps(calls[-1],ensure_ascii=False)
    new_claim=first_claim(http_api,worker,binding,new[0].id)
    result=ack(http_api,worker,new_claim)
    assert result['task']['status']=='completed'
    with backend.SessionLocal() as db:
        assert db.get(backend.ReplyAction,new[0].id).status=='sent'
        assert db.query(backend.SentAck).filter_by(send_result='sent').count()==1
        assert db.query(backend.HandoffEvent).count()==0
    assert async_generation['counts']=={'scheduled':2,'executed':2,'generated':2}
    if segmented:
        finished=http_api.post(f"/api/workers/{worker['id']}/inflight-flow/finish",
            headers={**backend._worker_headers(worker),'X-Inflight-Flow-Id':flow},
            json={'flow_id':flow,'terminal_kind':'read_confirmed','conversation_id':binding['conversation_id']})
        assert finished.status_code==200,finished.text
