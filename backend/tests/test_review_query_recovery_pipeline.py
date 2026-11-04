"""Real ingest -> Brain/query/review -> persisted retry -> recovery -> send permit.

Synthetic model responses, local SQLite, in-process provider with JSON boundary,
database inventory; no production/customer calls or actual message sends.
"""
from copy import deepcopy
from datetime import timedelta
import json
import sys

import pytest
from sqlalchemy import select

import test_catalog_brain_joined as joined
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction
from app.models.task import Task
from app.models.vehicle import KnowledgeItem
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.c3_recovery import recover_due_message_batches_once

api, flow = joined.api, joined.flow
reviewer = sys.modules[brain.review_brain_reply_semantics.__module__]


@pytest.mark.parametrize("failure", ["schema", "budget_unit", "wrapper", "json", "review_timeout", "review_protocol"])
@pytest.mark.parametrize("recovers", [True, False])
def test_failure_recovers_or_hands_off_without_sending_unreviewed(monkeypatch, failure, recovers):
    api.setup_function()
    api._create_listed_vehicle()
    settings = get_settings()
    monkeypatch.setattr(settings, "c3_omniauto_root", str(joined.root / "worker-client/omniauto-rpa"))
    monkeypatch.setattr(settings, "c3_batch_recovery_max_attempts", 2)
    attempts, calls, receipts, inventory_reads = [], [], [], []
    state = {}
    query_failure_fields = {"schema": "sort", "budget_unit": "budget.unit", "wrapper": "catalog_query"}

    class DBRuntime:
        def __init__(self):
            with SessionLocal() as db:
                self.tenant_id = db.scalar(select(KnowledgeItem.tenant_id).where(KnowledgeItem.category_id == "products"))

        def list_items(self, kind):
            inventory_reads.append(state["attempt"])
            with SessionLocal() as db:
                return [x.payload for x in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id == kind))]

    original_attach = joined.cq.attach_receipt
    def attach(pack, receipt):
        state["receipt"] = receipt
        receipts.append(receipt)
        return original_attach(pack, receipt)
    monkeypatch.setattr(joined.cq, "attach_receipt", attach)
    monkeypatch.setattr(joined.ev, "KnowledgeRuntime", DBRuntime)
    monkeypatch.setattr(joined.legacy_ev, "build_evidence_pack", lambda *a, **k: {
        "evidence": {"products": []}, "safety": {"allowed_auto_reply": True, "must_handoff": False, "reasons": []}})
    monkeypatch.setattr(brain, "resolve_llm_api_key", lambda **_: "synthetic")
    monkeypatch.setattr(reviewer, "resolve_llm_api_key", lambda **_: "synthetic")

    def transport(**kwargs):
        attempt = state["attempt"]
        stage = kwargs["progress_stage"]
        calls.append((attempt, stage, deepcopy(kwargs["messages"])))
        broken = attempt == 1 or not recovers
        if failure in query_failure_fields and attempt > 1:
            feedback = state["invocation"]["target_state"].get("brain_retry_instruction", "")
            broken |= query_failure_fields[failure] not in feedback
        if stage == "semantic_reviewer":
            if broken and failure == "review_timeout":
                return {"ok": False, "status": 0, "error_kind": "transport", "error": "wall_timeout_25s"}
            payload = joined.review("query_summary")
            if broken and failure == "review_protocol":
                payload.pop("hard_boundary_concerns")
        elif not state.get("receipt"):
            mid = str(state["invocation"]["batch"][-1]["id"])
            q = joined.query(source_ids=[mid])
            q["budget"]["source_ids"] = [mid]
            q["budget"]["value"] = 200
            if broken and failure == "schema":
                q.pop("sort")
            elif broken and failure == "budget_unit":
                q["budget"].pop("unit")
            payload = q if broken and failure == "wrapper" else {"catalog_query": q}
            if failure == "json":
                # Only the existing repair request with the actual query contract
                # can recover. Plain regeneration and the old BrainPlan repair fail.
                repair_schema = json.loads(kwargs["messages"][-1]["content"]).get("schema", "") if stage.endswith("json_structure_repair") else ""
                can_repair = not broken and stage.endswith("json_structure_repair") and joined.cq.QUERY_FORMAT_INSTRUCTION in repair_schema
                if not can_repair:
                    return {"ok": True, "status": 200, "response_text": json.dumps(payload).replace('"catalog_query":', '"catalog_query"', 1)}
        else:
            receipt = state["receipt"]
            payload = joined.summary_plan(receipt)
            payload["facts_claimed"][0]["value"] = receipt["match_count"]
            payload["reply_segments"] = [f"按本轮预算条件，目前符合条件的在售车辆有{receipt['match_count']}辆。"]
        return {"ok": True, "status": 200, "response_text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(brain, "call_llm_request_with_failover", transport)
    monkeypatch.setattr(reviewer, "call_llm_request_with_failover", transport)
    config = joined.fixtures.base_config(joined.fixtures.base_plan())
    config["customer_service_brain"].update(mode="brain_first", provider="openai", api_key="synthetic", low_authority_fast_enabled=False,
                                             semantic_reviewer_cache_enabled=False)

    class Adapter(RealOmniAutoAIEngineAdapter):
        def _load_config(self):
            return deepcopy(config)

        def _run_brain_isolated(self, *, config, invocation, timeout_seconds):
            request = json.loads(json.dumps({"config": config, "invocation": invocation}))
            attempts.append(request)
            state.clear()
            state.update(attempt=len(attempts), invocation=request["invocation"])
            return json.loads(json.dumps(self._load_brain()(config=request["config"], **request["invocation"])))

    worker, binding, initial = flow.generate(monkeypatch, Adapter())
    bid = initial["batch"]["id"]
    expected = {"schema": "AI_ENGINE_CATALOG_QUERY_INVALID", "budget_unit": "AI_ENGINE_CATALOG_QUERY_INVALID", "wrapper": "AI_ENGINE_CATALOG_QUERY_INVALID",
                "review_timeout": "AI_ENGINE_REVIEW_TIMEOUT", "review_protocol": "AI_ENGINE_REVIEW_INVALID_RESPONSE",
                "json": "AI_ENGINE_RESPONSE_INVALID"}[failure]
    with SessionLocal() as db:
        diagnostics = db.get(MessageBatch, bid).ai_response_snapshot
    assert initial["decision"] == "retry_later" and initial["batch"]["error_code"] == expected, diagnostics
    with SessionLocal() as db:
        saved = db.get(MessageBatch, bid)
        assert saved.generation_attempt_count == 1
        assert not list(db.scalars(select(ReplyAction)))
        assert not list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))
        due = saved.generated_at + timedelta(seconds=settings.c3_batch_retry_delay_seconds + 1)
        if failure == "json":
            assert not inventory_reads
            native = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]
            assert native["llm_status"]["status"] == 200
            assert native["llm_status"]["json_structure_repair"]["parse_evidence"]["kind"] == "invalid_json"
        if failure in query_failure_fields:
            assert not inventory_reads
            metadata = saved.ai_response_snapshot["raw_payload"]["omniauto_brain_result"]["catalog_stages"]["validation_error"]
            assert metadata["fields"] == [query_failure_fields[failure]]
            assert joined.cq.QUERY_FORMAT_INSTRUCTION == metadata["repair_instruction"]
    monkeypatch.setattr(c3_service, "utcnow", lambda: due)
    recovered = recover_due_message_batches_once()
    assert recovered["generated"] == 1 and recovered["failed"] == 0, recovered
    recover_due_message_batches_once()
    assert len(attempts) == 2
    assert all(stage != "brain_quality_repair" for _, stage, _ in calls)
    with SessionLocal() as db:
        saved = db.get(MessageBatch, bid)
        replies = list(db.scalars(select(ReplyAction)))
        tasks = list(db.scalars(select(Task).where(Task.task_type == "chat_reply")))
        handoffs = list(db.scalars(select(HandoffEvent)))
        assert len(saved.ai_response_snapshot["generation_attempt_history"]) == 2
        if recovers:
            assert len(replies) == len(tasks) == 1 and not handoffs
            result = {"reply_action_id": replies[0].id, "task_id": tasks[0].id}
        else:
            assert not replies and not tasks and len(handoffs) == 1
            assert handoffs[0].handoff_reason_code == "AI_ENGINE_RETRY_EXHAUSTED"
    if failure in query_failure_fields:
        feedback = attempts[1]["invocation"]["target_state"]["brain_retry_instruction"]
        assert query_failure_fields[failure] in feedback
        assert joined.cq.QUERY_FORMAT_INSTRUCTION in feedback
        assert "尚未查询库存" in feedback
        second_generation = next(messages for a, stage, messages in calls if a == 2 and stage == "brain_llm")
        assert "尚未查询库存" in json.dumps(second_generation, ensure_ascii=False)
    if failure == "json":
        assert len([1 for a, stage, _ in calls if a == 1]) == 4
        if recovers:
            assert inventory_reads and all(a == 2 for a in inventory_reads)
            assert any(a == 2 and stage.endswith("json_structure_repair") for a, stage, _ in calls)
        else:
            assert not inventory_reads
    if recovers:
        permit = flow.claim_send(worker, result, flow.claim(worker, binding, result))
        assert permit.status_code == 200, permit.text
