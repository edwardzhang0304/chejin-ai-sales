"""Duplicate detection for knowledge candidates."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from .knowledge_base_store import KnowledgeBaseStore
from apps.wechat_ai_customer_service.knowledge_paths import tenant_review_candidates_root
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import PRODUCT_SCOPED_SCHEMAS


SIMILARITY_THRESHOLD = 0.92


class KnowledgeDeduper:
    def __init__(self, base_store: KnowledgeBaseStore | None = None) -> None:
        self.base_store = base_store or KnowledgeBaseStore()
        self._existing_cache: dict[tuple[str, bool], list[dict[str, Any]]] = {}

    def check_candidate(self, candidate: dict[str, Any], *, include_review_candidates: bool = True) -> dict[str, Any]:
        patch = (((candidate.get("proposal") or {}).get("formal_patch")) or {})
        category_id = str(patch.get("target_category") or "")
        item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
        if not category_id or not item:
            return {"duplicate": False}
        item_id = str(item.get("id") or "")
        candidate_text = duplicate_text(category_id, item)
        candidate_fp = normalized_fingerprint(candidate_text)
        if not candidate_fp:
            return {"duplicate": False}

        for existing in self._list_existing_items(category_id):
            result = compare_item(category_id, item_id, candidate_fp, item, existing, source="knowledge_base")
            if result.get("duplicate"):
                return result

        if not include_review_candidates:
            return {"duplicate": False}

        for existing_candidate in iter_review_candidates():
            if str(existing_candidate.get("candidate_id") or "") == str(candidate.get("candidate_id") or ""):
                continue
            existing_patch = (((existing_candidate.get("proposal") or {}).get("formal_patch")) or {})
            if str(existing_patch.get("target_category") or "") != category_id:
                continue
            existing_item = existing_patch.get("item") if isinstance(existing_patch.get("item"), dict) else {}
            if not existing_item:
                continue
            result = compare_item(
                category_id,
                item_id,
                candidate_fp,
                item,
                existing_item,
                source=f"review_candidate:{existing_candidate.get('candidate_id')}",
            )
            if result.get("duplicate"):
                return result
        return {"duplicate": False}

    def _list_existing_items(self, category_id: str) -> list[dict[str, Any]]:
        cache_key = (category_id, False)
        if cache_key not in self._existing_cache:
            self._existing_cache[cache_key] = self.base_store.list_items(category_id, include_archived=False)
        return self._existing_cache[cache_key]


def compare_item(
    category_id: str,
    item_id: str,
    candidate_fp: str,
    candidate_item: dict[str, Any],
    existing_item: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    existing_id = str(existing_item.get("id") or "")
    existing_fp = normalized_fingerprint(duplicate_text(category_id, existing_item))
    if not existing_fp:
        return {"duplicate": False}
    if item_id and existing_id and item_id == existing_id and candidate_fp == existing_fp:
        return duplicate_result("same_id_same_content", source, existing_id, 1.0)
    similarity = SequenceMatcher(None, candidate_fp, existing_fp).ratio()
    if item_id and existing_id and item_id == existing_id and similarity >= 0.98:
        return duplicate_result("same_id_near_same_content", source, existing_id, similarity)
    if category_id != "chats" and similarity >= SIMILARITY_THRESHOLD:
        return duplicate_result("highly_similar_content", source, existing_id, similarity)
    candidate_key = semantic_key(category_id, candidate_item)
    existing_key = semantic_key(category_id, existing_item)
    if category_id == "products" and candidate_key and candidate_key == existing_key:
        if product_candidate_is_covered(candidate_item, existing_item):
            return duplicate_result("same_product_existing_covers_candidate", source, existing_id, max(similarity, 0.99))
    if candidate_key and candidate_key == existing_key:
        if similarity >= 0.78:
            return duplicate_result("same_business_key_similar_content", source, existing_id, similarity)
    return {"duplicate": False, "similarity": round(similarity, 3)}


def product_candidate_is_covered(candidate_item: dict[str, Any], existing_item: dict[str, Any]) -> bool:
    candidate_data = candidate_item.get("data", {}) or {}
    existing_data = existing_item.get("data", {}) or {}
    comparable_fields = [
        key
        for key, value in candidate_data.items()
        if key not in {"sku", "name", "additional_details", "extra_fields"} and not is_empty(value)
    ]
    if not comparable_fields:
        return True
    return all(field_value_is_covered(key, candidate_data.get(key), existing_data.get(key)) for key in comparable_fields)


def field_value_is_covered(key: str, candidate_value: Any, existing_value: Any) -> bool:
    if is_empty(candidate_value):
        return True
    if key == "price_tiers":
        candidate_tiers = set(normalize_price_tiers(candidate_value))
        existing_tiers = set(normalize_price_tiers(existing_value))
        return bool(candidate_tiers) and candidate_tiers.issubset(existing_tiers)
    if isinstance(candidate_value, (int, float)) or isinstance(existing_value, (int, float)):
        candidate_number = as_float(candidate_value)
        existing_number = as_float(existing_value)
        return candidate_number is not None and existing_number is not None and abs(candidate_number - existing_number) < 0.0001
    if isinstance(candidate_value, list):
        candidate_values = {normalized_fingerprint(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in candidate_value}
        existing_values = {normalized_fingerprint(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in existing_value} if isinstance(existing_value, list) else set()
        return candidate_values.issubset(existing_values)
    if isinstance(candidate_value, dict):
        if not isinstance(existing_value, dict):
            return False
        if key == "reply_templates":
            candidate_texts = {normalized_fingerprint(value) for value in candidate_value.values() if not is_empty(value)}
            existing_texts = {normalized_fingerprint(value) for value in existing_value.values() if not is_empty(value)}
            return candidate_texts.issubset(existing_texts)
        return all(field_value_is_covered(str(sub_key), sub_value, existing_value.get(sub_key)) for sub_key, sub_value in candidate_value.items())
    return normalized_fingerprint(candidate_value) == normalized_fingerprint(existing_value)


def normalize_price_tiers(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    rows = []
    for row in value:
        if not isinstance(row, dict):
            continue
        quantity = as_float(row.get("min_quantity"))
        price = as_float(row.get("unit_price"))
        if quantity is None or price is None:
            continue
        rows.append((quantity, price))
    return sorted(rows, key=lambda row: row[0])


def duplicate_result(reason: str, source: str, existing_id: str, similarity: float) -> dict[str, Any]:
    return {
        "duplicate": True,
        "reason": reason,
        "source": source,
        "existing_item_id": existing_id,
        "similarity": round(float(similarity), 3),
        "message": "检测到与现有知识或候选高度重复，已跳过去重。",
    }


def duplicate_text(category_id: str, item: dict[str, Any]) -> str:
    data = item.get("data", {}) or {}
    if category_id == "products":
        values = [
            item.get("id"),
            data.get("name"),
            data.get("sku"),
            data.get("category"),
            data.get("aliases"),
            data.get("specs"),
            data.get("price"),
            data.get("unit"),
            data.get("price_tiers"),
            data.get("shipping_policy"),
            data.get("warranty_policy"),
            data.get("reply_templates"),
            data.get("risk_rules"),
            data.get("additional_details"),
        ]
    elif category_id == "policies":
        values = [
            data.get("title"),
            data.get("policy_type"),
            data.get("keywords"),
            data.get("answer"),
            data.get("handoff_reason"),
            data.get("additional_details"),
        ]
    elif category_id == "chats":
        values = [
            data.get("customer_message"),
            data.get("service_reply"),
            data.get("intent_tags"),
            data.get("tone_tags"),
            data.get("additional_details"),
        ]
    elif category_id == "erp_exports":
        values = [data.get("source_system"), data.get("record_type"), data.get("external_id"), data.get("fields")]
    elif category_id in PRODUCT_SCOPED_SCHEMAS:
        values = [
            data.get("product_id"),
            data.get("title"),
            data.get("keywords"),
            data.get("question"),
            data.get("answer"),
            data.get("content"),
            data.get("additional_details"),
        ]
    else:
        values = [data]
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def semantic_key(category_id: str, item: dict[str, Any]) -> str:
    data = item.get("data", {}) or {}
    if category_id == "products":
        return normalize_key(data.get("sku") or data.get("name"))
    if category_id == "policies":
        return normalize_key(f"{data.get('policy_type') or ''}:{data.get('title') or ''}")
    if category_id == "chats":
        return normalize_key(data.get("service_reply"))
    if category_id == "erp_exports":
        return normalize_key(f"{data.get('source_system') or ''}:{data.get('external_id') or ''}")
    if category_id in PRODUCT_SCOPED_SCHEMAS:
        return normalize_key(f"{data.get('product_id') or ''}:{data.get('title') or item.get('id') or ''}")
    return normalize_key(item.get("id"))


def normalized_fingerprint(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[\"'`，,。.;；:：、|/\\\[\]{}()（）<>《》_-]+", "", text)
    return text


def normalize_key(value: Any) -> str:
    return normalized_fingerprint(str(value or ""))


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iter_review_candidates() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    review_root = tenant_review_candidates_root()
    for status in ("pending", "approved"):
        root = review_root / status
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return items
