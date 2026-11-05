"""Read-only projection of saved, customer-visible CheJin vehicle details."""
from typing import Any
from copy import deepcopy
import hashlib
import json
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
            # A model suffix/digit is meaningful (A4 is not A45 or A4L).
            if (label[-1].isascii() and label[-1].isalnum() and match.end()<len(normalized)
                    and normalized[match.end()].isascii() and normalized[match.end()].isalnum()):
                continue
            matches.append({"start": positions[match.start()], "end": positions[match.end() - 1] + 1, "ids": sorted(ids)})
    chosen = []
    for match in sorted(matches, key=lambda m: (-(m["end"] - m["start"]), m["start"])):
        if not any(m["start"] <= match["start"] and match["end"] <= m["end"] for m in chosen):
            chosen.append(match)
    return sorted(chosen, key=lambda m: m["start"])


def _number_entity_owners(text: str, entities: list[dict], numbers: list[dict]) -> dict[int, list[int]]:
    """Resolve explicit local syntax, never match a number by its stock value.

    Adjectival numbers belong to the following name; an explicit 分别/均
    coordination owns its list. Remaining postfix facts need one local subject.
    Ambiguous coordinated lists deliberately stay unassigned.
    """
    owners = {}
    joiner = r"\s*(?:和|及|与|以及|还有|、|，|,)\s*"
    groups = []
    for index, entity in enumerate(entities):
        if groups and re.fullmatch(joiner, text[entities[groups[-1][-1]]["end"]:entity["start"]]):
            groups[-1].append(index)
        else:
            groups.append([index])
    for group in groups:
        if len(group) < 2:
            continue
        last = entities[group[-1]]
        following = [n for n in numbers if n["start"] >= last["end"]]
        if not following:
            continue
        first = following[0]
        bridge = text[last["end"]:first["start"]]
        # Require an explicit relationship rather than guessing list order.
        marker = re.fullmatch(r"\s*(分别|均|都)(?:的)?(?:报价|售价|价格|车价|表显里程|里程)?(?:为|是|分别为)?\s*[:：]?\s*", bridge)
        if not marker:
            continue
        if marker[1] in ("均", "都"):
            owners[first["start"]] = group
            continue
        values = [first]
        for number in following[1:]:
            if number["unit"] != first["unit"] or not re.fullmatch(joiner, text[values[-1]["end"]:number["start"]]):
                break
            values.append(number)
        if len(values) == len(group):
            owners.update({n["start"]: [i] for n, i in zip(values, group)})
    for number in numbers:
        if number["start"] in owners:
            continue
        following = next((i for i, e in enumerate(entities) if e["start"] >= number["end"]), None)
        if following is not None and re.fullmatch(r"\s*(?:左右)?的?\s*", text[number["end"]:entities[following]["start"]]):
            owners[number["start"]] = [following]
            continue
        preceding = [i for i, e in enumerate(entities) if e["end"] <= number["start"]]
        if not preceding:
            continue
        index = preceding[-1]
        bridge = text[entities[index]["end"]:number["start"]]
        group = next(g for g in groups if index in g)
        # A sentence boundary or an unqualified multi-subject list does not
        # identify a vehicle. Explicit references are entities of their own.
        continued_subject = re.fullmatch(r"[。；;]\s*(?:报价|售价|价格|车价|表显里程|里程)(?:为|是)?\s*[:：]?\s*", bridge)
        if len(group) == 1 and (continued_subject or not re.search(r"[。！？!?；;]|分别|均|都", bridge)):
            owners[number["start"]] = [index]
    return owners


def _reply_references(text: str, entities: list[dict], prior: list[dict]) -> list[dict]:
    """Resolve explicit anaphora only when the antecedent is unambiguous."""
    result = list(entities)
    if not entities and prior and re.match(r"\s*(?:报价|售价|车价|表显里程|里程)(?:为|是)?", text):
        result.append({"start": 0, "end": 0, "ids": list(dict.fromkeys(i for e in prior for i in e["ids"]))})
    for match in re.finditer(r"前者|后者|这辆(?:车)?|该车|它", text):
        if any(e["start"] <= match.start() < e["end"] for e in entities):
            continue
        ordinal = match[0] in ("前者", "后者")
        previous = [e for e in (entities if ordinal else result) if e["end"] <= match.start()]
        if previous:
            # Keep the subjects of the last sentence with actual mentions.
            boundary = text.rfind("。", 0, previous[-1]["start"])
            previous = [e for e in previous if e["start"] > boundary]
        else:
            previous = prior
        ids = list(dict.fromkeys(i for e in previous for i in e["ids"]))
        if ordinal:
            ids = [ids[0 if match[0] == "前者" else 1]] if len(ids) == 2 else []
        if ids:
            result.append({"start": match.start(), "end": match.end(), "ids": ids})
    return sorted(result, key=lambda e: e["start"])


def _customer_budget_spans(text: str, entities: list[dict], budget: dict) -> list[tuple[int, int]]:
    """Locate requirement restatements, then compare the complete relation.

    Equality of a lone number is insufficient: a named vehicle or fee owns its
    amount, and a range/cap/around must retain the frozen customer's meaning.
    """
    from apps.wechat_ai_customer_service.workflows.catalog_budget import budget_statement, amount_role
    if budget.get("state") != "specified":
        return []
    expected = (budget.get("mode"), *(public_number(budget.get(k)) for k in ("value", "lower", "upper")))
    spans = []
    for clause in re.finditer(r"[^，,。；;！？!?]+", text):
        first_entity = next((e for e in entities if clause.start() <= e["start"] < clause.end()), None)
        value = text[clause.start():first_entity["start"] if first_entity else clause.end()]
        parsed = budget_statement(value)
        actual = parsed["budget"]
        if not actual or not parsed["recognized_spans"] or parsed["unconsumed_comparisons"]:
            continue
        mode = {"cap": "upper", "range": "range", "around": "around"}.get(actual.kind)
        candidate = (mode, None, actual.lower, actual.upper) if mode == "range" else (mode, actual.target, None, None)
        if candidate != expected:
            continue
        recognized = parsed["recognized_spans"]
        # More than one unrelated monetary expression is not one requirement.
        if len(recognized) != 1:
            continue
        start, end = recognized[0]
        absolute_start = clause.start() + start
        role = amount_role(text, absolute_start)
        if role not in (None, "预算"):
            continue
        if any(clause.start() <= e["start"] < absolute_start for e in entities):
            continue
        tail = value[end:]
        if re.match(r"\s*(?:的)?(?:首付|月供|定金|订金|手续费|服务费|车价|售价|报价|价格)", tail):
            continue
        if re.search(r"原来|以前|之前|过去|旧预算|不是|并非|不要|不看|不选", value[:start]):
            continue
        # A naked amount needs an explicit requirement relation or a budget
        # noun; '4万的A' is an adjectival price, not a customer budget.
        relation = re.search(r"(?:万|元)内|以内|以下|封顶|左右|上下|附近|出头|不超过|最多|至多|上限|到|至|[-~—～－]", value)
        if role != "预算" and not relation and not re.match(r"\s*(?:这个|的)?(?:总预算|预算|区间|价位|范围)", tail):
            continue
        if re.fullmatch(r"\s*(?:左右)?的?\s*", tail) and first_entity:
            continue
        spans.append((absolute_start, clause.start() + end))
    return spans


def _scanned_count_scope(text: str, number: dict) -> bool:
    """Distinguish explicit inventory/reading scope from a displayed car list."""
    start = max((m.end() for m in re.finditer(r"[，,。；;！？!?\n]", text[:number["start"]])), default=0)
    prefix, tail = text[start:number["start"]], text[number["end"]:]
    read_scope = (re.search(r"(?:查了|读取了|看过)(?:目前|当前)?(?:这|那)?$", prefix)
                  and re.match(r"(?:在售|库存)(?:车源|车辆|车)", tail))
    total_scope = re.fullmatch(r"(?:目前|当前|全店)?(?:的)?(?:在售车源|在售车辆|库存)(?:一共|总共|共|有|是)?", prefix)
    deictic_scope = (re.fullmatch(r"(?:目前|当前)(?:这|那)?", prefix)
                      and re.match(r"(?:在售|库存)(?:车源|车辆|车)", tail))
    return bool(read_scope or total_scope or deictic_scope)


def _known_count_scope(text: str, number: dict, receipt: dict) -> bool:
    """A count must name the actually executed scope, not borrow an equal value."""
    start = max((m.end() for m in re.finditer(r"[，,。；;！？!?\n]", text[:number["start"]])), default=0)
    prefix = text[start:number["start"]]
    match = re.fullmatch(r"(?:我(?:这边)?)?按(?:照)?(.+?)(?:查到|找到|筛出|筛到|匹配到)(?:了)?", prefix)
    if not match:
        return False
    scope_text = match[1]
    if re.fullmatch(r"(?:当前|目前)?已(?:核实|验证|明确)(?:的)?(?:条件|范围)", scope_text):
        return True
    scope = receipt.get("known_condition_scope") or {}
    # Every explicit condition must account for the executed scope. Reuse
    # the budget relation checker; equal endpoints alone are not authority.
    if scope.get("exclude_ids"):
        return False
    budget = scope.get("budget") or {}
    specified = budget.get("state") == "specified"
    remainder = scope_text
    if specified:
        if not _customer_budget_spans(scope_text, [], budget):
            return False
        from apps.wechat_ai_customer_service.workflows.catalog_budget import budget_statement
        remainder = budget_statement(scope_text)["remainder"]
        remainder = re.sub(r"总预算|预算|总价", "", remainder)
    filters = scope.get("filters") or []
    if not filters and not specified:
        return False
    for item in filters:
        if item["op"] not in {"eq", "in"}:
            return False
        values = [item["value"]] if item["op"] == "eq" else item["value"]
        labels = [ENERGY_TYPES.get(v, v) if item["field"] == "energy_type" else
                  DRIVE_TYPES.get(v, v) if item["field"] == "drive_type" else v for v in values]
        if item["field"] == "energy_type" and set(values) == set(ENERGY_TYPES) - {"fuel"}:
            labels = ["新能源"]
        for label in sorted(labels, key=len, reverse=True):
            if label not in remainder:
                return False
            remainder = remainder.replace(label, "", 1)
    return bool(re.fullmatch(r"[\s、和或及的车条件类型范围]*", remainder))


def _inventory_absences(text: str):
    """Recognizable absence assertions with local polarity, not a semantic parser."""
    pattern = re.compile(rf"(?:没有|暂无|暂时没有|暂时没|未|没)(?:找到|匹配到|筛到|筛出)?(?:的)?(?:符合[^，,。；;！？!?]*?|合适的?|匹配的?|在售的?|{_ENERGY_LABEL})?(?:车源|车辆|车款|车型|车子|车)")
    for clause in re.finditer(r"[^，,。；;！？!?]+[，,。；;！？!?]?", text):
        previous_end = 0
        for match in pattern.finditer(clause.group()):
            prefix = clause.group()[previous_end:match.start()]
            suffix = clause.group()[match.end():]
            previous_end = match.end()
            # A vehicle noun must be the absence object, not just the prefix
            # of a longer object (车辆的上牌资料 / 车款年份信息). Only a noun
            # boundary or a vehicle predicate makes this a stock assertion;
            # other object phrases remain with the semantic reviewer.
            vehicle_object = re.match(r"(?:的?库存)?(?:[，,。；;！？!?]|$)|(?:的)?(?:了|呢|啊|呀)(?:[，,。；;！？!?]|$)|(?:可|能|供|给|卖|推荐|符合|满足|适合|合适|在售|出售|您|你|我)", suffix)
            if not vehicle_object:
                continue
            # A denial/question governs only the immediately following
            # assertion. It cannot excuse a later claim after 但/可是/而是.
            denial = re.search(r"(?:并非|并不是|不是|不能说|不能认定|不能判断|不能断定|不代表|不等于)\s*[‘“\"']?\s*$", prefix)
            question = prefix.endswith("有") or bool(re.fullmatch(r"[吗么呢]?\s*[？?]", suffix))
            corrected_quote = bool(re.match(r"[’”\"'](?:这个|这种|的)?(?:说法|结论)(?:不对|不准确|不成立)", suffix))
            if not (denial or question or corrected_quote):
                yield clause.start() + match.start(), clause.start() + match.end()


def _display_counts(segments: list[str], products: list[dict], receipt: dict | None = None) -> dict[tuple[int, int], dict]:
    """Resolve a displayed list's cardinality independently of stock counts."""
    text = "\n".join(segments)
    entities = _reply_entities(text, products)
    counts = [n for n in factual_number_spans(text, excluded=[(e["start"], e["end"]) for e in entities]) if n["unit"] == "台"]
    result = {}
    for number in counts:
        start = max((m.end() for m in re.finditer(r"[，,。；;！？!?\n]", text[:number["start"]])), default=0)
        prefix = text[start:number["start"]]
        retrospective = bool(re.search(r"(?:这|那|上述|以上)(?:共|一共)?\s*$", prefix))
        prospective = bool(re.search(r"(?:看|推荐|介绍|给|补|列|挑|选|说)", prefix))
        inventory = bool(_stock_count_predicate(prefix)
                         or _scanned_count_scope(text, number) or _known_count_scope(text, number, receipt or {}))
        if inventory or not (retrospective or prospective):
            continue
        forward = prospective and (not retrospective or re.search(r"先|再|接下来", prefix)
                                    or re.match(r"\s*[:：]",text[number["end"]:]))
        if retrospective and not forward:
            candidates = [e for e in entities if e["end"] <= number["start"]]
            if candidates:
                sentences = list(re.finditer(r"[^。；;！？!?\n]+", text[:number["start"]]))
                boundary = candidates[-1]["start"]
                found = False
                for sentence in reversed(sentences):
                    mentioned = [e for e in candidates if sentence.start() <= e["start"] < sentence.end()]
                    if mentioned:
                        boundary = sentence.start()
                        found = True
                    elif found:
                        break
                candidates = [e for e in candidates if e["start"] >= boundary]
        else:
            # Follow one explicit list through consecutive entity sentences.
            # An introductory colon may bridge a physical reply segment; a
            # new list/count or a non-list sentence closes this scope.
            next_count = next((n["start"] for n in counts if n["start"] > number["end"]), len(text))
            candidates = []
            for index, sentence in enumerate(re.finditer(r"[^。；;！？!?\n]+", text[number["end"]:next_count])):
                a, b = number["end"] + sentence.start(), number["end"] + sentence.end()
                mentioned = [e for e in entities if a <= e["start"] < b]
                if not mentioned:
                    if index == 0 and re.search(r"[:：]\s*$", sentence.group()):
                        continue
                    break
                if candidates and re.match(r"\s*(?:另外一组|另一组|换个话题|至于|再推荐|再补|再给)", sentence.group()):
                    break
                candidates.extend(mentioned)
        # A later sentence discussing existing members adds no new member.
        # Repetition inside the actual list remains visible and fails below.
        groups = []
        for entity in candidates:
            if not groups or re.search(r"[。；;！？!?\n]", text[groups[-1][-1]["end"]:entity["start"]]):
                groups.append([])
            groups[-1].append(entity)
        members = []
        for group in groups:
            known = {i for e in members for i in e["ids"]}
            if all(len(e["ids"]) == 1 and e["ids"][0] in known for e in group):
                continue
            members.extend(group)
        candidates = members
        ids = [e["ids"][0] for e in candidates if len(e["ids"]) == 1]
        valid = bool(candidates) and len(ids) == len(candidates) == len(set(ids)) and public_number(number["value"]) == len(ids)
        offset = 0
        for segment_index, segment in enumerate(segments):
            if number["start"] < offset + len(segment):
                break
            offset += len(segment) + 1
        result[(segment_index, number["start"] - offset)] = {"verified": valid, "role": "display_count", "product_ids": ids,
            "expected_count": len(set(ids)), "rejection_reason": None if valid else "display_count_scope_mismatch"}
    return result


def _vehicle_year_assertion(text, number, entities):
    """A year is not a stock fact until the sentence attributes it to a car."""
    left = re.split(r"[，,。！？!?；;]", text[:number["start"]])[-1]
    right = re.split(r"[，,。！？!?；;]", text[number["end"]:])[0]
    start = number["start"] - len(left)
    # Use the relation to the number, not the punctuation ending the sentence.
    # A later question does not erase an earlier assertion about an actual car.
    named = [e for e in entities if start <= e["start"] < number["start"]]
    subject_end = named[-1]["end"] - start if named else None
    subject = re.search(r"(?:这|那|该)(?:辆|台|款)?车(?:辆|型|源)?|(?:现车|在售车辆)", left)
    if subject:
        subject_end = max(subject_end or 0, subject.end())
    if subject_end is not None:
        relation = left[subject_end:].strip()
        if re.fullmatch(r"(?:的|是|在|于|为|已|约|首次|实际|上牌|登记|注册|出厂|生产|时间|日期|年份|年款|车龄|有|：|:|\s)*", relation):
            return True
    # Explicit attribute/value assignment can stand alone in the same reply.
    if re.search(r"(?:上牌|登记|出厂|生产)(?:年份|时间|日期)?(?:为|是|：|:)\s*$", left):
        return True
    # An identified vehicle following its registration date is still a claim.
    return bool(re.match(r"(?:首次)?(?:上牌|登记|注册|出厂)(?:的)?", right) and
                any(number["end"] <= e["start"] < number["end"] + len(right) for e in entities))


# Energy scope uses the inventory's enum, including the broader nonfuel group.
_ENERGY_SCOPE = {label.removeprefix("新能源"): {key} for key, label in ENERGY_TYPES.items()}
_ENERGY_SCOPE["新能源"] = set(ENERGY_TYPES) - {"fuel"}
_ENERGY_LABEL = "|".join(sorted(map(re.escape, {*_ENERGY_SCOPE, *ENERGY_TYPES.values()}), key=len, reverse=True))
_PRESENCE = re.compile(
    rf"有(?:在售)?(?P<kind>{_ENERGY_LABEL})?(?:现车|车源|车辆|车型|车)"
    rf"|(?P<topic>{_ENERGY_LABEL})(?:车型?|车源)?[^，。；！？]{{0,7}}?(?:是有的|有现车|有车源)")


def _energy_present(receipt, label, *, whole_stock=False):
    """True/False only when the receipt proves it. Old/incomplete data can be unknown."""
    kinds = _ENERGY_SCOPE.get(label.removeprefix("新能源") or "新能源", set())
    scope = "stock" if whole_stock else "known"
    counts = receipt.get(f"{scope}_energy_counts")
    if not isinstance(counts, dict):
        return None
    if any(counts.get(k, 0) for k in kinds):
        return True
    if receipt.get(f"{scope}_energy_unknown_rows") == 0:
        return False
    return None


def _unsupported_condition_claims(text, entities):
    """Distinguish requirements from assertions about an actual vehicle's history."""
    for sentence in re.finditer(r"[^。；;！？!?]+", text):
        value = sentence.group()
        for condition in re.finditer(r"(?:无|没有|未(?:发生|经过)?)(?:过)?(?:大修|事故|水泡|火烧)", value):
            before = value[:condition.start()]
            subject = re.search(r"(?:这|那|该)(?:一)?(?:台|辆|款)(?:车)?|现车|在售车(?:源|辆)?|我们(?:这边)?有(?:一)?[辆台]", before)
            named = [e for e in entities if sentence.start() <= e["start"] < sentence.start()+condition.start()]
            if not subject and not named:
                continue
            subject_end = max(subject.end() if subject else 0,
                              named[-1]["end"] - sentence.start() if named else 0)
            relation = before[subject_end:]
            if not re.search(r"(?:希望|想要|需求|要求|待核实|未核实|不能确认|是否|有没有|不能保证)", relation):
                yield "vehicle_history_assertion_not_verified"


def _inventory_assertion_errors(text, receipt):
    """Check an explicit existence or resolved-condition assertion at its stated scope."""
    from apps.wechat_ai_customer_service.workflows.catalog_query import receipt_state
    partial = receipt_state(receipt) != "complete"
    for clause in re.split(r"[。；;，,]", text):
        qualified = re.search(r"(?:尚未|暂未|不能确认|待核实|需核实|还没|是否|有没有)[^。；！？]*$", clause)
        if partial and re.search(r"年份|车龄|上牌", " ".join(receipt.get("unsupported_requirements", []))):
            unsupported_filter = re.search(r"(?:已(?:经)?|能|可以|我(?:再|会|来)?|系统(?:可|会)?)(?:按|以)(?:具体|上牌)?(?:年份|年限|车龄).{0,8}(?:筛|过滤)",clause)
            if unsupported_filter and not re.search(r"(?:无法|没法|不能|待核实后|核实后|确认后|核实之后|确认之后)",clause[:unsupported_filter.end()]):
                yield "unsupported_year_filter_promised"
        if partial and not qualified and re.search(r"(?:车|款|车型).{0,5}(?:都|均|已)?(?:符合|满足).{0,14}(?:年份|车龄|全部条件|所有条件)", clause):
            yield "unverified_full_condition_match"
        # A positive inventory assertion is distinct from speaking about
        # missing records or asking about a customer's requirements.
        for claim in _PRESENCE.finditer(clause):
            before, after = clause[:claim.start()], clause[claim.end():]
            if (re.search(r"(?:没|无|不|是否|能否|不能确认|不能说|无法确认|不代表|不等于)$", before)
                    or re.fullmatch(r"[吗么呢]?\s*[？?]", after)):
                continue
            label = claim["kind"] or claim["topic"]
            if label:
                if _energy_present(receipt, label, whole_stock="全店" in before) is False:
                    yield "inventory_presence_scope_contradicted"
            else:
                count = receipt.get("known_condition_match_count") if partial else receipt.get("match_count")
                if count == 0 and not receipt.get("unknown_field_count"):
                    yield "inventory_presence_not_verified"



_WHOLE_STOCK = re.compile(r"全店|全库|整个(?:门店|库存)|所有在售")
_REMAINING = re.compile(r"(?:还|仅|只)?(?:剩(?:下|余)?|余下|余有|余)|其余")
_STOCK_PREDICATE = re.compile(r"查到|找到|筛到|看到|匹配到|库存|在售|总共|一共|合计|(?:现有|共有|有)\s*$")


def _stock_count_predicate(clause):
    return bool(_REMAINING.search(clause) or _WHOLE_STOCK.search(clause) or _STOCK_PREDICATE.search(clause))

_PREVIOUS_EXCLUDED = re.compile(
    r"(?:刚才|之前|上次|前面)?(?:的)?(?:那|这)(?:一)?(?:辆|台|款)(?:车)?(?:先)?"
    r"(?:放(?:在)?一边|不(?:看|算|考虑)|排除|不计入|跳过)")


def _customer_quantity_prompt(text, number):
    """A number governed by a customer's purchase/use verb is not a stock total."""
    if number["unit"] != "台":
        return False
    prefix = re.split(r"[，,。；;！？!?]", text[:number["start"]])[-1]
    tail = re.match(r"[^，,。；;！？!?]*[，,。；;！？!?]?", text[number["end"]:])[0]
    # Bind the subject and verb directly to this quantity. Include the
    # first arm of a coordinated choice when checking its second quantity.
    action = re.search(
        r"(?P<subject>您|你|家里|家中)(?:家里|家中)?(?P<modal>是|已经|目前|现在|准备|打算|计划|想要|想|"
        r"希望|需要|更想|可以|不妨|要不要|是否|是不是|通常|平时)*"
        r"(?:购买|添置|选购|选|买|开|用|留|有|要|看)"
        r"(?:[0-9零〇一二两三四五六七八九十百]+(?:台|辆|款)(?:车)?(?:还是|或(?:者)?))?\s*$", prefix)
    if not action:
        return False
    return bool(re.search(r"[？?]|吗|呢|还是|或者", tail) or
                re.search(r"是否|要不要|是不是|可以|不妨|还是|或者", action.group()))


def _stated_inventory_scope(subject, receipt, *, whole_stock=False):
    """Resolve qualifiers against the executed scope before looking at a number."""
    from apps.wechat_ai_customer_service.workflows.catalog_budget import budget_statement
    known = receipt.get("known_condition_scope") or {}
    scope = {} if whole_stock else known
    if re.search(r"年份|上牌|车龄|大修|事故|水泡|火烧|所有条件|全部条件", subject):
        return None
    if whole_stock:
        subject = _WHOLE_STOCK.sub("", subject)
    elif _WHOLE_STOCK.search(subject):
        return None
    if known.get("exclude_ids"):
        # An anaphora can only remove a uniquely confirmed, actually sent car.
        excluded = known["exclude_ids"]
        links = (receipt.get("query") or {}).get("id_sources") or {}
        acknowledged = _PREVIOUS_EXCLUDED.search(subject)
        if acknowledged:
            if len(excluded) != 1 or not any(v.get("kind") == "confirmed_native_reply" for v in links.get(excluded[0], [])):
                return None
            subject = _PREVIOUS_EXCLUDED.sub("", subject)
        if not whole_stock and not acknowledged and not _REMAINING.search(subject):
            return None
    subject = _REMAINING.sub("", subject)
    budget = scope.get("budget") or {}
    if budget.get("state") == "specified" and not _customer_budget_spans(subject, [], budget):
        return None
    if budget.get("state") != "specified" and budget_statement(subject)["present"]:
        return None
    kinds = set()
    for label in re.findall(_ENERGY_LABEL,subject):
        kinds.update(_ENERGY_SCOPE.get(label.removeprefix("新能源") or "新能源",set()))
    for f in scope.get("filters", []):
        vals = f["value"] if f["op"]=="in" else [f["value"]]
        if f["field"] == "energy_type":
            if not kinds or not kinds <= set(vals):
                return None
        elif f["op"] not in ("eq","in") or not all(str(v) in subject for v in vals):
            return None
    if not whole_stock and not kinds and not scope.get("filters") and budget.get("state") != "specified":
        return None
    # Every qualifier must have a source. Removing verified dimensions leaves
    # only stock/count/price wording, never another free-form car attribute.
    remaining = subject
    for start, end in reversed(_customer_budget_spans(subject, [], budget)):
        remaining = remaining[:start] + remaining[end:]
    remaining = re.sub(_ENERGY_LABEL, "", remaining)
    for f in scope.get("filters", []):
        if f["field"] != "energy_type":
            for value in f["value"] if f["op"] == "in" else [f["value"]]:
                remaining = remaining.replace(str(value), "")
    words = (r"好的|门店|店里|这边|我这儿|目前|现在|当前|已经|一共|总共|合计|只有|仅有|库存|在售|现有|现车|车源|二手车|车型|车辆|"
             r"查到|找到|筛到|看到|查了|价格区间|最低价|最高价|价格|售价|报价|价位|预算|以内|以下|"
             r"代步车|可选|可供|条件|这些|这批|我们|我|还|尚|仅|只|的|按|有|共|车|为|是|从|在|内|[，,：:\s]")
    qualifier = re.sub(words, "", remaining)
    if qualifier:
        level = "stock" if whole_stock else "known"
        counts = receipt.get(level + "_energy_counts") or {}
        uses = receipt.get(level + "_common_public_uses") or {}
        present = [k for k in (kinds or counts) if counts.get(k)]
        if (receipt.get(level + "_energy_unknown_rows") != 0 or not present
                or not all(qualifier in uses.get(k, []) for k in present)):
            return None
    return sorted(kinds)


def _scoped_numbers(segment, numbers, entities, receipt, carried):
    from apps.wechat_ai_customer_service.workflows.catalog_query import scoped_aggregate_value, aggregate_ready
    result = {}
    last_scope, last_end = carried, 0
    def record(n, field, expected, scope):
        result[n["start"]] = {"role":field,"expected":expected,"scope_energy":scope,
                              "verified":expected is not None and public_number(n["value"]) == public_number(expected)}
    for index,n in enumerate(numbers):
        start = max((m.end() for m in re.finditer(r"[。；;！？!?]",segment[:n["start"]])),default=0)
        prefix = segment[start:n["start"]]
        # An inventory count requires a stock predicate and its subject. It
        # cannot borrow an equal list size or include unverified conditions.
        if n["unit"] == "台" and _stock_count_predicate(re.split(r"[，,]", prefix)[-1]):
            tail = re.split(r"[，,。；;！？!?]",segment[n["end"]:])[0]
            stated = prefix + tail
            whole_stock = bool(_WHOLE_STOCK.search(stated))
            scope = _stated_inventory_scope(stated,receipt,whole_stock=whole_stock)
            named_count = any(start <= e["start"] < n["start"] for e in entities)
            if named_count:
                last_scope = None
            elif whole_stock:
                expected = scoped_aggregate_value(receipt, "stock_count", scope) if scope is not None else None
                record(n, "stock_count", expected, scope or [])
                last_scope = None
            elif scope is not None:
                record(n,"known_condition_match_count",scoped_aggregate_value(receipt,"known_condition_match_count",scope),scope)
                last_scope, last_end = scope, n["end"]
            elif re.search(_ENERGY_LABEL,stated) and not _scanned_count_scope(segment,n):
                record(n,"known_condition_match_count",None,[])
                last_scope = None
            elif aggregate_ready(receipt) and not _scanned_count_scope(segment,n):
                expected = None if (receipt.get("known_condition_scope") or {}).get("exclude_ids") else receipt.get("match_count")
                record(n,"match_count",expected,[])
        if n["unit"] != "万元":
            continue
        local = re.split(r"[，,]",prefix)[-1]
        next_number = numbers[index+1] if index+1<len(numbers) else None
        connector = segment[n["end"]:next_number["start"]].strip() if next_number else ""
        pair = (next_number is not None and next_number["unit"]=="万元"
                and (connector in ("到","至","-","—","～","~") or
                     (connector in ("和","与") and re.match(r"\s*(?:之间|间)",segment[next_number["end"]:]))))
        if pair and re.search(r"(?:价格|售价|报价|价位|价格区间)(?:在|从|为|是|约|大概|大约|\s)*$",local):
            endpoints = [(n,"min"),(next_number,"max")]
        else:
            bound = (re.search(r"(最低|最高)(?:车价|价|售价|报价)?(?:为|是)?\s*$",local) or
                     re.match(r"\s*(起)(?:售|步)?",segment[n["end"]:]))
            if not bound:
                continue
            endpoints = [(n,"max" if bound[1]=="最高" else "min")]
        end = endpoints[-1][0]
        if any(start <= e["start"] < end["end"] for e in entities):
            continue  # A particular vehicle or a customer budget owns its own amount.
        scope = _stated_inventory_scope(prefix,receipt)
        if scope is None and last_scope is not None and not any(e["start"] >= last_end and e["start"] < n["start"] for e in entities):
            # An immediately following bare price-range clause continues the
            # last verified inventory subject, even across a reply segment.
            continuation = segment[last_end:n["start"]]
            if re.fullmatch(rf"\s*(?:{_ENERGY_LABEL})?(?:现车|车辆|车源|二手车|车)?[，,。\s]*(?:其|这些车的|这批车的)?(?:价格|售价|报价|价位|价格区间)(?:在|从|为|是|约|大概|大约|\s)*", continuation):
                scope = last_scope
        complete = aggregate_ready(receipt)
        explicit_conditions = re.search(_ENERGY_LABEL + r"|年份|车龄|大修|事故|所有条件|全部条件|预算", prefix) or _WHOLE_STOCK.search(prefix)
        for num, bound in endpoints:
            field = "known_price_"+bound if scope is not None or not complete else "price_"+bound
            expected = (scoped_aggregate_value(receipt,field,scope) if scope is not None
                        else receipt.get(field) if complete and not explicit_conditions and not receipt.get("price_unknown_count") else None)
            record(num,field,expected,scope or [])
    if any(e["start"]>=last_end for e in entities):
        last_scope = None
    return result, last_scope


_PAYMENT_ROLE = re.compile(r"首付|月供|定金|订金")
_PAYMENT_OFFER = re.compile(r"获批|批准|包过|保证|审批|规则|政策|最低|只需|只要|即可|就能|可以办|可办|可贷|能做到|没问题|给您办|门店规定")


def _payment_value(n, role):
    value = public_number(n["value"]) if n["value"] is not None else None
    if n["unit"] == "万元":
        return value
    # A bare monthly-payment amount is denominated in yuan, not ten-thousand yuan.
    if n["unit"] is None and role == "月供":
        raw = public_number(n["text"])
        return raw / 10000 if raw is not None else None
    return None


def _latest_customer_payments(sources):
    from apps.wechat_ai_customer_service.workflows.catalog_budget import amount_role
    payments = {}
    for source in sources:
        if source["sender"] not in ("customer","client"):
            continue
        for n in factual_number_spans(source["content"]):
            role = amount_role(source["content"], n["start"])
            if role in {"首付","月供","定金"}:
                value = _payment_value(n,role)
                if value is not None:
                    payments[role] = (value,source["id"])
    return payments


def _payment_clause(text, start, end):
    before = re.split(r"[，,。；;！？!?、]",text[:start])[-1]
    after = re.split(r"[，,。；;！？!?、]",text[end:])[0]
    return before, after


def _neutral_finance_span(text):
    """A question or statement of missing information supplies no seller terms."""
    if not _PAYMENT_ROLE.search(text) or _PAYMENT_OFFER.search(text):
        return False
    if re.search(r"利率|免息|担保|审核|金融方案|资方|合同|手续费",text):
        return False
    return bool(re.search(r"(?:待|需(?:要)?|还得|要)(?:再|先)?(?:核实|确认)|不等于(?:车辆)?总价|"
                          r"(?:硬性要求|期望|预算).{0,5}[吗么？?]", text))


def validate_reply_public_facts(plan: dict, evidence: dict, *, formal_validation: dict | None = None) -> dict:
    """Inspect actual text, before/after semantic review; never trust claim IDs.

    This verifies recognizable entities/numeric facts. Unstructured claims
    remain subject to the existing semantic reviewer, not string-based proof.
    """
    from apps.wechat_ai_customer_service.workflows.catalog_query import aggregate_ready, message_sources, receipt_state, receipt_view, receipt_products, aggregate_fact_ready
    from apps.wechat_ai_customer_service.workflows.catalog_budget import amount_role
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import formal_knowledge_source_table
    receipt = (evidence.get("catalog_protocol") or {}).get("receipt") or {}
    products = receipt_products(receipt) if receipt else ((evidence.get("knowledge") or {}).get("product_master") or {}).get("items") or []
    receipt = receipt_view(receipt)
    by_id = {str(p["id"]): p for p in products}
    errors, facts, occurrences, actual_ids = [], [], [], []
    priced_ids = set()
    customer_statements = []
    display_counts = _display_counts(plan.get("reply_segments") or [], products, receipt)
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

    def customer_amount(segment, number, budget_spans):
        role = amount_role(segment, number["start"])
        budget = (receipt.get("query") or {}).get("budget") or {}
        if any(start <= number["start"] and number["end"] <= end for start, end in budget_spans):
            for source in message_sources(evidence):
                if source["sender"] in ("customer", "client") and source["id"] in budget.get("source_ids", []):
                    add("customer_requirement", number["text"], "current_conversation", source["id"])
                    return True
        if role == "预算":
            return False  # The whole relation must match, not just an endpoint.
        if role not in {"首付","月供","定金"}:
            return False
        before,after = _payment_clause(segment, number["start"],number["end"])
        if (_PAYMENT_OFFER.search(before+after) or
                re.search(r"实际|给您|我们|我方|门店|销售|公司|我能|本车|办理",before)):
            return False
        payment = customer_payments.get(role)
        if payment and _payment_value(number,role) == payment[0]:
            add("customer_requirement", number["text"], "current_conversation", payment[1])
            return True
        return False

    customer_payments = _latest_customer_payments(message_sources(evidence))
    target = receipt.get("reply_target") or {}
    target_ids = set(target.get("vehicle_ids", []))
    segment_ids = []
    inventory_scope = None
    prior_entities = [{"start":0, "end":0, "ids":sorted(target_ids)}] if target_ids else []
    for segment_index, segment in enumerate(plan.get("reply_segments") or []):
        entities = _reply_references(segment, _reply_entities(segment, products), prior_entities)
        # A shared brand adjective after an explicitly named target describes
        # that target; a different unambiguous vehicle name remains visible.
        if len(target_ids) == 1:
            for entity in entities:
                if len(entity["ids"]) > 1 and target_ids.issubset(entity["ids"]):
                    entity["ids"] = sorted(target_ids)
        segment_ids.append(list(dict.fromkeys(e["ids"][0] for e in entities if len(e["ids"]) == 1)))
        numbers = factual_number_spans(segment, excluded=[(e["start"], e["end"]) for e in entities])
        # A customer preference question is not an assertion about a car's year.
        numbers = [n for n in numbers if n["unit"] != "年" or
                   _vehicle_year_assertion(segment, n, entities) or policy_number(n, segment_index)]
        inventory_numbers = []
        for n in numbers:
            if _customer_quantity_prompt(segment, n):
                occurrences.append({"segment_index":segment_index, **n, "role":"customer_quantity_prompt",
                                    "verified":True})
            else:
                inventory_numbers.append(n)
        numbers = inventory_numbers
        owners = _number_entity_owners(segment, entities, numbers)
        budget_spans = _customer_budget_spans(segment, entities, (receipt.get("query") or {}).get("budget") or {})
        if entities:
            boundary = segment.rfind("。", 0, entities[-1]["start"])
            prior_entities = [e for e in entities if e["start"] > boundary]
        customer_positions = set()
        for n in numbers:
            clause_start = max((m.end() for m in re.finditer(r"[，,。；;！？!?]", segment[:n["start"]])), default=0)
            if amount_role(segment, n["start"]) not in (None, "车价") and not any(clause_start <= e["start"] < n["start"] for e in entities):
                customer_positions.add(n["start"])
        customer_numbers = {n["start"] for n in numbers if customer_amount(segment, n, budget_spans)}
        display_positions = set()
        scoped, inventory_scope = _scoped_numbers(segment,numbers,entities,receipt,inventory_scope)
        for number in numbers:
            item = scoped.get(number["start"])
            if not item or number["start"] in customer_numbers:
                continue
            display_positions.add(number["start"]); owners.pop(number["start"],None)
            occurrences.append({"segment_index":segment_index,**number,**item})
            if not item["verified"]:
                errors.append("inventory_scoped_metric_mismatch" if item["expected"] is not None else "unverified_reply_numeric_fact")
                continue
            kind = {"known_condition_match_count":"catalog_known_condition_count","match_count":"catalog_match_count","stock_count":"catalog_stock_count",
                    "known_price_min":"catalog_known_price_min","known_price_max":"catalog_known_price_max",
                    "price_min":"catalog_price_min","price_max":"catalog_price_max"}[item["role"]]
            fact = {"fact_type":kind,"value":item["expected"],"source_level":"catalog_query","source_id":receipt["query_id"]}
            if item["scope_energy"]: fact["scope_energy"] = item["scope_energy"]
            if fact not in facts: facts.append(fact)
        for number in numbers:
            if number["unit"] != "台" or number["start"] in display_positions:
                continue
            field = ("known_condition_match_count" if _known_count_scope(segment, number, receipt) else
                     "scanned_count" if _scanned_count_scope(segment, number) else None)
            if field is None:
                continue
            valid = aggregate_fact_ready(receipt, field) and public_number(number["value"]) == receipt.get(field)
            display_positions.add(number["start"])
            owners.pop(number["start"], None)
            occurrences.append({"segment_index": segment_index, **number, "verified": valid,
                                "role": field, "expected": receipt.get(field)})
            if valid:
                add("catalog_scanned_count" if field == "scanned_count" else "catalog_known_condition_count", receipt[field], "catalog_query", receipt["query_id"])
            else:
                errors.append("reply_scanned_count_mismatch" if field == "scanned_count" else "reply_known_condition_count_mismatch")
        for number in numbers:
            display = display_counts.get((segment_index, number["start"]))
            if display is not None and number["start"] not in display_positions:
                display_positions.add(number["start"])
                owners.pop(number["start"], None)
                occurrences.append({"segment_index": segment_index, **number, **display})
                if not display["verified"]:
                    errors.append("reply_display_count_mismatch")
        for clause in re.finditer(r"[^，,。；;！？!?]+", segment):
            clause_numbers = [n for n in numbers if clause.start() <= n["start"] < clause.end()]
            only_customer_numbers = all(n["start"] in customer_numbers for n in clause_numbers)
            sole_amount = clause_numbers and only_customer_numbers and not segment[clause_numbers[-1]["end"]:clause.end()].strip()
            if (sole_amount or (only_customer_numbers and _neutral_finance_span(clause.group()))) and not _PAYMENT_OFFER.search(clause.group()):
                customer_statements.append({"segment_index": segment_index, "start": clause.start(), "end": clause.end()})
        for number in numbers:
            if number["start"] in customer_numbers:
                occurrences.append({"segment_index": segment_index, **number, "verified": True, "role": "customer_requirement"})
        for entity_index, entity in enumerate(entities):
            end = entities[entity_index + 1]["start"] if entity_index + 1 < len(entities) else len(segment)
            section = segment[entity["start"]:end]
            if len(entity["ids"]) != 1:
                if any(entity_index in owners.get(n["start"], []) for n in numbers):
                    errors.append("reply_vehicle_reference_ambiguous")
                    for n in numbers:
                        if entity_index in owners.get(n['start'], []):
                            occurrences.append({'segment_index':segment_index, **n, 'verified':False,
                                'role':'vehicle_reference_unconfirmed', 'candidate_product_ids':entity['ids']})
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
            for number in (n for n in numbers if entity_index in owners.get(n["start"], [])):
                if number["start"] in customer_numbers or number["start"] in display_positions:
                    continue
                # Once a named vehicle introduces its own amount, earlier
                # budget wording is not the role of that vehicle's price.
                local_role = amount_role(segment, number["start"])
                if entity["end"] <= number["start"] and local_role == "预算":
                    local_role = amount_role(segment[entity["end"]:], number["start"] - entity["end"])
                if (number["start"] in customer_positions or
                        (number["unit"] == "万元" and local_role not in (None, "车价"))):
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
                occurrences.append({"segment_index": segment_index, **number, "product_id": product_id, "verified": valid,
                                    "role": field, "expected": expected, "source_id": product_id,
                                    "rejection_reason": None if valid else "vehicle_fact_value_mismatch"})
                if not valid:
                    errors.append("reply_numeric_fact_mismatch" if value is not None else "numeric_expression_unverifiable")
                else:
                    add({"price": "price", "stock": "stock", "mileage_km": "mileage", "first_registration": "year"}.get(field, "configuration"),
                        expected, "product_master", product_id)
                    if field == "price":
                        priced_ids.add(product_id)
        # Unassigned facts may be a policy or an aggregate, but a number in an
        # ambiguous vehicle assertion cannot borrow the aggregate price range.
        for number in (n for n in numbers if n["start"] not in owners):
            if number["start"] in customer_numbers or number["start"] in display_positions:
                continue
            policy = policy_number(number, segment_index)
            valid = policy
            clause_start = max((m.end() for m in re.finditer(r"[，,。；;！？!?]", segment[:number["start"]])), default=0)
            local_entities = [e for e in entities if clause_start <= e["start"] < number["end"]]
            occurrences.append({"segment_index": segment_index, **number, "verified": valid and (not policy or formal_validation is not None),
                                "pending_policy_review": policy and formal_validation is None,
                                "role_candidates": [amount_role(segment, number["start"]), "catalog_aggregate"],
                                "query_id": receipt.get("query_id"),
                                "rejection_reason": None if valid else "no_matching_authority_for_role_and_scope"})
            local_text = re.split(r"[。！？!?；;]", segment[clause_start:])[0]
            role = amount_role(segment, number["start"])
            policy_statement = any(si == segment_index and lo <= number["start"] < hi
                                   for si, lo, hi in (formal_validation or {}).get("policy_statements", []))
            # Counted vehicles need an owner even when the wording uses an
            # unfamiliar stock predicate. A question about a customer's choice
            # has no asserted inventory total.
            asserted_quantity = (local_entities or role or policy_statement or number["unit"] in {"台", "年"} or
                                 re.search(r"共|合计|符合|条件|查到|匹配|在售|库存|最低|最高|价[位格]|预算|区间|报价|服务费|定金|首付|月供", local_text))
            if not valid and asserted_quantity:
                errors.append("unverified_reply_numeric_fact")
        # Negative inventory statements still assert a result, even with no
        # digits. Unknown conditions or nonzero results cannot authorize them.
        errors.extend(_inventory_assertion_errors(segment, receipt))
        errors.extend(_unsupported_condition_claims(segment, entities))
        for start, end in _inventory_absences(segment):
            category = re.search(_ENERGY_LABEL, segment[start:end])
            whole_stock = "全店" in re.split(r"[，,。；;！？!?]", segment[:start])[-1]
            if category:
                verified_absence = _energy_present(receipt, category.group(), whole_stock=whole_stock) is False
            elif whole_stock:
                verified_absence = receipt.get("scan_complete") and receipt.get("scanned_count") == 0
            else:
                verified_absence = aggregate_ready(receipt) and receipt.get("match_count") == 0
            if not verified_absence:
                errors.append("inventory_absence_not_verified")
        if (not entities and aggregate_ready(receipt) and receipt.get("match_count") == 0
                and re.search(r"(?:没有|暂无|未找到|没找到)(?:符合[^。！？]*|合适的?|匹配的?|在售的?)?(?:车|车源)", segment)):
            add("catalog_match_count", 0, "catalog_query", receipt["query_id"])
    preparation = (evidence.get("catalog_protocol") or {}).get("preparation") or {}
    context_ids = {p["id"] for p in receipt.get("known_condition_facts", [])}
    scoped_values = any(f["fact_type"].startswith("catalog_known_") for f in facts)
    body = " ".join(plan.get("reply_segments") or [])
    pending = " ".join(receipt.get("unsupported_requirements", []))
    if receipt_state(receipt) == "clarification_required" and (context_ids.intersection(actual_ids) or scoped_values):
        # A model-selected mode is not proof of a claim. Judge the actual
        # scope disclosed with the examples and keep the unresolved condition.
        if re.search(r"年份|上牌|车龄",pending) and not re.search(
                r"(?:年份|上牌|车龄).{0,18}(?:待|需|未|没|查不到|不确定|再核实|不能确认|暂时)|(?:待核实|未核实).{0,8}(?:年份|车龄)", body):
            errors.append("catalog_clarification_required")
        elif not re.search(r"年份|上牌|车龄",pending) and plan.get("answer_mode") != "ask_clarifying_question" and not re.search(
                r"(?:首付|月供|贷款|方案).{0,30}(?:待核实|需核实|需确认|审核|核实后)",body):
            errors.append("catalog_clarification_required")
    recommendations = receipt.get("recommendations") or []
    ordered_ids = [str(p["id"]) for p in recommendations]
    if target.get("kind") in {"named_reference", "reference_and_list", "unresolved_reference"}:
        if target.get("kind") != "reference_and_list" and set(actual_ids) - target_ids:
            errors.append("reply_wrong_referenced_vehicle")
        if target_ids and not target_ids.issubset(actual_ids):
            errors.append("answer_referenced_vehicle_omitted")
        if target.get("asks_price") and any(by_id[i].get("price") is not None and i not in priced_ids for i in target_ids if i in by_id):
            errors.append("answer_referenced_price_omitted")
    if target.get("kind") == "reference_and_list" or (target.get("kind") not in {"named_reference", "unresolved_reference"}
            and receipt.get("query", {}).get("operation") == "search"):
        options = recommendations or receipt.get("known_condition_facts", [])
        ordered_ids = [str(p["id"]) for p in options if p["id"] not in target_ids]
        actual_recommendations = [i for i in actual_ids if i in ordered_ids]
        if actual_recommendations != ordered_ids[:len(actual_recommendations)]:
            errors.append("reply_recommendation_order_mismatch")
        if (preparation.get("requires_recommendation") or target.get("kind") == "reference_and_list") and receipt_state(receipt) in {"complete", "partial"} and ordered_ids:
            if not actual_recommendations:
                errors.append("answer_incomplete")
            if any(by_id[i].get("price") is not None and i not in priced_ids for i in actual_recommendations):
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
    result = {"ok": not errors, "errors": list(dict.fromkeys(errors)),
            "conflict_evidence": ({"query_id":receipt.get("query_id"), "target":target,
                                   "actual_vehicle_ids":actual_ids, "priced_ids":sorted(priced_ids),
                                   "ordered_ids":ordered_ids, "segments":list(plan.get("reply_segments") or [])}
                                  if errors else None), "facts_claimed": facts,
            "product_ids": actual_ids, "segment_vehicle_ids":segment_ids, "occurrences": occurrences, "customer_statements": customer_statements,
            "reply_sha256": hashlib.sha256(json.dumps(plan.get("reply_segments") or [], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}

    from apps.wechat_ai_customer_service.workflows.fact_review_routing import classify_fact_findings
    result.update(classify_fact_findings(plan, products, receipt, result))
    # Keep extraction evidence, but never derive a guessed numeric ownership as
    # a mechanically verified fact, even if its value happened to match.
    numeric_kinds = {'price':'price', 'stock':'stock', 'mileage_km':'mileage',
                     'first_registration':'year', 'displacement':'configuration'}
    uncertain = {(o.get('product_id'), numeric_kinds.get(o.get('role')))
                 for o in occurrences if o.get('disposition') == 'semantic_pending' and o.get('product_id')}
    certain = {(o.get('product_id'), numeric_kinds.get(o.get('role')))
               for o in occurrences if o.get('disposition') == 'verified' and o.get('product_id')}
    result['mechanical_facts_claimed'] = [f for f in facts if (f['source_id'], f['fact_type']) not in uncertain - certain]
    return result


def validate_reviewed_reply_facts(plan: dict, evidence: dict, *, refs: dict, mapping: dict, semantic_approved: bool = False) -> dict:
    """One actual-text check used by semantic clearance and final Guard.

    Policy selections can support only their actual monetary roles. Vehicle
    prices, customer amounts and query aggregates retain independent authority.
    """
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import validate_policy_refs
    preliminary = validate_reply_public_facts(plan, evidence)
    formal = validate_policy_refs(refs, segments=plan.get("reply_segments") or [], mapping=mapping,
                                  customer_statements=preliminary["customer_statements"])
    validated = validate_reply_public_facts(plan, evidence, formal_validation=formal)
    # A selected policy cannot lend an absent numeric term to the draft.
    # Preserve this independent source check when vehicle wording is cleared.
    policy_conflicts = [o for o in validated['occurrences']
        if not o.get('verified') and not o.get('product_id') and any(
            si == o['segment_index'] and lo <= o['start'] < hi
            for si, lo, hi in formal.get('policy_statements', []))]
    if policy_conflicts:
        validated['hard_errors'].append('policy_numeric_fact_not_supported')
    validated["facts_claimed"].extend(formal["facts_claimed"])
    validated['mechanical_facts_claimed'].extend(formal['facts_claimed'])
    validated["formal_source_ids"] = formal["source_ids"]
    validated["errors"] = list(dict.fromkeys([*formal["errors"], *(validated["hard_errors"] if semantic_approved else validated["errors"])]))
    validated["semantic_approved"] = semantic_approved
    validated["ok"] = not validated["errors"]
    return validated


def derive_reviewed_fact_fields(plan: dict, validated: dict) -> None:
    """Called only by the existing semantic-clearance owner after actual pass."""
    if not validated.get("ok"):
        raise ValueError("cannot_derive_unverified_facts")
    plan["facts_claimed"] = deepcopy(validated['mechanical_facts_claimed'])
    evidence = plan.setdefault("evidence_used", {})
    evidence["product_ids"] = list(validated["product_ids"])
    evidence["formal_knowledge_ids"] = list(validated.get("formal_source_ids") or [])
