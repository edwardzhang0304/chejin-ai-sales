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


def _is_non_budget_amount(text: str, start: int) -> bool:
    prefix = re.split(r"[，,。；;！？!?]", text[:start])[-1]
    roles = list(re.finditer(r"首付|月供|定金|订金|总预算|预算|车价|总价", prefix))
    if roles and roles[-1].group() in {"首付", "月供", "定金", "订金"}:
        return True
    return bool(re.search(r"(?:不要|不看|不选|不考虑|不是|排除)(?:价格|价位|大概|那些|这种|的|在|是|约|为|\s)*$", prefix))


def _budget_directive(text: str) -> tuple[bool, CatalogBudget | None]:
    raw = unicodedata.normalize("NFKC", str(text or "")).lower()
    # Mask mileage first so a range regex cannot partially match its suffix.
    raw = re.sub(rf"{_NUMBER}\s*(?:万|w)\s*(?:公里|千米|km)", " ", raw)
    # Explicit release is an event, not a whole-sentence shortcut: a newer
    # positive cap can supersede "原来预算不限" within the same message.
    matches: list[tuple[int, CatalogBudget | None]] = [
        (m.start(), None) for m in re.finditer(
            r"(?:不限|不限制|不设|不考虑)预算|预算(?:不限|不限制|不设上限)", raw)
        if not re.search(r"(?:不是|不要|并非)\s*$", raw[:m.start()])
    ]
    occupied: list[tuple[int, int]] = []
    def record(match, low, high):
        occupied.append(match.span())
        if _is_non_budget_amount(raw, match.start()) or low is None or high is None or min(low, high) <= 0:
            return
        low, high = sorted((low, high))
        matches.append((match.start(), CatalogBudget(low, high, (low + high) / 2, "range")))
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
        if re.match(r"以内|以下|内|封顶", suffix) or re.search(r"不超过|最多|至多|上限", prefix):
            budget = CatalogBudget(0, value, value, "cap")
        elif re.match(r"以上|起", suffix) or re.search(r"至少|不低于", prefix):
            budget = CatalogBudget(value, None, value, "floor")
        elif re.match(r"左右|上下|附近|出头", suffix):
            upper = catalog_budget_around_upper(value)
            budget = CatalogBudget(max(0, 2*value-upper), upper, value, "around")
        else:
            budget = CatalogBudget(0, value, value, "cap")
        matches.append((m.start(), budget))
    # In "原来10万，现在4万" the latest explicit budget wins.
    return (True, max(matches, key=lambda item: item[0])[1]) if matches else (False, None)


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
