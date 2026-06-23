"""Dynamic product vocabulary derived from the product master.

Routing may use generic product-category words, but concrete商品/车型/型号词必须
来自商品主数据，避免把测试车型或过期库存散落在代码里。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

try:  # pragma: no cover - supports package and direct workflow imports.
    from knowledge_runtime import KnowledgeRuntime
except ImportError:  # pragma: no cover
    from .knowledge_runtime import KnowledgeRuntime


GENERIC_PRODUCT_QUERY_TERMS = {
    "二手车",
    "车源",
    "现车",
    "车型",
    "轿车",
    "suv",
    "mpv",
    "新能源",
    "油车",
    "混动",
    "自动挡",
    "手动挡",
    "上牌",
    "表显",
    "公里",
    "车况",
    "检测",
    "检测报告",
    "内饰",
    "外观",
    "补漆",
    "一手车",
    "库存",
    "价格",
    "预算",
}

TOKEN_SPLIT_RE = re.compile(r"[\s,，/、|;；()（）\[\]【】]+")


def load_product_vocabulary(tenant_id: str | None = None) -> dict[str, Any]:
    runtime = KnowledgeRuntime(tenant_id=tenant_id)
    products = runtime.list_items("products", include_unacknowledged=False)
    product_terms: list[dict[str, Any]] = []
    all_terms: set[str] = set(GENERIC_PRODUCT_QUERY_TERMS)
    for item in products:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        terms = product_terms_from_item(item)
        if terms:
            all_terms.update(terms)
            product_terms.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(data.get("name") or item.get("name") or ""),
                    "sku": str(data.get("sku") or item.get("sku") or ""),
                    "category": str(data.get("category") or item.get("category") or ""),
                    "aliases": [str(alias) for alias in data.get("aliases", []) or [] if str(alias).strip()],
                    "terms": sorted(terms, key=lambda value: (-len(value), value)),
                }
            )
    return {
        "tenant_id": tenant_id or "",
        "product_terms": product_terms,
        "generic_terms": sorted(GENERIC_PRODUCT_QUERY_TERMS),
        "terms": sorted(all_terms, key=lambda value: (-len(value), value)),
    }


def product_terms_from_item(item: dict[str, Any]) -> set[str]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    raw_values: list[Any] = [
        item.get("id"),
        data.get("name") or item.get("name"),
        data.get("sku") or item.get("sku"),
        data.get("category") or item.get("category"),
        data.get("aliases"),
    ]
    terms: set[str] = set()
    for value in raw_values:
        terms.update(normalize_product_terms(flatten_text_values(value)))
    specs = str(data.get("specs") or "")
    for token in TOKEN_SPLIT_RE.split(specs):
        token = token.strip()
        if 2 <= len(token) <= 24 and looks_like_product_token(token):
            terms.add(normalize_term(token))
    return {term for term in terms if is_usable_product_term(term)}


def contains_product_term(text: str, vocabulary: dict[str, Any] | None = None, *, include_generic: bool = True) -> bool:
    normalized = normalize_term(text)
    if not normalized:
        return False
    vocab = vocabulary or load_product_vocabulary()
    terms = vocab.get("terms" if include_generic else "product_terms", [])
    if not include_generic:
        terms = [term for item in vocab.get("product_terms", []) or [] for term in item.get("terms", []) or []]
    return any(str(term).lower() in normalized for term in terms if is_usable_product_term(str(term)))


def mentioned_product_ids(text: str, vocabulary: dict[str, Any] | None = None) -> list[str]:
    normalized = normalize_term(text)
    vocab = vocabulary or load_product_vocabulary()
    ids: list[str] = []
    for item in vocab.get("product_terms", []) or []:
        product_id = str(item.get("id") or "")
        if not product_id:
            continue
        if any(str(term).lower() in normalized for term in item.get("terms", []) or [] if is_usable_product_term(str(term))):
            ids.append(product_id)
    return ids


def product_terms_for_prompt(vocabulary: dict[str, Any] | None = None, *, limit: int = 80) -> list[str]:
    vocab = vocabulary or load_product_vocabulary()
    terms = [str(term) for term in vocab.get("terms", []) or [] if is_usable_product_term(str(term))]
    return terms[: max(1, limit)]


def flatten_text_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(flatten_text_values(item))
        return values
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(flatten_text_values(item))
        return values
    return [str(value)]


def normalize_product_terms(values: Iterable[str]) -> set[str]:
    terms: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        candidates = {text}
        candidates.update(token for token in TOKEN_SPLIT_RE.split(text) if token)
        for candidate in candidates:
            term = normalize_term(candidate)
            if is_usable_product_term(term):
                terms.add(term)
                compact = "".join(term.split())
                if compact != term and is_usable_product_term(compact):
                    terms.add(compact)
    return terms


def normalize_term(text: str) -> str:
    return str(text or "").strip().lower().replace("＋", "+")


def is_usable_product_term(term: str) -> bool:
    value = normalize_term(term)
    if len(value) < 2:
        return False
    if value.isdigit():
        return False
    if value in {"active", "true", "false", "none", "null"}:
        return False
    return True


def looks_like_product_token(token: str) -> bool:
    value = normalize_term(token)
    if not is_usable_product_term(value):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", value))
