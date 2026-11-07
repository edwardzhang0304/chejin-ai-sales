"""Vehicle API -> full catalog -> Brain/Guard -> original native receipt settlement.

Synthetic model and Hook transport; real storage, query and recovery contracts.
No production conversations, model service or Windows actions.
"""
import json
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, HandoffEvent
import test_model_query_hook_pipeline as pipeline
from apps.wechat_ai_customer_service import llm_config
from test_model_query_hook_pipeline import actual_brain
from test_hook_business_http import clean
from test_hook_history_business_http import coverage as original_coverage
from test_program_preparation_pipeline import listed


@pytest.mark.parametrize("field, value, op, stored, other, demand", [
    ("exterior_color", "白色", "eq", "白", "黑", "四万元以内，外观要白色的车"),
    ("interior_color", "黑色", "eq", "黑", "米", "四万元以内，内饰要黑色的车"),
    ("first_registration", "2021-01", "gte", "2022-02", "2020-12", "四万元以内，2021年1月及以后首次上牌的车"),
])
def test_managed_field_to_reply_and_native_settlement(monkeypatch, actual_brain, field, value, op, stored, other, demand):
    state = actual_brain
    listed("合成符合车", 3.8, **{field: stored})
    listed("合成其他车", 3.8, **{field: other})
    def coverage(binding):
        result = original_coverage(binding)
        for scan in result["scans"]:
            for message in scan["databases"][0]["pages"][0]:
                message["StrContent"] = demand
        return result
    monkeypatch.setattr(pipeline.native, "coverage", coverage)
    def transport(**kw):
        state["wire"].append(kw)
        assert len(state["wire"]) <= 3
        if kw["model"] == "synthetic-reviewer":
            result = pipeline.joined.review("vehicle_facts")
            state["reviews"].append(kw)
        elif not state.get("receipt"):
            mid = state["invocations"][-1]["batch"][-1]["id"]
            q = pipeline.joined.query(source_ids=[mid], filters=[
                {"field": field, "op": op, "value": value, "source_ids": [mid]}])
            q["budget"]["source_ids"] = [mid]
            q.pop("version"); q.pop("sort"); q["budget"].pop("unit")
            result = {"catalog_query": q}
        else:
            receipt = state["receipt"]
            assert receipt["evaluation_state"] == "complete" and receipt["match_count"] == 1
            assert [p["name"] for p in receipt["recommendations"]] == ["合成符合车"]
            assert "PRIVATE_SENTINEL" not in kw["messages"][1]["content"]
            result = pipeline.joined.fixtures.base_plan()
            result.update(answer_mode="recommend_from_catalog", reply_segments=["合成符合车报价3.8万元。"])
            result.pop("facts_claimed", None); result.pop("evidence_used", None)
        return {"ok": True, "status": 200, "response_text": json.dumps(result, ensure_ascii=False)}
    monkeypatch.setattr(llm_config, "call_llm_request_once_with_wall_timeout", transport)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend("unique", monkeypatch)
    assert len(state["wire"]) == 2  # Understanding + generation; no forced clarification/repair.
    assert not state["reviews"]
    with SessionLocal() as db:
        assert not list(db.scalars(select(HandoffEvent)))
        batch = db.scalar(select(MessageBatch))
        assert batch.generation_attempt_count == 1
