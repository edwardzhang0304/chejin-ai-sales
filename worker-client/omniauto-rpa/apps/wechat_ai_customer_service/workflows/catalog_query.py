"""Hook Brain catalog contract: AI meaning, deterministic tenant inventory query.

No customer-language parsing, provider calls, business state writes or sends.
Receipts are constructed here, never accepted from a model response.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.wechat_ai_customer_service.workflows.catalog_budget import catalog_budget_around_upper, budget_statement
from apps.wechat_ai_customer_service.adapters.vehicle_fields import (
    canonical_enum, numeric_field, ENERGY_TYPES, DRIVE_TYPES, SERIES_OPTIONS,
)


PROTOCOL = "vehicle_query_v1"
MODEL_QUERY_CONTRACT = "vehicle_need_v2"
TEXT_FIELDS = {"brand", "series", "model", "energy_type", "drive_type", "location", "displacement"}
NUMBER_FIELDS = {"mileage_km", "battery_capacity_kwh"}
QUERY_REQUIRED_FIELDS = ("version", "operation", "requirement_state", "budget", "filters", "reference_ids",
                         "exclude_ids", "sort", "source_ids", "unsupported_requirements", "interpretation")
QUERY_ENVELOPE_EXAMPLE = {"catalog_query": {
    "version": 1, "operation": "search", "requirement_state": "clear",
    "budget": {"state": "not_mentioned", "unit": "万元", "source_ids": []},
    "filters": [], "name_queries": [], "reference_ids": [], "exclude_ids": [], "sort": "price_asc",
    "source_ids": ["消息ID"], "unsupported_requirements": [], "interpretation": "按客户原话说明需求与待核验条件",
}}
QUERY_FORMAT_INSTRUCTION = (
    "首轮查车必须输出完整外层对象，不能把QUERY字段直接放在最外层。合法结构示例（值须按真实需求替换）："
    + json.dumps(QUERY_ENVELOPE_EXAMPLE, ensure_ascii=False, separators=(",", ":"))
    + "。catalog_query内必填字段：" + ",".join(QUERY_REQUIRED_FIELDS)
    + "；只有name_queries可省略。sort即使没有预算也必须填写，只能选price_asc/price_desc/budget_distance；"
      "不支持的筛选或排序条件保留在unsupported_requirements，不得省略字段或假装已筛选。"
      "所有预算状态均须保留unit=万元；不允许用示例值替客户设预算。"
)

QUERY_INSTRUCTION = """
本轮车辆事实只能通过 catalog_query 获取。未查询不代表没有库存；历史推荐不是当前库存证明。
先理解完整当前消息和历史，分清当前总预算、以前预算、首付、月供、否定、指代及拒绝旧推荐。
需要库存、价格、推荐或比较时，首轮只输出 {"catalog_query": QUERY}，不同时输出回复。
无需车辆事实的闲聊/常识可直接输出原 BrainPlan；意思含糊可用原 send_reply + ask_clarifying_question。
QUERY字段说明：version为整数1；operation选search/details/compare；requirement_state选clear/uncertain。
budget.state选specified/unlimited/not_mentioned/uncertain，unit固定万元。
filters每项为{"field":"brand","op":"eq","value":"大众","source_ids":["消息ID"]}这样的条件对象。
name_queries每项为{"text":"客户点名的完整车型称呼","kind":"reference","source_ids":["消息ID"]}，kind选reference/exclude。
预算 specified 时 upper/around 仅填写 value，range 仅填写 lower/upper；其他状态不填金额或mode；not_mentioned使用source_ids=[]。
指定上限预算对象示例：{"state":"specified","unit":"万元","mode":"upper","value":4,"source_ids":["消息ID"]}。
指定区间预算对象示例：{"state":"specified","unit":"万元","mode":"range","lower":3,"upper":5,"source_ids":["消息ID"]}。金额必须来自真实预算，不照抄示例。
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
QUERY_INSTRUCTION += "\n" + QUERY_FORMAT_INSTRUCTION


V2_QUERY_EXAMPLE = deepcopy(QUERY_ENVELOPE_EXAMPLE)
V2_QUERY_EXAMPLE["catalog_query"].pop("version")
V2_QUERY_EXAMPLE["catalog_query"].pop("sort")
V2_QUERY_EXAMPLE["catalog_query"]["budget"].pop("unit")


def model_contract(protocol: dict) -> str:
    value = protocol.get("model_query_contract", PROTOCOL)
    if value not in (PROTOCOL, MODEL_QUERY_CONTRACT):
        raise CatalogQueryError("unsupported_model_query_contract", fields=["model_query_contract"])
    return value


def query_format_instruction(contract: str = PROTOCOL) -> str:
    if contract == PROTOCOL:
        return QUERY_FORMAT_INSTRUCTION
    model_contract({"model_query_contract": contract})
    return (
        "首轮查车必须输出唯一完整外层对象，合法示例（值须按真实需求替换）："
        + json.dumps(V2_QUERY_EXAMPLE, ensure_ascii=False, separators=(",", ":"))
        + "。catalog_query必填：" + ",".join(k for k in QUERY_REQUIRED_FIELDS if k not in {"version", "sort"})
        + "；name_queries可省略。version由程序提供，不用复述。budget金额value/lower/upper均为万元，"
          "如80000元须理解为8；不输出unit，不猜测含糊金额。"
          "sort仅在客户有明确排序偏好时填写对象，例如"
          '{"value":"price_asc","source_ids":["原话消息ID"]}，value只能为price_asc/price_desc/budget_distance。'
          "没有明确偏好请省略sort，程序按已确认规则排序：有明确预算优先接近budget_bounds.target，无预算价格升序。"
          "禁止用默认排序代替客户明确偏好；不支持的条件/排序保留unsupported_requirements。"
          "业务字段/显式空列表仍必填，不得照抄示例或将遗漏条件补为空。"
    )


def query_instruction(contract: str = PROTOCOL, *, understanding_only: bool = False) -> str:
    instruction = QUERY_INSTRUCTION
    if understanding_only:
        instruction = re.sub(r"第二轮收到程序查询回执后.*?(?=\n可用能源值)", "", instruction, flags=re.S)
    if contract == PROTOCOL:
        return instruction
    # Shared semantic instructions, with only the v1 wire metadata examples removed.
    common = instruction.removesuffix("\n" + QUERY_FORMAT_INSTRUCTION)
    common = common.replace("QUERY字段说明：version为整数1；", "QUERY字段说明：")
    common = common.replace("，unit固定万元。", "；所有金额字段以万元计，固定单位由程序提供。")
    common = common.replace('"unit":"万元",', '')
    return common + "\n" + query_format_instruction(contract)


# These are the three existing sort values. Both routes read the same
# chronological instructions; they do not infer a fourth ordering policy.
_SORT_INSTRUCTION = re.compile(
    r"(?P<price_asc>便宜的?先(?:给我看|看|推荐)?|便宜(?:的)?优先|(?:价格)?从低到高|低价优先)"
    r"|(?P<price_desc>贵的?先(?:给我看|看|推荐)?|贵的?优先|(?:价格)?从高到低|高价优先)"
    r"|(?P<budget_distance>(?:最?接近|贴近)预算(?:的)?|按预算距离)"
    r"|(?P<reset>(?:取消|不用|不再|不按|不需要)(?:之前的?|原来(?:的)?)?(?:价格)?排序|排序(?:随意|不限))")


def sort_instructions(text):
    """Yield only affirmative instructions or an explicit withdrawal, in order."""
    for match in _SORT_INSTRUCTION.finditer(text):
        clause = re.split(r"[。；;，,！？!?]", text[:match.start()])[-1]
        if re.search(r"之前说|原来说|以前说|如果|假如|是否", clause):
            continue
        revoked = bool(re.search(r"(?:不要|不再|不用|别|取消|不按)(?:按|看|选|按原来)?\s*$", clause))
        yield match, match.lastgroup, revoked


def effective_customer_sort(sources, supplied, supplied_ids):
    chosen = None
    touched = False
    for source in sources:
        if source.get("sender") not in {"customer", "client"}:
            continue
        text = budget_statement(source["content"])["remainder"]
        instructions = list(sort_instructions(text))
        if not instructions and supplied and source["id"] in supplied_ids:
            # A later customer-authored ordering request interpreted by the
            # semantic path may supersede an earlier explicit preference.
            if not touched or re.search(r"排序|优先|先看|先推荐|排前", text):
                chosen = (supplied, list(supplied_ids))
        for _, value, revoked in instructions:
            touched = True
            if value == "reset" or revoked:
                if value == "reset" or chosen is None or chosen[0] == value:
                    chosen = None
            else:
                chosen = (value, [source["id"]])
    return chosen


def normalize_model_query(raw: Any, sources: list[dict], *, known_ids: dict | None = None,
                          contract: str = PROTOCOL, current_ids=None) -> dict:
    """Adapt trusted wire identity once; never infer a contract from model fields."""
    if contract == PROTOCOL:
        return normalize_query(raw, sources, known_ids=known_ids)
    model_contract({"model_query_contract": contract})
    if not isinstance(raw, dict):
        raise CatalogQueryError("invalid_query_schema", fields=["catalog_query"])
    candidate = deepcopy(raw)
    redundant = []
    if "version" in candidate:
        if type(candidate["version"]) is not int or candidate["version"] != 1:
            raise CatalogQueryError("model_contract_metadata_conflict", fields=["version"])
        redundant.append("version")
    candidate["version"] = 1
    budget = candidate.get("budget")
    if isinstance(budget, dict):
        if "unit" in budget:
            if budget["unit"] != "万元":
                raise CatalogQueryError("model_contract_metadata_conflict", fields=["budget.unit"])
            redundant.append("budget.unit")
        budget["unit"] = "万元"
    explicit = "sort" in raw
    sort_sources = []
    if explicit:
        sort = raw["sort"]
        if not isinstance(sort, dict) or set(sort) != {"value", "source_ids"}:
            raise CatalogQueryError("invalid_sort_preference", fields=["sort"])
        sort_sources = _ids(sort["source_ids"], field="sort.source_ids",
                            allowed={item["id"] for item in sources}, required=True)
        candidate["sort"] = sort["value"]
    else:
        # Validate all business fields first; never infer unlimited or a budget.
        candidate["sort"] = "price_asc"
    query = normalize_query(candidate, sources, known_ids=known_ids)
    from apps.wechat_ai_customer_service.workflows.catalog_budget import explicit_total_budget
    # Check the model's own cited total-price sources with the same budget
    # parser. Finance/monthly amounts cannot replace an explicit total.
    explicit_budget = None
    for source in sources:
        if source["id"] not in query["budget"].get("source_ids", []) or source.get("sender") not in ("customer", "client"):
            continue
        if budget_statement(source["content"])["present"]:
            explicit_budget = explicit_total_budget(source["content"])
    if explicit_budget and query["budget_bounds"] != {k: v for k, v in explicit_budget.as_dict().items() if k != "kind"}:
        raise CatalogQueryError("budget_conflicts_with_explicit_source", fields=["budget", "budget.source_ids"])
    preference = effective_customer_sort(sources, candidate["sort"] if explicit else None, sort_sources)
    explicit = preference is not None
    if explicit:
        query["sort"], sort_sources = preference
        query["source_ids"] = list(dict.fromkeys([*query["source_ids"], *sort_sources]))
    else:
        query["sort"] = "budget_distance" if query["budget_bounds"] else "price_asc"
        sort_sources = []
    # "Exclude the one just shown" identifies a vehicle by the settled reply,
    # never by a guessed name or by a matching number in unsent conversation.
    customer = [m for m in sources if m.get("sender") in {"customer", "client"}]
    current = [m for m in customer if m["id"] in current_ids] if current_ids is not None else customer[-1:]
    current_text = " ".join(m["content"] for m in current)
    mentions_name = any(n["kind"] == "exclude" and normalized_name(n["text"]) in normalized_name(current_text)
                        for n in query["name_queries"])
    relative = re.search(r"(?:刚才|上[次一]|刚刚|之前|那)[^，,。！？]{0,8}(?:辆|台|款)", current_text)
    exclude = re.search(r"不要|不看|排除|换(?:成|一|个|辆|台)|看看别的|看别的", current_text)
    revoked = re.search(r"不是不要|别排除|不要排除|不换", current_text)
    if relative and revoked and not mentions_name and query["exclude_ids"]:
        raise CatalogQueryError("previous_vehicle_exclusion_revoked", fields=["exclude_ids"])
    if relative and exclude and not mentions_name and not revoked:
        confirmed = [key for key, links in (known_ids or {}).items()
                     if any(link.get("kind") == "confirmed_native_reply" for link in links)]
        if len(confirmed) != 1 or (query["exclude_ids"] and set(query["exclude_ids"]) != set(confirmed)):
            query["unresolved_previous_exclusion"] = True
            query["exclude_ids"] = []
            query["name_queries"] = [n for n in query["name_queries"] if n["kind"] != "exclude"]
        else:
            query["exclude_ids"] = confirmed
            query["id_sources"][confirmed[0]] = deepcopy(known_ids[confirmed[0]])
            query["name_queries"] = [n for n in query["name_queries"] if n["kind"] != "exclude"]
    query.update(model_query_contract=contract, raw_model_query=deepcopy(raw),
                 sort_origin="customer_explicit" if explicit else "program_default",
                 sort_source_ids=sort_sources, redundant_metadata=redundant)
    return query


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
            if isinstance(value, list) and key.isdecimal() and int(key) < len(value):
                value = value[int(key)]
            elif isinstance(value, dict) and key in value:
                value = value[key]
            else:
                present = False
                break
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


def prepare_catalog_need(evidence: dict) -> dict:
    result = _prepare_catalog_need(evidence)
    current = " ".join(m["content"] for m in message_sources({"current_batch": evidence.get("current_batch") or []})
                       if m["sender"] in ("customer", "client"))
    result.update(catalog_answer_goal(current, non_inventory=result["route"] == "non_inventory"))
    return result


def catalog_answer_goal(current: str, *, non_inventory: bool = False) -> dict:
    """Freeze what an answer must deliver, independently of query routing.

    A request for choices does not depend on an explicit noun (车). Count and
    recommendation requests can coexist; negated requests are not obligations.
    Unrecognized intent remains unknown, never a permission to answer counts.
    """
    summary = recommendation = False
    choice = r"推荐|介绍|挑选|挑几|选几|看看|给我看|给我发|有哪些|有什么|哪(?:些|款|辆|台)|有没有[^，,。；;！？!?]*(?:合适|适合)"
    statistics = r"多少(?:台|辆|款)|几(?:台|辆|款)|数量|价格区间|最低价|最高价"
    for clause in re.split(r"[，,。；;！？!?]", current):
        # Limit negation to its requested action, not the whole utterance.
        clean = re.sub(r"(?:不用|不要|不必|无需|别)(?:再|给我|帮我|只)?(?:" + choice + r")", "", clause)
        recommendation |= bool(re.search(choice, clean))
        clean = re.sub(r"(?:不用|不要|不必|无需|别)(?:只)?(?:告诉我|报|说|统计)?(?:" + statistics + r")", "", clause)
        summary |= bool(re.search(statistics, clean))
    goal = "non_inventory" if non_inventory or not current.strip() else (
        "recommendation" if recommendation else "summary" if summary else "uncertain")
    return {"answer_goal": goal, "asks_summary": summary,
            "requires_recommendation": True if goal == "recommendation" else None if goal == "uncertain" else False}


def _prepare_catalog_need(evidence: dict) -> dict:
    """Conservative program-first routing, before any model call.

    Only fully consumed, unambiguous shopping statements execute directly.
    Unknown conditions/negation/references use the existing AI interpretation
    path and then the SAME normalize_model_query/execute_query functions.
    """
    protocol = evidence.get("catalog_protocol") or {}
    if model_contract(protocol) != MODEL_QUERY_CONTRACT:
        return {"route": "semantic", "reason": "legacy_query_contract"}
    sources = message_sources(evidence)
    current_ids = {str(m.get("id") or m.get("message_event_id") or "") for m in evidence.get("current_batch") or []}
    customer = [m for m in sources if m["sender"] in ("customer", "client")]
    current = [m for m in customer if m["id"] in current_ids]
    if not current:
        return {"route": "semantic", "reason": "business_trigger_or_missing_customer_source"}
    current_text = " ".join(m["content"] for m in current)
    if re.fullmatch(r"[\s，。！？!?]*(你好|您好|在吗|谢谢|好的|再见)[\s，。！？!?]*", current_text):
        return {"route": "non_inventory", "reason": "plain_social_turn", "source_ids": [m["id"] for m in current]}
    budget = {"state": "not_mentioned", "source_ids": []}
    used = []
    # This grammar does not guess brands or strip adjectives. Any unconsumed
    # phrase (e.g. 纯电/年份/不要/刚才那辆) requires semantic supplementation.
    phrases = (
        "我想买", "想买", "买一台", "一台", "你们", "你这边", "请问", "那请问", "我想", "想看看", "看看",
        "有什么合适的车", "有什么车", "有哪些车", "什么车", "哪些车", "什么合适的车", "合适", "代步车", "二手车", "车",
        "总预算", "预算", "总价", "车价", "别超过", "不超过", "最多", "至多", "上限", "以内", "以下", "左右", "上下",
        "附近", "封顶", "出头", "的", "了", "吗", "呢", "呀", "啊", "我", "给", "看", "有", "买", "推荐", "先",
        "总共多少台", "多少辆", "多少台", "几辆", "几台", "价格区间", "最低价", "最高价",
    )
    filler = re.compile("|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)))
    for message in customer:
        text = message["content"]
        if re.fullmatch(r"[\s，。！？!?]*(你好|您好|在吗|谢谢|好的|再见)[\s，。！？!?]*", text):
            continue
        parsed = budget_statement(text)
        remainder = parsed["remainder"]
        order_spans = list(sort_instructions(remainder))
        for match, _, _ in reversed(order_spans):
            remainder = remainder[:match.start()] + " " * len(match.group()) + remainder[match.end():]
        remainder = filler.sub("", remainder)
        remainder = re.sub(r"[\s，,。；;！？!?、：:]", "", remainder)
        # Multiple monetary directives can express a correction/conflict. The
        # old parser's last-number policy alone cannot authorize this route.
        if remainder or parsed["unconsumed_comparisons"] or len(parsed["recognized_spans"]) > 1:
            return {"route": "semantic", "reason": "unresolved_conditions_or_context", "source_ids": [message["id"]]}
        if not parsed["present"] and not order_spans and not re.search(r"车|推荐|多少台|多少辆|几台|几辆|价格", text):
            return {"route": "semantic", "reason": "no_complete_catalog_request"}
        if parsed["present"]:
            value = parsed["budget"]
            if value is None:
                budget = {"state": "unlimited", "source_ids": [message["id"]]}
            elif value.kind == "floor":
                return {"route": "semantic", "reason": "budget_floor_requires_interpretation"}
            else:
                budget = {"state": "specified", "source_ids": [message["id"]],
                          "mode": {"cap": "upper", "range": "range", "around": "around"}[value.kind]}
                if value.kind == "range":
                    budget.update(lower=value.lower, upper=value.upper)
                else:
                    budget["value"] = value.target
        used.append(message["id"])
    raw = {"operation": "search", "requirement_state": "clear", "budget": budget,
           "filters": [], "name_queries": [], "reference_ids": [], "exclude_ids": [],
           "source_ids": used, "unsupported_requirements": [], "interpretation": "程序完整解析明确车辆请求"}
    preference = effective_customer_sort(sources, None, [])
    if preference:
        raw["sort"] = {"value": preference[0], "source_ids": preference[1]}
    normalized = normalize_model_query(raw, sources, contract=MODEL_QUERY_CONTRACT, known_ids=protocol.get("known_ids"),
                                       current_ids=current_ids)
    return {"route": "direct", "reason": "complete_explicit_conditions", "query": normalized, "source_ids": used}


def _ids(value: Any, *, field: str, allowed: set[str] | None = None, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v for v in value):
        raise CatalogQueryError("invalid_id_list", fields=[field])
    result = list(dict.fromkeys(value))
    if required and not result:
        raise CatalogQueryError("missing_message_source", fields=[field])
    if allowed is not None and not set(result) <= allowed:
        raise CatalogQueryError("message_source_not_in_frozen_conversation", fields=[field])
    return result


def _number(value: Any, *, field: str, zero: bool = False) -> float:
    if type(value) not in {int, float}:
        raise CatalogQueryError("invalid_numeric_constraint", fields=[field])
    try:
        number = float(value)
    except OverflowError as exc:
        raise CatalogQueryError("invalid_numeric_constraint", fields=[field]) from exc
    if not math.isfinite(number) or number < 0 or (number == 0 and not zero):
        raise CatalogQueryError("invalid_numeric_constraint", fields=[field])
    return number


def normalize_query(raw: Any, sources: list[dict], *, known_ids: dict | None = None) -> dict:
    keys = set(QUERY_REQUIRED_FIELDS)
    if not isinstance(raw, dict):
        raise CatalogQueryError("invalid_query_schema", fields=["catalog_query"])
    if set(raw) - {"name_queries"} != keys or type(raw.get("version")) is not int or raw["version"] != 1:
        fields = sorted(keys - set(raw))
        if set(raw) - keys - {"name_queries"}:
            fields.append("<unexpected_fields>")
        if type(raw.get("version")) is not int or raw.get("version") != 1:
            fields.append("version")
        raise CatalogQueryError("invalid_query_schema", fields=fields)
    for field, choices in (("operation", {"search", "details", "compare"}), ("requirement_state", {"clear", "uncertain"})):
        if not isinstance(raw[field], str) or raw[field] not in choices:
            raise CatalogQueryError("invalid_query_operation", fields=[field])
    allowed = {s["id"] for s in sources}
    query = deepcopy(raw)
    query["source_ids"] = _ids(raw["source_ids"], field="source_ids", allowed=allowed, required=True)
    for key in ("reference_ids", "exclude_ids"):
        query[key] = _ids(raw[key], field=key)
        if key == "reference_ids" and len(query[key]) > 20:
            raise CatalogQueryError("too_many_referenced_vehicles", fields=['reference_ids'])
    if set(query["reference_ids"]) & set(query["exclude_ids"]):
        raise CatalogQueryError("reference_exclusion_conflict", fields=['reference_ids', 'exclude_ids'])
    known_ids = known_ids or {}
    if not set(query["reference_ids"] + query["exclude_ids"]) <= set(known_ids):
        raise CatalogQueryError("vehicle_id_without_trusted_conversation_source", fields=['reference_ids', 'exclude_ids'])
    query["id_sources"] = {key: deepcopy(known_ids[key]) for key in query["reference_ids"] + query["exclude_ids"]}
    names = raw.get("name_queries", [])
    if not isinstance(names, list) or len(names) > 10:
        raise CatalogQueryError("invalid_name_queries", fields=['name_queries'])
    for index, name in enumerate(names):
        path = f"name_queries.{index}"
        if not isinstance(name, dict) or set(name) != {"text", "kind", "source_ids"}:
            raise CatalogQueryError("invalid_name_query", fields=[path])
        if not isinstance(name["kind"], str) or name["kind"] not in {"reference", "exclude"}:
            raise CatalogQueryError("invalid_name_query", fields=[f"{path}.kind"])
        if not isinstance(name["text"], str) or not 1 <= len(name["text"].strip()) <= 200:
            raise CatalogQueryError("invalid_name_query", fields=[f"{path}.text"])
        _ids(name["source_ids"], field=f"{path}.source_ids", allowed=allowed, required=True)
    query["name_queries"] = deepcopy(names)
    if raw["operation"] in {"details", "compare"} and not (query["reference_ids"] or any(n["kind"] == "reference" for n in names)):
        raise CatalogQueryError("missing_referenced_vehicle", fields=['reference_ids', 'name_queries'])
    if not isinstance(raw["sort"], str) or raw["sort"] not in {"price_asc", "price_desc", "budget_distance"}:
        raise CatalogQueryError("unsupported_sort", fields=['sort'])
    if not isinstance(raw["interpretation"], str) or not raw["interpretation"].strip() or len(raw["interpretation"]) > 1500:
        raise CatalogQueryError("missing_need_interpretation", fields=['interpretation'])
    unsupported = raw["unsupported_requirements"]
    if not isinstance(unsupported, list) or len(unsupported) > 20 or any(not isinstance(s, str) or len(s) > 200 for s in unsupported):
        raise CatalogQueryError("invalid_unsupported_requirements", fields=['unsupported_requirements'])
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
    _ids(budget.get("source_ids"), field="budget.source_ids", allowed=allowed, required=state in {"specified", "unlimited"})
    expected = {"state", "unit", "source_ids"}
    bounds = None
    if state == "specified":
        mode = budget.get("mode")
        if not isinstance(mode, str):
            raise CatalogQueryError("invalid_budget_mode", fields=["budget.mode"])
        if mode == "range":
            expected |= {"mode", "lower", "upper"}
            lower, upper = _number(budget.get("lower"), field="budget.lower", zero=True), _number(budget.get("upper"), field="budget.upper")
            if lower > upper:
                raise CatalogQueryError("inverted_budget_range", fields=['budget.lower', 'budget.upper'])
            target = (lower + upper) / 2
        elif mode in {"upper", "around"}:
            expected |= {"mode", "value"}
            target = _number(budget.get("value"), field="budget.value")
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
        raise CatalogQueryError("invalid_filters", fields=['filters'])
    for index, f in enumerate(filters):
        path = f"filters.{index}"
        if not isinstance(f, dict) or set(f) != {"field", "op", "value", "source_ids"}:
            raise CatalogQueryError("invalid_filter_schema", fields=[path])
        _ids(f["source_ids"], field=f"{path}.source_ids", allowed=allowed, required=True)
        for key in ("field", "op"):
            if not isinstance(f[key], str):
                raise CatalogQueryError("invalid_filter_schema", fields=[f"{path}.{key}"])
        if f["field"] in TEXT_FIELDS and f["op"] in {"eq", "in"}:
            values = [f["value"]] if f["op"] == "eq" else f["value"]
            if not isinstance(values, list) or not values or len(values) > 20 or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values):
                raise CatalogQueryError("invalid_text_filter", fields=[f"{path}.value"])
        elif f["field"] in NUMBER_FIELDS and f["op"] in {"lte", "gte"}:
            _number(f["value"], field=f"{path}.value", zero=True)
        else:
            raise CatalogQueryError("unsupported_filter_must_be_explicit", fields=[f"{path}.field", f"{path}.op"])
    for index, f in enumerate(query["filters"]):
        if f["field"] in {"energy_type", "drive_type", "series"}:
            try:
                f["value"] = canonical_enum(f["field"], f["value"]) if f["op"] == "eq" else [canonical_enum(f["field"], v) for v in f["value"]]
            except ValueError as exc:
                raise CatalogQueryError("unsupported_vehicle_enum", fields=[f"filters.{index}.value"]) from exc
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


def _selection_blockers(query: dict, names: list[dict], available_ids: set[str]) -> list[str]:
    """Locate uncertainty from validated conditions, never from interpretation prose."""
    reasons = []
    if query.get("unresolved_previous_exclusion"):
        reasons.append("unresolved_previous_exclusion")
    if query["budget"]["state"] == "uncertain":
        reasons.append("budget_uncertain")
    if any(n["status"] != "unique" for n in names):
        reasons.append("name_unresolved")
    for field in ("reference_ids", "exclude_ids"):
        if set(query[field]) - available_ids:
            reasons.append("missing_" + field)
    if query["requirement_state"] == "uncertain":
        reasons.append("uncertainty_scope_unresolved")
    if query["unsupported_requirements"]:
        reasons.append("unsupported_scope_unresolved")
    # Contradictory supported conditions cannot be interpreted as real zero stock.
    for field in sorted(TEXT_FIELDS | NUMBER_FIELDS):
        filters = [f for f in query["filters"] if f["field"] == field]
        if field in TEXT_FIELDS and filters:
            sets = [{str(v).strip().casefold() for v in ([f["value"]] if f["op"] == "eq" else f["value"])} for f in filters]
            if not set.intersection(*sets):
                reasons.append("conflicting_filter:" + field)
        elif filters:
            lower = max((f["value"] for f in filters if f["op"] == "gte"), default=0)
            upper = min((f["value"] for f in filters if f["op"] == "lte"), default=math.inf)
            if lower > upper:
                reasons.append("conflicting_filter:" + field)
    return reasons


def _known_conditions_evaluable(query: dict, blockers: list[str]) -> bool:
    hard = [reason for reason in blockers if reason not in {"uncertainty_scope_unresolved", "unsupported_scope_unresolved"}]
    return not hard and (not blockers or bool(query["filters"] or query["budget_bounds"] or query["exclude_ids"]))


def _receipt_query_blockers(receipt: dict) -> list[str] | None:
    query = receipt.get("query") or {}
    if not {"requirement_state", "budget", "budget_bounds", "filters", "reference_ids", "exclude_ids", "unsupported_requirements"} <= set(query):
        return None  # Older non-query/OCR projections retain their own contract.
    known_ids = set(query["reference_ids"] + query["exclude_ids"])
    known_ids -= set(receipt.get("missing_reference_ids", []) + receipt.get("missing_exclude_ids", []))
    return _selection_blockers(query, receipt.get("name_results", []), known_ids)


def receipt_state(receipt: dict) -> str:
    """One interpretation for new and persisted receipts; unknown never means zero."""
    if receipt.get("scan_complete") is not True:
        return "unavailable"
    if (_receipt_query_blockers(receipt) or receipt.get("clarification_reasons") or receipt.get("missing_reference_ids")
            or receipt.get("missing_exclude_ids")
            or any(n.get("status") != "unique" for n in receipt.get("name_results", []))):
        return "clarification_required"
    # Older uncertain receipts skipped every row. Their numeric zero/candidates
    # cannot acquire authority merely by being loaded by a newer consumer.
    if receipt.get("requirement_uncertain") and receipt.get("evaluation_version") != 1:
        return "clarification_required"
    if receipt.get("unsupported_requirements"):
        return "clarification_required"
    if receipt.get("unknown_field_count") or receipt.get("unknown_fields"):
        return "partial"
    if receipt.get("requirement_uncertain"):
        return "clarification_required"
    return "complete"


def receipt_view(receipt: dict) -> dict:
    """Sanitize a copy for consumers without rewriting historical evidence."""
    result = deepcopy(receipt)
    state = receipt_state(receipt)
    # Old clear queries really evaluated their supported conditions. Retain
    # those immutable facts for clarification, never their full-match license.
    query = receipt.get("query") or {}
    old_blockers = _receipt_query_blockers(receipt)
    legacy_evaluated = (receipt.get("evaluation_version") is None and receipt.get("scan_complete") is True
                        and old_blockers is not None and _known_conditions_evaluable(query, old_blockers)
                        and query["requirement_state"] == "clear" and not receipt.get("requirement_uncertain"))
    if legacy_evaluated and state == "clarification_required" and "known_conditions_evaluated" not in receipt:
        result.update(known_conditions_evaluated=True, known_condition_match_count=receipt.get("match_count"),
                      known_condition_facts=deepcopy(receipt.get("recommendations", [])),
                      known_condition_scope={"budget": deepcopy(query["budget"]), "filters": deepcopy(query.get("filters", [])),
                                             "exclude_ids": deepcopy(query.get("exclude_ids", [])), "evaluated": True,
                                             "scan_complete": True, "derived_from": "legacy_executed_query"})
        for product in result["known_condition_facts"]:
            product["selection_role"] = "known_condition_context"
    result["evaluation_state"] = state
    if state != "complete":
        result.update(match_count=None, price_min=None, price_max=None)
    if state in {"clarification_required", "unavailable"}:
        result["recommendations"] = []
        if not result.get("known_conditions_evaluated") or state == "unavailable":
            result.update(known_condition_match_count=None, known_condition_facts=[])
    return result


RECEIPT_INSTRUCTION = (
    "查询回执evaluation_state=complete才表示全部条件核实，match_count=0才是真零匹配。"
    "partial表示仅核实known_condition_scope；match_count=null不是0。可以介绍recommendations里的具体车型和真实价格，"
    "必须说明pending_conditions尚未核实，不得称候选满足全部条件或给全条件最低价。"
    "clarification_required表示关键条件不明确，必须用ask_clarifying_question针对原话中的实际未知点澄清，不能从空列表推断无车。"
    "known_condition_facts是已执行条件下的有序材料，举例也沿原排序、说明未知字段，不能说已按未支持的年份筛选。"
    "reply_target点名目标只核对所指车及其价格，不需要从全库第一辆说起；未唯一定位就澄清。"
    "库存数量、上下界和实际展示台数各按所指范围；客户说的首付、月供是客户意向，不是可办方案。"
    "如年份近一点基准不明，询问具体年份范围；不要绕开实际未知点机械询问预算。"
    "scanned_count只表示读取库存行数，不是匹配数或正文展示数；已知条件数量见known_condition_match_count，"
    "不得扩大其范围。依据、版本、来源均由程序维护，无需复制来源表。"
)


def _energy_distribution(values, unresolved=0):
    """Aggregate before truncation. Unknown records cannot establish absence."""
    counts = dict.fromkeys(ENERGY_TYPES, 0)
    for value in values:
        if value in counts:
            counts[value] += 1
        else:
            unresolved += 1
    return counts, unresolved


def _common_public_uses(details):
    """Keep only affirmative public uses shared by every car of an energy type."""
    common = {}
    for item in details:
        energy = item.get("energy_type")
        if energy not in ENERGY_TYPES:
            continue
        description = item.get("customer_description") or ""
        uses = set()
        if isinstance(description, str) and not re.search(r"不|未|仅|只|除非|但|如果|需要|需核实", description):
            for clause in re.split(r"[，,。；;！!]", description):
                stated = re.fullmatch(r"(?:适合|适用于)([\u4e00-\u9fff]{2,12})", clause.strip())
                if stated:
                    uses.add(stated[1])
                    uses.add(re.sub(r"^(?:日常城市|城市|日常|家庭)(?=.{2,})", "", stated[1]))
        if energy in common:
            common[energy].intersection_update(uses)
        else:
            common[energy] = uses
    return {key: sorted(values) for key, values in common.items() if values}


def execute_query(query: dict, runtime, *, scope: dict, limit: int = 8, sources=()) -> dict:
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import catalog_product_payload
    rows = inventory_snapshot(runtime)
    bounds = query["budget_bounds"]
    name_results = resolve_names(query["name_queries"], rows)
    # Ground an explicit target in customer speech and the same full inventory
    # used for selection. A guessed model name must not replace a named car.
    current = [m for m in sources if m["id"] in scope.get("current_message_ids", []) and m["sender"] in ("customer","client")]
    text = " ".join(m["content"] for m in current)
    focus = _named_request(text)
    if focus:
        from apps.wechat_ai_customer_service.workflows.vehicle_public_facts import _reply_entities
        labels = [{"id":r["id"],"name":r["data"].get("name"),"aliases":r["data"].get("aliases") or [],
                   "public_details":{"model":(r["data"].get("additional_details") or {}).get("model")}} for r in rows]
        mentioned = _reply_entities(text, labels)
        if mentioned:
            name_results = [n for n in name_results if n["kind"] != "reference"]
            names = [{"text":text[e["start"]:e["end"]],"kind":"reference",
                      "source_ids":[m["id"] for m in current if normalized_name(text[e["start"]:e["end"]]) in normalized_name(m["content"])]} for e in mentioned]
            name_results.extend(resolve_names(names,rows))
    blockers = _selection_blockers(query, name_results, {r["id"] for r in rows})
    can_evaluate = _known_conditions_evaluable(query, blockers)
    matches, references, unknown = [], [], {}
    excluded = set(query["exclude_ids"])
    ref_ids = set(query["reference_ids"])
    for match in name_results:
        if match["status"] == "unique":
            (ref_ids if match["kind"] == "reference" else excluded).update(match["matched_ids"])
    if ref_ids & excluded:
        raise CatalogQueryError("reference_exclusion_conflict", fields=['reference_ids', 'exclude_ids'])
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
        if row["id"] in excluded or not can_evaluate:
            continue
        missing = []
        fits = True
        if bounds:
            if not price_valid:
                missing.append("price")
            elif not bounds["lower"] <= price <= bounds["upper"]:
                fits = False
        for index, f in enumerate(query["filters"]):
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
        if missing and fits:
            unknown[row["id"]] = missing
        if fits and not missing:
            payload["selection_role"] = "budget_match" if bounds else "query_match"
            matches.append(payload)
    energy_counts, missing_energy = _energy_distribution(
        ((p.get("public_details") or {}).get("energy_type") for p in matches), len(unknown))
    stock_counts, stock_unknown = _energy_distribution(
        ((r["data"].get("additional_details") or {}).get("energy_type") for r in rows))
    energy_prices = {}
    for item in matches:
        energy = (item.get("public_details") or {}).get("energy_type")
        if energy not in ENERGY_TYPES:
            continue
        stats = energy_prices.setdefault(energy, {"min":None, "max":None, "unknown":0})
        price = item.get("price")
        if type(price) not in (float,int) or not math.isfinite(price) or price <= 0:
            stats["unknown"] += 1
        else:
            stats["min"] = min(price, stats["min"]) if stats["min"] is not None else price
            stats["max"] = max(price, stats["max"]) if stats["max"] is not None else price
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
               "requirement_uncertain": query["requirement_state"] == "uncertain" or bool(blockers), "name_results": name_results,
               "evaluation_version": 1, "clarification_reasons": blockers,
               "known_conditions_evaluated": can_evaluate,
               "known_energy_prices": energy_prices,
               "known_price_min": min(prices) if prices else None,
               "known_price_max": max(prices) if prices else None,
               "stock_energy_counts": stock_counts, "stock_energy_unknown_rows": stock_unknown,
               "stock_common_public_uses": _common_public_uses((r["data"].get("additional_details") or {}) for r in rows),
               "known_common_public_uses": _common_public_uses((p.get("public_details") or {}) for p in matches),
               "known_energy_counts": energy_counts if can_evaluate else None,
               "known_energy_unknown_rows": missing_energy if can_evaluate else None,
               "known_condition_match_count": len(matches) if can_evaluate else None,
               "known_condition_facts": [],
               "known_condition_scope": {"budget": deepcopy(query["budget"]), "filters": deepcopy(query["filters"]),
                                         "exclude_ids": sorted(excluded), "scan_complete": True, "evaluated": can_evaluate},
               "pending_conditions": {"unsupported_requirements": deepcopy(query["unsupported_requirements"]),
                                      "unknown_fields": deepcopy(unknown), "clarification_reasons": blockers},
               "missing_exclude_ids": sorted(excluded - {r["id"] for r in rows}),
               "price_unknown_count": len(matches) - len(prices),
               "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
               "recommendations": matches[:max(0, limit)], "references": references,
               "truncated": len(matches) > limit, "created_at": time.time()}
    if blockers and can_evaluate:
        receipt["known_condition_facts"] = receipt["recommendations"]
        for product in receipt["known_condition_facts"]:
            product["selection_role"] = "known_condition_context"
        receipt["recommendations"] = []
    return receipt_view(receipt)


def receipt_products(receipt: dict) -> list[dict]:
    """Project allowed vehicle materials once, including persisted receipts."""
    view = receipt_view(receipt)
    items = [*view.get("recommendations", []), *view.get("known_condition_facts", [])]
    known = {p["id"] for p in items}
    items.extend(p for p in view.get("references", []) if p["id"] not in known)
    return items


def _named_request(words):
    return bool(re.search(r"只(?:看|想|要|问)|价格|报价|售价|多少钱|介绍|对比|比较|车况|上牌|里程", words))


def _requests_other_choices(words):
    """Comparison of named cars is not a request to introduce another car."""
    for clause in re.split(r"[，,。；;！？!?]", words):
        if not re.search(r"别的|其他|其它|另(?:外|有)|同类|替代|备选", clause):
            continue
        if re.search(r"(?:不用|不必|无需|不要|别|不想|不看)(?:再|给我|帮我)?(?:推荐|介绍|看|考虑|其他|另外)", clause):
            continue
        if re.search(r"推荐|介绍|看看|还有|有没有|有哪些|挑|找|换|给我", clause):
            return True
    return False


def reply_target(receipt, evidence):
    """Describe the requested object, not the implementation of its stock lookup."""
    query = receipt["query"]
    ids = set(receipt.get("scope", {}).get("current_message_ids", []))
    current = [m for m in message_sources(evidence) if m["id"] in ids and m["sender"] in ("customer", "client")]
    words = " ".join(m["content"] for m in current)
    names = [n for n in receipt.get("name_results", []) if n["kind"] == "reference"]
    named = [n for n in names if any(m["id"] in n["source_ids"] and normalized_name(n["text"]) in normalized_name(m["content"]) for m in current)]
    # A named car can occur in a comparison or in a request for alternatives.
    # Such mentions alone do not turn a list into an exclusive price request.
    alternatives = _requests_other_choices(words)
    direct = query.get("operation") == "details" or bool(names and _named_request(words))
    if not direct:
        return {"kind": "list", "source_ids": [m["id"] for m in current]}
    requested = named or names
    if not named and names and not query.get("reference_ids"):
        return {"kind":"unresolved_reference", "source_ids":[m["id"] for m in current], "vehicle_ids":[]}
    resolved = all(n["status"] == "unique" for n in requested)
    target_ids = sorted({key for n in requested for key in n["matched_ids"]} or set(query["reference_ids"]))
    if not resolved or not target_ids or not set(target_ids) <= {p["id"] for p in receipt["references"]}:
        return {"kind":"unresolved_reference", "source_ids":[m["id"] for m in current], "vehicle_ids":[]}
    return {"kind":"reference_and_list" if alternatives else "named_reference", "source_ids":[m["id"] for m in current], "vehicle_ids":target_ids,
            "asks_price": bool(re.search(r"价格|报价|售价|多少钱|几万", words))}


def attach_receipt(evidence: dict, receipt: dict) -> None:
    receipt = receipt_view(receipt)
    preparation = (evidence.get("catalog_protocol") or {}).get("preparation")
    if preparation and receipt.get("query"):
        receipt["reply_target"] = reply_target(receipt, evidence)
    evidence["catalog_protocol"] = {"protocol": PROTOCOL, "phase": "queried", "receipt": receipt,
                                    "model_query_contract": model_contract(evidence.get("catalog_protocol") or {})}
    if preparation is not None:
        evidence["catalog_protocol"]["preparation"] = deepcopy(preparation)
    items = receipt_products(receipt)
    knowledge = evidence["knowledge"]
    knowledge["product_master"] = {"items": items, "catalog_selection": receipt,
                                   "authority_level": "product_master", "can_authorize_product_facts": True}
    knowledge["evidence"]["products"] = items
    knowledge["evidence"]["catalog_candidates"] = items
    evidence.setdefault("audit_summary", {})["catalog_selection"] = receipt
    # Preserve formal IDs, replace only product IDs through the existing collector.
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import collect_evidence_ids
    evidence["evidence_ids"] = collect_evidence_ids(knowledge)


def prepare_catalog_round(*, evidence: dict, settings: dict, brain_input: dict, runtime_factory) -> dict:
    """Prepare a direct receipt or record why the existing AI path is needed."""
    preparation = prepare_catalog_need(evidence)
    evidence["catalog_protocol"]["preparation"] = deepcopy(preparation)
    if preparation["route"] != "direct":
        return preparation
    runtime = runtime_factory()
    scope = {"conversation_id": brain_input["target"]["conversation_id"],
             "current_message_ids": brain_input["current_message"]["message_ids"]}
    receipt = execute_query(preparation["query"], runtime, scope=scope, sources=message_sources(evidence),
                            limit=min(20, max(1, int(settings.get("max_catalog_candidates") or 8))))
    attach_receipt(evidence, receipt)
    return preparation


def prompt_protocol(evidence: dict) -> dict | None:
    protocol = evidence.get("catalog_protocol")
    if not protocol:
        return None
    result = deepcopy(protocol)
    receipt = result.get("receipt")
    if receipt:
        receipt = result["receipt"] = receipt_view(receipt)
        receipt.pop("unknown_fields", None)
        pending = receipt.get("pending_conditions") or {}
        if "unknown_fields" in pending:
            pending["unknown_field_count"] = len(pending.pop("unknown_fields"))
        receipt.pop("created_at", None)
        for match in receipt.get("name_results", []):
            match["matched_ids"] = match["matched_ids"][:20]
        for key in ("recommendations", "references", "known_condition_facts"):
            if key in receipt:
                receipt[key] = [p["id"] for p in receipt[key]]
    return result


def query_plan_errors(plan: dict, evidence: dict) -> list[str]:
    protocol = evidence.get("catalog_protocol")
    if not protocol:
        return []  # OCR compatibility retains its existing contract.
    if any(k in plan for k in ("catalog_query", "catalog_receipt")):
        return ["catalog_intermediate_or_forged_receipt_not_sendable"]
    receipt = receipt_view(protocol.get("receipt") or {})
    # Real product facts still pass the existing fact/ID validator as well.
    products = (plan.get("evidence_used") or {}).get("product_ids") or []
    facts = [f for f in plan.get("facts_claimed", []) if f.get("source_level") == "product_master"]
    if (products or facts) and not receipt.get("scan_complete"):
        return ["current_inventory_not_queried"]
    errors = []
    for fact in plan.get("facts_claimed", []):
        if fact.get("source_level") == "catalog_query" or fact.get("fact_type") in AGGREGATES:
            field = AGGREGATES.get(fact.get("fact_type"))
            energy = fact.get("scope_energy") or []
            expected = scoped_aggregate_value(receipt,field,energy) if energy or field == 'stock_count' else receipt.get(field)
            if (not field or fact.get("source_level") != "catalog_query"
                    or (not energy and not aggregate_fact_ready(receipt, field))
                    or fact.get("source_id") != receipt.get("query_id") or expected is None
                    or (field in {"price_min", "price_max"} and receipt.get("price_unknown_count"))):
                errors.append("invalid_catalog_aggregate_source")
                continue
            try:
                value = Decimal(str(fact.get("value")))
                if not value.is_finite() or value != Decimal(str(expected)):
                    errors.append("catalog_aggregate_value_mismatch")
            except (InvalidOperation, ValueError):
                errors.append("invalid_catalog_aggregate_value")
    return errors


AGGREGATES = {"catalog_match_count": "match_count", "catalog_price_min": "price_min", "catalog_price_max": "price_max",
              "catalog_scanned_count": "scanned_count", "catalog_stock_count": "stock_count", "catalog_known_condition_count": "known_condition_match_count",
              "catalog_known_price_min":"known_price_min", "catalog_known_price_max":"known_price_max"}


def scoped_aggregate_value(receipt, field, energy):
    """Look up a witnessed metric in the full evaluated scope, never the displayed page."""
    if field == "stock_count":
        if not receipt.get("scan_complete") or not isinstance(energy, (list,tuple)):
            return None
        if not energy:
            return receipt.get("scanned_count")
        if receipt.get("stock_energy_unknown_rows") != 0 or not all(isinstance(k, str) and k in ENERGY_TYPES for k in energy):
            return None
        counts = receipt.get("stock_energy_counts") or {}
        return sum(counts[k] for k in set(energy)) if set(energy) <= set(counts) else None
    if not receipt.get("known_conditions_evaluated") or receipt.get("unknown_field_count"):
        return None
    if not energy:
        return receipt.get(field) if aggregate_fact_ready(receipt, field) else None
    if not isinstance(energy, (list,tuple)) or not all(isinstance(k,str) for k in energy):
        return None
    kinds = set(energy)
    if not kinds <= set(ENERGY_TYPES) or receipt.get("known_energy_unknown_rows") != 0:
        return None
    counts = receipt.get("known_energy_counts") or {}
    if field == "known_condition_match_count":
        return sum(counts.get(k, 0) for k in kinds)
    ranges = [v for k,v in (receipt.get("known_energy_prices") or {}).items() if k in kinds]
    if not ranges or any(v["unknown"] for v in ranges):
        return None
    if field == "known_price_min":
        return min(v["min"] for v in ranges)
    if field == "known_price_max":
        return max(v["max"] for v in ranges)
    return None


def aggregate_ready(receipt):
    return receipt_state(receipt) == "complete"


def aggregate_fact_ready(receipt, field):
    if field in {"scanned_count", "stock_count"}:
        return receipt.get("scan_complete") is True
    if field in {"known_condition_match_count", "known_price_min", "known_price_max"}:
        view = receipt_view(receipt)
        return bool(view.get("scan_complete") is True and view.get("known_conditions_evaluated")
                    and not view.get("unknown_field_count") and view.get(field) is not None
                    and (field == "known_condition_match_count" or not view.get("price_unknown_count")))
    return aggregate_ready(receipt)


def _source_statement_texts(content):
    return [part.strip() for part in re.findall(r".+?(?:[。！？!?；;\n]+|$)", content, re.S) if part.strip()]


def standard_source_refs(mapping):
    """Only complete, contiguous published answers are controlled policy text."""
    statements = list(mapping["statements"].items())
    texts = [row["text"].strip() for _, row in statements]
    refs = {}
    for key, source in mapping["knowledge"].items():
        if not source.get("release_id") or not source.get("revision_id"):
            continue
        parts = _source_statement_texts(source["content"])
        for start in range(len(texts)):
            if parts and texts[start:start + len(parts)] == parts:
                for sid, _ in statements[start:start + len(parts)]:
                    refs.setdefault(sid, []).append(key)
    return refs


def _bind_clearance(evidence, plan, kind, *, method, reason, refs, validated=None):
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import join_reply_segments
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import reply_authority_snapshot
    proof = {"reply": fingerprint(join_reply_segments(plan["reply_segments"]).strip()),
             "protocol": fingerprint(evidence["catalog_protocol"]), "kind": kind}
    if (evidence.get("catalog_protocol") or {}).get("preparation"):
        proof.update(version=2, method=method, reason=reason, source_refs=deepcopy(refs),
                     authority=fingerprint(reply_authority_snapshot(evidence)),
                     plan=fingerprint(plan), policy_map=fingerprint(evidence["policy_reference_map"]),
                     segment_vehicle_ids=deepcopy(validated["segment_vehicle_ids"]))
        proof['fact_findings'] = {k: deepcopy(validated[k]) for k in
            ('decision', 'semantic_findings', 'confirmed_conflicts', 'occurrences')}
        proof["segment_binding"] = fingerprint([proof["plan"],proof["reply"],proof["protocol"],proof["segment_vehicle_ids"]])
    evidence["_catalog_clearance"] = proof


def _content_rejection(errors):
    return {"ok": False, "enforced": True, "invoked": False, "status": "program_checked",
            "verdict": "repair", "reason": errors[0], "errors": list(dict.fromkeys(errors))}


def prepared_review_route(evidence, plan):
    """Return a content decision, or the concrete dependency needing a reviewer."""
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import prepare_policy_reference_map, is_appointment_schedule_question
    from apps.wechat_ai_customer_service.workflows.llm_reply_guard import extract_current_message_text, has_unqualified_commitment
    from apps.wechat_ai_customer_service.workflows.vehicle_public_facts import validate_reply_public_facts, validate_reviewed_reply_facts, derive_reviewed_fact_fields
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import validate_brain_plan, validate_policy_refs

    mapping = prepare_policy_reference_map(plan, evidence)
    refs = standard_source_refs(mapping)
    preliminary = validate_reply_public_facts(plan, evidence)
    remaining = " ".join(row["text"] for sid, row in mapping["statements"].items() if sid not in refs)
    errors = query_plan_errors(plan, evidence) + preliminary["hard_errors"]
    if has_unqualified_commitment(remaining):
        errors.append("unsafe_commitment_without_caution")
    if errors:
        return _content_rejection(errors)
    if preliminary["semantic_findings"]:
        return {"needs_semantic_review": True, "reason": "public_fact_semantics_pending",
                "findings": preliminary["semantic_findings"]}
    formal = validate_policy_refs(refs, segments=plan["reply_segments"], mapping=mapping,
                                  customer_statements=preliminary["customer_statements"])
    covered = {(n["segment_index"], n["start"], n["end"]) for n in formal["covered_numbers"]}
    policy_amount = any(o.get("pending_policy_review") and (o["segment_index"], o["start"], o["end"]) not in covered
                        for o in preliminary["occurrences"])
    question = extract_current_message_text(evidence)
    appointment_terms_in_reply = is_appointment_schedule_question(remaining) and (
        bool(re.search(r"提前|预约|排期|直接过去|门店确认|无需确认|保证|随时", remaining)))
    if (formal["errors"] or policy_amount or appointment_terms_in_reply
            or (not refs and is_appointment_schedule_question(question))):
        return {"needs_semantic_review": True, "reason": "free_policy_explanation"}
    validated = validate_reviewed_reply_facts(plan, evidence, refs=refs, mapping=mapping)
    if not validated["ok"]:
        return _content_rejection(validated["errors"])
    derive_reviewed_fact_fields(plan, validated)
    check = validate_brain_plan(plan, require_fact_claims=False)
    if not check["ok"]:
        return {"ok": False, "enforced": True, "invoked": False, "status": "program_checked",
                "verdict": "repair", "reason": "invalid_final_plan", "errors": check["errors"]}
    reason = "published_standard_answer" if refs else "routine_reply_checked"
    _bind_clearance(evidence, plan, "program_checked", method="program", reason=reason, refs=refs, validated=validated)
    return {"ok": True, "enforced": True, "invoked": False, "status": "program_checked",
            "reason": reason, "errors": []}


def record_semantic_clearance(evidence, plan, raw_review, review):
    """Semantic classification + mechanical authority; never accept model-issued proof."""
    evidence.pop("_catalog_clearance", None)
    kind = raw_review.get("catalog_claim_kind")
    errors = query_plan_errors(plan, evidence)
    receipt = (evidence.get("catalog_protocol") or {}).get("receipt") or {}
    prepared = bool((evidence.get("catalog_protocol") or {}).get("preparation"))
    validated = None
    if prepared:
        if review.get("customer_visible_risk") == "high":
            errors.append("semantic_reviewer_high_risk")
        from apps.wechat_ai_customer_service.workflows.vehicle_public_facts import validate_reviewed_reply_facts, derive_reviewed_fact_fields
        from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import policy_ref_errors
        from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import prepare_policy_reference_map
        mapping = prepare_policy_reference_map(plan, evidence)
        contract_errors = policy_ref_errors(raw_review.get("policy_refs"), mapping=mapping)
        if contract_errors:
            return {**review, "ok": False, "status": "error", "verdict": "block", "error_kind": "protocol",
                    "contract_errors": contract_errors, "errors": contract_errors, "enforced": True,
                    "reason": "policy_refs_invalid_contract", "soft_pass_low_risk": False}
        validated = validate_reviewed_reply_facts(plan, evidence, refs=raw_review["policy_refs"], mapping=mapping,
            semantic_approved=bool(review.get("ok") and raw_review.get("verdict") == "pass" and raw_review.get("status", "ok") == "ok"))
        errors.extend(validated["errors"])
        if validated["product_ids"] and kind != "vehicle_facts":
            errors.append("catalog_claim_classification_conflict")
    if kind not in {"non_inventory", "vehicle_facts", "query_summary", "uncertain"} or kind == "uncertain":
        errors.append("catalog_claim_classification_required")
    if kind in {"vehicle_facts", "query_summary"} and not receipt.get("scan_complete"):
        errors.append("current_inventory_not_queried")
    preparation = (evidence.get("catalog_protocol") or {}).get("preparation") or {}
    if kind == "query_summary" and (preparation.get("requires_recommendation")
            or preparation.get("answer_goal") == "uncertain"):
        # A reviewer-declared count-only answer cannot satisfy a known
        # recommendation request or resolve an unconfirmed answer goal.
        errors.append("catalog_summary_does_not_satisfy_answer_goal")
    if kind == "query_summary" and not prepared:
        aggregates = [f for f in (validated["facts_claimed"] if validated else plan.get("facts_claimed", [])) if f.get("fact_type") in AGGREGATES]
        if not aggregates or any(not aggregate_fact_ready(receipt, AGGREGATES[f["fact_type"]]) for f in aggregates):
            errors.append("catalog_summary_requires_complete_scoped_facts")
    if kind == "vehicle_facts" and not prepared and not any(f.get("source_level") == "product_master" for f in (validated["facts_claimed"] if validated else plan.get("facts_claimed", []))):
        errors.append("catalog_vehicle_facts_must_be_declared")
    if errors:
        return {**review, "ok": False, "verdict": "repair", "enforced": True,
                "errors": list(dict.fromkeys([*(review.get("errors") or []), *errors])), "soft_pass_low_risk": False}
    if review.get("ok") and raw_review.get("verdict") == "pass":
        if prepared:
            plan["policy_refs"] = {sid: list(dict.fromkeys(keys)) for sid, keys in raw_review["policy_refs"].items()}
            derive_reviewed_fact_fields(plan, validated)
            from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import validate_brain_plan
            final_validation = validate_brain_plan(plan, require_fact_claims=True)
            if not final_validation["ok"]:
                return {**review, "ok": False, "verdict": "repair", "enforced": True,
                        "errors": final_validation["errors"], "soft_pass_low_risk": False}
        _bind_clearance(evidence, plan, kind, method="semantic", reason="required_semantic_review",
                        refs=plan.get("policy_refs"), validated=validated)
    return review


def clearance_kind(reply, evidence):
    proof = evidence.get("_catalog_clearance") or {}
    if (evidence.get("catalog_protocol") or {}).get("preparation"):
        from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import reply_authority_snapshot
        try:
            if proof.get("authority") != fingerprint(reply_authority_snapshot(evidence)):
                return None
        except ValueError:
            return None
    if (proof.get("reply") == fingerprint(reply.strip())
            and proof.get("protocol") == fingerprint(evidence.get("catalog_protocol"))):
        return proof.get("kind")
    return None


def prepared_binding_matches(reply, plan, evidence):
    """Identity check only: the consumer does not interpret the text again."""
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import join_reply_segments
    proof = evidence.get("_catalog_clearance") or {}
    return bool(isinstance(plan, dict) and clearance_kind(reply, evidence)
                and proof.get("plan") == fingerprint(plan)
                and proof.get("policy_map") == fingerprint(evidence.get("policy_reference_map"))
                and reply.strip() == join_reply_segments(plan.get("reply_segments") or []).strip())


def serialized_clearance_valid(result: dict, *, require_current=False) -> bool:
    """Recheck the existing approval across the Provider JSON boundary."""
    protocol = result.get("catalog_protocol") or {}
    if not protocol.get("preparation"):
        return True  # Persisted/OCR results retain their existing contract.
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import join_reply_segments
    proof = result.get("_catalog_clearance") or {}
    snapshot = result.get("reply_authority_snapshot") or {}
    plan = result.get("brain_plan") or {}
    reply = str(result.get("reply_text") or "").strip()
    if not isinstance(proof, dict) or not isinstance(plan, dict) or not isinstance(snapshot, dict):
        return False
    checked_kind = proof.get("kind") in {"non_inventory", "vehicle_facts", "query_summary"}
    if "version" not in proof:
        # The prior prepared contract consisted of exactly these six digests/
        # fields. A damaged new proof must not be reinterpreted as an old one.
        if require_current or set(proof) != {"kind", "reply", "protocol", "authority", "plan", "policy_map"} or result.get("reply_check_history"):
            return False
    elif type(proof["version"]) is not int or proof["version"] != 2:
        return False
    else:
        if require_current or "segment_binding" in proof or "segment_vehicle_ids" in proof:
            segment_ids = proof.get("segment_vehicle_ids")
            if (not isinstance(segment_ids,list) or len(segment_ids) != len(plan.get("reply_segments") or [])
                    or any(not isinstance(group,list) or not all(isinstance(v,str) for v in group) for group in segment_ids)
                    or proof.get("segment_binding") != fingerprint([proof.get("plan"),proof.get("reply"),proof.get("protocol"),segment_ids])):
                return False
        history = result.get("reply_check_history") or []
        if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
            return False
        if history[-1].get("stage") != "consume_decision" or history[-1].get("ok") is not True or history[-1].get("reply") != proof.get("reply"):
            return False
        if not isinstance(proof.get("guard"), dict) or proof["guard"].get("allowed") is not True:
            return False
        if not isinstance(proof.get("content_result"), dict) or proof["content_result"].get("ok") is not True:
            return False
        mapping = result.get("policy_reference_map") or {}
        if not isinstance(mapping, dict) or not isinstance(mapping.get("knowledge"), dict) or not isinstance(mapping.get("statements"), dict):
            return False
        if proof.get("method") == "program":
            checked_kind = (proof.get("kind") == "program_checked" and
                            proof.get("source_refs") == standard_source_refs(mapping) and
                            proof.get("reason") in {"routine_reply_checked", "published_standard_answer"})
        elif proof.get("method") == "semantic":
            reviews = result.get("review_history") or []
            if not reviews or not isinstance(reviews[-1], dict):
                return False
            reviewed = reviews[-1]
            raw = reviewed.get("raw_result") or {}
            request = reviewed.get("request") or {}
            checked_kind = (checked_kind and raw.get("verdict") == "pass" and raw.get("status", "ok") == "ok"
                            and raw.get("catalog_claim_kind") == proof.get("kind")
                            and raw.get("policy_refs") == proof.get("source_refs") == plan.get("policy_refs")
                            and reviewed.get("authority_sha256") == proof.get("authority")
                            and request.get("draft_reply") == reply and request.get("policy_reference_map") == mapping)
        else:
            return False
    return bool(reply and checked_kind
                and reply == join_reply_segments(plan.get("reply_segments") or []).strip()
                and proof.get("reply") == fingerprint(reply)
                and proof.get("protocol") == fingerprint(protocol)
                and proof.get("plan") == fingerprint(plan)
                and snapshot.get("sha256") == proof.get("authority") == fingerprint(snapshot.get("content"))
                and proof.get("policy_map") == fingerprint(result.get("policy_reference_map"))
                and (result.get("policy_reference_map") or {}).get("reply_sha256") == fingerprint(plan.get("reply_segments")))


def catalog_generation_audit(result: dict) -> dict:
    """One metadata-only projection for understanding and completion stages.

    Recovery summaries are already bounded by Brain's existing retry policy.
    Keep those summaries, never the model response body or full prompt.
    """
    return {key: deepcopy(result[key]) for key in (
        "ok", "usage", "prompt_estimate", "provider", "model", "status", "elapsed_ms",
        "error", "error_kind", "http_status", "generation_stage", "json_structure_repair",
        "json_structure_repaired", "same_capture_retry", "same_capture_parse_retry_after_unavailable",
        "previous_llm_status", "retry_reason",
    ) if key in result}


def run_catalog_round(*, first_result: dict, settings: dict, brain_input: dict, run_llm, runtime_factory) -> dict:
    """At most one read-only query then one completion, inside Brain's time budget."""
    evidence = brain_input["evidence"]
    first = first_result.get("brain_plan") or {}
    if (evidence.get("catalog_protocol") or {}).get("phase") == "queried":
        # Program-prepared completion may not reopen inventory or replace the
        # frozen conditions. Any query in it reaches the ordinary validator.
        return first_result
    query_without_wrapper = "catalog_query" not in first and "operation" in first and ("budget" in first or "filters" in first)
    if not first_result.get("ok") or ("catalog_query" not in first and not query_without_wrapper):
        return first_result
    audit = {"understanding": catalog_generation_audit(first_result),
             "model_query_contract": model_contract(evidence.get("catalog_protocol") or {})}
    started = time.monotonic()
    phase = "validation"
    try:
        if "catalog_query" not in first:
            raise CatalogQueryError("catalog_query_wrapper_required", fields=["catalog_query"])
        if set(first) != {"catalog_query"}:
            raise CatalogQueryError("query_and_reply_must_be_separate", fields=["catalog_query"])
        query = normalize_model_query(first["catalog_query"], message_sources(evidence),
                                      contract=model_contract(evidence.get("catalog_protocol") or {}),
                                      known_ids=(evidence.get("catalog_protocol") or {}).get("known_ids"),
                                      current_ids=(brain_input.get("current_message") or {}).get("message_ids"))
        audit["validation_seconds"] = time.monotonic() - started
        phase = "inventory_query"
        runtime = runtime_factory()
        scope = {"conversation_id": brain_input["target"]["conversation_id"],
                 "current_message_ids": brain_input["current_message"]["message_ids"]}
        receipt = execute_query(query, runtime, scope=scope, sources=message_sources(evidence), limit=min(20, max(1, int(settings.get("max_catalog_candidates") or 8))))
        attach_receipt(evidence, receipt)
        audit["query_seconds"] = time.monotonic() - started
        phase = "completion"
        from apps.wechat_ai_customer_service.llm_config import llm_stage_context
        with llm_stage_context("completion"):
            final = run_llm(settings=settings, brain_input=brain_input)
        audit["completion"] = catalog_generation_audit(final)
        if final.get("ok"):
            candidate = final.get("brain_plan") or {}
            errors = query_plan_errors(candidate, evidence)
            # These are local content/contract findings after a successful
            # model response, not an inventory/service outage. Preserve the
            # original candidate in the existing protected attempt snapshot;
            # the normal Brain validator owns its finite repair path.
            audit["completion_validation"] = {
                "stage": "completion_validation", "ok": not errors,
                "error_codes": errors, "candidate": deepcopy(candidate),
                "candidate_sha256": fingerprint(candidate),
                "query_id": receipt["query_id"], "snapshot_id": receipt["snapshot_id"],
                "model_query_contract": model_contract(evidence.get("catalog_protocol") or {}),
            }
            if final.get("raw_response_text"):
                audit["completion_validation"]["raw_response_text"] = final["raw_response_text"]
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
                "field_evidence": ([{"field": "catalog_query", "present": False}] if query_without_wrapper else
                                   rejected_field_evidence(first.get("catalog_query"), exc.fields)),
                "query_sha256": fingerprint(first.get("catalog_query", first)),
                "envelope_present": "catalog_query" in first,
                "repair_instruction": query_format_instruction(model_contract(evidence.get("catalog_protocol") or {})),
            }
        return {"ok": False, "error": str(exc) if isinstance(exc, CatalogQueryError) else "catalog_query_unavailable",
                "error_kind": "catalog_query_validation" if invalid_query else "catalog_query_execution",
                "status": "error", "http_status": first_result.get("status"),
                "catalog_stages": audit, "provider": first_result.get("provider"), "model": first_result.get("model")}


def trusted_vehicle_ids(target_state, batch, conversation_id):
    """Only the validated backend context can contribute IDs, never Brain output/context hints."""
    context = target_state.get("chejin_brain_context") or {}
    if context.get("conversation_id") != conversation_id or not conversation_id:
        return {}
    ids = {}
    # Select only the latest self turn. An unrelated later sales message or
    # multiple vehicle IDs does not identify "the previous one".
    history = context.get("history") or []
    self_rows = [r for r in history if r.get("sender_role") == "self"]
    last = (self_rows[-1].get("confirmed_reply") or {}) if self_rows else {}
    if last:
        for row in self_rows:
            proof = row.get("confirmed_reply") or {}
            if proof.get("batch") == last["batch"]:
                for vehicle in proof["vehicles"]:
                    ids.setdefault(vehicle, []).append({"source_id":row["message_event_id"],"conversation_id":conversation_id,
                                                        "kind":"confirmed_native_reply","batch":proof["batch"],"native":proof["native"]})
    for row in [*(context.get("history") or []), *batch]:
        key = row.get("id") or row.get("message_event_id")
        product = row.get("server_validated_product_id")
        if row.get("message_type") == "image" and key and isinstance(product, str) and product:
            ids.setdefault(product, []).append({"source_id": key, "conversation_id": conversation_id,
                                                 "kind": "backend_validated_image"})
    return ids
