"""Public management fields must remain usable across query and reply authority."""
from copy import deepcopy
import ast
import json
from pathlib import Path

import pytest
from test_catalog_query import cq, execute, query, row, evidence, Runtime as cq_test_runtime
from test_program_catalog_preparation import prepared
from apps.wechat_ai_customer_service.adapters.vehicle_fields import CATALOG_FILTER_TYPES, PUBLIC_DETAIL_FIELDS
from apps.wechat_ai_customer_service.workflows.vehicle_public_facts import validate_reply_public_facts


def condition(field, value, op="eq"):
    return {"field": field, "op": op, "value": value, "source_ids": ["m1"]}


@pytest.mark.parametrize("field", ["exterior_color", "interior_color"])
@pytest.mark.parametrize("reverse", [False, True])
def test_color_scans_whole_inventory_normalizes_basic_labels_without_guessing_shades(field, reverse):
    rows = [row(str(i), 3, **{field: "黑"}) for i in range(65)]
    rows += [row("fit", 3.8, **{field: "白"}), row("shade", 3.7, **{field: "珍珠白"}),
             row("two-tone", 3.6, **{field: "黑白双色"})]
    receipt = execute(query(filters=[condition(field, "白色")]), rows[::-1] if reverse else rows)
    assert receipt["scanned_count"] == 68
    assert receipt["match_count"] == 1
    assert [p["id"] for p in receipt["recommendations"]] == ["fit"]


def test_color_in_alias_intersection_and_price_boundaries():
    receipt = execute(query(filters=[condition("exterior_color", ["白色", "黑色"], "in"),
                                    condition("exterior_color", "白")]),
                      [row("fit", 4, exterior_color="白色"), row("over", 4.01, exterior_color="白"),
                       row("black", 3, exterior_color="黑")])
    assert receipt["evaluation_state"] == "complete" and receipt["match_count"] == 1


@pytest.mark.parametrize("field, value, op, valid", [
    ("exterior_color", "白色", "eq", "白"),
    ("interior_color", "黑色", "eq", "黑"),
    ("first_registration", "2021-01", "gte", "2022-03"),
])
@pytest.mark.parametrize("missing", [None, "", "未知", {}, True])
def test_missing_or_malformed_stock_fields_are_unknown_not_matches_or_false_zero(field, value, op, valid, missing):
    receipt = execute(query(filters=[condition(field, value, op)]),
                      [row("known", 3, **{field: valid}), row("unknown", 3, **{field: missing})])
    assert receipt["evaluation_state"] == "partial" and receipt["match_count"] is None
    assert receipt["unknown_fields"] == {"unknown": [field]}
    assert [p["id"] for p in receipt["recommendations"]] == ["known"]


@pytest.mark.parametrize("op,expected", [("eq", ["b"]), ("gte", ["b", "c"]), ("lte", ["a", "b"])])
def test_registration_month_comparison_is_calendar_order(op, expected):
    receipt = execute(query(filters=[condition("first_registration", "2021-01", op)]),
                      [row("a", 3, first_registration="2020-12"), row("b", 3, first_registration="2021-01"),
                       row("c", 3, first_registration="2021-10")])
    assert [p["id"] for p in receipt["recommendations"]] == expected


@pytest.mark.parametrize("bad", ["2021", "2021-1", "2021-13", "2021-00", "2021-02-01", "21款", 2021, True, None, []])
def test_registration_filter_rejects_invalid_or_model_year_inputs(bad):
    with pytest.raises(cq.CatalogQueryError) as error:
        cq.normalize_query(query(filters=[condition("first_registration", bad)]), cq.message_sources(evidence()))
    assert error.value.fields == ["filters.0.value"]


def test_conflicting_registration_bounds_require_clarification():
    receipt = execute(query(filters=[condition("first_registration", "2022-01", "gte"),
                                    condition("first_registration", "2021-12", "lte")]), [])
    assert receipt["evaluation_state"] == "clarification_required" and receipt["match_count"] is None


def test_unsupported_inventory_condition_is_not_an_unclear_customer_requirement():
    receipt = execute(query(unsupported_requirements=["无大修"]), [row("a", 3)])
    assert receipt["evaluation_state"] == "partial" and not receipt["requirement_uncertain"]
    assert receipt["match_count"] is None and not cq.aggregate_ready(receipt)
    assert receipt["known_condition_match_count"] == 1
    legacy = deepcopy(receipt)
    legacy["requirement_uncertain"] = True  # Earlier exporter conflated unsupported and ambiguous.
    assert cq.receipt_view(legacy)["evaluation_state"] == "partial"
    assert legacy["requirement_uncertain"] is True
    unclear = execute(query(requirement_state="uncertain", unsupported_requirements=["年份近一点"]), [row("a", 3)])
    assert unclear["evaluation_state"] == "clarification_required"


def test_color_requirement_reaches_prepared_reply_without_forced_repeat_question():
    pack, preparation, _, runtime = prepared("我想要四万左右的白色车", rows=[
        row("a", 3.8, exterior_color="白", interior_color="黑"), row("b", 4, exterior_color="黑")])
    assert preparation["route"] == "semantic"
    raw = query(filters=[condition("exterior_color", "白色")])
    normalized = cq.normalize_query(raw, cq.message_sources(pack))
    receipt = cq.execute_query(normalized, runtime, scope={"conversation_id": "c1"})
    cq.attach_receipt(pack, receipt)
    plan = {"answer_mode": "recommend_from_catalog", "reply_segments": ["合成车a报价3.8万元，登记的外观颜色是白色。"]}
    checked = validate_reply_public_facts(plan, pack)
    assert "catalog_clarification_required" not in checked["errors"]
    assert [p["id"] for p in pack["knowledge"]["product_master"]["items"]] == ["a"]
    assert receipt["evaluation_state"] == "complete" and receipt["match_count"] == 1


def test_vehicle_management_fields_have_an_explicit_public_query_or_private_role():
    root = next(p for p in Path(__file__).resolve().parents if (p / "backend/app/schemas/vehicle.py").exists())
    module = ast.parse((root / "backend/app/schemas/vehicle.py").read_text())
    schema = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "VehicleFields")
    fields = {n.target.id for n in schema.body if isinstance(n, ast.AnnAssign)}
    private = {"vin", "plate_number", "purchase_price", "internal_notes"}
    # Name resolution, price bounds and public narrative have separate deliberate roles.
    assert fields == set(CATALOG_FILTER_TYPES) | private | {"display_name", "public_price", "customer_description"}
    assert set(PUBLIC_DETAIL_FIELDS) == set(CATALOG_FILTER_TYPES) | {"customer_description"}
    receipt = execute(query(), [row("a", 3, **dict.fromkeys(private, "PRIVATE_SENTINEL"))])
    assert "PRIVATE_SENTINEL" not in json.dumps(cq.receipt_products(receipt))
    for field in private | {"customer_description"}:
        with pytest.raises(cq.CatalogQueryError):
            cq.normalize_query(query(filters=[condition(field, "x")]), cq.message_sources(evidence()))


@pytest.mark.parametrize("field,value,op,stored,other", [
    ("brand", "合成甲", "eq", "合成甲", "合成乙"),
    ("series", "德系车", "eq", "德系车", "日系车"),
    ("model", "SUV", "eq", "SUV", "轿车"),
    ("energy_type", "新能源纯电", "eq", "electric", "fuel"),
    ("drive_type", "前驱", "eq", "front_wheel_drive", "rear_wheel_drive"),
    ("location", "合成城市", "eq", "合成城市", "其他城市"),
    ("displacement", "1.5T", "eq", "1.5T", "2.0T"),
    ("mileage_km", 60000, "lte", 59999, 60001),
    ("battery_capacity_kwh", 60, "gte", "60.0", "59.9"),
    ("exterior_color", "白色", "eq", "白", "黑"),
    ("interior_color", "黑色", "eq", "黑", "米"),
    ("first_registration", "2021-01", "gte", "2022-02", "2020-12"),
])
def test_all_structured_public_fields_filter_before_display_limit(field, value, op, stored, other):
    rows = [row(str(i), 3, **{field: other}) for i in range(65)]
    rows.append(row("fit", 3.8, **{field: stored}))
    receipt = execute(query(filters=[condition(field, value, op)]), rows)
    assert receipt["scanned_count"] == 66
    assert receipt["evaluation_state"] == "complete"
    assert receipt["match_count"] == 1
    assert [p["id"] for p in receipt["recommendations"]] == ["fit"]


@pytest.mark.parametrize('supplied', ['price_asc', 'price_desc', 'budget_distance'])
def test_model_cannot_turn_a_color_request_into_a_customer_sort_preference(supplied):
    # Reduced from the live model result: the source exists, but says nothing
    # about ordering. Keep the raw output for diagnosis; do not reject the reply.
    sources = [{'id': 'm1', 'sender': 'customer',
                'content': '四万元以内，内饰要黑色的车，有哪些？请说下车名、价格和内饰颜色。'}]
    raw = query(filters=[condition('interior_color', '黑色')])
    raw.pop('version'); raw['budget'].pop('unit')
    raw['sort'] = {'value': supplied, 'source_ids': ['m1']}
    q = cq.normalize_model_query(raw, sources, contract=cq.MODEL_QUERY_CONTRACT)
    assert q['raw_model_query'] == raw
    assert (q['sort'], q['sort_origin'], q['sort_source_ids']) == ('budget_distance', 'program_default', [])
    result = cq.execute_query(q, cq_test_runtime([
        row(str(i), price, interior_color='黑') for i, price in enumerate([2.5, 2.8, 3, 3.2, 3.9])
    ]), scope={}, limit=4)
    assert result['match_count'] == 5
    assert [p['price'] for p in result['recommendations']] == [3.9, 3.2, 3, 2.8]


@pytest.mark.parametrize('text,supplied,expected', [
    ('四万以内，便宜的先推荐', 'price_desc', 'price_asc'),
    ('四万以内，价格从高到低', 'price_asc', 'price_desc'),
    ('四万以内，先看更实惠的', 'price_asc', 'price_asc'),
])
def test_real_customer_order_still_overrides_default(text, supplied, expected):
    raw = query(); raw.pop('version'); raw['budget'].pop('unit')
    raw['sort'] = {'value': supplied, 'source_ids': ['m1']}
    q = cq.normalize_model_query(raw, [{'id':'m1','sender':'customer','content':text}], contract=cq.MODEL_QUERY_CONTRACT)
    assert (q['sort'], q['sort_origin']) == (expected, 'customer_explicit')


def test_unrelated_later_message_cannot_replace_an_earlier_customer_order():
    sources = [{'id':'h1','sender':'customer','content':'四万以内，价格从高到低'},
               {'id':'m1','sender':'customer','content':'内饰要黑色，价格是多少？'}]
    raw = query(filters=[condition('interior_color','黑色')]); raw.pop('version'); raw['budget'].pop('unit')
    raw['sort'] = {'value':'price_asc','source_ids':['m1']}
    q = cq.normalize_model_query(raw, sources, contract=cq.MODEL_QUERY_CONTRACT)
    assert (q['sort'],q['sort_source_ids']) == ('price_desc',['h1'])
