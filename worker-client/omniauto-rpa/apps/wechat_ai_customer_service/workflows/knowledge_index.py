"""Search helpers for category knowledge items."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - supports both package and script imports.
    from .knowledge_runtime import KnowledgeRuntime
    from .product_name_matcher import collect_matched_aliases
except ImportError:  # pragma: no cover
    from knowledge_runtime import KnowledgeRuntime
    from product_name_matcher import collect_matched_aliases

from apps.wechat_ai_customer_service.platform_understanding_rules import (
    intent_group,
    intent_keywords,
    quantity_unit_pattern,
    string_map,
)


PRODUCT_SCOPED_CATEGORY_IDS = {"product_faq", "product_rules", "product_explanations"}


def policy_type_to_intent() -> dict[str, str]:
    return string_map("policy_type_to_intent")


def product_context_intents() -> set[str]:
    group = intent_group("product_context")
    if group:
        return group
    return {"shipping", "warranty", "spec", "quote", "stock", "discount", "scene_product"}


def business_intents() -> set[str]:
    return intent_group("business")


def product_related_intents() -> set[str]:
    return intent_group("product_related")


@dataclass(frozen=True)
class KnowledgeHit:
    category_id: str
    item_id: str
    title: str
    matched_fields: tuple[str, ...]
    match_reason: str
    confidence: float
    category: dict[str, Any]
    schema: dict[str, Any]
    resolver: dict[str, Any]
    item: dict[str, Any]


class KnowledgeIndex:
    """A small in-memory index built from resolver-defined match fields."""

    def __init__(self, runtime: KnowledgeRuntime | None = None) -> None:
        self.runtime = runtime or KnowledgeRuntime()

    def search(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
        intent_tags: list[str] | None = None,
        limit: int = 12,
    ) -> list[KnowledgeHit]:
        context = context or {}
        intent_tags = sorted(set(intent_tags or detect_intent_tags(text)))
        normalized_text = normalize_text(text)
        hits = self._collect_hits(normalized_text, context=context, intent_tags=intent_tags)
        inferred_tags = infer_intent_tags_from_hits(intent_tags, hits)
        if set(inferred_tags) != set(intent_tags):
            intent_tags = inferred_tags
            hits = self._collect_hits(normalized_text, context=context, intent_tags=intent_tags)

        hits = dedupe_hits(hits)
        hits = suppress_shared_hits_covered_by_tenant(hits)
        hits.sort(key=lambda hit: (hit.confidence, layer_rank(hit), -category_rank(hit.category_id)), reverse=True)
        return hits[:limit]

    def _collect_hits(
        self,
        normalized_text: str,
        *,
        context: dict[str, Any],
        intent_tags: list[str],
    ) -> list[KnowledgeHit]:
        hits: list[KnowledgeHit] = []
        product_category = self.runtime.get_category("products") or {
            "id": "products",
            "name": "商品主数据",
            "scope": "product_master",
        }
        try:
            product_schema = self.runtime.load_schema("products")
            product_resolver = self.runtime.load_resolver("products")
            product_items = self.runtime.list_items("products", include_unacknowledged=False)
        except FileNotFoundError:
            product_items = []
            product_schema = {}
            product_resolver = {}
        for item in product_items:
            hit = self._match_item(product_category, product_schema, product_resolver, item, normalized_text, intent_tags)
            if hit:
                hits.append(hit)
            context_hit = self._context_product_hit(product_category, product_schema, product_resolver, item, context, intent_tags)
            if context_hit:
                hits.append(context_hit)

        for category, schema, resolver, item in self.runtime.iter_reply_items():
            category_id = str(category.get("id") or "")
            if not item_applies_to_context(category_id, item, context, hits):
                continue
            hit = self._match_item(category, schema, resolver, item, normalized_text, intent_tags)
            if hit:
                hits.append(hit)

        product_ids = {hit.item_id for hit in hits if hit.category_id == "products"}
        if context.get("last_product_id"):
            product_ids.add(str(context.get("last_product_id")))
        for category, schema, resolver, item in self.runtime.iter_product_scoped_items(product_ids):
            hit = self._match_item(category, schema, resolver, item, normalized_text, intent_tags)
            if hit:
                hits.append(hit)

        return hits

    def _match_item(
        self,
        category: dict[str, Any],
        schema: dict[str, Any],
        resolver: dict[str, Any],
        item: dict[str, Any],
        normalized_text: str,
        intent_tags: list[str],
    ) -> KnowledgeHit | None:
        category_id = str(category.get("id") or "")
        data = item.get("data", {}) or {}
        matched_fields: list[str] = []
        exact_matches = 0
        intent_matches = 0
        if category_id == "global_guidelines" and data.get("always_include", False):
            matched_fields.append("always_include")
            intent_matches += 1
        if category_id in PRODUCT_SCOPED_CATEGORY_IDS and data.get("always_include", False):
            matched_fields.append("always_include")
            intent_matches += 1
        for field_id in resolver.get("match_fields", []) or []:
            values = flatten_values(data.get(field_id))
            field_matched, field_exact, field_intent = field_matches(field_id, values, normalized_text, intent_tags, data)
            if field_matched:
                matched_fields.append(str(field_id))
                exact_matches += field_exact
                intent_matches += field_intent

        if category_id == "products" and "catalog" in intent_tags:
            matched_fields.append("catalog")
            intent_matches += 1

        if category_id == "policies":
            policy_intent = policy_type_to_intent().get(str(data.get("policy_type") or ""))
            if policy_intent and policy_intent in intent_tags:
                matched_fields.append("policy_type")
                intent_matches += 1

        if not matched_fields:
            return None
        if category_id == "policies" and item_blocks_auto_reply(data, item.get("runtime", {}) or {}) and exact_matches <= 0:
            return None
        if category_id == "policies" and item_blocks_auto_reply(data, item.get("runtime", {}) or {}) and not blocking_policy_match_is_contextual(
            item_id=str(item.get("id") or ""),
            data=data,
            normalized_text=normalized_text,
            matched_fields=matched_fields,
        ):
            return None

        confidence = score_hit(category_id, matched_fields, exact_matches, intent_matches)
        minimum = float(resolver.get("minimum_confidence", 0.4) or 0.4)
        if confidence < minimum:
            return None
        return KnowledgeHit(
            category_id=category_id,
            item_id=str(item.get("id") or ""),
            title=item_title(schema, item),
            matched_fields=tuple(sorted(set(matched_fields))),
            match_reason=match_reason(category_id, matched_fields, intent_matches),
            confidence=round(confidence, 3),
            category=category,
            schema=schema,
            resolver=resolver,
            item=item,
        )

    def _context_product_hit(
        self,
        category: dict[str, Any],
        schema: dict[str, Any],
        resolver: dict[str, Any],
        item: dict[str, Any],
        context: dict[str, Any],
        intent_tags: list[str],
    ) -> KnowledgeHit | None:
        if str(category.get("id") or "") != "products":
            return None
        last_product_id = str(context.get("last_product_id") or "")
        if not last_product_id or str(item.get("id") or "") != last_product_id:
            return None
        if not (set(intent_tags) & product_context_intents()):
            return None
        return KnowledgeHit(
            category_id="products",
            item_id=str(item.get("id") or ""),
            title=item_title(schema, item),
            matched_fields=("conversation_context",),
            match_reason="conversation_context_product",
            confidence=0.7,
            category=category,
            schema=schema,
            resolver=resolver,
            item=item,
        )


def detect_intent_tags(text: str) -> list[str]:
    normalized = normalize_text(text)
    tags = {
        tag
        for tag, keywords in intent_keywords().items()
        if any(normalize_text(keyword) in normalized for keyword in keywords)
    }
    if re.search(rf"\d+\s*({quantity_unit_pattern()})", normalized, re.IGNORECASE):
        tags.add("quote")
        tags.add("product")
    if tags & product_related_intents():
        tags.add("product")
    if not tags:
        tags.add("unknown")
    return sorted(tags)


def infer_intent_tags_from_hits(intent_tags: list[str], hits: list[KnowledgeHit]) -> list[str]:
    tags = set(intent_tags)
    if hits and "unknown" in tags:
        tags.remove("unknown")
    known_tags = set(intent_keywords()) | business_intents() | {"product", "small_talk", "greeting", "unknown"}
    for hit in hits:
        data = hit.item.get("data", {}) or {}
        if hit.category_id == "products":
            tags.add("product")
            continue
        matched_fields = {str(field) for field in hit.matched_fields}
        if hit.category_id in {"global_guidelines", "reply_style"}:
            continue
        if not (matched_fields & {"keywords", "title", "question", "customer_message", "answer", "content", "guideline_text"}):
            continue
        strong_intent_fields = matched_fields & {"keywords", "title", "question", "customer_message"}
        policy_type = normalize_text(data.get("policy_type"))
        mapped = policy_type_to_intent().get(policy_type)
        if mapped and strong_intent_fields:
            tags.add(mapped)
            if mapped in product_related_intents():
                tags.add("product")
        if not strong_intent_fields:
            continue
        for value in flatten_values(data.get("intent_tags")):
            tag = normalize_text(value)
            if tag in known_tags:
                tags.add(tag)
                if tag in product_related_intents():
                    tags.add("product")
    if not tags:
        tags.add("unknown")
    return sorted(tags)


def normalize_text(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower().strip()


def flatten_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(flatten_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(flatten_values(item))
        return values
    return [str(value)]


def field_matches(
    field_id: str,
    values: list[str],
    normalized_text: str,
    intent_tags: list[str],
    data: dict[str, Any],
) -> tuple[bool, int, int]:
    exact_matches = 0
    intent_matches = 0
    if field_id in {"name", "aliases", "sku"}:
        matched_aliases = collect_matched_aliases([str(value) for value in values], normalized_text)
        if matched_aliases:
            exact_matches += len(matched_aliases)

    for value in values:
        normalized_value = normalize_text(value)
        if not normalized_value:
            continue
        if field_id in {"intent_tags", "tone_tags"}:
            if normalized_value in intent_tags:
                intent_matches += 1
                continue
        if field_id == "policy_type":
            mapped = policy_type_to_intent().get(normalized_value, normalized_value)
            if mapped in intent_tags:
                intent_matches += 1
                continue
        if len(normalized_value) >= 2 and normalized_value in normalized_text:
            exact_matches += 1
            continue
        if len(normalized_text) >= 4 and normalized_text in normalized_value:
            exact_matches += 1
            continue
        if field_id in {"answer", "content", "question"} and has_shared_fragment(normalized_value, normalized_text):
            exact_matches += 1
            continue

    return exact_matches + intent_matches > 0, exact_matches, intent_matches


def blocking_policy_match_is_contextual(
    *,
    item_id: str,
    data: dict[str, Any],
    normalized_text: str,
    matched_fields: list[str],
) -> bool:
    """Let visible policy data define when broad blocking keywords are contextual."""
    if not ({"keywords", "answer", "title"} & set(matched_fields)):
        return True
    strict_phrases = strict_match_phrases(data)
    if not strict_phrases:
        return True
    return any(normalize_text(phrase) in normalized_text for phrase in strict_phrases)


def strict_match_phrases(data: dict[str, Any]) -> list[str]:
    details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
    values = []
    values.extend(flatten_values(data.get("strict_match_phrases")))
    values.extend(flatten_values(details.get("strict_match_phrases")))
    return [str(value) for value in values if str(value).strip()]


def item_applies_to_context(
    category_id: str,
    item: dict[str, Any],
    context: dict[str, Any],
    hits: list[KnowledgeHit],
) -> bool:
    if category_id not in {"chats", "policies"}:
        return True
    data = item.get("data", {}) or {}
    scope = str(data.get("applicability_scope") or "global").strip() or "global"
    if scope == "global":
        return True
    if scope == "specific_product":
        target_product = normalize_text(data.get("product_id"))
        if not target_product:
            return False
        return target_product in context_product_ids(context, hits)
    if scope == "product_category":
        target_category = normalize_text(data.get("product_category"))
        if not target_category:
            return False
        return target_category in context_product_categories(context, hits)
    return True


def suppress_shared_hits_covered_by_tenant(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
    tenant_hits = [hit for hit in hits if knowledge_layer(hit) != "shared"]
    if not tenant_hits:
        return hits
    filtered: list[KnowledgeHit] = []
    for hit in hits:
        if knowledge_layer(hit) == "shared" and shared_hit_overlaps_tenant(hit, tenant_hits):
            continue
        filtered.append(hit)
    return filtered


def shared_hit_overlaps_tenant(shared_hit: KnowledgeHit, tenant_hits: list[KnowledgeHit]) -> bool:
    if shared_hit.category_id == "global_guidelines" and "always_include" in shared_hit.matched_fields:
        return False
    for tenant_hit in tenant_hits:
        if tenant_hit.category_id in {"products"} and shared_hit.category_id in {"reply_style", "global_guidelines"}:
            continue
        if same_business_key(shared_hit, tenant_hit):
            return True
        if same_title(shared_hit, tenant_hit):
            return True
        if keyword_overlap(shared_hit, tenant_hit):
            return True
        if text_overlap(shared_hit, tenant_hit):
            return True
    return False


def same_business_key(left: KnowledgeHit, right: KnowledgeHit) -> bool:
    if left.category_id == right.category_id and left.item_id and left.item_id == right.item_id:
        return True
    left_data = left.item.get("data", {}) or {}
    right_data = right.item.get("data", {}) or {}
    for key in ("policy_type", "product_id", "applicability_scope"):
        left_value = normalize_text(left_data.get(key))
        right_value = normalize_text(right_data.get(key))
        if left_value and right_value and left_value == right_value:
            return True
    return False


def same_title(left: KnowledgeHit, right: KnowledgeHit) -> bool:
    left_title = normalize_text(left.title or (left.item.get("data", {}) or {}).get("title"))
    right_title = normalize_text(right.title or (right.item.get("data", {}) or {}).get("title"))
    return bool(left_title and right_title and (left_title == right_title or left_title in right_title or right_title in left_title))


def keyword_overlap(left: KnowledgeHit, right: KnowledgeHit) -> bool:
    left_keywords = item_keywords(left)
    right_keywords = item_keywords(right)
    if not left_keywords or not right_keywords:
        return False
    return bool(left_keywords & right_keywords)


def text_overlap(left: KnowledgeHit, right: KnowledgeHit) -> bool:
    left_text = item_search_text(left)
    right_text = item_search_text(right)
    if len(left_text) < 8 or len(right_text) < 8:
        return False
    if left_text in right_text or right_text in left_text:
        return True
    left_tokens = business_tokens(left_text)
    right_tokens = business_tokens(right_text)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) >= 2


def item_keywords(hit: KnowledgeHit) -> set[str]:
    data = hit.item.get("data", {}) or {}
    values = flatten_values(data.get("keywords"))
    values.extend(flatten_values(data.get("intent_tags")))
    values.extend(flatten_values(data.get("policy_type")))
    return {normalize_text(value) for value in values if len(normalize_text(value)) >= 2}


def item_search_text(hit: KnowledgeHit) -> str:
    data = hit.item.get("data", {}) or {}
    fields = ["title", "keywords", "policy_type", "customer_message", "service_reply", "question", "answer", "content", "guideline_text", "handoff_reason"]
    parts = [hit.title, hit.item_id]
    for field in fields:
        parts.extend(flatten_values(data.get(field)))
    return normalize_text(" ".join(str(part) for part in parts if part not in (None, "", [], {})))


def business_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", normalize_text(text)))
    stopwords = {"客户", "客服", "需要", "可以", "确认", "回复", "规则", "人工", "处理", "问题", "信息", "不能", "应该"}
    return {token for token in tokens if token not in stopwords}


def context_product_ids(context: dict[str, Any], hits: list[KnowledgeHit]) -> set[str]:
    values: set[str] = set()
    for key in ("product_id", "last_product_id"):
        value = normalize_text(context.get(key))
        if value:
            values.add(value)
    product_ids = context.get("product_ids") or []
    if isinstance(product_ids, (str, int, float)):
        product_ids = [product_ids]
    for value in product_ids:
        normalized = normalize_text(value)
        if normalized:
            values.add(normalized)
    for hit in hits:
        if hit.category_id == "products":
            values.add(normalize_text(hit.item_id))
    return values


def context_product_categories(context: dict[str, Any], hits: list[KnowledgeHit]) -> set[str]:
    values: set[str] = set()
    for key in ("product_category", "last_product_category"):
        value = normalize_text(context.get(key))
        if value:
            values.add(value)
    product_categories = context.get("product_categories") or []
    if isinstance(product_categories, (str, int, float)):
        product_categories = [product_categories]
    for value in product_categories:
        normalized = normalize_text(value)
        if normalized:
            values.add(normalized)
    for hit in hits:
        if hit.category_id == "products":
            data = hit.item.get("data", {}) or {}
            category = normalize_text(data.get("category"))
            if category:
                values.add(category)
    return values


def has_shared_fragment(value: str, text: str, *, min_size: int = 4) -> bool:
    if len(value) < min_size or len(text) < min_size:
        return False
    for start in range(0, len(value) - min_size + 1):
        fragment = value[start : start + min_size]
        if fragment and fragment in text:
            return True
    return False


def score_hit(category_id: str, matched_fields: list[str], exact_matches: int, intent_matches: int) -> float:
    base = 0.32 + min(0.32, 0.08 * len(set(matched_fields)))
    exact_score = min(0.36, 0.1 * exact_matches)
    intent_score = min(0.2, 0.07 * intent_matches)
    category_boost = 0.0
    if category_id == "products" and any(field in matched_fields for field in ("name", "aliases", "sku")):
        category_boost = 0.12
    if category_id == "policies" and any(field in matched_fields for field in ("keywords", "policy_type")):
        category_boost = 0.1
    if category_id == "chats" and "intent_tags" in matched_fields:
        category_boost = 0.08
    if category_id == "global_guidelines":
        category_boost = 0.04
    if category_id in PRODUCT_SCOPED_CATEGORY_IDS:
        category_boost = 0.1
    return min(0.98, base + exact_score + intent_score + category_boost)


def item_blocks_auto_reply(data: dict[str, Any], runtime: dict[str, Any]) -> bool:
    data_has_auto_reply = "allow_auto_reply" in data
    data_has_handoff = "requires_handoff" in data
    allow_auto_reply = data.get("allow_auto_reply") if data_has_auto_reply else runtime.get("allow_auto_reply", True)
    requires_handoff = data.get("requires_handoff") if data_has_handoff else runtime.get("requires_handoff", False)
    return bool(allow_auto_reply is False or requires_handoff)


def match_reason(category_id: str, matched_fields: list[str], intent_matches: int) -> str:
    if "conversation_context" in matched_fields:
        return "conversation_context_product"
    if category_id == "products" and any(field in matched_fields for field in ("name", "aliases", "sku")):
        return "product_alias_or_identifier"
    if category_id == "policies" and intent_matches:
        return "policy_keyword_or_intent"
    if category_id == "chats":
        return "chat_style_or_intent"
    if category_id == "global_guidelines":
        return "global_style_guideline"
    if category_id in PRODUCT_SCOPED_CATEGORY_IDS:
        return "product_scoped_knowledge"
    return "resolver_match_fields"


def item_title(schema: dict[str, Any], item: dict[str, Any]) -> str:
    data = item.get("data", {}) or {}
    title_field = str(schema.get("item_title_field") or "title")
    return str(data.get(title_field) or item.get("id") or "")


def category_rank(category_id: str) -> int:
    order = {"products": 10, "product_rules": 18, "product_faq": 19, "product_explanations": 20, "policies": 30, "chats": 40, "global_guidelines": 80}
    return order.get(category_id, 90)


def knowledge_layer(hit: KnowledgeHit) -> str:
    metadata = hit.item.get("metadata") if isinstance(hit.item.get("metadata"), dict) else {}
    return str(hit.item.get("_knowledge_layer") or metadata.get("knowledge_layer") or "tenant")


def layer_rank(hit: KnowledgeHit) -> int:
    return {"product_master": 35, "tenant_product": 30, "tenant": 20, "shared": 10}.get(knowledge_layer(hit), 20)


def dedupe_hits(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
    best: dict[tuple[str, str], KnowledgeHit] = {}
    for hit in hits:
        key = (hit.category_id, hit.item_id)
        existing = best.get(key)
        if not existing or hit.confidence > existing.confidence:
            best[key] = hit
    return list(best.values())
