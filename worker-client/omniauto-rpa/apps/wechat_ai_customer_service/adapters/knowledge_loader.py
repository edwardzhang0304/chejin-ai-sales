"""Scoped business-knowledge loading for the WeChat AI customer-service app.

This module is intentionally side-effect free. It reads only the app-local
manifest and structured data files, then builds a compact evidence pack for one
customer message.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
STRUCTURED_DATA_ROOT = APP_ROOT / "data" / "structured"
DEFAULT_MANIFEST_PATH = STRUCTURED_DATA_ROOT / "manifest.json"
WORKFLOWS_ROOT = APP_ROOT / "workflows"
if str(WORKFLOWS_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_ROOT))

from evidence_resolver import EvidenceResolver  # noqa: E402
from knowledge_runtime import KnowledgeRuntime  # noqa: E402
from rag_layer import RagService  # noqa: E402
from apps.wechat_ai_customer_service.workflows.product_name_matcher import (  # noqa: E402
    collect_matched_aliases,
)
from apps.wechat_ai_customer_service.workflows.llm_product_name_matcher import (  # noqa: E402
    llm_match_product_name,
)
from apps.wechat_ai_customer_service.platform_understanding_rules import (  # noqa: E402
    intent_group,
    intent_keywords,
    map_of_lists,
    quantity_unit_pattern,
    rag_terms,
    string_map,
)
try:
    from customer_service_brain_contract import strip_nonsemantic_runtime_markers  # noqa: E402
except Exception:  # pragma: no cover - keep knowledge loading usable in narrow script contexts
    strip_nonsemantic_runtime_markers = None  # type: ignore[assignment]

PRODUCT_SCOPED_CATEGORY_IDS = {"product_faq", "product_rules", "product_explanations"}
RAG_SOFT_REFERENCE_MIN_SCORE = 0.18


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or DEFAULT_MANIFEST_PATH
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    return load_json(manifest_path)


def detect_intent_tags(text: str) -> list[str]:
    normalized = normalize_text(text)
    tags = {
        tag
        for tag, keywords in intent_keywords().items()
        if any(keyword.lower() in normalized for keyword in keywords)
    }
    if is_pre_purchase_vehicle_maintenance_guidance(normalized):
        tags.discard("after_sales")
        tags.add("scene_product")
    if re.search(rf"\d+\s*({quantity_unit_pattern()})", normalized, re.IGNORECASE):
        tags.add("quote")
        tags.add("product")
    if tags & {"quote", "discount", "stock", "shipping", "warranty", "spec", "scene_product"}:
        tags.add("product")
    if not tags:
        tags.add("unknown")
    return sorted(tags)


def build_evidence_pack(
    text: str,
    *,
    manifest_path: Path | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_text = semantic_text_for_evidence_matching(text)
    if manifest_path is not None:
        pack = build_structured_evidence_pack(semantic_text, manifest_path=manifest_path, context=context)
    else:
        pack = build_category_evidence_pack(semantic_text, context=context)
    if semantic_text != str(text or ""):
        pack["raw_input_text"] = str(text or "")
        pack["input_text_sanitized"] = True
    return pack


def semantic_text_for_evidence_matching(text: str) -> str:
    raw = str(text or "")
    if callable(strip_nonsemantic_runtime_markers):
        return str(strip_nonsemantic_runtime_markers(raw) or "").strip()
    return raw.strip()


def build_structured_evidence_pack(
    text: str,
    *,
    manifest_path: Path | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    manifest = load_manifest(manifest_path)
    intent_tags = detect_intent_tags(text)
    selected_items = select_manifest_items(manifest, intent_tags)
    evidence: dict[str, Any] = {
        "products": [],
        "faq": [],
        "policies": {},
        "style_examples": [],
        "product_scoped": [],
    }

    for item in selected_items:
        item_path = resolve_item_path(item)
        if item.get("id") == "product_knowledge":
            merge_product_knowledge_evidence(evidence, load_json(item_path), text, intent_tags, context=context)
        elif item.get("id") == "style_examples":
            merge_style_evidence(evidence, load_json(item_path), intent_tags)

    return {
        "schema_version": 1,
        "scope": str(manifest.get("scope") or "wechat_ai_customer_service"),
        "input_text": text,
        "intent_tags": intent_tags,
        "selected_items": [
            {
                "id": str(item.get("id") or ""),
                "path": str(item.get("path") or ""),
                "matched_tags": sorted(set(intent_tags) & set(item.get("intent_tags", []) or [])),
                "summary": str(item.get("summary") or ""),
            }
            for item in selected_items
        ],
        "conversation_context": sanitize_context(context),
        "evidence": evidence,
        "safety": build_safety_summary(intent_tags, evidence, text),
    }


def build_category_evidence_pack(text: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    category_pack = EvidenceResolver(KnowledgeRuntime()).resolve(text, context=context)
    resolver_tags = set(category_pack.get("intent_tags", []) or [])
    detected_tags = set(detect_intent_tags(text))
    # If the resolver already inferred meaningful intents, don't let local keyword detection
    # inject "unknown" back and override the inferred classification.
    if resolver_tags - {"unknown"}:
        detected_tags.discard("unknown")
    intent_tags = normalize_pre_purchase_vehicle_tags(sorted(resolver_tags | detected_tags), text)
    evidence = legacy_evidence_from_category_pack(category_pack)
    suppress_weak_product_evidence_without_need(evidence, text)
    safety = build_safety_summary(intent_tags, evidence, text)
    rag_evidence = build_rag_runtime_evidence(text, intent_tags=intent_tags, evidence=evidence, context=context)
    if rag_evidence.get("hits"):
        evidence["rag"] = rag_evidence
        allow_soft_rag_reference(safety, intent_tags=intent_tags, rag_evidence=rag_evidence, text=text)
    category_safety = category_pack.get("safety", {}) or {}
    soft_pre_purchase_category_safety = is_soft_pre_purchase_category_safety(category_safety, text)
    for reason in category_safety.get("reasons", []) or []:
        if soft_pre_purchase_category_safety and reason == "no_relevant_business_evidence":
            continue
        if reason not in safety["reasons"]:
            safety["reasons"].append(reason)
    if category_safety.get("must_handoff") and not soft_pre_purchase_category_safety:
        safety["must_handoff"] = True
        safety["allowed_auto_reply"] = False
    if rag_evidence.get("hits"):
        allow_soft_rag_reference(safety, intent_tags=intent_tags, rag_evidence=rag_evidence, text=text)
    return {
        "schema_version": 1,
        "scope": "wechat_ai_customer_service",
        "input_text": text,
        "intent_tags": intent_tags,
        "selected_items": legacy_selected_items(category_pack),
        "conversation_context": sanitize_context(context),
        "evidence": evidence,
        "rag_evidence": rag_evidence,
        "safety": safety,
        "category_evidence": category_pack,
        "matched_categories": category_pack.get("matched_categories", []),
    }


def legacy_evidence_from_category_pack(category_pack: dict[str, Any]) -> dict[str, Any]:
    runtime = KnowledgeRuntime()
    evidence: dict[str, Any] = {
        "products": [],
        "faq": [],
        "policies": {},
        "style_examples": [],
        "product_scoped": [],
    }
    for evidence_item in category_pack.get("evidence_items", []) or []:
        category_id = str(evidence_item.get("category_id") or "")
        item_id = str(evidence_item.get("item_id") or "")
        item = runtime.get_item(category_id, item_id)
        if not item:
            continue
        data = item.get("data", {}) or {}
        if category_id == "products":
            evidence["products"].append(legacy_product_snippet(item, evidence_item))
        elif category_id == "policies":
            faq = legacy_policy_faq(item, evidence_item)
            evidence["faq"].append(faq)
            policy_key = legacy_policy_key(data, item_id)
            if policy_key:
                evidence["policies"][policy_key] = legacy_policy_payload(data)
        elif category_id == "chats":
            evidence["style_examples"].append(legacy_style_example(item, evidence_item))
        elif category_id == "global_guidelines":
            evidence["style_examples"].append(legacy_style_example(item, evidence_item))
        elif category_id == "reply_style":
            evidence["style_examples"].append(legacy_style_example(item, evidence_item))
        elif category_id == "risk_control":
            evidence["faq"].append(legacy_shared_risk_control_faq(item, evidence_item))
        elif category_id in PRODUCT_SCOPED_CATEGORY_IDS:
            scoped = legacy_product_scoped_item(item, evidence_item)
            evidence["product_scoped"].append(scoped)
            if scoped.get("answer"):
                evidence["faq"].append(legacy_product_scoped_faq(scoped))
    evidence["products"] = dedupe_legacy_items(evidence["products"], "id")
    evidence["faq"] = dedupe_legacy_items(evidence["faq"], "intent")
    evidence["style_examples"] = dedupe_legacy_items(evidence["style_examples"], "id")
    evidence["product_scoped"] = dedupe_legacy_items(evidence["product_scoped"], "id")
    return evidence


def build_rag_runtime_evidence(
    text: str,
    *,
    intent_tags: list[str],
    evidence: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not should_use_rag(intent_tags, evidence, text=text):
        return build_ai_experience_reference_evidence_when_rag_skipped(text)
    try:
        rag = RagService().evidence(text, context=context, limit=5)
    except Exception as exc:
        return {"enabled": True, "ok": False, "error": repr(exc), "hits": [], "rag_can_authorize": False, "structured_priority": True}
    rag["ok"] = True
    return rag


def build_ai_experience_reference_evidence_when_rag_skipped(text: str) -> dict[str, Any]:
    payload = {"enabled": True, "skipped": True, "reason": "structured_evidence_sufficient", "hits": []}
    if not should_use_ai_experience_reference(text):
        return payload
    try:
        reference = RagService().search_experience_references(text, limit=3)
    except Exception as exc:
        payload.update({"ok": False, "reference_error": repr(exc)})
        return payload
    payload["ok"] = True
    payload["reference_hit_count"] = len(reference.get("hits", []) or [])
    payload["hits"] = reference.get("hits", []) or []
    if payload["hits"]:
        payload["reason"] = "structured_evidence_sufficient_with_ai_experience_reference"
    return payload


def should_use_ai_experience_reference(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized) < 4:
        return False
    markers = (
        "怎么回复",
        "客户",
        "用户",
        "买家",
        "嫌",
        "接受不了",
        "担心",
        "比较",
        "推荐",
        "建议",
        "挑",
        "适合",
        "预算",
        "用途",
        "家人",
        "老婆",
        "老公",
        "新手",
        "先看看",
    )
    return any(marker in normalized for marker in markers)


def allow_soft_rag_reference(
    safety: dict[str, Any],
    *,
    intent_tags: list[str],
    rag_evidence: dict[str, Any],
    text: str = "",
) -> None:
    if not rag_can_support_soft_reference(intent_tags, rag_evidence, text=text):
        return
    reasons = [str(item) for item in safety.get("reasons", []) or [] if str(item)]
    removable = {"no_relevant_business_evidence"}
    if is_soft_installation_reference(text):
        removable.update(
            {
                "handoff_intent_detected",
                "matched_faq_requires_handoff",
                "installation_requires_manual_confirmation",
                "auto_reply_disabled",
            }
        )
        safety["rag_soft_installation_reference_allowed"] = True
    reasons = [reason for reason in reasons if reason not in removable]
    safety["reasons"] = reasons
    safety["rag_soft_reference_allowed"] = True
    if not reasons:
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True


def rag_can_support_soft_reference(intent_tags: list[str], rag_evidence: dict[str, Any], *, text: str = "") -> bool:
    tag_set = set(intent_tags)
    blocked_tags = tag_set & intent_group("rag_authority_block")
    soft_installation = is_soft_installation_reference(text)
    if blocked_tags and not (blocked_tags == {"handoff"} and soft_installation):
        return False
    if not soft_installation and not (tag_set & intent_group("rag_soft_reference")):
        return False
    hits = [item for item in rag_evidence.get("hits", []) or [] if isinstance(item, dict)]
    if not hits:
        return False
    return any(rag_hit_can_support_soft_reference(item) for item in hits)


def rag_hit_can_support_soft_reference(hit: dict[str, Any]) -> bool:
    if hit.get("risk_terms"):
        return False
    try:
        score = float(hit.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score < RAG_SOFT_REFERENCE_MIN_SCORE:
        return False
    category = str(hit.get("category") or "")
    source_type = str(hit.get("source_type") or "")
    return category in rag_terms("soft_reference_categories") or source_type in rag_terms("soft_reference_source_types")


def is_soft_installation_reference(text: str) -> bool:
    normalized = normalize_text(text)
    if "安装" not in normalized:
        return False
    hard_terms = ["安装费", "费用", "多少钱", "报价", "上门", "预约", "师傅", "现场", "城市", "时间", "几天"]
    if any(term in normalized for term in hard_terms):
        return False
    soft_terms = ["安装前", "注意事项", "确认", "供电", "门厚", "开孔", "方向", "要不要", "需要看"]
    return any(term in normalized for term in soft_terms)


def should_use_rag(intent_tags: list[str], evidence: dict[str, Any], *, text: str = "") -> bool:
    tag_set = set(intent_tags)
    if is_soft_installation_reference(text):
        return True
    has_business_evidence = any(evidence.get(key) for key in ("products", "faq", "policies", "product_scoped"))
    if not has_business_evidence:
        return True
    if tag_set & {"scene_product", "spec", "warranty", "small_talk", "unknown", "catalog"}:
        return True
    return False


def legacy_selected_items(category_pack: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for item in category_pack.get("evidence_items", []) or []:
        category_id = str(item.get("category_id") or "")
        item_id = str(item.get("item_id") or "")
        selected.append(
            {
                "id": f"{category_id}:{item_id}",
                "path": f"knowledge_bases/{category_id}/items/{item_id}.json",
                "matched_tags": category_pack.get("intent_tags", []),
                "summary": str(item.get("title") or ""),
            }
        )
    return selected


def legacy_product_snippet(item: dict[str, Any], evidence_item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    snippet: dict[str, Any] = {
        "id": item.get("id"),
        "name": data.get("name"),
        "category": data.get("category"),
        "price": data.get("price"),
        "unit": data.get("unit"),
        "stock": data.get("inventory"),
        "shipping": data.get("shipping_policy"),
        "warranty": data.get("warranty_policy"),
        "spec": data.get("specs"),
        "discount_policy": (data.get("reply_templates") or {}).get("discount_policy"),
        "discount_tiers": data.get("price_tiers", []) or [],
        "matched_aliases": list(evidence_item.get("matched_fields", []) or []),
        "match_reason": evidence_item.get("match_reason"),
        "match_confidence": evidence_item.get("confidence"),
    }
    return {key: value for key, value in snippet.items() if value not in (None, "", [], {})}


def suppress_weak_product_evidence_without_need(evidence: dict[str, Any], text: str) -> None:
    """Drop weak product-category hits when the customer did not ask for a product.

    The Brain should reason over authoritative product evidence, but broad
    category words like "包装" can appear in non-purchase questions.  Without a
    purchase/detail signal, those weak hits should not clear the
    no-relevant-business-evidence safety boundary.
    """

    products = [item for item in evidence.get("products", []) or [] if isinstance(item, dict)]
    if not products or text_has_product_need_signal(text):
        return
    strong_products = [item for item in products if not product_hit_is_weak_category_only(item)]
    evidence["products"] = strong_products


def product_hit_is_weak_category_only(item: dict[str, Any]) -> bool:
    matched_fields = {str(value).lower() for value in item.get("matched_aliases", []) or [] if str(value)}
    if not matched_fields:
        return False
    return matched_fields <= {"aliases", "category", "keywords", "tags", "title"}


def text_has_product_need_signal(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    need_terms = (
        "买",
        "采购",
        "订",
        "要",
        "需要",
        "想找",
        "找个",
        "看看",
        "看下",
        "推荐",
        "报价",
        "价格",
        "多少钱",
        "库存",
        "现货",
        "在售",
        "有没有",
        "有吗",
        "合适",
        "能放",
        "预算",
        "规格",
        "尺寸",
        "型号",
        "参数",
        "质保",
        "保修",
        "送货",
        "安装",
    )
    return any(term in normalized for term in need_terms)


def legacy_policy_faq(item: dict[str, Any], evidence_item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    runtime = item.get("runtime", {}) or {}
    data_has_auto_reply = "allow_auto_reply" in data
    data_has_handoff = "requires_handoff" in data
    explicit_auto_reply = data.get("allow_auto_reply") if data_has_auto_reply else runtime.get("allow_auto_reply", True)
    explicit_requires_handoff = data.get("requires_handoff") if data_has_handoff else runtime.get("requires_handoff", False)
    runtime_allows_auto_reply = runtime.get("allow_auto_reply", True) is not False or data_has_auto_reply
    auto_reply_allowed = bool(explicit_auto_reply is not False and runtime_allows_auto_reply)
    needs_handoff = bool(explicit_requires_handoff or evidence_item.get("requires_handoff", False) or not auto_reply_allowed)
    return {
        "intent": str(item.get("id") or data.get("policy_type") or ""),
        "policy_type": str(data.get("policy_type") or ""),
        "matched_fields": list(evidence_item.get("matched_fields", []) or []),
        "priority": 100 if needs_handoff else 50,
        "matched_keywords": list(data.get("keywords", []) or []),
        "answer": data.get("answer", ""),
        "needs_handoff": needs_handoff,
        "auto_reply_allowed": auto_reply_allowed,
        "operator_alert": bool(data.get("operator_alert", False) or runtime.get("operator_alert", False) or evidence_item.get("requires_handoff", False) or not auto_reply_allowed),
        "reason": data.get("handoff_reason") or ("auto_reply_disabled" if not auto_reply_allowed else "") or evidence_item.get("match_reason") or "",
    }


def legacy_policy_key(data: dict[str, Any], item_id: str) -> str:
    if item_id.endswith("_details"):
        return item_id.removesuffix("_details")
    policy_type = str(data.get("policy_type") or "")
    mapping = {
        "catalog": "catalog_policy",
        "product_catalog": "catalog_policy",
        "company": "company_profile",
        "invoice": "invoice_policy",
        "payment": "payment_policy",
        "logistics": "logistics_policy",
        "after_sales": "after_sales_policy",
    }
    return mapping.get(policy_type, "")


def legacy_policy_payload(data: dict[str, Any]) -> Any:
    answer = data.get("answer", "")
    if isinstance(answer, str) and answer.strip().startswith("{"):
        try:
            return json.loads(answer)
        except json.JSONDecodeError:
            return answer
    return answer


def legacy_style_example(item: dict[str, Any], evidence_item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    return {
        "id": item.get("id"),
        "customer_message": data.get("customer_message", ""),
        "service_reply": data.get("service_reply") or data.get("guideline_text") or "",
        "intent_tags": data.get("intent_tags", []) or [],
        "tone_tags": data.get("tone_tags", []) or [],
        "matched_fields": evidence_item.get("matched_fields", []) or [],
    }


def legacy_shared_risk_control_faq(item: dict[str, Any], evidence_item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    runtime = item.get("runtime", {}) or {}
    data_has_auto_reply = "allow_auto_reply" in data
    data_has_handoff = "requires_handoff" in data
    explicit_auto_reply = data.get("allow_auto_reply") if data_has_auto_reply else runtime.get("allow_auto_reply", True)
    explicit_requires_handoff = data.get("requires_handoff") if data_has_handoff else runtime.get("requires_handoff", False)
    runtime_requires_handoff = bool(runtime.get("requires_handoff", False)) and not data_has_handoff
    risk_level = str(runtime.get("risk_level") or data.get("risk_level") or "normal").strip().lower()
    auto_reply_allowed = bool(explicit_auto_reply is not False and runtime.get("allow_auto_reply", True) is not False)
    needs_handoff = bool(
        explicit_requires_handoff
        or runtime_requires_handoff
        or evidence_item.get("requires_handoff", False)
        or risk_level == "high"
        or explicit_auto_reply is False
    )
    return {
        "intent": str(item.get("id") or data.get("title") or "shared_risk_control"),
        "priority": 100 if needs_handoff else 30,
        "matched_keywords": list(data.get("keywords", []) or []),
        "answer": data.get("guideline_text") or data.get("answer") or data.get("content") or "",
        "needs_handoff": needs_handoff,
        "auto_reply_allowed": auto_reply_allowed and not needs_handoff,
        "operator_alert": bool(data.get("operator_alert", False) or runtime.get("operator_alert", False) or needs_handoff),
        "reason": data.get("handoff_reason") or evidence_item.get("handoff_reason") or ("shared_risk_control" if needs_handoff else ""),
        "advisory_only": not needs_handoff,
    }


def legacy_product_scoped_item(item: dict[str, Any], evidence_item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", {}) or {}
    return {
        "id": item.get("id"),
        "category_id": item.get("category_id") or evidence_item.get("category_id"),
        "product_id": data.get("product_id"),
        "title": data.get("title") or item.get("id"),
        "keywords": data.get("keywords", []) or [],
        "answer": data.get("answer") or data.get("content") or "",
        "needs_handoff": bool(data.get("requires_handoff", False) or evidence_item.get("requires_handoff", False)),
        "auto_reply_allowed": bool(evidence_item.get("allow_auto_reply", True)),
        "operator_alert": bool(data.get("operator_alert", False) or evidence_item.get("requires_handoff", False)),
        "reason": data.get("handoff_reason") or evidence_item.get("handoff_reason") or evidence_item.get("match_reason") or "",
        "matched_fields": evidence_item.get("matched_fields", []) or [],
    }


def legacy_product_scoped_faq(scoped: dict[str, Any]) -> dict[str, Any]:
    needs_handoff = bool(scoped.get("needs_handoff") or scoped.get("auto_reply_allowed") is False)
    return {
        "intent": str(scoped.get("id") or ""),
        "priority": 100 if needs_handoff else 70,
        "matched_keywords": list(scoped.get("keywords", []) or []),
        "answer": scoped.get("answer", ""),
        "needs_handoff": needs_handoff,
        "auto_reply_allowed": bool(scoped.get("auto_reply_allowed", True)),
        "operator_alert": bool(scoped.get("operator_alert", False) or needs_handoff),
        "reason": scoped.get("reason", ""),
    }


def dedupe_legacy_items(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        value = item.get(key)
        if value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def select_manifest_items(manifest: dict[str, Any], intent_tags: list[str]) -> list[dict[str, Any]]:
    tag_set = set(intent_tags)
    selected = []
    for item in manifest.get("items", []) or []:
        item_tags = set(item.get("intent_tags", []) or [])
        if tag_set & item_tags:
            selected.append(item)
    return selected


def resolve_item_path(item: dict[str, Any]) -> Path:
    item_path = STRUCTURED_DATA_ROOT / str(item.get("path") or "")
    if not item_path.exists():
        raise FileNotFoundError(str(item_path))
    return item_path


def merge_product_knowledge_evidence(
    evidence: dict[str, Any],
    knowledge: dict[str, Any],
    text: str,
    intent_tags: list[str],
    *,
    context: dict[str, Any],
) -> None:
    normalized = normalize_text(text)
    products = select_products(knowledge, normalized, intent_tags, context)
    evidence["products"] = products
    evidence["faq"] = select_faq(knowledge, normalized, intent_tags)
    evidence["policies"] = select_policies(knowledge, intent_tags)


def select_products(
    knowledge: dict[str, Any],
    normalized_text: str,
    intent_tags: list[str],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    products = knowledge.get("products", []) or []
    matched = []
    for product in products:
        aliases = [str(product.get("name") or ""), *[str(value) for value in product.get("aliases", []) or []]]
        matched_aliases = collect_matched_aliases(aliases, normalized_text)
        if matched_aliases:
            matched.append(product_snippet(product, matched_aliases=matched_aliases))

    if matched:
        return matched[:3]

    llm_match = maybe_llm_match_product(
        products=products,
        normalized_text=normalized_text,
        intent_tags=intent_tags,
        context=context,
    )
    if llm_match:
        return llm_match

    last_product_id = str(context.get("last_product_id") or "")
    if last_product_id and set(intent_tags) & {"quote", "discount", "shipping", "stock", "warranty", "spec"}:
        for product in products:
            if str(product.get("id") or "") == last_product_id:
                return [product_snippet(product, matched_aliases=["conversation_context"])]

    if "catalog" in intent_tags:
        return [product_snippet(product, compact=True) for product in products[:8]]

    return []


def product_snippet(
    product: dict[str, Any],
    *,
    matched_aliases: list[str] | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    keys = ["id", "name", "category", "price", "unit", "stock", "lead_time", "shipping", "warranty", "spec", "discount_policy"]
    if compact:
        keys = ["id", "name", "category", "price", "unit", "spec"]
    snippet = {key: product.get(key) for key in keys if key in product}
    if not compact and product.get("discount_tiers"):
        snippet["discount_tiers"] = product.get("discount_tiers")
    if matched_aliases:
        snippet["matched_aliases"] = matched_aliases
    return snippet


def maybe_llm_match_product(
    *,
    products: list[dict[str, Any]],
    normalized_text: str,
    intent_tags: list[str],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = normalize_product_entity_resolution_settings(context.get("product_entity_resolution"))
    if not settings.get("enabled", False):
        return []
    if not should_try_llm_product_resolution(normalized_text, intent_tags, products):
        return []
    result = llm_match_product_name(normalized_text, products, settings=settings)
    if not result.get("matched"):
        return []
    product_id = str(result.get("product_id") or "")
    if not product_id:
        return []
    for product in products:
        if str(product.get("id") or "") != product_id:
            continue
        snippet = product_snippet(product, matched_aliases=["semantic_llm_match"])
        confidence = result.get("confidence")
        if confidence is not None:
            snippet["semantic_match_confidence"] = confidence
        snippet["semantic_match_reason"] = str(result.get("reason") or "llm_semantic_product_match")
        return [snippet]
    return []


def should_try_llm_product_resolution(
    normalized_text: str,
    intent_tags: list[str],
    products: list[dict[str, Any]],
) -> bool:
    if not products:
        return False
    if len(normalize_text(normalized_text)) < 2:
        return False
    tag_set = {str(item) for item in intent_tags if str(item)}
    relevant_tags = {"product", "catalog", "quote", "discount", "stock", "shipping", "warranty", "spec", "scene_product"}
    return bool(tag_set & relevant_tags)


def normalize_product_entity_resolution_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = dict(raw)
    normalized["enabled"] = bool(normalized.get("enabled", False))
    return normalized


def select_faq(knowledge: dict[str, Any], normalized_text: str, intent_tags: list[str]) -> list[dict[str, Any]]:
    selected = []
    tag_set = set(intent_tags)
    for faq in knowledge.get("faq", []) or []:
        keywords = [str(value) for value in faq.get("keywords", []) or []]
        matched_keywords = [keyword for keyword in keywords if keyword and keyword.lower() in normalized_text]
        intent = str(faq.get("intent") or "")
        if matched_keywords or faq_matches_tags(intent, tag_set):
            selected.append(
                {
                    "intent": intent,
                    "priority": faq.get("priority", 0),
                    "matched_keywords": matched_keywords,
                    "answer": faq.get("answer", ""),
                    "needs_handoff": bool(faq.get("needs_handoff", False)),
                    "auto_reply_allowed": bool(faq.get("auto_reply_allowed", True)),
                    "operator_alert": bool(faq.get("operator_alert", False)),
                    "reason": faq.get("reason", ""),
                }
            )
    return sorted(selected, key=lambda item: int(item.get("priority", 0) or 0), reverse=True)[:5]


def faq_matches_tags(intent: str, tag_set: set[str]) -> bool:
    if intent in tag_set:
        return True
    mapped = string_map("policy_type_to_intent")
    return mapped.get(intent) in tag_set


def select_policies(knowledge: dict[str, Any], intent_tags: list[str]) -> dict[str, Any]:
    policies = {}
    for tag, key in string_map("policy_tags").items():
        if tag in intent_tags and knowledge.get(key):
            policies[key] = knowledge[key]
    return policies


def merge_style_evidence(evidence: dict[str, Any], style_data: dict[str, Any], intent_tags: list[str]) -> None:
    tag_set = set(intent_tags)
    examples = []
    for example in style_data.get("examples", []) or []:
        example_tags = set(example.get("intent_tags", []) or [])
        if tag_set & example_tags:
            examples.append(example)
    evidence["style_examples"] = examples[:4]


def build_safety_summary(intent_tags: list[str], evidence: dict[str, Any], text: str) -> dict[str, Any]:
    tag_set = set(intent_tags)
    # Privacy minimum collection is a guideline, not a handoff trigger, when the customer is providing data.
    faq_requires_handoff = any(
        item.get("needs_handoff")
        for item in evidence.get("faq", []) or []
        if not (
            item.get("intent") == "shared_global_privacy_minimum_collection" and "customer_data" in tag_set
        )
    )
    discount_check = evaluate_discount_request(text, evidence)
    must_handoff = bool(tag_set & {"handoff"} or faq_requires_handoff)
    has_business_evidence = has_authoritative_business_evidence(intent_tags, evidence)
    reasons = []
    if "handoff" in tag_set:
        reasons.append("handoff_intent_detected")
    if faq_requires_handoff:
        reasons.append("matched_faq_requires_handoff")
    if discount_check.get("needs_handoff"):
        must_handoff = True
        reasons.append(str(discount_check.get("reason") or "discount_requires_approval"))
    has_chat_evidence = bool(evidence.get("style_examples"))
    style_only = bool(tag_set & {"small_talk", "greeting"}) and not (tag_set - {"small_talk", "greeting"})
    customer_data_only = "customer_data" in tag_set and not (tag_set - {"customer_data"})
    if not has_business_evidence and not style_only and not customer_data_only:
        if "unknown" in tag_set or not has_chat_evidence or (tag_set & evidence_required_intents()):
            reasons.append("no_relevant_business_evidence")
            must_handoff = True
    elif "unknown" in tag_set and not has_business_evidence:
        reasons.append("no_relevant_business_evidence")
        must_handoff = True
    return {
        "must_handoff": must_handoff,
        "reasons": reasons,
        "allowed_auto_reply": not must_handoff,
        "discount_check": discount_check,
    }


def evidence_required_intents() -> set[str]:
    return set(intent_group("business")) | {"product", "scene_product", "spec", "warranty"}


def normalize_pre_purchase_vehicle_tags(intent_tags: list[str], text: str) -> list[str]:
    tags = {str(item) for item in intent_tags if str(item)}
    if is_pre_purchase_vehicle_maintenance_guidance(text):
        tags.discard("after_sales")
        tags.add("scene_product")
        tags.add("product")
    return sorted(tags)


def is_pre_purchase_vehicle_maintenance_guidance(text: str) -> bool:
    normalized = normalize_text(text)
    if "维修" not in normalized and "维护" not in normalized:
        return False
    hard_after_sales = [
        "售后",
        "质保",
        "保修",
        "退换",
        "退款",
        "赔偿",
        "买完",
        "坏了",
        "出问题",
        "负责",
        "保修期",
    ]
    if any(term in normalized for term in hard_after_sales):
        return False
    maintenance_context = ["后期维修", "维修成本", "维修麻烦", "维修不贵", "维护成本", "少维修", "小毛病", "后期省心"]
    selection_context = [
        "怎么选",
        "哪一类",
        "哪类",
        "哪种",
        "哪个",
        "建议",
        "先看",
        "适合",
        "预算",
        "通勤",
        "代步",
        "家用",
        "二手车",
        "车型",
        "车况",
        "保值",
        "不想太贵",
    ]
    vehicle_context = [
        "车",
        "车型",
        "二手",
        "电车",
        "新能源",
        "纯电",
        "混动",
        "轿车",
        "suv",
        "mpv",
        "里面选",
        "选哪",
        "选哪个",
        "哪一类",
        "哪种",
    ]
    return (
        any(term in normalized for term in maintenance_context)
        and any(term in normalized for term in selection_context)
        and any(term in normalized for term in vehicle_context)
    )


def is_soft_pre_purchase_category_safety(category_safety: dict[str, Any], text: str) -> bool:
    if not is_pre_purchase_vehicle_maintenance_guidance(text):
        return False
    reasons = {str(item) for item in (category_safety or {}).get("reasons", []) or [] if str(item)}
    return bool(reasons) and reasons <= {"no_relevant_business_evidence"}


def has_authoritative_business_evidence(intent_tags: list[str], evidence: dict[str, Any]) -> bool:
    tag_set = set(intent_tags)
    if evidence.get("products") or evidence.get("product_scoped"):
        return True

    for policy_key in (evidence.get("policies", {}) or {}).keys():
        if policy_key_matches_intent(str(policy_key), tag_set):
            return True

    for faq in evidence.get("faq", []) or []:
        if not isinstance(faq, dict):
            continue
        if faq.get("needs_handoff") or faq.get("auto_reply_allowed") is False:
            return True
        matched_fields = {str(value).lower() for value in faq.get("matched_fields", []) or []}
        if matched_fields & {"keywords", "title", "question", "customer_message"}:
            return True
        if policy_type_matches_intent(str(faq.get("policy_type") or ""), tag_set):
            return True
        if policy_key_matches_intent(str(faq.get("intent") or ""), tag_set):
            return True
        if faq_matches_tags(str(faq.get("intent") or ""), tag_set):
            return True
    return False


def policy_type_matches_intent(policy_type: str, tag_set: set[str]) -> bool:
    type_tags = {key: set(values) for key, values in map_of_lists("policy_type_tags").items()}
    tags = type_tags.get(policy_type.strip().lower())
    return bool(tags and tags & tag_set)


def policy_key_matches_intent(policy_key: str, tag_set: set[str]) -> bool:
    key = policy_key.strip().lower()
    if not key:
        return False
    key_tags = {key: set(values) for key, values in map_of_lists("policy_key_tags").items()}
    if key in key_tags:
        return bool(key_tags[key] & tag_set)
    if any(fragment in key for fragment in ("company", "profile", "address")):
        return "company" in tag_set
    if any(fragment in key for fragment in ("invoice", "tax", "fapiao")):
        return "invoice" in tag_set
    if any(fragment in key for fragment in ("payment", "bank", "account")):
        return "payment" in tag_set
    if any(fragment in key for fragment in ("logistics", "shipping", "delivery", "free_shipping")):
        return bool({"shipping", "discount"} & tag_set)
    if any(fragment in key for fragment in ("after_sales", "warranty", "refund")):
        return bool({"after_sales", "warranty"} & tag_set)
    if any(fragment in key for fragment in ("discount", "promo", "coupon")):
        return bool({"discount", "shipping"} & tag_set)
    if any(fragment in key for fragment in ("contract", "manual", "handoff")):
        return "handoff" in tag_set
    return False


def sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "last_product_id",
        "last_product_name",
        "last_quantity",
        "last_shipping_city",
        "last_unit_price",
        "last_total",
        "last_product_price",
        "last_product_source",
    }
    return {key: context.get(key) for key in allowed if context.get(key) not in (None, "")}


def normalize_text(text: str) -> str:
    return text.lower().strip()


def evaluate_discount_request(text: str, evidence: dict[str, Any]) -> dict[str, Any]:
    requested_unit_price = extract_requested_unit_price(text)
    quantity = extract_quantity(text)
    if requested_unit_price is None:
        return {"detected": False}

    products = evidence.get("products", []) or []
    if not products:
        return {
            "detected": True,
            "requested_unit_price": requested_unit_price,
            "quantity": quantity,
            "needs_handoff": False,
            "reason": "discount_request_without_matched_product",
        }

    product = products[0]
    eligible_price = eligible_unit_price(product, quantity)
    if eligible_price is None:
        return {
            "detected": True,
            "product_id": product.get("id"),
            "requested_unit_price": requested_unit_price,
            "quantity": quantity,
            "needs_handoff": True,
            "reason": "discount_request_without_public_price_basis",
        }
    if requested_unit_price < eligible_price:
        return {
            "detected": True,
            "product_id": product.get("id"),
            "requested_unit_price": requested_unit_price,
            "quantity": quantity,
            "eligible_unit_price": eligible_price,
            "needs_handoff": True,
            "reason": "requested_price_below_public_tier",
        }
    return {
        "detected": True,
        "product_id": product.get("id"),
        "requested_unit_price": requested_unit_price,
        "quantity": quantity,
        "eligible_unit_price": eligible_price,
        "needs_handoff": False,
        "reason": "requested_price_within_public_tier",
    }


def eligible_unit_price(product: dict[str, Any], quantity: int | None) -> float | None:
    base_price = product.get("price")
    try:
        eligible = float(base_price)
    except (TypeError, ValueError):
        return None
    if quantity is None:
        return eligible
    tiers = []
    for tier in product.get("discount_tiers", []) or []:
        try:
            tiers.append((int(tier.get("min_quantity")), float(tier.get("unit_price"))))
        except (TypeError, ValueError):
            continue
    for min_quantity, unit_price in sorted(tiers):
        if quantity >= min_quantity:
            eligible = unit_price
    return eligible


def extract_quantity(text: str) -> int | None:
    match = re.search(rf"(\d+)\s*({quantity_unit_pattern()})", text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def extract_requested_unit_price(text: str) -> float | None:
    patterns = [
        r"按\s*(\d+(?:\.\d+)?)\s*(?:元|块|块钱)?",
        rf"每\s*(?:{quantity_unit_pattern()})\s*(\d+(?:\.\d+)?)\s*(?:元|块|块钱)?",
        rf"(\d+(?:\.\d+)?)\s*(?:元|块|块钱)\s*(?:每|/)?\s*(?:{quantity_unit_pattern()})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None
