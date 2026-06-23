"""Legacy/advisory fast routing for WeChat customer-service replies.

When Brain First is enabled, this module must not own or finalize
customer-visible wording. The main workflow disables these legacy generators and
may only use their outputs as routing/evidence hints for the
customer_service_brain. Direct local replies are kept for explicit legacy-mode
compatibility tests only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from apps.wechat_ai_customer_service.wechat_message_normalizer import (
    split_wechat_ocr_speaker_prefix,
)
from knowledge_runtime import KnowledgeRuntime
from product_name_matcher import collect_matched_aliases, compact_match_text, fold_confusable_text
from product_vocabulary import contains_product_term


DEFAULT_MAX_PROMPT_TOKENS = 3000
DEFAULT_MAX_COMPLETION_TOKENS = 500

GREETING_TERMS = ("你好", "您好", "在吗", "在么", "在不在", "有人吗", "哈喽", "嗨")
SOCIAL_SUMMON_TERMS = (
    "在不在",
    "在吗",
    "在么",
    "有人吗",
    "出来",
    "有事问你",
    "有事咨询",
    "方便吗",
    "忙吗",
    "人呢",
    "回下",
    "回一下",
)
DATA_TERMS = ("电话", "手机号", "联系方式", "我叫", "姓名", "地址")
RECOMMEND_TERMS = (
    "预算",
    "推荐",
    "车源",
    "代步",
    "通勤",
    "家用",
    "省油",
    "自动挡",
    "上下班",
    "接娃",
    "买菜",
    "练手",
    "新手",
    "刚拿驾照",
    "跑高速",
    "后排",
    "空间",
    "舒服",
    "年限",
    "爸妈",
    "父母",
    "看车",
    "到店",
    "周末",
    "周六",
    "周日",
    "上午",
    "下午",
    "过去",
    "预约",
    "车还在",
    "安排",
    "suv",
    "皮实",
    "耐造",
    "工地",
)
APPOINTMENT_TERMS = ("看车", "能看", "去看", "到店", "试驾", "周末", "周六", "周日", "上午", "下午", "几点", "安排", "过去", "来店", "留车")
VEHICLE_AVAILABILITY_CONFIRM_TERMS = ("车还在", "车源还在", "现车还在", "车还在不在", "车源还在不在", "现车还在不在")
SAME_DAY_DELIVERY_TERMS = ("办手续", "直接办", "当天办", "提车", "直接提", "当天提", "临牌", "过户", "交车")
STORE_ARRIVAL_HANDOFF_TERMS = ("店地址", "门店地址", "地址", "位置", "导航", "在哪", "哪里", "找谁", "联系人")
FOLLOWUP_TERMS = ("刚才", "你刚才", "那台", "这个", "这台", "上一台", "前面", "前面说的", "按这个", "就按这个")
FOLLOWUP_SOURCE_TERMS = (
    "这两台",
    "这几台",
    "上面两台",
    "前面两台",
    "刚才两台",
    "哪台",
    "哪款",
    "哪一辆",
    "两台里",
    "几台里",
    "从里面",
    "里面选",
    "锁定",
    "按刚才",
    "继续按",
    "别再问预算",
    "不用再问预算",
    "库存表",
    "库存",
    "车源清单",
    "现车清单",
)
EXPLICIT_SOURCE_TERMS = (
    "哪台",
    "哪款",
    "哪一辆",
    "两台",
    "几台",
    "车源",
    "发我",
    "挑两",
    "挑几",
    "挑两台",
    "挑几台",
    "直接挑",
    "给我挑",
    "给我两台",
    "给我几台",
    "推荐两",
    "推荐几",
    "两三个方向",
    "几个方向",
    "库存表",
    "库存",
    "车源清单",
    "现车清单",
    "在售清单",
)
RECENT_GUIDANCE_TERMS = ("预算", "用途", "车况", "缩到", "筛", "两三台", "省心")
CUSTOMER_CHALLENGE_TERMS = (
    "答非所问",
    "没回答",
    "没回到",
    "不是这个意思",
    "我问的是",
    "请直接回答",
    "别绕",
    "别老",
    "重复",
    "还在推荐",
    "到底",
    "你先回答",
    "先别推荐",
)
VALUE_RETENTION_TERMS = ("保值", "再卖", "卖掉", "亏得少", "残值", "二手行情")
VEHICLE_COMPARE_TERMS = (
    "哪个更适合",
    "哪台更适合",
    "哪一辆更适合",
    "哪个更好",
    "哪台更好",
    "哪一辆更好",
    "哪个更贴近",
    "哪台更贴近",
    "哪一辆更贴近",
    "更贴近",
    "怎么选",
    "更适合",
    "更偏哪台",
    "更偏哪个",
    "更偏哪款",
    "先看哪款",
    "先看哪台",
    "先看哪个",
    "建议先看哪款",
    "建议先看哪台",
    "建议先看哪个",
    "好开",
    "好停",
    "少操心",
    "还是",
)
SINGLE_CHOICE_COMPARE_TERMS = (
    "哪一辆",
    "选一辆",
    "二选一",
    "就这两台",
    "就这两个",
    "只选一台",
    "只选一个",
    "更推荐哪",
    "更偏向哪",
    "先看哪款",
    "先看哪台",
    "先看哪个",
    "建议先看哪款",
    "建议先看哪台",
    "建议先看哪个",
    "只能选一台",
    "优先顺序",
    "先后顺序",
    "排个先后",
    "排顺序",
)
TRADE_IN_TERMS = (
    "置换",
    "抵车款",
    "抵多少",
    "抵扣",
    "抵一点",
    "卖车",
    "收车",
    "旧车",
    "估价",
    "估个",
    "估一下",
    "估一",
    "现场估",
    "现场评估",
    "开过去",
    "老车",
    "剐蹭",
    "怎么估",
    "大概区间",
    "准备哪些东西",
    "准备什么东西",
    "准备什么材料",
)
NEW_ENERGY_CHECK_TERMS = ("电池", "三电", "插混", "混动", "新能源", "dmi", "dm-i", "DM-i", "DMI")
INSPECTION_GUIDANCE_TERMS = ("检测报告", "维保记录", "车况", "公里数", "事故", "事故车", "水泡", "火烧", "第三方检测", "出险", "补漆", "换件", "披露", "说清楚", "把关")
FEATURE_GUIDANCE_TERMS = ("安全配置", "倒车影像", "倒车雷达", "雷达", "360", "高速", "气囊", "ESP", "辅助驾驶")
FINANCE_GUIDANCE_TERMS = ("贷款", "分期", "按揭", "首付", "月供", "金融", "征信", "利率", "审批")
FEE_GUIDANCE_TERMS = ("过户", "保险", "上牌", "费用", "收费", "杂费", "加费用", "费用明细", "提前说清楚")
TESTDRIVE_MATERIAL_TERMS = (
    "试驾",
    "试车",
    "身份证",
    "驾驶证",
    "材料",
    "资料",
    "证件",
    "带什么",
    "需要带",
    "准备什么",
    "准备哪些",
)
PRICE_NEGOTIATION_TERMS = ("价格", "优惠", "贵", "谈一点", "少一点", "便宜点", "还能谈", "再谈", "让点")
PRICE_QUERY_TERMS = ("多少钱", "什么价", "报价", "价格", "价位", "区间", "落地价", "总价")
CATALOG_PRICE_SUPERLATIVE_TERMS = ("最贵", "价格最高", "报价最高", "标价最高", "最高价", "最高价格", "贵的车", "高端")
CATALOG_PRICE_LIST_TERMS = ("标价", "报价单", "价格表", "发一下报价", "发下报价", "发一下价格", "发下价格", "公开报价", "在售报价", "报价发我", "把报价发我", "发我报价", "发报价")
WARRANTY_GUIDANCE_TERMS = ("质保", "保修", "售后", "买完", "核心部件", "出问题", "维修")
COMFORT_HIGHWAY_TERMS = ("后排", "孩子", "老人", "父母", "高速", "回老家", "颠", "舒适", "舒服", "隔音", "胎噪", "底盘")
CARGO_SPACE_GUIDANCE_TERMS = (
    "第二排",
    "二排",
    "座椅放倒",
    "放倒",
    "后备厢",
    "后备箱",
    "装东西",
    "拉东西",
    "装得下",
    "放得下",
    "灯架",
    "背景架",
    "展架",
    "音响架",
    "物料",
    "折叠桌",
    "箱子",
    "尺寸",
    "容积",
)
VEHICLE_TYPE_GUIDANCE_TERMS = ("轿车", "suv", "mpv", "七座", "车型", "方向", "太大", "停车", "油耗")
MAINTENANCE_COST_TERMS = ("保养成本", "后期成本", "养车成本", "用车成本", "维修成本", "维护成本", "维护", "后期保养", "后期维修", "小毛病", "少操心", "豪华品牌", "mpv", "油车", "油耗")
COMMON_VEHICLE_BRAND_OR_MODEL_TERMS = ("品牌", "车型", "车系", "型号", "德系", "日系", "美系", "韩系", "法系", "国产")
NO_DEPOSIT_VISIT_TERMS = (
    "不交定金",
    "先不交定金",
    "不想先交定金",
    "不想交定金",
    "不用定金",
    "不付定金",
    "不交订金",
    "先不交订金",
    "不用订金",
    "合适再谈定金",
    "满意再谈定金",
    "满意了再决定",
    "满意再决定",
    "看完满意再决定",
    "先看车再谈定金",
    "先看车和报告",
    "先看车试驾",
    "不想一上来就被催",
    "不想一上来就付",
    "不想一上来就交",
    "不被催付钱",
)
STRICT_BUDGET_CAP_TERMS = (
    "别超过",
    "别超",
    "不超过",
    "不超",
    "不能超过",
    "不要超过",
    "不高于",
    "别高于",
    "以内",
    "万内",
    "预算内",
    "以下",
    "控制在",
    "最多",
    "上限",
    "封顶",
)
IDENTITY_PROBE_TERMS = (
    "是不是ai",
    "是不是AI",
    "ai吗",
    "AI吗",
    "ai自动",
    "AI自动",
    "真人吗",
    "是真人",
    "机器人",
    "自动回复",
    "机器客服",
    "智能客服",
)
DETAILED_NEED_SOURCE_TERMS = (
    "帮我看个车",
    "看个车",
    "想买",
    "换台",
    "换车",
    "推荐",
    "挑",
    "筛",
    "车源",
    "什么车",
    "哪台",
    "哪款",
)
DETAILED_NEED_USE_TERMS = (
    "通勤",
    "代步",
    "家用",
    "接娃",
    "买菜",
    "周末",
    "全家",
    "孙子",
    "退休",
    "老婆开",
    "媳妇开",
    "女士开",
    "女司机",
    "新手",
    "练手",
    "停车",
    "跑高速",
    "后排",
    "空间",
    "摄影",
    "工作室",
    "活动策划",
    "外拍",
    "接客户",
    "甲方客户",
    "客户接待",
    "商务接待",
    "公司用",
    "拉器材",
    "拉东西",
    "物料",
    "展架",
    "爸妈",
    "父母",
    "孩子",
)
DETAILED_NEED_PREFERENCE_TERMS = (
    "自动挡",
    "手动挡",
    "倒车",
    "影像",
    "雷达",
    "360",
    "小一点",
    "小巧",
    "省油",
    "别太费油",
    "省心",
    "别太费心",
    "舒服",
    "舒适",
    "空间大",
    "后备厢",
    "后备箱",
    "器材",
    "灯架",
    "展架",
    "物料",
    "音响架",
    "折叠桌",
    "相机箱",
    "背景架",
    "能拉",
    "装东西",
    "拉东西",
    "suv",
    "皮实",
    "耐造",
)
USED_CAR_EXPLICIT_QUERY_TERMS = (
    "车",
    "车型",
    "车源",
    "二手车",
    "自动挡",
    "手动挡",
    "看车",
    "到店",
    "试驾",
    "上牌",
    "公里",
    "后备厢",
    "后备箱",
    "空间",
    "器材",
    "灯架",
    "相机箱",
    "置换",
    "收车",
)
USED_CAR_SCENE_QUERY_TERMS = (
    "通勤",
    "代步",
    "家用",
    "接娃",
    "省油",
    "油耗",
    "老婆开",
    "女士开",
    "哪台",
    "挑两",
    "挑几",
    "摄影",
    "工作室",
    "活动策划",
    "外拍",
    "接客户",
    "甲方客户",
    "客户接待",
    "商务接待",
    "公司用",
    "后备厢",
    "后备箱",
    "器材",
    "拉东西",
    "物料",
    "展架",
    "装东西",
    "suv",
    "皮实",
    "耐造",
    "工地",
)
USED_CAR_PRODUCT_TERMS = (
    "二手车",
    "轿车",
    "suv",
    "mpv",
    "新能源",
    "上牌",
    "表显",
    "公里",
    "自动挡",
    "手动挡",
    "现车",
    "车况",
    "检测报告",
    "检测",
    "内饰",
    "外观",
    "补漆",
    "一手车",
)
CONCRETE_USED_CAR_MODEL_TERMS: tuple[str, ...] = ()
HIGH_RISK_TERMS = (
    "最低价",
    "底价",
    "绝对",
    "保证",
    "包过",
    "赔",
    "赔偿",
    "事故",
    "水泡",
    "火烧",
    "定金",
    "订金",
    "锁车",
    "少开",
    "合同",
    "发票",
    "电池",
    "三电",
    "迁入政策",
)
STRICT_HIGH_RISK_TERMS = (
    "最低价",
    "底价",
    "绝对",
    "保证",
    "包过",
    "赔",
    "赔偿",
    "定金",
    "订金",
    "锁车",
    "少开",
    "合同",
    "发票",
    "迁入政策",
)
CONDITION_SENSITIVE_RISK_TERMS = ("事故", "水泡", "火烧", "电池", "三电")
GUARANTEE_CONTEXT_TERMS = ("保证", "绝对", "肯定", "包", "赔", "赔偿", "负责", "出问题", "没问题", "有没有问题")
OFF_TOPIC_OR_SECURITY_TERMS = (
    "外挂",
    "破解",
    "脚本",
    "系统提示词",
    "内部规则",
    "api密钥",
    "api key",
    "密钥",
    "prompt",
)
FAREWELL_TERMS = (
    "再见",
    "拜拜",
    "拜",
    "先这样",
    "先聊到这",
    "回头聊",
    "晚安",
    "改天聊",
    "下次聊",
)
POLITE_SMALLTALK_TERMS = (
    "谢谢",
    "感谢",
    "辛苦了",
    "麻烦你了",
)
OFFTOPIC_SOCIAL_TERMS = (
    "天气",
    "下雨",
    "吃饭",
    "午饭",
    "晚饭",
    "早餐",
    "电影",
    "电视剧",
    "综艺",
    "音乐",
    "星座",
    "宠物",
    "旅游",
    "减肥",
    "美食",
    "游戏",
    "笑话",
    "八卦",
    "忙不忙",
    "在吗",
    "在不在",
)


def decide_realtime_reply_route(
    *,
    config: dict[str, Any],
    combined: str,
    decision: Any,
    intent_result: Any,
    intent_assist: dict[str, Any],
    rag_reply: dict[str, Any],
    llm_reply: dict[str, Any],
    product_knowledge: dict[str, Any] | None,
    data_capture: dict[str, Any],
    evidence_pack: dict[str, Any],
    recent_reply_texts: list[str] | None = None,
) -> dict[str, Any]:
    settings = config.get("realtime_reply", {}) or {}
    enabled = settings.get("enabled", True) is not False
    route: dict[str, Any] = {
        "enabled": enabled,
        "level": "disabled",
        "reason": "realtime_reply_disabled",
        "foreground_llm_allowed": bool((config.get("llm_reply_synthesis", {}) or {}).get("enabled", False)),
        "max_prompt_tokens": int(settings.get("max_prompt_tokens", DEFAULT_MAX_PROMPT_TOKENS) or DEFAULT_MAX_PROMPT_TOKENS),
        "max_completion_tokens": int(settings.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS) or DEFAULT_MAX_COMPLETION_TOKENS),
        "background_jobs": [],
    }
    if not enabled:
        return route

    current_combined = current_customer_text(combined)
    contextual_combined = realtime_contextual_customer_text(
        combined,
        recent_reply_texts=recent_reply_texts or [],
    )
    text = normalize_text(current_combined)
    source_text = normalize_text(contextual_combined)
    llm_synthesis_enabled = bool((config.get("llm_reply_synthesis", {}) or {}).get("enabled", False))
    common_sense_advisor_enabled = settings.get("common_sense_advisor_enabled", True) is not False
    common_sense_advisor_allowed = bool(settings.get("allow_foreground_llm", True)) and llm_synthesis_enabled and common_sense_advisor_enabled
    business_local_style_refinement_allowed = (
        common_sense_advisor_allowed
        and bool(settings.get("business_local_style_foreground_llm_enabled", False))
    )

    def _common_sense_advisor_route(
        reason: str,
        *,
        advisor_mode: str = "clear_common_sense_recommendation",
        advisor_goal: str = "give_a_clear_recommendation_when_safe",
        advisor_max_reply_chars: int = 0,
    ) -> dict[str, Any]:
        payload = {
            **route,
            "level": "L1",
            "reason": reason,
            "foreground_llm_allowed": common_sense_advisor_allowed,
            "background_jobs": ["reply_quality_review"],
        }
        if common_sense_advisor_allowed:
            payload["advisor_mode"] = advisor_mode
            payload["advisor_goal"] = advisor_goal
            if advisor_max_reply_chars > 0:
                payload["advisor_max_reply_chars"] = int(advisor_max_reply_chars)
        return payload

    def _business_local_style_route(
        reason: str,
        *,
        background_jobs: list[str] | None = None,
        advisor_mode: str = "direct_question_resolution",
        advisor_goal: str = "refine_local_business_draft_with_grounded_direct_answer",
        advisor_max_reply_chars: int = 140,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            **route,
            "level": "L1",
            "reason": reason,
            "foreground_llm_allowed": business_local_style_refinement_allowed,
            "background_jobs": background_jobs or ["reply_quality_review"],
        }
        if extra:
            payload.update(extra)
        if business_local_style_refinement_allowed:
            payload["advisor_mode"] = advisor_mode
            payload["advisor_goal"] = advisor_goal
            if advisor_max_reply_chars > 0:
                payload["advisor_max_reply_chars"] = int(advisor_max_reply_chars)
        return payload

    if is_identity_probe(text) and not contains_any(text, HIGH_RISK_TERMS):
        return {
            **route,
            "level": "L1",
            "reason": "identity_probe_can_use_local_style",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    intent_primary = str(getattr(intent_result, "intent", "") or "").strip()
    intent_assisted = str(intent_assist.get("intent") or "").strip()
    intent = intent_primary or intent_assisted
    if (
        intent_primary in {"", "unknown", "unclear"}
        or (intent_primary == "general_chat" and intent_assisted in {"greeting", "small_talk"})
    ) and intent_assisted:
        intent = intent_assisted
    safety = ((intent_assist.get("evidence", {}) or {}).get("safety", {}) or {})
    safety_reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    soft_missing_evidence = bool(safety.get("must_handoff")) and bool(safety_reasons) and safety_reasons <= {"no_relevant_business_evidence", "auto_reply_disabled"}
    route["soft_missing_evidence"] = soft_missing_evidence
    route["soft_missing_evidence_reasons"] = sorted(safety_reasons) if soft_missing_evidence else []
    decision_requires_handoff = bool(getattr(decision, "need_handoff", False)) and str(getattr(decision, "reason", "") or "") != "no_rule_matched"
    must_handoff = (bool(safety.get("must_handoff")) and not soft_missing_evidence) or decision_requires_handoff
    product_block = bool(product_knowledge and (product_knowledge.get("needs_handoff") or product_knowledge.get("auto_reply_allowed") is False))

    if requires_high_risk_boundary(text) or contains_any(text, OFF_TOPIC_OR_SECURITY_TERMS):
        return {
            **route,
            "level": "L0",
            "reason": "deterministic_handoff_or_high_risk_boundary",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if product_block:
        return {
            **route,
            "level": "L0",
            "reason": "deterministic_handoff_or_high_risk_boundary",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if intent == "customer_data_provide" or is_actual_customer_data_message(text):
        return {
            **route,
            "level": "L0",
            "reason": "customer_data_or_contact_message",
            "foreground_llm_allowed": False,
            "background_jobs": ["customer_profile_update"],
        }
    if is_friendly_farewell_message(text):
        return {
            **route,
            "level": "L1",
            "reason": "friendly_farewell",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if is_short_greeting(text) or (intent == "greeting" and not has_business_signal(text)):
        return {
            **route,
            "level": "L1",
            "reason": "friendly_social_greeting",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if not soft_missing_evidence and (rag_reply.get("applied") or bool((evidence_pack.get("rag_evidence", {}) or {}).get("hits"))):
        return {
            **route,
            "level": "L1",
            "reason": "rag_or_style_experience_available",
            "foreground_llm_allowed": False,
            "background_jobs": ["rag_experience_audit"],
        }
    if intent == "small_talk" and not has_business_signal(text):
        return {
            **route,
            "level": "L1",
            "reason": "small_talk",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if is_offtopic_social_message(text, recent_reply_texts=recent_reply_texts or []):
        return _common_sense_advisor_route(
            "offtopic_soft_redirect_needs_light_synthesis",
            advisor_mode="soft_topic_redirect",
            advisor_goal="respond_warmly_then_redirect_to_used_car",
            advisor_max_reply_chars=140,
        )
    if is_compound_multi_question_message(current_combined):
        return _common_sense_advisor_route(
            "multi_question_compound_requires_synthesis",
            advisor_mode="multi_question_compound_answer",
            advisor_goal="cover_all_recent_questions_concisely",
            advisor_max_reply_chars=140,
        )
    if is_customer_challenge_direct_answer_request(text):
        return _common_sense_advisor_route(
            "customer_challenge_needs_direct_answer",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_latest_question_directly_without_repeating_catalog",
            advisor_max_reply_chars=120,
        )
    current_priority_guidance = current_turn_preempts_vehicle_candidate_reply(text, recent_reply_texts or [])
    if (
        current_priority_guidance
        and source_text != text
        and current_guidance_can_yield_to_contextual_candidates(text)
        and contextual_vehicle_need_ready_for_candidates(source_text, recent_reply_texts or [])
    ):
        current_priority_guidance = False
    compare_question = is_vehicle_compare_question(text, recent_reply_texts or [])
    if compare_question and is_finance_guidance_question(text) and not contains_product_term(text, include_generic=False):
        compare_question = False
    if (
        compare_question
        and is_vehicle_type_guidance_question(text)
        and not is_existing_vehicle_option_compare(text)
        and (
            not has_named_compare_options(text)
            or not has_specific_customer_compare_option(text)
            or contains_any(text, ("哪个方向", "这类", "哪类", "哪一类"))
        )
    ):
        compare_question = False
    if is_catalog_price_superlative_request(text) or is_catalog_price_superlative_request(source_text):
        return _business_local_style_route(
            "catalog_price_superlative_available",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_most_expensive_product_from_product_master",
            advisor_max_reply_chars=120,
        )
    if is_catalog_price_list_request(text) or is_catalog_price_list_request(source_text):
        return _business_local_style_route(
            "catalog_price_list_available",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_public_quote_list_from_product_master",
            advisor_max_reply_chars=140,
        )
    if is_direct_product_price_query(text) or is_direct_product_price_query(source_text):
        return _common_sense_advisor_route(
            "direct_product_price_requires_synthesis",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_product_quote_from_product_master_or_context",
            advisor_max_reply_chars=96,
        )
    if not current_priority_guidance and (
        is_followup_vehicle_source_request(text, recent_reply_texts or [])
        or is_followup_vehicle_source_request(source_text, recent_reply_texts or [])
    ) and not compare_question:
        return _business_local_style_route(
            "followup_ready_for_vehicle_candidates",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_without_repeating_old_catalog",
        )
    if (
        not current_priority_guidance
        and not compare_question
        and (
            is_explicit_vehicle_source_request(text)
            or is_explicit_vehicle_source_request(source_text)
            or is_inventory_category_availability_request(text)
            or is_inventory_category_availability_request(source_text)
        )
    ):
        return _business_local_style_route(
            "explicit_vehicle_candidates_requested",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_matching_latest_need",
        )
    if (
        not current_priority_guidance
        and not compare_question
        and (is_detailed_vehicle_need_ready(text) or is_detailed_vehicle_need_ready(source_text))
    ):
        return _business_local_style_route(
            "detailed_vehicle_need_ready_for_candidates",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_matching_latest_need",
        )
    if is_testdrive_material_question(text):
        return _business_local_style_route("common_testdrive_materials_can_use_local_style")
    if compare_question:
        current_compare_products = extract_current_catalog_compare_products(text)
        recent_compare_products = extract_recent_catalog_compare_products(recent_reply_texts or [], limit=2) if is_existing_vehicle_option_compare(text) else []
        if len(current_compare_products) >= 2 or recent_compare_products:
            return _business_local_style_route(
                "common_vehicle_compare_can_use_local_style",
                advisor_mode="catalog_grounded_compare",
                advisor_goal="compare_only_mentioned_catalog_options_and_pick_when_safe",
            )
        return _common_sense_advisor_route("common_vehicle_compare_can_use_local_style")
    if is_vehicle_type_guidance_question(text):
        return _common_sense_advisor_route("common_vehicle_type_guidance_can_use_local_style")
    if is_maintenance_cost_question(text):
        return _business_local_style_route("common_maintenance_cost_can_use_local_style")
    if is_fee_transparency_question(text):
        return _business_local_style_route("common_fee_guidance_can_use_local_style")
    if is_finance_guidance_question(text):
        return _business_local_style_route("common_finance_guidance_can_use_local_style")
    if is_specific_testdrive_appointment_question(text):
        return {
            **route,
            "level": "L0",
            "reason": "deterministic_handoff_or_high_risk_boundary",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if is_visit_report_prep_question(text):
        return _business_local_style_route("common_visit_timing_can_use_local_style")
    if (
        contains_any(text, TRADE_IN_TERMS)
        and contains_any(text, ("现场估", "现场评估", "开过去", "老车", "估价", "估一下"))
        and not contains_any(text, EXPLICIT_SOURCE_TERMS + ("推荐", "优先看", "值得看", "更想看", "想看", "备选", "当备选"))
    ):
        return _business_local_style_route(
            "common_trade_in_collect_can_use_local_style",
            background_jobs=["customer_profile_update", "reply_quality_review"],
        )
    if is_inspection_guidance_question(text):
        return _business_local_style_route("common_inspection_guidance_can_use_local_style")
    if is_cargo_space_guidance_question(text):
        return _business_local_style_route("common_cargo_space_guidance_can_use_local_style")
    if is_comfort_highway_question(text):
        return _business_local_style_route("common_comfort_highway_guidance_can_use_local_style")
    if is_feature_guidance_question(text):
        return _business_local_style_route("common_feature_guidance_can_use_local_style")
    if is_price_negotiation_question(text):
        return _business_local_style_route("common_price_negotiation_can_use_local_style")
    if is_warranty_guidance_question(text):
        return _business_local_style_route("common_warranty_guidance_can_use_local_style")
    if is_visit_timing_question(text):
        return _business_local_style_route("common_visit_timing_can_use_local_style")
    if is_no_deposit_visit_question(text):
        return _business_local_style_route("common_no_deposit_visit_can_use_local_style")
    if must_handoff:
        return {
            **route,
            "level": "L0",
            "reason": "deterministic_handoff_or_high_risk_boundary",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if is_short_greeting(text) or (intent == "greeting" and not has_business_signal(text)):
        return {
            **route,
            "level": "L1",
            "reason": "friendly_social_greeting",
            "foreground_llm_allowed": False,
            "background_jobs": ["reply_quality_review"],
        }
    if is_catalog_price_superlative_request(text) or is_catalog_price_superlative_request(source_text):
        return _business_local_style_route(
            "catalog_price_superlative_available",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_most_expensive_product_from_product_master",
            advisor_max_reply_chars=120,
        )
    if is_catalog_price_list_request(text) or is_catalog_price_list_request(source_text):
        return _business_local_style_route(
            "catalog_price_list_available",
            advisor_mode="direct_question_resolution",
            advisor_goal="answer_public_quote_list_from_product_master",
            advisor_max_reply_chars=140,
        )
    if product_knowledge and product_knowledge.get("matched") and not product_knowledge.get("needs_handoff"):
        return {
            **route,
            "level": "L0",
            "reason": "structured_product_fact_available",
            "foreground_llm_allowed": False,
        }
    if not compare_question and (is_followup_vehicle_source_request(text, recent_reply_texts or []) or is_followup_vehicle_source_request(source_text, recent_reply_texts or [])):
        return _business_local_style_route(
            "followup_ready_for_vehicle_candidates",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_without_repeating_old_catalog",
        )
    if not compare_question and (
        is_explicit_vehicle_source_request(text)
        or is_explicit_vehicle_source_request(source_text)
        or is_inventory_category_availability_request(text)
        or is_inventory_category_availability_request(source_text)
    ):
        return _business_local_style_route(
            "explicit_vehicle_candidates_requested",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_matching_latest_need",
        )
    if not compare_question and (is_detailed_vehicle_need_ready(text) or is_detailed_vehicle_need_ready(source_text)):
        return _business_local_style_route(
            "detailed_vehicle_need_ready_for_candidates",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_matching_latest_need",
        )
    if contains_any(text, VALUE_RETENTION_TERMS):
        return _business_local_style_route("common_value_retention_followup_can_use_local_style")
    if contains_any(text, NEW_ENERGY_CHECK_TERMS):
        return _business_local_style_route("common_new_energy_check_can_use_local_style")
    if contains_any(text, TRADE_IN_TERMS):
        return _business_local_style_route(
            "common_trade_in_collect_can_use_local_style",
            background_jobs=["customer_profile_update", "reply_quality_review"],
        )
    if is_appointment_context_query(text):
        return _business_local_style_route("common_visit_timing_can_use_local_style")
    if contains_any(text, FOLLOWUP_TERMS) and len(text) >= 8:
        return {
            **route,
            "level": "L2",
            "reason": "multi_turn_reference_needs_light_synthesis",
            "foreground_llm_allowed": bool(settings.get("allow_foreground_llm", True)),
            "background_jobs": ["conversation_summary"],
        }
    if contains_any(text, RECOMMEND_TERMS):
        return _business_local_style_route(
            "common_recommendation_can_use_local_candidates",
            advisor_mode="clear_common_sense_recommendation",
            advisor_goal="recommend_catalog_candidates_matching_latest_need",
        )
    if not soft_missing_evidence and (rag_reply.get("applied") or bool((evidence_pack.get("rag_evidence", {}) or {}).get("hits"))):
        return {
            **route,
            "level": "L1",
            "reason": "rag_or_style_experience_available",
            "foreground_llm_allowed": False,
            "background_jobs": ["rag_experience_audit"],
        }

    return {
        **route,
        "level": "L1",
        "reason": "uncertain_message_light_synthesis_allowed",
        "foreground_llm_allowed": False,
        "background_jobs": ["reply_quality_review"],
    }


def requires_high_risk_boundary(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(clean, ("不要求你保证", "不用保证", "不需要保证", "不是让你保证")):
        clean = clean.replace("保证", "")
    if contains_any(clean, NO_DEPOSIT_VISIT_TERMS):
        clean = clean.replace("定金", "").replace("订金", "")
    if contains_any(clean, STRICT_HIGH_RISK_TERMS):
        return True
    if is_store_arrival_contact_question(clean):
        return True
    if is_same_day_delivery_or_transaction_question(clean):
        return True
    if is_specific_price_approval_question(clean):
        return True
    if not contains_any(clean, CONDITION_SENSITIVE_RISK_TERMS):
        return False
    if contains_any(clean, ("检测", "检测报告", "记录", "三电记录", "怎么看", "怎么查", "一般怎么看")) and not contains_any(clean, ("保证", "绝对", "肯定", "负责", "赔", "赔偿")):
        return False
    return contains_any(clean, GUARANTEE_CONTEXT_TERMS)


def is_specific_price_approval_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    has_specific_target = bool(re.search(r"\d+(?:\.\d+)?\s*(?:万|整)", clean))
    if not has_specific_target:
        return False
    return contains_any(
        clean,
        (
            "帮我问",
            "问问",
            "能不能谈",
            "谈到",
            "能不能给",
            "给到",
            "最低",
            "底价",
            "现在定",
            "今天定",
            "直接定",
            "马上定",
            "能少",
            "少点",
            "便宜点",
            "优惠点",
            "能优惠",
        ),
    )


def is_same_day_delivery_or_transaction_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, SAME_DAY_DELIVERY_TERMS):
        return False
    transaction_context = (
        "当天",
        "今天",
        "马上",
        "直接",
        "能不能",
        "可以吗",
        "行不行",
        "试驾没问题",
        "看完",
        "办手续",
        "资料齐",
    )
    return contains_any(clean, transaction_context)


def is_store_arrival_contact_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, STORE_ARRIVAL_HANDOFF_TERMS):
        return False
    visit_context = ("你们店", "门店", "店里", "到店", "到了", "过去", "看车", "试驾", "跑错", "导航")
    return contains_any(clean, visit_context)


def is_actual_customer_data_message(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", clean):
        return True
    if re.search(r"(?:我叫|我姓|本人|联系人|姓名)\s*[\u4e00-\u9fff·]{1,8}", clean):
        return True
    return contains_any(clean, ("客户资料", "客户信息", "收货信息", "联系电话", "手机号"))


def is_vehicle_compare_question(text: str, recent_reply_texts: list[str]) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if is_testdrive_material_question(clean):
        return False
    if contains_any(clean, STRICT_HIGH_RISK_TERMS) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    # Follow-up wording like "这两个更推荐哪一辆" should still hit compare.
    if is_existing_vehicle_option_compare(clean):
        return True
    if is_single_choice_compare_request(clean) and len(meaningful_customer_compare_options(clean)) >= 2:
        return True
    has_named_options = has_named_compare_options(clean)
    if not has_named_options and not contains_any(clean, VEHICLE_COMPARE_TERMS):
        return False
    if not has_named_options and contains_any(clean, EXPLICIT_SOURCE_TERMS) and not is_existing_vehicle_option_compare(clean):
        return False
    # Category tradeoffs like "sedan vs SUV for a two-child family" should be
    # answered as vehicle-type guidance, not hijacked by previously mentioned
    # concrete candidates.
    if (
        contains_any(clean, ("轿车", "suv", "mpv", "七座", "车型"))
        and not contains_catalog_product_term(clean)
        and not has_specific_customer_compare_option(clean)
    ):
        return False
    recent = normalize_text(" ".join(recent_reply_texts[-4:]))
    return (
        has_named_options
        or contains_catalog_product_term(clean)
        or contains_any(clean, USED_CAR_SCENE_QUERY_TERMS + USED_CAR_EXPLICIT_QUERY_TERMS + USED_CAR_PRODUCT_TERMS)
        or contains_any(clean, COMMON_VEHICLE_BRAND_OR_MODEL_TERMS)
        or contains_used_car_product_signal(recent)
    )


def is_existing_vehicle_option_compare(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    existing_option_refs = (
        "这两台",
        "这几台",
        "这两款",
        "这几款",
        "这两个",
        "这几个",
        "两台里",
        "几台里",
        "两款里",
        "几款里",
        "上面两台",
        "前面两台",
        "刚才两台",
    )
    compare_markers = ("哪个", "哪台", "哪款", "哪一辆", "更适合", "更好", "怎么选", "好停", "好开", "停车")
    return contains_any(clean, existing_option_refs) and contains_any(clean, compare_markers)


def has_specific_customer_compare_option(text: str) -> bool:
    category_options = {
        "轿车",
        "suv",
        "mpv",
        "七座",
        "七座车",
        "车型",
        "油车",
        "电车",
        "新能源",
        "纯电",
        "混动",
    }
    filler_terms = (
        "如果",
        "也考虑",
        "都能接受",
        "能接受",
        "可以接受",
        "考虑",
        "哪种",
        "哪类",
        "哪一类",
        "这种",
        "那种",
    )
    for normalized in meaningful_customer_compare_options(text):
        if normalized.lower() in category_options or normalized in category_options:
            continue
        return True
    return False


def has_named_compare_options(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if not contains_any(clean, ("和", "跟", "与", "、", "还是", "或者", "对比", "比较")):
        return False
    option_markers = (
        "哪个",
        "哪台",
        "哪款",
        "哪一辆",
        "哪一类",
        "先看",
        "更适合",
        "更好",
        "怎么选",
        "建议",
        "优先顺序",
        "先后顺序",
        "排顺序",
        "定一个优先",
    )
    if not contains_any(clean, option_markers):
        return False
    # "按商品库建议先看哪两台" is a source recommendation request, not
    # a comparison between named options. Require at least two concrete
    # customer-provided options before taking the compare route.
    return len(meaningful_customer_compare_options(clean)) >= 2


def meaningful_customer_compare_options(text: str) -> list[str]:
    generic_terms = {
        "",
        "哪台",
        "哪一辆",
        "哪款",
        "哪两台",
        "哪几台",
        "哪种",
        "哪类",
        "哪一类",
        "哪个方向",
        "哪类更合适",
        "哪种更合适",
        "两台",
        "几台",
        "推荐两台",
        "推荐几台",
        "商品库",
        "现车",
        "车源",
        "优先顺序",
        "先后顺序",
        "给我明确建议",
        "明确建议",
    }
    filler_terms = (
        "如果",
        "优先",
        "你会",
        "您会",
        "你",
        "您",
        "我想",
        "我想看",
        "我更想看",
        "也考虑",
        "都能接受",
        "能接受",
        "可以接受",
        "考虑",
        "建议我",
        "建议",
        "明确建议",
        "给我明确建议",
        "给我明确",
        "明确",
        "优先顺序",
        "先后顺序",
        "排顺序",
        "定一个优先顺序",
        "定一个顺序",
        "顺序",
        "先看",
        "推荐",
        "帮我",
        "哪种",
        "哪类",
        "哪一类",
        "更合适",
        "按现在商品库",
        "商品库",
        "现在",
    )
    options: list[str] = []
    for option in extract_customer_compare_options(text):
        normalized = normalize_text(option).strip(" ？?。.!！：:")
        normalized = re.sub(r"^(?:那|这)(?=[\u4e00-\u9fffA-Za-z0-9]{2,})", "", normalized).strip(" ？?。.!！：:")
        for term in filler_terms:
            normalized = normalized.replace(term, "")
        normalized = re.sub(r"(你|您)$", "", normalized).strip(" ？?。.!！：:")
        normalized = normalized.strip(" ？?。.!！：:")
        if not normalized or normalized in generic_terms:
            continue
        if normalized.lower() in generic_terms:
            continue
        if contains_any(normalized, ("预算", "车况", "油耗", "家用", "通勤", "省心", "透明", "别太高")) and not contains_catalog_product_term(normalized):
            continue
        if contains_any(normalized, ("跑客户", "放样品", "二胎", "家庭", "接娃", "带孩子", "买菜", "老人孩子")) and not contains_catalog_product_term(normalized):
            continue
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9.\- ]{1,23}", normalized):
            if normalized not in options:
                options.append(normalized)
    return options


def extract_customer_compare_options(text: str) -> list[str]:
    raw = current_customer_text(text)
    if not raw:
        return []
    fragment = raw
    # Keep the concrete options before tail markers like "建议先看哪款".
    cut_markers = ("哪个", "哪台", "哪款", "哪一辆", "哪一类", "怎么选", "更适合", "更好")
    cut_positions = [fragment.find(marker) for marker in cut_markers if marker in fragment]
    if cut_positions:
        first_cut = min(pos for pos in cut_positions if pos >= 0)
        if first_cut > 0:
            fragment = fragment[:first_cut]
    if "里面" in fragment and "在" in fragment:
        fragment = fragment.rsplit("在", 1)[-1].split("里面", 1)[0]
    elif "先看" in fragment:
        left, right = fragment.rsplit("先看", 1)
        right_key = normalize_text(right)
        if (
            contains_any(right_key, ("和", "跟", "与", "还是", "或者", "/", "、"))
            or contains_catalog_product_term(right_key)
        ) and len(right.strip()) >= 2:
            fragment = right
        else:
            fragment = left if left.strip() else fragment
    for marker in ("如果", "优先", "你会", "您会", "我想", "我想看", "我更想看"):
        fragment = fragment.replace(marker, "")
    for separator in ("还是", "或者", "跟", "与", "和", "/", "，", ",", "；", ";", "。", "！", "？"):
        fragment = fragment.replace(separator, "、")
    raw_options = [item.strip(" ？?。.!！：:") for item in fragment.split("、")]
    ignored = {"", "哪个", "哪台", "哪款", "哪一辆", "哪一类", "选", "里面选", "你建议我", "您建议我"}
    options: list[str] = []
    for item in raw_options:
        item = re.sub(r"^(?:你?答非所问|我问的是|问的是|你直接给结论|直接给结论|给结论)\s*[，,:：]*", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"^(?:我打错了|打错了|我写错了|写错了|更正一下|更正下|更正|纠正一下|纠正下)\s*[，,:：]*", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"^(?:不是)\s*", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"^(?:是)\s*", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"^(?:那你|那您|那|你|您)?在", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"^(?:你好|您好|哈喽|嗨|我想看|我更想看|我想|想看|优先|建议|先看|看)+", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"(?:你|您|建议|先看|哪个|哪台|哪款|哪一辆|更适合|更好|怎么选|吗)$", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"(?:二选一)$", "", item).strip(" ？?。.!！：:")
        item = re.sub(r"(?:里)?(?:先)?(?:帮我)?(?:定一个|定下|排个|排一下)?(?:优先)?顺序$", "", item).strip(" ？?。.!！：:")
        if item in ignored or len(item) > 24:
            continue
        if contains_any(normalize_text(item), ("答非所问", "我问的是", "问的是", "直接给结论")):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", item):
            continue
        if item not in options:
            options.append(item)
    return options[:8]


def is_vehicle_type_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, VEHICLE_TYPE_GUIDANCE_TERMS):
        return False
    if contains_any(clean, EXPLICIT_SOURCE_TERMS + ("哪两台", "哪台", "推荐", "具体车源", "锁定两台")):
        return False
    if requires_high_risk_boundary(clean) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    guidance_markers = (
        "建议",
        "怎么选",
        "怎么建议",
        "先看",
        "哪类",
        "哪种",
        "哪个方向",
        "哪个更",
        "会不会",
        "大不大",
        "压力",
        "太大",
        "适合",
        "纠结",
        "取舍",
    )
    return contains_any(clean, guidance_markers)


def is_maintenance_cost_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, MAINTENANCE_COST_TERMS):
        return False
    if contains_any(clean, EXPLICIT_SOURCE_TERMS + ("哪两台", "哪台", "推荐", "具体车源", "锁定两台")):
        return False
    if requires_high_risk_boundary(clean) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    return contains_any(clean, ("差多少", "差别", "贵不贵", "成本", "保养", "维修", "后期", "养车", "费用", "油耗", "小毛病", "少操心"))


def is_inspection_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, INSPECTION_GUIDANCE_TERMS):
        return False
    if requires_high_risk_boundary(clean):
        return False
    return contains_any(clean, ("怎么", "怎么看", "把关", "报告", "记录", "能不能看", "能不能带", "一起看", "第三方", "不踏实", "只听", "有没有", "实际", "只听口头", "具体", "怕", "补漆", "换件", "说清楚", "披露", "事故车"))


def is_feature_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, FEATURE_GUIDANCE_TERMS):
        return False
    if is_comfort_highway_question(clean):
        return False
    if contains_any(clean, EXPLICIT_SOURCE_TERMS + ("哪两台", "哪台", "推荐", "具体车源")):
        return False
    if requires_high_risk_boundary(clean):
        return False
    return contains_any(clean, ("安全配置", "倒车影像", "倒车雷达", "雷达", "360", "气囊", "esp", "辅助驾驶", "重点看", "是不是", "要不要", "安全", "配置"))


def is_comfort_highway_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, COMFORT_HIGHWAY_TERMS):
        return False
    if contains_any(clean, EXPLICIT_SOURCE_TERMS + ("哪两台", "哪台", "推荐", "具体车源", "锁定两台")):
        return False
    if requires_high_risk_boundary(clean) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    return contains_any(clean, ("重点看", "应该看", "怎么看", "会不会", "颠", "舒服", "舒适", "隔音", "胎噪", "高速", "后排"))


def is_cargo_space_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, CARGO_SPACE_GUIDANCE_TERMS):
        return False
    if contains_any(clean, EXPLICIT_SOURCE_TERMS + ("哪两台", "哪台", "推荐", "具体车源", "锁定两台")):
        return False
    if requires_high_risk_boundary(clean) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    return contains_any(clean, ("能不能", "能装", "装", "放", "尺寸", "容积", "够不够", "够用", "实用", "现场", "实车", "第二排", "二排", "后备"))


def is_finance_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, FINANCE_GUIDANCE_TERMS):
        return False
    if contains_any(clean, ("不要求你保证", "不用保证", "不需要保证", "不是让你保证")):
        clean = clean.replace("保证", "")
    if contains_any(clean, ("包过", "保证", "肯定能批", "一定能批", "最低首付", "零首付")):
        return False
    return contains_any(
        clean,
        (
            "大概",
            "范围",
            "流程",
            "多久",
            "麻烦",
            "怎么算",
            "谁算",
            "谁给我算",
            "谁给算",
            "谁来算",
            "谁负责算",
            "谁负责",
            "谁给",
            "方案是谁",
            "测算",
            "准备",
            "手续",
            "首付",
            "月供",
            "征信",
        ),
    )


def is_fee_transparency_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, FEE_GUIDANCE_TERMS):
        return False
    if contains_any(clean, ("少开", "低开", "合同", "发票")):
        return False
    return contains_any(clean, ("费用", "提前", "说清楚", "明细", "上牌", "过户", "保险", "加费用", "收费"))


def is_testdrive_material_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, TESTDRIVE_MATERIAL_TERMS):
        return False
    if contains_any(clean, ("押金", "定金", "订金", "锁车", "保证")):
        return False
    material_specific = contains_any(clean, ("资料", "证件", "身份证", "驾驶证", "带什么", "准备什么", "准备哪些", "材料"))
    if contains_any(clean, ("几点", "时间", "周日", "周六", "下午", "上午")) and not material_specific:
        return False
    if "白跑" in clean and not material_specific:
        return False
    return contains_any(
        clean,
        ("带", "材料", "资料", "身份证", "驾驶证", "证件", "需要带", "准备什么", "准备哪些", "什么资料", "哪些资料"),
    )


def is_specific_testdrive_appointment_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, ("试驾", "试车")):
        return False
    if is_testdrive_material_question(clean):
        return False
    return contains_any(clean, ("今天", "明天", "周六", "周日", "上午", "下午", "晚上", "几点", "时间", "到店", "过去", "预约", "安排", "能看", "能不能看"))


def is_visit_timing_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not (contains_any(clean, APPOINTMENT_TERMS) or is_vehicle_availability_confirmation(clean)):
        return False
    if contains_any(clean, ("定金", "订金", "锁车", "留车")) or is_actual_customer_data_message(clean):
        return False
    if is_visit_report_prep_question(clean):
        return True
    if is_vehicle_availability_confirmation(clean):
        return True
    return contains_any(
        clean,
        (
            "几点",
            "时间",
            "今天",
            "明天",
            "周一",
            "周二",
            "周三",
            "周四",
            "周五",
            "周六",
            "周日",
            "上午",
            "下午",
            "晚上",
            "白跑",
            "稳",
            "一次看",
            "对比",
            "试驾",
            "试车",
            "预约",
            "安排",
            "过去",
            "到店",
        ),
    )


def is_visit_report_prep_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not (contains_any(clean, APPOINTMENT_TERMS) or is_vehicle_availability_confirmation(clean)):
        return False
    if requires_high_risk_boundary(clean):
        return False
    return is_vehicle_availability_confirmation(clean) or contains_any(clean, ("检测报告", "车准备好", "准备好", "提前把", "提前准备", "车源状态"))


def is_vehicle_availability_confirmation(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(clean, VEHICLE_AVAILABILITY_CONFIRM_TERMS):
        return True
    return contains_any(clean, ("在不在", "还在吗", "还在不")) and contains_any(clean, ("车", "车源", "现车", "这台", "那台"))


def is_no_deposit_visit_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, NO_DEPOSIT_VISIT_TERMS):
        return False
    if contains_any(clean, ("最低价", "底价", "包过", "保证", "赔偿", "合同", "发票", "少开", "迁入政策")):
        return False
    return contains_any(clean, ("先看车", "看车", "报告", "检测", "满意", "再谈", "流程", "可以吧", "行不行"))


def is_price_negotiation_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    # "车型/这辆 + 多少钱/报价" is a direct product quote intent. Do not route
    # it through local negotiation templates; the synthesis layer must ground
    # the answer in product master/context and final visible polish.
    if is_direct_product_price_query(clean):
        return False
    if not contains_any(clean, PRICE_NEGOTIATION_TERMS):
        return False
    if contains_any(clean, NO_DEPOSIT_VISIT_TERMS):
        return False
    if contains_any(clean, ("最低价", "底价", "今天就定", "现在就定", "保证", "绝对", "包过")):
        return False
    return contains_any(clean, ("能不能", "还能", "谈", "优惠", "贵", "少", "便宜", "现场", "合适"))


def is_direct_product_price_query(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, PRICE_QUERY_TERMS):
        return False
    if "同价位" in clean and not contains_any(clean, ("多少钱", "什么价", "报价", "价格多少", "发报价", "发价格")):
        return False
    if is_catalog_price_list_request(clean) and not (contains_catalog_product_term(clean) or re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2,16}(多少钱|什么价|价格多少)", clean)):
        return False
    if contains_any(clean, ("最低", "底价", "优惠", "便宜", "少点", "少一点", "包过", "赔", "赔偿", "保证", "绝对")):
        return False
    if contains_catalog_product_term(clean) or contains_used_car_product_signal(clean):
        return True
    model_price_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,16})(多少钱|什么价|报价|价格)", clean)
    if model_price_match:
        candidate = normalize_text(model_price_match.group(1))
        generic_prefixes = ("估个", "估一", "准", "预算", "车款", "抵", "给我", "这边", "你们", "现在")
        if candidate and not contains_any(candidate, generic_prefixes):
            return True
    return contains_any(clean, ("这台", "那台", "这辆", "那辆", "刚才那台", "刚推荐", "上面那台"))


def is_catalog_price_superlative_request(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or requires_high_risk_boundary(clean):
        return False
    return contains_any(clean, CATALOG_PRICE_SUPERLATIVE_TERMS) and contains_any(clean, ("车", "车型", "车源", "款"))


def is_catalog_price_list_request(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or requires_high_risk_boundary(clean):
        return False
    if contains_any(clean, ("最低", "底价", "优惠", "便宜", "少点", "少一点", "包过", "保证", "绝对")):
        return False
    if not contains_any(clean, CATALOG_PRICE_LIST_TERMS):
        return False
    return contains_any(
        clean,
        (
            "车",
            "车型",
            "车源",
            "价格",
            "价",
            "报价",
            "标价",
            "贷款",
            "分期",
            "预算",
            "手续",
            "车况",
            "合适",
            "看车",
        ),
    )


def is_warranty_guidance_question(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or not contains_any(clean, WARRANTY_GUIDANCE_TERMS):
        return False
    if contains_any(clean, ("赔偿", "退车", "退款", "维权", "投诉", "保证", "绝对")):
        return False
    return contains_any(clean, ("怎么", "具体", "理解", "找谁", "买完", "售后", "质保", "保修", "出问题"))


def is_service_guidance_question(text: str) -> bool:
    return any(
        checker(text)
        for checker in (
            is_fee_transparency_question,
            is_finance_guidance_question,
            is_testdrive_material_question,
            is_inspection_guidance_question,
            is_feature_guidance_question,
            is_comfort_highway_question,
            is_vehicle_type_guidance_question,
            is_maintenance_cost_question,
            is_no_deposit_visit_question,
            is_price_negotiation_question,
            is_warranty_guidance_question,
            is_visit_timing_question,
        )
    )


def maybe_build_realtime_reply(
    *,
    config: dict[str, Any],
    route: dict[str, Any],
    combined: str,
    evidence_pack: dict[str, Any],
    current_reply_text: str,
    recent_reply_texts: list[str] | None = None,
) -> dict[str, Any]:
    if route.get("enabled") is False or route.get("level") != "L1":
        return {"applied": False, "reason": "route_not_l1"}
    current_combined = current_customer_text(combined)
    contextual_combined = realtime_contextual_customer_text(
        combined,
        recent_reply_texts=recent_reply_texts or [],
    )
    text = normalize_text(current_combined)
    source_text = normalize_text(contextual_combined)
    recent_reply_texts = recent_reply_texts or []
    if is_identity_probe(text):
        reply, variant_index = build_identity_probe_reply(
            config,
            query=current_combined,
            recent_reply_texts=recent_reply_texts,
        )
        return {
            "applied": True,
            "rule_name": "realtime_identity_probe",
            "reason": "local_identity_guard_reply",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_identity_probe",
        }
    route_reason = str(route.get("reason") or "")
    if route_reason in {
        "multi_question_compound_requires_synthesis",
        "customer_challenge_needs_direct_answer",
    }:
        return {"applied": False, "reason": "prefer_foreground_llm_direct_resolution"}
    if route_reason == "direct_product_price_requires_synthesis":
        return {"applied": False, "reason": "prefer_foreground_llm_direct_resolution"}
    if route_reason == "catalog_price_superlative_available":
        products = catalog_products_by_public_price(limit=3)
        reply, variant_index = build_catalog_price_superlative_reply(
            current_combined,
            products,
            recent_reply_texts=recent_reply_texts,
        )
        if reply:
            return {
                "applied": True,
                "rule_name": "realtime_catalog_price_fact",
                "reason": "product_master_price_superlative",
                "raw_reply_text": reply,
                "reply_text": reply,
                "variant_index": variant_index,
                "used_product_ids": [str(item.get("id") or "") for item in products[:1] if item.get("id")],
                "saved_reason": "foreground_llm_skipped_for_product_master_price_fact_draft",
            }
    if route_reason == "catalog_price_list_available":
        products = catalog_products_by_public_price(limit=3)
        reply, variant_index = build_catalog_price_list_reply(
            current_combined,
            products,
            recent_reply_texts=recent_reply_texts,
        )
        if reply:
            return {
                "applied": True,
                "rule_name": "realtime_catalog_price_fact",
                "reason": "product_master_price_list",
                "raw_reply_text": reply,
                "reply_text": reply,
                "variant_index": variant_index,
                "used_product_ids": [str(item.get("id") or "") for item in products[:3] if item.get("id")],
                "saved_reason": "foreground_llm_skipped_for_product_master_price_fact_draft",
            }
    if route_reason == "uncertain_message_light_synthesis_allowed":
        recent_catalog_options = extract_recent_catalog_compare_products(recent_reply_texts, limit=2)
        if is_single_choice_compare_request(text) and len(recent_catalog_options) >= 2:
            single_choice = build_single_choice_compare_reply(
                current_combined,
                catalog_options=recent_catalog_options,
                recent_reply_texts=recent_reply_texts,
            )
            if single_choice is not None:
                reply, variant_index = single_choice
                return {
                    "applied": True,
                    "rule_name": "realtime_single_choice_compare_followup",
                    "reason": "local_single_choice_compare_from_recent_catalog_context",
                    "raw_reply_text": reply,
                    "reply_text": reply,
                    "variant_index": variant_index,
                    "used_product_ids": [str(item.get("id") or "") for item in recent_catalog_options if item.get("id")],
                    "saved_reason": "foreground_llm_skipped_for_single_choice_followup",
                }
        reply, variant_index = build_uncertain_business_clarify_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_uncertain_business_clarify",
            "reason": "local_uncertain_business_clarify",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_uncertain_message",
        }
    followup_source_request = is_followup_vehicle_source_request(text, recent_reply_texts) or is_followup_vehicle_source_request(source_text, recent_reply_texts)
    explicit_source_request = (
        is_explicit_vehicle_source_request(text)
        or is_explicit_vehicle_source_request(source_text)
        or is_inventory_category_availability_request(text)
        or is_inventory_category_availability_request(source_text)
    )
    detailed_source_request = is_detailed_vehicle_need_ready(text) or is_detailed_vehicle_need_ready(source_text)
    if route_reason == "friendly_social_greeting":
        reply, variant_index = build_friendly_social_greeting_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_friendly_social_greeting",
            "reason": "local_friendly_social_greeting",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_friendly_social_greeting",
        }
    if route_reason == "friendly_farewell":
        reply, variant_index = build_friendly_farewell_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_friendly_farewell",
            "reason": "local_friendly_farewell",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_friendly_farewell",
        }
    if route_reason == "small_talk":
        reply = str(current_reply_text or "").strip()
        if not reply:
            reply, variant_index = build_friendly_social_greeting_reply(
                current_combined,
                recent_reply_texts=recent_reply_texts,
            )
        else:
            variant_index = 0
        return {
            "applied": True,
            "rule_name": "small_talk",
            "reason": "intent_assist_small_talk",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_small_talk",
        }
    if route_reason == "offtopic_soft_redirect_needs_light_synthesis":
        reply, variant_index = build_offtopic_soft_redirect_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_offtopic_soft_redirect",
            "reason": "local_offtopic_soft_redirect_fallback",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_offtopic_soft_redirect",
        }
    if route_reason == "common_vehicle_compare_can_use_local_style":
        reply, variant_index = build_vehicle_compare_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_vehicle_compare_guidance",
            "reason": "local_vehicle_compare_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_vehicle_compare",
        }
    if route_reason == "common_vehicle_type_guidance_can_use_local_style":
        reply, variant_index = build_vehicle_type_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_vehicle_type_guidance",
            "reason": "local_vehicle_type_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_vehicle_type_guidance",
        }
    if route_reason == "common_maintenance_cost_can_use_local_style":
        reply, variant_index = build_maintenance_cost_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_maintenance_cost_guidance",
            "reason": "local_maintenance_cost_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_maintenance_cost_guidance",
        }
    if route_reason == "common_inspection_guidance_can_use_local_style":
        reply, variant_index = build_inspection_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_inspection_guidance",
            "reason": "local_inspection_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_inspection_guidance",
        }
    if route_reason == "common_cargo_space_guidance_can_use_local_style":
        reply, variant_index = build_cargo_space_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_cargo_space_guidance",
            "reason": "local_cargo_space_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_cargo_space_guidance",
        }
    if route_reason == "common_feature_guidance_can_use_local_style":
        reply, variant_index = build_feature_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_feature_guidance",
            "reason": "local_feature_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_feature_guidance",
        }
    if route_reason == "common_comfort_highway_guidance_can_use_local_style":
        reply, variant_index = build_comfort_highway_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_comfort_highway_guidance",
            "reason": "local_comfort_highway_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_comfort_highway_guidance",
        }
    if route_reason == "common_finance_guidance_can_use_local_style":
        reply, variant_index = build_finance_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_finance_guidance",
            "reason": "local_finance_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_finance_guidance",
        }
    if route_reason == "common_fee_guidance_can_use_local_style":
        reply, variant_index = build_fee_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_fee_guidance",
            "reason": "local_fee_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_fee_guidance",
        }
    if route_reason == "common_testdrive_materials_can_use_local_style":
        reply, variant_index = build_testdrive_material_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_testdrive_materials",
            "reason": "local_testdrive_material_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_testdrive_materials",
        }
    if route_reason == "common_visit_timing_can_use_local_style":
        reply, variant_index = build_visit_timing_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_visit_timing",
            "reason": "local_visit_timing_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_visit_timing",
        }
    if route_reason == "common_no_deposit_visit_can_use_local_style":
        reply, variant_index = build_no_deposit_visit_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_no_deposit_visit_guidance",
            "reason": "local_no_deposit_visit_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_no_deposit_visit",
        }
    if route_reason == "common_price_negotiation_can_use_local_style":
        reply, variant_index = build_price_negotiation_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_price_negotiation",
            "reason": "local_price_negotiation_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_price_negotiation",
        }
    if route_reason == "common_warranty_guidance_can_use_local_style":
        reply, variant_index = build_warranty_guidance_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_warranty_guidance",
            "reason": "local_warranty_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_warranty_guidance",
        }
    if contains_any(text, VALUE_RETENTION_TERMS) and not (followup_source_request or explicit_source_request or detailed_source_request):
        reply, variant_index = build_value_retention_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_value_retention_followup",
            "reason": "local_value_retention_general_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_common_value_retention_followup",
        }
    if contains_any(text, NEW_ENERGY_CHECK_TERMS):
        reply, variant_index = build_new_energy_check_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_new_energy_check",
            "reason": "local_new_energy_check_guidance",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_common_new_energy_check",
        }
    if route_reason == "common_trade_in_collect_can_use_local_style":
        reply, variant_index = build_trade_in_collect_reply(current_combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_trade_in_collect",
            "reason": "local_trade_in_information_collection",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_common_trade_in_collect",
        }
    if not contains_any(text, RECOMMEND_TERMS) and not followup_source_request and not explicit_source_request and not detailed_source_request:
        return {"applied": False, "reason": "not_recommendation_scene"}
    current_has_substantive_body = current_reply_has_substantive_body(current_reply_text, config)
    if current_has_substantive_body and not can_override_current_reply(current_reply_text) and not route.get("soft_missing_evidence"):
        return {"applied": False, "reason": "current_rule_reply_has_priority"}
    if not followup_source_request and not explicit_source_request and not detailed_source_request:
        reply, variant_index = build_generic_recommendation_reply(combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_local_recommendation",
            "reason": "first_broad_recommendation_stays_consultative",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_first_broad_recommendation",
        }
    recommendation_query = query_with_recent_requirement_context(
        combined,
        recent_reply_texts,
        allow_vehicle_source_context=(
            followup_source_request or explicit_source_request or detailed_source_request
        ),
    )
    single_choice_requested = is_single_choice_compare_request(text) or is_single_choice_compare_request(source_text)
    customer_compare_options = meaningful_customer_compare_options(current_combined)
    explicit_compare_candidates = extract_current_catalog_compare_products(text) or extract_current_catalog_compare_products(source_text)
    named_options_ready = has_named_compare_options(current_combined) and len(customer_compare_options) >= 2
    if single_choice_requested and named_options_ready and not explicit_compare_candidates:
        named_choice = build_named_option_single_choice_reply(
            current_combined,
            options=customer_compare_options,
            recent_reply_texts=recent_reply_texts,
        )
        if named_choice is not None:
            reply, variant_index = named_choice
            return {
                "applied": True,
                "rule_name": "realtime_single_choice_compare_named_options",
                "reason": "local_single_choice_compare_without_catalog_candidates",
                "raw_reply_text": reply,
                "reply_text": reply,
                "variant_index": variant_index,
                "used_product_ids": [],
                "saved_reason": "foreground_llm_skipped_for_named_single_choice_compare",
            }
    if explicit_compare_candidates and (
        len(explicit_compare_candidates) >= 2
        or single_choice_requested
    ):
        candidates = explicit_compare_candidates
    else:
        candidates = rank_product_candidates(
            recommendation_query,
            evidence_pack,
            allow_catalog_fallback=followup_source_request or explicit_source_request or detailed_source_request,
            allow_broad_fallback=followup_source_request or explicit_source_request or detailed_source_request,
        )
    if not candidates:
        reply, variant_index = build_generic_recommendation_reply(combined, recent_reply_texts=recent_reply_texts)
        return {
            "applied": True,
            "rule_name": "realtime_local_recommendation",
            "reason": "local_recommendation_clarifying_fallback",
            "raw_reply_text": reply,
            "reply_text": reply,
            "variant_index": variant_index,
            "used_product_ids": [],
            "saved_reason": "foreground_llm_skipped_for_common_recommendation",
        }
    if single_choice_requested:
        single_choice = build_single_choice_compare_reply(
            current_combined,
            catalog_options=candidates[:2],
            recent_reply_texts=recent_reply_texts,
        )
        if single_choice is not None:
            reply, variant_index = single_choice
            return {
                "applied": True,
                "rule_name": "realtime_single_choice_compare",
                "reason": "local_single_choice_compare_with_catalog_candidates",
                "raw_reply_text": reply,
                "reply_text": reply,
                "variant_index": variant_index,
                "used_product_ids": [str(item.get("id") or "") for item in candidates[:2] if item.get("id")],
                "saved_reason": "foreground_llm_skipped_for_single_choice_compare",
            }
    reply, variant_index = build_recommendation_reply(recommendation_query, candidates[:2], recent_reply_texts=recent_reply_texts)
    if not reply:
        return {"applied": False, "reason": "empty_realtime_reply"}
    return {
        "applied": True,
        "rule_name": "realtime_local_recommendation",
        "reason": "local_product_candidates_with_service_style",
        "raw_reply_text": reply,
        "reply_text": reply,
        "variant_index": variant_index,
        "used_product_ids": [str(item.get("id") or "") for item in candidates[:2] if item.get("id")],
        "saved_reason": "foreground_llm_skipped_for_common_recommendation",
    }


def query_with_recent_requirement_context(
    query: str,
    recent_reply_texts: list[str] | None = None,
    *,
    allow_vehicle_source_context: bool = False,
) -> str:
    current = str(query or "").strip()
    if extract_budget_wan(current) > 0:
        return current
    context = recent_requirement_context(recent_reply_texts or [])
    if not context:
        return current
    if should_reuse_recent_requirement_context(current):
        return f"近期已确认需求：{context}\n当前客户问题：{current}"
    if (
        allow_vehicle_source_context
        and should_reuse_recent_requirement_for_vehicle_source(current, context)
    ):
        return f"近期已确认需求：{context}\n当前客户问题：{current}"
    return current


def should_reuse_recent_requirement_for_vehicle_source(
    current_query: str,
    recent_requirement_context_text: str,
) -> bool:
    current = normalize_text(current_query)
    if not current:
        return False
    # Fresh first-contact openings should not inherit prior budget/history.
    if contains_any(current, ("刚加好友", "第一次咨询", "第一次聊", "初次咨询", "新客户")):
        return False
    if extract_budget_wan(current) > 0:
        return False
    if extract_budget_wan(recent_requirement_context_text) <= 0:
        return False
    if contains_any(current, APPOINTMENT_TERMS):
        return False
    # Directional inventory asks ("先给我个库存方向/先看有哪些车") should
    # inherit the immediately confirmed budget to avoid recommending off-budget cars.
    source_markers = (
        "库存",
        "库存方向",
        "车源",
        "有哪些车",
        "先看",
        "先挑",
        "先筛",
        "方向",
        "选两台",
        "挑两台",
        "挑几台",
    )
    return (
        is_explicit_vehicle_source_request(current)
        or is_direction_candidate_request(current)
        or contains_any(current, source_markers)
    )


def recent_requirement_context(recent_reply_texts: list[str]) -> str:
    for reply in reversed(recent_reply_texts[-4:]):
        text = re.sub(r"\s+", " ", str(reply or "")).strip()
        if not text or extract_budget_wan(text) <= 0:
            continue
        for pattern in (
            r"需求这块[^。；;]*",
            r"从\d+(?:\.\d+)?万(?:以内|以下|内|左右)?[^。；;]*",
            r"按您说的\d+(?:\.\d+)?万(?:以内|以下|内|左右)?[^。；;]*",
            r"\d+(?:\.\d+)?万(?:以内|以下|内|左右)[^。；;]*",
        ):
            match = re.search(pattern, text)
            if match:
                return match.group(0)[:180]
    return ""


def should_reuse_recent_requirement_context(current_query: str) -> bool:
    current = normalize_text(current_query)
    if not current:
        return False
    # A fresh opening question should not inherit previous budget from history.
    if contains_any(current, ("刚加好友", "第一次咨询", "第一次聊", "初次咨询", "新客户")):
        return False
    return has_followup_context_reference(current)


def has_followup_context_reference(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(
        clean,
        (
            "刚才",
            "前面",
            "上面",
            "你刚",
            "你推荐",
            "那两台",
            "这两台",
            "两台里",
            "几台里",
            "从里面",
            "里面选",
            "继续按",
            "按刚才",
            "按你说",
            "再比",
            "二选一",
            "到底哪个",
        ),
    ):
        return True
    return is_existing_vehicle_option_compare(clean)


def build_generic_recommendation_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    summary = build_need_summary(query)
    budget_known = extract_budget_wan(query) > 0
    if is_appointment_context_query(query):
        visit_time = extract_visit_time_label(query)
        if visit_time:
            return choose_natural_reply_variant(
                [
                    f"可以，我先按{visit_time}帮您核车源和门店排期，确认车还在、能不能试驾、能不能一次看两台，尽量别让您白跑。",
                    f"{visit_time}这个时间我先记下，接下来主要确认车源状态、排期和试驾安排；确认没问题再让您过去更稳。",
                    f"行，我按{visit_time}去核一下现车和排期。到店前把车源还在不在、能不能一起看、试驾怎么排确认清楚，省得您多跑。",
                ],
                key_text=query,
                recent_reply_texts=recent_reply_texts,
            )
        return choose_natural_reply_variant(
            [
                "可以的，您来之前跟我说一下大概时间和想看的车型，我帮您确认一下车源还在不在、门店排期和能不能一次安排两三台一起看，尽量别让您白跑。",
                "行，您把大概到店时间和想看的方向发我，我确认车源、门店排期和能不能集中安排几台一起看，省得您跑空。",
                "可以安排，您先说下周末大概几点到、想看轿车还是SUV。我这边把现车和排期核一下，尽量让您一次看得完整点。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if budget_known and summary:
        followup = build_next_info_prompt(query, recent_reply_texts=recent_reply_texts)
        return choose_natural_reply_variant(
            [
                f"按您说的{summary}，先看车况、好不好上手和后期成本，不只看车名。{followup}",
                f"信息已经够先筛了：{summary}。接下来按这个方向看现车和车况记录，{followup}",
                f"这个需求比较明确了，{summary}。先把不合适的排掉，重点看车况透明、驾驶好上手、后期费用别太高的。{followup}",
                f"那就不从大方向绕了，先按{summary}往下缩范围。车况、公里数和配置适配度放前面看，{followup}",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in query for term in ("露营", "钓鱼", "郊外", "后备箱", "底盘高", "通过性")):
        return choose_natural_reply_variant(
            [
                "您这个更像周末户外用车场景，重点看SUV空间、后备箱、底盘通过性、油耗和底盘车况。先别只看外观配置，预算和是否置换确认后，我按现车把更适合露营钓鱼的几台缩出来。",
                "露营钓鱼的话，方向可以先放在空间够用、底盘别太低、后备箱规整、油耗别太夸张的SUV上。您把预算上限和能不能接受置换/贷款说一下，我再往具体车源上靠。",
                "这个用途我理解，车不能太娇气，后备箱和底盘状态也要看。先按SUV、空间、油耗、检测报告和后期保养成本筛，后面再给您缩到两台更稳的。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in query for term in ("工地", "皮实", "耐造")):
        return choose_natural_reply_variant(
            [
                "可以，这类需求我会按预算、底盘耐用度、车况透明和后期维修成本来筛，SUV也要看年份和公里数，不能只图便宜。您方便的话我帮您把四五万左右能看的方向捋一下。",
                "明白，跑工地更要看皮实耐造和车况，不建议只看外观和配置。按四五万左右、SUV、维修成本别太高的方向筛，具体还是以检测报告为准。",
                "这个预算可以先筛耐用、省心、维修不贵的SUV方向，车况比年份更关键。我帮您看有没有合适现车，再确认检测和公里数。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in query for term in ("跑高速", "后排", "空间", "舒服", "年限", "爸妈", "父母")):
        return choose_natural_reply_variant(
            [
                "明白，您这个方向就不能只看价格了，我会优先按后排舒适、年限公里数、车况记录和高速稳定性来筛，具体还是以检测报告和实车情况为准。",
                "这个需求我理解，重点放在后排空间、底盘稳定、年限别太老和车况透明上，不会只按便宜来推。您后面如果方便到店，我可以先把适合的几台排出来。",
                "可以，您这个更偏家庭舒适和长途稳定，按车况干净、公里数合理、后排坐着不累的方向筛，别只看车名。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in query for term in ("维护", "省事", "少操心", "公里数", "车龄", "别太老", "后期成本")):
        return choose_natural_reply_variant(
            [
                "按您这几个点，我建议先看省心耐用的日系轿车方向，再把SUV当备选。后面我按预算给您缩到两台，先核车况和里程。",
                "先给明确建议：优先省心、公里数合理、车龄别太老的轿车；SUV做第二梯队。您补个预算区间，我直接给两台可看车源。",
                "您要的是维护省事，那我建议先看后期成本更稳、里程和车龄更友好的方向。预算一给，我就按这个思路直接筛两台。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in query for term in ("接娃", "家用", "通勤", "省油", "代步")):
        return choose_natural_reply_variant(
            [
                "您这个需求我建议先按预算、用途和车况筛，不要只看年份。家用通勤优先看省心、省油、维修成本低的车，具体车况还是以检测报告为准。您预算大概卡在多少，能不能接受贷款或置换？我再帮您缩到两三台。",
                "家用通勤的话，重点看省心、省油、后期维修成本低不低。您把预算和能不能贷款/置换说一下，我就能直接帮您缩小到两三台。",
                "这种需求重点不是年份越新越好，而是车况干净、公里数合理、后期好养。您预算大概卡在哪个区间？我按这个思路给您挑几台更稳的。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    return choose_natural_reply_variant(
        [
            "可以，先从预算、用途和偏好的车型入手。您把预算范围、主要用途、能否贷款或置换发我，我再给您缩到两三台合适的。",
            "没问题，预算、用途、偏轿车还是SUV、是否考虑贷款/置换，这几项说清楚后，我按条件给您筛几台更贴近的。",
            "可以先筛一轮。您把预算上限、用车场景和有没有置换发我，后面按车况和养车成本一起看，不只看车名。",
            "行，先别铺太大范围。您说下预算、主要怎么用车、有没有置换，我按这些条件直接往合适车源上靠。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_uncertain_business_clarify_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "可以，我先按二手车需求帮您收窄。您先说预算区间和主要用途（家用/通勤/接待），我马上给您两台可看车源。",
            "收到，先给您走实用路线：预算大概多少、偏轿车还是SUV？我按这个先筛两台车况透明的。",
            "明白，我先按您关心的方向来。您给我两个点：预算上限、主要用途；我据此先给您一版可看车源。",
        ],
        key_text=query or "uncertain_business_clarify",
        recent_reply_texts=recent_reply_texts,
    )


def build_friendly_social_greeting_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "在的，您说。",
            "你好呀，在呢，您慢慢说。",
            "收到，我在，您直接说就行。",
        ],
        key_text=query or "friendly_social_greeting",
        recent_reply_texts=recent_reply_texts,
    )


def build_friendly_farewell_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "好嘞，您先忙，有需要随时喊我。",
            "收到，那您先忙，回头再聊。",
            "行，辛苦啦，有空再找我就行。",
        ],
        key_text=query or "friendly_farewell",
        recent_reply_texts=recent_reply_texts,
    )


def build_offtopic_soft_redirect_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    topic_ack = infer_offtopic_social_ack(query)
    recent_context = normalize_text(" ".join((recent_reply_texts or [])[-3:]))
    if recent_context and has_business_signal(recent_context):
        redirect_variants = [
            "要不我接着按你刚才的需求往下走，给你一版更贴近的看车建议。",
            "咱们顺手把看车也推进一下，我按你前面提到的需求继续给结论。",
            "不耽误你，我沿着你刚才说的条件继续往下筛，直接给你下一步建议。",
        ]
    else:
        redirect_variants = [
            "不耽误你，我们顺手把看车推进一下：你给我预算和用途，我马上给你一版清晰方向。",
            "我们轻松聊的同时也把正事推进下：说下预算和主要用途，我直接给你两台方向。",
            "要不我先帮你把选车框架搭好：预算区间+主要用途给我，我马上往下筛。",
        ]
    variants = [f"{topic_ack}{suffix}" for suffix in redirect_variants]
    return choose_natural_reply_variant(
        variants,
        key_text=query or "offtopic_soft_redirect",
        recent_reply_texts=recent_reply_texts,
    )


def infer_offtopic_social_ack(query: str) -> str:
    clean = normalize_text(query)
    if contains_any(clean, ("电影", "电视剧", "综艺", "动漫", "追剧")):
        return "这个我能接住，最近大家确实都在聊片单。"
    if contains_any(clean, ("天气", "下雨", "升温", "降温", "刮风", "台风")):
        return "确实，这两天天气变化挺明显。"
    if contains_any(clean, ("吃饭", "午饭", "晚饭", "早餐", "夜宵", "美食")):
        return "哈哈，先吃好最重要。"
    if contains_any(clean, ("游戏", "上分", "排位", "开黑")):
        return "这话题很真实，放松一下挺好。"
    if contains_any(clean, ("旅游", "出行", "去哪玩", "机票", "酒店")):
        return "这个话题我懂，出去走走确实能放松不少。"
    if contains_any(clean, ("累", "烦", "心情", "压力", "忙不忙")):
        return "辛苦了，先缓一口气。"
    return "这个话题我接得住，聊起来也挺轻松。"


def build_value_retention_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "实际一点说，保值主要看品牌口碑、年份里程、车况和后面市场行情。家用车里同级别一般日系更稳一些，但最终不能只看车名，还是要结合检测报告、出险记录和成交价。您如果就是想以后少亏点，我建议优先选车况透明、公里数正常、后期好卖的那台。",
            "如果考虑以后再卖，别只盯车名，重点看公里数、车况透明度、保养记录和市场流通量。车况干净、后面好出手的，一般亏得少；具体还得看检测报告和当时行情。",
            "换个说法，二手车想少亏，核心是选好卖的车型和干净的车况。品牌是一方面，出险、公里数、保养记录更影响后面卖价；您可以优先挑车况透明、受众广的那台。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_vehicle_compare_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    normalized_query = normalize_text(query)
    recent = normalize_text(" ".join((recent_reply_texts or [])[-3:]))
    query_context = normalized_query
    context = normalized_query + recent
    spouse_or_new_driver_context = contains_any(
        query_context,
        ("老婆", "媳妇", "爱人", "女士", "女司机", "给她开", "她停车", "停车不熟", "新手"),
    )
    family_mpv_context = contains_any(query_context, ("家用", "孩子", "老人", "二胎", "一家", "全家", "七座", "mpv", "mpv或大七座")) and contains_any(
        query_context,
        ("gl8", "奥德赛", "mpv", "七座"),
    )
    business_negative_context = contains_any(query_context, ("不想商务", "商务味太重", "别太商务", "不要商务", "商务感太重"))
    business_or_cargo_context = contains_any(
        query_context,
        ("公司用", "接客户", "甲方", "活动策划", "商务", "物料", "展架", "音响架", "折叠桌", "器材"),
    ) and not business_negative_context and not family_mpv_context
    catalog_options = extract_current_catalog_compare_products(query)
    if not catalog_options and is_existing_vehicle_option_compare(query):
        catalog_options = extract_recent_catalog_compare_products(recent_reply_texts or [], limit=2)
    single_choice_request = is_single_choice_compare_request(query)
    named_options = meaningful_customer_compare_options(query) if single_choice_request else []
    if (
        single_choice_request
        and len(catalog_options) < 2
        and len(named_options) >= 2
        and not family_mpv_context
        and not is_vehicle_type_guidance_question(query)
    ):
        named_choice = build_named_option_single_choice_reply(query, options=named_options, recent_reply_texts=recent_reply_texts)
        if named_choice is not None:
            return named_choice
    if single_choice_request:
        single_choice = build_single_choice_compare_reply(
            query,
            catalog_options=catalog_options,
            recent_reply_texts=recent_reply_texts,
        )
        if single_choice is not None:
            return single_choice
    options = [product_short_compare_label(item, context) for item in catalog_options] if catalog_options else extract_customer_compare_options(query)
    option_label = "、".join(options[:3])
    economy_transparency_context = contains_any(query_context, ("车况透明", "车况清楚", "油耗别高", "省油", "油耗", "后期成本", "维护成本"))
    non_suv_options = [item for item in options if item and not contains_any(normalize_text(item), ("suv", "mpv", "七座", "越野"))]
    suv_options = [item for item in options if item and contains_any(normalize_text(item), ("suv", "越野"))]
    if spouse_or_new_driver_context:
        preferred = parking_friendly_compare_product(catalog_options)
        if preferred:
            preferred_label = product_short_compare_label(preferred, context)
            backup_labels = [product_short_compare_label(item, context) for item in catalog_options if str(item.get("id") or item.get("name") or "") != str(preferred.get("id") or preferred.get("name") or "")]
            backup_text = "、".join(label for label in backup_labels if label)
            variants = [
                f"这两台里我会先看{preferred_label}：车身更小、停车压力低，新手倒车更友好。{backup_text}空间或配置更强，但先放备选。",
                f"按新手好停这个前提，{preferred_label}优先；{backup_text}可以对比，但停车和挪车压力会更大一点。",
                f"我会把{preferred_label}排前面，理由是好停好开、后期成本也更好控。{backup_text}等试驾后再看是否值得上。",
            ]
            return choose_compare_reply_variant(query, variants, key_text=query + recent, recent_reply_texts=recent_reply_texts)
        variants = option_context_variants(
            option_label,
            [
                "按新手/好停这个前提，{options}里我建议先看车身更小、视野更好、倒车影像/雷达更齐的那台；尺寸大的先放备选，后面再看车况和检测报告。",
                "{options}可以对比，但这个场景建议先排好开好停、省心成本低的方案，空间或动力更强的做备选；最终按检测报告和试驾体感定。",
                "如果在{options}里排，先看好不好停，再看车况透明和后期成本，最后看配置。",
            ],
            [
                "按新手/好停这个前提，我会先看车身更小、视野更好、倒车影像/雷达更齐的那台；尺寸大的先放备选。最后再用检测报告和试驾感受确认。",
                "这个场景建议先排好开好停、省心成本低的方案，空间或动力更强的可以做备选。别只看车名，车况和实际停车体感更关键。",
                "如果让我排优先级：先好停好开，再看车况透明和后期成本，最后看配置。到店试坐试倒车，比单看参数更准。",
            ],
        )
    elif family_mpv_context:
        variants = option_context_variants(
            option_label,
            [
                "{options}里，家用我建议先看奥德赛这类，坐姿和油耗更适合家庭；GL8空间和气场更强，但商务感会重一点。到店再看第三排、行李空间和检测报告。",
                "按家用、不想商务味太重这个前提，{options}里先看奥德赛方向；GL8适合空间刚需或接待多的情况，油耗和车身尺寸要现场感受。",
                "{options}可以都看，但家用优先级我会放在舒适、省油、第三排好坐上；GL8空间优势明显，奥德赛家庭属性更强。",
            ],
            [
                "家用 MPV 我会优先看舒适、省油、第三排和儿童座椅接口，不想商务味太重的话，奥德赛这类会更贴近；GL8空间强，但车身和商务感也更明显。",
                "如果主要家用，我会先看奥德赛这类家庭属性更强的，再把GL8当空间刚需备选。到店重点试第三排、行李空间和保养记录。",
                "家用七座别只看气场，第三排舒适、上下车、油耗和车况更关键。商务感不想太重的话，优先看家庭属性更强的MPV。",
            ],
        )
    elif economy_transparency_context and suv_options and non_suv_options:
        sedan_text = "、".join(non_suv_options[:2])
        suv_text = "、".join(suv_options[:1]) or "SUV"
        variants = [
            f"按车况透明、油耗别高这个前提，我会先看{sedan_text}这类轿车方向，{suv_text}先放备选；同预算下轿车通常油耗和后期成本更好控。",
            f"这个需求我会把{sedan_text}排前面，{suv_text}做备选。核心看检测报告、里程和保养记录，别只看车名。",
            f"如果只按省心和油耗排序，先看{sedan_text}这类轿车，再对比{suv_text}；SUV空间强，但油耗和车况差异更要细查。",
        ]
    elif business_or_cargo_context:
        preferred = business_friendly_compare_product(catalog_options, context)
        if preferred:
            preferred_label = product_short_compare_label(preferred, context)
            backup_labels = [
                product_short_compare_label(item, context)
                for item in catalog_options
                if str(item.get("id") or item.get("name") or "") != str(preferred.get("id") or preferred.get("name") or "")
            ]
            backup_text = "、".join(label for label in backup_labels if label)
            variants = [
                f"公司用兼顾接客户，我建议先看{preferred_label}：接待观感更稳，后备厢和通过性也够用；{backup_text}性价比高，可以当备选。",
                f"这两台里{preferred_label}优先，更适合公司接待和拉物料一起用；{backup_text}预算优势明显，但接待观感略弱一点。",
                f"按活动物料加接客户这个场景，我建议先看{preferred_label}，{backup_text}放备选。到店再重点核后备厢、二排放倒和检测报告。",
            ]
            return choose_compare_reply_variant(query, variants, key_text=query + recent, recent_reply_texts=recent_reply_texts)
        variants = option_context_variants(
            option_label,
            [
                "{options}里，公司用我会先看装载空间、接待观感和后期成本。后备厢更实用、车况更透明的排前面。",
                "{options}都别只看品牌，先按后备厢实装、第二排放倒、油耗和维护成本排。能兼顾接待形象和车况透明的优先。",
                "如果要装物料又接客户，{options}里我会把空间和车况放第一，价格贴合放第二。",
            ],
            [
                "公司用我会先看装载空间、接待观感和后期成本。后备厢更实用、车况更透明的排前面；预算优势明显的可以当备选。",
                "这个对比别只看品牌，先按后备厢实装、第二排放倒、油耗和维护成本排。能兼顾接待形象和车况透明的，优先级更高。",
                "如果要装物料又接客户，我会把空间和车况放第一，价格贴合放第二。具体哪台更值，还是看商品库里的价格、里程和检测信息。",
            ],
        )
    else:
        variants = option_context_variants(
            option_label,
            [
                "{options}里，我会先按预算贴合、用途匹配、车况透明和后期成本排序；明显偏预算或偏场景的做备选。",
                "{options}不要只按车名拍板。先看哪台更贴合预算和用车场景，再看检测报告、里程、保养记录和试驾感受。",
                "如果在{options}里给方向：优先车况清楚、价格贴合、后期成本好控的；成本更高的放第二梯队对比。",
            ],
            [
                "这类对比我会先按预算贴合、用途匹配、车况透明和后期成本排序；能同时满足这几项的排前面，明显偏预算或偏场景的做备选。",
                "不要只按车名拍板。先看哪台更贴合预算和用车场景，再看检测报告、里程、保养记录和试驾感受，这样更稳。",
                "如果让我给方向：优先车况清楚、价格贴合、后期成本好控的；动力/空间/品牌优势明显但成本更高的，放第二梯队对比。",
            ],
        )
    return choose_compare_reply_variant(query, variants, key_text=query + recent, recent_reply_texts=recent_reply_texts)


def option_context_variants(option_label: str, with_options: list[str], fallback: list[str]) -> list[str]:
    if not option_label:
        return fallback
    normalized_option_label = str(option_label or "").strip()
    if normalized_option_label.endswith("里"):
        normalized_option_label = normalized_option_label[:-1]
    return [template.format(options=normalized_option_label) for template in with_options]


def choose_compare_reply_variant(
    query: str,
    variants: list[str],
    *,
    key_text: str = "",
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    reply, variant_index = choose_natural_reply_variant(
        variants,
        key_text=key_text,
        recent_reply_texts=recent_reply_texts,
    )
    return ensure_explicit_compare_recommendation(reply, query=query), variant_index


def ensure_explicit_compare_recommendation(reply: str, *, query: str) -> str:
    text = str(reply or "").strip()
    if not text:
        return text
    normalized = normalize_text(text)
    # For compare-style questions, enforce at least one explicit recommendation cue.
    if contains_any(normalized, ("建议", "优先", "更推荐", "先看", "先给明确结论", "先给结论", "二选一", "我会先")):
        return text
    if not is_vehicle_compare_question(query, []):
        return text
    return f"先给结论：建议先看更贴近需求的一台。{text}"


def extract_current_catalog_compare_products(query: str) -> list[dict[str, Any]]:
    options = extract_customer_compare_options(query)
    if len(options) < 2:
        return []
    generic_category_terms = {
        "轿车",
        "suv",
        "mpv",
        "七座",
        "七座车",
        "车型",
        "方向",
        "哪一类",
        "哪类",
        "哪种",
        "油车",
        "电车",
        "新能源",
        "纯电",
        "混动",
    }
    concrete_options = [
        option
        for option in options
        if normalize_text(option) not in generic_category_terms and len(normalize_text(option)) >= 2
    ]
    if len(concrete_options) < 2:
        return []
    products = load_catalog_product_candidates()
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in concrete_options:
        option_key = normalize_text(option)
        if not option_key:
            continue
        for product in products:
            product_id = str(product.get("id") or product.get("name") or "")
            if product_id in seen:
                continue
            if catalog_product_matches_text(product, option_key):
                matched.append(product)
                seen.add(product_id)
                break
    return matched[:3]


def extract_recent_catalog_compare_products(recent_reply_texts: list[str], *, limit: int = 3) -> list[dict[str, Any]]:
    recent_items = [str(item or "") for item in recent_reply_texts[-4:] if str(item or "").strip()]
    # For "这两台/前面两台" style questions, the customer's reference points to
    # the latest concrete recommendation, not every candidate mentioned earlier.
    for item in reversed(recent_items):
        products = catalog_compare_products_from_text(item, limit=limit)
        if len(products) >= 2:
            return products[:limit]
    recent_text = " ".join(recent_items)
    return catalog_compare_products_from_text(recent_text, limit=limit)


def catalog_compare_products_from_text(text: str, *, limit: int = 3) -> list[dict[str, Any]]:
    recent_key = normalize_text(text)
    if not recent_key:
        return []
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for product in load_catalog_product_candidates():
        best_pos = -1
        best_len = 0
        for term in catalog_product_compare_terms(product):
            term_key = normalize_text(term)
            if not term_key:
                continue
            pos = recent_key.find(term_key)
            if pos < 0:
                continue
            if best_pos < 0 or pos < best_pos or (pos == best_pos and len(term_key) > best_len):
                best_pos = pos
                best_len = len(term_key)
        if best_pos >= 0:
            matches.append((best_pos, -best_len, product))
    matches.sort(key=lambda item: (item[0], item[1]))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, product in matches:
        product_id = str(product.get("id") or product.get("name") or "")
        if product_id in seen:
            continue
        seen.add(product_id)
        result.append(product)
        if len(result) >= limit:
            break
    return result


def catalog_product_matches_text(product: dict[str, Any], text_key: str) -> bool:
    for term in catalog_product_compare_terms(product):
        term_key = normalize_text(term)
        if not term_key:
            continue
        if term_key in text_key or (len(text_key) >= 2 and text_key in term_key):
            return True
    return False


def catalog_product_compare_terms(product: dict[str, Any]) -> list[str]:
    values = [str(product.get("name") or "")]
    values.extend(str(alias) for alias in product.get("aliases", []) or [])
    terms: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and is_concrete_compare_term(clean) and clean not in terms:
            terms.append(clean)
    return terms


def is_concrete_compare_term(value: str) -> bool:
    term = normalize_text(value)
    if len(term) < 2:
        return False
    generic = {
        "代步车",
        "练手车",
        "小车",
        "省油",
        "通勤",
        "家用轿车",
        "大空间",
        "性价比",
        "神车",
        "国产suv",
        "商务接待",
        "豪华品牌",
        "豪华轿车",
        "自动挡省油",
        "首付三成",
        "两厢车",
        "七座",
        "电动车",
        "新能源",
        "混动",
        "商务车",
        "保姆车",
        "工具车",
        "越野",
        "后驱",
        "试驾",
        "小钢炮",
        "韩系",
        "颜值高",
        "操控好",
    }
    return term not in generic


def product_short_compare_label(product: dict[str, Any], context_key: str = "") -> str:
    aliases = [str(alias).strip() for alias in product.get("aliases", []) or [] if str(alias).strip()]
    context = normalize_text(context_key)
    for alias in aliases:
        alias_key = normalize_text(alias)
        if is_concrete_compare_term(alias) and alias_key and alias_key in context and len(alias) <= 10:
            return alias
    for alias in aliases:
        if is_concrete_compare_term(alias) and len(alias) <= 10:
            return alias
    return str(product.get("name") or "").strip()


def parking_friendly_compare_product(products: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not products:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for product in products:
        searchable = normalize_text(product_search_text(product))
        score = 0.0
        if contains_any(searchable, ("小型轿车", "两厢", "小巧", "好停车", "新手", "练车", "polo")):
            score += 4.0
        if contains_any(searchable, ("倒车影像", "倒车", "雷达", "360")):
            score += 1.0
        if contains_any(searchable, ("suv", "越野", "四驱", "中型", "中大型")):
            score -= 2.5
        try:
            price = float(product.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        scored.append((score - price * 0.01, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else None


def business_friendly_compare_product(products: list[dict[str, Any]], context_key: str = "") -> dict[str, Any] | None:
    if not products:
        return None
    context = normalize_text(context_key)
    scored: list[tuple[float, dict[str, Any]]] = []
    for product in products:
        searchable = normalize_text(product_search_text(product))
        score = 0.0
        if contains_any(searchable, ("suv", "后备厢", "后备箱", "第二排", "大空间", "四驱", "2.5l", "2.0t")):
            score += 2.0
        if contains_any(searchable, ("合资", "四驱", "轻度越野", "中型suv", "空间实用")):
            score += 2.6
        if contains_any(searchable, ("国产suv", "性价比", "预算有限", "价格友好")):
            score += 1.0
        if contains_any(context, ("接客户", "甲方", "商务", "别太寒酸", "体面", "观感")):
            if contains_any(searchable, ("合资", "四驱", "中型", "中大型", "空间实用")):
                score += 2.0
            if contains_any(searchable, ("国产suv", "预算有限", "性价比")):
                score -= 0.8
        if contains_any(context, ("预算紧", "预算有限", "便宜", "价格低", "成本低")):
            if contains_any(searchable, ("性价比", "预算有限", "价格友好", "入门suv")):
                score += 2.0
        try:
            price = float(product.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if contains_any(context, ("接客户", "商务", "体面")) and price:
            score += min(1.5, price * 0.08)
        scored.append((score, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def is_single_choice_compare_request(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(clean, SINGLE_CHOICE_COMPARE_TERMS):
        return True
    if contains_any(clean, ("优先顺序", "先后顺序", "排顺序", "定一个优先顺序")) and contains_any(clean, ("和", "跟", "与", "还是", "、")):
        return True
    if contains_any(clean, ("哪个", "哪台", "哪款", "哪一辆")) and contains_any(clean, ("和", "跟", "与", "还是", "二选一", "先看", "这两台", "这两款")):
        return True
    return contains_any(clean, ("哪个", "哪台", "哪款", "哪一辆")) and contains_any(clean, ("就这两", "这两台", "这两个", "二选一"))


def extract_product_year(product: dict[str, Any]) -> int:
    text = product_search_text(product)
    years = [int(match.group(0)) for match in re.finditer(r"(?:19|20)\d{2}", text)]
    if not years:
        return 0
    return max(years)


def extract_product_mileage_wan(product: dict[str, Any]) -> float:
    text = product_search_text(product)
    match = re.search(r"(\d+(?:\.\d+)?)\s*万公里", text)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return 0.0


def extract_product_price_wan(product: dict[str, Any]) -> float:
    try:
        return float(product.get("price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def select_single_compare_product(products: list[dict[str, Any]], *, context_key: str) -> dict[str, Any] | None:
    if not products:
        return None
    context = normalize_text(context_key)
    budget = extract_budget_wan(context)
    prefer_low_mileage = contains_any(context, ("公里数别太多", "公里别太多", "里程别太高", "里程低", "公里数少", "里程少"))
    prefer_newer = contains_any(context, ("别太老", "车别太老", "新一点", "年限近", "年份新"))
    prefer_easy_maint = contains_any(context, ("维护省事", "省心", "后期成本", "养护", "油耗别高", "省油", "毛病少", "稳定"))
    need_budget_fit = budget > 0 or contains_any(context, ("预算", "总价", "价格"))
    scored: list[dict[str, Any]] = []
    for product in products:
        searchable = normalize_text(product_search_text(product))
        year = extract_product_year(product)
        mileage_wan = extract_product_mileage_wan(product)
        price_wan = extract_product_price_wan(product)
        score = 0.0
        reasons: list[str] = []
        if prefer_newer and year > 0:
            score += max(0.0, (year - 2016) * 0.55)
            reasons.append(f"年份更新（{year}）")
        if prefer_low_mileage and mileage_wan > 0:
            score += max(0.0, 12.0 - mileage_wan) * 0.9
            reasons.append(f"里程更低（约{mileage_wan:g}万公里）")
        if prefer_easy_maint:
            if contains_any(searchable, ("自然吸气", "cvt", "at", "省油", "混动", "皮实", "耐用")):
                score += 0.9
            if contains_any(searchable, ("双离合", "高功率", "高性能", "四驱", "2.0t", "2.5t", "3.0")):
                score -= 0.8
            reasons.append("后期维护压力相对更小")
        if need_budget_fit and price_wan > 0:
            if budget > 0:
                score += max(0.0, 5.0 - abs(price_wan - budget)) * 0.45
            if price_wan <= budget + 0.6 or budget <= 0:
                score += 0.5
            reasons.append(f"价格更贴近预算（约{price_wan:g}万）")
        # Tie-breakers: prefer newer, lower mileage, and avoid over-pricing.
        if year > 0:
            score += max(0.0, year - 2010) * 0.02
        if mileage_wan > 0:
            score += max(0.0, 20.0 - mileage_wan) * 0.02
        if price_wan > 0:
            score -= price_wan * 0.01
        scored.append(
            {
                "product": product,
                "score": score,
                "year": year,
                "mileage_wan": mileage_wan,
                "price_wan": price_wan,
                "reasons": reasons,
            }
        )
    scored.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return scored[0] if scored else None


def build_named_option_single_choice_reply(
    query: str,
    *,
    options: list[str],
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int] | None:
    cleaned: list[str] = []
    for option in options:
        item = str(option or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)
    if len(cleaned) < 2:
        return None
    context = normalize_text(query + " " + " ".join((recent_reply_texts or [])[-2:]))
    preferred = cleaned[0]
    backup = cleaned[1]
    business_negative = contains_any(context, ("不想商务", "别太商务", "不要商务", "商务味太重", "商务感太重"))
    if contains_any(context, ("新手", "停车", "倒车", "女司机")):
        shortest = min(cleaned[:2], key=lambda item: len(normalize_text(item)))
        if shortest:
            preferred = shortest
            backup = cleaned[0] if cleaned[0] != preferred else cleaned[1]
    elif contains_any(context, ("家用", "家庭", "孩子", "全家")) and business_negative:
        family_like = next((item for item in cleaned[:2] if not contains_any(normalize_text(item), ("gl8", "商务"))), "")
        if family_like:
            preferred = family_like
            backup = cleaned[0] if cleaned[0] != preferred else cleaned[1]
    elif contains_any(context, ("公司", "接待", "商务", "后备箱", "后备厢", "装东西")) and not business_negative:
        suv_like = next((item for item in cleaned[:2] if contains_any(normalize_text(item), ("suv", "mpv", "越野"))), "")
        if suv_like:
            preferred = suv_like
            backup = cleaned[0] if cleaned[0] != preferred else cleaned[1]
    elif contains_any(context, ("家用", "通勤", "省油", "油耗", "维护", "后期成本", "省心")):
        sedan_like = next((item for item in cleaned[:2] if not contains_any(normalize_text(item), ("suv", "mpv", "越野", "七座"))), "")
        if sedan_like:
            preferred = sedan_like
            backup = cleaned[0] if cleaned[0] != preferred else cleaned[1]
    reasons = "按您当前需求（通勤/家用/后期成本）更贴近"
    if contains_any(context, ("新手", "停车", "倒车")):
        reasons = "新手停车和日常好开这块更稳"
    elif contains_any(context, ("家用", "家庭", "孩子", "全家")) and business_negative:
        reasons = "家用舒适和日常省心这块更贴近"
    elif contains_any(context, ("公司", "接待", "商务")) and not business_negative:
        reasons = "接待和日常实用性更贴近"
    variants = [
        f"这两台里我建议先看{preferred}，{backup}先做备选。主要是{reasons}，最后再结合车况和检测报告定。",
        f"先给明确结论：优先{preferred}，{backup}放第二顺位。到店先对比车况、里程和检测报告，再拍板。",
        f"如果二选一，我会先看{preferred}；{backup}先当备选。理由是{reasons}，试驾后再做最终确认。",
    ]
    return choose_natural_reply_variant(variants, key_text=query + context, recent_reply_texts=recent_reply_texts)


def build_single_choice_compare_reply(
    query: str,
    *,
    catalog_options: list[dict[str, Any]],
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int] | None:
    if not catalog_options:
        return None
    context = normalize_text(query + " " + " ".join((recent_reply_texts or [])[-3:]))
    if len(catalog_options) == 1:
        preferred_label = product_short_compare_label(catalog_options[0], context)
        mentions = extract_customer_compare_options(query)
        has_multiple_mentions = len(mentions) >= 2
        missing_hint = (
            "另一台我先帮您继续按同级条件找在售备选，后面再按车况和检测报告对比。"
            if has_multiple_mentions
            else "我再按同级条件补一台备选给您对比，最终按车况和检测报告拍板。"
        )
        variants = [
            f"这两个里我建议先看{preferred_label}。{missing_hint}",
            f"先给明确结论：优先{preferred_label}。{missing_hint}",
            f"按您这个问题先看{preferred_label}，{missing_hint}",
        ]
        return choose_natural_reply_variant(variants, key_text=query + context, recent_reply_texts=recent_reply_texts)
    selected = select_single_compare_product(catalog_options, context_key=context)
    if not isinstance(selected, dict):
        return None
    preferred = selected.get("product") if isinstance(selected.get("product"), dict) else None
    if not isinstance(preferred, dict):
        return None
    preferred_label = product_short_compare_label(preferred, context)
    backup_labels = [
        product_short_compare_label(item, context)
        for item in catalog_options
        if str(item.get("id") or item.get("name") or "") != str(preferred.get("id") or preferred.get("name") or "")
    ]
    backup_label = next((label for label in backup_labels if label), "")
    reasons = [item for item in selected.get("reasons", []) if isinstance(item, str) and item.strip()]
    reason_text = "、".join(reasons[:2]) if reasons else "综合条件更贴近您当前需求"
    backup_clause = f"{backup_label}先做备选。" if backup_label else "另一台先做备选。"
    variants = [
        f"就您这几个条件，我更推荐先看{preferred_label}。主要是{reason_text}。{backup_clause}最后按车况和检测报告定。",
        f"如果这两台里只选一台，我会先看{preferred_label}。它在{reason_text}这块更贴近，{backup_clause}最后按车况和检测报告定。",
        f"按您现在的偏好，优先先看{preferred_label}。原因是{reason_text}；{backup_clause}最后按车况和检测报告拍板。",
    ]
    return choose_natural_reply_variant(variants, key_text=query + context, recent_reply_texts=recent_reply_texts)


def build_vehicle_type_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    normalized = normalize_text(query)
    if contains_any(normalized, ("和suv比", "还是suv", "轿车还是suv", "轿车和suv")) or (
        contains_any(normalized, ("中级车", "中型车", "b级车", "合资轿车", "轿车", "三厢"))
        and contains_any(normalized, ("suv", "方向"))
    ):
        if contains_any(normalized, ("公司", "接待", "商务", "客户", "活动")):
            return choose_natural_reply_variant(
                [
                    "这类场景我建议先看轿车方向，接待观感和油耗更稳；SUV放备选，后面再看空间和停车压力。",
                    "先给明确结论：优先轿车，再看SUV。公司通勤和接待并用时，轿车通常更省心、成本更可控。",
                    "我建议先看轿车方向，理由是城市通勤+接待更均衡；SUV作为备选，重点再核空间和车况。",
                ],
                key_text=query,
                recent_reply_texts=recent_reply_texts,
            )
        if contains_any(normalized, ("二胎", "全家", "老人孩子", "后备厢", "装东西", "露营")):
            return choose_natural_reply_variant(
                [
                    "按您这个家用场景，我建议先看中型SUV方向，空间和装载更实用；轿车做备选，再比油耗和后期成本。",
                    "先给结论：优先SUV，再看轿车。家用多人和后备厢需求明显时，SUV更贴合。",
                    "我会先排SUV方向，理由是空间和通过性更稳；轿车作为备选，重点看省油和好开。",
                ],
                key_text=query,
                recent_reply_texts=recent_reply_texts,
            )
        return choose_natural_reply_variant(
            [
                "先给您明确建议：优先看轿车方向，油耗和后期成本通常更友好；SUV放备选，再比空间和停车压力。",
                "我的建议是先轿车后SUV。先把省心、油耗和车况透明度稳住，再决定要不要上SUV。",
                "我建议先看轿车方向，原因是更贴近省油和成本可控；SUV作为第二步备选对比。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    spouse_or_new_driver_context = contains_any(
        normalized,
        ("老婆", "媳妇", "爱人", "女士", "女司机", "给她开", "她停车", "停车不熟", "新手"),
    )
    if contains_any(normalized, ("二胎", "全家", "老人孩子", "家庭")) and contains_any(normalized, ("轿车", "suv", "mpv", "车型")):
        return choose_natural_reply_variant(
            [
                "您这个二胎家庭场景，我建议先看中型SUV或空间大的轿车；MPV只在七座刚需时优先。最终再用试驾确认停车压力。",
                "先给结论：家用多人先SUV/大空间轿车，MPV按七座刚需再上。车况透明和试驾感受比车名更重要。",
                "按您这个家庭场景，我会先推空间够用且停车压力可控的SUV/大空间轿车，MPV做第二梯队。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if contains_any(normalized, ("suv", "停车", "老婆", "爱人", "偶尔也会开", "市区")):
        if spouse_or_new_driver_context:
            variants = [
                "我建议先看尺寸别太大的SUV，停车压力会小很多。到店重点试倒车影像、雷达、转弯半径和坐姿盲区。",
                "先给结论：SUV可以选，但优先小一号、好停的。别只看空间，先试倒车和视野。",
                "我的建议是先好停再谈空间：优先车身更小、辅助配置齐的SUV，车况透明放第一位。",
            ]
        else:
            variants = [
                "我建议先看城市友好的SUV：停车、油耗、装载三项都平衡，再看配置。公司用车别只盯空间大。",
                "先给明确方向：SUV能选，但优先车身尺寸适中、后备厢实用、油耗可控的方案。",
                "我的建议是先筛“好停+能装+省心”的SUV，再按车况和成本做最终取舍。",
            ]
        return choose_natural_reply_variant(variants, key_text=query, recent_reply_texts=recent_reply_texts)
    return choose_natural_reply_variant(
        [
            "我建议先按使用比例取舍：通勤接待多先轿车，空间通过性需求高再看SUV，七座刚需再看MPV。",
            "先给方向：轿车偏省心稳、SUV偏全能、MPV偏多人舒适。您先按主要场景定第一优先级。",
            "不建议只看车型名。我会先按停车压力、油耗、空间和车况透明度排序，再决定轿车/SUV/MPV。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_maintenance_cost_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    normalized = normalize_text(query)
    if contains_any(normalized, ("mpv", "商务车", "七座", "7座", "多人")):
        variants = [
            "后期成本大体可以这么看：豪华品牌保养、轮胎、易损件和维修单价通常更高；保有量大、维修体系成熟的车维护会更友好。MPV还要额外看电动门、座椅滑轨、底盘件和混动系统状态，具体到某台车还是以里程、保养记录和检测报告为准。",
            "如果您想后面少操心，别只看车价。豪华品牌开着有质感，但保养和维修成本一般更高；MPV则要重点看空间机构、电动门、悬挂和保养记录。最终我会把车况干净、保养连续的放前面。",
            "养车成本这块我会先分两层看：品牌零整比和这台车自己的车况。豪华品牌正常会贵一些，主流保有量大的体系相对好养；MPV还要看有没有长期商用痕迹、内饰磨损、底盘和保养记录，不能只按年份判断。",
        ]
    else:
        variants = [
            "后期成本我会先看品牌零整比、里程、保养记录和这台车自己的车况。豪华品牌通常保养维修贵一些，保有量大的日系或主流合资车一般更好养；具体还是以检测报告和维保记录为准。",
            "如果您想后面少操心，别只看车价。重点看发动机变速箱状态、底盘件、轮胎刹车、保养是否连续，还有这台车有没有明显维修隐患；车况干净的通常比配置高但记录乱的更稳。",
            "养车成本不能只按车名判断，我会把品牌维修单价、公里数、保养记录、易损件状态和检测报告一起看。能选的话，优先放车况透明、后期配件好找、保养记录连续的那台。",
        ]
    return choose_natural_reply_variant(
        variants,
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_inspection_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "车况这块不能只听口头说。正常我会看检测报告、维保/出险记录、漆膜和结构件情况；补漆、换件、事故水泡火烧这些要分清楚说，重大问题必须以报告和实车为准。",
            "看二手车我建议抓几个硬点：检测报告、出险记录、保养记录、公里数逻辑、底盘和结构件。小补漆和重大事故不是一回事，能看到的补漆换件我会提醒您结合报告一起看。",
            "实际把关就是别只看外观。先看检测报告和维保出险，再看结构件、底盘、发动机变速箱工况；涉及事故、水泡、火烧或关键部位换件，必须以检测和记录为准，不靠一句口头承诺。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_cargo_space_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "这个要看实车空间，不能只按参数拍板。到店我建议把第二排放倒试一下，量后备厢进深和开口高度，展架、音响架、折叠桌和箱子最好按实际尺寸比一遍；能装下还要看固定稳不稳、会不会挡视线。",
            "装物料这块得实测。SUV一般比轿车更合适，但具体还是要看第二排放倒后的地台、后备厢开口、箱子高度和装卸方便程度。您可以把最大那件器材尺寸发我，我先帮您对一遍方向。",
            "您问得很细，这个场景我会把后备厢开口、第二排能不能放平、地台高不高、箱子放进去是否晃动一起看。最好带一两个常用箱子到现场试装，光看图片容易误判。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_feature_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "您这个关注点对。倒车影像、雷达、车身尺寸、视野和刹车脚感都要看；偶尔高速的话，再看胎况、制动、ESP/气囊这些基础安全配置，别只看屏幕配置。",
            "重点可以这么看：城市停车看倒车影像、雷达和车身大小；高速通勤看轮胎、刹车、底盘稳定和基础安全配置。到店实际试坐试开一下，比参数更直观。",
            "这些配置建议重点看，尤其是停车场景多的话，影像和雷达很实用。但最终还得结合车况、检测报告和开起来顺不顺手，配置多不代表一定适合。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_comfort_highway_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "后排家人坐、偶尔跑高速的话，重点看底盘稳定性、座椅支撑、轮胎胎况、隔音胎噪和悬挂有没有松散异响。SUV不是只看空间，试驾时过减速带、烂路和高速巡航感受都要一起看，检测报告里底盘件和轮胎也要核。",
            "这个问题问得细。别只看后排大不大，还要看座椅角度、悬挂支撑、胎噪、风噪和底盘有没有松旷；家人坐得舒服不舒服，试驾一圈比参数更准。车况、轮胎和避震状态也会明显影响舒适度。",
            "如果想长途舒服一点，我会把后排坐姿、底盘滤震、隔音、轮胎和车况记录放前面看。尤其二手SUV，里程和底盘保养很关键，不是配置高就一定坐着舒服。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_finance_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    clean = normalize_text(query)
    if contains_any(clean, ("谁算", "谁给我算", "谁给算", "谁来算", "谁负责算", "谁负责", "谁给", "方案是谁", "测算")):
        return choose_natural_reply_variant(
            [
                "贷款方案到店后我这边先按车价、首付和年限帮您测算方向，具体利率和能不能批要以金融/资方审核为准。",
                "这块不用您自己跑。到店后我先按车价、首付、年限帮您测月供，最终利率和审批结果以金融/资方为准。",
                "贷款方案我们这边会先帮您算大概月供和首付，资料齐了再对接金融确认准方案；审批结果还是以资方为准。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    return choose_natural_reply_variant(
        [
            "分期可以先按车价、首付比例和贷款年限做大概测算，但月供和利率要看车型、首付和资方审批。您征信没问题的话流程一般不会太复杂，材料齐了再让金融那边给准方案。",
            "贷款流程通常是先定车型和车价，再看首付、年限、征信和资方方案。大概范围我可以先帮您算方向，但不能口头保证审批结果；到店前先把方案核清楚，避免您白跑。",
            "首付月供这块能先聊大概逻辑：车价越高、首付越低，月供压力就越大；具体利率、期数和能不能批，要金融审核后才准。您先不用急着定，我可以按目标预算帮您控制总成本。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_fee_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "这个您提前问是对的。车价、过户、保险、上牌、金融相关费用这些都应该提前列清楚，能确认的我会让您看明细；如果哪项没说清楚，不建议您急着定。",
            "费用这块我建议到店前先核一遍：裸车价、过户/上牌、保险、金融或服务相关费用分别是多少。别到现场才发现多一块少一块，确认清楚再看车更稳。",
            "可以提前说清楚的，您关心的就是总落地成本。我这边会按车价、过户、保险、上牌和金融费用分开核，能列明细就列明细，避免后面产生误会。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_testdrive_material_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "建议先带身份证和驾驶证就能看车试驾；如果要置换，再补旧车行驶证、登记证书、保单和车主证件。",
            "先给结论：优先带身份证+驾驶证。涉及置换时，再带旧车手续和保单，资料越全估价越快。",
            "我建议分两步带：先身份证、驾驶证；置换再加旧车行驶证、登记证书、保单和车主证件。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_visit_timing_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    visit_time = extract_visit_time_label(query)
    clean = normalize_text(query)
    if visit_time and is_vehicle_availability_confirmation(clean) and contains_any(clean, TRADE_IN_TERMS + ("旧车",)):
        return choose_natural_reply_variant(
            [
                f"可以，我先按{visit_time}帮您核车源和门店排期，车还在、能不能试驾、置换怎么评估都一起确认。",
                f"{visit_time}我先记下，接下来核车源还在不在、排期和置换初评，确认好再给您准话。",
                f"行，我按{visit_time}去核现车和排期。旧车置换这块也一起安排，到店别让您白跑。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if contains_any(clean, ("检测报告", "车准备好", "准备好", "提前把", "提前准备", "车源状态")):
        label = visit_time or "到店前"
        return choose_natural_reply_variant(
            [
                f"可以，我按{label}帮您核车源和排期，检测报告也提前一起确认，确认没问题再让您过去。",
                f"{label}我先记下，车源状态、检测报告和试驾安排我这边一起核，避免您到店后白等。",
                f"行，我按{label}去确认。到店前把现车、检测报告和试驾排期都核清楚，再给您准话。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if visit_time:
        return choose_natural_reply_variant(
            [
                f"可以，我先按{visit_time}帮您确认车源、排期和试驾情况，确认没问题再让您过去，尽量别白跑。",
                f"{visit_time}我先记下，接下来核车源还在不在、能不能一次看两台、试驾怎么排，确认好再给您准话。",
                f"行，我按{visit_time}去核排期。到店前把现车、试驾和资料一起确认清楚，这样您过去效率高一点。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    return choose_natural_reply_variant(
        [
            "可以，周末建议先按下午两点到四点这个区间看，时间相对好排一点。您确定来之前我再核车源还在不在、能不能试驾和能否一次看两三台，尽量别让您白跑。",
            "能一次对比最好，我会先按您说的方向核两三台现车。周六下午一般三点左右比较稳，但最终还是要看门店排期和车源状态，我确认好再跟您说准。",
            "可以安排成对比看车，不建议只看一台就下判断。您周六下午来的话，我先按三点左右去核排期和试驾情况，确认没问题再让您过去。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_no_deposit_visit_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "可以，先看实车和检测报告，不用一上来就急着交定金。您看完车况、试驾感受和费用明细都满意了，再谈价格和手续会更稳；我这边先把车源状态和看车排期核清楚。",
            "这个流程没问题。先看车、看报告、试驾，觉得合适再确认价格、手续和是否留车；没看明白之前不建议急着付定金，这样您心里也踏实。",
            "可以按这个节奏来。先把实车、检测报告、费用和试驾感受看清楚，满意后再谈成交和留车，不需要一开始就被定金绑住；我先帮您把要看的车和排期确认好。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_price_negotiation_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "价格这块可以帮您争取，但我不建议还没看车就只盯最低。先把车况、检测报告和付款方式看清楚，现场如果确实合适，再按实际成交方案去谈会更靠谱。",
            "能谈的空间一般要结合车源、车况、付款方式和当天政策看。我可以先帮您把方向记下，但不随口报最低，免得到店前后说法对不上。",
            "您关注价格很正常。我的建议是先确认这台车值不值得看，再谈成交空间；如果车况和试驾都满意，我再帮您按付款方式和置换情况去争取。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_warranty_guidance_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "售后这块要看合同约定和具体车源政策。一般会重点说清楚质保范围、期限、哪些部件覆盖、哪些属于易损件；真遇到问题先按合同和检测记录处理，不建议只听口头承诺。",
            "质保可以提前问清楚，尤其是发动机、变速箱这些核心部件怎么保、保多久、哪些情况不保。到店我建议把条款写清楚再定，这样后面不扯皮。",
            "买完以后如果有问题，先看合同质保条款和检测报告。能覆盖的按流程处理，易损件或人为使用问题通常另算；这块到店前我也会建议您问清楚。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_new_energy_check_reply(query: str, *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    return choose_natural_reply_variant(
        [
            "三电这块可以先看检测报告、电池状态、维保记录和实际用车强度。具体到某台车，我会重点核电池健康、充放电表现和有没有异常维修记录，别只听一句口头说没问题。",
            "您这个问法很对，插混/新能源不能只看年份和公里数。先看三电检测、电池状态、维保记录，再结合您的实际使用场景判断合不合适；具体到某台车，我再按检测记录帮您核。",
            "新能源这块我会先看三点：电池状态、三电检测记录、之前有没有异常维修。日常用车强度只是参考，具体适不适合还得落到这台车的检测和使用记录上。",
        ],
        key_text=query,
        recent_reply_texts=recent_reply_texts,
    )


def build_trade_in_collect_reply(query: str = "trade_in_collect", *, recent_reply_texts: list[str] | None = None) -> tuple[str, int]:
    normalized = normalize_text(query)
    if contains_any(normalized, ("准价", "别给区间", "准确价格", "能抵多少", "抵多少")):
        return choose_natural_reply_variant(
            [
                "置换不能只凭一句话给准价，我先按车型、年份、公里数、上牌地和车况给大概区间；最终还要核实行情和实车检测，确认后再说能抵多少车款。",
                "这个我不建议直接报死价，旧车要看手续、漆面、换件、事故水泡火烧和市场行情。您把基础信息和照片发我，我先核实一个区间，现场检测后再定准价。",
                "可以先估方向，但准价要等实车检测和行情核实。您先发车型年份、公里数、城市、配置和车况照片，我按这些给您看区间，再到店确认能抵多少。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    if contains_any(normalized, ("现场估", "现场评估", "开过去", "老车", "剐蹭")):
        recent_trade_in_context = normalize_text("\n".join(recent_reply_texts or []))
        if contains_any(recent_trade_in_context, ("基础信息够", "关键信息已经", "年份公里数", "年份、公里数", "上牌地")):
            return choose_natural_reply_variant(
                [
                    "可以，开过来现场看会更准。您这台基础信息已经有了，接下来主要补外观内饰照片、配置版本、手续和事故水泡火烧情况；到店再结合实车检测和行情定准价。",
                    "能现场评估。既然年份、公里数和上牌地前面已经说了，我这边再看照片、配置、手续和具体车况瑕疵，先估区间，到店后再按实车核准。",
                    "可以带过来估，旧车最终还是看实车。您这台我先按已给的基础信息建个底，再补照片、配置和事故水泡火烧情况，现场看漆面、结构件和行情后定准一点。",
                ],
                key_text=query,
                recent_reply_texts=recent_reply_texts,
            )
        return choose_natural_reply_variant(
            [
                "可以，开过来现场看会更准。外观剐蹭、公里数、手续和实车检测都会影响置换价；您也可以先发外观内饰照片、行驶证信息和大概车况，我先按行情给个粗区间，到店再结合检测确认。",
                "能现场评估，旧车这块最终还是看实车。您这台如果只是外观剐蹭、没有大事故，先把照片、公里数、配置和手续情况发我，我按行情估一版；到店再看漆面、结构件和车况细节。",
                "可以带过来估。现场主要看外观内饰、漆面换件、底盘、手续和当时行情；您先发几张车身四角和内饰照片，我可以先给大概方向，最终价以实车检测为准。",
            ],
            key_text=query,
            recent_reply_texts=recent_reply_texts,
        )
    has_year = bool(re.search(r"(?:19|20)\d{2}", normalized))
    has_mileage = contains_any(normalized, ("公里", "万公里", "公里数", "万多公里"))
    has_city_or_plate = contains_any(normalized, ("牌", "上牌", "南京", "苏州", "上海", "杭州", "无锡", "常州", "合肥"))
    has_vehicle_hint = contains_any(
        normalized,
        ("旧车", "老车", "这台", "这辆", "台", "车名", "车型", "车系", "版本", "排量", "车"),
    )
    if has_year and has_mileage and has_city_or_plate and has_vehicle_hint:
        return choose_natural_reply_variant(
            [
                "您这台旧车的基础信息够先估一版了。我这边还要再看有没有事故水泡火烧、配置版本、手续是否齐，再加几张外观内饰照片；这些齐了我先按行情给您粗区间，最终以实车检测为准。",
                "可以，年份、公里数和上牌地这些关键信息已经有了。接下来您再补一下配置、有没有事故水泡火烧、外观内饰照片和手续情况，我先给您做个置换区间，到店再核准价。",
                "这个信息已经比空问好判断多了。先按您给的年份、公里数和牌照地做初筛，再补车况照片、配置和事故/水泡/火烧情况，我就能先估一个大概区间；最终还是看实车和行情。",
            ],
            key_text=query or "trade_in_collect",
            recent_reply_texts=recent_reply_texts,
        )
    return choose_natural_reply_variant(
        [
            "可以估，您先把车型年份、上牌城市、公里数、排量配置、有没有事故水泡火烧、外观内饰大概成色发我。有行驶证照片和车身四角照片更好，我帮您做个大概区间；最终价还是要看实车检测和当时行情。",
            "置换可以先做个大概区间。您发我车型、哪年上牌、公里数、在哪个城市、有没有事故水泡火烧，再加几张外观内饰照片，我按行情粗估一下。",
            "可以先估一版，您这个思路没问题。您把车龄、公里数、配置、上牌地、车况大概情况发我，我核实行情给您看区间；最后还是要结合实车检测。",
        ],
        key_text=query or "trade_in_collect",
        recent_reply_texts=recent_reply_texts,
    )


def build_synthesis_config_for_route(config: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    next_config = dict(config)
    settings = dict(next_config.get("llm_reply_synthesis", {}) or {})
    realtime = dict(next_config.get("realtime_reply", {}) or {})
    max_tokens = int(route.get("max_completion_tokens") or realtime.get("max_completion_tokens") or DEFAULT_MAX_COMPLETION_TOKENS)
    advisor_mode = str(route.get("advisor_mode") or "").strip()
    if advisor_mode:
        advisor_max_tokens = int(realtime.get("common_sense_advisor_max_tokens", 260) or 260)
        max_tokens = min(max_tokens, max(120, advisor_max_tokens))
    max_history_messages = int(realtime.get("max_history_messages", 6) or 6)
    history_char_budget = int(realtime.get("history_char_budget", 1200) or 1200)
    max_rag_hits = int(realtime.get("max_rag_hits", 2) or 2)
    max_rag_text_chars = int(realtime.get("max_rag_text_chars", 180) or 180)
    max_catalog_candidates = int(realtime.get("max_catalog_candidates", 3) or 3)
    settings["timeout_seconds"] = int(realtime.get("foreground_llm_timeout_seconds", 8) or 8)
    settings["retry_count"] = int(realtime.get("foreground_llm_retry_count", 0) or 0)
    settings["max_tokens"] = max_tokens
    settings["max_history_messages"] = max_history_messages
    settings["history_char_budget"] = history_char_budget
    settings["max_rag_hits"] = max_rag_hits
    settings["max_rag_text_chars"] = max_rag_text_chars
    settings["max_catalog_candidates"] = max_catalog_candidates
    if advisor_mode:
        settings["advisor_mode"] = advisor_mode
        settings["advisor_goal"] = str(route.get("advisor_goal") or "give_a_clear_recommendation_when_safe")
        settings["advisor_route_reason"] = str(route.get("reason") or "")
        existing_max_reply_chars = int(settings.get("max_reply_chars") or 0)
        advisor_max_reply_chars = int(realtime.get("common_sense_advisor_max_reply_chars", 180) or 180)
        route_advisor_max_reply_chars = int(route.get("advisor_max_reply_chars") or 0)
        if route_advisor_max_reply_chars > 0:
            advisor_max_reply_chars = min(advisor_max_reply_chars, route_advisor_max_reply_chars)
        settings["max_reply_chars"] = min(existing_max_reply_chars, advisor_max_reply_chars) if existing_max_reply_chars > 0 else advisor_max_reply_chars
        settings["max_history_messages"] = min(settings["max_history_messages"], int(realtime.get("common_sense_advisor_max_history_messages", 4) or 4))
        settings["history_char_budget"] = min(settings["history_char_budget"], int(realtime.get("common_sense_advisor_history_char_budget", 700) or 700))
        settings["max_rag_hits"] = min(settings["max_rag_hits"], int(realtime.get("common_sense_advisor_max_rag_hits", 1) or 1))
        settings["max_catalog_candidates"] = min(settings["max_catalog_candidates"], int(realtime.get("common_sense_advisor_max_catalog_candidates", 3) or 3))
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    capped_profiles: dict[str, Any] = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            capped_profiles[name] = profile
            continue
        capped = dict(profile)
        capped["max_history_messages"] = min(int(capped.get("max_history_messages") or max_history_messages), max_history_messages)
        capped["history_char_budget"] = min(int(capped.get("history_char_budget") or history_char_budget), history_char_budget)
        capped["max_rag_hits"] = min(int(capped.get("max_rag_hits") or max_rag_hits), max_rag_hits)
        capped["max_rag_text_chars"] = min(int(capped.get("max_rag_text_chars") or max_rag_text_chars), max_rag_text_chars)
        capped["max_catalog_candidates"] = min(int(capped.get("max_catalog_candidates") or max_catalog_candidates), max_catalog_candidates)
        capped["max_tokens"] = min(int(capped.get("max_tokens") or max_tokens), max_tokens)
        capped_profiles[name] = capped
    if capped_profiles:
        settings["profiles"] = capped_profiles
    settings["foreground_realtime"] = True
    next_config["llm_reply_synthesis"] = settings
    return next_config


def initial_token_budget(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_prompt_tokens": int(route.get("max_prompt_tokens") or DEFAULT_MAX_PROMPT_TOKENS),
        "max_completion_tokens": int(route.get("max_completion_tokens") or DEFAULT_MAX_COMPLETION_TOKENS),
        "actual_prompt_tokens": 0,
        "actual_completion_tokens": 0,
        "actual_total_tokens": 0,
        "saved_reason": "" if route.get("foreground_llm_allowed") else str(route.get("reason") or "foreground_llm_not_allowed"),
    }


def update_token_budget_from_synthesis(budget: dict[str, Any], llm_synthesis: dict[str, Any]) -> dict[str, Any]:
    next_budget = dict(budget)
    usage = llm_synthesis.get("llm_usage") if isinstance(llm_synthesis.get("llm_usage"), dict) else {}
    prompt_estimate = llm_synthesis.get("prompt_estimate") if isinstance(llm_synthesis.get("prompt_estimate"), dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or prompt_estimate.get("rough_prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    next_budget.update(
        {
            "actual_prompt_tokens": prompt_tokens,
            "actual_completion_tokens": completion_tokens,
            "actual_total_tokens": total_tokens,
            "token_budget_exceeded": bool(total_tokens and total_tokens > int(next_budget.get("max_prompt_tokens", DEFAULT_MAX_PROMPT_TOKENS)) + int(next_budget.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS))),
        }
    )
    if total_tokens:
        next_budget["saved_reason"] = ""
    return next_budget


def is_followup_vehicle_source_request(text: str, recent_reply_texts: list[str]) -> bool:
    if not text or not recent_reply_texts:
        return False
    if is_service_guidance_question(text):
        return False
    recent_text = normalize_text(" ".join(recent_reply_texts[-3:]))
    if not contains_any(recent_text, RECENT_GUIDANCE_TERMS):
        return False
    if contains_any(text, VALUE_RETENTION_TERMS):
        return False
    if contains_any(text, TRADE_IN_TERMS) and not contains_any(text, FOLLOWUP_SOURCE_TERMS + EXPLICIT_SOURCE_TERMS + ("更想看", "想看", "备选", "当备选")):
        return False
    if requires_high_risk_boundary(text) or contains_any(text, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    if contains_any(text, APPOINTMENT_TERMS):
        return False
    return contains_any(text, FOLLOWUP_SOURCE_TERMS) or has_followup_context_reference(text)


def is_explicit_vehicle_source_request(text: str) -> bool:
    if not text:
        return False
    explicit_terms = EXPLICIT_SOURCE_TERMS + ("推荐", "优先看", "值得看", "更想看", "想看", "备选", "当备选")
    direct_model_price_query = False
    if contains_any(text, PRICE_QUERY_TERMS):
        if contains_catalog_product_term(text):
            direct_model_price_query = True
        else:
            model_price_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,16})(多少钱|什么价|报价|价格)", str(text))
            if model_price_match:
                candidate = normalize_text(model_price_match.group(1))
                generic_prefixes = ("估个", "估一", "准", "预算", "车款", "抵", "给我", "这边", "你们", "现在")
                if candidate and not contains_any(candidate, generic_prefixes):
                    direct_model_price_query = True
    if direct_model_price_query:
        return True
    if is_service_guidance_question(text) and not contains_any(text, explicit_terms):
        return False
    if contains_any(text, VALUE_RETENTION_TERMS):
        return False
    if contains_any(text, TRADE_IN_TERMS) and not contains_any(text, explicit_terms):
        return False
    if requires_high_risk_boundary(text) or contains_any(text, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    if contains_any(text, ("更想看", "想看", "备选", "当备选")) and contains_catalog_product_term(text):
        return True
    if contains_catalog_product_term(text) and contains_any(text, PRICE_QUERY_TERMS):
        return True
    if contains_any(text, EXPLICIT_SOURCE_TERMS):
        return True
    if is_direction_candidate_request(text):
        return True
    return "具体" in text and contains_any(text, ("推荐", "车源", "给我", "挑", "看哪", "哪台", "哪款", "哪一辆"))


def is_inventory_category_availability_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if requires_high_risk_boundary(normalized) or contains_any(normalized, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    has_availability_ask = contains_any(
        normalized,
        (
            "有没有",
            "有没",
            "有吗",
            "有不有",
            "还有吗",
            "还有没有",
            "现车有",
            "库存有",
        ),
    )
    if not has_availability_ask and re.search(r"有.{0,16}(?:车|车型|车源|现车|库存).{0,4}吗", normalized):
        has_availability_ask = True
    if not has_availability_ask:
        return False
    if not contains_any(normalized, ("车", "车型", "车源", "现车", "库存")):
        return False
    category_terms = (
        "德系",
        "日系",
        "美系",
        "韩系",
        "法系",
        "国产",
        "轿车",
        "suv",
        "mpv",
        "新能源",
        "油车",
        "混动",
        "七座",
        "自动挡",
        "豪华",
        "合资",
    )
    return contains_any(normalized, category_terms) or contains_catalog_product_term(normalized)


def is_direction_candidate_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized or "方向" not in normalized:
        return False
    if not contains_any(normalized, ("给我", "推荐", "挑", "筛", "先按", "缩到", "两三", "几个", "具体")):
        return False
    return is_used_car_query(normalized) or extract_budget_wan(normalized) > 0


def is_detailed_vehicle_need_ready(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if requires_high_risk_boundary(normalized) or contains_any(normalized, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    if extract_budget_wan(normalized) <= 0:
        return False
    if not is_used_car_query(normalized):
        return False
    detail_score = 0
    if contains_any(normalized, DETAILED_NEED_SOURCE_TERMS):
        detail_score += 1
    if contains_any(normalized, DETAILED_NEED_USE_TERMS):
        detail_score += 1
    if contains_any(normalized, DETAILED_NEED_PREFERENCE_TERMS):
        detail_score += 1
    if contains_any(normalized, VALUE_RETENTION_TERMS) and detail_score < 2:
        return False
    if (
        contains_any(normalized, TRADE_IN_TERMS)
        and detail_score < 2
        and not contains_any(normalized, ("推荐", "哪台", "哪款", "两台", "几台", "优先看"))
    ):
        return False
    return detail_score >= 2


def contextual_vehicle_need_ready_for_candidates(text: str, recent_reply_texts: list[str]) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    return (
        is_followup_vehicle_source_request(clean, recent_reply_texts)
        or is_explicit_vehicle_source_request(clean)
        or is_inventory_category_availability_request(clean)
        or is_detailed_vehicle_need_ready(clean)
    )


def current_guidance_can_yield_to_contextual_candidates(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    return is_comfort_highway_question(clean) and contains_any(
        clean,
        (
            "按刚才",
            "刚才需求",
            "怎么排",
            "怎么排优先级",
            "保值",
            "再卖",
            "亏太多",
            "后排舒服",
        ),
    )


def is_identity_probe(text: str) -> bool:
    normalized = normalize_text(text)
    return contains_any(normalized, IDENTITY_PROBE_TERMS)


def identity_guard_enabled(config: dict[str, Any]) -> bool:
    synthesis = config.get("llm_reply_synthesis") if isinstance(config.get("llm_reply_synthesis"), dict) else {}
    if "identity_guard_enabled" in synthesis:
        return synthesis.get("identity_guard_enabled") is not False
    style = config.get("reply_style_adapter") if isinstance(config.get("reply_style_adapter"), dict) else {}
    if "identity_guard_enabled" in style:
        return style.get("identity_guard_enabled") is not False
    return True


def build_identity_probe_reply(
    config: dict[str, Any],
    *,
    query: str = "",
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    if identity_guard_enabled(config):
        return choose_natural_reply_variant(
            [
                "这类内部信息不对外提供，您别介意；您关心车况、报价或手续哪块，我按实际情况帮您确认。",
                "这类内容不方便外发。您要是担心回复准不准，可以直接问具体车况、报价或者手续，我按实际情况给您核。",
                "提示词、内部规则这些肯定不能往外发；您关心哪台车、报价、手续还是置换，我继续帮您确认。",
                "内部规则就不展开说了。咱们直接聊车就行，车源、车况、价格这些我按能核实的给您回。",
            ],
            key_text=query or "identity_probe_guarded",
            recent_reply_texts=recent_reply_texts,
        )
    return choose_natural_reply_variant(
        [
            "是智能客服助手在协助回复，能直接确认的我会先答；涉及价格、车况、合同这些关键细节，还是会按门店实际情况核实。",
            "当前是智能客服先帮您接待，车源、需求和基础问题我可以先整理；关键承诺类信息会以门店核实结果为准。",
            "我是智能客服助手，可以先帮您查车源、梳理需求和记录重点；价格、合同、车况承诺这些还是按门店确认结果来。",
        ],
        key_text=query or "identity_probe_open",
        recent_reply_texts=recent_reply_texts,
    )


def choose_reply_variant(
    variants: list[str],
    *,
    key_text: str = "",
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    candidates = [str(item).strip() for item in variants if str(item).strip()]
    if not candidates:
        return "", 0
    recent_reply_texts = recent_reply_texts or []
    if not recent_reply_texts:
        index = stable_variant_index(key_text, len(candidates))
        return candidates[index], index
    scored: list[tuple[float, int, str]] = []
    for index, candidate in enumerate(candidates):
        max_similarity = max((reply_similarity(candidate, recent) for recent in recent_reply_texts[-5:]), default=0.0)
        scored.append((max_similarity, index, candidate))
    scored.sort(key=lambda item: (item[0], stable_variant_tiebreak(key_text, item[1])))
    _, index, candidate = scored[0]
    return candidate, index


def choose_natural_reply_variant(
    variants: list[str],
    *,
    key_text: str = "",
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    reply, index = choose_reply_variant(variants, key_text=key_text, recent_reply_texts=recent_reply_texts)
    return de_template_reply_text(reply, key_text=key_text, recent_reply_texts=recent_reply_texts), index


def de_template_reply_text(
    reply: str,
    *,
    key_text: str = "",
    recent_reply_texts: list[str] | None = None,
) -> str:
    """Reduce overused local-template phrases before a customer sees them."""
    text = str(reply or "").strip()
    if not text:
        return text
    recent_reply_texts = recent_reply_texts or []
    replacements: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("我先不乱推", ("先按您说的条件筛一下", "我按您这几个条件往下看", "先把范围收窄一点")),
        ("尽量别给您乱推荐", ("尽量把范围缩准一点", "避免范围越聊越散", "尽量筛得更贴近一点")),
        ("我先帮您记录", ("我把这点记下", "这块我记下", "信息我先留好")),
        ("我先记录", ("我把这点记下", "这块我记下", "信息我先留好")),
        ("我先帮您看", ("我帮您看", "这边帮您看", "我来帮您看")),
        ("我先帮您核", ("我帮您核", "这边帮您核", "我来核一下")),
        ("我先确认", ("我确认", "这边确认", "我来确认")),
        ("我先核实", ("我核实", "这边核实", "我来核一下")),
        ("我会重点看", ("重点看", "我主要看", "这边先核")),
        ("我会优先看", ("优先看", "我主要看", "这边先看")),
        ("我会按", ("我按", "这边按", "先按")),
        ("这个需求已经比较明确了", ("条件已经比较清楚了", "这个方向挺明确了", "需求这块已经不算泛了")),
        ("客户资料已记录", ("资料我收到了", "资料这边已经记下", "信息已经记下")),
        ("我会尽快为您继续处理", ("后面按这个继续跟进", "接下来按这个处理", "我这边继续往下跟")),
        ("我看到了客户资料，但还缺少", ("资料我看到了，还差", "信息收到了，还缺", "这边看到了，还需要补一下")),
        ("请补充后我再记录", ("您补一下我就接着处理", "您发我一下，我这边补齐", "补齐后我继续往下跟")),
        ("请您稍等片刻", ("您稍等一下", "您等我一下", "稍等我确认下")),
        ("请稍等我回复您", ("稍等我回复您", "等我确认后回您", "我核一下再回您")),
        ("稍等我回复您", ("等我确认后回您", "我核一下再回您", "确认后回您")),
        ("再给您准确处理意见", ("再跟您说怎么处理", "再把处理意见跟您说清楚", "再给您明确处理意见")),
        ("再给您准确说法", ("再跟您说清楚", "再给您明确说法", "再把情况讲清楚")),
        ("给您准确处理意见", ("给您明确处理意见", "把处理意见跟您说清楚", "再跟您说怎么处理")),
        ("给您准确说法", ("跟您说清楚", "给您明确说法", "再把情况讲清楚")),
        ("不能直接拍板", ("不能随口定", "不能直接定", "得按实际情况确认")),
        ("不随口答复", ("不直接乱说", "不能凭一句话定", "得看实际情况")),
        ("不随口承诺", ("不直接乱承诺", "不能凭一句话承诺", "得按实际情况确认")),
        ("确认后第一时间回复您", ("弄清楚后回您", "确认好就回您", "核完再回您")),
        ("确认后马上回您", ("确认好就回您", "核完再回您", "弄清楚后回您")),
        ("确认清楚后马上给您准话", ("核清楚再跟您说准", "确认好再给您明确答复", "弄清楚后再回您")),
        ("确认清楚后马上回复您", ("核清楚再回您", "确认好再给您明确答复", "弄清楚后再回您")),
        ("核实清楚后第一时间回复您", ("核清楚后回您", "弄清楚后再回您", "确认好再给您明确答复")),
        ("核清楚后给您准话", ("核清楚再跟您说准", "确认好再给您明确答复", "弄清楚后再回您")),
        ("再给您准话", ("再跟您说准", "再给您明确答复", "再把结果告诉您")),
        ("给您准话", ("跟您说准", "给您明确答复", "把结果告诉您")),
    )
    for old, variants in replacements:
        if old not in text:
            continue
        replacement, _ = choose_reply_variant(
            list(variants),
            key_text=f"{key_text}|{old}",
            recent_reply_texts=recent_reply_texts,
        )
        text = text.replace(old, replacement, 1)
    text = text.replace("再再", "再")
    return text


def stable_variant_index(key_text: str, count: int) -> int:
    if count <= 1:
        return 0
    digest = hashlib.sha256(str(key_text or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % count


def stable_variant_tiebreak(key_text: str, index: int) -> int:
    digest = hashlib.sha256(f"{key_text}|{index}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def reply_similarity(left: str, right: str) -> float:
    left_tokens = char_bigrams(normalize_text(left))
    right_tokens = char_bigrams(normalize_text(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def char_bigrams(text: str) -> set[str]:
    if len(text) <= 1:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def normalize_text(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def current_customer_text(text: str) -> str:
    raw = str(text or "")
    marker = "当前客户问题："
    if marker in raw:
        return strip_leading_group_speaker_line(raw.rsplit(marker, 1)[-1].strip())
    stripped = raw.strip()
    if not stripped:
        return ""
    stripped = strip_leading_group_speaker_line(stripped)
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 2:
        transcript_like_count = sum(1 for line in lines if is_transcript_line(line))
        if transcript_like_count < 2:
            return stripped
        for line in reversed(lines):
            candidate = strip_transcript_line_prefix(line)
            if candidate:
                return candidate
    return stripped


def strip_leading_group_speaker_line(text: str) -> str:
    split = split_wechat_ocr_speaker_prefix(text)
    return str(split.get("content") or "").strip() if split.get("changed") else str(text or "").strip()


def is_transcript_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    return bool(
        re.match(r"^\[[^\]]{1,72}\]\s*", text)
        or re.match(r"^\d{1,2}:\d{2}(?::\d{2})?\s*", text)
        or re.match(
            r"^(?:self|user|customer|client|assistant|system|agent|客服|客户)\s*[:：]\s*",
            text,
            flags=re.IGNORECASE,
        )
    )


def strip_transcript_line_prefix(line: str) -> str:
    candidate = str(line or "").strip()
    if not candidate:
        return ""
    candidate = re.sub(r"^\[[^\]]{1,72}\]\s*", "", candidate).strip()
    candidate = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s*", "", candidate).strip()
    candidate = re.sub(
        r"^(?:self|user|customer|client|assistant|system|agent|客服|客户)\s*[:：]\s*",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()
    return candidate


def has_realtime_context_prefix(text: str) -> bool:
    raw = str(text or "")
    return "近期客户需求：" in raw and "当前客户问题：" in raw


def realtime_contextual_customer_text(text: str, *, recent_reply_texts: list[str] | None = None) -> str:
    """Return current text plus compact history only when the current turn asks for it.

    File Transfer Assistant is often used for chaotic self-tests, so the router
    must not blindly let old messages steer every new turn. The history prefix is
    only trusted for follow-up recommendation/comparison scenes where customers
    naturally say things like "按前面说的挑两台".
    """
    raw = str(text or "").strip()
    current = current_customer_text(raw)
    if not raw or not current or not has_realtime_context_prefix(raw):
        return current
    normalized_current = normalize_text(current)
    if (
        requires_high_risk_boundary(normalized_current)
        or contains_any(normalized_current, OFF_TOPIC_OR_SECURITY_TERMS)
        or is_identity_probe(normalized_current)
        or is_actual_customer_data_message(normalized_current)
    ):
        return current
    context_candidate_markers = (
        FOLLOWUP_TERMS
        + FOLLOWUP_SOURCE_TERMS
        + EXPLICIT_SOURCE_TERMS
        + VEHICLE_COMPARE_TERMS
        + (
            "别再问",
            "不用再问",
            "按这个",
            "就按这个",
            "按前面",
            "前面说的",
            "刚说的",
            "这俩",
            "它俩",
            "哪个更适合",
            "哪台更适合",
            "好停",
            "好开",
        )
    )
    if contains_any(normalized_current, context_candidate_markers):
        return raw
    if contains_any(normalized_current, RECOMMEND_TERMS) and has_business_signal(normalize_text(raw)):
        return raw
    if recent_reply_texts and contains_any(normalized_current, ("具体", "怎么选", "哪个", "哪台", "哪一辆", "合适")):
        return raw
    return current


def extract_visit_time_label(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    normalized = normalize_text(raw)
    day = ""
    for marker in ("周一", "周二", "周三", "周四", "周五", "周六", "周日", "今天", "明天", "后天"):
        if normalize_text(marker) in normalized:
            day = marker
            break
    period = ""
    for marker in ("上午", "中午", "下午", "晚上"):
        if marker in raw:
            period = marker
            break
    clock = ""
    match = re.search(r"([一二三四五六七八九十两\d]{1,3}点(?:半)?)", raw)
    if match:
        clock = match.group(1)
    label = f"{day}{period}{clock}".strip()
    return label


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms if term)


def contains_catalog_product_term(text: str) -> bool:
    try:
        return contains_product_term(text, include_generic=False)
    except Exception:
        return False


def contains_used_car_product_signal(text: str) -> bool:
    normalized = normalize_text(text)
    return contains_any(normalized, USED_CAR_PRODUCT_TERMS) or contains_catalog_product_term(normalized)


def is_short_greeting(text: str) -> bool:
    clean = normalize_text(text)
    if not clean or len(clean) > 14:
        return False
    if has_business_signal(clean):
        return False
    return contains_any(clean, GREETING_TERMS + SOCIAL_SUMMON_TERMS)


def is_friendly_farewell_message(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(clean, FAREWELL_TERMS):
        return True
    return len(clean) <= 18 and contains_any(clean, POLITE_SMALLTALK_TERMS) and not has_business_signal(clean)


def is_offtopic_social_message(text: str, recent_reply_texts: list[str] | None = None) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    recent = recent_reply_texts or []
    # Off-topic soft redirect should never preempt legitimate business intents.
    if (
        current_turn_preempts_vehicle_candidate_reply(clean, recent)
        or is_followup_vehicle_source_request(clean, recent)
        or is_explicit_vehicle_source_request(clean)
        or is_inventory_category_availability_request(clean)
        or is_detailed_vehicle_need_ready(clean)
        or is_service_guidance_question(clean)
        or is_vehicle_compare_question(clean, recent)
    ):
        return False
    if has_business_signal(clean):
        return False
    # Guard business-like guidance first: off-topic routing must not preempt
    # legitimate customer-service questions.
    if contains_any(
        clean,
        PRICE_QUERY_TERMS
        + PRICE_NEGOTIATION_TERMS
        + VEHICLE_COMPARE_TERMS
        + FOLLOWUP_SOURCE_TERMS
        + EXPLICIT_SOURCE_TERMS
        + TRADE_IN_TERMS
        + VALUE_RETENTION_TERMS
        + APPOINTMENT_TERMS,
    ):
        return False
    if is_identity_probe(clean) or contains_any(clean, OFF_TOPIC_OR_SECURITY_TERMS):
        return False
    if contains_any(clean, HIGH_RISK_TERMS):
        return False
    if is_short_greeting(clean) or is_friendly_farewell_message(clean):
        return False
    # Store-operation questions are business-adjacent and should not be treated
    # as pure social smalltalk.
    if contains_any(clean, ("门店", "店里", "地址", "营业", "上班", "联系", "电话", "到店", "试驾", "看车")):
        return False
    if contains_any(clean, OFFTOPIC_SOCIAL_TERMS):
        return True
    social_smalltalk_cues = (
        "你觉得",
        "怎么看",
        "在干嘛",
        "做什么",
        "做饭",
        "心情",
        "压力",
        "无聊",
        "人生",
        "聊聊",
        "最近咋样",
        "最近怎么样",
    )
    if len(clean) <= 26 and contains_any(clean, social_smalltalk_cues):
        return True
    social_question_cues = (
        "比赛",
        "球赛",
        "球队",
        "球员",
        "演唱会",
        "追剧",
        "片单",
        "新片",
        "热搜",
        "明星",
        "八卦",
        "段子",
        "表情包",
        "吃啥",
        "吃什么",
        "去哪玩",
        "周末去哪",
        "忙不忙",
        "睡了吗",
    )
    if is_question_like(clean) and len(clean) <= 36 and contains_any(clean, social_question_cues):
        return True
    if is_question_like(clean) and len(clean) <= 18:
        return True
    return len(clean) <= 22 and contains_any(clean, POLITE_SMALLTALK_TERMS)


def is_question_like(text: str) -> bool:
    clean = str(text or "").strip()
    return any(
        marker in clean
        for marker in (
            "?",
            "？",
            "吗",
            "么",
            "怎么",
            "多少",
            "能不能",
            "有没有",
            "可不可以",
            "到底",
            "哪个",
            "哪台",
            "哪款",
            "选哪",
        )
    )


def is_compound_multi_question_message(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or "\n" not in raw:
        return False
    lines = [line.strip(" \t-•·、") for line in raw.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    question_lines = [
        line
        for line in lines
        if (
            is_question_like(line)
            or bool(re.match(r"^\d+[.、\)]\s*", line))
        )
        and not line.startswith("客户连续")
        and not line.startswith("当前客户问题")
        and len(normalize_text(line)) >= 6
    ]
    return len(question_lines) >= 2


def is_customer_challenge_direct_answer_request(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if not contains_any(clean, CUSTOMER_CHALLENGE_TERMS):
        return False
    if not has_business_signal(clean):
        return False
    return is_question_like(clean) or contains_any(clean, ("哪个", "哪台", "哪款", "到底", "先回答", "二选一", "给结论", "直接给结论"))


def has_business_signal(text: str) -> bool:
    if contains_any(text, RECOMMEND_TERMS + APPOINTMENT_TERMS + FOLLOWUP_SOURCE_TERMS + EXPLICIT_SOURCE_TERMS + VALUE_RETENTION_TERMS + TRADE_IN_TERMS + HIGH_RISK_TERMS):
        return True
    if re.search(r"\d+(?:\.\d+)?万", text):
        return True
    return contains_any(text, ("车", "预算", "通勤", "家用", "接娃", "贷款", "置换", "到店", "看车"))


def is_generic_price_approval_handoff_reply(normalized_text: str) -> bool:
    """Detect generic price/approval handoffs that should not block product candidates."""
    if not normalized_text:
        return False
    has_price_or_approval_scope = contains_any(
        normalized_text,
        ("价格", "优惠", "最低价", "额外优惠", "库存", "审批", "负责人", "核实", "确认"),
    )
    has_non_commitment_boundary = contains_any(
        normalized_text,
        ("不方便口头", "不能直接", "口头承诺", "口头保证", "不能保证", "不敢保证", "不直接答应"),
    )
    has_followup_action = contains_any(
        normalized_text,
        ("我先核", "我来核", "核实下", "核清楚", "确认后", "确认清楚", "负责人确认", "负责人意见", "马上回复", "明确答复", "给您准话"),
    )
    return has_price_or_approval_scope and has_non_commitment_boundary and has_followup_action


def strip_current_reply_service_prefix(text: str, config: dict[str, Any] | None = None) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    prefixes: list[str] = []
    reply_config = config.get("reply", {}) if isinstance(config, dict) else {}
    configured_prefix = str(reply_config.get("prefix") or "").strip()
    if configured_prefix:
        prefixes.extend([configured_prefix, configured_prefix.rstrip()])
    prefixes.extend(
        (
            "[车金实盘]",
            "[车金客服]",
            "[OmniAuto客服]",
            "[OmniAuto自测]",
            "[OmniAuto文件助手测试]",
            "[FTA连续验收]",
        )
    )
    for prefix in sorted({item.strip() for item in prefixes if item and item.strip()}, key=len, reverse=True):
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    label_match = re.match(r"^[\[【（(][^\]】）)]{1,24}[\]】）)]\s*", body)
    if label_match:
        body = body[label_match.end() :].strip()
    return body.lstrip(":：，,。.;；-— ")


def current_reply_has_substantive_body(text: str, config: dict[str, Any] | None = None) -> bool:
    body = strip_current_reply_service_prefix(text, config)
    normalized = normalize_text(body)
    if not normalized:
        return False
    if is_low_information_ack_reply(normalized):
        return False
    prefix_only_labels = {
        "车金实盘",
        "车金客服",
        "omniauto客服",
        "omniauto自测",
        "omniauto文件助手测试",
        "fta连续验收",
    }
    return normalized not in prefix_only_labels


def can_override_current_reply(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return True
    if is_low_information_ack_reply(normalized):
        return True
    generic_markers = (
        "收到我先记录",
        "我先记录一下",
        "涉及车况价格金融或到店安排",
        "暂时没匹配到",
        "稍后确认",
        "需要负责人确认",
        "请示负责人",
        "问清楚后",
        "内部确认",
        "我把情况记下",
        "最低价或破例优惠不能直接口头保证",
        "价格和优惠我会帮您核",
        "价格我会尽量帮您争取",
        "价格我肯定帮您争取",
        "超出公开规则",
        "负责人意见",
        "争取归争取",
        "价格、库存和审批结果",
        "核清楚后再给您准话",
        "核清楚后再给您明确答复",
        "商品数量和负责人意见",
        "商品、数量和负责人意见",
        "数量库存和负责人意见",
        "数量、库存和负责人意见",
        "具体价格库存及审批结果",
        "具体价格、库存及审批结果",
        "最终核实为准",
    )
    return any(marker in normalized for marker in generic_markers) or is_generic_price_approval_handoff_reply(normalized)


def is_low_information_ack_reply(normalized_text: str) -> bool:
    value = normalize_text(normalized_text)
    if not value:
        return True
    exact = {
        "收到我先看一下",
        "收到我先看下",
        "收到先看一下",
        "收到先看下",
        "我先看一下",
        "我先看下",
    }
    if value in exact:
        return True
    return any(marker in value for marker in ("收到我先看一下", "收到我先看下", "先看一下", "先看下")) and len(value) <= 14


def current_turn_preempts_vehicle_candidate_reply(text: str, recent_reply_texts: list[str]) -> bool:
    """Keep an explicit current-turn service question from being hijacked by older needs."""
    clean = normalize_text(text)
    if not clean:
        return False
    if is_vehicle_compare_question(clean, recent_reply_texts):
        return True
    if (
        is_followup_vehicle_source_request(clean, recent_reply_texts)
        or is_explicit_vehicle_source_request(clean)
        or is_inventory_category_availability_request(clean)
        or is_detailed_vehicle_need_ready(clean)
    ):
        return False
    if any(
        checker(clean)
        for checker in (
            is_fee_transparency_question,
            is_finance_guidance_question,
            is_testdrive_material_question,
            is_visit_report_prep_question,
            is_visit_timing_question,
            is_no_deposit_visit_question,
            is_price_negotiation_question,
            is_warranty_guidance_question,
        )
    ):
        return True
    return any(
        checker(clean)
        for checker in (
            is_vehicle_type_guidance_question,
            is_maintenance_cost_question,
            is_inspection_guidance_question,
            is_cargo_space_guidance_question,
            is_feature_guidance_question,
            is_comfort_highway_question,
        )
    )


def is_appointment_context_query(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    if contains_any(clean, ("周末经常", "平时周末", "每周末", "周末带", "周末用")):
        return False
    action_terms = ("看车", "能看", "去看", "到店", "试驾", "试车", "安排", "过去", "预约", "车还在", "来店")
    time_terms = ("周末", "周六", "周日", "上午", "下午", "今天", "明天", "几点", "时间")
    return contains_any(clean, action_terms) and (
        contains_any(clean, time_terms)
        or contains_any(clean, ("别白跑", "白跑", "排期", "一次看", "对比"))
    )


def rank_product_candidates(
    query: str,
    evidence_pack: dict[str, Any],
    *,
    allow_catalog_fallback: bool = False,
    allow_broad_fallback: bool = False,
) -> list[dict[str, Any]]:
    products = (evidence_pack.get("evidence", {}) or {}).get("products", []) or []
    catalog_products: list[dict[str, Any]] = []
    if allow_catalog_fallback and not products:
        products = load_catalog_product_candidates()
    elif allow_catalog_fallback:
        catalog_products = load_catalog_product_candidates()
    budget = extract_budget_wan(query)
    budget_range = extract_budget_range_wan(query)
    strict_budget_cap = has_strict_budget_cap(query)
    used_car_query = is_used_car_query(query)
    requested_semantic_tags = requested_vehicle_semantic_tags(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    score_product_items(
        query=query,
        items=products,
        budget=budget,
        budget_range=budget_range,
        strict_budget_cap=strict_budget_cap,
        used_car_query=used_car_query,
        requested_semantic_tags=requested_semantic_tags,
        scored=scored,
        allow_broad_fallback=allow_broad_fallback,
    )
    if allow_catalog_fallback and catalog_products:
        # Evidence retrieval can be semantically narrow. Merge the
        # authoritative catalog so a clear budget range is not trapped by
        # cheaper-but-keyword-similar hits.
        evidence_ids = {str(item.get("id") or "") for item in products if isinstance(item, dict)}
        extra_catalog_products = [
            item
            for item in catalog_products
            if str(item.get("id") or "") not in evidence_ids
        ]
        score_product_items(
            query=query,
            items=extra_catalog_products,
            budget=budget,
            budget_range=budget_range,
            strict_budget_cap=strict_budget_cap,
            used_car_query=used_car_query,
            requested_semantic_tags=requested_semantic_tags,
            scored=scored,
            allow_broad_fallback=True,
        )
    scored.sort(key=lambda pair: (pair[0], float(pair[1].get("price") or 0)), reverse=True)
    return [item for _, item in scored]


def score_product_items(
    *,
    query: str,
    items: list[dict[str, Any]],
    budget: float,
    strict_budget_cap: bool,
    used_car_query: bool,
    requested_semantic_tags: set[str],
    scored: list[tuple[float, dict[str, Any]]],
    allow_broad_fallback: bool,
    budget_range: tuple[float, float] | None = None,
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            price = 0.0
        range_low, range_high = budget_range if budget_range else (0.0, budget)
        budget_upper = range_high or budget
        searchable = product_search_text(item)
        matched_aliases = explicit_product_aliases_for_query(item, query)
        # Used-car conversations commonly tolerate a small upward stretch when
        # the match is stronger, especially around "8万/10万预算" shorthand.
        if budget_upper and price and strict_budget_cap and price > budget_upper and not matched_aliases:
            continue
        if budget_upper and price and not strict_budget_cap and price > max(budget_upper * 1.15, budget_upper + 0.8) and not matched_aliases:
            continue
        if used_car_query and not is_used_car_product_search_text(searchable):
            continue
        item_semantic_tags = set(derived_vehicle_semantic_tags(item))
        if requested_semantic_tags and not (requested_semantic_tags & item_semantic_tags):
            continue
        if requires_mpv_or_seven_seat_candidate(query) and not is_mpv_or_seven_seat_product_search_text(searchable):
            continue
        score = product_query_score(query, searchable)
        if matched_aliases:
            score += 80.0 + max(len(normalize_text(alias)) for alias in matched_aliases if normalize_text(alias))
        if budget and price:
            if budget_range:
                score += budget_range_position_score(price=price, low=range_low, high=range_high, strict_budget_cap=strict_budget_cap)
            else:
                score += max(0.0, 4.0 - abs(budget - price) / max(budget, 1.0) * 4.0)
                score += budget_position_score(query, price=price, budget=budget, strict_budget_cap=strict_budget_cap)
        if price and price <= 10 and (not budget or budget <= 10):
            score += 0.8
        score += persona_fit_score(query, searchable)
        if int(item.get("stock") or 0) > 0:
            score += 0.5
        if score > 0:
            scored.append((score, item))
        elif allow_broad_fallback:
            fallback_score = broad_product_fallback_score(item, budget=budget, strict_budget_cap=strict_budget_cap)
            if fallback_score > 0:
                scored.append((fallback_score, item))


def is_used_car_query(query: str) -> bool:
    normalized = normalize_text(query)
    if contains_any(normalized, USED_CAR_EXPLICIT_QUERY_TERMS):
        return True
    has_budget_signal = extract_budget_wan(normalized) > 0
    return has_budget_signal and contains_any(normalized, USED_CAR_SCENE_QUERY_TERMS)


def is_used_car_product_search_text(searchable: str) -> bool:
    return contains_any(normalize_text(searchable), USED_CAR_PRODUCT_TERMS)


def requires_mpv_or_seven_seat_candidate(query: str) -> bool:
    """Narrow inventory only when the buyer explicitly asks for MPV/seven-seat."""
    normalized = normalize_text(query)
    if not normalized:
        return False
    if contains_any(
        normalized,
        ("suv或mpv", "suv或者mpv", "suv/mpv", "suv和mpv", "suv、mpv", "suv也行", "suv也可以"),
    ):
        return False
    return contains_any(
        normalized,
        (
            "mpv",
            "大七座",
            "七座",
            "7座",
            "六座",
            "6座",
            "商务车",
            "gl8",
            "奥德赛",
            "塞纳",
            "威然",
            "传祺m8",
        ),
    )


def is_mpv_or_seven_seat_product_search_text(searchable: str) -> bool:
    normalized = normalize_text(searchable)
    return contains_any(
        normalized,
        (
            "二手车/mpv",
            "mpv",
            "大七座",
            "七座",
            "7座",
            "六座",
            "6座",
            "商务车",
            "商务mpv",
            "gl8",
            "奥德赛",
            "塞纳",
            "威然",
            "传祺m8",
        ),
    )


def product_query_score(query: str, searchable: str) -> float:
    query_text = normalize_text(query)
    searchable_text = normalize_text(searchable)
    score = 0.0
    for token, weight in (
        ("通勤", 2.0),
        ("代步", 2.0),
        ("省油", 2.0),
        ("自动", 1.2),
        ("suv", 2.2),
        ("空间", 1.4),
        ("孩子", 0.8),
        ("后排", 1.0),
        ("家用", 1.5),
        ("上下班", 1.8),
        ("女士", 0.6),
        ("一手", 0.6),
        ("高速", 1.0),
        ("舒服", 1.0),
        ("舒适", 1.0),
        ("父母", 0.8),
        ("老人", 0.8),
        ("露营", 1.5),
        ("钓鱼", 1.2),
        ("后备箱", 1.4),
        ("后备厢", 1.4),
        ("摄影", 1.0),
        ("工作室", 1.0),
        ("活动策划", 1.0),
        ("接客户", 1.0),
        ("甲方客户", 1.0),
        ("器材", 1.3),
        ("灯架", 1.3),
        ("展架", 1.3),
        ("物料", 1.3),
        ("音响架", 1.3),
        ("折叠桌", 1.1),
        ("相机箱", 1.3),
        ("底盘高", 1.4),
        ("通过性", 1.4),
        ("德系", 2.4),
        ("日系", 2.4),
        ("美系", 2.0),
        ("国产", 1.8),
        ("合资", 1.8),
        ("新能源", 2.0),
        ("混动", 1.8),
        ("纯电", 1.8),
    ):
        if normalize_text(token) in query_text and normalize_text(token) in searchable_text:
            score += weight
    if "suv" in query_text and "suv" not in searchable_text:
        score -= 3.0
    if contains_any(query_text, ("露营", "钓鱼", "郊外", "后备箱", "底盘高", "通过性")):
        if contains_any(searchable_text, ("suv", "四驱", "空间", "后备箱", "底盘", "通过性", "2.0t", "2.5l")):
            score += 2.2
        if contains_any(searchable_text, ("紧凑型轿车", "小型轿车", "两厢", "轿车")) and "suv" in query_text:
            score -= 2.2
    if contains_any(query_text, ("摄影", "工作室", "活动策划", "接客户", "甲方客户", "客户接待", "商务接待", "器材", "灯架", "展架", "物料", "音响架", "折叠桌", "相机箱", "后备厢", "后备箱")):
        if contains_any(searchable_text, ("suv", "mpv", "空间", "后备", "电动尾门", "七座", "商务", "客户", "大空间")):
            score += 2.4
        if contains_any(searchable_text, ("小型轿车", "紧凑型轿车", "新能源轿车", "轿车", "两厢", "小巧", "练车")) and not contains_any(searchable_text, ("suv", "mpv")):
            score -= 2.4
    if contains_any(query_text, ("父母", "老人", "高速", "舒服", "舒适", "回老家")) and contains_any(
        searchable_text,
        ("豪华轿车", "中大型", "mpv", "suv", "空间", "舒适", "保养记录", "一手", "混动"),
    ):
        score += 1.8
    if contains_any(query_text, ("二胎", "全家", "老人孩子", "孩子", "老人", "回老家", "后排", "空间")):
        if contains_any(searchable_text, ("suv", "中型", "中大型", "空间", "后排", "后备箱", "2.0t", "2.5l", "混动", "大空间")):
            score += 2.4
        if contains_any(searchable_text, ("小型轿车", "两厢", "小巧", "练车")):
            score -= 1.8
    if contains_any(query_text, ("后期小毛病", "小毛病", "省心", "少操心")) and contains_any(
        searchable_text,
        ("保养记录", "一手", "原版原漆", "保有量", "主流", "检测报告"),
    ):
        score += 1.2
    if contains_any(query_text, ("老婆开", "媳妇开", "给老婆", "我老婆", "老婆换", "老婆用", "老婆接送", "女士开", "女司机")) and contains_any(
        searchable_text,
        ("女士", "小巧", "好停车", "倒车", "影像", "雷达", "360"),
    ):
        score += 2.0
    if contains_any(query_text, ("停车", "倒车", "影像", "雷达")) and contains_any(
        searchable_text,
        ("倒车", "影像", "雷达", "360", "小巧", "好停车"),
    ):
        score += 2.6
    if contains_any(query_text, ("停车不太熟", "停车不熟", "倒车不熟")) and "suv" in searchable_text:
        score -= 1.2
    return score


def persona_fit_score(query: str, searchable: str) -> float:
    """Prefer candidates that fit the buyer's lived scenario, not just price."""
    query_text = normalize_text(query)
    searchable_text = normalize_text(searchable)
    score = 0.0
    new_driver_or_parking = contains_any(
        query_text,
        ("老婆开", "媳妇开", "给老婆", "我老婆", "老婆换", "老婆用", "老婆接送", "女士开", "女司机", "新手", "停车", "好停", "好开", "别太大", "不太大"),
    )
    explicit_suv_need = contains_any(query_text, ("想要suv", "要suv", "只看suv", "suv优先", "大空间suv"))
    if new_driver_or_parking:
        if contains_any(searchable_text, ("小型轿车", "紧凑型轿车", "两厢", "小巧", "好停车", "女士一手", "新手", "练车")):
            score += 1.8
        if contains_any(searchable_text, ("倒车影像", "倒车", "雷达", "360")):
            score += 0.8
        if contains_any(searchable_text, ("suv", "越野", "四驱")) and not explicit_suv_need:
            budget = extract_budget_wan(query_text)
            spouse_context = contains_any(query_text, ("老婆", "媳妇", "爱人", "女士", "女司机"))
            if contains_any(query_text, ("停车", "好停", "别太大", "不太大")):
                score -= 3.2
            elif spouse_context and budget and budget <= 9:
                score -= 5.0
            else:
                score -= 2.0
    if contains_any(query_text, ("省心", "少操心", "后期", "好养")):
        if contains_any(searchable_text, ("省油", "保值", "一手", "公里数少", "保养记录", "车况透明", "好开")):
            score += 1.0
        if contains_any(searchable_text, ("维修成本", "后期成本", "好养")):
            score += 0.8
    business_equipment_need = contains_any(
        query_text,
        (
            "摄影",
            "工作室",
            "外拍",
            "接客户",
            "客户接待",
            "商务接待",
            "公司用",
            "器材",
            "灯架",
            "相机箱",
            "背景架",
            "后备厢",
            "后备箱",
            "拉东西",
            "装东西",
        ),
    )
    if business_equipment_need:
        if contains_any(searchable_text, ("suv", "mpv", "空间大", "后备", "电动尾门", "七座", "商务", "客户", "大空间")):
            score += 2.2
        if contains_any(searchable_text, ("小型轿车", "紧凑型轿车", "新能源轿车", "轿车", "两厢", "小巧", "练车")) and not contains_any(searchable_text, ("suv", "mpv")):
            score -= 2.2
    return score


def budget_position_score(query: str, *, price: float, budget: float, strict_budget_cap: bool) -> float:
    """Avoid recommending much-cheaper cars just because they are below a loose cap."""
    if not price or not budget:
        return 0.0
    query_text = normalize_text(query)
    budget_limited = contains_any(query_text, ("预算有限", "越便宜越好", "便宜点", "练手", "代步就行", "几万"))
    ratio = price / max(budget, 1.0)
    if budget_limited:
        return 0.0
    if strict_budget_cap:
        # "X万以内" usually means "around X with a hard cap", not "as cheap as possible".
        if ratio < 0.55:
            return -2.4
        if 0.55 <= ratio < 0.65:
            return -0.8
        if 0.82 <= ratio <= 1.0:
            return 1.1
        if 0.65 <= ratio < 0.82:
            return 0.2
    else:
        if ratio < 0.5:
            return -1.2
        if 0.5 <= ratio < 0.65:
            return -0.4
        if 0.78 <= ratio <= 1.05:
            return 0.7
    return 0.0


def budget_range_position_score(*, price: float, low: float, high: float, strict_budget_cap: bool) -> float:
    """Strongly prefer cars inside the buyer's stated range, not merely below the upper number."""
    if not price or not low or not high:
        return 0.0
    lower = min(low, high)
    upper = max(low, high)
    midpoint = (lower + upper) / 2.0
    span = max(upper - lower, 1.0)
    if lower <= price <= upper:
        return 8.0 - min(1.5, abs(price - midpoint) / span * 1.5)
    if price < lower:
        gap_ratio = (lower - price) / max(lower, 1.0)
        if gap_ratio <= 0.08:
            return 1.0
        if gap_ratio <= 0.15:
            return -1.8
        return -6.0 - min(4.0, gap_ratio * 8.0)
    if strict_budget_cap:
        return -8.0
    over_ratio = (price - upper) / max(upper, 1.0)
    if over_ratio <= 0.05:
        return 0.4
    return -2.0 - min(4.0, over_ratio * 8.0)


def product_search_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("name") or ""),
        str(item.get("category") or ""),
        str(item.get("spec") or item.get("specs") or ""),
        str(item.get("shipping") or item.get("shipping_policy") or ""),
        str(item.get("warranty") or ""),
        str(item.get("recommendation") or ""),
        " ".join(str(alias) for alias in item.get("aliases", []) or []),
        " ".join(str(alias) for alias in item.get("matched_aliases", []) or []),
        " ".join(derived_vehicle_semantic_tags(item)),
    ]
    return " ".join(parts)


def derived_vehicle_semantic_tags(item: dict[str, Any]) -> list[str]:
    """Read searchable vehicle class tags that are explicitly carried by product-master data."""
    searchable = normalize_text(
        " ".join(
            [
                str(item.get("category") or ""),
                " ".join(str(alias) for alias in item.get("aliases", []) or []),
                " ".join(str(alias) for alias in item.get("matched_aliases", []) or []),
                " ".join(str(tag) for tag in item.get("tags", []) or []),
            ]
        )
    )
    if not searchable:
        return []
    return [tag for tag in ("德系", "日系", "美系", "韩系", "法系", "国产") if tag in searchable]


def requested_vehicle_semantic_tags(query: str) -> set[str]:
    normalized = normalize_text(query)
    tags: set[str] = set()
    for tag in ("德系", "日系", "美系", "韩系", "法系", "国产"):
        if tag in normalized:
            tags.add(tag)
    return tags


def load_catalog_product_candidates() -> list[dict[str, Any]]:
    try:
        items = KnowledgeRuntime().list_items("products")
    except Exception:
        return []
    products: list[dict[str, Any]] = []
    for item in items:
        data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
        runtime = item.get("runtime", {}) if isinstance(item.get("runtime"), dict) else {}
        if item.get("status") == "archived" or runtime.get("allow_auto_reply") is False:
            continue
        products.append(
            {
                "id": item.get("id"),
                "name": data.get("name"),
                "category": data.get("category"),
                "price": data.get("price"),
                "stock": data.get("inventory"),
                "spec": data.get("specs"),
                "shipping": data.get("shipping_policy"),
                "warranty": data.get("warranty_policy"),
                "recommendation": (data.get("reply_templates") or {}).get("recommendation") if isinstance(data.get("reply_templates"), dict) else "",
                "aliases": data.get("aliases", []) or [],
            }
        )
    return products


def broad_product_fallback_score(item: dict[str, Any], *, budget: float = 0.0, strict_budget_cap: bool = False) -> float:
    try:
        price = float(item.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    stock = int(item.get("stock") or 0)
    if stock <= 0:
        return 0.0
    if budget and price:
        if strict_budget_cap and price > budget:
            return 0.0
        if price > max(budget * 1.2, budget + 1.0):
            return 0.0
        return 2.0 + max(0.0, 3.0 - abs(budget - price) / max(budget, 1.0) * 3.0)
    if 5 <= price <= 12:
        return 2.0 + (12 - price) / 12
    if 12 < price <= 16:
        return 1.2
    return 0.5 if price else 0.0


def extract_budget_range_wan(text: str) -> tuple[float, float] | None:
    compact = normalize_text(text)
    for pattern in (
        r"(\d+(?:\.\d+)?)\s*(?:到|至|~|～|-|—|－)\s*(\d+(?:\.\d+)?)\s*万",
        r"(\d+(?:\.\d+)?)\s*万\s*(?:到|至|~|～|-|—|－)\s*(\d+(?:\.\d+)?)\s*万",
    ):
        match = re.search(pattern, compact)
        if not match:
            continue
        try:
            first = float(match.group(1))
            second = float(match.group(2))
        except ValueError:
            continue
        low, high = sorted((first, second))
        if high > low:
            return low, high
    chinese_ranges: dict[str, tuple[float, float]] = {
        "三四万": (3.0, 4.0),
        "四五万": (4.0, 5.0),
        "五六万": (5.0, 6.0),
        "六七万": (6.0, 7.0),
        "七八万": (7.0, 8.0),
        "八九万": (8.0, 9.0),
        "十一二万": (11.0, 12.0),
        "十二三万": (12.0, 13.0),
        "十三四万": (13.0, 14.0),
        "十四五万": (14.0, 15.0),
        "十五六万": (15.0, 16.0),
    }
    for marker, value in sorted(chinese_ranges.items(), key=lambda item: len(item[0]), reverse=True):
        if marker in compact:
            return value
    return None


def extract_budget_wan(text: str) -> float:
    compact = normalize_text(text)
    match = re.search(r"(\d+(?:\.\d+)?)\s*万", compact)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    chinese_ranges = {
        "三四万": 3.5,
        "四五万": 4.5,
        "五六万": 5.5,
        "六七万": 6.5,
        "七八万": 7.5,
        "八九万": 8.5,
        "十来万": 10.0,
        "十万出头": 10.5,
        "十万": 10.0,
        "十一二万": 11.5,
        "十二三万": 12.5,
        "十三四万": 13.5,
        "十四五万": 14.5,
        "十五六万": 15.5,
    }
    for marker, value in sorted(chinese_ranges.items(), key=lambda item: len(item[0]), reverse=True):
        if marker in compact:
            return value
    if "十来万" in compact or "十万" in compact:
        return 10.0
    if "八万" in compact:
        return 8.0
    if "七万" in compact:
        return 7.0
    if "六万" in compact:
        return 6.0
    if "五万" in compact:
        return 5.0
    if "四万" in compact:
        return 4.0
    if "三万" in compact:
        return 3.0
    return 0.0


def has_strict_budget_cap(text: str) -> bool:
    normalized = normalize_text(text)
    return extract_budget_wan(normalized) > 0 and contains_any(normalized, STRICT_BUDGET_CAP_TERMS)


def build_need_summary(query: str) -> str:
    normalized = normalize_text(query)
    parts: list[str] = []
    budget = extract_budget_wan(normalized)
    budget_range = extract_budget_range_wan(normalized)
    if budget_range:
        low, high = budget_range
        parts.append(f"{low:g}-{high:g}万")
    elif budget:
        suffix = "以内" if has_strict_budget_cap(normalized) else "左右"
        parts.append(f"{budget:g}万{suffix}")
    if contains_any(normalized, ("老婆开", "媳妇开", "给老婆", "我老婆", "老婆换", "老婆用", "老婆接送")):
        parts.append("主要给您爱人开")
    elif contains_any(normalized, ("女士开", "女司机")):
        parts.append("女士开")
    if contains_any(normalized, ("停车不太熟", "停车不熟", "倒车不熟", "停车")):
        parts.append("停车要好上手")
    if "自动挡" in normalized:
        parts.append("自动挡")
    if contains_any(normalized, ("倒车影像", "影像", "雷达", "360")):
        parts.append("倒车/影像配置优先")
    if contains_any(normalized, ("摄影", "工作室", "活动策划", "外拍", "接客户", "甲方客户", "客户接待", "商务接待", "公司用")):
        parts.append("工作室用车/偶尔接客户")
    if contains_any(normalized, ("器材", "灯架", "展架", "物料", "音响架", "折叠桌", "相机箱", "背景架", "后备厢", "后备箱", "拉东西", "装东西")):
        parts.append("后备厢和装载实用")
    if contains_any(normalized, ("跑高速", "高速", "父母", "爸妈", "老人", "后排舒服", "后排舒适", "长途")):
        parts.append("长途/后排舒适")
    if contains_any(normalized, VALUE_RETENTION_TERMS):
        parts.append("保值/后期好卖")
    if contains_any(normalized, ("置换", "旧车")):
        parts.append("兼顾置换")
    if contains_any(normalized, ("suv", "mpv", "七座")) and not any("SUV" in part or "MPV" in part for part in parts):
        if "mpv" in normalized:
            parts.append("MPV方向")
        elif "suv" in normalized:
            parts.append("SUV方向")
    if contains_any(normalized, ("通勤", "代步", "接娃", "买菜", "家用")) and not any("家用" in part for part in parts):
        parts.append("日常家用代步")
    return "、".join(parts)


def build_next_info_prompt(query: str, *, recent_reply_texts: list[str] | None = None) -> str:
    normalized = normalize_text(query)
    needs_finance = not contains_any(normalized, ("贷款", "按揭", "全款", "首付", "月供", "置换", "旧车"))
    needs_visit = not contains_any(normalized, ("南京", "苏州", "到店", "看车", "试驾", "周末", "周六", "周日", "今天", "明天"))
    prompts: list[str] = []
    if needs_finance:
        prompts.append("如果有贷款或置换，也可以顺手告诉我")
    if needs_visit:
        prompts.append("方便的话再说下看车城市或大概到店时间")
    if not prompts:
        return "我按这个方向继续核现车和车况。"
    if needs_finance and needs_visit:
        reply, _ = choose_reply_variant(
            [
                "贷款/置换情况、看车城市或大概到店时间，您方便时一起补一下，我再把优先级排细一点。",
                "您把贷款/置换情况、看车城市或大概到店时间发我，我好把顺序排清楚。",
                "这两点方便的话补一下：贷款/置换、看车城市或到店时间，我再往下细筛。",
            ],
            key_text=f"{query}|next-info-both",
            recent_reply_texts=recent_reply_texts,
        )
        return reply
    if needs_finance:
        reply, _ = choose_reply_variant(
            [
                "如果有贷款或置换，也可以顺手告诉我，我再把优先级排细一点。",
                "贷款/置换这块您方便时补一下，我好按总成本继续筛。",
                "全款、贷款或置换情况您说一下，我再帮您把范围收窄。",
            ],
            key_text=f"{query}|next-info-finance",
            recent_reply_texts=recent_reply_texts,
        )
        return reply
    reply, _ = choose_reply_variant(
        [
            "方便的话再说下看车城市或大概到店时间，我再把优先级排细一点。",
            "看车城市或大概到店时间您后面补一下，我好核现车和排期。",
            "如果准备到店，您说下城市和大概时间，我再按现车情况往下排。",
        ],
        key_text=f"{query}|next-info-visit",
        recent_reply_texts=recent_reply_texts,
    )
    return reply


def build_recommendation_reply(
    query: str,
    products: list[dict[str, Any]],
    *,
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    if not products:
        return "", 0
    segments = []
    for product in products:
        name = str(product.get("name") or "").strip()
        price = product.get("price")
        searchable = product_search_text(product)
        spec = "" if is_used_car_product_search_text(searchable) else brief_spec(str(product.get("spec") or product.get("specs") or ""))
        if not name:
            continue
        price_text = f"{price:g}万" if isinstance(price, (int, float)) else (f"{price}万" if price else "")
        detail = "，".join(part for part in (price_text, spec) if part)
        segments.append(f"{name}（{detail}）" if detail else name)
    if not segments:
        return "", 0
    if len(segments) == 1:
        product_text = segments[0]
        candidate_ref = "这台"
    else:
        product_text = "和".join(segments[:2])
        candidate_ref = "这两台"
    summary = build_need_summary(query)
    if summary:
        variants = [
            f"按{summary}，先看{product_text}。优先核车况和公里数。",
            f"{summary}这个范围里，先排{product_text}。以检测报告和实车为准。",
            f"先缩到{product_text}，更贴近{summary}。先把车况确认清楚。",
            f"按{summary}，先看{product_text}。后面重点比车况和后期成本。",
        ]
    else:
        variants = [
            f"我会先看{product_text}。{candidate_ref}先按车况和公里数筛。",
            f"先从{product_text}里挑。先看检测报告，再看实车状态。",
            f"{product_text}先放前面。到店先核车况、公里数和价格。",
            f"先给您缩到{product_text}。车况没问题再谈下一步。",
        ]
    return choose_natural_reply_variant(variants, key_text=query + product_text, recent_reply_texts=recent_reply_texts)


def catalog_products_by_public_price(*, limit: int = 3) -> list[dict[str, Any]]:
    products = []
    for item in load_catalog_product_candidates():
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        copied = dict(item)
        copied["_public_price_value"] = price
        products.append(copied)
    products.sort(key=lambda item: float(item.get("_public_price_value") or 0), reverse=True)
    return products[: max(0, int(limit or 0))]


def build_catalog_price_superlative_reply(
    query: str,
    products: list[dict[str, Any]],
    *,
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    if not products:
        return "", 0
    top = products[0]
    product_text = product_price_label(top)
    spec = brief_spec(str(top.get("spec") or top.get("specs") or ""))
    spec_text = f"，{spec}" if spec else ""
    variants = [
        f"目前公开标价最高的是{product_text}{spec_text}。价格是商品库标价，最终成交空间要结合车况、付款方式和到店沟通。",
        f"按现在商品库看，最高标价是{product_text}{spec_text}。我先按公开标价给您说，成交价还要看车况和具体谈法。",
        f"现在库里标价最高的是{product_text}{spec_text}。这个属于公开标价，后面如果您真看这台，再核车况和成交空间。",
    ]
    return choose_natural_reply_variant(variants, key_text=query + product_text, recent_reply_texts=recent_reply_texts)


def build_catalog_price_list_reply(
    query: str,
    products: list[dict[str, Any]],
    *,
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int]:
    if not products:
        return "", 0
    labels = [product_price_label(item) for item in products[:3] if product_price_label(item)]
    if not labels:
        return "", 0
    joined = "、".join(labels)
    variants = [
        f"先按公开标价给您几台参考：{joined}。最终成交空间要结合车况、付款方式和到店沟通。",
        f"可以，当前公开标价先看这几台：{joined}。标价能先给您，最终价格还要按实车和付款方式谈。",
        f"我先给您发公开标价：{joined}。这不是最终成交价，后面看车况和付款方式再谈空间。",
    ]
    return choose_natural_reply_variant(variants, key_text=query + joined, recent_reply_texts=recent_reply_texts)


def build_direct_catalog_price_reply(
    query: str,
    evidence_pack: dict[str, Any],
    *,
    recent_reply_texts: list[str] | None = None,
) -> tuple[str, int, list[str]]:
    products = rank_product_candidates(
        query,
        evidence_pack,
        allow_catalog_fallback=True,
        allow_broad_fallback=False,
    )
    explicit_products = [item for item in products if explicit_product_aliases_for_query(item, query)]
    if not explicit_products:
        return "", 0, []
    primary = explicit_products[0]
    label = product_price_label(primary)
    if not label:
        return "", 0, []

    product_id = str(primary.get("id") or "")
    used_product_ids = [product_id] if product_id else []
    try:
        price = float(primary.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    budget = extract_budget_wan(query)
    budget_hint = ""
    if budget > 0 and price > 0:
        if price > budget * 1.08:
            budget_hint = f"按{budget:g}万预算看，这台会超一些。"
        elif price <= budget:
            budget_hint = f"按{budget:g}万预算看，这台在预算内。"

    alternatives = [
        item
        for item in rank_product_candidates(
            query,
            evidence_pack,
            allow_catalog_fallback=True,
            allow_broad_fallback=True,
        )
        if str(item.get("id") or "") != product_id
        and (not budget or 0 < float(item.get("price") or 0) <= max(budget * 1.05, budget + 0.5))
    ][:1]
    alternative_text = ""
    if alternatives and budget > 0:
        alternative_label = product_price_label(alternatives[0])
        if alternative_label:
            alternative_text = f"更贴预算的可以顺手看{alternative_label}。"
            alt_id = str(alternatives[0].get("id") or "")
            if alt_id:
                used_product_ids.append(alt_id)

    variants = [
        f"{label}。{budget_hint}{alternative_text}我先按商品库公开标价说，成交价还要看车况和付款方式。",
        f"这台是{label}。{budget_hint}{alternative_text}公开标价能先给您，最终成交空间要到店结合车况谈。",
        f"{label}，这是当前商品库公开标价。{budget_hint}{alternative_text}后面重点看检测报告和实车。",
    ]
    reply, variant_index = choose_natural_reply_variant(
        variants,
        key_text=query + label + alternative_text,
        recent_reply_texts=recent_reply_texts,
    )
    return reply, variant_index, used_product_ids


def product_price_label(product: dict[str, Any]) -> str:
    name = str(product.get("name") or "").strip()
    if not name:
        return ""
    try:
        price = float(product.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0:
        return f"{name}（{price:g}万）"
    return name


def explicit_product_aliases_for_query(product: dict[str, Any], query: str) -> list[str]:
    aliases = [
        str(product.get("name") or ""),
        str(product.get("sku") or ""),
        *[str(alias) for alias in product.get("aliases", []) or []],
    ]
    matched = collect_matched_aliases(aliases, normalize_text(query))
    if matched:
        return matched
    product_name_key = compact_match_text(product.get("name") or "")
    query_key = compact_match_text(query)
    for term in ("多少钱", "什么价", "报价", "价格", "价位", "落地价", "总价", "现在", "目前", "这台", "那台"):
        query_key = query_key.replace(compact_match_text(term), "")
    if len(query_key) >= 2 and query_key in product_name_key:
        return [query_key]
    folded_query = compact_match_text(fold_confusable_text(query_key))
    folded_name = compact_match_text(fold_confusable_text(product_name_key))
    if len(folded_query) >= 2 and folded_query in folded_name:
        return [query_key]
    return []


def brief_spec(spec: str) -> str:
    clean = " ".join(str(spec or "").split())
    if not clean:
        return ""
    parts = re.split(r"[，,。；;]", clean)
    useful = [part.strip() for part in parts if part.strip()]
    return "，".join(useful[:3])[:80]
