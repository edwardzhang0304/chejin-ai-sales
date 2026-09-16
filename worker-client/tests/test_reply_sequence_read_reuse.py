"""Reuse rejects uncertain ownership, frame completeness and checkpoint changes."""
import copy
from types import SimpleNamespace

import pytest

from chejin_worker_client import reply_sequence_runtime as runtime
from chejin_worker_client.task_runner import TaskRunner
from test_task_runner import FakeBridge
from test_pre_send_checkpoint import _checkpoint, _fact


@pytest.mark.parametrize("changed", [
    "none", "flow", "lease", "lost_lease", "expired_lock", "fence", "no_frame",
    "new_customer", "failed_read", "invalidated", "history_gap", "guard",
    "unknown_source", "checkpoint", "unconfirmed_row", "suffix",
])
def test_reuse_requires_same_live_scope_and_equal_complete_checkpoint(monkeypatch, changed):
    fact = _fact("message-1", sender_role="customer", message_type="text", content="请介绍一下")
    observations = [copy.deepcopy(fact["_business_observation"])]
    guard = FakeBridge._send_context_guard(observations)
    checkpoint = _checkpoint(fact)
    target = SimpleNamespace(conversation_id="conv-1", raw={"pre_send_fact_checkpoint_context":{
        "checkpoint":checkpoint,"binding":{"checkpoint_digest":"a"*64}}})
    frame = {"ok":True,"frame_id":"captured-frame-1","observations":observations,
             "authoritative_frame_source":"final_read","ui_frame_invalidated":False,"send_context_guard":guard}
    observation = {"ok":True,"new_customer_message_count":0,"new_self_message_count":1,
                   "_reply_sequence_frame":frame,"send_context_guard":guard}
    lease = SimpleNamespace(lock_id="lock-1", fencing_token=7, lease_lost=False)
    lock = {"locked":True,"lock_id":"lock-1","fencing_token":7}
    flow = {"inflight_flow_id":"flow-1"}
    runner = TaskRunner.__new__(TaskRunner); runner.current_ui_lock=lease
    if changed == "flow": flow["inflight_flow_id"]="flow-2"
    if changed == "lease": runner.current_ui_lock=copy.copy(lease)
    if changed == "lost_lease": lease.lease_lost=True
    if changed == "expired_lock": lock["locked"]=False
    if changed == "fence": lock["fencing_token"]=8
    if changed == "no_frame": observation.pop("_reply_sequence_frame")
    if changed == "new_customer": observation["new_customer_message_count"]=1
    if changed == "failed_read": observation["ok"]=False
    if changed == "invalidated": frame["ui_frame_invalidated"]=True
    if changed == "history_gap": frame["history_gap"]=True
    if changed == "guard": frame["send_context_guard"]={}
    if changed == "unknown_source": frame["authoritative_frame_source"]="unknown"
    if changed == "checkpoint": target.raw["pre_send_fact_checkpoint_context"]["checkpoint"]=_checkpoint(_fact("other",sender_role="customer",message_type="text",content="已经换了话题"))
    if changed == "unconfirmed_row": observations[0]["sender_role"]="unknown"
    if changed == "suffix":
        observations.append(_fact("message-2",sender_role="customer",message_type="text",content="客户新问题")["_business_observation"])
        frame["send_context_guard"]=FakeBridge._send_context_guard(observations)
    monkeypatch.setattr(runtime,"load_runtime_control",lambda:flow)
    monkeypatch.setattr(runtime,"lock_summary",lambda:lock)
    # Comparison-only reuse must not allocate identities when it falls back.
    monkeypatch.setattr(runner,"_assign_sequence_new_suffix_identities",lambda **kw:pytest.fail("reuse allocated a message identity"))
    result=runtime.reuse_continuation_read(runner,target,observation,flow_id="flow-1",lease=lease)
    if changed == "none":
        assert result is not None
        assert result["pre_send_fact_checkpoint_comparison"]["comparison_result"]=="checkpoint_equal"
        assert result["new_self_message_count"]==0
        assert observation["new_self_message_count"]==1
    else:
        assert result is None
