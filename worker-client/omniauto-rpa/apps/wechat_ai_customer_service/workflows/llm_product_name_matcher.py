"""LLM-assisted product-name normalization for typo/homophone queries."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from apps.wechat_ai_customer_service.llm_config import (
    apply_llm_reasoning_effort,
    call_llm_request_with_failover,
    resolve_effective_llm_provider,
    resolve_llm_api_key,
    resolve_llm_base_url,
    resolve_llm_tier_model,
)
from apps.wechat_ai_customer_service.workflows.product_name_matcher import compact_match_text


DEFAULT_TIMEOUT_SECONDS = 3
DEFAULT_MAX_CANDIDATES = 12
DEFAULT_MIN_CONFIDENCE = 0.72
MAX_CACHE_ENTRIES = 256

_RESULT_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_ORDER: list[str] = []


def llm_match_product_name(
    query: str,
    products: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings if isinstance(settings, dict) else {}
    if not bool(settings.get("enabled", False)):
        return {"applied": False, "matched": False, "reason": "disabled"}

    query_text = str(query or "").strip()
    query_key = compact_match_text(query_text)
    if len(query_key) < 2:
        return {"applied": False, "matched": False, "reason": "query_too_short"}

    normalized_products = normalize_products(products)
    if not normalized_products:
        return {"applied": False, "matched": False, "reason": "empty_products"}

    timeout = bounded_int(settings.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 12)
    max_candidates = bounded_int(settings.get("max_candidates"), DEFAULT_MAX_CANDIDATES, 3, 24)
    min_confidence = bounded_float(settings.get("min_confidence"), DEFAULT_MIN_CONFIDENCE, 0.4, 0.98)
    candidates = shortlist_candidates(query_key, normalized_products, max_candidates=max_candidates)
    if not candidates:
        return {"applied": False, "matched": False, "reason": "no_shortlist_candidates"}

    cache_key = stable_cache_key(query_key, candidates, settings=settings)
    cached = _RESULT_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)

    provider = resolve_effective_llm_provider(settings.get("provider"))
    api_key = resolve_llm_api_key(provider=provider)
    base_url = resolve_llm_base_url(provider=provider, explicit_base_url=str(settings.get("base_url") or "").strip() or None)
    model = resolve_llm_tier_model(
        provider=provider,
        tier="flash",
        explicit_model=str(settings.get("model") or "").strip() or None,
    )
    if not api_key or not base_url or not model:
        result = {"applied": False, "matched": False, "reason": "llm_provider_not_ready", "provider": provider}
        cache_result(cache_key, result)
        return result

    payload = build_llm_payload(query_text, candidates, model=model, provider=provider, settings=settings)
    result_payload = call_llm_request_with_failover(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=list(payload.get("messages") or []),
        timeout=max(1, timeout),
        max_tokens=int(payload.get("max_tokens") or 180),
        temperature=float(payload.get("temperature") or 0),
        tier="flash",
        json_mode=True,
        allow_fallback=False,
    )
    if not result_payload.get("ok"):
        result = {
            "applied": False,
            "matched": False,
            "reason": "llm_http_error" if int(result_payload.get("status") or 0) > 0 else "llm_request_failed",
            "status": int(result_payload.get("status") or 0),
            "error": summarize_error_body(str(result_payload.get("error") or "")),
        }
        cache_result(cache_key, result)
        return result
    parsed = parse_llm_response(str(result_payload.get("response_text") or ""))

    resolved = reconcile_llm_candidate(parsed, candidates, min_confidence=min_confidence)
    cache_result(cache_key, resolved)
    return resolved


def normalize_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in products or []:
        if not isinstance(raw, dict):
            continue
        item_data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        product_id = str(raw.get("id") or item_data.get("id") or "").strip()
        name = str(item_data.get("name") or raw.get("name") or "").strip()
        if not product_id or not name:
            continue
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        aliases = [str(alias).strip() for alias in (item_data.get("aliases", []) or raw.get("aliases", []) or []) if str(alias).strip()]
        sku = str(item_data.get("sku") or raw.get("sku") or "").strip()
        normalized.append({"id": product_id, "name": name, "aliases": aliases, "sku": sku})
    return normalized


def shortlist_candidates(query_key: str, products: list[dict[str, Any]], *, max_candidates: int) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for product in products:
        terms = [str(product.get("name") or ""), str(product.get("sku") or ""), *[str(item) for item in product.get("aliases", []) or []]]
        best_score = 0.0
        for term in terms:
            score = lexical_similarity_score(query_key, compact_match_text(term))
            if score > best_score:
                best_score = score
        if best_score > 0:
            scored.append((best_score, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    shortlisted = [product for _score, product in scored[: max(1, max_candidates)]]
    if shortlisted:
        return shortlisted
    return products[: max(1, max_candidates)]


def lexical_similarity_score(query: str, term: str) -> float:
    if not query or not term:
        return 0.0
    if query in term or term in query:
        return 15.0 + min(len(query), len(term))
    query_chars = set(query)
    term_chars = set(term)
    shared = len(query_chars & term_chars)
    if shared <= 0:
        return 0.0
    score = shared * 2.2
    if has_common_bigram(query, term):
        score += 3.5
    length_gap = abs(len(query) - len(term))
    score -= min(3.0, length_gap * 0.35)
    return max(0.0, score)


def has_common_bigram(left: str, right: str) -> bool:
    if len(left) < 2 or len(right) < 2:
        return False
    left_bigrams = {left[index : index + 2] for index in range(0, len(left) - 1)}
    right_bigrams = {right[index : index + 2] for index in range(0, len(right) - 1)}
    if left_bigrams & right_bigrams:
        return True
    # Latin-style token overlap.
    left_tokens = set(re.findall(r"[a-z0-9]{2,}", left, flags=re.IGNORECASE))
    right_tokens = set(re.findall(r"[a-z0-9]{2,}", right, flags=re.IGNORECASE))
    return bool(left_tokens & right_tokens)


def build_llm_payload(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    model: str,
    provider: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    trimmed_candidates = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "aliases": [str(alias) for alias in (item.get("aliases", []) or [])[:6]],
            "sku": str(item.get("sku") or ""),
        }
        for item in candidates
    ]
    schema = {
        "type": "object",
        "properties": {
            "matched": {"type": "boolean"},
            "product_id": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["matched", "product_id", "confidence", "reason"],
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是商品名归一匹配器。任务：把客户输入匹配到候选商品。"
                    "要识别错别字、同音字、口语简称、英文缩写、音译差异。"
                    "不确定时必须返回 matched=false。仅输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "query": query,
                        "candidates": trimmed_candidates,
                        "output_schema": schema,
                        "decision_rule": "只有高置信度同一商品时 matched=true；否则 matched=false。",
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": bounded_float(settings.get("temperature"), 0.0, 0.0, 0.4),
        "max_tokens": bounded_int(settings.get("max_tokens"), 240, 80, 600),
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    apply_llm_reasoning_effort(payload, provider=provider, tier="flash")
    return payload


def parse_llm_response(raw_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str):
        return {}
    return parse_json_object(content) or {}


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def reconcile_llm_candidate(candidate: dict[str, Any], candidates: list[dict[str, Any]], *, min_confidence: float) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {"applied": True, "matched": False, "reason": "llm_empty_candidate"}
    if not bool(candidate.get("matched", False)):
        return {"applied": True, "matched": False, "reason": str(candidate.get("reason") or "llm_no_match")}
    product_id = str(candidate.get("product_id") or "").strip()
    if not product_id:
        return {"applied": True, "matched": False, "reason": "llm_missing_product_id"}
    confidence = bounded_float(candidate.get("confidence"), 0.0, 0.0, 1.0)
    valid_ids = {str(item.get("id") or "") for item in candidates}
    if product_id not in valid_ids:
        return {"applied": True, "matched": False, "reason": "llm_product_id_not_in_candidates", "confidence": confidence}
    if confidence < min_confidence:
        return {"applied": True, "matched": False, "reason": "llm_confidence_too_low", "confidence": confidence}
    return {
        "applied": True,
        "matched": True,
        "product_id": product_id,
        "confidence": confidence,
        "reason": str(candidate.get("reason") or "llm_semantic_product_match"),
    }


def summarize_error_body(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return str(body or "")[:500]


def stable_cache_key(query_key: str, candidates: list[dict[str, Any]], *, settings: dict[str, Any]) -> str:
    digest = hashlib.sha1()
    digest.update(query_key.encode("utf-8", errors="ignore"))
    for item in candidates:
        digest.update(str(item.get("id") or "").encode("utf-8", errors="ignore"))
        digest.update(str(item.get("name") or "").encode("utf-8", errors="ignore"))
    digest.update(str(settings.get("provider") or "").encode("utf-8", errors="ignore"))
    digest.update(str(settings.get("model") or "").encode("utf-8", errors="ignore"))
    digest.update(str(settings.get("base_url") or "").encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def cache_result(cache_key: str, result: dict[str, Any]) -> None:
    _RESULT_CACHE[cache_key] = dict(result)
    _CACHE_ORDER.append(cache_key)
    while len(_CACHE_ORDER) > MAX_CACHE_ENTRIES:
        stale = _CACHE_ORDER.pop(0)
        _RESULT_CACHE.pop(stale, None)


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
