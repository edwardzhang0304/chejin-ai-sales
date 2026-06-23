"""Local product/FAQ knowledge for WeChat customer-service tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.platform_understanding_rules import (
    product_keywords,
    quantity_unit_pattern,
)
from apps.wechat_ai_customer_service.workflows.product_name_matcher import (
    collect_matched_aliases,
)
from apps.wechat_ai_customer_service.workflows.llm_product_name_matcher import (
    llm_match_product_name,
)


def load_product_knowledge(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if knowledge_has_entries(payload):
        return payload

    # Compatibility: when compiled snapshot is empty, fall back to the
    # structured example so offline checks remain deterministic.
    fallback_paths = candidate_product_knowledge_fallback_paths(path)
    for fallback in fallback_paths:
        if not fallback.exists():
            continue
        try:
            fallback_payload = json.loads(fallback.read_text(encoding="utf-8"))
        except Exception:
            continue
        if knowledge_has_entries(fallback_payload):
            return fallback_payload
    return payload


def knowledge_has_entries(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    products = payload.get("products")
    faq = payload.get("faq")
    return bool(products) or bool(faq)


def candidate_product_knowledge_fallback_paths(path: Path) -> list[Path]:
    paths: list[Path] = []
    normalized_parts = [part.lower() for part in path.parts]
    if "compiled" in normalized_parts and "structured_compat" in normalized_parts:
        try:
            base = path.parents[2]
        except IndexError:
            base = path.parent
        paths.append(base / "structured" / path.name)
    paths.append(path.with_name(path.stem + ".fallback.json"))
    deduped: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        key = str(item.resolve()) if item.exists() else str(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def decide_product_knowledge_reply(
    text: str,
    knowledge: dict[str, Any],
    context: dict[str, Any] | None = None,
    matching_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    matching_settings = normalize_matching_settings(matching_settings)
    normalized = normalize_text(text)
    faq = match_faq(normalized, knowledge)
    intent = detect_intent(normalized)

    if has_any(normalized, product_keywords("catalog")):
        return build_catalog_result(knowledge)

    product = match_product(normalized, knowledge)
    product_match_reason = "product_alias_matched"
    product_match_confidence: float | None = None
    if not product:
        llm_fallback = try_llm_product_match(
            normalized_text=normalized,
            knowledge=knowledge,
            intent=intent,
            matching_settings=matching_settings,
        )
        if llm_fallback:
            product = llm_fallback.get("product")
            product_match_reason = str(llm_fallback.get("reason") or "llm_semantic_product_match")
            confidence = llm_fallback.get("confidence")
            if confidence not in (None, ""):
                try:
                    product_match_confidence = float(confidence)
                except (TypeError, ValueError):
                    product_match_confidence = None
    if faq and int(faq.get("priority", 0) or 0) >= 80 and should_high_priority_faq_preempt_product(faq, product):
        return build_faq_result(faq)
    context_used = False
    if not product and should_use_context_product(intent, faq):
        product = find_product_by_id(knowledge, str(context.get("last_product_id") or ""))
        context_used = bool(product)

    if faq and should_prioritize_faq(faq, product, intent):
        return build_faq_result(faq)

    if product:
        reply = build_product_reply(product, intent, text, context=context if context_used else None)
        payload = {
            "enabled": True,
            "matched": True,
            "match_type": "product",
            "intent": intent,
            "product_id": product.get("id"),
            "product_name": product.get("name"),
            "product_unit": product.get("unit"),
            "context_used": context_used,
            "quantity": reply.get("quantity"),
            "unit_price": reply.get("unit_price"),
            "total": reply.get("total"),
            "shipping_city": reply.get("shipping_city"),
            "reply_text": reply.get("reply_text"),
            "needs_handoff": bool(reply.get("needs_handoff")),
            "operator_alert": bool(reply.get("operator_alert")),
            "approval_reason": reply.get("approval_reason"),
            "reason": str(reply.get("reason") or product_match_reason),
        }
        if product_match_confidence is not None:
            payload["semantic_match_confidence"] = round(product_match_confidence, 3)
        return payload

    if faq:
        return build_faq_result(faq)

    return {
        "enabled": True,
        "matched": False,
        "intent": intent,
        "reason": "no_product_or_faq_match",
    }


def normalize_text(text: str) -> str:
    replacements = {
        "：": ":",
        "，": ",",
        "。": ".",
        "；": ";",
        "\r\n": "\n",
        "\r": "\n",
    }
    normalized = text.lower()
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized.strip()


def match_product(normalized_text: str, knowledge: dict[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for product in knowledge.get("products", []) or []:
        aliases = [str(product.get("name") or ""), *[str(item) for item in product.get("aliases", []) or []]]
        matched_aliases = collect_matched_aliases(aliases, normalized_text)
        if not matched_aliases:
            continue
        candidates.append((max(len(alias) for alias in matched_aliases), len(matched_aliases), product))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def try_llm_product_match(
    *,
    normalized_text: str,
    knowledge: dict[str, Any],
    intent: str,
    matching_settings: dict[str, Any],
) -> dict[str, Any] | None:
    if not matching_settings.get("enabled", False):
        return None
    if not should_try_llm_product_resolution(normalized_text, intent):
        return None
    products = knowledge.get("products", []) or []
    if not products:
        return None
    llm_result = llm_match_product_name(normalized_text, products, settings=matching_settings)
    if not llm_result.get("matched"):
        return None
    product_id = str(llm_result.get("product_id") or "")
    if not product_id:
        return None
    product = find_product_by_id(knowledge, product_id)
    if not product:
        return None
    return {
        "product": product,
        "reason": str(llm_result.get("reason") or "llm_semantic_product_match"),
        "confidence": llm_result.get("confidence"),
    }


def should_try_llm_product_resolution(normalized_text: str, intent: str) -> bool:
    if len(normalize_text(normalized_text)) < 2:
        return False
    return intent in {"catalog", "quote", "discount", "shipping", "stock", "warranty", "spec", "product_info"}


def normalize_matching_settings(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = dict(value)
    normalized["enabled"] = bool(normalized.get("enabled", False))
    return normalized


def find_product_by_id(knowledge: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    if not product_id:
        return None
    for product in knowledge.get("products", []) or []:
        if str(product.get("id") or "") == product_id:
            return product
    return None


def should_use_context_product(intent: str, faq: dict[str, Any] | None) -> bool:
    if faq and int(faq.get("priority", 0) or 0) >= 80:
        return False
    return intent in {"quote", "discount", "shipping", "stock", "warranty", "spec"}


def match_faq(normalized_text: str, knowledge: dict[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for faq in knowledge.get("faq", []) or []:
        keywords = [str(item) for item in faq.get("keywords", []) or []]
        matched = [keyword for keyword in keywords if keyword and keyword.lower() in normalized_text]
        if matched:
            priority = int(faq.get("priority", 0) or 0)
            candidates.append((priority, len(matched), max(len(item) for item in matched), faq))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def should_prioritize_faq(faq: dict[str, Any], product: dict[str, Any] | None, intent: str) -> bool:
    if not product:
        return True
    if str(faq.get("intent") or "") in {"selection_help", "service_reliability"}:
        return False
    if int(faq.get("priority", 0) or 0) >= 80:
        return True
    product_intents = {"quote", "discount", "shipping", "stock", "warranty", "spec", "product_info"}
    return intent not in product_intents


def should_high_priority_faq_preempt_product(faq: dict[str, Any], product: dict[str, Any] | None) -> bool:
    if not product:
        return True
    return str(faq.get("intent") or "") not in {"selection_help", "service_reliability"}


FAQ_INTENT_ALIASES = {
    "special_invoice_rule": "invoice",
}


def canonical_faq_intent(faq: dict[str, Any]) -> str:
    raw_intent = str(faq.get("intent") or "faq").strip() or "faq"
    return FAQ_INTENT_ALIASES.get(raw_intent, raw_intent)


def build_faq_result(faq: dict[str, Any]) -> dict[str, Any]:
    raw_intent = str(faq.get("intent") or "faq").strip() or "faq"
    intent = canonical_faq_intent(faq)
    payload = {
        "enabled": True,
        "matched": True,
        "match_type": "faq",
        "intent": intent,
        "faq_keywords": faq.get("keywords", []),
        "reply_text": str(faq.get("answer") or ""),
        "needs_handoff": bool(faq.get("needs_handoff", False)),
        "auto_reply_allowed": bool(faq.get("auto_reply_allowed", True)),
        "operator_alert": bool(faq.get("operator_alert", False)),
        "reason": str(faq.get("reason") or "faq_keyword_matched"),
    }
    if raw_intent != intent:
        payload["faq_source_intent"] = raw_intent
    return payload


def build_catalog_result(knowledge: dict[str, Any]) -> dict[str, Any]:
    products = knowledge.get("products", []) or []
    preferred_ids = [
        "commercial_fridge_bx_200",
        "office_chair_oc_300",
        "water_filter_core",
        "ap-88",
    ]
    product_by_id = {str(product.get("id") or ""): product for product in products}
    selected_products: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for product_id in preferred_ids:
        product = product_by_id.get(product_id)
        if product:
            selected_products.append(product)
            seen_ids.add(str(product.get("id") or ""))
    for product in products:
        product_id = str(product.get("id") or "")
        if product_id in seen_ids:
            continue
        selected_products.append(product)
        seen_ids.add(product_id)
        if len(selected_products) >= 4:
            break

    items = []
    for product in selected_products[:4]:
        name = str(product.get("name") or "")
        price = product.get("price")
        unit = str(product.get("unit") or "件")
        category = str(product.get("category") or "")
        brief = f"{name}"
        if category:
            brief += f"（{category}）"
        if price not in (None, ""):
            brief += f"：{price} 元/{unit}"
        items.append(brief)
    remaining = len(products) - len(items)
    more_text = f"另外还有 {remaining} 类商品可选。" if remaining > 0 else ""
    reply = "目前测试商品主要有：" + "；".join(items) + f"。{more_text}您告诉我具体用途、数量和收货城市，我帮您推荐并核算报价。"
    return {
        "enabled": True,
        "matched": True,
        "match_type": "catalog",
        "intent": "catalog",
        "reply_text": reply,
        "needs_handoff": False,
        "operator_alert": False,
        "reason": "catalog_keyword_matched",
    }


def detect_intent(normalized_text: str) -> str:
    if (
        has_any(normalized_text, product_keywords("approval"))
        or re.search(rf"按\s*\d+\s*(?:{quantity_unit_pattern()})?\s*的?价", normalized_text)
        or extract_requested_unit_price(normalized_text) is not None
    ):
        return "discount"
    if has_any(normalized_text, product_keywords("discount")):
        return "discount"
    if has_any(normalized_text, product_keywords("quote")):
        return "quote"
    if has_any(normalized_text, product_keywords("warranty")):
        return "warranty"
    if has_any(normalized_text, product_keywords("stock")):
        return "stock"
    if has_any(normalized_text, product_keywords("shipping")):
        return "shipping"
    if has_any(normalized_text, product_keywords("spec")):
        return "spec"
    if re.search(rf"\d+\s*({quantity_unit_pattern()})", normalized_text, re.IGNORECASE):
        return "quote"
    return "product_info"


def build_product_reply(
    product: dict[str, Any],
    intent: str,
    original_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    name = str(product.get("name") or "该产品")
    unit = str(product.get("unit") or "件")
    price = product.get("price")
    quantity = extract_quantity(original_text)
    if quantity is None:
        quantity = safe_float(context.get("last_quantity"))
    shipping_city = extract_shipping_city(original_text) or str(context.get("last_shipping_city") or "")
    quote_unit_price = best_unit_price_for_quantity(product, quantity)
    total = None
    if quote_unit_price is not None and quantity:
        try:
            total = float(quote_unit_price) * quantity
        except (TypeError, ValueError):
            total = None

    approval = detect_approval_required(product, intent, original_text, quantity)
    if approval.get("required"):
        return {
            "reply_text": (
                f"{name} 当前可执行的公开阶梯价是：{format_discount_policy(product)}。"
                "您提到的价格/优惠超出了我能直接确认的范围，我把情况记下，按正式流程核准后再回复您。"
            ),
            "needs_handoff": True,
            "operator_alert": True,
            "approval_reason": approval.get("reason"),
            "reason": "approval_required",
            "quantity": quantity,
            "unit_price": quote_unit_price,
            "total": total,
            "shipping_city": shipping_city,
        }

    if intent == "discount":
        if quantity:
            unit_price = best_unit_price_for_quantity(product, quantity)
            if unit_price is not None:
                total_text = format_money(float(unit_price) * quantity)
                return {
                    "reply_text": (
                        f"{name} 按当前规则，{quantity:g}{unit} 可按 {format_money(float(unit_price))} 元/{unit} 核算，"
                        f"预估小计 {total_text} 元。公开优惠规则：{format_discount_policy(product)}。"
                    ),
                    "needs_handoff": False,
                    "operator_alert": False,
                    "reason": "discount_policy_matched",
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total": float(unit_price) * quantity,
                    "shipping_city": shipping_city,
                }
        return {
            "reply_text": f"{name} 当前参考价 {price} 元/{unit}。优惠规则：{format_discount_policy(product)}。请确认采购数量和收货城市，我帮您核算总价。",
            "needs_handoff": False,
            "operator_alert": False,
            "reason": "discount_policy_matched",
            "quantity": quantity,
            "unit_price": quote_unit_price,
            "total": total,
            "shipping_city": shipping_city,
        }
    if intent == "shipping":
        lead_time = clean_policy_text(product.get("lead_time"))
        shipping = clean_policy_text(product.get("shipping"))
        if lead_time and shipping and lead_time in shipping:
            reply_text = f"{name} 发货/物流信息：{shipping}。"
        elif lead_time and shipping:
            reply_text = f"{name} 发货信息：{lead_time}；物流：{shipping}。"
        elif lead_time or shipping:
            reply_text = f"{name} 发货/物流信息：{lead_time or shipping}。"
        else:
            reply_text = f"{name} 的发货/物流信息当前没有明确记录，我问清楚后再回复您。"
        return {
            "reply_text": reply_text,
            "needs_handoff": False,
            "operator_alert": False,
            "reason": "product_alias_matched",
            "quantity": quantity,
            "unit_price": quote_unit_price,
            "total": total,
            "shipping_city": shipping_city,
        }
    if intent == "stock":
        min_order = min_order_phrase(product, unit)
        min_order_text = f"，{min_order}" if min_order else ""
        return {"reply_text": f"{name} 当前测试库存约 {product.get('stock')} {unit}{min_order_text}。", "needs_handoff": False, "operator_alert": False, "reason": "product_alias_matched"}
    if intent == "warranty":
        return {"reply_text": f"{name} 售后政策：{product.get('warranty')}。如需售后登记，请发订单信息和问题照片。", "needs_handoff": False, "operator_alert": False, "reason": "product_alias_matched"}
    if intent == "spec":
        return {"reply_text": f"{name} 规格参数：{product.get('spec')}。参考价 {price} 元/{unit}。", "needs_handoff": False, "operator_alert": False, "reason": "product_alias_matched"}
    if intent == "quote":
        if total is not None:
            total_text = format_money(total)
            return {
                "reply_text": build_quote_reply_text(
                    product=product,
                    name=name,
                    unit=unit,
                    quantity=quantity,
                    unit_price=float(quote_unit_price),
                    total_text=total_text,
                    shipping_city=shipping_city,
                ),
                "needs_handoff": False,
                "operator_alert": False,
                "reason": "product_alias_matched",
                "quantity": quantity,
                "unit_price": quote_unit_price,
                "total": total,
                "shipping_city": shipping_city,
            }
        return {
            "reply_text": build_quote_without_quantity_text(product, name, unit, price),
            "needs_handoff": False,
            "operator_alert": False,
            "reason": "product_alias_matched",
            "quantity": quantity,
            "unit_price": quote_unit_price,
            "total": total,
            "shipping_city": shipping_city,
        }
    return {
        "reply_text": f"{name}：参考价 {price} 元/{unit}，规格：{product.get('spec')}，库存约 {product.get('stock')} {unit}。",
        "needs_handoff": False,
        "operator_alert": False,
        "reason": "product_alias_matched",
        "quantity": quantity,
        "unit_price": quote_unit_price,
        "total": total,
        "shipping_city": shipping_city,
    }


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def extract_quantity(text: str) -> float | None:
    match = re.search(rf"(\d+(?:\.\d+)?)\s*({quantity_unit_pattern()})", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def extract_shipping_city(text: str) -> str:
    patterns = [
        r"(江苏南京|浙江杭州|浙江宁波|上海|北京|广州|深圳|苏州|无锡|常州|南京|杭州|宁波)",
        r"(?:发到|送到|寄到|发往|寄往|送往|发|送|寄|收货(?:地址)?(?:是)?|地址(?:是)?)[：:\s]*([\u4e00-\u9fff]{2,12})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        city = clean_shipping_city(match.group(1))
        if not city:
            continue
        return city[:12]
    return ""


def clean_shipping_city(value: str) -> str:
    city = value.strip("，,。.;； ?？")
    city = re.sub(r"^(到|往)", "", city)
    city = re.sub(r"(多少钱|运费|多久|几天|到货|包邮|吗|呢).*$", "", city)
    return city.strip("，,。.;； ?？")


def build_quote_reply_text(
    product: dict[str, Any],
    name: str,
    unit: str,
    quantity: float,
    unit_price: float,
    total_text: str,
    shipping_city: str,
) -> str:
    base = (
        f"{name} 按当前规则，{quantity:g}{unit} 可按 {format_money(unit_price)} 元/{unit} 核算，"
        f"货款小计 {total_text} 元。公开优惠规则：{format_discount_policy(product)}。"
    )
    shipping_note = quote_shipping_note(product, shipping_city)
    if shipping_note:
        return base + shipping_note
    return base + "请再补充联系人姓名、电话和收货城市。"


def quote_shipping_note(product: dict[str, Any], shipping_city: str) -> str:
    if not shipping_city:
        return ""
    shipping = str(product.get("shipping") or "")
    if "包邮" in shipping and is_free_shipping_region(shipping_city, shipping):
        return f"{shipping_city} 按当前物流规则预计包邮，合计暂按货款小计计算。请再补充联系人姓名和电话。"
    return f"{shipping_city} 的运费需要按物流实报实销或人工确认；当前仅能先确认货款小计。请再补充联系人姓名和电话。"


def is_free_shipping_region(city: str, shipping_policy: str) -> bool:
    if "江浙沪" in shipping_policy and any(region in city for region in ["江苏", "浙江", "上海", "南京", "苏州", "无锡", "常州", "杭州", "宁波"]):
        return True
    return "包邮" in shipping_policy and not any(region in city for region in ["新疆", "西藏", "青海", "港澳台"])


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_requested_unit_price(text: str) -> float | None:
    patterns = [
        rf"(\d+(?:\.\d+)?)\s*元?\s*/\s*(?:{quantity_unit_pattern()})",
        rf"(\d+(?:\.\d+)?)\s*元\s*(?:一|每)?(?:{quantity_unit_pattern()})",
        r"按\s*(\d+(?:\.\d+)?)\s*(?:元)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def extract_requested_tier_quantity(text: str) -> float | None:
    patterns = [
        rf"按\s*(\d+(?:\.\d+)?)\s*(?:{quantity_unit_pattern()})?\s*的?价",
        rf"(\d+(?:\.\d+)?)\s*(?:{quantity_unit_pattern()})?\s*起?的?价",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def discount_tiers(product: dict[str, Any]) -> list[dict[str, Any]]:
    tiers = []
    for item in product.get("discount_tiers", []) or []:
        try:
            tiers.append(
                {
                    "min_quantity": float(item.get("min_quantity")),
                    "unit_price": float(item.get("unit_price")),
                }
            )
        except (TypeError, ValueError):
            continue
    return sorted(tiers, key=lambda item: item["min_quantity"])


def best_unit_price_for_quantity(product: dict[str, Any], quantity: float | None) -> float | None:
    try:
        best = float(product.get("price"))
    except (TypeError, ValueError):
        best = None
    if quantity is None:
        return best
    for tier in discount_tiers(product):
        if quantity >= tier["min_quantity"]:
            best = tier["unit_price"] if best is None else min(best, tier["unit_price"])
    return best


def format_discount_policy(product: dict[str, Any]) -> str:
    tiers = discount_tiers(product)
    unit = str(product.get("unit") or "件")
    if tiers:
        return "，".join(
            f"{tier['min_quantity']:g}{unit}起 {format_money(tier['unit_price'])} 元/{unit}"
            for tier in tiers
        )
    return str(product.get("discount_policy") or "暂无公开优惠")


def clean_policy_text(value: Any) -> str:
    lines = []
    seen = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def min_order_phrase(product: dict[str, Any], unit: str) -> str:
    explicit = safe_float(product.get("min_order_quantity"))
    if explicit is not None:
        return f"起订量 {explicit:g} {unit}"
    tiers = discount_tiers(product)
    if tiers:
        first_tier = tiers[0]["min_quantity"]
        return f"阶梯价 {first_tier:g}{unit}起"
    return ""


def build_quote_without_quantity_text(product: dict[str, Any], name: str, unit: str, price: Any) -> str:
    pieces = [f"{name} 参考价 {price} 元/{unit}"]
    min_order = min_order_phrase(product, unit)
    if min_order:
        pieces.append(min_order)
    discount = format_discount_policy(product)
    if discount:
        pieces.append(f"公开优惠规则：{discount}")
    return "，".join(pieces) + "。请发采购数量、联系人和收货城市，我帮您核算。"


def detect_approval_required(
    product: dict[str, Any],
    intent: str,
    original_text: str,
    quantity: float | None,
) -> dict[str, Any]:
    normalized = normalize_text(original_text)
    requested_tier_quantity = extract_requested_tier_quantity(normalized)
    requested_unit_price = extract_requested_unit_price(normalized)
    eligible_price = best_unit_price_for_quantity(product, quantity)

    if intent != "discount" and requested_tier_quantity is None and requested_unit_price is None:
        return {"required": False}

    if requested_tier_quantity is not None and quantity is not None and requested_tier_quantity > quantity:
        return {
            "required": True,
            "reason": "requested_discount_tier_not_met",
            "requested_tier_quantity": requested_tier_quantity,
            "actual_quantity": quantity,
        }
    if requested_unit_price is not None and eligible_price is not None and requested_unit_price < eligible_price:
        return {
            "required": True,
            "reason": "requested_unit_price_below_allowed_policy",
            "requested_unit_price": requested_unit_price,
            "eligible_unit_price": eligible_price,
        }
    if has_any(normalized, product_keywords("approval")) and requested_unit_price is not None and eligible_price is None:
        return {
            "required": True,
            "reason": "requested_discount_without_known_policy",
            "requested_unit_price": requested_unit_price,
        }
    return {"required": False}


def format_money(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"
