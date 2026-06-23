"""Industry catalog, defaults, and cloud policy bundles for shared knowledge."""

from __future__ import annotations

import copy
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import DEFAULT_TENANT_ID


GLOBAL_INDUSTRY_ID = "global"
DEFAULT_INDUSTRY_ID = "home_appliance"

INDUSTRY_DEFINITIONS: list[dict[str, Any]] = [
    {
        "industry_id": "used_car",
        "name": "二手车",
        "description": "二手车销售与售后咨询场景。",
        "status": "active",
    },
    {
        "industry_id": "home_appliance",
        "name": "家电",
        "description": "家电商品销售、物流安装与售后场景。",
        "status": "active",
    },
    {
        "industry_id": "fast_food",
        "name": "快餐",
        "description": "快餐门店点餐、配送与食品安全咨询场景。",
        "status": "active",
    },
    {
        "industry_id": "lab_instruments",
        "name": "实验室仪器",
        "description": "实验室仪器销售、校准维保与资质咨询场景。",
        "status": "active",
    },
]

INDUSTRY_ID_SET = {str(item.get("industry_id") or "") for item in INDUSTRY_DEFINITIONS}


DEFAULT_TENANT_INDUSTRY_BINDINGS: dict[str, str] = {
    DEFAULT_TENANT_ID: "home_appliance",
    "test01": "home_appliance",
    "chejin": "used_car",
    "chejin_usedcar_regression": "used_car",
}


def normalize_industry_id(value: Any, *, fallback: str = DEFAULT_INDUSTRY_ID) -> str:
    text = str(value or "").strip()
    if text in INDUSTRY_ID_SET:
        return text
    return fallback if fallback in INDUSTRY_ID_SET else DEFAULT_INDUSTRY_ID


def normalize_shared_item_industry_id(value: Any, *, fallback: str = GLOBAL_INDUSTRY_ID) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text == GLOBAL_INDUSTRY_ID:
        return GLOBAL_INDUSTRY_ID
    if text in INDUSTRY_ID_SET:
        return text
    return fallback


def industry_catalog() -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in INDUSTRY_DEFINITIONS]


def default_industry_for_tenant(tenant_id: str) -> str:
    return normalize_industry_id(DEFAULT_TENANT_INDUSTRY_BINDINGS.get(str(tenant_id or "").strip(), DEFAULT_INDUSTRY_ID))


def resolve_tenant_industry_id(
    tenant_id: str,
    *,
    tenant_record: dict[str, Any] | None = None,
    tenant_metadata: dict[str, Any] | None = None,
) -> str:
    sources = [tenant_record if isinstance(tenant_record, dict) else {}, tenant_metadata if isinstance(tenant_metadata, dict) else {}]
    for source in sources:
        for key in ("industry_id", "industry"):
            value = str(source.get(key) or "").strip()
            if value:
                return normalize_industry_id(value)
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        for key in ("industry_id", "industry"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return normalize_industry_id(value)
    return default_industry_for_tenant(tenant_id)


def global_platform_safety_rules() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": "平台底线规则（云端）",
        "description": "由服务端统一下发的通用安全边界。",
        "prompt_rules": [
            {
                "id": "evidence_first",
                "title": "先看证据",
                "description": "正式知识和共享知识优先，不做隐藏假设。",
                "instruction": "优先依据 evidence_pack 的正式知识、商品库、共享知识与历史上下文回复，不得臆造未给出的业务事实。",
                "enabled": True,
            },
            {
                "id": "no_fabrication",
                "title": "禁止编造",
                "description": "关键事实必须可追溯。",
                "instruction": "价格、库存、时效、审批、合同、赔付、开票、资质等关键信息必须来自证据；无证据时先说明需核实并转人工。",
                "enabled": True,
            },
            {
                "id": "entity_normalization_first",
                "title": "实体归一优先",
                "description": "客户提及商品/车型/型号时先做归一识别，再基于证据答复。",
                "instruction": "当客户提到商品、车型、型号、品牌等实体时，先进行别名、错别字、同音、音译和口语简称的归一识别；确认是同一实体后再依据商品库和正式知识回复。若仍不确定，先澄清确认，不得答非所问。",
                "enabled": True,
            },
            {
                "id": "human_action_boundary",
                "title": "人工动作边界",
                "description": "强约束场景默认转人工。",
                "instruction": "涉及签约、付款确认、退款赔偿、审批承诺、法律判断、医疗诊断等高风险动作，必须转人工处理。",
                "enabled": True,
            },
            {
                "id": "safe_refusal",
                "title": "自然拒绝",
                "description": "拒绝风险请求时给出自然解释。",
                "instruction": "对违规或越权请求应简洁说明边界并提供可执行的下一步（转人工/补充资料），避免机械模板化重复。",
                "enabled": True,
            },
            {
                "id": "contextual_honorific_usage",
                "title": "称呼不要机械重复",
                "description": "称呼要服务于真人感，而不是每条回复固定套用。",
                "instruction": "称呼只在开场寒暄和新话题切入时有选择地使用；连续对话中不要每条都加称呼。称呼应结合客户画像自然轮换，例如男性可在“姓哥/哥/老板”之间切换，女性可使用“姓姐/姐”；不要把文件传输助手、群名、门店名、测试名等非真人会话名拆成姓氏称呼。",
                "enabled": True,
            },
        ],
        "guard_terms": {
            "authority_tags": ["quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff", "customer_data"],
            "commitment_terms": ["包过", "绝对", "一定通过", "保证通过", "最低价保证", "无条件赔付"],
            "caution_terms": ["需核实", "需要确认", "人工", "转人工", "无法直接确认", "请以正式单据为准"],
            "forbidden_reply_terms": ["私下转账", "个人账户收款", "虚开发票", "假发票", "绕监管", "保证审批通过"],
            "forbidden_safe_markers": ["不支持", "不可以", "不能", "需要人工", "需要核实"],
            "appointment_commitment_terms": ["预留", "留货", "锁价", "代签", "代付"],
            "appointment_caution_terms": ["确认", "核实", "人工", "同事", "负责人"],
            "sales_followup_actors": ["销售", "同事", "顾问", "专员"],
            "sales_followup_actions": ["联系", "对接", "安排", "回电", "跟进"],
        },
    }


def global_platform_understanding_rules() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": "平台通用理解词典（云端）",
        "description": "由服务端统一下发的基础意图与检索词典。",
        "intent_keywords": {
            "greeting": ["你好", "您好", "在吗", "hello"],
            "catalog": ["有什么", "有哪些", "商品列表", "产品列表", "推荐下"],
            "quote": ["价格", "报价", "多少钱", "费用", "总价"],
            "discount": ["优惠", "便宜点", "折扣", "最低", "能少吗"],
            "stock": ["库存", "现货", "有货吗"],
            "shipping": ["发货", "物流", "多久到", "运费", "配送"],
            "invoice": ["发票", "开票", "专票", "普票", "税号"],
            "payment": ["付款", "支付", "对公", "收款账号", "账期"],
            "after_sales": ["售后", "保修", "退换", "退款", "赔偿"],
            "handoff": ["人工", "转人工", "投诉", "法务", "合同纠纷"],
            "customer_data": ["姓名", "电话", "地址", "联系人"],
        },
        "intent_groups": {
            "business": ["catalog", "quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff"],
            "rag_authority_block": ["quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff", "customer_data"],
        },
        "policy_type_to_intent": {
            "catalog": "catalog",
            "invoice": "invoice",
            "payment": "payment",
            "shipping": "shipping",
            "after_sales": "after_sales",
            "manual_required": "handoff",
        },
        "policy_tags": {
            "catalog": "catalog_policy",
            "invoice": "invoice_policy",
            "payment": "payment_policy",
            "shipping": "shipping_policy",
            "after_sales": "after_sales_policy",
        },
        "policy_type_tags": {
            "catalog": ["catalog"],
            "invoice": ["invoice"],
            "payment": ["payment"],
            "shipping": ["shipping"],
            "after_sales": ["after_sales"],
            "manual_required": ["handoff"],
        },
        "policy_key_tags": {
            "catalog_policy": ["catalog"],
            "invoice_policy": ["invoice"],
            "payment_policy": ["payment"],
            "shipping_policy": ["shipping"],
            "after_sales_policy": ["after_sales"],
        },
        "product_knowledge_keywords": {
            "quote": ["价格", "报价", "多少钱", "费用", "总价"],
            "stock": ["库存", "现货", "有货"],
            "shipping": ["发货", "物流", "时效", "配送"],
            "warranty": ["售后", "保修", "退换"],
            "discount": ["优惠", "折扣", "最低"],
            "spec": ["规格", "型号", "参数", "尺寸"],
        },
        "semantic_equivalents": {
            "推荐": ["建议", "适合", "可考虑"],
            "对比": ["比较", "区别", "怎么选"],
            "适合": ["适用", "用途", "场景"],
        },
        "rag": {
            "high_risk_terms": ["最低价", "赔偿", "退款", "账期", "合同", "保证", "包过"],
            "soft_reference_categories": ["product_explanations", "product_faq", "product_rules", "products"],
            "soft_reference_source_types": ["product_doc", "manual"],
        },
        "risk_keywords": {
            "knowledge_intake": ["赔偿", "退款", "账期", "保证", "虚开", "包过"],
            "knowledge_generator": ["赔偿", "退款", "账期", "保证", "虚开", "包过"],
            "diagnostics": ["赔偿", "退款", "账期", "合同", "保证"],
            "hard_handoff": ["必须转人工", "转人工", "法务", "投诉升级"],
            "review_warning": ["最低价", "赔偿", "退款", "账期", "保证", "承诺"],
        },
        "customer_data_field_labels": {
            "name": ["姓名", "联系人"],
            "phone": ["电话", "手机号", "联系方式"],
            "address": ["地址", "收货地址"],
            "product": ["商品", "产品", "需求"],
            "quantity": ["数量", "件数"],
            "spec": ["规格", "型号", "参数"],
            "budget": ["预算", "价格"],
            "note": ["备注", "补充说明"],
        },
        "quantity_units": ["台", "个", "件", "套", "箱", "份", "kg", "公斤"],
    }


INDUSTRY_SAFETY_OVERRIDES: dict[str, dict[str, Any]] = {
    "used_car": {
        "prompt_rules": [
            {
                "id": "used_car_truthful_disclosure",
                "title": "车况披露",
                "description": "事故/水泡/火烧等车况信息必须如实且可核实。",
                "instruction": "二手车场景下，事故、水泡、火烧、重大维修、里程、过户次数等信息必须基于已确认记录，不得做确定性承诺。",
                "enabled": True,
            }
        ],
        "guard_terms": {
            "commitment_terms": ["零事故保证", "包你过户", "里程绝对真实", "金融包过"],
            "forbidden_reply_terms": ["帮你改里程", "隐瞒事故", "不过户先开走", "包过征信"],
        },
    },
    "home_appliance": {
        "prompt_rules": [
            {
                "id": "home_appliance_install_boundary",
                "title": "安装与电气边界",
                "description": "安装和改造需符合安全与资质要求。",
                "instruction": "家电场景中，涉及电改、燃气改造、打孔承重、上门施工等事项必须提示按服务规范执行，超范围需人工确认。",
                "enabled": True,
            }
        ],
        "guard_terms": {
            "forbidden_reply_terms": ["无资质安装", "免检通电", "忽略接地", "拆机免责"],
        },
    },
    "fast_food": {
        "prompt_rules": [
            {
                "id": "fast_food_food_safety",
                "title": "食品安全优先",
                "description": "食品安全、过敏原和时效信息需谨慎。",
                "instruction": "快餐场景中，食品安全、过敏原、保温时效、改配禁忌等问题应按门店标准说明，不确定时必须转人工或建议停止食用。",
                "enabled": True,
            }
        ],
        "guard_terms": {
            "forbidden_reply_terms": ["过期继续吃", "随便替换过敏原", "熟度不够也可售卖"],
        },
    },
    "lab_instruments": {
        "prompt_rules": [
            {
                "id": "lab_instruments_calibration_boundary",
                "title": "校准与合规边界",
                "description": "检测结论和校准资质不得夸大。",
                "instruction": "实验室仪器场景中，不得承诺未经检定/校准的准确度；涉及合规报告、资质范围、认证结论须引用证据或转人工。",
                "enabled": True,
            }
        ],
        "guard_terms": {
            "forbidden_reply_terms": ["无校准证书也等同合格", "包过认证审核", "跳过计量检定"],
        },
    },
}


INDUSTRY_UNDERSTANDING_OVERRIDES: dict[str, dict[str, Any]] = {
    "used_car": {
        "intent_keywords": {
            "vehicle_condition": ["车况", "事故", "水泡", "火烧", "里程", "检测报告"],
            "transfer": ["过户", "转籍", "牌照", "登记"],
            "finance": ["首付", "月供", "征信", "分期", "金融方案"],
            "contract": ["订金", "定金", "合同", "违约", "赔偿"],
        },
        "intent_groups": {
            "rag_authority_block": ["vehicle_condition", "transfer", "finance"],
        },
        "risk_keywords": {
            "hard_handoff": ["重大事故争议", "里程争议", "合同纠纷", "征信投诉", "金融审批投诉"],
        },
    },
    "home_appliance": {
        "intent_keywords": {
            "install": ["安装", "上门", "打孔", "电改", "燃气改造"],
            "delivery": ["送装一体", "预约送货", "搬楼", "旧机回收"],
            "energy": ["能效", "耗电", "功率", "电压"],
            "after_sales_dispute": ["开箱破损", "外观划伤", "安装后退货", "维修争议", "赔付"],
        }
    },
    "fast_food": {
        "intent_keywords": {
            "order": ["下单", "套餐", "加料", "备注", "堂食", "外卖"],
            "delivery": ["骑手", "配送", "超时", "洒漏", "漏单"],
            "allergen": ["过敏", "忌口", "花生", "乳制品", "海鲜"],
            "food_safety_incident": ["食物中毒", "异物", "变质", "腹泻", "发热"],
        },
        "risk_keywords": {
            "hard_handoff": ["食物中毒", "异物", "严重过敏", "食品安全投诉"],
        },
    },
    "lab_instruments": {
        "intent_keywords": {
            "calibration": ["校准", "检定", "溯源", "证书"],
            "qualification": ["资质", "CMA", "CNAS", "认证"],
            "maintenance": ["保养", "维修", "故障代码", "返修"],
            "compliance_validation": ["审计追踪", "方法学验证", "软件版本", "电子签名", "数据完整性"],
        },
        "risk_keywords": {
            "hard_handoff": ["认证结论承诺", "法规豁免", "超资质检测", "危化样品处置", "生物安全咨询"],
        },
    },
}


def _merge_string_list(base: list[str], extra: list[str]) -> list[str]:
    merged = list(base)
    for item in extra:
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _merge_map_of_lists(base: dict[str, list[str]], extra: dict[str, list[str]]) -> dict[str, list[str]]:
    merged = {str(key): [str(item) for item in value] for key, value in base.items()}
    for key, values in extra.items():
        clean_key = str(key).strip()
        if not clean_key:
            continue
        normalized_values = [str(item).strip() for item in values if str(item).strip()]
        if clean_key in merged:
            merged[clean_key] = _merge_string_list(merged[clean_key], normalized_values)
        else:
            merged[clean_key] = normalized_values
    return merged


def build_policy_bundle(industry_id: str) -> dict[str, Any]:
    industry = normalize_industry_id(industry_id)
    global_safety = copy.deepcopy(global_platform_safety_rules())
    global_understanding = copy.deepcopy(global_platform_understanding_rules())
    safety_override = copy.deepcopy(INDUSTRY_SAFETY_OVERRIDES.get(industry, {}))
    understanding_override = copy.deepcopy(INDUSTRY_UNDERSTANDING_OVERRIDES.get(industry, {}))

    merged_safety = copy.deepcopy(global_safety)
    merged_safety["prompt_rules"] = _merge_string_list_by_id(global_safety.get("prompt_rules", []), safety_override.get("prompt_rules", []))
    merged_guard_terms = copy.deepcopy(global_safety.get("guard_terms", {}))
    for key, values in (safety_override.get("guard_terms", {}) or {}).items():
        existing = merged_guard_terms.get(key, [])
        existing_list = existing if isinstance(existing, list) else []
        merged_guard_terms[key] = _merge_string_list(existing_list, values if isinstance(values, list) else [])
    merged_safety["guard_terms"] = merged_guard_terms

    merged_understanding = copy.deepcopy(global_understanding)
    for key in (
        "intent_keywords",
        "intent_groups",
        "policy_type_tags",
        "policy_key_tags",
        "product_knowledge_keywords",
        "semantic_equivalents",
        "risk_keywords",
        "customer_data_field_labels",
    ):
        base_value = merged_understanding.get(key, {})
        if not isinstance(base_value, dict):
            base_value = {}
        extra_value = understanding_override.get(key, {})
        if not isinstance(extra_value, dict):
            extra_value = {}
        merged_understanding[key] = _merge_map_of_lists(base_value, extra_value)
    if isinstance(understanding_override.get("policy_type_to_intent"), dict):
        merged_understanding["policy_type_to_intent"] = {
            **(merged_understanding.get("policy_type_to_intent") if isinstance(merged_understanding.get("policy_type_to_intent"), dict) else {}),
            **understanding_override.get("policy_type_to_intent", {}),
        }
    if isinstance(understanding_override.get("policy_tags"), dict):
        merged_understanding["policy_tags"] = {
            **(merged_understanding.get("policy_tags") if isinstance(merged_understanding.get("policy_tags"), dict) else {}),
            **understanding_override.get("policy_tags", {}),
        }
    if isinstance(understanding_override.get("rag"), dict):
        merged_understanding["rag"] = copy.deepcopy(merged_understanding.get("rag", {}))
        for rag_key, rag_value in understanding_override["rag"].items():
            if isinstance(rag_value, list):
                merged_understanding["rag"][rag_key] = _merge_string_list(
                    merged_understanding["rag"].get(rag_key, []) if isinstance(merged_understanding["rag"].get(rag_key), list) else [],
                    rag_value,
                )
            else:
                merged_understanding["rag"][rag_key] = rag_value
    if isinstance(understanding_override.get("quantity_units"), list):
        merged_understanding["quantity_units"] = _merge_string_list(
            merged_understanding.get("quantity_units", []) if isinstance(merged_understanding.get("quantity_units"), list) else [],
            understanding_override.get("quantity_units", []),
        )

    return {
        "schema_version": 1,
        "source": "cloud_official_shared_library",
        "industry_id": industry,
        "global": {
            "platform_safety_rules": global_safety,
            "platform_understanding_rules": global_understanding,
        },
        "industry": {
            "platform_safety_rules": safety_override,
            "platform_understanding_rules": understanding_override,
        },
        "merged": {
            "platform_safety_rules": merged_safety,
            "platform_understanding_rules": merged_understanding,
        },
    }


def _merge_string_list_by_id(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*(base or []), *(extra or [])]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        merged.append(copy.deepcopy(item))
    return merged


def seed_shared_library_items() -> list[dict[str, Any]]:
    """Default global/industry shared items used to bootstrap cloud shared library."""
    return [
        {
            "item_id": "shared_global_evidence_first",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "global_guidelines",
            "title": "证据优先回复",
            "content": "回复必须优先依据正式知识、共享规则和已确认上下文；无证据时明确说明需核实并转人工。",
            "keywords": ["证据", "核实", "转人工"],
            "applies_to": "所有行业的客服对话",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "平台通用规则",
            "data": {"title": "证据优先回复", "guideline_text": "回复必须优先依据正式知识、共享规则和已确认上下文；无证据时明确说明需核实并转人工。"},
        },
        {
            "item_id": "shared_global_entity_normalization_first",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "global_guidelines",
            "title": "实体归一识别优先",
            "content": "客户提到商品、车型、型号、品牌等实体时，先做别名、错别字、同音、音译、简称归一；确认是同一实体后再按商品库与正式知识回复，不确定时先澄清。",
            "keywords": ["别名", "错别字", "同音", "音译", "简称", "归一", "商品", "车型", "型号"],
            "applies_to": "所有行业的商品/服务实体问答",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "平台通用实体理解原则",
            "data": {
                "title": "实体归一识别优先",
                "guideline_text": "客户提到商品、车型、型号、品牌等实体时，先做别名、错别字、同音、音译、简称归一；确认是同一实体后再按商品库与正式知识回复，不确定时先澄清。",
                "intent_tags": ["catalog", "quote", "discount", "stock", "shipping", "spec", "scene_product", "product"],
                "tone_tags": ["natural", "precise", "evidence_first"],
                "always_include": True,
            },
        },
        {
            "item_id": "shared_global_contextual_honorific_usage",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "reply_style",
            "title": "自然称呼使用规则",
            "content": "微信客服不要每条回复都在开头加称呼。称呼主要用于开场寒暄，或客户切换到新话题时有选择地使用；中途连续追问默认不加。称呼要结合客户画像自然轮换：男性可在“姓哥/哥/老板”之间切换，女性可用“姓姐/姐”。不要把文件传输助手、群名、门店名、客服名、测试名等非真人会话名拆成姓氏称呼。",
            "keywords": ["称呼", "哥", "姐", "老板", "开场", "新话题", "真人感"],
            "applies_to": "所有行业的微信客服对话",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "平台通用回复口吻",
            "data": {
                "title": "自然称呼使用规则",
                "guideline_text": "微信客服不要每条回复都在开头加称呼。称呼主要用于开场寒暄，或客户切换到新话题时有选择地使用；中途连续追问默认不加。称呼要结合客户画像自然轮换：男性可在“姓哥/哥/老板”之间切换，女性可用“姓姐/姐”。不要把文件传输助手、群名、门店名、客服名、测试名等非真人会话名拆成姓氏称呼。",
                "applies_to": "所有微信 AI 客服账号的客户可见回复",
            },
        },
        {
            "item_id": "shared_global_no_fabrication",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "risk_control",
            "title": "关键事实不得编造",
            "content": "价格、库存、合同、赔付、资质、时效等关键事实没有证据时，不得生成确定性承诺。",
            "keywords": ["价格", "库存", "合同", "赔付", "资质"],
            "applies_to": "所有行业的高风险问答",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "平台通用风险边界",
            "data": {"title": "关键事实不得编造", "guideline_text": "价格、库存、合同、赔付、资质、时效等关键事实没有证据时，不得生成确定性承诺。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "missing_authoritative_evidence"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_global_privacy_minimum_collection",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "risk_control",
            "title": "隐私信息最小化采集",
            "content": "仅在履约必要时采集姓名、电话、地址等信息，避免索取与交易无关的身份证件、银行卡、验证码等敏感信息。",
            "keywords": ["隐私", "个人信息", "手机号", "地址", "最小化"],
            "applies_to": "所有行业的信息收集场景",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《个人信息保护法》最小必要原则",
            "data": {"title": "隐私信息最小化采集", "guideline_text": "仅在履约必要时采集姓名、电话、地址等信息，避免索取与交易无关的身份证件、银行卡、验证码等敏感信息。"},
        },
        {
            "item_id": "shared_global_no_private_payment",
            "industry_id": GLOBAL_INDUSTRY_ID,
            "category_id": "risk_control",
            "title": "禁止私下收款引导",
            "content": "不得引导客户向个人账户、社交账号或非官方渠道转账；涉及收款必须提示以企业官方支付链路为准。",
            "keywords": ["收款", "转账", "个人账户", "官方渠道"],
            "applies_to": "所有行业支付咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "禁止私下收款引导", "guideline_text": "不得引导客户向个人账户、社交账号或非官方渠道转账；涉及收款必须提示以企业官方支付链路为准。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "payment_channel_compliance"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_used_car_compliance_disclosure",
            "industry_id": "used_car",
            "category_id": "global_guidelines",
            "title": "二手车车况披露规则",
            "content": "涉及事故、水泡、火烧、里程、过户次数、检修记录等问题，必须基于可验证资料；不确定时先说明待核验。",
            "keywords": ["车况", "事故", "水泡", "火烧", "里程", "过户"],
            "applies_to": "二手车咨询场景",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《二手车流通管理办法》卖方信息披露要求",
            "data": {"title": "二手车车况披露规则", "guideline_text": "涉及事故、水泡、火烧、里程、过户次数、检修记录等问题，必须基于可验证资料；不确定时先说明待核验。"},
        },
        {
            "item_id": "shared_used_car_transfer_docs",
            "industry_id": "used_car",
            "category_id": "global_guidelines",
            "title": "二手车交易资料完整性",
            "content": "提醒客户交易完成后应交付合法证照与交易凭证，涉及过户办理必须按登记流程执行。",
            "keywords": ["过户", "登记", "发票", "行驶证", "登记证书"],
            "applies_to": "二手车交易流程咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《二手车流通管理办法》第二十二至二十五条",
            "data": {"title": "二手车交易资料完整性", "guideline_text": "提醒客户交易完成后应交付合法证照与交易凭证，涉及过户办理必须按登记流程执行。"},
        },
        {
            "item_id": "shared_used_car_finance_handoff",
            "industry_id": "used_car",
            "category_id": "risk_control",
            "title": "二手车金融审批风险边界",
            "content": "征信、审批通过率、放款时效等不能保证；只能说明流程与所需资料，并转人工顾问。",
            "keywords": ["征信", "审批", "放款", "首付", "月供"],
            "applies_to": "二手车金融问答",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "二手车金融审批风险边界", "guideline_text": "征信、审批通过率、放款时效等不能保证；只能说明流程与所需资料，并转人工顾问。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "used_car_finance_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_used_car_test_drive_notice",
            "industry_id": "used_car",
            "category_id": "global_guidelines",
            "title": "二手车试驾安全提示",
            "content": "试驾沟通需明确有效证件、保险覆盖、试驾路线与责任边界；涉及高风险路况或改装车辆必须人工确认。",
            "keywords": ["试驾", "保险", "证件", "改装车", "责任"],
            "applies_to": "二手车试驾预约与沟通",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "二手车试驾安全提示", "guideline_text": "试驾沟通需明确有效证件、保险覆盖、试驾路线与责任边界；涉及高风险路况或改装车辆必须人工确认。"},
        },
        {
            "item_id": "shared_used_car_deposit_contract_boundary",
            "industry_id": "used_car",
            "category_id": "risk_control",
            "title": "二手车订金合同边界",
            "content": "订金是否可退、违约责任、车况争议处理必须以书面合同或门店条款为准，自动回复不得替代合同承诺。",
            "keywords": ["订金", "定金", "可退", "违约", "合同"],
            "applies_to": "二手车下订与合同咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "二手车订金合同边界", "guideline_text": "订金是否可退、违约责任、车况争议处理必须以书面合同或门店条款为准，自动回复不得替代合同承诺。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "used_car_contract_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_home_appliance_three_guarantees",
            "industry_id": "home_appliance",
            "category_id": "global_guidelines",
            "title": "家电三包与售后说明",
            "content": "家电售后答复需围绕三包、质保期、维修记录与票据凭证，避免超出标准承诺。",
            "keywords": ["三包", "质保", "维修", "票据", "售后"],
            "applies_to": "家电售后咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《部分商品修理更换退货责任规定》",
            "data": {"title": "家电三包与售后说明", "guideline_text": "家电售后答复需围绕三包、质保期、维修记录与票据凭证，避免超出标准承诺。"},
        },
        {
            "item_id": "shared_home_appliance_install_scope",
            "industry_id": "home_appliance",
            "category_id": "risk_control",
            "title": "家电安装改造边界",
            "content": "涉及电气/燃气改造、墙体承重、额外施工的请求，必须由人工确认现场条件和服务范围。",
            "keywords": ["安装", "电改", "燃气", "打孔", "承重"],
            "applies_to": "家电安装咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "家电安装改造边界", "guideline_text": "涉及电气/燃气改造、墙体承重、额外施工的请求，必须由人工确认现场条件和服务范围。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "home_appliance_install_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_home_appliance_delivery_style",
            "industry_id": "home_appliance",
            "category_id": "reply_style",
            "title": "家电配送时效回复口径",
            "content": "先给出可确认的配送区间，再提示受库存、仓配与预约档期影响，必要时转人工排期。",
            "keywords": ["配送", "时效", "预约", "库存"],
            "applies_to": "家电物流咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "家电配送时效回复口径", "guideline_text": "先给出可确认的配送区间，再提示受库存、仓配与预约档期影响，必要时转人工排期。"},
        },
        {
            "item_id": "shared_home_appliance_energy_label_notice",
            "industry_id": "home_appliance",
            "category_id": "global_guidelines",
            "title": "家电能效与功率说明",
            "content": "能效等级、额定功率、用电环境相关答复应引用规格参数，不得臆测省电比例或改写官方标识信息。",
            "keywords": ["能效", "功率", "耗电", "参数", "规格"],
            "applies_to": "家电选购参数咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "家电能效与功率说明", "guideline_text": "能效等级、额定功率、用电环境相关答复应引用规格参数，不得臆测省电比例或改写官方标识信息。"},
        },
        {
            "item_id": "shared_home_appliance_refund_policy_boundary",
            "industry_id": "home_appliance",
            "category_id": "risk_control",
            "title": "家电退换与损耗争议边界",
            "content": "安装后退换、外观损伤、运输损耗、旧机回收折价等争议需依据验收记录处理，自动回复不得直接给出赔付承诺。",
            "keywords": ["退换", "损耗", "外观损伤", "验收", "赔付"],
            "applies_to": "家电售后争议",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "家电退换与损耗争议边界", "guideline_text": "安装后退换、外观损伤、运输损耗、旧机回收折价等争议需依据验收记录处理，自动回复不得直接给出赔付承诺。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "home_appliance_after_sales_dispute"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_fast_food_food_safety",
            "industry_id": "fast_food",
            "category_id": "risk_control",
            "title": "快餐食品安全优先",
            "content": "遇到疑似异物、变质、食安风险、严重过敏反应时，优先提示停止食用并立即转人工处理。",
            "keywords": ["异物", "变质", "过敏", "食物中毒", "食品安全"],
            "applies_to": "快餐投诉与风险反馈",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《食品安全法》与《餐饮服务食品安全操作规范》",
            "data": {"title": "快餐食品安全优先", "guideline_text": "遇到疑似异物、变质、食安风险、严重过敏反应时，优先提示停止食用并立即转人工处理。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "fast_food_safety_manual_escalation"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_fast_food_order_recheck",
            "industry_id": "fast_food",
            "category_id": "global_guidelines",
            "title": "快餐订单确认要素",
            "content": "自动回复应优先确认门店、用餐方式、餐品、份数、忌口/过敏原与联系方式，减少错单漏单。",
            "keywords": ["点餐", "堂食", "外卖", "份数", "忌口", "过敏原"],
            "applies_to": "快餐下单前沟通",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "快餐订单确认要素", "guideline_text": "自动回复应优先确认门店、用餐方式、餐品、份数、忌口/过敏原与联系方式，减少错单漏单。"},
        },
        {
            "item_id": "shared_fast_food_delivery_style",
            "industry_id": "fast_food",
            "category_id": "reply_style",
            "title": "快餐配送异常回复口径",
            "content": "对超时、洒漏、漏单场景先致歉，明确补救路径（补发/退款/人工处理）并收集最小必要信息。",
            "keywords": ["超时", "洒漏", "漏单", "补发", "退款"],
            "applies_to": "快餐配送异常",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "快餐配送异常回复口径", "guideline_text": "对超时、洒漏、漏单场景先致歉，明确补救路径（补发/退款/人工处理）并收集最小必要信息。"},
        },
        {
            "item_id": "shared_fast_food_allergen_boundary",
            "industry_id": "fast_food",
            "category_id": "risk_control",
            "title": "快餐过敏原提示边界",
            "content": "涉及严重过敏史、儿童/孕妇敏感食材咨询时，必须提示以门店配方公告和人工确认为准，不得给出医疗诊断建议。",
            "keywords": ["过敏原", "花生", "乳制品", "儿童", "孕妇"],
            "applies_to": "快餐配料与过敏咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "快餐过敏原提示边界", "guideline_text": "涉及严重过敏史、儿童/孕妇敏感食材咨询时，必须提示以门店配方公告和人工确认为准，不得给出医疗诊断建议。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "fast_food_allergen_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_fast_food_temperature_notice",
            "industry_id": "fast_food",
            "category_id": "global_guidelines",
            "title": "快餐出餐温控提示",
            "content": "对保温时长、复热建议、冷链餐品时效等应使用门店标准口径；不确定时提醒尽快食用并转人工确认。",
            "keywords": ["保温", "复热", "冷链", "时效", "尽快食用"],
            "applies_to": "快餐食用安全咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "快餐出餐温控提示", "guideline_text": "对保温时长、复热建议、冷链餐品时效等应使用门店标准口径；不确定时提醒尽快食用并转人工确认。"},
        },
        {
            "item_id": "shared_lab_instruments_calibration_scope",
            "industry_id": "lab_instruments",
            "category_id": "global_guidelines",
            "title": "仪器校准与检定口径",
            "content": "精度、不确定度、合规结论需基于有效校准/检定证书与资质范围，不得超范围承诺。",
            "keywords": ["校准", "检定", "证书", "不确定度", "资质"],
            "applies_to": "实验室仪器技术咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "notes": "参考《计量法》《检验检测机构资质认定管理办法》",
            "data": {"title": "仪器校准与检定口径", "guideline_text": "精度、不确定度、合规结论需基于有效校准/检定证书与资质范围，不得超范围承诺。"},
        },
        {
            "item_id": "shared_lab_instruments_compliance_handoff",
            "industry_id": "lab_instruments",
            "category_id": "risk_control",
            "title": "实验室资质合规边界",
            "content": "涉及认证结论、法规豁免、官方资质解释时，必须转人工合规或技术负责人确认。",
            "keywords": ["CMA", "CNAS", "认证", "法规", "合规"],
            "applies_to": "资质合规与审计问答",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "实验室资质合规边界", "guideline_text": "涉及认证结论、法规豁免、官方资质解释时，必须转人工合规或技术负责人确认。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "lab_compliance_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_lab_instruments_maintenance_style",
            "industry_id": "lab_instruments",
            "category_id": "reply_style",
            "title": "实验室仪器故障受理口径",
            "content": "先确认型号、序列号、故障现象、使用环境和校准状态，再给出基础排查建议与送修路径。",
            "keywords": ["故障", "维修", "序列号", "送修", "保养"],
            "applies_to": "实验室仪器售后",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "实验室仪器故障受理口径", "guideline_text": "先确认型号、序列号、故障现象、使用环境和校准状态，再给出基础排查建议与送修路径。"},
        },
        {
            "item_id": "shared_lab_instruments_hazard_notice",
            "industry_id": "lab_instruments",
            "category_id": "risk_control",
            "title": "危化/生物样品处置边界",
            "content": "涉及危化品、生物样品、放射源相关操作咨询时，仅可提供设备使用边界提示，不得替代实验室SOP和EHS审批。",
            "keywords": ["危化品", "生物样品", "放射源", "EHS", "SOP"],
            "applies_to": "实验室高风险样品咨询",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "危化/生物样品处置边界", "guideline_text": "涉及危化品、生物样品、放射源相关操作咨询时，仅可提供设备使用边界提示，不得替代实验室SOP和EHS审批。", "allow_auto_reply": False, "requires_handoff": True, "handoff_reason": "lab_hse_manual_review"},
            "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
        },
        {
            "item_id": "shared_lab_instruments_validation_evidence",
            "industry_id": "lab_instruments",
            "category_id": "global_guidelines",
            "title": "仪器软件版本与验证记录",
            "content": "涉及审计追踪、软件版本、方法学验证、电子签名等合规问答时，应提示以已归档验证记录为准。",
            "keywords": ["审计追踪", "软件版本", "方法学验证", "电子签名", "合规审计"],
            "applies_to": "实验室审计与合规问答",
            "status": "active",
            "source": "system_seed_industry_library",
            "data": {"title": "仪器软件版本与验证记录", "guideline_text": "涉及审计追踪、软件版本、方法学验证、电子签名等合规问答时，应提示以已归档验证记录为准。"},
        },
    ]
