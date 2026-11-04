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
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.wechat_ai_customer_service.workflows.catalog_budget import catalog_budget_around_upper
from apps.wechat_ai_customer_service.adapters.vehicle_fields import (
    canonical_enum, numeric_field, ENERGY_TYPES, DRIVE_TYPES, SERIES_OPTIONS,
)


PROTOCOL = "vehicle_query_v1"
TEXT_FIELDS = {"brand", "series", "model", "energy_type", "drive_type", "location", "displacement"}
NUMBER_FIELDS = {"mileage_km", "battery_capacity_kwh"}

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
 "name_queries":[{"text":"客户点名的完整车型称呼","kind":"reference|exclude","source_ids":["消息ID"]}],
 "reference_ids":[],"exclude_ids":[],"sort":"price_asc|price_desc|budget_distance",
 "source_ids":["消息ID"],"unsupported_requirements":[],"interpretation":"简短说明当前需求与历史取舍"}
预算 specified 时 upper/around 仅填写 value，range 仅填写 lower/upper；其他状态不填金额或mode；not_mentioned使用source_ids=[]。
当前未提但历史有可靠预算时应解析成 specified 并引用该历史消息；未设预算询问品牌允许 not_mentioned。
unlimited 只用于客户明确取消预算，uncertain 用于含糊或矛盾。首付/月供不等于总价，不用最近出现的数字覆盖总预算。
支持 brand/series/model/energy_type/drive_type/location/displacement 的 eq（或多值 in），以及 mileage_km（公里）/battery_capacity_kwh（kWh）的 lte/gte。排量如1.5T是文字，不能数值比较。
其他条件如无大修、低油耗先列 unsupported_requirements，不能假装已核验；车源数据缺字段也不能当满足。
name_queries可为空；首次点名车型用name_queries，程序按全库公开名称/型号/别名字面匹配；不要编造ID。品牌找车用filters，不是name_queries。多义或未找到须澄清，不等于没有库存。reference_ids/exclude_ids只允许known_ids中有可信来源的ID；历史仅有名称则按名称查，不自造ID。
所有条件先查全库再限制展示。拒绝旧推荐用 exclude_ids；点名旧车问价比较用 reference_ids。
引用ID必须来自附带的消息来源表/历史推荐记录，不得编造。query_id/tenant/snapshot/统计由程序生成，不要输出。
around只表示客户说的目标金额，程序按统一已批准范围换算，不自行发明范围。
第二轮收到程序查询回执后，只输出原BrainPlan；不得再次查询或改变需求。保持不确定条件说明；
推荐依据recommendations中的ID及product_master对应车源，references中的ID仅用于点名事实，不能用超预算references挤掉预算内推荐。
零匹配只代表回执所列条件范围；未知字段/不支持条件不等于全店无车。
数量/最低最高报价必须声明facts_claimed：fact_type=catalog_match_count/catalog_price_min/catalog_price_max，
source_level=catalog_query，source_id=程序query_id，value=准确数值（价格单位万元，数量0也须声明）。
只有完整查询且无未知字段/未核验条件/未解决指代才可用此聚合权威；正文必须限定相同查询范围。
"""

QUERY_INSTRUCTION += "\n可用能源值：" + json.dumps(ENERGY_TYPES, ensure_ascii=False) + "；驱动：" + json.dumps(DRIVE_TYPES, ensure_ascii=False) + "；车系：" + json.dumps(SERIES_OPTIONS, ensure_ascii=False)


class CatalogQueryError(ValueError):
    def __init__(self, code: str, *, fields: list[str] | None = None):
        super().__init__(code)
        # Only validator-owned paths, never customer text or rejected values.
        self.fields = list(dict.fromkeys(fields or []))[:20]


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def rejected_field_evidence(raw: Any, fields: list[str]) -> list[dict]:
    """Describe validator-selected fields without retaining arbitrary values."""
    result = []
    for field in fields[:20]:
        value, present = raw, True
        if field == "<unexpected_fields>":
            result.append({"field": field})
            continue
        for key in ([] if field == "catalog_query" else field.split(".")):
            if not isinstance(value, dict) or key not in value:
                present = False
                break
            value = value[key]
        item = {"field": field, "present": present}
        if present:
            item.update(value_type=type(value).__name__, value_sha256=fingerprint(value))
        result.append(item)
    return result


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


def normalize_query(raw: Any, sources: list[dict], *, known_ids: dict | None = None) -> dict:
    keys = {"version", "operation", "requirement_state", "budget", "filters", "reference_ids", "exclude_ids", "sort", "source_ids", "unsupported_requirements", "interpretation"}
    if not isinstance(raw, dict):
        raise CatalogQueryError("invalid_query_schema", fields=["catalog_query"])
    if set(raw) - {"name_queries"} != keys or type(raw.get("version")) is not int or raw["version"] != 1:
        fields = sorted(keys - set(raw))
        if set(raw) - keys - {"name_queries"}:
            fields.append("<unexpected_fields>")
        if type(raw.get("version")) is not int or raw.get("version") != 1:
            fields.append("version")
        raise CatalogQueryError("invalid_query_schema", fields=fields)
    if (not isinstance(raw["operation"], str) or raw["operation"] not in {"search", "details", "compare"}
            or not isinstance(raw["requirement_state"], str) or raw["requirement_state"] not in {"clear", "uncertain"}):
        raise CatalogQueryError("invalid_query_operation", fields=["operation", "requirement_state"])
    allowed = {s["id"] for s in sources}
    query = deepcopy(raw)
    query["source_ids"] = _ids(raw["source_ids"], allowed=allowed, required=True)
    for key in ("reference_ids", "exclude_ids"):
        query[key] = _ids(raw[key])
        if key == "reference_ids" and len(query[key]) > 20:
            raise CatalogQueryError("too_many_referenced_vehicles")
    if set(query["reference_ids"]) & set(query["exclude_ids"]):
        raise CatalogQueryError("reference_exclusion_conflict")
    known_ids = known_ids or {}
    if not set(query["reference_ids"] + query["exclude_ids"]) <= set(known_ids):
        raise CatalogQueryError("vehicle_id_without_trusted_conversation_source")
    query["id_sources"] = {key: deepcopy(known_ids[key]) for key in query["reference_ids"] + query["exclude_ids"]}
    names = raw.get("name_queries", [])
    if not isinstance(names, list) or len(names) > 10:
        raise CatalogQueryError("invalid_name_queries")
    for name in names:
        if (not isinstance(name, dict) or set(name) != {"text", "kind", "source_ids"}
                or name["kind"] not in {"reference", "exclude"}
                or not isinstance(name["text"], str) or not 1 <= len(name["text"].strip()) <= 200):
            raise CatalogQueryError("invalid_name_query")
        _ids(name["source_ids"], allowed=allowed, required=True)
    query["name_queries"] = deepcopy(names)
    if raw["operation"] in {"details", "compare"} and not (query["reference_ids"] or any(n["kind"] == "reference" for n in names)):
        raise CatalogQueryError("missing_referenced_vehicle")
    if raw["sort"] not in {"price_asc", "price_desc", "budget_distance"}:
        raise CatalogQueryError("unsupported_sort")
    if not isinstance(raw["interpretation"], str) or not raw["interpretation"].strip() or len(raw["interpretation"]) > 1500:
        raise CatalogQueryError("missing_need_interpretation")
    unsupported = raw["unsupported_requirements"]
    if not isinstance(unsupported, list) or len(unsupported) > 20 or any(not isinstance(s, str) or len(s) > 200 for s in unsupported):
        raise CatalogQueryError("invalid_unsupported_requirements")
    budget = deepcopy(raw["budget"])
    # An absent budget has no evidence to cite. Normalize only this empty metadata;
    # specified/unlimited/uncertain constraints still require the declared fields.
    if isinstance(budget, dict) and budget.get("state") == "not_mentioned":
        budget.setdefault("source_ids", [])
    query["budget"] = budget
    if not isinstance(budget, dict):
        raise CatalogQueryError("invalid_budget_state_or_unit", fields=["budget"])
    budget_errors = []
    if not isinstance(budget.get("state"), str) or budget["state"] not in {"specified", "unlimited", "not_mentioned", "uncertain"}:
        budget_errors.append("budget.state")
    if budget.get("unit") != "万元":
        budget_errors.append("budget.unit")
    if budget_errors:
        raise CatalogQueryError("invalid_budget_state_or_unit", fields=budget_errors)
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
            raise CatalogQueryError("invalid_budget_mode", fields=["budget.mode"])
        bounds = {"lower": lower, "upper": upper, "target": target, "unit": "万元"}
    if set(budget) != expected:
        raise CatalogQueryError("invalid_budget_fields", fields=["budget"])
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
    for f in query["filters"]:
        if f["field"] in {"energy_type", "drive_type", "series"}:
            try:
                f["value"] = canonical_enum(f["field"], f["value"]) if f["op"] == "eq" else [canonical_enum(f["field"], v) for v in f["value"]]
            except ValueError as exc:
                raise CatalogQueryError("unsupported_vehicle_enum") from exc
    query["request"] = deepcopy(raw)
    query["source_evidence"] = deepcopy(sources)
    return query


def normalized_name(value: str) -> str:
    # Preserve all meaningful digits/punctuation: 1.5T and 2.0T must not collapse.
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolve_names(names: list[dict], rows: list[dict]) -> list[dict]:
    results = []
    for name in names:
        key = normalized_name(name["text"])
        matches = []
        for row in rows:
            data = row.get("data") or {}
            labels = [data.get("name"), (data.get("additional_details") or {}).get("model"), *(data.get("aliases") or [])]
            if key and key in {normalized_name(v) for v in labels if isinstance(v, str)}:
                matches.append({"id": row["id"], "name": data.get("name")})
        matches.sort(key=lambda r: str(r["id"]))
        results.append({**deepcopy(name), "normalized_text": key, "match_scope": "authorized_active_tenant_snapshot",
                        "status": "unique" if len(matches) == 1 else "ambiguous" if matches else "not_found",
                        "matched_ids": [m["id"] for m in matches], "match_count": len(matches),
                        "candidates": matches[:20], "truncated": len(matches) > 20})
    return results


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
    name_results = resolve_names(query["name_queries"], rows)
    unresolved = any(n["status"] != "unique" for n in name_results)
    uncertain |= unresolved or bool(set(query["exclude_ids"]) - {r["id"] for r in rows})
    matches, references, unknown = [], [], {}
    excluded = set(query["exclude_ids"])
    ref_ids = set(query["reference_ids"])
    for match in name_results:
        if match["status"] == "unique":
            (ref_ids if match["kind"] == "reference" else excluded).update(match["matched_ids"])
    if ref_ids & excluded:
        raise CatalogQueryError("reference_exclusion_conflict")
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
            else:
                numeric = numeric_field(f["field"], value)
                if numeric is None:
                    missing.append(f["field"])
                else:
                    expected = Decimal(str(f["value"]))
                    fits &= numeric <= expected if f["op"] == "lte" else numeric >= expected
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
    references.sort(key=lambda p: str(p["id"]))
    prices = [r["price"] for r in matches if type(r.get("price")) in {int, float} and math.isfinite(r["price"]) and r["price"] > 0]
    receipt = {"protocol": PROTOCOL, "query_id": fingerprint({"scope": scope, "query": query}),
               "scope": deepcopy(scope), "tenant_id": str(runtime.tenant_id),
               "snapshot_id": snapshot_id(rows), "query": deepcopy(query),
               "scan_complete": True, "scanned_count": len(rows), "match_count": len(matches),
               "unknown_fields": unknown, "unknown_field_count": len(unknown),
               "unsupported_requirements": query["unsupported_requirements"],
               "missing_reference_ids": sorted(ref_ids - {r["id"] for r in references}),
               "requirement_uncertain": uncertain, "name_results": name_results,
               "missing_exclude_ids": sorted(excluded - {r["id"] for r in rows}),
               "price_unknown_count": len(matches) - len(prices),
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
        for match in receipt.get("name_results", []):
            match["matched_ids"] = match["matched_ids"][:20]
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
    errors = []
    for fact in plan.get("facts_claimed", []):
        if fact.get("source_level") == "catalog_query" or fact.get("fact_type") in AGGREGATES:
            field = AGGREGATES.get(fact.get("fact_type"))
            if (not field or fact.get("source_level") != "catalog_query" or not aggregate_ready(receipt)
                    or fact.get("source_id") != receipt.get("query_id") or receipt.get(field) is None
                    or (field != "match_count" and receipt.get("price_unknown_count"))):
                errors.append("invalid_catalog_aggregate_source")
                continue
            try:
                value = Decimal(str(fact.get("value")))
                if not value.is_finite() or value != Decimal(str(receipt[field])):
                    errors.append("catalog_aggregate_value_mismatch")
            except (InvalidOperation, ValueError):
                errors.append("invalid_catalog_aggregate_value")
    return errors


AGGREGATES = {"catalog_match_count": "match_count", "catalog_price_min": "price_min", "catalog_price_max": "price_max"}


def aggregate_ready(receipt):
    return bool(receipt.get("scan_complete") and not receipt.get("requirement_uncertain")
                and not receipt.get("unsupported_requirements") and not receipt.get("unknown_field_count")
                and not receipt.get("missing_reference_ids") and not receipt.get("missing_exclude_ids"))


def record_semantic_clearance(evidence, plan, raw_review, review):
    """Semantic classification + mechanical authority; never accept model-issued proof."""
    evidence.pop("_catalog_clearance", None)
    kind = raw_review.get("catalog_claim_kind")
    errors = query_plan_errors(plan, evidence)
    receipt = (evidence.get("catalog_protocol") or {}).get("receipt") or {}
    if kind not in {"non_inventory", "vehicle_facts", "query_summary", "uncertain"} or kind == "uncertain":
        errors.append("catalog_claim_classification_required")
    if kind in {"vehicle_facts", "query_summary"} and not receipt.get("scan_complete"):
        errors.append("current_inventory_not_queried")
    if kind == "query_summary":
        aggregates = [f for f in plan.get("facts_claimed", []) if f.get("fact_type") in AGGREGATES]
        if not aggregates or not aggregate_ready(receipt):
            errors.append("catalog_summary_requires_complete_scoped_facts")
    if kind == "vehicle_facts" and not any(f.get("source_level") == "product_master" for f in plan.get("facts_claimed", [])):
        errors.append("catalog_vehicle_facts_must_be_declared")
    if errors:
        return {**review, "ok": False, "verdict": "repair", "enforced": True,
                "errors": list(dict.fromkeys([*(review.get("errors") or []), *errors])), "soft_pass_low_risk": False}
    if review.get("ok") and raw_review.get("verdict") == "pass":
        from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import join_reply_segments
        evidence["_catalog_clearance"] = {"reply": fingerprint(join_reply_segments(plan.get("reply_segments") or []).strip()),
                                           "protocol": fingerprint(evidence["catalog_protocol"]), "kind": kind}
    return review


def clearance_kind(reply, evidence):
    proof = evidence.get("_catalog_clearance") or {}
    if (proof.get("reply") == fingerprint(reply.strip())
            and proof.get("protocol") == fingerprint(evidence.get("catalog_protocol"))):
        return proof.get("kind")
    return None


def run_catalog_round(*, first_result: dict, settings: dict, brain_input: dict, run_llm, runtime_factory) -> dict:
    """At most one read-only query then one completion, inside Brain's time budget."""
    evidence = brain_input["evidence"]
    first = first_result.get("brain_plan") or {}
    if not first_result.get("ok") or "catalog_query" not in first:
        return first_result
    audit = {"understanding": {k: first_result.get(k) for k in ("usage", "prompt_estimate", "provider", "model", "status", "elapsed_ms")}}
    started = time.monotonic()
    phase = "validation"
    try:
        if set(first) != {"catalog_query"}:
            raise CatalogQueryError("query_and_reply_must_be_separate", fields=["catalog_query"])
        query = normalize_query(first["catalog_query"], message_sources(evidence),
                                known_ids=(evidence.get("catalog_protocol") or {}).get("known_ids"))
        audit["validation_seconds"] = time.monotonic() - started
        phase = "inventory_query"
        runtime = runtime_factory()
        scope = {"conversation_id": brain_input["target"]["conversation_id"],
                 "current_message_ids": brain_input["current_message"]["message_ids"]}
        receipt = execute_query(query, runtime, scope=scope, limit=min(20, max(1, int(settings.get("max_catalog_candidates") or 8))))
        attach_receipt(evidence, receipt)
        audit["query_seconds"] = time.monotonic() - started
        phase = "completion"
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
        audit["error_code"] = str(exc) if isinstance(exc, CatalogQueryError) else "catalog_query_unavailable"
        audit["query_seconds"] = time.monotonic() - started
        audit["failed_stage"] = phase
        invalid_query = phase == "validation" and isinstance(exc, CatalogQueryError)
        if invalid_query:
            audit["validation_error"] = {
                "code": str(exc), "fields": exc.fields,
                "field_evidence": rejected_field_evidence(first.get("catalog_query"), exc.fields),
                "query_sha256": fingerprint(first.get("catalog_query")),
            }
        return {"ok": False, "error": str(exc) if isinstance(exc, CatalogQueryError) else "catalog_query_unavailable",
                "error_kind": "catalog_query_validation" if invalid_query else "catalog_query_execution",
                "status": "error", "http_status": first_result.get("status"),
                "catalog_stages": audit, "provider": first_result.get("provider"), "model": first_result.get("model")}


def trusted_image_ids(target_state, batch, conversation_id):
    """Only the validated backend context can contribute IDs, never Brain output/context hints."""
    context = target_state.get("chejin_brain_context") or {}
    if context.get("conversation_id") != conversation_id or not conversation_id:
        return {}
    ids = {}
    for row in [*(context.get("history") or []), *batch]:
        key = row.get("id") or row.get("message_event_id")
        product = row.get("server_validated_product_id")
        if row.get("message_type") == "image" and key and isinstance(product, str) and product:
            ids.setdefault(product, []).append({"source_id": key, "conversation_id": conversation_id,
                                                 "kind": "backend_validated_image"})
    return ids
