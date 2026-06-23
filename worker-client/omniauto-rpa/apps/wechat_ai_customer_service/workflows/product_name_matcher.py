"""Robust product-name matching helpers shared by customer-service runtimes."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from apps.wechat_ai_customer_service.platform_understanding_rules import (
    platform_understanding_cache_token,
    semantic_equivalents as visible_semantic_equivalents,
)

# Project defaults for commonly seen used-car transliteration variations.
# User-managed semantic equivalents from platform rules are merged in at runtime.
DEFAULT_PRODUCT_NAME_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "赛那": ("塞纳", "塞那", "赛纳"),
}

# Minimal confusable folds to support common model-name variants without
# over-expanding and causing false positives.
CONFUSABLE_CHAR_GROUPS: tuple[tuple[str, str], ...] = (
    ("赛塞", "赛"),
    ("纳那哪", "那"),
)

_EQUIVALENT_MAP_CACHE: dict[str, Any] = {
    "key": None,
    "value": None,
}


def normalize_match_text(text: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower().strip()
    replacements = {
        "\r\n": "\n",
        "\r": "\n",
        "：": ":",
        "，": ",",
        "。": ".",
        "；": ";",
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
    return normalized.strip()


def compact_match_text(text: Any) -> str:
    normalized = normalize_match_text(text)
    kept: list[str] = []
    for ch in normalized:
        category = unicodedata.category(ch)
        if category.startswith("L") or category.startswith("N"):
            kept.append(ch)
    return "".join(kept)


def fold_confusable_text(text: Any) -> str:
    normalized = normalize_match_text(text)
    folded = [confusable_char_fold_map().get(ch, ch) for ch in normalized]
    return "".join(folded)


def confusable_char_fold_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group, canonical in CONFUSABLE_CHAR_GROUPS:
        root = normalize_match_text(canonical)
        if not root:
            continue
        canonical_char = root[0]
        for char in group:
            mapping[char] = canonical_char
    return mapping


def semantic_equivalent_map() -> dict[str, set[str]]:
    cache_key = platform_understanding_cache_token()
    if _EQUIVALENT_MAP_CACHE.get("key") == cache_key and isinstance(_EQUIVALENT_MAP_CACHE.get("value"), dict):
        return _EQUIVALENT_MAP_CACHE["value"]

    merged: dict[str, set[str]] = {}
    for raw_map in (visible_semantic_equivalents(), DEFAULT_PRODUCT_NAME_EQUIVALENTS):
        for raw_key, raw_values in raw_map.items():
            key = compact_match_text(raw_key)
            if not key:
                continue
            bucket = merged.setdefault(key, set())
            for raw_value in raw_values:
                value = compact_match_text(raw_value)
                if value and value != key:
                    bucket.add(value)

    for key, values in list(merged.items()):
        for value in values:
            merged.setdefault(value, set()).add(key)

    _EQUIVALENT_MAP_CACHE["key"] = cache_key
    _EQUIVALENT_MAP_CACHE["value"] = merged
    return merged


def semantic_group(token: str) -> set[str]:
    compact = compact_match_text(token)
    if not compact:
        return set()
    equivalents = semantic_equivalent_map()
    group = {compact}
    group.update(equivalents.get(compact, set()))
    for key, values in equivalents.items():
        if compact in values:
            group.add(key)
            group.update(values)
    return {item for item in group if item}


def build_query_variants(text: str) -> set[str]:
    normalized = normalize_match_text(text)
    compact = compact_match_text(normalized)
    variants: set[str] = {
        normalized,
        compact,
        fold_confusable_text(normalized),
        fold_confusable_text(compact),
    }

    semantic_terms = semantic_equivalent_map()
    for term, equivalents in semantic_terms.items():
        group = {term, *equivalents}
        if any(token and token in compact for token in group):
            for src in group:
                for dst in group:
                    if not src or not dst or src == dst:
                        continue
                    if src in compact:
                        variants.add(compact.replace(src, dst))

    expanded: set[str] = set()
    for item in variants:
        if not item:
            continue
        compact_item = compact_match_text(item)
        expanded.add(item)
        if compact_item:
            expanded.add(compact_item)
        folded = fold_confusable_text(item)
        if folded:
            expanded.add(folded)
        if compact_item:
            folded_compact = fold_confusable_text(compact_item)
            if folded_compact:
                expanded.add(folded_compact)
    return {item for item in expanded if item}


def build_alias_variants(alias: str) -> set[str]:
    normalized = normalize_match_text(alias)
    compact = compact_match_text(normalized)
    if not compact:
        return set()
    variants = {compact, fold_confusable_text(compact)}
    for equivalent in semantic_group(compact):
        variants.add(equivalent)
        variants.add(fold_confusable_text(equivalent))
    return {item for item in variants if item}


def alias_matches_query(alias: str, query_text: str) -> bool:
    alias_variants = build_alias_variants(alias)
    if not alias_variants:
        return False
    query_variants = build_query_variants(query_text)
    if not query_variants:
        return False
    for alias_variant in alias_variants:
        if len(alias_variant) < 2:
            continue
        for query_variant in query_variants:
            if alias_variant in query_variant:
                return True
    return False


def collect_matched_aliases(aliases: list[str], query_text: str) -> list[str]:
    matched: list[str] = []
    for alias in aliases:
        clean = str(alias or "").strip()
        if not clean:
            continue
        if (
            alias_matches_query(clean, query_text)
            or likely_single_typo_match(clean, query_text)
            or likely_entity_prefix_match(clean, query_text)
        ):
            matched.append(clean)
    return matched


def likely_entity_prefix_match(alias_token: str, query_token: str) -> bool:
    """Match brand/model shorthand such as "奥迪" -> "奥迪A4L".

    This is intentionally narrower than free-form fuzzy matching: it only
    fires for concrete aliases that start with a Chinese brand-like prefix and
    continue with a model suffix, and only when the customer is asking about
    availability/details.  That gives the Brain the right product evidence
    without turning generic words into product facts.
    """

    alias = compact_match_text(alias_token)
    query = compact_match_text(query_token)
    if len(alias) < 4 or len(alias) > 16 or not query or len(query) > 64:
        return False
    match = re.match(r"^([\u4e00-\u9fff]{2,4})(?=[a-z0-9])", alias)
    if not match:
        return False
    prefix = match.group(1)
    if prefix not in query:
        return False
    availability_terms = (
        "有",
        "有没有",
        "还有",
        "库存",
        "现车",
        "在售",
        "报价",
        "价格",
        "多少钱",
        "情况",
        "推荐",
        "看看",
    )
    return any(compact_match_text(term) in query for term in availability_terms)


def likely_single_typo_match(alias_token: str, query_token: str) -> bool:
    alias = compact_match_text(alias_token)
    query = compact_match_text(query_token)
    if len(alias) < 3 or len(alias) > 12 or len(query) < len(alias):
        return False
    # Avoid overly broad typo matching for long mixed sentences.
    if len(query) > 48:
        return False
    for start in range(0, len(query) - len(alias) + 1):
        window = query[start : start + len(alias)]
        if not window:
            continue
        if window[0] != alias[0] and window[-1] != alias[-1]:
            continue
        if edit_distance_with_cap(alias, window, cap=1) <= 1:
            return True
    return False


def edit_distance_with_cap(left: str, right: str, *, cap: int) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > cap:
        return cap + 1
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        row_min = current[0]
        for j, char_right in enumerate(right, start=1):
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            substitution = previous[j - 1] + (0 if char_left == char_right else 1)
            value = min(insertion, deletion, substitution)
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > cap:
            return cap + 1
        previous = current
    return previous[-1]
