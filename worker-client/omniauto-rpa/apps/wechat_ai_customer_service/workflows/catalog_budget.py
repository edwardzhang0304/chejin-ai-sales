"""Shared shopping-budget interpretation, in 万元, before catalog truncation.

Only explicit monetary units authorize a numeric bound. Mileage, model numbers
and partial amounts never become prices. Current-turn constraints take priority
at the caller; this parser does not merge old and new budgets by taking a max.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
import unicodedata


@dataclass(frozen=True)
class CatalogBudget:
    lower: float
    upper: float | None
    target: float
    kind: str

    def contains(self, price: float) -> bool:
        return math.isfinite(price) and price > 0 and price >= self.lower and (self.upper is None or price <= self.upper)

    def as_dict(self) -> dict:
        return {**asdict(self), "unit": "万元"}


def catalog_budget_around_upper(value: float) -> float:
    # Preserve the existing shopping-band policy; explicit caps/ranges do not
    # use this widening rule.
    if value <= 0:
        return value
    if value <= 6:
        return value + 1.0
    if value <= 15:
        return value + 1.5
    return value * 1.12


_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)"
_UNIT = r"(?:万(?:元|块钱|块)?|w|元|块钱|块)"
_NOT_MILEAGE = r"(?!\s*(?:公里|千米|km))"
_UNLIMITED = r"(?:不限|不限制|不设|不考虑)预算|预算(?:不限|不限制|不设上限)|没有预算限制"
_CAP_PREFIX = r"不超过|别超过|最多|至多|上限"
_CAP_SUFFIX = r"以内|以下|内|封顶"
_FLOOR_PREFIX = r"至少|不低于"
_FLOOR_SUFFIX = r"以上|起"
_AROUND_SUFFIX = r"左右|上下|附近|出头"
_COMPARISON = re.compile(r"不(?:少于|多于|高于|低于|超过|足|满|到)|[大小高低多少]于|超过|不到|以内|以下|以上|左右|上下|附近|出头|封顶|最多|至多|至少|上限|[<>≤≥]")


def monetary_number(token: str) -> float | None:
    try:
        value = float(token)
        return value if math.isfinite(value) else None
    except ValueError:
        pass
    integer, _, decimal = token.partition("点")
    total, current = 0, 0
    if len(integer) > 1 and not any(unit in integer for unit in "十百"):
        return None  # 三五 is a range, not 35 or 5.
    for char in integer:
        if char in _DIGITS:
            current = _DIGITS[char]
        elif char in "十百":
            total += (current or 1) * (10 if char == "十" else 100)
            current = 0
        else:
            return None
    if any(char not in _DIGITS for char in decimal):
        return None
    return float(f"{total + current}." + ("".join(str(_DIGITS[c]) for c in decimal) or "0"))


def _wan(token: str, unit: str) -> float | None:
    value = monetary_number(token)
    return None if value is None else value / (1 if unit.startswith(("万", "w")) else 10000)


def amount_role(text: str, start: int) -> str | None:
    """Explicit monetary role in the current clause, shared with reply checks."""
    prefix = re.split(r"[，,。；;！？!?]", text[:start])[-1]
    roles = list(re.finditer(r"首付|月供|定金|订金|手续费|服务费|利息|利率|总预算|预算|车价|总价|售价|报价|价格", prefix))
    if not roles:
        return None
    role = roles[-1].group()
    if role in {"车价", "总价", "售价", "报价", "价格"}:
        return "车价"
    return "预算" if role == "总预算" else "定金" if role == "订金" else role


def _is_non_budget_amount(text: str, start: int) -> bool:
    prefix = re.split(r"[，,。；;！？!?]", text[:start])[-1]
    if amount_role(text, start) in {"首付", "月供", "定金", "手续费", "服务费", "利息", "利率"}:
        return True
    return bool(re.search(r"(?:不要|不看|不选|不考虑|不是|排除)(?:价格|价位|大概|那些|这种|的|在|是|约|为|\s)*$", prefix))


def _budget_directive(text: str, *, recognized: list | None = None,
                      recognized_relations: list | None = None) -> tuple[bool, CatalogBudget | None]:
    raw = unicodedata.normalize("NFKC", str(text or "")).lower()
    # Mask mileage first so a range regex cannot partially match its suffix.
    raw = re.sub(rf"{_NUMBER}\s*(?:万|w)\s*(?:公里|千米|km)", " ", raw)
    # Explicit release is an event, not a whole-sentence shortcut: a newer
    # positive cap can supersede "原来预算不限" within the same message.
    matches: list[tuple[int, CatalogBudget | None]] = [
        (m.start(), None) for m in re.finditer(
            _UNLIMITED, raw)
        if not re.search(r"(?:不是|不要|并非)\s*$", raw[:m.start()])
    ]
    if recognized is not None:
        recognized.extend(m.span() for m in re.finditer(
            _UNLIMITED, raw)
            if not re.search(r"(?:不是|不要|并非)\s*$", raw[:m.start()]))
    occupied: list[tuple[int, int]] = []
    def record(match, low, high):
        occupied.append(match.span())
        if _is_non_budget_amount(raw, match.start()) or low is None or high is None or min(low, high) <= 0:
            return
        low, high = sorted((low, high))
        matches.append((match.start(), CatalogBudget(low, high, (low + high) / 2, "range")))
        if recognized is not None:
            recognized.append(match.span())
            # Preserve the established range rule: a trailing approximate
            # qualifier does not widen explicit endpoints into an around band.
            suffix = re.match(r"\s*(?:" + _AROUND_SUFFIX + ")", raw[match.end():])
            if suffix and recognized_relations is not None:
                recognized_relations.append((match.end(), match.end() + suffix.end()))
    for m in re.finditer(rf"({_NUMBER})\s*({_UNIT})?\s*(?:到|至|[-~—～－])\s*({_NUMBER})\s*({_UNIT}){_NOT_MILEAGE}", raw):
        record(m, _wan(m[1], m[2] or m[4]), _wan(m[3], m[4]))
    for m in re.finditer(r"(?<![零〇一二两三四五六七八九十百])([一二两三四五六七八九])([一二两三四五六七八九])\s*万(?:元|块钱|块)?", raw):
        record(m, monetary_number(m[1]), monetary_number(m[2]))
    for m in re.finditer(rf"({_NUMBER})\s*({_UNIT}){_NOT_MILEAGE}", raw):
        if any(start <= m.start() < end for start, end in occupied):
            continue
        value = _wan(m[1], m[2])
        if value is None or value <= 0 or _is_non_budget_amount(raw, m.start()):
            continue
        suffix = raw[m.end():m.end()+6].lstrip()
        prefix = raw[max(0, m.start()-8):m.start()].rstrip()
        prefix_match = suffix_match = None
        if re.match(_CAP_SUFFIX, suffix) or re.search(_CAP_PREFIX, prefix):
            budget = CatalogBudget(0, value, value, "cap")
            prefix_match, suffix_match = re.search(_CAP_PREFIX, prefix), re.match(_CAP_SUFFIX, suffix)
        elif re.match(_FLOOR_SUFFIX, suffix) or re.search(_FLOOR_PREFIX, prefix):
            budget = CatalogBudget(value, None, value, "floor")
            prefix_match, suffix_match = re.search(_FLOOR_PREFIX, prefix), re.match(_FLOOR_SUFFIX, suffix)
        elif re.match(_AROUND_SUFFIX, suffix):
            upper = catalog_budget_around_upper(value)
            budget = CatalogBudget(max(0, 2*value-upper), upper, value, "around")
            suffix_match = re.match(_AROUND_SUFFIX, suffix)
        else:
            budget = CatalogBudget(0, value, value, "cap")
        matches.append((m.start(), budget))
        if recognized is not None:
            recognized.append(m.span())
        if recognized_relations is not None:
            if prefix_match:
                offset = max(0, m.start()-8)
                recognized_relations.append((offset + prefix_match.start(), offset + prefix_match.end()))
            if suffix_match:
                offset = m.end() + len(raw[m.end():m.end()+6]) - len(suffix)
                recognized_relations.append((offset + suffix_match.start(), offset + suffix_match.end()))
    # In "原来10万，现在4万" the latest explicit budget wins.
    return (True, max(matches, key=lambda item: item[0])[1]) if matches else (False, None)


def budget_statement(text: str) -> dict:
    """One parser also supplies exact consumed spans to the preparation router.

    A parsed number is not proof that the whole request is understood. Unknown
    remainder is deliberately retained for the shared semantic fallback.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    spans, relations = [], []
    present, budget = _budget_directive(normalized, recognized=spans, recognized_relations=relations)
    # Mileage masking in the legacy parser changes offsets. Such utterances
    # require semantic understanding; never mark them completely consumed.
    if re.search(r"公里|千米|km", normalized):
        spans = []
        relations = []
    remainder = list(normalized)
    for start, end in [*spans, *relations]:
        remainder[start:end] = " " * (end - start)
    return {"present": present, "budget": budget, "normalized_text": normalized,
            "remainder": "".join(remainder), "recognized_spans": spans, "relation_spans": relations,
            "unconsumed_comparisons": [{"start": m.start(), "end": m.end(), "text": m.group()}
                                       for m in _COMPARISON.finditer("".join(remainder))]}


def explicit_total_budget(text: str) -> CatalogBudget | None:
    """A positive, explicitly named total at the start of a complex request.

    This supplies a conflict constraint, not permission to execute the remaining
    conditions. Corrections, negation and historical phrasing stay semantic.
    """
    parsed = budget_statement(text)
    spans = parsed["recognized_spans"]
    raw = parsed["normalized_text"]
    if len(spans) != 1 or re.search(r"不是|并非|不要|不看|不选|以前|之前|原来|过去|改成|改为|还是|或者|可能", raw):
        return None
    prefix = raw[:spans[0][0]].strip()
    if not re.fullmatch(r"(?:我(?:的)?|您(?:的)?|你(?:的)?)?(?:总预算|预算|总价|车价)(?:是|为|控制在|最多|不超过|别超过)?", prefix):
        return None
    return parsed["budget"]


def parse_catalog_budget(text: str) -> CatalogBudget | None:
    return _budget_directive(text)[1]


def resolve_catalog_budget(text: str, *context_texts: str) -> tuple[CatalogBudget | None, str]:
    """One precedence/release rule for selection and reply quality checks."""
    for index, value in enumerate((text, *context_texts)):
        present, budget = _budget_directive(value)
        if present:
            return budget, "current_message" if index == 0 else "conversation_need"
    return None, ""


def extract_catalog_budget_upper(text: str) -> float | None:
    budget = parse_catalog_budget(text)
    return budget.upper if budget else None


def extract_catalog_budget_target(text: str) -> float | None:
    budget = parse_catalog_budget(text)
    return budget.target if budget else None
