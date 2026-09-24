"""Field transport through real projection/compaction, with no model or database."""
from copy import deepcopy
from pathlib import Path
import json
import sys

import pytest

APP = Path(__file__).resolve().parents[1]
for path in (APP.parents[1], APP, APP / "workflows", APP / "adapters"):
    sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.adapters.knowledge_loader import legacy_product_snippet
from apps.wechat_ai_customer_service.workflows import reply_evidence_builder as evidence
from apps.wechat_ai_customer_service.workflows.customer_service_brain import build_brain_prompt_pack


def vehicle():
    return {
        "id": "vehicle-a", "source": {"type": "chejin_backend"},
        "data": {"name": "2016款测试车", "specs": "排量：1.6L", "price": 9.37,
                 "additional_details": {"first_registration": "2017-08", "mileage_km": 0,
                    "exterior_color": "珍珠白", "interior_color": "栗棕", "location": "甲展厅",
                    "customer_description": "说明" * 2497 + "结尾哨兵完整",
                    "unknown_future_field": "PRIVATE-FUTURE", "vin": "PRIVATE-NESTED"}},
        "internal": {"vin": "PRIVATE-VIN", "internal_notes": "PRIVATE-NOTES"},
    }


@pytest.mark.parametrize("route", ["catalog", "retrieval", "merged"])
@pytest.mark.parametrize("fast", [False, True])
def test_all_public_details_survive_final_prompt_without_clipping(monkeypatch, route, fast):
    original = vehicle()
    before = deepcopy(original)
    catalog = evidence.catalog_product_payload(original)
    snippet = legacy_product_snippet(original, {})
    monkeypatch.setattr(evidence, "catalog_product_candidates", lambda *a, **kw: [] if route == "retrieval" else [catalog])
    packed = evidence.compact_knowledge_pack("这台车的上牌日期和其他资料", {
        "evidence": {"products": [] if route == "catalog" else [snippet]},
    }, max_rag_hits=0, max_rag_text_chars=100, max_catalog_candidates=5)
    prompt = build_brain_prompt_pack(settings={"prompt_profile": "routine_product_fast" if fast else "normal",
        "prompt_item_text_chars": 80 if fast else 260}, brain_input={"evidence": {"knowledge": packed}})
    items = prompt["user"]["brain_input"]["content_basis"]["product_master"]["items"]
    assert len(items) == 1
    specs = items[0]["specs"]
    for expected in ("排量：1.6L", "首次上牌（上牌日期，非年款）：2017-08", "表显里程（公里）：0",
                     "车身颜色：珍珠白", "内饰颜色：栗棕", "车辆所在地：甲展厅",
                     "车辆描述：" + original["data"]["additional_details"]["customer_description"]):
        assert expected in specs
    assert "PRIVATE-" not in json.dumps(prompt, ensure_ascii=False)
    assert original == before


@pytest.mark.parametrize("details", [None, {}, {"first_registration": None, "mileage_km": None}])
def test_missing_details_do_not_invent_registration_from_model_year(details):
    original = vehicle()
    original["data"]["additional_details"] = details
    result = evidence.catalog_product_payload(original)
    assert result["specs"] == "排量：1.6L"


def test_other_sources_keep_existing_projection():
    original = vehicle()
    original["source"]["type"] = "other_source"
    assert evidence.catalog_product_payload(original)["specs"] == "排量：1.6L"
    snippet = legacy_product_snippet(original, {})
    assert snippet["spec"] == "排量：1.6L"
    assert "source_type" not in snippet and "specs" not in snippet


def test_same_name_vehicle_facts_remain_bound_to_product_id(monkeypatch):
    a, b = vehicle(), vehicle()
    b["id"] = "vehicle-b"
    b["data"]["additional_details"]["first_registration"] = "2019-12"
    candidates = [evidence.catalog_product_payload(item) for item in (a, b)]
    monkeypatch.setattr(evidence, "catalog_product_candidates", lambda *a, **kw: candidates)
    packed = evidence.compact_knowledge_pack("两台都说一下", {"evidence": {}},
        max_rag_hits=0, max_rag_text_chars=100, max_catalog_candidates=5)
    items = {item["id"]: item for item in packed["product_master"]["items"]}
    assert "2017-08" in items["vehicle-a"]["specs"] and "2019-12" not in items["vehicle-a"]["specs"]
    assert "2019-12" in items["vehicle-b"]["specs"] and "2017-08" not in items["vehicle-b"]["specs"]
