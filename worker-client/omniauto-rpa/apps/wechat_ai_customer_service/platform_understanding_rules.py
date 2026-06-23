"""Visible platform understanding dictionaries for WeChat customer-service runtime."""

from __future__ import annotations

import copy
import json
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import shared_runtime_snapshot_path


DEFAULT_INTENT_KEYWORDS: dict[str, list[str]] = {
    "greeting": ["你好", "您好", "在吗", "hello", "hi"],
    "small_talk": ["哈哈", "先看看", "随便看看", "辛苦了", "回复挺快", "先了解一下"],
    "quote": ["价格", "报价", "多少钱", "什么价"],
    "discount": ["优惠", "折扣", "便宜点", "最低价", "砍价"],
    "stock": ["库存", "现货", "有货", "有没有"],
    "shipping": ["发货", "物流", "运费", "配送", "多久到", "包邮"],
    "invoice": ["发票", "开票", "专票", "普票"],
    "payment": ["付款", "对公", "账号", "转账", "账期", "月结"],
    "after_sales": ["售后", "质保", "保修", "退换", "维修"],
    "handoff": ["人工", "转人工", "请示", "核实", "确认一下", "顾问", "月结", "账期", "合同", "盖章"],
    "catalog": ["有哪些", "推荐", "车源", "清单", "目录"],
    "scene_product": [
        "场景",
        "使用场景",
        "用途",
        "适合",
        "推荐思路",
        "预算",
        "二手车",
        "车型",
        "车源",
        "车况",
        "省油",
        "油耗",
        "通勤",
        "家用",
        "接娃",
        "看车",
    ],
    "spec": ["型号", "规格", "参数", "容量", "功率", "供电", "安装"],
}

DEFAULT_INTENT_GROUPS: dict[str, list[str]] = {
    "business": ["catalog", "quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff"],
    "rag_authority_block": ["quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff", "customer_data"],
    "rag_soft_reference": ["scene_product", "spec", "small_talk", "unknown"],
    "rag_soft_actions": ["answer_from_evidence", "review_or_default_reply", "reply_small_talk"],
    "product_related": ["product", "scene_product", "spec", "quote", "discount", "stock", "shipping", "warranty", "catalog"],
    "product_context": ["shipping", "warranty", "spec", "quote", "stock", "discount", "scene_product"],
}

DEFAULT_PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "spec": ["型号", "规格", "参数", "容量", "功率", "供电", "门厚", "开孔"],
}

DEFAULT_RAG_GROUPS: dict[str, list[str]] = {
    "high_risk_terms": ["最低价", "赔偿", "退款", "账期", "合同", "保证", "包过"],
    "soft_reference_categories": ["product_explanations", "product_faq", "product_rules", "products"],
    "soft_reference_source_types": ["product_doc", "manual"],
}

DEFAULT_CUSTOMER_DATA_FIELD_LABELS: dict[str, list[str]] = {
    "name": ["姓名", "客户姓名", "联系人", "名字", "称呼", "我叫", "我是"],
    "phone": ["电话", "手机号", "手机", "联系电话", "联系方式"],
    "address": ["地址", "收货地址", "上牌城市", "所在城市", "城市"],
    "product": ["商品", "产品", "车型", "车源", "意向车型", "想看车型"],
    "quantity": ["数量", "台数"],
    "spec": ["规格", "配置", "版本"],
    "budget": ["预算", "预算范围"],
    "note": ["备注", "需求", "用车需求", "到店时间", "看车时间"],
}

DEFAULT_POLICY_TYPE_TO_INTENT: dict[str, str] = {
    "invoice": "invoice",
    "payment": "payment",
    "shipping": "shipping",
    "logistics": "shipping",
    "after_sales": "after_sales",
    "warranty": "after_sales",
    "product_catalog": "catalog",
}

DEFAULT_POLICY_TAGS: dict[str, str] = {
    "invoice": "invoice_policy",
    "payment": "payment_policy",
    "shipping": "shipping_policy",
    "after_sales": "after_sales_policy",
}

DEFAULT_QUANTITY_UNITS: list[str] = ["个", "件", "台", "套", "箱", "辆"]


_RULES_CACHE: dict[str, Any] = {
    "key": None,
    "payload": None,
}


def platform_understanding_cache_token() -> str:
    path = shared_runtime_snapshot_path()
    try:
        stat = path.stat()
    except OSError:
        return f"missing:{path}"
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"


def load_platform_understanding_rules(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    del settings
    return cached_platform_understanding_rules()


def cached_platform_understanding_rules() -> dict[str, Any]:
    key = platform_understanding_cache_token()
    if _RULES_CACHE.get("key") == key and isinstance(_RULES_CACHE.get("payload"), dict):
        return _RULES_CACHE["payload"]
    cloud = load_platform_understanding_rules_from_cloud()
    if cloud is not None:
        item = normalize_platform_understanding_rules(cloud)
        item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_understanding_rules"
        payload = {
            "ok": True,
            "path": item["_path"],
            "source": "cloud_shared_snapshot",
            "readonly": True,
            "item": item,
        }
    else:
        item = empty_rules()
        item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_understanding_rules"
        payload = {
            "ok": False,
            "path": item["_path"],
            "source": "cloud_shared_snapshot",
            "readonly": True,
            "error": "platform_understanding_rules_cloud_snapshot_required",
            "item": item,
        }
    _RULES_CACHE["key"] = key
    _RULES_CACHE["payload"] = payload
    return payload


def save_platform_understanding_rules(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    del payload, settings
    cloud = load_platform_understanding_rules_from_cloud()
    item = normalize_platform_understanding_rules(cloud) if cloud is not None else empty_rules()
    item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_understanding_rules"
    return {
        "ok": False,
        "path": item["_path"],
        "source": "cloud_shared_snapshot",
        "readonly": True,
        "error": "platform_understanding_rules_managed_by_cloud",
        "item": item,
    }


def normalize_platform_understanding_rules(payload: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(payload)
    item.setdefault("schema_version", 1)
    item.setdefault("title", "平台通用理解词典")
    item.setdefault("description", "所有客户通用的基础意图、检索和归类词典。行业专属业务规则不应写在这里。")
    item["intent_keywords"] = merge_map_of_string_lists(
        normalize_map_of_string_lists(item.get("intent_keywords")),
        DEFAULT_INTENT_KEYWORDS,
    )
    item["intent_groups"] = merge_map_of_string_lists(
        normalize_map_of_string_lists(item.get("intent_groups")),
        DEFAULT_INTENT_GROUPS,
    )
    item["policy_type_to_intent"] = merge_string_maps(
        normalize_string_map(item.get("policy_type_to_intent")),
        DEFAULT_POLICY_TYPE_TO_INTENT,
    )
    item["policy_tags"] = merge_string_maps(
        normalize_string_map(item.get("policy_tags")),
        DEFAULT_POLICY_TAGS,
    )
    item["policy_type_tags"] = normalize_map_of_string_lists(item.get("policy_type_tags"))
    item["policy_key_tags"] = normalize_map_of_string_lists(item.get("policy_key_tags"))
    item["product_knowledge_keywords"] = merge_map_of_string_lists(
        normalize_map_of_string_lists(item.get("product_knowledge_keywords")),
        DEFAULT_PRODUCT_KEYWORDS,
    )
    item["semantic_equivalents"] = normalize_map_of_string_lists(item.get("semantic_equivalents"))
    item["rag"] = merge_map_of_string_lists(
        normalize_map_of_string_lists(item.get("rag")),
        DEFAULT_RAG_GROUPS,
    )
    item["risk_keywords"] = normalize_map_of_string_lists(item.get("risk_keywords"))
    item["customer_data_field_labels"] = merge_map_of_string_lists(
        normalize_map_of_string_lists(item.get("customer_data_field_labels")),
        DEFAULT_CUSTOMER_DATA_FIELD_LABELS,
    )
    item["quantity_units"] = merge_string_lists(normalize_string_list(item.get("quantity_units")), DEFAULT_QUANTITY_UNITS)
    return item


def empty_rules() -> dict[str, Any]:
    return normalize_platform_understanding_rules({"schema_version": 1})


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_map_of_string_lists(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): normalize_string_list(raw) for key, raw in value.items() if str(key).strip()}


def normalize_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        clean_key = str(key).strip()
        clean_value = str(raw).strip()
        if clean_key and clean_value:
            result[clean_key] = clean_value
    return result


def merge_string_lists(primary: list[str], fallback: list[str]) -> list[str]:
    result: list[str] = []
    for value in [*primary, *fallback]:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def merge_map_of_string_lists(primary: dict[str, list[str]], fallback: dict[str, list[str]]) -> dict[str, list[str]]:
    merged = {key: list(values) for key, values in primary.items()}
    for key, values in fallback.items():
        clean_key = str(key).strip()
        if not clean_key:
            continue
        if clean_key in merged:
            merged[clean_key] = merge_string_lists(merged[clean_key], list(values))
        else:
            merged[clean_key] = merge_string_lists([], list(values))
    return merged


def merge_string_maps(primary: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    merged = dict(primary)
    for key, value in fallback.items():
        clean_key = str(key).strip()
        clean_value = str(value).strip()
        if clean_key and clean_value and clean_key not in merged:
            merged[clean_key] = clean_value
    return merged


def platform_understanding_item(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return load_platform_understanding_rules(settings).get("item", {})


def string_list(name: str, settings: dict[str, Any] | None = None) -> list[str]:
    item = platform_understanding_item(settings)
    return list(item.get(name, []) or []) if isinstance(item.get(name), list) else []


def string_set(name: str, settings: dict[str, Any] | None = None) -> set[str]:
    return set(string_list(name, settings=settings))


def map_of_lists(name: str, settings: dict[str, Any] | None = None) -> dict[str, list[str]]:
    item = platform_understanding_item(settings)
    value = item.get(name, {})
    return normalize_map_of_string_lists(value)


def string_map(name: str, settings: dict[str, Any] | None = None) -> dict[str, str]:
    item = platform_understanding_item(settings)
    return normalize_string_map(item.get(name, {}))


def intent_keywords(settings: dict[str, Any] | None = None) -> dict[str, list[str]]:
    return map_of_lists("intent_keywords", settings=settings)


def intent_group(name: str, settings: dict[str, Any] | None = None) -> set[str]:
    groups = map_of_lists("intent_groups", settings=settings)
    return set(groups.get(name, []) or [])


def product_keywords(name: str, settings: dict[str, Any] | None = None) -> list[str]:
    groups = map_of_lists("product_knowledge_keywords", settings=settings)
    return list(groups.get(name, []) or [])


def rag_terms(name: str, settings: dict[str, Any] | None = None) -> set[str]:
    groups = map_of_lists("rag", settings=settings)
    return set(groups.get(name, []) or [])


def risk_keywords(name: str, settings: dict[str, Any] | None = None) -> list[str]:
    groups = map_of_lists("risk_keywords", settings=settings)
    return list(groups.get(name, []) or [])


def semantic_equivalents(settings: dict[str, Any] | None = None) -> dict[str, tuple[str, ...]]:
    return {key: tuple(values) for key, values in map_of_lists("semantic_equivalents", settings=settings).items()}


def quantity_unit_pattern(settings: dict[str, Any] | None = None) -> str:
    units = string_list("quantity_units", settings=settings)
    if not units:
        return r"个|件|台|套|箱"
    return "|".join(sorted((escape_regex(item) for item in units), key=len, reverse=True))


def escape_regex(value: str) -> str:
    import re

    return re.escape(str(value))


def load_platform_understanding_rules_from_cloud() -> dict[str, Any] | None:
    path = shared_runtime_snapshot_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
    merged = policy_bundle.get("merged") if isinstance(policy_bundle.get("merged"), dict) else {}
    rules = merged.get("platform_understanding_rules") if isinstance(merged.get("platform_understanding_rules"), dict) else None
    return copy.deepcopy(rules) if isinstance(rules, dict) else None
