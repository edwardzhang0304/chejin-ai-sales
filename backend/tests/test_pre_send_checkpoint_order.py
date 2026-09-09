"""Synthetic frame inputs; real HTTP/DB/C3 and the unchanged Worker checkpoint gate.

No customer evidence, external model request, or physical WeChat send is used.
The AI adapter is the existing controlled test adapter. Generation must happen
automatically after ingestion, before any checkpoint assertions are evaluated.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import test_c3_api as api
from test_lead_followup_eligibility import http_api, isolated_db
from app.core.database import SessionLocal, engine
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction
from app.models.base import utcnow
from app.models.task import Task
from app.models.wechat import MessageEvent
from app.services import c3_service


@pytest.fixture
def async_generation(monkeypatch, record_property):
    """Use the production asynchronous route; replace only the model boundary.

    Wrappers observe and delegate scheduling/execution. The negative case can
    suppress the scheduler, but never creates a reply on the test's behalf.
    """
    from app.api.routes import wechat
    from app.core.config import get_settings
    from app.services.ai_adapter import MockOmniAutoAIEngineAdapter
    from starlette.background import BackgroundTask, BackgroundTasks

    monkeypatch.setattr(get_settings(), "c3_ai_adapter_mode", "real")
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", MockOmniAutoAIEngineAdapter)
    counts = {"scheduled": 0, "executed": 0, "generated": 0}
    control = {"suppress": False, "counts": counts}
    generate = wechat._generate_message_batch
    add_task = BackgroundTasks.add_task
    call = BackgroundTask.__call__

    def observed_generate(*args, **kwargs):
        counts["generated"] += 1
        return generate(*args, **kwargs)

    def observed_schedule(self, func, *args, **kwargs):
        if func is observed_generate:
            counts["scheduled"] += 1
            if control["suppress"]:
                return None
        return add_task(self, func, *args, **kwargs)

    async def observed_execution(self):
        if self.func is observed_generate:
            counts["executed"] += 1
        return await call(self)

    monkeypatch.setattr(wechat, "_generate_message_batch", observed_generate)
    monkeypatch.setattr(BackgroundTasks, "add_task", observed_schedule)
    monkeypatch.setattr(BackgroundTask, "__call__", observed_execution)
    yield control
    for name, count in counts.items():
        record_property("background_" + name, count)


class FrameInputHTTP:
    """Reuse the existing legal ingest fixture, adding synthetic visible history.

    Only request input changes. No route, response, generation, or checkpoint
    function is replaced. Historical events retain their original projection.
    """

    def __init__(self, http):
        self.http = http
        self.visible = []
        self.previous = []
        self.previous_frame_id = ""

    def get(self, path, **kwargs):
        return self.http.get(path, **kwargs)

    def post(self, path, **kwargs):
        if path.endswith("/wechat/messages/ingest"):
            payload = deepcopy(kwargs["json"])
            message = payload["messages"][0]
            observation = message["raw_payload"]["observation"]
            self.visible.append(deepcopy(observation))
            payload["evidence"]["observations"] = deepcopy(self.visible)
            projection = api.normalized_business_message_sequence(
                self.visible, message_viewport_bounds=None
            )[-1]
            message["raw_payload"]["business_projection"] = projection
            # V3 physical slots are one-based; the business projection is zero-based.
            message["message_position"]["screen_order"] = len(self.visible)
            payload["evidence"]["slot_ledger_states"][0]["screen_order"] = len(self.visible)
            payload["evidence"]["slot_ledger_states"] = [{
                "observation_id": o["observation_id"], "screen_order": i + 1,
                "order_source": "observation_index_fallback", "row_kind": "text_bubble",
                "source_message_key": o["source_message"]["id"],
                "origin_read_run_id": "read-" + o["source_message"]["id"],
                "fact_scope": "historical", "delivery_state": "backend_confirmed",
                "item_state": "completed",
            } for i, o in enumerate(self.visible[:-1])] + payload["evidence"]["slot_ledger_states"]
            if self.previous:
                previous_indexes = {o["_worker_stable_id"]: i for i, o in enumerate(self.previous)}
                alignment = payload["evidence"]["sequence_alignment_evidence"]
                alignment.update({
                    "pre_sequence_source": "checkpoint",
                    "pre_frame_id": self.previous_frame_id,
                    "alignment_status": "unique", "candidate_alignment_count": 1,
                    "matched_pairs": [{
                        "identity_state": "committed", "worker_stable_id": o["_worker_stable_id"],
                        "pre_observation_id": o["observation_id"], "post_observation_id": o["observation_id"],
                        "pre_index": previous_indexes[o["_worker_stable_id"]], "post_index": i,
                        "match_basis": "stable_business_projection",
                    } for i, o in enumerate(self.visible[:-1])],
                })
            self.previous = deepcopy(self.visible)
            self.previous_frame_id = f"frame:{payload['read_run_id']}"
            kwargs["json"] = payload
        return self.http.post(path, **kwargs)


WORKER_PROBE = r'''
import json, sys
from pathlib import Path
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, WechatReadTarget
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import load_c2_state
from chejin_worker_client.pre_send_checkpoint import compare_checkpoint_to_observations

request = json.loads(Path(sys.argv[1]).read_text())
api = WorkerApiClient(request["base_url"] + "/api")
binding = Binding(**request["binding"])
status = api.get_wechat_message_batch(binding, request["batch_id"])
target = WechatReadTarget.from_api(request["target"])
# Only invoke the production checkpoint entry: no background loops or UI bridge.
runner = TaskRunner.__new__(TaskRunner)
result = runner._bind_pre_send_fact_checkpoint(status=status, target=target)
if result.get("ok"):
    comparison = compare_checkpoint_to_observations(
        status["pre_send_fact_checkpoint"], request["observations"],
        before_frame_id="checkpoint:http", after_frame_id="synthetic:pre-send",
        current_tail_complete=True,
    )
    repeated = runner._bind_pre_send_fact_checkpoint(status=status, target=target)
else:
    comparison = {}; repeated = {}
saved = load_c2_state("pre_send_fact_checkpoint:" + status["reply_action"]["id"])
print(json.dumps({"binding":result,"comparison":comparison,
                 "repeated":repeated,"saved":saved}))
'''


@pytest.mark.parametrize("frame_source", ["initial_read", "final_read"])
@pytest.mark.parametrize("remaining_history", [1, 3])
def test_shifted_history_reaches_existing_worker_over_http(
    http_api, monkeypatch, tmp_path, frame_source, remaining_history, async_generation
):
    _assert_shifted_history(http_api, monkeypatch, tmp_path, frame_source, remaining_history)
    assert async_generation["counts"] == {"scheduled": 1, "executed": 1, "generated": 1}


def test_missing_async_scheduler_cannot_pass_the_positive_case(
    http_api, monkeypatch, tmp_path, async_generation
):
    async_generation["suppress"] = True
    with pytest.raises(AssertionError, match="automatic reply task missing"):
        _assert_shifted_history(http_api, monkeypatch, tmp_path, "initial_read", 1)
    assert async_generation["counts"] == {"scheduled": 1, "executed": 0, "generated": 0}
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        assert not list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))


def _assert_shifted_history(
    http_api, monkeypatch, tmp_path, frame_source, remaining_history
):
    inputs = FrameInputHTTP(http_api)
    monkeypatch.setattr(api, "client", inputs)
    worker, binding = api._setup_bound_conversation()
    with SessionLocal() as db:
        handoff = HandoffEvent(conversation_id=binding["conversation_id"],
                               handoff_reason_code="AI_ENGINE_RETRY_EXHAUSTED", notify_status="succeeded")
        db.add(handoff)
        db.commit()
        handoff_id = handoff.id
    event_ids = []
    for index in range(6):
        event_ids.append(api._ingest(
            worker, binding["conversation_id"], f"synthetic-history-{index}",
            f"测试历史消息{index}", authoritative_frame_source=frame_source,
        ))
    with SessionLocal() as db:
        before = {event_id: deepcopy(db.get(MessageEvent, event_id).raw_payload)
                  for event_id in event_ids}
        assert [before[x]["business_projection"]["screen_order"] for x in event_ids] == list(range(6))
        assert not list(db.scalars(select(ReplyAction)))
    # Historical top rows leave the viewport; the remaining history moves up.
    inputs.visible = inputs.visible[-remaining_history:]
    for index in range(6):
        api._ingest(worker, binding["conversation_id"], f"synthetic-new-{index}",
                    f"新的需求消息{index}", authoritative_frame_source=frame_source)
    with SessionLocal() as db:
        # Fixture setup only: the conversation now waits for a customer reply.
        db.get(Conversation, binding["conversation_id"]).status = "waiting_user_reply"
        db.get(HandoffEvent, handoff_id).deleted_at = utcnow()
        db.commit()
    api._ingest(worker, binding["conversation_id"], "synthetic-final-question",
                "想看看十万元以内的车", authoritative_frame_source=frame_source)
    deadline = time.monotonic() + 10
    while True:
        with SessionLocal() as db:
            actions = list(db.scalars(select(ReplyAction)))
            tasks = list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))
            if actions:
                batch_id = actions[0].batch_id
                action_id = actions[0].id
        if actions or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    # No generate_for_batch/_generate call may create the expected task for us.
    assert len(actions) == len(tasks) == 1, "automatic reply task missing"
    assert tasks[0].reply_action_id == action_id
    response = http_api.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=api._worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    status = response.json()["data"]
    checkpoint = status["pre_send_fact_checkpoint"]
    tail = checkpoint["committed_tail"]
    current_ids = [o["_worker_stable_id"] for o in inputs.visible]
    assert [item["worker_stable_id"] for item in tail] == current_ids
    assert [item["business_projection"]["screen_order"] for item in tail] == list(range(len(current_ids)))
    with SessionLocal() as db:
        assert {event_id: db.get(MessageEvent, event_id).raw_payload for event_id in event_ids} == before
        assert db.get(MessageBatch, batch_id).ai_request_snapshot["pre_send_fact_checkpoint"] == checkpoint
        old_items = [db.get(MessageEvent, x) for x in event_ids[-remaining_history:]]
        for item, old in zip(tail, old_items):
            old_projection = deepcopy(old.raw_payload["business_projection"])
            old_projection["screen_order"] = item["business_projection"]["screen_order"]
            assert item["business_projection"] == old_projection
            assert item["message_identity_commit_record"] == old.raw_payload["message_identity_commit_record"]
    request_file = tmp_path / "worker-input.json"
    request_file.write_text(json.dumps({
        "base_url": response.url.split("/api/")[0],
        "binding": {"worker_id": worker["id"], "worker_token": worker["worker_token"],
                    "client_instance_id": "client-c3"},
        "batch_id": batch_id, "target": binding, "observations": inputs.visible,
    }))
    worker_root = Path(__file__).resolve().parents[2] / "worker-client"
    result = subprocess.run(
        [sys.executable, "-c", WORKER_PROBE, str(request_file)],
        env={**os.environ, "PYTHONPATH": str(worker_root),
             "CHEJIN_WORKER_HOME": str(tmp_path / "worker")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    worker_result = json.loads(result.stdout.splitlines()[-1])
    assert worker_result["binding"]["ok"] is True, worker_result
    assert worker_result["comparison"]["comparison_result"] == "checkpoint_equal", worker_result
    assert worker_result["repeated"]["restored"] is True
    assert worker_result["saved"]["checkpoint"] == checkpoint
    restarted = subprocess.run(
        [sys.executable, "-c", WORKER_PROBE, str(request_file)],
        env={**os.environ, "PYTHONPATH": str(worker_root),
             "CHEJIN_WORKER_HOME": str(tmp_path / "worker")},
        capture_output=True, text=True, timeout=30,
    )
    assert restarted.returncode == 0, restarted.stderr
    restored = json.loads(restarted.stdout.splitlines()[-1])
    assert restored["binding"]["ok"] is True and restored["binding"]["restored"] is True
    assert restored["saved"] == worker_result["saved"]
    # Claim-send returns the same frozen bytes; it must not regenerate history.
    claim = http_api.post(f"/api/tasks/{tasks[0].id}/claim", json={
        "worker_id": worker["id"], "current_step": "chat_reply_claimed",
        "claim_source": "c2_conversation_flow", "conversation_id": binding["conversation_id"],
    }, headers=api._worker_headers(worker))
    assert claim.status_code == 200, claim.text
    send = http_api.post(f"/api/reply-actions/{action_id}/claim-send", json={
        "worker_id": worker["id"], "task_id": tasks[0].id,
    }, headers=api._task_lease_headers(worker, claim))
    assert send.status_code == 200, send.text
    assert send.json()["data"]["pre_send_fact_checkpoint"] == checkpoint
    assert send.json()["data"]["pre_send_fact_checkpoint_binding"] == status["pre_send_fact_checkpoint_binding"]
    evidence = {
        "scope": "Synthetic frames; real HTTP, DB, automatic controlled AI, Worker subprocess and SQLite. No physical send.",
        "database": engine.dialect.name, "frame_source": frame_source,
        "original_history_orders": [before[x]["business_projection"]["screen_order"] for x in event_ids],
        "retained_history_orders": [before[x]["business_projection"]["screen_order"] for x in event_ids[-remaining_history:]],
        "checkpoint_orders": [x["business_projection"]["screen_order"] for x in tail],
        "historical_records_unchanged": True, "automatic_reply_count": len(actions),
        "automatic_task_count": len(tasks), "worker_binding": worker_result["binding"],
        "worker_comparison": worker_result["comparison"]["comparison_result"],
        "worker_sqlite_persisted": True, "claim_send_same_checkpoint": True,
        "worker_second_process_restored_same_sqlite": True,
    }
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))


def _historical_events():
    """Artificial committed text facts, with old per-frame positions."""
    events = []
    for index, order in enumerate([5, 1, 2]):
        observation_id = f"observation-{index}"
        stable_id = f"worker-message-{index}"
        observation = {
            "observation_id": observation_id, "row_kind": "text_bubble",
            "sender_role": "customer", "message_type": "text",
            "content_clean": f"Synthetic message {index}",
            "_worker_stable_id": stable_id,
        }
        commit = api.committed_identity_record(
            worker_stable_id=stable_id, commit_basis=api.MessageCommitBasis.NEW_SUFFIX,
            observation_id=observation_id, sender_role="customer", message_type="text",
            proof={"new_suffix_observation_id": observation_id},
        )
        projection = api.normalized_business_message_sequence(
            [observation], message_viewport_bounds=None
        )[0]
        projection["screen_order"] = order
        events.append(SimpleNamespace(
            id=f"event-{index}", source_message_key=f"source-{index}",
            sender_role="customer", message_type="text", error_code=None,
            content=observation["content_clean"], evidence={}, raw_payload={
                "worker_stable_id": stable_id, "source_message_key": f"source-{index}",
                "observation": observation, "business_projection": projection,
                "message_identity_commit_record": commit,
                "message_identity_runtime_evidence": {}, "strong_boundary_tokens": [],
            },
        ))
    events[-1].evidence = {
        "authoritative_frame_source": "initial_read",
        "observation_validation_errors": [],
        "observations": [deepcopy(e.raw_payload["observation"]) for e in events],
    }
    return events


def _freeze(events):
    tail = c3_service._checkpoint_tail_from_latest_complete_frame(events)
    batch = SimpleNamespace(id="synthetic-batch", conversation_id="synthetic-conversation")
    checkpoint = c3_service._build_pre_send_fact_checkpoint(
        batch=batch, ordered_messages=tail,
        authoritative_frame_source=c3_service._checkpoint_frame_source(tail),
        tail_complete=bool(tail),
    )
    batch.ai_request_snapshot = {"pre_send_fact_checkpoint": checkpoint}
    return batch, c3_service._pre_send_fact_checkpoint_response(
        batch, SimpleNamespace(id="synthetic-action")
    )


def _binding_error(response):
    return api.worker_checkpoint_binding_error(
        response["pre_send_fact_checkpoint"], response["pre_send_fact_checkpoint_binding"],
        conversation_id="synthetic-conversation", batch_id="synthetic-batch",
        reply_action_id="synthetic-action",
    )


@pytest.mark.parametrize("invalid_order", [None, -1, True, "5", 1.5])
def test_rebasing_does_not_repair_invalid_historical_projection(invalid_order):
    events = _historical_events()
    events[0].raw_payload["business_projection"]["screen_order"] = invalid_order
    _, response = _freeze(events)
    assert _binding_error(response), "Invalid source projection was silently repaired"


@pytest.mark.parametrize("invalid", ["missing_projection", "missing_commit", "wrong_role", "unknown_item", "duplicate_item", "untrusted_frame", "frame_error"])
def test_rebasing_keeps_identity_and_complete_frame_guards(invalid):
    events = _historical_events()
    evidence = events[-1].evidence
    if invalid == "missing_projection":
        events[0].raw_payload.pop("business_projection")
    elif invalid == "missing_commit":
        events[0].raw_payload.pop("message_identity_commit_record")
    elif invalid == "wrong_role":
        events[0].raw_payload["business_projection"]["sender_role"] = "self"
    elif invalid == "unknown_item":
        evidence["observations"][0]["_worker_stable_id"] = "missing"
    elif invalid == "duplicate_item":
        evidence["observations"].append(deepcopy(evidence["observations"][0]))
    elif invalid == "untrusted_frame":
        evidence["authoritative_frame_source"] = "partial_frame"
    else:
        evidence["observation_validation_errors"] = ["incomplete"]
    _, response = _freeze(events)
    assert _binding_error(response)


def test_rebased_checkpoint_stays_frozen_and_digest_guard_remains():
    events = _historical_events()
    before = deepcopy([e.raw_payload for e in events])
    batch, response = _freeze(events)
    assert _binding_error(response) == ""
    assert [e.raw_payload for e in events] == before
    events[0].raw_payload["business_projection"]["screen_order"] = 99
    assert c3_service._pre_send_fact_checkpoint_response(
        batch, SimpleNamespace(id="synthetic-action")
    ) == response
    altered = deepcopy(response)
    altered["pre_send_fact_checkpoint"]["committed_tail"][0]["business_projection"]["screen_order"] = 99
    assert _binding_error(altered) == "checkpoint_digest_mismatch"
    altered = deepcopy(response)
    altered["pre_send_fact_checkpoint_binding"]["reply_action_id"] = "other-action"
    assert _binding_error(altered) == "binding_reply_action_id_mismatch"


def test_old_frozen_bad_checkpoint_is_not_silently_rewritten_on_read():
    batch, response = _freeze(_historical_events())
    # Historical snapshot fixture: previous backend had already frozen order 5.
    batch.ai_request_snapshot["pre_send_fact_checkpoint"]["committed_tail"][0]["business_projection"]["screen_order"] = 5
    before = deepcopy(batch.ai_request_snapshot)
    response = c3_service._pre_send_fact_checkpoint_response(batch, SimpleNamespace(id="synthetic-action"))
    assert batch.ai_request_snapshot == before
    assert response["pre_send_fact_checkpoint"] == before["pre_send_fact_checkpoint"]
    assert _binding_error(response) == "checkpoint_item_invalid"
