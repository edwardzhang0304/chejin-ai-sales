"""Read-only projection of saved, customer-visible CheJin vehicle details."""
from typing import Any
from copy import deepcopy
import re
import unicodedata

from apps.wechat_ai_customer_service.adapters.vehicle_fields import (
    factual_number_spans, public_number, ENERGY_TYPES, DRIVE_TYPES,
)


_PUBLIC_DETAIL_LABELS = {
    "first_registration": "首次上牌（上牌日期，非年款）",
    "mileage_km": "表显里程（公里）",
    "exterior_color": "车身颜色",
    "interior_color": "内饰颜色",
    "location": "车辆所在地",
    "customer_description": "车辆描述",
}


def vehicle_specs_for_evidence(item: dict[str, Any]) -> str:
    """Enrich the existing specs contract without rewriting saved Product Master rows.

    Only the six declared public fields cross this boundary. Never copy the
    additional_details object wholesale: old/imported rows may hold private keys.
    """
    data = item.get("data") or {}
    specs = str(data.get("specs") or "")
    if (item.get("source") or {}).get("type") != "chejin_backend":
        return specs
    details = data.get("additional_details")
    if not isinstance(details, dict):
        return specs
    parts = [specs] if specs else []
    for key, label in _PUBLIC_DETAIL_LABELS.items():
        value = details.get(key)
        if key == "mileage_km":
            if type(value) is not int or value < 0:
                continue
        elif not isinstance(value, str) or not value.strip():
            continue
        parts.append(f"{label}：{value}")
    return " / ".join(parts)


def _reply_entities(text: str, products: list[dict]) -> list[dict]:
    labels = {}
    for product in products:
        for label in (product.get("name"), (product.get("public_details") or {}).get("model"), *(product.get("aliases") or [])):
            if isinstance(label, str) and label.strip():
                key = "".join(unicodedata.normalize("NFKC", label).casefold().split())
                labels.setdefault(key, set()).add(str(product["id"]))
    chars, positions = [], []
    for index, char in enumerate(text):
        for c in unicodedata.normalize("NFKC", char).casefold():
            if not c.isspace():
                chars.append(c)
                positions.append(index)
    normalized = "".join(chars)
    matches = []
    for label, ids in labels.items():
        for match in re.finditer(re.escape(label), normalized):
            matches.append({"start": positions[match.start()], "end": positions[match.end() - 1] + 1, "ids": sorted(ids)})
    chosen = []
    for match in sorted(matches, key=lambda m: (-(m["end"] - m["start"]), m["start"])):
        if not any(m["start"] <= match["start"] and match["end"] <= m["end"] for m in chosen):
            chosen.append(match)
    return sorted(chosen, key=lambda m: m["start"])


def validate_reply_public_facts(plan: dict, evidence: dict, *, formal_validation: dict | None = None) -> dict:
    """Inspect actual text, before/after semantic review; never trust claim IDs.

    This verifies recognizable entities/numeric facts. Unstructured claims
    remain subject to the existing semantic reviewer, not string-based proof.
    """
    from apps.wechat_ai_customer_service.workflows.catalog_query import aggregate_ready, message_sources
    from apps.wechat_ai_customer_service.workflows.catalog_budget import amount_role
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import formal_knowledge_source_table
    receipt = (evidence.get("catalog_protocol") or {}).get("receipt") or {}
    products = ((evidence.get("knowledge") or {}).get("product_master") or {}).get("items") or []
    by_id = {str(p["id"]): p for p in products}
    errors, facts, occurrences, actual_ids = [], [], [], []
    priced_ids = set()
    customer_statements = []
    policy_numbers = [n for source in formal_knowledge_source_table(evidence).values()
                      for n in factual_number_spans(source["content"])]

    def policy_number(number, segment_index):
        if number["unit"] == "万元" and amount_role(plan["reply_segments"][segment_index], number["start"]) in (None, "预算", "车价"):
            return False  # Neither an unassigned amount nor a car price borrows a policy fee.
        if formal_validation is not None:
            return any(n["segment_index"] == segment_index and n["start"] == number["start"] and n["end"] == number["end"]
                       for n in formal_validation.get("covered_numbers", []))
        # Defer only a real possible policy value, not any unexplained number.
        # This is not clearance: the post-review pass must locate its actual
        # assertion and selected program-owned source in the same full draft.
        return any(number["unit"] is not None and number["unit"] == n["unit"]
                   and public_number(number["value"]) == public_number(n["value"]) for n in policy_numbers)

    def add(kind, value, source, source_id):
        fact = {"fact_type": kind, "value": value, "source_level": source, "source_id": source_id}
        if fact not in facts:
            facts.append(fact)

    def customer_amount(segment, number):
        role = amount_role(segment, number["start"])
        if role is None or number["unit"] != "万元":
            return False
        if role != "预算":
            prefix = re.split(r"[，,。；;！？!?]", segment[:number["start"]])[-1].strip()
            if not re.fullmatch(r"(?:您|你)(?:的)?(?:希望|计划|打算|预计|考虑)?(?:首付|月供|定金)(?:希望|计划|是|为|预算|大约|约)?", prefix):
                return False
        value = public_number(number["value"])
        budget = (receipt.get("query") or {}).get("budget") or {}
        # A historic amount is not today's effective budget. Check the frozen
        # query first, then retain the real customer source (never AI history).
        if role == "预算" and (budget.get("state") != "specified" or value not in {
                public_number(budget.get(k)) for k in ("value", "lower", "upper")}):
            return False
        for source in message_sources(evidence):
            if source["sender"] not in ("customer", "client"):
                continue
            if role == "预算" and source["id"] not in budget.get("source_ids", []):
                continue
            for n in factual_number_spans(source["content"]):
                source_role = amount_role(source["content"], n["start"])
                if (n["unit"] == number["unit"] and value is not None and value == public_number(n["value"])
                        and (source_role == role or (role == "预算" and source_role in (None, "车价")))):
                    add("customer_requirement", number["text"], "current_conversation", source["id"])
                    return True
        return False

    for segment_index, segment in enumerate(plan.get("reply_segments") or []):
        entities = _reply_entities(segment, products)
        numbers = factual_number_spans(segment, excluded=[(e["start"], e["end"]) for e in entities])
        customer_positions = set()
        for n in numbers:
            clause_start = max((m.end() for m in re.finditer(r"[，,。；;！？!?]", segment[:n["start"]])), default=0)
            if amount_role(segment, n["start"]) not in (None, "车价") and not any(clause_start <= e["start"] < n["start"] for e in entities):
                customer_positions.add(n["start"])
        customer_numbers = {n["start"] for n in numbers if n["start"] in customer_positions and customer_amount(segment, n)}
        for clause in re.finditer(r"[^，,。；;！？!?]+", segment):
            clause_numbers = [n for n in numbers if clause.start() <= n["start"] < clause.end()]
            if (clause_numbers and all(n["start"] in customer_numbers for n in clause_numbers)
                    and not segment[clause_numbers[-1]["end"]:clause.end()].strip()):
                customer_statements.append({"segment_index": segment_index, "start": clause.start(), "end": clause.end()})
        for number in numbers:
            if number["start"] in customer_numbers:
                occurrences.append({"segment_index": segment_index, **number, "verified": True, "role": "customer_requirement"})
        for entity_index, entity in enumerate(entities):
            end = entities[entity_index + 1]["start"] if entity_index + 1 < len(entities) else len(segment)
            section = segment[entity["start"]:end]
            if len(entity["ids"]) != 1:
                if any(entity["end"] <= n["start"] < end for n in numbers):
                    errors.append("reply_vehicle_reference_ambiguous")
                continue
            product_id = entity["ids"][0]
            product = by_id[product_id]
            if product_id not in actual_ids:
                actual_ids.append(product_id)
            add("product_name", product.get("name"), "product_master", product_id)
            details = product.get("public_details") or {}
            for field, value in details.items():
                public_value = ENERGY_TYPES.get(value, value) if field == "energy_type" else DRIVE_TYPES.get(value, value) if field == "drive_type" else value
                if isinstance(public_value, str) and public_value and public_value in section:
                    add("configuration", f"{field}:{public_value}", "product_master", product_id)
            for number in (n for n in numbers if entity["end"] <= n["start"] < end):
                if number["start"] in customer_numbers:
                    continue
                if (number["start"] in customer_positions or
                        (number["unit"] == "万元" and amount_role(segment, number["start"]) not in (None, "车价"))):
                    valid = policy_number(number, segment_index)
                    if not valid:
                        errors.append("unverified_reply_monetary_role")
                    occurrences.append({"segment_index": segment_index, **number, "verified": valid and formal_validation is not None,
                                        "pending_policy_review": valid and formal_validation is None})
                    continue
                clause_start = max((m.end() for m in re.finditer(r"[，,。；;！？!?]", segment[:number["start"]])), default=0)
                if clause_start > entity["end"] and policy_number(number, segment_index):
                    occurrences.append({"segment_index": segment_index, **number, "verified": formal_validation is not None,
                                        "pending_policy_review": formal_validation is None})
                    continue
                unit, value = number["unit"], public_number(number["value"])
                field = {"万元": "price", "公里": "mileage_km", "kWh": "battery_capacity_kwh", "台": "stock"}.get(unit)
                expected = product.get(field) if field in ("price", "stock") else details.get(field)
                valid = field is not None and value is not None and value == public_number(expected)
                if unit == "年" and "上牌" in section:
                    date = str(details.get("first_registration") or "")
                    valid = bool(re.match(r"^\d{4}[-/]\d{2}", date)) and value == public_number(date[:4])
                    field = "first_registration"
                    expected = date
                if unit in ("L", "T"):
                    expected = details.get("displacement")
                    valid = str(expected or "").upper() == f"{number['text']}".upper()
                    field = "displacement"
                occurrences.append({"segment_index": segment_index, **number, "product_id": product_id, "verified": valid})
                if not valid:
                    errors.append("reply_numeric_fact_mismatch" if value is not None else "numeric_expression_unverifiable")
                else:
                    add({"price": "price", "stock": "stock", "mileage_km": "mileage", "first_registration": "year"}.get(field, "configuration"),
                        expected, "product_master", product_id)
                    if field == "price":
                        priced_ids.add(product_id)
        # Numeric clauses before any vehicle cannot borrow any later price.
        first_entity = entities[0]["start"] if entities else len(segment)
        prefix = segment[:first_entity]
        for number in (n for n in numbers if n["start"] < first_entity):
            if number["start"] in customer_numbers:
                continue
            value = public_number(number["value"])
            policy = policy_number(number, segment_index)
            valid = policy
            if amount_role(segment, number["start"]) is None and aggregate_ready(receipt) and value is not None:
                candidates = [("catalog_match_count", "match_count")] if number["unit"] == "台" else (
                    [("catalog_price_min", "price_min"), ("catalog_price_max", "price_max")]
                    if number["unit"] == "万元" and not receipt.get("price_unknown_count") else [])
                for kind, field in candidates:
                    if value == public_number(receipt.get(field)):
                        valid = True
                        add(kind, receipt[field], "catalog_query", receipt["query_id"])
            occurrences.append({"segment_index": segment_index, **number, "verified": valid and (not policy or formal_validation is not None),
                                "pending_policy_review": policy and formal_validation is None})
            if not valid:
                errors.append("unverified_reply_numeric_fact")
        if (not entities and aggregate_ready(receipt) and receipt.get("match_count") == 0
                and re.search(r"(?:没有|暂无|未找到|没找到)(?:符合[^。！？]*|合适的?|匹配的?|在售的?)?(?:车|车源)", segment)):
            add("catalog_match_count", 0, "catalog_query", receipt["query_id"])
    preparation = (evidence.get("catalog_protocol") or {}).get("preparation") or {}
    recommendations = receipt.get("recommendations") or []
    ordered_ids = [str(p["id"]) for p in recommendations]
    if receipt.get("query", {}).get("operation") == "search":
        actual_recommendations = [i for i in actual_ids if i in ordered_ids]
        if actual_recommendations != ordered_ids[:len(actual_recommendations)]:
            errors.append("reply_recommendation_order_mismatch")
        if preparation.get("requires_recommendation") and aggregate_ready(receipt) and recommendations:
            if not actual_recommendations:
                errors.append("answer_incomplete")
            for product in recommendations[:len(actual_recommendations)]:
                if product.get("price") is not None and str(product["id"]) not in priced_ids:
                    errors.append("answer_known_price_omitted")
        if (preparation.get("answer_goal") == "uncertain" and aggregate_ready(receipt) and recommendations
                and not actual_recommendations and any(f["source_level"] == "catalog_query" for f in facts)):
            errors.append("answer_goal_unresolved_for_summary")
    # A model-supplied true ID with a wrong value remains a conflict; do not
    # quietly replace its declaration and hide the failure.
    for declared in plan.get("facts_claimed") or []:
        if declared.get("fact_type") in ("price", "product_price"):
            product = by_id.get(str(declared.get("source_id")))
            values = factual_number_spans(str(declared.get("value")))
            value = public_number(declared.get("value"))
            if value is None and len(values) == 1 and values[0]["unit"] == "万元":
                value = public_number(values[0]["value"])
            if not product or value is None or value != public_number(product.get("price")):
                errors.append("model_declared_fact_conflict")
    return {"ok": not errors, "errors": list(dict.fromkeys(errors)), "facts_claimed": facts,
            "product_ids": actual_ids, "occurrences": occurrences, "customer_statements": customer_statements}


def validate_reviewed_reply_facts(plan: dict, evidence: dict, *, refs: dict, mapping: dict) -> dict:
    """One actual-text check used by semantic clearance and final Guard.

    Policy selections can support only their actual monetary roles. Vehicle
    prices, customer amounts and query aggregates retain independent authority.
    """
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import validate_policy_refs
    preliminary = validate_reply_public_facts(plan, evidence)
    formal = validate_policy_refs(refs, segments=plan.get("reply_segments") or [], mapping=mapping,
                                  customer_statements=preliminary["customer_statements"])
    validated = validate_reply_public_facts(plan, evidence, formal_validation=formal)
    validated["facts_claimed"].extend(formal["facts_claimed"])
    validated["formal_source_ids"] = formal["source_ids"]
    validated["errors"] = list(dict.fromkeys([*formal["errors"], *validated["errors"]]))
    validated["ok"] = not validated["errors"]
    return validated


def derive_reviewed_fact_fields(plan: dict, validated: dict) -> None:
    """Called only by the existing semantic-clearance owner after actual pass."""
    if not validated.get("ok"):
        raise ValueError("cannot_derive_unverified_facts")
    plan["facts_claimed"] = deepcopy(validated["facts_claimed"])
    evidence = plan.setdefault("evidence_used", {})
    evidence["product_ids"] = list(validated["product_ids"])
    evidence["formal_knowledge_ids"] = list(validated.get("formal_source_ids") or [])
