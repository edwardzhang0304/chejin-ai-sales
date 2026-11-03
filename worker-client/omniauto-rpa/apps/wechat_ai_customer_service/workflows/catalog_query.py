"""Hook Brain catalog contract: AI meaning, deterministic tenant inventory query.

No customer-language parsing, provider calls, business state writes or sends.
Receipts are constructed here, never accepted from a model response.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time
from typing import Any

from apps.wechat_ai_customer_service.workflows.catalog_budget import catalog_budget_around_upper


PROTOCOL = "vehicle_query_v1"
TEXT_FIELDS = {"brand", "series", "model", "energy_type", "drive_type", "location"}
NUMBER_FIELDS = {"mileage_km", "battery_capacity_kwh", "displacement"}

QUERY_INSTRUCTION = """
本轮车辆事实只能通过 catalog_query 获取。未查询不代表没有库存；历史推荐不是当前库存证明。
先理解完整当前消息和历史，分清当前总预算、以前预算、首付、月供、否定、指代及拒绝旧推荐。
需要库存、价格、推荐或比较时，首轮只输出 {"catalog_query": QUERY}，不同时输出回复。
无需车辆事实的闲聊/常识可直接输出原 BrainPlan；意思含糊可用原 send_reply + ask_clarifying_question。
QUERY格式（仅以下字段）：
{"version":1,"operation":"search|details|compare","requirement_state":"clear|uncertain",
 "budget":{"state":"specified|unlimited|not_mentioned|uncertain","unit":"万元",
           "mode":"upper|range|around","value":4,"lower":3,"upper":5,"source_ids":["消息ID"]},
 "filters":[{"field":"brand","op":"eq","value":"大众","source_ids":["消息ID"]}],
 "reference_ids":[],"exclude_ids":[],"sort":"price_asc|price_desc|budget_distance",
 "source_ids":["消息ID"],"unsupported_requirements":[],"interpretation":"简短说明当前需求与历史取舍"}
预算 specified 时 upper/around 仅填写 value，range 仅填写 lower/upper；其他状态不填金额或mode。
当前未提但历史有可靠预算时应解析成 specified 并引用该历史消息；未设预算询问品牌允许 not_mentioned。
unlimited 只用于客户明确取消预算，uncertain 用于含糊或矛盾。首付/月供不等于总价，不用最近出现的数字覆盖总预算。
支持 brand/series/model/energy_type/drive_type/location 的 eq（或多值 in），以及 mileage_km/battery_capacity_kwh/displacement 的 lte/gte。
其他条件如无大修、低油耗先列 unsupported_requirements，不能假装已核验；车源数据缺字段也不能当满足。
所有条件先查全库再限制展示。拒绝旧推荐用 exclude_ids；点名旧车问价比较用 reference_ids。
引用ID必须来自附带的消息来源表/历史推荐记录，不得编造。query_id/tenant/snapshot/统计由程序生成，不要输出。
around只表示客户说的目标金额，程序按统一已批准范围换算，不自行发明范围。
第二轮收到程序查询回执后，只输出原BrainPlan；不得再次查询或改变需求。保持不确定条件说明；
推荐依据recommendations中的ID及product_master对应车源，references中的ID仅用于点名事实，不能用超预算references挤掉预算内推荐。
零匹配只代表回执所列条件范围；未知字段/不支持条件不等于全店无车。
"""


class CatalogQueryError(ValueError):
    pass


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def message_sources(evidence: dict) -> list[dict]:
    rows = [*(evidence.get("conversation", {}).get("history") or []), *(evidence.get("current_batch") or [])]
    sources = {}
    for row in rows:
        key = str(row.get("id") or row.get("message_event_id") or "")
        if key:
            sources[key] = {"id": key, "sender": row.get("sender", ""), "content": row.get("content", "")}
    return list(sources.values())


def _ids(value: Any, *, allowed: set[str] | None = None, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v for v in value):
        raise CatalogQueryError("invalid_id_list")
    result = list(dict.fromkeys(value))
    if required and not result:
        raise CatalogQueryError("missing_message_source")
    if allowed is not None and not set(result) <= allowed:
        raise CatalogQueryError("message_source_not_in_frozen_conversation")
    return result


def _number(value: Any, *, zero: bool = False) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise CatalogQueryError("invalid_numeric_constraint")
    return float(value)


def normalize_query(raw: Any, sources: list[dict]) -> dict:
    keys = {"version", "operation", "requirement_state", "budget", "filters", "reference_ids", "exclude_ids", "sort", "source_ids", "unsupported_requirements", "interpretation"}
    if not isinstance(raw, dict) or set(raw) != keys or type(raw.get("version")) is not int or raw["version"] != 1:
        raise CatalogQueryError("invalid_query_schema")
    if raw["operation"] not in {"search", "details", "compare"} or raw["requirement_state"] not in {"clear", "uncertain"}:
        raise CatalogQueryError("invalid_query_operation")
    allowed = {s["id"] for s in sources}
    query = deepcopy(raw)
    query["source_ids"] = _ids(raw["source_ids"], allowed=allowed, required=True)
    for key in ("reference_ids", "exclude_ids"):
        query[key] = _ids(raw[key])
        if key == "reference_ids" and len(query[key]) > 20:
            raise CatalogQueryError("too_many_referenced_vehicles")
    if raw["operation"] in {"details", "compare"} and not query["reference_ids"]:
        raise CatalogQueryError("missing_referenced_vehicle")
    if raw["sort"] not in {"price_asc", "price_desc", "budget_distance"}:
        raise CatalogQueryError("unsupported_sort")
    if not isinstance(raw["interpretation"], str) or not raw["interpretation"].strip() or len(raw["interpretation"]) > 1500:
        raise CatalogQueryError("missing_need_interpretation")
    unsupported = raw["unsupported_requirements"]
    if not isinstance(unsupported, list) or len(unsupported) > 20 or any(not isinstance(s, str) or len(s) > 200 for s in unsupported):
        raise CatalogQueryError("invalid_unsupported_requirements")
    budget = raw["budget"]
    if not isinstance(budget, dict) or budget.get("state") not in {"specified", "unlimited", "not_mentioned", "uncertain"} or budget.get("unit") != "万元":
        raise CatalogQueryError("invalid_budget_state_or_unit")
    state = budget["state"]
    _ids(budget.get("source_ids"), allowed=allowed, required=state in {"specified", "unlimited"})
    expected = {"state", "unit", "source_ids"}
    bounds = None
    if state == "specified":
        mode = budget.get("mode")
        if mode == "range":
            expected |= {"mode", "lower", "upper"}
            lower, upper = _number(budget.get("lower"), zero=True), _number(budget.get("upper"))
            if lower > upper:
                raise CatalogQueryError("inverted_budget_range")
            target = (lower + upper) / 2
        elif mode in {"upper", "around"}:
            expected |= {"mode", "value"}
            target = _number(budget.get("value"))
            upper = catalog_budget_around_upper(target) if mode == "around" else target
            lower = max(0.0, target - (upper - target)) if mode == "around" else 0.0
        else:
            raise CatalogQueryError("invalid_budget_mode")
        bounds = {"lower": lower, "upper": upper, "target": target, "unit": "万元"}
    if set(budget) != expected:
        raise CatalogQueryError("invalid_budget_fields")
    query["budget_bounds"] = bounds
    filters = raw["filters"]
    if not isinstance(filters, list) or len(filters) > 20:
        raise CatalogQueryError("invalid_filters")
    for f in filters:
        if not isinstance(f, dict) or set(f) != {"field", "op", "value", "source_ids"}:
            raise CatalogQueryError("invalid_filter_schema")
        _ids(f["source_ids"], allowed=allowed, required=True)
        if f["field"] in TEXT_FIELDS and f["op"] in {"eq", "in"}:
            values = [f["value"]] if f["op"] == "eq" else f["value"]
            if not isinstance(values, list) or not values or len(values) > 20 or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values):
                raise CatalogQueryError("invalid_text_filter")
        elif f["field"] in NUMBER_FIELDS and f["op"] in {"lte", "gte"}:
            _number(f["value"], zero=True)
        else:
            raise CatalogQueryError("unsupported_filter_must_be_explicit")
    query["source_evidence"] = deepcopy(sources)
    return query


def inventory_snapshot(runtime) -> list[dict]:
    # Storage implementation is tenant-scoped and unpaginated; never use UI list APIs.
    return [r for r in runtime.list_items("products") if isinstance(r, dict)
            and r.get("status", "active") in {"active", "approved", "published"}]


def snapshot_id(rows: list[dict]) -> str:
    return fingerprint(sorted(rows, key=lambda r: str(r.get("id") or "")))


def execute_query(query: dict, runtime, *, scope: dict, limit: int = 8) -> dict:
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import catalog_product_payload
    rows = inventory_snapshot(runtime)
    bounds = query["budget_bounds"]
    uncertain = query["requirement_state"] == "uncertain" or query["budget"]["state"] == "uncertain"
    matches, references, unknown = [], [], {}
    excluded = set(query["exclude_ids"])
    ref_ids = set(query["reference_ids"])
    for row in rows:
        data = row.get("data") or {}
        payload = catalog_product_payload(row)
        payload["version"] = fingerprint(row)
        price = data.get("price")
        price_valid = type(price) in {int, float} and math.isfinite(price) and price > 0
        if row["id"] in ref_ids:
            payload["selection_role"] = "referenced_vehicle"
            payload["within_budget"] = (bounds["lower"] <= price <= bounds["upper"]) if bounds and price_valid else None
            references.append(deepcopy(payload))
        if row["id"] in excluded or uncertain:
            continue
        missing = []
        fits = True
        if bounds:
            if not price_valid:
                missing.append("price")
            elif not bounds["lower"] <= price <= bounds["upper"]:
                fits = False
        for f in query["filters"]:
            value = (data.get("additional_details") or {}).get(f["field"])
            if value is None or value == "":
                missing.append(f["field"])
            elif f["field"] in TEXT_FIELDS:
                options = [f["value"]] if f["op"] == "eq" else f["value"]
                fits &= str(value).strip().casefold() in {v.strip().casefold() for v in options}
            elif type(value) not in {int, float} or not math.isfinite(value):
                missing.append(f["field"])
            else:
                fits &= value <= f["value"] if f["op"] == "lte" else value >= f["value"]
        if missing:
            unknown[row["id"]] = missing
        if fits and not missing:
            payload["selection_role"] = "budget_match" if bounds else "query_match"
            matches.append(payload)
    def rank(p):
        value = p.get("price")
        valid = type(value) in {int, float} and math.isfinite(value) and value > 0
        if not valid:
            return (1, 0, str(p["id"]))
        score = (-value if query["sort"] == "price_desc" else
                 abs(value - bounds["target"]) if bounds and query["sort"] == "budget_distance" else value)
        return (0, score, str(p["id"]))
    matches.sort(key=rank)
    references.sort(key=lambda p: query["reference_ids"].index(p["id"]))
    prices = [r["price"] for r in matches if type(r.get("price")) in {int, float} and math.isfinite(r["price"]) and r["price"] > 0]
    receipt = {"protocol": PROTOCOL, "query_id": fingerprint({"scope": scope, "query": query}),
               "scope": deepcopy(scope), "tenant_id": str(runtime.tenant_id),
               "snapshot_id": snapshot_id(rows), "query": deepcopy(query),
               "scan_complete": True, "scanned_count": len(rows), "match_count": len(matches),
               "unknown_fields": unknown, "unknown_field_count": len(unknown),
               "unsupported_requirements": query["unsupported_requirements"],
               "missing_reference_ids": sorted(ref_ids - {r["id"] for r in references}),
               "requirement_uncertain": uncertain,
               "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
               "recommendations": matches[:max(0, limit)], "references": references,
               "truncated": len(matches) > limit, "created_at": time.time()}
    return receipt


def attach_receipt(evidence: dict, receipt: dict) -> None:
    evidence["catalog_protocol"] = {"protocol": PROTOCOL, "phase": "queried", "receipt": receipt}
    items = list(receipt["recommendations"])
    known = {p["id"] for p in items}
    items.extend(p for p in receipt["references"] if p["id"] not in known)
    knowledge = evidence["knowledge"]
    knowledge["product_master"] = {"items": items, "catalog_selection": receipt,
                                   "authority_level": "product_master", "can_authorize_product_facts": True}
    knowledge["evidence"]["products"] = items
    knowledge["evidence"]["catalog_candidates"] = items
    evidence.setdefault("audit_summary", {})["catalog_selection"] = receipt
    # Preserve formal IDs, replace only product IDs through the existing collector.
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import collect_evidence_ids
    evidence["evidence_ids"] = collect_evidence_ids(knowledge)


def prompt_protocol(evidence: dict) -> dict | None:
    protocol = evidence.get("catalog_protocol")
    if not protocol:
        return None
    result = deepcopy(protocol)
    receipt = result.get("receipt")
    if receipt:
        receipt.pop("unknown_fields", None)
        receipt.pop("created_at", None)
        for key in ("recommendations", "references"):
            receipt[key] = [p["id"] for p in receipt[key]]
    return result


def query_plan_errors(plan: dict, evidence: dict) -> list[str]:
    protocol = evidence.get("catalog_protocol")
    if not protocol:
        return []  # OCR compatibility retains its existing contract.
    if any(k in plan for k in ("catalog_query", "catalog_receipt")):
        return ["catalog_intermediate_or_forged_receipt_not_sendable"]
    receipt = protocol.get("receipt") or {}
    # Real product facts still pass the existing fact/ID validator as well.
    products = (plan.get("evidence_used") or {}).get("product_ids") or []
    facts = [f for f in plan.get("facts_claimed", []) if f.get("source_level") == "product_master"]
    if (products or facts) and not receipt.get("scan_complete"):
        return ["current_inventory_not_queried"]
    return []


def run_catalog_round(*, first_result: dict, settings: dict, brain_input: dict, run_llm, runtime_factory) -> dict:
    """At most one read-only query then one completion, inside Brain's time budget."""
    evidence = brain_input["evidence"]
    first = first_result.get("brain_plan") or {}
    if not first_result.get("ok") or "catalog_query" not in first:
        return first_result
    audit = {"understanding": {k: first_result.get(k) for k in ("usage", "prompt_estimate", "provider", "model")}}
    started = time.monotonic()
    try:
        if set(first) != {"catalog_query"}:
            raise CatalogQueryError("query_and_reply_must_be_separate")
        query = normalize_query(first["catalog_query"], message_sources(evidence))
        runtime = runtime_factory()
        scope = {"conversation_id": brain_input["target"]["conversation_id"],
                 "current_message_ids": brain_input["current_message"]["message_ids"]}
        receipt = execute_query(query, runtime, scope=scope, limit=min(20, max(1, int(settings.get("max_catalog_candidates") or 8))))
        attach_receipt(evidence, receipt)
        audit["query_seconds"] = time.monotonic() - started
        final = run_llm(settings=settings, brain_input=brain_input)
        audit["completion"] = {k: final.get(k) for k in ("usage", "prompt_estimate", "provider", "model")}
        if final.get("ok") and query_plan_errors(final.get("brain_plan") or {}, evidence):
            raise CatalogQueryError("catalog_second_query_or_forged_receipt")
        # A reply based on a stale catalog must not bypass existing backend invalidation.
        if final.get("ok") and snapshot_id(inventory_snapshot(runtime)) != receipt["snapshot_id"]:
            raise CatalogQueryError("catalog_changed_during_generation")
        final["catalog_stages"] = audit
        return final
    except Exception as exc:
        audit["error_type"] = type(exc).__name__
        audit["query_seconds"] = time.monotonic() - started
        return {"ok": False, "error": str(exc) if isinstance(exc, CatalogQueryError) else "catalog_query_unavailable",
                "catalog_stages": audit, "provider": first_result.get("provider"), "model": first_result.get("model")}
