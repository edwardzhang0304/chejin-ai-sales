"""Contracts for the Brain First customer-service reply path.

The brain contract is intentionally small and JSON-friendly. It lets the
runtime audit what the LLM thought the customer asked, which authority sources
were used, and which short WeChat reply segments should be polished/sent.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


SCHEMA_VERSION = 1

NONSEMANTIC_TEST_MARKER_RE = re.compile(
    r"\s*[\(（](?:"
    r"BRAIN_BOUNDARY|LIVEFLOW|FRESHLONG|REALWX|WXTEST|TESTWX|LIVE_REGRESSION|DEBUGFLOW|SELFQA"
    r")[A-Za-z0-9_:\-]{0,120}[\)）]"
    r"|\s*[\[【](?:live-regression|test-run|BRAIN_BOUNDARY|LIVEFLOW|FRESHLONG|REALWX|DEBUGFLOW|SELFQA)"
    r"[A-Za-z0-9_:\-]{0,120}[\]】]",
    re.I,
)

ANSWER_MODES = {
    "direct_answer",
    "ask_clarifying_question",
    "soft_social_reply",
    "soft_redirect_to_business",
    "recommend_from_catalog",
    "compare_options",
    "quote_product_fact",
    "collect_customer_info",
    "handoff",
    "fallback_existing",
}

GUARD_ACTIONS = {"send_reply", "handoff", "handoff_for_approval", "fallback_existing"}

PRODUCT_FACT_TYPES = {
    "product",
    "product_name",
    "product_name_price",
    "product_price",
    "product_alias",
    "price",
    "product_name_price",
    "product_price",
    "stock",
    "inventory",
    "availability",
    "year",
    "mileage",
    "condition",
    "location",
    "configuration",
}

PRODUCT_MASTER_ONLY_FACT_TYPES = {
    "price",
    "stock",
    "inventory",
    "availability",
    "year",
    "mileage",
    "condition",
    "location",
    "configuration",
}

POLICY_FACT_TYPES = {
    "policy",
    "finance",
    "loan",
    "trade_in",
    "after_sales",
    "transfer",
    "contract",
    "invoice",
    "appointment",
    "handoff_rule",
}

PRODUCT_AUTHORITY_LEVELS = {"product_master", "product_scoped_formal", "current_conversation_fact"}
POLICY_AUTHORITY_LEVELS = {"formal_knowledge", "product_scoped_formal", "current_conversation_fact"}
STYLE_ONLY_LEVELS = {"style_memory", "ai_experience_pool", "rag_experience", "llm_common_sense"}
FACT_CLAIM_REQUIRED_MODES = {"recommend_from_catalog", "compare_options", "quote_product_fact"}
NON_AUTHORITATIVE_ANALYSIS_FACT_TYPES = {
    "analysis",
    "reasoning",
    "recommendation_basis",
    "common_sense",
    "common_sense_analysis",
    "advice",
    "suggestion",
    "boundary",
    "risk_boundary",
    "safe_boundary",
    "caveat",
    "style",
}
FACT_TYPE_ALIASES = {
    "car": "product",
    "vehicle": "product",
    "vehicle_name": "product_name",
    "car_name": "product_name",
    "product_title": "product_name",
    "product_price": "price",
    "vehicle_price": "price",
    "car_price": "price",
    "quote": "price",
    "quoted_price": "price",
    "product_quote": "price",
    "product_stock": "stock",
    "product_inventory": "inventory",
    "product_availability": "availability",
    "product_year": "year",
    "product_mileage": "mileage",
    "product_condition": "condition",
    "product_location": "location",
    "product_specs": "configuration",
    "product_spec": "configuration",
    "specs": "configuration",
}
EVIDENCE_USED_ALIASES = {
    "product_ids": ("product_ids", "product_id", "product_master", "product_master_ids", "products"),
    "formal_knowledge_ids": (
        "formal_knowledge_ids",
        "formal_knowledge_id",
        "formal_knowledge",
        "policy_ids",
        "policies",
        "formal_ids",
    ),
    "conversation_fact_ids": (
        "conversation_fact_ids",
        "conversation_fact_id",
        "current_conversation_fact",
        "current_conversation_facts",
        "conversation_facts",
    ),
    "common_sense_topics": ("common_sense_topics", "common_sense", "llm_common_sense"),
    "style_ids": ("style_ids", "style_id", "style_context", "style_memory", "style_examples"),
    "rag_ids": ("rag_ids", "rag_id", "rag", "ai_experience_pool", "rag_experience"),
}
SOURCE_LEVEL_ALIASES = {
    "product": "product_master",
    "products": "product_master",
    "product_master_ids": "product_master",
    "policy": "formal_knowledge",
    "policies": "formal_knowledge",
    "formal": "formal_knowledge",
    "formal_knowledge_ids": "formal_knowledge",
    "conversation": "current_conversation_fact",
    "conversation_fact": "current_conversation_fact",
    "current_conversation": "current_conversation_fact",
    "common_sense": "llm_common_sense",
    "common-sense": "llm_common_sense",
    "style": "style_memory",
    "style_context": "style_memory",
    "rag": "rag_experience",
    "ai_pool": "ai_experience_pool",
}
FACT_HINT_TERMS = (
    "万",
    "库存",
    "现车",
    "表显",
    "公里",
    "车况",
    "检测报告",
    "原版原漆",
    "一手车",
    "贷款",
    "分期",
    "过户",
    "发票",
    "合同",
    "售后",
)
AUTHORITY_FACT_HINT_TERMS = (
    "库存",
    "现车",
    "原版原漆",
    "一手车",
    "贷款",
    "分期",
    "过户",
    "发票",
    "合同",
    "售后",
)

PRICE_QUESTION_TERMS = ("多少钱", "价格", "报价", "怎么卖", "几万", "多少米", "落地", "费用")
RECOMMENDATION_QUESTION_TERMS = ("推荐", "建议", "怎么选", "选哪", "哪款", "哪台", "哪个", "更适合", "优先", "挑一")
COMPARISON_QUESTION_TERMS = ("对比", "区别", "哪个好", "哪一个好", "比起来", "相比")
DIRECT_DECISION_REQUEST_TERMS = (
    "直接帮我挑",
    "直接给我挑",
    "直接帮我选",
    "直接给我选",
    "直接挑",
    "直接选",
    "直接推荐",
    "帮我挑最",
    "帮我选最",
    "你来挑",
    "你来选",
    "你帮我定",
    "你来定",
    "你决定",
    "你说哪台",
    "你说哪个",
    "你觉得哪台",
    "别让我选太多",
    "不要让我选",
    "别问太多",
    "不要问太多",
    "不用问太多",
    "少问点",
)
DIRECT_DECISION_BROAD_NEED_QUESTION_TERMS = (
    "预算多少",
    "预算大概",
    "预算上限",
    "预算范围",
    "您预算",
    "你预算",
    "日常代步还是家用",
    "代步还是家用",
    "主要用途",
    "什么用途",
    "有没有偏好",
    "偏好的牌子",
    "喜欢什么牌子",
    "轿车还是SUV",
    "轿车还是suv",
    "SUV还是轿车",
    "suv还是轿车",
    "想看轿车",
    "想看SUV",
    "想看suv",
)
AMBIGUOUS_PRODUCT_FOLLOWUP_TERMS = (
    "这台",
    "这辆",
    "这个车",
    "它",
    "车况",
    "检测",
    "事故",
    "水泡",
    "火烧",
    "补漆",
    "换件",
    "油耗",
    "保养",
    "后期",
    "维护",
    "空间",
    "后备厢",
    "第二排",
    "试驾",
    "看车",
    "现车",
)
GREETING_TERMS = ("你好", "您好", "在吗", "在不在", "早", "早上好", "下午好", "晚上好", "哈喽", "hello", "hi")
THANKS_TERMS = ("谢谢", "谢了", "感谢", "多谢", "辛苦")
FAREWELL_TERMS = ("再见", "拜拜", "回头聊", "下次聊")
GOODBYE_TERMS = FAREWELL_TERMS + THANKS_TERMS
SOCIAL_SUMMON_TERMS = ("人呢", "在不", "在么", "在嘛", "有人吗", "老板在吗", "还在吗", "忙吗")
SOCIAL_ONLY_BUSINESS_INTENT_TERMS = (
    "车",
    "预算",
    "报价",
    "价格",
    "多少钱",
    "贷款",
    "分期",
    "置换",
    "过户",
    "保险",
    "赔",
    "合同",
    "发票",
    "到店",
    "看车",
    "试驾",
    "推荐",
    "车型",
    "电话",
    "手机号",
    "联系方式",
    "库存",
)
BOUNDARY_OR_INTERNAL_PROBE_TERMS = (
    "系统提示词",
    "提示词",
    "内部规则",
    "开发者消息",
    "源码",
    "密钥",
    "api key",
    "apikey",
    "token",
    "是不是ai",
    "是不是AI",
    "机器人",
    "自动回复",
)
INTERNAL_SECRET_PROBE_TERMS = (
    "系统提示词",
    "提示词",
    "内部规则",
    "开发者消息",
    "源码",
    "密钥",
    "api key",
    "apikey",
    "token",
)
GENERIC_STALL_TERMS = (
    "我先看",
    "我看一下",
    "稍等",
    "等我看",
    "帮您看看",
    "我确认一下",
    "我先确认",
    "先确认一下",
    "马上回复",
    "我查一下",
)
CLEAR_BOUNDARY_REFUSAL_TERMS = (
    "不能帮",
    "没法帮",
    "无法帮",
    "不能做",
    "不能操作",
    "不能处理",
    "不能配合",
    "不能这么",
    "不可以这么",
    "不建议这么",
    "不能调",
    "不能改",
    "不能篡改",
    "不能承诺",
    "不能保证",
    "不能包过",
)
CLEAR_BOUNDARY_SUBSTANTIVE_TERMS = (
    "真实",
    "如实",
    "正常流程",
    "合规",
    "交易真实性",
    "违法",
    "违规",
    "诚信",
    "风险",
    "资方",
    "审批",
    "征信",
    "保险公司",
    "保单",
    "审核",
    "检测",
    "车况",
    "评估",
    "实际",
    "正式",
)
UNSUPPORTED_INFO_COLLECTION_TERMS = ("电话", "手机号", "联系方式", "预算", "轿车还是", "SUV", "suv", "资料", "补齐")
BUSINESS_REDIRECT_TERMS = (
    "预算",
    "车型",
    "车源",
    "看车",
    "试驾",
    "报价",
    "价格",
    "贷款",
    "置换",
    "过户",
    "车况",
    "公里",
    "库存",
    "到店",
    "安排",
    "预约",
)
SOCIAL_COMPANION_BUSINESS_PULLBACK_TERMS = (
    "充电",
    "充电桩",
    "绿牌",
    "纯电",
    "混动",
    "油耗",
    "保养",
    "检测报告",
    "门店",
    "到店",
    "看车",
    "试驾",
    "车源",
    "报价",
    "价格",
    "预算",
    "贷款",
    "置换",
    "过户",
)
SOCIAL_CLOSING_STALE_CONTEXT_TERMS = (
    "预算",
    "车型",
    "车源",
    "看车",
    "试驾",
    "报价",
    "价格",
    "贷款",
    "置换",
    "过户",
    "车况",
    "公里",
    "库存",
    "到店",
    "安排",
    "预约",
    "电话",
    "手机号",
    "联系方式",
    "推荐",
    "候选",
    "轿车",
    "SUV",
    "suv",
    "MPV",
    "mpv",
)
SOCIAL_TURN_STALE_BUSINESS_CONTEXT_TERMS = tuple(
    dict.fromkeys(
        SOCIAL_CLOSING_STALE_CONTEXT_TERMS
        + BUSINESS_REDIRECT_TERMS
        + SOCIAL_COMPANION_BUSINESS_PULLBACK_TERMS
        + UNSUPPORTED_INFO_COLLECTION_TERMS
        + (
            "资料",
            "配置",
            "产品",
            "商品",
            "型号",
            "规格",
            "清单",
            "方案",
            "门店",
            "销售",
        )
    )
)
UNNECESSARY_HANDOFF_VISIBLE_TERMS = ("转人工", "人工客服", "人工帮", "负责人", "专员")
INTERNAL_VISIBLE_MARKER_TERMS = (
    "【内部处理】",
    "[内部处理]",
    "内部处理",
    "不能作为自动回复",
    "不能直接发给客户",
    "客户可见回复",
    "系统内部",
)
HIGH_RISK_COMMITMENT_ECHO_TERMS = (
    "保证贷款包过",
    "贷款包过",
    "保证包过",
    "包过",
    "一定能批",
    "肯定能批",
    "保证最低价",
    "绝对最低价",
    "绝对最低",
    "最低价锁",
    "保证赔",
    "肯定赔",
    "一定赔",
    "保证无事故",
    "绝对无事故",
    "保证没事故",
    "百分百无事故",
    "100%无事故",
    "保证没水泡",
    "保证不是水泡",
    "保证能赔",
)
OVERSELL_OR_UNAUTHORIZED_ACTION_TERMS = (
    "闭眼入",
    "不用再看",
    "不用纠结",
    "完全不用纠结",
    "挑不出毛病",
    "放心冲",
    "直接冲",
    "直接定",
    "今天能定",
    "今天就定",
    "现在就定",
    "马上定",
)
OVERSELL_OR_UNAUTHORIZED_ACTION_PATTERNS = (
    ("direct_close_pressure", r"(直接|马上|现在|今天).{0,6}(给[你您]?|帮[你您]?)?(定下|订下|下定|锁定|拿下|冲|下手|定车|订车)"),
    ("direct_reserve_this_vehicle", r"(直接|马上|现在|今天).{0,6}(给[你您]?|帮[你您]?)?(定|订)(这台|下来|了|吧)"),
    ("vehicle_condition_overclaim", r"(车况|其他|整车|发动机|变速箱|底盘).{0,8}(没问题|无问题|不用担心|已经把关)"),
    ("seller_condition_vouch", r"(车况)?(已经|都)?把关过"),
    ("no_need_hesitation", r"(不用|不必|无需).{0,4}纠结"),
)
UNAUTHORIZED_RESERVATION_ACTION_TERMS = (
    "帮你留车",
    "帮您留车",
    "先帮你留",
    "先帮您留",
    "给你留车",
    "给您留车",
    "帮你锁车",
    "帮您锁车",
    "给你锁车",
    "给您锁车",
    "锁住这台",
    "锁定这台",
)
INCOMPLETE_CONDITION_OPENERS = ("如果", "要是", "假如", "若", "但如果")
INCOMPLETE_TRAILING_TERMS = (
    "如果",
    "要是",
    "假如",
    "因为",
    "所以",
    "但是",
    "不过",
    "另外",
    "比如",
    "包括",
    "或者",
    "以及",
    "然后",
    "的话",
)
INSURANCE_TOPIC_TERMS = ("保险", "车损险", "理赔", "赔不赔", "赔吗", "报案", "定损", "保单", "剐蹭", "撞墙")
INSURANCE_REPLY_TERMS = (
    "保险",
    "车损险",
    "商业险",
    "交强险",
    "理赔",
    "报案",
    "定损",
    "保单",
    "保险公司",
    "出险",
    "保费",
    "自费",
    "维修金额",
    "不一定",
    "以保单",
    "审核",
)
CARGO_TOPIC_TERMS = ("后备厢", "后备箱", "装东西", "装载", "箱子", "灯架", "梯子", "工具箱", "第二排", "后排", "放倒", "大件", "空间", "塞得下", "塞下")
CARGO_REPLY_TERMS = ("后备厢", "后备箱", "装", "装载", "空间", "第二排", "后排", "放倒", "大件", "开口", "容积", "梯子", "工具箱", "尺寸", "实车", "比划", "塞得下", "塞下")
CARGO_FIT_CATEGORY_TERMS = ("suv", "mpv", "旅行", "两厢", "掀背", "皮卡", "van")
APPOINTMENT_TOPIC_TERMS = ("到店", "看车", "试驾", "预约", "排期", "安排", "过去", "来店", "周末", "周六", "周日", "上午", "下午", "几点")
APPOINTMENT_OVERCOMMIT_TERMS = (
    "过来就可以",
    "过来就行",
    "直接过来",
    "直接来",
    "来就可以",
    "来就行",
    "就可以过来",
    "就行过来",
)
APPOINTMENT_CONFIRMATION_BOUNDARY_TERMS = (
    "确认",
    "核实",
    "排期",
    "车源",
    "回复",
    "回您",
    "记下",
    "记上",
    "先记",
    "白跑",
)
TRADE_IN_TOPIC_TERMS = ("置换", "旧车", "收购", "估价", "评估", "验车", "卖车", "抵车款")
TRADE_IN_OVERCOMMIT_TERMS = (
    "上门验车",
    "上门检测",
    "当天打款",
    "当天可以打款",
    "当天能打款",
    "当天安排打款",
    "当天付款",
    "立刻打款",
    "马上打款",
)
TRADE_IN_FINAL_PRICE_TERMS = ("最终收购价", "最终收车价", "最终价格", "最终报价")
TRADE_IN_DIRECT_PRICE_REQUEST_TERMS = ("准价", "准数", "具体价", "具体数字", "确定价", "抵多少", "能抵多少", "给个价", "报个价")
TRADE_IN_BOUNDARY_TERMS = (
    "以门店",
    "以实际",
    "以验车",
    "现场核验",
    "现场核实",
    "现场验车",
    "核验结果",
    "核实结果",
    "实车检测",
    "验车核实",
    "核实后",
    "确认后",
    "需要确认",
    "需要核实",
    "先初估",
    "初步评估",
    "大概区间",
    "不能直接",
    "没法直接",
    "无法直接",
    "不能给个准价",
    "没法给个准价",
    "无法给个准价",
    "不先承诺",
    "手续",
)
NO_NONSEDAN_AVAILABLE_TERMS = (
    "现有车源里都还是轿车",
    "现有车源都是轿车",
    "现车里都还是轿车",
    "现车都是轿车",
    "只有轿车",
    "都是轿车",
    "没有SUV",
    "没有suv",
    "暂无SUV",
    "暂无suv",
)
DEFAULT_QUALITY_REPLY_MAX_CHARS = 120
DEFAULT_QUALITY_MIXED_TOPIC_REPLY_MAX_CHARS = 180
DEFAULT_QUALITY_SPLIT_REPLY_MAX_CHARS = 150
DIRECT_RECOMMENDATION_TERMS = (
    "建议",
    "推荐",
    "主推",
    "首推",
    "优先",
    "先看",
    "更适合",
    "更偏",
    "偏向",
    "更稳",
    "更保值",
    "更好卖",
    "亏得少",
    "亏得少一点",
    "一般还是",
    "可以看",
    "可以先",
    "不建议",
    "排除",
    "选",
    "挑",
    "第一顺位",
    "第二顺位",
    "放第二",
)
CLEAR_RECOMMENDATION_STRUCTURE_TERMS = (
    "第一",
    "第二",
    "第三",
    "第一台",
    "第二台",
    "第三台",
    "第一款",
    "第二款",
    "第三款",
    "备选",
    "方向",
    "排序",
    "顺位",
    "放前面",
    "放后面",
    "预算最轻松",
    "性价比",
    "更贴合",
    "最贴合",
    "更均衡",
)
UNCERTAIN_BUT_SAFE_TERMS = ("暂无", "还没", "需要核实", "以实际", "以门店", "我确认后")
KNOWN_PRICE_UNCERTAINTY_TERMS = (
    "能不能压进",
    "能不能进",
    "能不能到",
    "能不能控制在",
    "看那台实际挂牌价",
    "看实际挂牌价",
    "挂牌价能不能",
    "报价能不能",
    "价格能不能",
)
OVER_BUDGET_CAVEAT_TERMS = (
    "超预算",
    "超您预算",
    "明显超",
    "预算会高",
    "价格会高",
    "会高一些",
    "高一些",
    "不在预算",
    "预算外",
    "只能作为备选",
    "备选",
    "不作为等价推荐",
)
QUALITY_WEAK_PRODUCT_ENTITY_TERMS = {
    "suv",
    "mpv",
    "轿车",
    "中型suv",
    "燃油suv",
    "家用suv",
    "商务车",
    "自动挡",
    "新能源",
    "混动",
    "纯电",
    "两厢",
    "三厢",
    "现车",
    "南京",
    "省油",
    "家用",
    "通勤",
    "空间",
    "2.0t",
    "25l",
    "2.5l",
}
PRICE_VALUE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:万|万元|w|W)")
CONCRETE_MILEAGE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:万)?\s*公里")


def normalize_answer_mode(value: Any) -> str:
    mode = str(value or "").strip()
    return mode if mode in ANSWER_MODES else "direct_answer"


def normalize_guard_action(value: Any, *, needs_handoff: bool = False) -> str:
    action = str(value or "").strip()
    if action not in GUARD_ACTIONS:
        action = "handoff" if needs_handoff else "send_reply"
    return action


def normalize_reply_segments(value: Any, *, max_segments: int = 3) -> list[str]:
    """Return clean, complete reply segments.

    The model is expected to output 1-3 short WeChat messages. If it returns a
    single long string, split it conservatively on Chinese sentence boundaries.
    """

    segments: list[str] = []
    if isinstance(value, list):
        raw_segments = [str(item or "") for item in value]
    else:
        raw_segments = split_reply_like_text(str(value or ""))
    for raw in raw_segments:
        text = normalize_space(raw)
        if not text:
            continue
        text = remove_trailing_ellipsis(text)
        text = sanitize_forbidden_commitment_echo(text)
        if text and text not in segments:
            segments.append(text)
        if len(segments) >= max(1, int(max_segments or 3)):
            break
    return segments


def split_reply_like_text(text: str) -> list[str]:
    clean = normalize_space(text)
    if not clean:
        return []
    parts = re.split(r"(?<=[。！？!?])\s*", clean)
    return [part for part in parts if part]


def sanitize_forbidden_commitment_echo(text: str) -> str:
    clean = str(text or "")
    replacements = (
        (r"不能(?:直接)?说(?:一定|肯定|保证)赔(?:或(?:一定|肯定|保证)?不赔)?", "不能直接下结论"),
        (r"不(?:能|敢)?(?:直接)?(?:保证|肯定|承诺)(?:会)?赔", "不能直接下结论"),
        (r"没法(?:直接)?(?:保证|肯定|承诺)(?:会)?赔", "不能直接下结论"),
        (r"无法(?:直接)?(?:保证|肯定|承诺)(?:会)?赔", "不能直接下结论"),
    )
    for pattern, replacement in replacements:
        clean = re.sub(pattern, replacement, clean)
    return normalize_space(clean)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_nonsemantic_runtime_markers(text: str) -> str:
    """Remove runtime/test trace markers before semantic classification.

    These suffixes are metadata, similar to OCR speaker labels. They should
    never make a greeting or goodbye look like a business turn.
    """

    previous = str(text or "")
    while True:
        cleaned = NONSEMANTIC_TEST_MARKER_RE.sub("", previous).strip()
        if cleaned == previous:
            return cleaned
        previous = cleaned


def remove_trailing_ellipsis(text: str) -> str:
    clean = str(text or "").strip()
    clean = re.sub(r"[…\.]{2,}$", "", clean).strip()
    return clean


def join_reply_segments(segments: list[str]) -> str:
    return "\n".join(segment.strip() for segment in segments if str(segment or "").strip())


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_fact_claims(value: Any) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return facts
    for item in value:
        if not isinstance(item, dict):
            continue
        fact_type = str(item.get("fact_type") or item.get("type") or item.get("category") or "").strip()
        fact_type_key = re.sub(r"[\s\-]+", "_", fact_type.lower())
        fact_type = FACT_TYPE_ALIASES.get(fact_type_key, fact_type_key or fact_type)
        source_level = str(item.get("source_level") or item.get("source") or item.get("authority") or "").strip()
        source_level_key = re.sub(r"[\s\-]+", "_", source_level.lower())
        source_level = SOURCE_LEVEL_ALIASES.get(source_level_key, source_level_key or source_level)
        source_id = str(
            item.get("source_id")
            or item.get("id")
            or item.get("product_id")
            or item.get("policy_id")
            or item.get("knowledge_id")
            or ""
        ).strip()
        value_text = str(item.get("value") or item.get("claim") or item.get("text") or "").strip()
        if source_level in STYLE_ONLY_LEVELS and style_only_fact_claim_is_non_authoritative_note(
            fact_type=fact_type,
            value_text=value_text,
        ):
            continue
        if is_uncertainty_boundary_fact_claim(
            fact_type=fact_type,
            value_text=value_text,
            source_level=source_level,
        ):
            continue
        fact = {
            "fact_type": fact_type,
            "value": value_text,
            "source_level": source_level,
            "source_id": source_id,
        }
        if fact["fact_type"] or fact["value"]:
            facts.append(fact)
    return facts


def style_only_fact_claim_is_non_authoritative_note(*, fact_type: str, value_text: str) -> bool:
    """Drop Brain notes that were put in facts_claimed but cannot authorize facts.

    Some providers occasionally place Chinese labels such as "门店接待" or
    "置换评估流程" in facts_claimed even though the content is only style/common
    sense.  We should not let those auxiliary notes authorize product/policy
    facts, but they also should not block a Brain reply.  If the note looks like
    a real product/policy fact, keep it so authority validation can reject it.
    """

    normalized_type = str(fact_type or "").strip()
    normalized_value = str(value_text or "").strip()
    if normalized_type in NON_AUTHORITATIVE_ANALYSIS_FACT_TYPES:
        return True
    if normalized_type in PRODUCT_FACT_TYPES or normalized_type in POLICY_FACT_TYPES:
        return False
    combined = f"{normalized_type} {normalized_value}"
    if style_only_fact_claim_is_safe_boundary_note(combined):
        return True
    if reply_has_authority_fact_hint(combined):
        return False
    hard_fact_terms = (
        "价格",
        "报价",
        "售价",
        "库存",
        "现车",
        "车况",
        "公里",
        "里程",
        "事故",
        "水泡",
        "火烧",
        "贷款",
        "分期",
        "首付",
        "月供",
        "合同",
        "发票",
        "过户",
        "售后",
        "质保",
        "保险",
        "审批",
        "征信",
    )
    if contains_any(combined, hard_fact_terms):
        return False
    return True


def style_only_fact_claim_is_safe_boundary_note(text: str) -> bool:
    """Return True for auxiliary caution/process notes, not factual authority."""

    clean = normalize_space(text)
    if not clean:
        return False
    if PRICE_VALUE_RE.search(clean) or CONCRETE_MILEAGE_RE.search(clean):
        return False
    unsafe_commitment_terms = (
        "包过",
        "保证",
        "一定",
        "肯定",
        "绝对",
        "最低",
        "无事故",
        "没事故",
        "不是水泡",
        "不是火烧",
        "原版原漆",
        "一手车",
        "当天打款",
        "马上打款",
        "能批",
        "能过",
        "可以过",
        "承诺",
    )
    if contains_any(clean, unsafe_commitment_terms):
        return False
    return contains_any(
        clean,
        (
            "需要",
            "需",
            "要看",
            "结合",
            "核实",
            "确认",
            "验车",
            "评估",
            "流程",
            "按",
            "以",
            "为准",
            "不能",
            "无法",
            "没法",
            "不好",
            "不建议",
            "先",
        ),
    )


def is_uncertainty_boundary_fact_claim(*, fact_type: str, value_text: str, source_level: str) -> bool:
    if fact_type not in PRODUCT_MASTER_ONLY_FACT_TYPES:
        return False
    if source_level != "current_conversation_fact":
        return False
    return contains_any(
        value_text,
        (
            *UNCERTAIN_BUT_SAFE_TERMS,
            "需核实",
            "需要确认",
            "暂未确认",
            "暂时没有",
            "没有看到",
            "不能确认",
            "不敢凭印象",
            "以检测报告",
            "按检测报告",
            "看检测报告",
        ),
    )


def normalize_brain_plan(raw_plan: dict[str, Any] | None, *, max_segments: int = 3) -> dict[str, Any]:
    plan = dict(raw_plan) if isinstance(raw_plan, dict) else {}
    risk = normalize_mapping(plan.get("risk"))
    needs_handoff = bool(risk.get("needs_handoff") or plan.get("needs_handoff"))
    answer_mode = normalize_answer_mode(plan.get("answer_mode"))
    if answer_mode == "handoff":
        needs_handoff = True
    segments = normalize_reply_segments(plan.get("reply_segments") or plan.get("reply"), max_segments=max_segments)
    action = normalize_guard_action(plan.get("recommended_action"), needs_handoff=needs_handoff)
    if answer_mode == "fallback_existing":
        action = "fallback_existing"
    raw_confidence = plan.get("confidence")
    try:
        if raw_confidence is None or str(raw_confidence).strip() == "":
            # Some providers omit optional confidence when the visible Brain
            # answer is otherwise usable. Treat that as missing metadata, not
            # as an explicit zero-confidence business decision.
            confidence = 0.78 if segments else 0.0
        else:
            confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.78 if segments else 0.0
    confidence = max(0.0, min(1.0, confidence))
    normalized = {
        "schema_version": int(plan.get("schema_version") or SCHEMA_VERSION),
        "can_answer": plan.get("can_answer", True) is not False,
        "understanding": normalize_mapping(plan.get("understanding")),
        "answer_mode": answer_mode,
        "reply_strategy": normalize_mapping(plan.get("reply_strategy")),
        "evidence_used": normalize_evidence_used(plan.get("evidence_used")),
        "facts_claimed": normalize_fact_claims(plan.get("facts_claimed")),
        "reply_segments": segments,
        "risk": {
            "risk_level": str(risk.get("risk_level") or "low").strip() or "low",
            "risk_tags": normalize_string_list(risk.get("risk_tags") or plan.get("risk_tags")),
            "needs_handoff": needs_handoff,
            "handoff_reason": str(risk.get("handoff_reason") or plan.get("handoff_reason") or "").strip(),
        },
        "recommended_action": action,
        "confidence": confidence,
        "reason": str(plan.get("reason") or "").strip(),
        "self_check": normalize_mapping(plan.get("self_check")),
    }
    return normalized


def normalize_evidence_used(value: Any) -> dict[str, list[str]]:
    payload = normalize_mapping(value)
    normalized: dict[str, list[str]] = {}
    for key, aliases in EVIDENCE_USED_ALIASES.items():
        values: list[str] = []
        for alias in aliases:
            for item in normalize_string_list(payload.get(alias)):
                if item not in values:
                    values.append(item)
        normalized[key] = values
    return normalized


def validate_brain_plan(plan: dict[str, Any], *, require_fact_claims: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    if not plan.get("reply_segments") and plan.get("recommended_action") == "send_reply":
        errors.append("missing_reply_segments")
    if plan.get("answer_mode") not in ANSWER_MODES:
        errors.append("invalid_answer_mode")
    if plan.get("recommended_action") not in GUARD_ACTIONS:
        errors.append("invalid_recommended_action")
    facts = plan.get("facts_claimed", []) or []
    if require_fact_claims and plan_requires_fact_claims(plan) and not facts:
        errors.append("missing_fact_claims")
    for fact in plan.get("facts_claimed", []) or []:
        fact_type = str(fact.get("fact_type") or "").strip()
        source_level = str(fact.get("source_level") or "").strip()
        source_id = str(fact.get("source_id") or "").strip()
        if fact_type in PRODUCT_MASTER_ONLY_FACT_TYPES and source_level != "product_master":
            errors.append(f"product_master_fact_without_product_master_authority:{fact_type}")
        if fact_type in PRODUCT_FACT_TYPES and source_level not in PRODUCT_AUTHORITY_LEVELS:
            errors.append(f"product_fact_without_product_authority:{fact_type}")
        if fact_type in POLICY_FACT_TYPES and source_level not in POLICY_AUTHORITY_LEVELS:
            errors.append(f"policy_fact_without_formal_authority:{fact_type}")
        if fact_type in PRODUCT_FACT_TYPES and source_level in PRODUCT_AUTHORITY_LEVELS and not source_id:
            errors.append(f"product_fact_missing_source_id:{fact_type}")
        if fact_type in POLICY_FACT_TYPES and source_level in {"formal_knowledge", "product_scoped_formal"} and not source_id:
            errors.append(f"policy_fact_missing_source_id:{fact_type}")
        if source_level in STYLE_ONLY_LEVELS:
            errors.append(f"style_only_source_used_as_fact:{fact_type or source_level}")
    return {"ok": not errors, "errors": errors}


def validate_social_visible_reply_contract(plan: dict[str, Any], *, current_message: str) -> dict[str, Any]:
    """Require Brain-authored visible acknowledgements for real social turns.

    This is a reply-ownership contract, not a local answer template.  It keeps
    guard/quality/scheduler layers from treating greetings, summons, thanks, or
    goodbyes as "no visible reply" while still requiring the Brain to author the
    actual customer-facing sentence.
    """

    if not social_message_requires_visible_brain_reply(current_message):
        return {"ok": True, "errors": [], "warnings": []}
    action = str(plan.get("recommended_action") or "send_reply").strip()
    answer_mode = str(plan.get("answer_mode") or "").strip()
    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    reply = join_reply_segments(plan.get("reply_segments", []) or [])
    errors: list[str] = []
    warnings: list[str] = []
    if action != "send_reply":
        errors.append("social_message_requires_send_reply")
    if not bool(plan.get("can_answer", True)):
        errors.append("social_message_requires_can_answer")
    if bool(risk.get("needs_handoff")):
        errors.append("social_message_must_not_handoff_without_hard_boundary")
    if not reply.strip():
        errors.append("social_message_requires_visible_brain_reply")
    if answer_mode and answer_mode not in {"soft_social_reply", "direct_answer", "soft_redirect_to_business"}:
        warnings.append("social_message_answer_mode_should_be_social_or_direct")
    if reply.strip() and visible_content_char_count(reply) > 80:
        warnings.append("social_message_reply_too_long")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def verify_brain_reply_quality(
    plan: dict[str, Any],
    *,
    current_message: str,
    evidence_pack: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check whether a BrainPlan directly answers the current customer turn.

    This is a generic quality contract, not a business-rule source. It may
    reject evasive or mismatched replies, but it never authorizes product facts.
    """

    cfg = settings if isinstance(settings, dict) else {}
    if cfg.get("quality_verifier_enabled", True) is False:
        return {"ok": True, "errors": [], "warnings": [], "repair_instruction": ""}
    action = str(plan.get("recommended_action") or "send_reply")
    if action != "send_reply":
        return {"ok": True, "errors": [], "warnings": [], "repair_instruction": ""}

    question = normalize_space(strip_nonsemantic_runtime_markers(current_message))
    reply = join_reply_segments(plan.get("reply_segments", []) or [])
    clean_reply = normalize_space(reply)
    errors: list[str] = []
    warnings: list[str] = []

    if not clean_reply:
        errors.append("empty_visible_reply")
    if exposes_ai_identity(clean_reply):
        errors.append("customer_visible_ai_identity_leak")
    if identity_probe_reply_discusses_identity_truth(question, clean_reply):
        errors.append("customer_visible_identity_truth_discussion")
    if contains_any(clean_reply, INTERNAL_VISIBLE_MARKER_TERMS) or leaks_internal_prompt_content(clean_reply):
        errors.append("customer_visible_internal_marker_leak")
    if re.search(r"[…\.]{2,}\s*$", reply):
        errors.append("trailing_ellipsis_or_truncation")
    risky_echo = check_high_risk_commitment_echo(clean_reply)
    if risky_echo.get("error"):
        errors.append(str(risky_echo["error"]))
    oversell_check = check_overconfident_sales_or_unauthorized_action(clean_reply)
    if oversell_check.get("error"):
        errors.append(str(oversell_check["error"]))

    price_question = contains_any(question, PRICE_QUESTION_TERMS)
    contextual_recommendation_question = is_contextual_recommendation_followup(question)
    direct_decision_question = is_direct_decision_request(question)
    recommendation_question = (
        contains_any(question, RECOMMENDATION_QUESTION_TERMS)
        or contains_any(question, COMPARISON_QUESTION_TERMS)
        or contextual_recommendation_question
        or direct_decision_question
        or is_broad_product_recommendation_request(question)
    )
    concrete_question = price_question or recommendation_question or bool(plan.get("facts_claimed"))
    product_terms = collect_quality_product_terms(plan, evidence_pack or {})
    has_product_evidence = bool(collect_authoritative_product_ids(evidence_pack or {}))
    budget_upper = extract_quality_budget_upper(question, evidence_pack or {})

    delay_followup_state = extract_conversation_interaction_state(evidence_pack or {})
    delay_followup_posture = str(delay_followup_state.get("suggested_reply_posture") or "") == "acknowledge_delay_then_continue"

    if is_social_only_message(question):
        if contains_any(clean_reply, UNSUPPORTED_INFO_COLLECTION_TERMS):
            if not delay_followup_posture or not check_delay_followup_context_continuity(question, clean_reply, evidence_pack or {}) == {}:
                errors.append("unsupported_info_collection_for_social_message")
        stale_context_check = check_social_turn_over_carries_stale_business_context(question, clean_reply)
        if stale_context_check.get("error"):
            errors.append(str(stale_context_check["error"]))
        unsupported_context_check = check_social_turn_revives_unsupported_business_context(
            question,
            clean_reply,
            evidence_pack or {},
        )
        if unsupported_context_check.get("error"):
            errors.append(str(unsupported_context_check["error"]))
        if len(clean_reply) > int(cfg.get("social_reply_soft_max_chars") or 80):
            warnings.append("social_reply_too_long")
    elif is_thin_social_or_common_sense_reply(question, clean_reply, plan):
        warnings.append("thin_social_or_common_sense_reply")

    redirect_check = check_over_eager_business_redirect_after_social_fatigue(question, clean_reply, evidence_pack or {})
    if redirect_check.get("error"):
        errors.append(str(redirect_check["error"]))
    delay_followup_check = check_delay_followup_context_continuity(question, clean_reply, evidence_pack or {})
    if delay_followup_check.get("error"):
        errors.append(str(delay_followup_check["error"]))

    if concrete_question and is_generic_stall_reply(clean_reply):
        errors.append("generic_stall_reply_for_concrete_question")
    if contains_any(clean_reply, UNNECESSARY_HANDOFF_VISIBLE_TERMS):
        errors.append("unnecessary_handoff_language_for_send_reply")

    if price_question:
        if has_product_evidence:
            if not PRICE_VALUE_RE.search(clean_reply) and not contains_any(clean_reply, UNCERTAIN_BUT_SAFE_TERMS):
                errors.append("missing_direct_price_response")
            if product_terms and not mentions_any_entity(clean_reply, product_terms):
                errors.append("missing_referenced_product_in_reply")
            if contains_any(clean_reply, ("预算", "轿车还是", "SUV", "suv")) and not PRICE_VALUE_RE.search(clean_reply):
                errors.append("asks_new_need_instead_of_answering_price")
        elif PRICE_VALUE_RE.search(clean_reply):
            errors.append("price_answer_without_product_evidence")

    if contextual_recommendation_question and is_generic_stall_reply(clean_reply):
        errors.append("generic_stall_reply_for_contextual_recommendation")

    if contextual_recommendation_question and has_product_evidence:
        if not mentions_any_entity(clean_reply, product_terms):
            errors.append("missing_context_product_recommendation")
        relative_context_check = check_relative_context_product_binding(clean_reply, evidence_pack or {})
        if relative_context_check.get("error"):
            errors.append(str(relative_context_check["error"]))

    if (
        direct_decision_question
        and has_product_evidence
        and reply_asks_broad_new_need_instead_of_decision(clean_reply)
    ):
        errors.append("direct_decision_request_asked_new_need_instead_of_choice")

    if recommendation_question and has_product_evidence:
        if not reply_has_clear_recommendation_or_choice(clean_reply, plan, evidence_pack or {}):
            errors.append("missing_clear_recommendation_or_choice")
        if budget_upper is not None and budget_upper > 0:
            budget_check = check_budget_fit_recommendation(
                clean_reply,
                evidence_pack or {},
                current_message=question,
                budget_upper=budget_upper,
            )
            if budget_check.get("error"):
                errors.append(str(budget_check["error"]))
            price_uncertainty_check = check_known_budget_fit_product_price_uncertainty(
                clean_reply,
                evidence_pack or {},
                budget_upper=budget_upper,
            )
            if price_uncertainty_check.get("error"):
                errors.append(str(price_uncertainty_check["error"]))
        multi_check = check_multi_product_recommendation_count(
            clean_reply,
            evidence_pack or {},
            current_message=question,
            budget_upper=budget_upper,
        )
        if multi_check.get("error"):
            errors.append(str(multi_check["error"]))

    if has_product_evidence and is_ambiguous_product_followup(question, evidence_pack or {}):
        ambiguous_followup_check = check_ambiguous_followup_product_binding(clean_reply, evidence_pack or {})
        if ambiguous_followup_check.get("error"):
            errors.append(str(ambiguous_followup_check["error"]))

    if contains_any(question, INSURANCE_TOPIC_TERMS) and not contains_any(clean_reply, INSURANCE_REPLY_TERMS):
        errors.append("missing_insurance_common_sense_topic")

    if contains_any(question, CARGO_TOPIC_TERMS) and not contains_any(clean_reply, CARGO_REPLY_TERMS):
        errors.append("missing_cargo_space_topic")
    if has_product_evidence and contains_any(question, CARGO_TOPIC_TERMS) and is_broad_product_recommendation_request(question):
        cargo_fit_check = check_available_cargo_fit_candidate_for_broad_request(
            clean_reply,
            evidence_pack or {},
            budget_upper=budget_upper,
        )
        if cargo_fit_check.get("error"):
            errors.append(str(cargo_fit_check["error"]))
    if has_product_evidence and contains_any(question, CARGO_TOPIC_TERMS):
        cargo_capacity_check = check_unverified_cargo_capacity_claim(clean_reply, evidence_pack or {})
        if cargo_capacity_check.get("error"):
            errors.append(str(cargo_capacity_check["error"]))

    appointment_check = check_appointment_confirmation_boundary(question, clean_reply)
    if appointment_check.get("error"):
        errors.append(str(appointment_check["error"]))
    trade_in_check = check_trade_in_process_boundary(question, clean_reply)
    if trade_in_check.get("error"):
        errors.append(str(trade_in_check["error"]))

    total_limit = int(cfg.get("quality_reply_max_chars") or DEFAULT_QUALITY_REPLY_MAX_CHARS)
    total_chars = visible_content_char_count(clean_reply)
    if is_mixed_topic_customer_message(question) and len(plan.get("reply_segments", []) or []) >= 2:
        total_limit = max(total_limit, int(cfg.get("quality_mixed_topic_reply_max_chars") or DEFAULT_QUALITY_MIXED_TOPIC_REPLY_MAX_CHARS))
    if total_limit > 0 and total_chars > total_limit:
        if split_reply_is_sendable(plan.get("reply_segments", []) or [], settings=cfg, total_chars=total_chars):
            warnings.append("split_reply_over_soft_total_limit")
        else:
            errors.append("reply_too_long")

    for segment in plan.get("reply_segments", []) or []:
        segment_text = normalize_space(str(segment))
        if len(segment_text) > int(cfg.get("quality_segment_soft_max_chars") or 96):
            warnings.append("reply_segment_too_long")
        if is_incomplete_reply_segment(segment_text):
            errors.append("incomplete_reply_segment")
            break

    instruction = build_quality_repair_instruction(errors=errors, warnings=warnings, current_message=question, reply=clean_reply)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "repair_instruction": instruction}


def plan_requires_fact_claims(plan: dict[str, Any]) -> bool:
    if plan.get("recommended_action") != "send_reply":
        return False
    if plan_is_common_sense_only_advice(plan):
        return False
    answer_mode = str(plan.get("answer_mode") or "")
    if answer_mode in {"recommend_from_catalog", "quote_product_fact"}:
        return True
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    if evidence.get("product_ids") or evidence.get("formal_knowledge_ids"):
        return True
    reply = join_reply_segments(plan.get("reply_segments", []) or [])
    if answer_mode == "compare_options":
        return reply_has_authority_fact_hint(reply)
    return reply_has_authority_fact_hint(reply)


def is_mixed_topic_customer_message(text: str) -> bool:
    question = normalize_space(text)
    if not contains_any(question, ("顺便", "另外", "还有", "同时", "也想", "再问", "再说")):
        return False
    topic_count = 0
    if contains_any(question, PRICE_QUESTION_TERMS + RECOMMENDATION_QUESTION_TERMS + COMPARISON_QUESTION_TERMS):
        topic_count += 1
    if contains_any(question, INSURANCE_TOPIC_TERMS):
        topic_count += 1
    if contains_any(question, ("贷款", "分期", "置换", "旧车", "首付", "月供")):
        topic_count += 1
    if contains_any(question, APPOINTMENT_TOPIC_TERMS):
        topic_count += 1
    if contains_any(question, CARGO_TOPIC_TERMS):
        topic_count += 1
    return topic_count >= 2


def reply_has_authority_fact_hint(reply: str) -> bool:
    text = str(reply or "")
    return bool(reply_has_authority_price_value(text) or CONCRETE_MILEAGE_RE.search(text)) or any(
        term in text for term in AUTHORITY_FACT_HINT_TERMS
    )


def reply_has_authority_price_value(text: str) -> bool:
    for match in PRICE_VALUE_RE.finditer(str(text or "")):
        if not price_value_looks_like_customer_budget(text, match.start(), match.end()):
            return True
    return False


def price_value_looks_like_customer_budget(text: str, start: int, end: int) -> bool:
    before = str(text or "")[max(0, start - 12) : start]
    after = str(text or "")[end : min(len(str(text or "")), end + 8)]
    context = before + after
    if contains_any(context, ("报价", "价格", "标价", "售价", "车价", "首付", "月供", "落地", "按这台", "这台", "这辆")):
        return False
    if contains_any(after, ("以内", "之内", "以下", "内", "左右", "上下")):
        return True
    if contains_any(before, ("预算", "价位", "控制在", "卡在")):
        return True
    return False


def plan_is_common_sense_only_advice(plan: dict[str, Any]) -> bool:
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    return bool(evidence.get("common_sense_topics")) and not (
        evidence.get("product_ids") or evidence.get("formal_knowledge_ids") or plan.get("facts_claimed")
    )


def is_incomplete_reply_segment(text: str) -> bool:
    clean = normalize_space(text).rstrip("。！？!?")
    if not clean:
        return False
    if clean.endswith(INCOMPLETE_TRAILING_TERMS):
        return True
    if is_dangling_advisory_fragment(clean):
        return True
    if is_dangling_condition_clause(clean):
        return True
    if is_dangling_decision_fragment(clean):
        return True
    if any(clean.startswith(prefix) for prefix in INCOMPLETE_CONDITION_OPENERS):
        # A condition-only clause such as "如果损失不大。" reads like a truncated reply.
        return not any(mark in clean for mark in ("，", ",", "；", ";", "就", "可以", "建议", "最好", "一般"))
    return False


def is_dangling_advisory_fragment(clean: str) -> bool:
    """Reject advisory clauses cut off after the cue word.

    A segment ending with "不能算同档推荐" is complete, while
    "具体以报告为准，建议" is visibly truncated.  Keep this as a
    clause-shape check so quality review does not become a content rule.
    """

    clauses = [item.strip() for item in re.split(r"[，,。！？!?；;]", str(clean or "")) if item.strip()]
    if not clauses:
        return False
    last_clause = clauses[-1]
    return last_clause in {"建议", "推荐", "优先", "最好", "更建议", "更推荐", "我建议", "我推荐"}


def is_dangling_condition_clause(clean: str) -> bool:
    clauses = [item.strip() for item in re.split(r"[。！？!?；;]", clean) if item.strip()]
    if not clauses:
        return False
    last_clause = clauses[-1]
    condition_terms = (*INCOMPLETE_CONDITION_OPENERS, "如发现", "若发现", "未提前告知")
    condition_indexes: list[int] = []
    for term in condition_terms:
        if not term:
            continue
        start = last_clause.find(term)
        while start >= 0:
            # Avoid treating words such as "主要是..." as an unfinished
            # conditional merely because they contain "要是".
            if not (term == "要是" and start > 0 and last_clause[start - 1] == "主"):
                condition_indexes.append(start)
                break
            start = last_clause.find(term, start + 1)
    if not condition_indexes:
        return False
    tail = last_clause[min(condition_indexes) :]
    if condition_tail_has_complete_consequent(tail):
        return False
    outcome_terms = (
        "就",
        "可以",
        "可",
        "会",
        "按",
        "办理",
        "处理",
        "退",
        "赔",
        "补",
        "联系",
        "核实",
        "解决",
        "走",
        "一般",
        "通常",
        "还是",
        "更稳",
        "合适",
        "挺合适",
        "适合",
        "更适合",
        "更建议",
        "更偏",
        "建议",
        "推荐",
        "优先",
    )
    return not contains_any(tail, outcome_terms)


def condition_tail_has_complete_consequent(tail: str) -> bool:
    """Detect colloquial complete condition-result sentences.

    The quality gate should reject fragments like "如果损失不大，", but it
    must not punish natural WeChat wording such as "你要是想热闹一点，我投火锅".
    This is an expression-shape check, not a business-rule check.
    """

    parts = [normalize_space(item) for item in re.split(r"[，,]", str(tail or ""), maxsplit=1)]
    if len(parts) < 2:
        return False
    consequent = parts[1].strip()
    if not consequent:
        return False
    if consequent.endswith(INCOMPLETE_TRAILING_TERMS):
        return False
    if condition_tail_has_colloquial_consequent(consequent):
        return True
    if visible_content_char_count(consequent) < 3:
        return False
    return True


def condition_tail_has_colloquial_consequent(consequent: str) -> bool:
    """Accept complete spoken WeChat consequents.

    This remains an expression-shape check. It prevents the quality gate from
    treating complete colloquial turns such as "想聊车散散心也成" as truncated
    merely because they do not contain formal markers like "就/可以/建议".
    """

    clean = normalize_space(consequent)
    if not clean:
        return False
    if contains_any(clean, ("也成", "也行", "都行", "就行", "可以", "挺好", "蛮好", "没问题", "我听着", "我在")):
        return True
    if visible_content_char_count(clean) >= 6 and contains_any(clean, ("成", "行", "好", "聊", "听", "看", "问", "说")):
        return True
    return False


def is_dangling_decision_fragment(clean: str) -> bool:
    clauses = [item.strip() for item in re.split(r"[。！？!?；;]", clean) if item.strip()]
    if not clauses:
        return False
    last_clause = clauses[-1]
    if not contains_any(last_clause, ("要不要", "该不该", "是否需要")):
        return False
    decision_terms = ("出险", "走保险", "报保险", "贷款", "分期", "置换", "订车", "看车")
    if not contains_any(last_clause, decision_terms):
        return False
    completion_terms = (
        "要看",
        "取决于",
        "建议",
        "可以",
        "先",
        "再",
        "一般",
        "通常",
        "按",
        "看金额",
        "看维修",
        "看情况",
        "再决定",
    )
    return not contains_any(last_clause, completion_terms)


def visible_content_char_count(text: str) -> int:
    count = 0
    for char in str(text or ""):
        if not char.strip():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("Z"):
            continue
        count += 1
    return count


def split_reply_is_sendable(segments: list[Any], *, settings: dict[str, Any], total_chars: int) -> bool:
    """Allow complete multi-bubble replies to exceed the single-reply soft cap.

    Length is an expression-shape constraint, not an authority decision. If the
    Brain already produced 2-3 complete short WeChat bubbles, the sender/polish
    layer can deliver them safely; the quality gate should not discard the whole
    answer and fall back to a useless "wait while I check" message.
    """

    clean_segments = [normalize_space(str(item or "")) for item in segments if normalize_space(str(item or ""))]
    if len(clean_segments) < 2:
        return False
    max_segments = max(2, int(settings.get("max_reply_segments") or 3))
    if len(clean_segments) > max_segments:
        return False
    split_limit = int(settings.get("quality_split_reply_max_chars") or DEFAULT_QUALITY_SPLIT_REPLY_MAX_CHARS)
    if split_limit > 0 and total_chars > split_limit:
        return False
    segment_limit = int(settings.get("quality_segment_soft_max_chars") or 96)
    for segment in clean_segments:
        if visible_content_char_count(segment) > segment_limit:
            return False
        if is_incomplete_reply_segment(segment):
            return False
    return True


def contains_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    clean = str(text or "")
    lower = clean.lower()
    return any(str(term or "").lower() in lower for term in terms if str(term or ""))


def leaks_internal_prompt_content(text: str) -> bool:
    """Return True for internal implementation leaks, not safe refusal wording.

    Customer-facing replies may say that prompts/internal rules cannot be sent.
    They must not expose actual prompt text, schemas, hidden field names, or
    runtime implementation details.
    """

    clean = str(text or "")
    if not clean:
        return False
    leak_markers = (
        "developer message",
        "system prompt",
        "BrainPlan",
        "reply_segments",
        "customer_service_brain",
        "conversation_strategy_state",
        "conversation_interaction_state",
        "runtime_principles",
        "schema_version",
        "recommended_action",
        "facts_claimed",
        "source_level",
    )
    return contains_any(clean, leak_markers)


def is_social_only_message(text: str) -> bool:
    clean_text = strip_nonsemantic_runtime_markers(text).strip()
    clean = re.sub(r"[\s。！？!?，,、~～\.\-_:：；;]+", "", clean_text).lower()
    if not clean:
        return False
    terms = tuple(item.lower() for item in GREETING_TERMS + GOODBYE_TERMS + SOCIAL_SUMMON_TERMS)
    if clean in terms or (len(clean) <= 7 and any(term in clean for term in terms)):
        return True
    return is_extended_low_risk_social_message(clean_text)


def is_extended_low_risk_social_message(text: str) -> bool:
    clean = normalize_space(strip_nonsemantic_runtime_markers(text))
    if not clean:
        return False
    if visible_content_char_count(clean) > 28:
        return False
    if contains_any(clean, SOCIAL_ONLY_BUSINESS_INTENT_TERMS):
        return False
    if contains_any(clean, BOUNDARY_OR_INTERNAL_PROBE_TERMS):
        return False
    social_terms = GOODBYE_TERMS + GREETING_TERMS + SOCIAL_SUMMON_TERMS + (
        "先这样",
        "改天",
        "下次",
        "回头",
        "有空",
        "晚点",
        "辛苦了",
        "麻烦了",
    )
    return contains_any(clean, social_terms)


def check_high_risk_commitment_echo(reply: str) -> dict[str, Any]:
    """Flag risky guarantee phrases even when Brain repeats them negatively.

    This is a repair signal. Brain must paraphrase boundaries without echoing
    the exact customer-risk wording.
    """

    compact = re.sub(r"\s+", "", str(reply or ""))
    if not compact:
        return {}
    for term in HIGH_RISK_COMMITMENT_ECHO_TERMS:
        if term and re.sub(r"\s+", "", term) in compact:
            return {"error": f"high_risk_commitment_phrase_echo:{term}"}
    return {}


def check_overconfident_sales_or_unauthorized_action(reply: str) -> dict[str, Any]:
    """Flag pressure-selling or reservation claims as Brain repair signals.

    Brain may recommend confidently from authorized product evidence, but it
    must not pressure the customer, imply no alternatives need consideration,
    or promise a hold/lock/reservation unless the current turn/formal knowledge
    explicitly authorizes that action. This reviewer never writes replacement
    wording; it only asks Brain to soften the posture.
    """

    compact = re.sub(r"\s+", "", str(reply or ""))
    if not compact:
        return {}
    for term in UNAUTHORIZED_RESERVATION_ACTION_TERMS:
        if term and re.sub(r"\s+", "", term) in compact:
            return {"error": f"unauthorized_reservation_or_lock_claim:{term}"}
    for term in OVERSELL_OR_UNAUTHORIZED_ACTION_TERMS:
        if term and re.sub(r"\s+", "", term) in compact:
            return {"error": f"overconfident_sales_pressure_phrase:{term}"}
    for name, pattern in OVERSELL_OR_UNAUTHORIZED_ACTION_PATTERNS:
        if re.search(pattern, compact):
            return {"error": f"overconfident_sales_pressure_pattern:{name}"}
    return {}


def check_social_turn_over_carries_stale_business_context(question: str, reply: str) -> dict[str, Any]:
    """Prevent pure social closings from reviving stale business threads.

    The current message still wins. If the customer only thanks/says goodbye,
    Brain can be warm and brief; it should not reopen a previous car/price/
    contact collection thread unless the current turn explicitly asks to.
    """

    q = strip_nonsemantic_runtime_markers(question)
    r = normalize_space(reply)
    if not q or not r:
        return {}
    obligation = classify_social_reply_obligation(q)
    category = str(obligation.get("category") or "")
    if category not in {"thanks", "farewell"}:
        return {}
    if contains_any(q, PRICE_QUESTION_TERMS + RECOMMENDATION_QUESTION_TERMS + COMPARISON_QUESTION_TERMS):
        return {}
    if contains_any(q, APPOINTMENT_TOPIC_TERMS + TRADE_IN_TOPIC_TERMS + CARGO_TOPIC_TERMS):
        return {}
    if contains_any(q, ("接着", "继续", "刚才", "前面", "这台", "那台", "车", "价格", "预算", "看车", "电话")):
        return {}
    if contains_any(r, SOCIAL_CLOSING_STALE_CONTEXT_TERMS):
        return {"error": "social_closing_over_carries_stale_business_context"}
    return {}


def check_social_turn_revives_unsupported_business_context(
    question: str,
    reply: str,
    evidence_pack: dict[str, Any],
) -> dict[str, Any]:
    """Prevent pure social turns from reviving old unsupported entities.

    This check is intentionally generic. It does not know any account, product,
    image text, or industry-specific bad token. It only blocks a Brain draft
    that answers a greeting/summon by re-opening business/entity context that
    the current customer turn did not ask to continue and no authority source
    supports.
    """

    q = normalize_space(strip_nonsemantic_runtime_markers(question))
    r = normalize_space(reply)
    if not q or not r:
        return {}
    obligation = classify_social_reply_obligation(q)
    category = str(obligation.get("category") or "")
    if category not in {"greeting", "summon_or_chase", "social_short", "thanks", "farewell"}:
        return {}
    if social_companion_turn_has_real_business_intent(q):
        return {}
    if social_turn_explicitly_continues_prior_context(q):
        return {}
    if social_turn_has_valid_delay_followup_continuity(q, r, evidence_pack):
        return {}
    if contains_any(r, SOCIAL_TURN_STALE_BUSINESS_CONTEXT_TERMS):
        return {"error": "social_turn_revived_unsupported_business_context"}
    if reply_introduces_unbacked_entity_not_in_question(q, r):
        return {"error": "social_turn_revived_unsupported_business_context"}
    return {}


def social_turn_explicitly_continues_prior_context(question: str) -> bool:
    return contains_any(
        question,
        (
            "接着",
            "继续",
            "刚才",
            "前面",
            "上面",
            "上一条",
            "之前",
            "这台",
            "那台",
            "这个",
            "那个",
            "按刚才",
            "接着说",
            "继续说",
        ),
    )


def social_turn_has_valid_delay_followup_continuity(question: str, reply: str, evidence_pack: dict[str, Any]) -> bool:
    state = extract_conversation_interaction_state(evidence_pack)
    if str(state.get("suggested_reply_posture") or "") != "acknowledge_delay_then_continue":
        return False
    return check_delay_followup_context_continuity(question, reply, evidence_pack) == {}


def reply_introduces_unbacked_entity_not_in_question(question: str, reply: str) -> bool:
    compact_question = compact_entity_text(question)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9]*(?:[\s_\-/]+[A-Za-z0-9]+)*", str(reply or "")):
        token = compact_entity_text(match.group(0))
        if len(token) < 4 or not re.search(r"[a-zA-Z]", token):
            continue
        if token in compact_question:
            continue
        if is_common_social_reply_fragment(token):
            continue
        return True
    return False


def is_common_social_reply_fragment(fragment: str) -> bool:
    compact = compact_entity_text(fragment)
    if not compact:
        return True
    if re.fullmatch(r"[0-9]+", compact):
        return True
    common = (
        "在的",
        "在呢",
        "您说",
        "你说",
        "我在",
        "您好",
        "你好",
        "方便",
        "看到",
        "刚看到",
        "随时",
        "这边",
        "可以",
        "没问题",
        "马上",
        "好的",
    )
    return any(compact_entity_text(item) in compact or compact in compact_entity_text(item) for item in common)


def check_over_eager_business_redirect_after_social_fatigue(question: str, reply: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    """Flag repeated business pullback when strategy state says to soften it.

    This is a reviewer check only.  It does not write replacement wording and it
    does not weaken product/formal-knowledge authority rules.
    """

    state = extract_conversation_strategy_state(evidence_pack)
    if not state:
        return {}
    mode = str(state.get("suggested_engagement_mode") or "")
    fatigue = str(state.get("redirect_fatigue_level") or "")
    resisted = bool(state.get("customer_resists_business_redirect"))
    if mode == "boundary_only" or str(state.get("last_signal") or "") == "hard_boundary":
        return {}
    if mode not in {"social_companion", "soft_bridge"} and fatigue not in {"fatigued", "suppress"} and not resisted:
        return {}
    q = normalize_space(question)
    r = normalize_space(reply)
    if not q or not r:
        return {}
    if contains_any(q, INTERNAL_SECRET_PROBE_TERMS):
        return {}
    if social_companion_turn_has_real_business_intent(q):
        return {}
    if not reply_looks_like_business_pullback_after_social_fatigue(r, evidence_pack):
        return {}
    if fatigue == "light" and not resisted:
        return {}
    return {"error": "over_eager_business_redirect_after_social_fatigue"}


def reply_looks_like_business_pullback_after_social_fatigue(reply: str, evidence_pack: dict[str, Any]) -> bool:
    """Detect business pullback shapes after the customer asks to slow down.

    The check is deliberately broad and mechanism-level: explicit product
    mentions, business process terms, or EV/car-use follow-up terms count as
    pullback.  It only runs when conversation strategy already says to suppress
    business redirection, so normal business answers are unaffected.
    """

    r = normalize_space(reply)
    if not r:
        return False
    if contains_any(r, BUSINESS_REDIRECT_TERMS + SOCIAL_COMPANION_BUSINESS_PULLBACK_TERMS):
        return True
    for item in iter_authoritative_product_items(evidence_pack):
        if mentions_direct_product_entity(r, item):
            return True
    return False


def social_companion_turn_has_real_business_intent(question: str) -> bool:
    """Return true only when a fatigued social turn actually asks business.

    A complaint such as "怎么每句都往车上绕" contains the character "车", but
    it is not a fresh product request.  This helper keeps the reviewer generic:
    it only distinguishes real business intent from resistance/identity/social
    meta-talk, and never writes replacement wording.
    """

    q = normalize_space(question)
    if not q:
        return False
    if identity_probe_reply_discusses_identity_truth(q, "我不是机器人") or contains_any(q, ("别聊车", "别老聊车", "别推销", "别套话", "往车上绕", "只想随便聊")):
        return False
    strong_business_terms = (
        PRICE_QUESTION_TERMS
        + RECOMMENDATION_QUESTION_TERMS
        + COMPARISON_QUESTION_TERMS
        + APPOINTMENT_TOPIC_TERMS
        + TRADE_IN_TOPIC_TERMS
        + INSURANCE_TOPIC_TERMS
        + CARGO_TOPIC_TERMS
        + (
            "预算多少",
            "预算",
            "车型",
            "车源",
            "看车",
            "试驾",
            "贷款",
            "置换",
            "过户",
            "车况",
            "公里数",
            "配置",
            "库存",
            "到店",
            "预约",
        )
    )
    return contains_any(q, strong_business_terms)


def is_thin_social_or_common_sense_reply(question: str, reply: str, plan: dict[str, Any]) -> bool:
    """Ask Brain to repair very thin low-risk small talk/common-sense replies.

    This is intentionally generic: it does not prescribe a replacement answer
    and it does not authorize any product or policy fact.  It only catches
    non-greeting turns where Brain answered with a tiny isolated sentence that
    loses conversational continuity.
    """

    q = normalize_space(question)
    r = normalize_space(reply)
    if not q or not r:
        return False
    if is_social_only_message(q):
        return False
    if contains_any(q, BOUNDARY_OR_INTERNAL_PROBE_TERMS):
        return False
    if contains_any(q, PRICE_QUESTION_TERMS + RECOMMENDATION_QUESTION_TERMS + COMPARISON_QUESTION_TERMS):
        return False
    if contains_any(q, APPOINTMENT_TOPIC_TERMS + TRADE_IN_TOPIC_TERMS + CARGO_TOPIC_TERMS):
        return False
    if visible_content_char_count(r) >= 18:
        return False
    action = str(plan.get("recommended_action") or "send_reply").strip()
    if action != "send_reply":
        return False
    answer_mode = str(plan.get("answer_mode") or "").strip()
    if answer_mode not in {"soft_social_reply", "soft_redirect_to_business", "direct_answer", "compare_options"}:
        return False
    if plan.get("facts_claimed"):
        return False
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    if evidence.get("product_ids") or evidence.get("formal_knowledge_ids"):
        return False
    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    if bool(risk.get("needs_handoff")):
        return False
    hard_tags = {"illegal_request", "prompt_injection", "policy_violation", "finance_commitment", "price_commitment"}
    risk_tags = {str(item).strip().lower() for item in (risk.get("risk_tags") or []) if str(item).strip()}
    return not bool(risk_tags & hard_tags)


def check_delay_followup_context_continuity(question: str, reply: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    """Flag fresh-greeting replies when runtime says the customer is chasing.

    This is a generic continuity check. It only asks Brain to repair; it does
    not prescribe wording or authorize facts.
    """

    state = extract_conversation_interaction_state(evidence_pack)
    if not state:
        return {}
    if str(state.get("suggested_reply_posture") or "") != "acknowledge_delay_then_continue":
        return {}
    if not bool(state.get("customer_chase_up_detected")):
        return {}
    q = normalize_space(question)
    r = normalize_space(reply)
    if not q or not r:
        return {}
    if not is_social_only_message(q):
        return {}
    delay_ack_terms = (
        "抱歉",
        "不好意思",
        "回慢",
        "慢了",
        "等久",
        "久等",
        "刚才在",
        "查",
        "核",
        "打字",
        "处理",
    )
    context_anchor_terms = (
        "前面",
        "上面",
        "这个方向",
        "这台",
        "这辆",
        "上一",
        "刚说",
        "刚问",
    )
    fresh_greeting_terms = ("您说", "你说", "在的", "在呢", "马上跟您聊", "刚看到")
    mentions_unanswered = reply_mentions_unanswered_context(r, str(state.get("last_unanswered_customer_text") or ""))
    if contains_any(r, delay_ack_terms) or contains_any(r, context_anchor_terms) or mentions_unanswered:
        return {}
    if len(r) <= 80 and contains_any(r, fresh_greeting_terms):
        return {"error": "delay_followup_reply_looks_like_fresh_greeting"}
    return {}


def reply_mentions_unanswered_context(reply: str, unanswered_text: str) -> bool:
    question = normalize_space(unanswered_text)
    if not question:
        return False
    reply_text = normalize_space(reply)
    tokens = [
        item
        for item in re.split(r"[\s，。！？!?、,；;：:（）()【】\\[\\]\"'“”]+", question)
        if len(item) >= 2 and item not in {"这个", "那个", "一下", "帮我", "给我", "看看", "推荐", "价格", "多少"}
    ]
    return any(token in reply_text for token in tokens[:12])


def extract_conversation_interaction_state(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence_pack, dict):
        return {}
    value = evidence_pack.get("conversation_interaction_state")
    if isinstance(value, dict) and value:
        return value
    conversation = evidence_pack.get("conversation") if isinstance(evidence_pack.get("conversation"), dict) else {}
    value = conversation.get("conversation_interaction_state")
    if isinstance(value, dict) and value:
        return value
    knowledge = evidence_pack.get("knowledge") if isinstance(evidence_pack.get("knowledge"), dict) else {}
    value = knowledge.get("conversation_interaction_state")
    if isinstance(value, dict) and value:
        return value
    return {}


def extract_conversation_strategy_state(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence_pack, dict):
        return {}
    for key in ("conversation_strategy_state", "strategy_state"):
        value = evidence_pack.get(key)
        if isinstance(value, dict) and value:
            return value
    conversation = evidence_pack.get("conversation") if isinstance(evidence_pack.get("conversation"), dict) else {}
    value = conversation.get("conversation_strategy_state")
    if isinstance(value, dict) and value:
        return value
    knowledge = evidence_pack.get("knowledge") if isinstance(evidence_pack.get("knowledge"), dict) else {}
    value = knowledge.get("conversation_strategy_state")
    if isinstance(value, dict) and value:
        return value
    return {}


def classify_social_reply_obligation(text: str) -> dict[str, Any]:
    """Classify low-risk social turns that still require a Brain reply.

    This helper is a contract signal only. It does not provide customer-visible
    wording and must not be used as a local reply template.
    """

    clean_text = strip_nonsemantic_runtime_markers(text).strip()
    clean = re.sub(
        r"[\s。！？!?，,、~～\.\-_:：；;]+",
        "",
        clean_text,
    ).lower()
    if not clean:
        return {"must_reply": False, "category": "", "matched_term": ""}

    def match_category(category: str, terms: tuple[str, ...]) -> dict[str, Any] | None:
        normalized_terms = tuple(item.lower() for item in terms)
        for term in normalized_terms:
            if clean == term or (len(clean) <= 7 and term in clean):
                return {"must_reply": True, "category": category, "matched_term": term}
        return None

    for category, terms in (
        ("greeting", GREETING_TERMS),
        ("summon_or_chase", SOCIAL_SUMMON_TERMS),
        ("thanks", THANKS_TERMS),
        ("farewell", FAREWELL_TERMS),
    ):
        matched = match_category(category, terms)
        if matched:
            return matched
    if is_social_only_message(text):
        if contains_any(clean_text, THANKS_TERMS):
            return {"must_reply": True, "category": "thanks", "matched_term": ""}
        if contains_any(clean_text, FAREWELL_TERMS):
            return {"must_reply": True, "category": "farewell", "matched_term": ""}
        return {"must_reply": True, "category": "social_short", "matched_term": ""}
    terms = tuple(item.lower() for item in GREETING_TERMS + GOODBYE_TERMS + SOCIAL_SUMMON_TERMS)
    if len(clean) <= 7 and any(term in clean for term in terms):
        return {"must_reply": True, "category": "social_short", "matched_term": ""}
    return {"must_reply": False, "category": "", "matched_term": ""}


def social_message_requires_visible_brain_reply(text: str) -> bool:
    return bool(classify_social_reply_obligation(text).get("must_reply"))


def is_generic_stall_reply(text: str) -> bool:
    clean = normalize_space(text)
    if not contains_any(clean, GENERIC_STALL_TERMS):
        return False
    if is_clear_boundary_refusal_reply(clean):
        return False
    informative = PRICE_VALUE_RE.search(clean) or contains_any(clean, DIRECT_RECOMMENDATION_TERMS)
    return not informative and len(clean) <= 90


def is_clear_boundary_refusal_reply(text: str) -> bool:
    """Recognize Brain-authored refusals that answer a hard-boundary request.

    This is not a local reply template. It only prevents the quality gate from
    misclassifying a substantive refusal as a generic "I'll check" stall.
    """

    clean = normalize_space(text)
    if not clean:
        return False
    compact = re.sub(r"\s+", "", clean)
    if not contains_any(compact, CLEAR_BOUNDARY_REFUSAL_TERMS):
        return False
    if contains_any(compact, ("负责人", "专员", "人工客服", "马上回复", "稍后回复")) and not contains_any(
        compact,
        CLEAR_BOUNDARY_SUBSTANTIVE_TERMS,
    ):
        return False
    return contains_any(compact, CLEAR_BOUNDARY_SUBSTANTIVE_TERMS)


def check_appointment_confirmation_boundary(question: str, reply: str) -> dict[str, Any]:
    q = normalize_space(question)
    r = normalize_space(reply)
    if not contains_any(q, APPOINTMENT_TOPIC_TERMS):
        return {}
    if contains_any(r, APPOINTMENT_OVERCOMMIT_TERMS) and not contains_any(r, APPOINTMENT_CONFIRMATION_BOUNDARY_TERMS):
        return {"error": "appointment_commitment_without_confirmation_boundary"}
    if contains_any(q, ("电话", "手机号", "联系方式", "我叫", "姓", "周六", "周日", "上午", "下午", "晚上", "点")):
        if not contains_any(r, APPOINTMENT_CONFIRMATION_BOUNDARY_TERMS):
            return {"error": "missing_appointment_confirmation_boundary"}
    return {}


def check_trade_in_process_boundary(question: str, reply: str) -> dict[str, Any]:
    q = normalize_space(question)
    r = normalize_space(reply)
    if not contains_any(q + r, TRADE_IN_TOPIC_TERMS):
        return {}
    if contains_unqualified_trade_in_overcommit(r):
        return {"error": "trade_in_process_overcommit_without_formal_authority"}
    if contains_any(r, TRADE_IN_FINAL_PRICE_TERMS + TRADE_IN_DIRECT_PRICE_REQUEST_TERMS) and not trade_in_final_price_has_verification_boundary(r):
        return {"error": "trade_in_final_price_missing_verification_boundary"}
    return {}


def contains_unqualified_trade_in_overcommit(reply: str) -> bool:
    """Only block unconditional trade-in promises.

    Formal knowledge may describe a bounded process such as appointment-based
    inspection or payment after contract/procedure verification.  The quality
    gate should block risky guarantees, not fight Brain on properly qualified
    process explanations.
    """

    r = normalize_space(reply)
    if not contains_any(r, TRADE_IN_OVERCOMMIT_TERMS):
        return False
    risky_hits = tuple(term for term in TRADE_IN_OVERCOMMIT_TERMS if term in r)
    for term in risky_hits:
        window = context_window(r, term, before=24, after=28)
        if term_is_trade_in_process_qualified(window, term=term):
            continue
        return True
    return False


def context_window(text: str, needle: str, *, before: int = 16, after: int = 24) -> str:
    idx = text.find(needle)
    if idx < 0:
        return text
    start = max(0, idx - before)
    end = min(len(text), idx + len(needle) + after)
    return text[start:end]


def term_is_trade_in_process_qualified(window: str, *, term: str) -> bool:
    w = normalize_space(window)
    if not w:
        return False
    if "上门" in term:
        return contains_any(
            w,
            (
                "预约上门",
                "可预约上门",
                "到店或上门",
                "到店/上门",
                "先预约",
                "预约",
                "需要确认",
                "需要核实",
                "确认时间",
                "确认排期",
                "评估师",
            ),
        )
    if "打款" in term or "付款" in term:
        return contains_any(
            w,
            (
                "签合同",
                "合同",
                "手续",
                "手续齐全",
                "核实后",
                "确认后",
                "验车后",
                "评估后",
                "最终收购价",
                "以门店",
                "以实际",
            ),
        )
    return False


def trade_in_final_price_has_verification_boundary(reply: str) -> bool:
    r = normalize_space(reply)
    compact = re.sub(r"\s+", "", r)
    if trade_in_reply_has_natural_final_price_boundary(compact):
        return True
    if contains_any(r, ("最终价格以验车", "最终价格以门店", "最终价格以实际", "最终报价以验车", "最终收购价以验车")):
        return True
    if contains_any(
        r,
        (
            "以验车为准",
            "以门店验车",
            "以实际验车",
            "以现场核验",
            "以现场核实",
            "以实车检测",
            "按门店验车",
            "按实际车况",
            "按现场核验",
            "按现场核实",
            "验车核实后确认",
            "验车核实后再给",
            "验车核实后给",
            "验车核实后再确认",
            "验车后确定",
            "验车后才能确定",
            "到店验车后确定",
            "到店验车后才能确定",
            "现场核实后再给",
            "现场核验后再给",
            "实车检测后再给",
            "现场核验结果为准",
        ),
    ):
        return True
    return bool(
        re.search(
            r"(最终价格|最终报价|最终收购价).{0,28}(以|按).{0,24}(验车|门店|实际|车况|检测|核验|核实|现场|手续).{0,16}(为准|确认)",
            r,
        )
        or re.search(
            r"(以|按).{0,24}(验车|门店|实际|车况|检测|核验|核实|现场|手续).{0,16}(为准|确认).{0,28}(最终价格|最终报价|最终收购价)",
            r,
        )
        or re.search(
            r"(验车|现场|实车|车况|检测|核验|核实|手续).{0,12}(后|完).{0,12}(再|才能|才)?(给|确认|核定|确定).{0,8}(最终价格|最终报价|最终收购价|收购价|报价|价格)",
            r,
        )
        or re.search(
            r"(最终价格|最终报价|最终收购价|收购价|报价|价格).{0,14}(验车|现场|实车|车况|检测|核验|核实|手续).{0,12}(确认|核定|确定|为准)",
            r,
        )
    )


def trade_in_reply_has_natural_final_price_boundary(compact_reply: str) -> bool:
    """Accept natural Brain wording that refuses a fixed trade-in price.

    The quality gate should verify the presence of a boundary signal, not force
    one fixed phrase.  This keeps Brain as the reply owner while still blocking
    unconditional final-price promises.
    """

    text = str(compact_reply or "")
    if not text:
        return False
    cannot_set_price = (
        re.search(r"(不能|无法|没法|不好|不方便).{0,8}(直接|现在|远程|线上)?(给|定|报).{0,8}(准价|准数|具体价|具体数字|最终价|最终收购价|收购价|报价|价格)", text)
        or re.search(r"(线上|远程|现在).{0,8}(给不了|定不了|报不了|不能给|不能定|不能报|没法给|没法定|没法报|无法给|无法定|无法报).{0,8}(准价|准数|具体价|具体数字|最终价|最终收购价|收购价|报价|价格)", text)
        or re.search(r"(没有|没|未有|未看到).{0,10}(实车|手续|车况|检测|验车).{0,12}(给不了|定不了|报不了|不能给|不能定|不能报|没法给|没法定|没法报|无法给|无法定|无法报).{0,8}(准价|准数|具体价|具体数字|最终价|最终收购价|收购价|报价|价格)", text)
        or re.search(r"(准价|准数|具体价|具体数字|最终价|最终收购价|收购价|报价|价格|抵扣价|抵车款).{0,8}(不能|无法|没法|不好|不方便|给不了|定不了|报不了).{0,8}(直接|现在|远程|线上)?(给|定|报)?", text)
        or re.search(r"(验完车|验过车|验过实车|验车后|到店验车|门店验车|现场验车|实车检测).{0,18}(才能|才|再|后)?(定|给|报|确认|核定).{0,12}(准价|准数|具体数字|最终价|最终收购价|最终价格|抵多少|抵扣价|抵车款|收购价|报价|价格)", text)
        or re.search(r"(准价|准数|具体数字|最终价|最终收购价|最终价格|抵多少|抵扣价|抵车款|收购价|报价|价格).{0,18}(验完车|验过车|验过实车|验车后|到店验车|门店验车|现场验车|实车检测).{0,12}(才能|才|再|后)?(定|给|报|确认|核定)", text)
    )
    dependency_boundary = contains_any(
        text,
        (
            "得看实车",
            "要看实车",
            "看实车",
            "看车况",
            "看配置",
            "看手续",
            "看行情",
            "看完手续",
            "看完车况",
            "发几张",
            "外观内饰图",
            "按车况",
            "按配置",
            "按手续",
            "按行情",
            "逐项验",
            "验完车",
            "验过车",
            "验过实车",
            "验完",
            "结合车况",
            "结合配置",
            "结合手续",
            "结合行情",
            "实车车况",
            "实车检测",
            "验车核",
            "核完手续",
            "验车核完手续",
            "手续才能确认",
            "门店验车",
            "现场验车",
            "现场核验",
            "现场核实",
            "评估师",
        ),
    )
    whole_reply_boundary = (
        contains_any(
            text,
            (
                "给不了准价",
                "不能给准价",
                "没法给准价",
                "无法给准价",
                "给不了准数",
                "不能给准数",
                "没法给准数",
                "无法给准数",
                "给不了具体价",
                "不能给具体价",
                "没法给具体价",
                "无法给具体价",
                "不能直接报",
                "不建议直接报死价",
            ),
        )
        and contains_any(
            text,
            (
                "最终能抵多少",
                "最终抵多少",
                "抵多少车款",
                "准价",
                "准数",
                "具体价",
                "最终价格",
                "最终报价",
                "收购价",
            ),
        )
        and dependency_boundary
    )
    natural_cross_sentence_boundary = bool(
        re.search(r"(验车|实车|车况|检测|核验|核实).{0,18}(手续).{0,12}(才能|才|后|再)?(确认|核定|确定|给)", text)
        or re.search(r"(手续).{0,18}(验车|实车|车况|检测|核验|核实).{0,12}(才能|才|后|再)?(确认|核定|确定|给)", text)
    )
    return bool((cannot_set_price or whole_reply_boundary) and (dependency_boundary or natural_cross_sentence_boundary))


def is_contextual_recommendation_followup(text: str) -> bool:
    """Detect relative follow-ups that ask Brain to use prior need context."""
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    context_terms = (
        "刚才",
        "前面",
        "上面",
        "按你说的",
        "按我说的",
        "按刚才",
        "按前面",
        "这两台",
        "这几个",
        "那两台",
        "那几个",
        "这个方向",
    )
    action_terms = (
        "挑",
        "推荐",
        "选",
        "筛",
        "给我",
        "直接",
        "别再问",
        "不用再问",
        "哪台",
        "哪个",
        "更适合",
    )
    return any(term in compact for term in context_terms) and any(term in compact for term in action_terms)


def is_direct_decision_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    return any(re.sub(r"\s+", "", term).lower() in compact for term in DIRECT_DECISION_REQUEST_TERMS)


def reply_asks_broad_new_need_instead_of_decision(reply: str) -> bool:
    clean = re.sub(r"\s+", "", str(reply or "")).lower()
    if not clean:
        return False
    clear_choice_terms = CLEAR_RECOMMENDATION_STRUCTURE_TERMS + (
        "主推",
        "首推",
        "我会选",
        "我选",
        "就选",
        "选这台",
        "挑这台",
        "定这台",
        "放第一",
    )
    if contains_any(reply, clear_choice_terms):
        return False
    question_count = clean.count("？") + clean.count("?")
    broad_ask = any(re.sub(r"\s+", "", term).lower() in clean for term in DIRECT_DECISION_BROAD_NEED_QUESTION_TERMS)
    return bool(broad_ask and (question_count > 0 or contains_any(reply, ("告诉我", "说下", "发我", "您说", "你说"))))


def exposes_ai_identity(text: str) -> bool:
    return contains_any(
        text,
        (
            "我是AI",
            "我是ai",
            "机器人",
            "自动回复",
            "智能客服",
            "大模型",
            "真人客服",
            "不是AI",
            "不是ai",
            "不是机器人",
            "不是什么AI",
            "不是什么ai",
        ),
    )


def identity_probe_reply_discusses_identity_truth(question: str, reply: str) -> bool:
    """Reject identity-truth discussion only when the customer is probing identity.

    This is a contract signal for Brain repair. It does not author replacement
    text; it only prevents ambiguous admissions/denials such as "被发现了" or
    "我不是机器人" from becoming customer-visible.
    """

    q = re.sub(r"\s+", "", strip_nonsemantic_runtime_markers(str(question or ""))).lower()
    r = re.sub(r"\s+", "", str(reply or "")).lower()
    if not q or not r:
        return False
    probe_terms = (
        "是不是ai",
        "是ai吗",
        "ai自动",
        "机器人",
        "自动回复",
        "智能客服",
        "机器客服",
        "人工智能",
        "真人吗",
        "是真人",
        "真人在",
    )
    if not any(term in q for term in probe_terms):
        return False
    ambiguous_admission_terms = (
        "被发现了",
        "被你发现了",
        "被您发现了",
        "被看出来了",
        "你猜对了",
        "您猜对了",
        "猜对了",
        "不装了",
        "实话说",
        "说实话",
        "确实是",
        "算是吧",
    )
    explicit_identity_terms = (
        "我是ai",
        "我是机器人",
        "我是智能客服",
        "我是自动回复",
        "我是人工智能",
        "我是真人",
        "我是人工",
        "不是ai",
        "不是机器人",
        "不是自动回复",
    )
    return any(term in r for term in ambiguous_admission_terms + explicit_identity_terms)


def collect_quality_product_terms(plan: dict[str, Any], evidence_pack: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    understanding = plan.get("understanding") if isinstance(plan.get("understanding"), dict) else {}
    entities = understanding.get("normalized_entities") if isinstance(understanding.get("normalized_entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for key in ("raw", "normalized", "name", "alias"):
            add_unique_text(terms, entity.get(key))
    for item in iter_authoritative_product_items(evidence_pack):
        for key in ("name", "title", "model", "sku", "id", "product_id"):
            add_unique_text(terms, item.get(key))
        aliases = item.get("aliases") or item.get("alias")
        if isinstance(aliases, list):
            for alias in aliases:
                add_unique_text(terms, alias)
        else:
            add_unique_text(terms, aliases)
    return terms


def collect_authoritative_product_ids(evidence_pack: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in iter_authoritative_product_items(evidence_pack):
        for key in ("id", "product_id", "sku"):
            value = str(item.get(key) or "").strip()
            if value:
                ids.add(value)
    return ids


def check_budget_fit_recommendation(
    reply: str,
    evidence_pack: dict[str, Any],
    *,
    current_message: str = "",
    budget_upper: float,
) -> dict[str, Any]:
    items = iter_authoritative_product_items(evidence_pack)
    if not items:
        return {}
    affordable_ids: set[str] = set()
    for item in items:
        price = quality_product_price_wan(item)
        if price <= 0 or price > budget_upper + 0.03:
            continue
        affordable_ids.add(quality_product_identity(item) or str(item.get("name") or "budget_fit_product"))
    if not affordable_ids:
        return {}
    over_budget_mentioned_set: set[str] = set()
    affordable_mentioned_set: set[str] = set()
    for item in items:
        terms = product_item_quality_terms(item)
        if not terms or not mentions_any_entity(reply, terms):
            continue
        item_id = quality_product_identity(item) or str(item.get("name") or "")
        price = quality_product_price_wan(item)
        if price > budget_upper + 0.03:
            over_budget_mentioned_set.add(item_id or "over_budget")
        elif price > 0:
            affordable_mentioned_set.add(item_id or "affordable")
    over_budget_mentioned = sorted(over_budget_mentioned_set)
    affordable_mentioned = sorted(affordable_mentioned_set)
    first_mentioned = first_mentioned_budget_class(reply, items, budget_upper=budget_upper)
    if first_mentioned == "over_budget" and not reply_marks_over_budget_caveat(reply):
        return {
            "error": "over_budget_primary_recommendation_without_caveat",
            "over_budget_mentioned": over_budget_mentioned,
            "affordable_mentioned": affordable_mentioned,
        }
    if over_budget_mentioned and not affordable_mentioned:
        return {
            "error": "over_budget_recommendation_ignores_budget_fit_candidates",
            "over_budget_mentioned": over_budget_mentioned,
        }
    if (
        over_budget_mentioned
        and len(affordable_mentioned) < 2
        and is_broad_product_recommendation_request(current_message)
    ):
        if len(affordable_ids) < 2 and reply_marks_over_budget_caveat(reply):
            return {}
        return {
            "error": "over_budget_recommendation_fills_budget_slot",
            "over_budget_mentioned": over_budget_mentioned,
            "affordable_mentioned": affordable_mentioned,
            "affordable_candidate_count": len(affordable_ids),
        }
    return {}


def reply_marks_over_budget_caveat(reply: str) -> bool:
    return contains_any(reply, OVER_BUDGET_CAVEAT_TERMS)


def first_mentioned_budget_class(reply: str, items: list[dict[str, Any]], *, budget_upper: float) -> str:
    positions: list[tuple[int, str]] = []
    for item in items:
        position = direct_product_entity_position(reply, item)
        if position < 0:
            continue
        price = quality_product_price_wan(item)
        if price <= 0:
            continue
        positions.append((position, "over_budget" if price > budget_upper + 0.03 else "affordable"))
    if not positions:
        return ""
    positions.sort(key=lambda pair: pair[0])
    return positions[0][1]


def check_known_budget_fit_product_price_uncertainty(reply: str, evidence_pack: dict[str, Any], *, budget_upper: float) -> dict[str, Any]:
    if not contains_any(reply, KNOWN_PRICE_UNCERTAINTY_TERMS):
        return {}
    uncertain_affordable: list[str] = []
    for item in iter_authoritative_product_items(evidence_pack):
        price = quality_product_price_wan(item)
        if price <= 0 or price > budget_upper + 0.03:
            continue
        terms = product_item_quality_terms(item)
        if terms and mentions_any_entity(reply, terms):
            uncertain_affordable.append(str(item.get("id") or item.get("product_id") or item.get("name") or "budget_fit_product"))
    if uncertain_affordable:
        return {
            "error": "known_budget_fit_product_marked_price_uncertain",
            "product_ids": uncertain_affordable,
        }
    return {}


def check_multi_product_recommendation_count(
    reply: str,
    evidence_pack: dict[str, Any],
    *,
    current_message: str,
    budget_upper: float | None,
) -> dict[str, Any]:
    if not is_multi_product_recommendation_request(current_message):
        return {}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in iter_authoritative_product_items(evidence_pack):
        item_id = quality_product_identity(item) or str(item.get("name") or "")
        if not item_id or item_id in seen:
            continue
        price = quality_product_price_wan(item)
        if budget_upper is not None and budget_upper > 0 and (price <= 0 or price > budget_upper + 0.03):
            continue
        seen.add(item_id)
        candidates.append(item)
    if len(candidates) < 2:
        return {}
    mentioned: list[str] = []
    for item in candidates:
        if mentions_direct_product_entity(reply, item):
            item_id = quality_product_identity(item) or str(item.get("name") or "")
            if item_id and item_id not in mentioned:
                mentioned.append(item_id)
    if len(mentioned) >= 2:
        return {}
    if reply_transparently_limits_multi_recommendation(reply, mentioned_count=len(mentioned)):
        return {}
    return {
        "error": "missing_multiple_product_recommendations",
        "mentioned_product_ids": mentioned,
        "candidate_count": len(candidates),
    }


def reply_transparently_limits_multi_recommendation(reply: str, *, mentioned_count: int) -> bool:
    if mentioned_count < 1:
        return False
    clean = normalize_space(reply)
    if not clean:
        return False
    no_second_terms = (
        "没有第二台",
        "暂时没有第二台",
        "当前没有第二台",
        "目前没有第二台",
        "只有一台",
        "只看到一台",
        "只确认到一台",
    )
    scope_terms = (
        "同时满足",
        "都满足",
        "完全贴合",
        "完全符合",
        "预算",
        "南京",
        "条件",
        "需求",
        "要求",
    )
    if contains_any(clean, no_second_terms) and contains_any(clean, scope_terms):
        return True
    explicit_limitation_terms = (
        "先看这一台",
        "不拿不贴合的车硬凑",
    )
    return contains_any(clean, explicit_limitation_terms) and contains_any(clean, no_second_terms)


def is_multi_product_recommendation_request(question: str) -> bool:
    clean = str(question or "")
    return contains_any(
        clean,
        (
            "两台",
            "两款",
            "两个",
            "两三个",
            "2台",
            "2款",
            "几个方向",
            "三台",
            "三款",
            "三个方向",
        ),
    ) and is_broad_product_recommendation_request(clean)


def reply_has_clear_recommendation_or_choice(reply: str, plan: dict[str, Any], evidence_pack: dict[str, Any]) -> bool:
    """Return whether the visible reply makes a concrete choice.

    This is deliberately a quality-contract helper, not a business rule.  Brain
    may express recommendations naturally ("主推/第一/备选/三个方向"), so the
    gate should recognize clear, product-anchored choices without forcing a
    narrow template.
    """

    clean = normalize_space(reply)
    if not clean:
        return False
    if contains_any(clean, DIRECT_RECOMMENDATION_TERMS):
        return True
    if not reply_mentions_authoritative_product(clean, evidence_pack):
        return False
    if contains_any(clean, CLEAR_RECOMMENDATION_STRUCTURE_TERMS):
        return True
    answer_mode = str(plan.get("answer_mode") or "").strip()
    if answer_mode in {"recommend_from_catalog", "compare_options"} and PRICE_VALUE_RE.search(clean):
        return True
    return False


def reply_mentions_authoritative_product(reply: str, evidence_pack: dict[str, Any]) -> bool:
    for item in iter_authoritative_product_items(evidence_pack):
        if mentions_direct_product_entity(reply, item):
            return True
    return False


def check_available_cargo_fit_candidate_for_broad_request(
    reply: str,
    evidence_pack: dict[str, Any],
    *,
    budget_upper: float | None,
) -> dict[str, Any]:
    candidates = collect_available_cargo_fit_candidates(evidence_pack, budget_upper=budget_upper)
    if not candidates:
        return {}
    candidate_ids = [quality_product_identity(item) or str(item.get("name") or "") for item in candidates[:5]]
    if contains_any(reply, NO_NONSEDAN_AVAILABLE_TERMS):
        return {
            "error": "contradicts_available_cargo_fit_candidate",
            "candidate_ids": candidate_ids,
        }
    if any(mentions_direct_product_entity(reply, item) for item in candidates):
        return {}
    return {
        "error": "missing_available_cargo_fit_candidate",
        "candidate_ids": candidate_ids,
    }


def collect_available_cargo_fit_candidates(evidence_pack: dict[str, Any], *, budget_upper: float | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in iter_authoritative_product_items(evidence_pack):
        if not is_cargo_fit_product_item(item):
            continue
        price = quality_product_price_wan(item)
        if budget_upper is not None and budget_upper > 0 and (price <= 0 or price > budget_upper + 0.03):
            continue
        identity = quality_product_identity(item) or str(item.get("name") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        candidates.append(item)
    return candidates


def check_unverified_cargo_capacity_claim(reply: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    """Block affirmative cargo-fit claims when no dimensions are available."""

    if cargo_dimension_evidence_available(evidence_pack):
        return {}
    check_text = str(reply or "")
    for neutral_phrase in ("能不能塞下", "能不能装下", "能否塞下", "能否装下", "是否塞下", "是否装下"):
        check_text = check_text.replace(neutral_phrase, "")
    affirmative_terms = (
        "大概率能塞",
        "大概率能装",
        "基本能塞",
        "基本能装",
        "应该能塞",
        "应该能装",
        "能塞下",
        "能装下",
        "塞得下",
        "装得下",
        "够装",
        "问题不大",
    )
    if contains_any(check_text, affirmative_terms):
        return {"error": "unverified_cargo_capacity_affirmative_claim"}
    return {}


def cargo_dimension_evidence_available(evidence_pack: dict[str, Any]) -> bool:
    dimension_terms = ("后备厢容积", "后备箱容积", "放倒后", "装载尺寸", "容积", "尺寸", "长", "宽", "高", "mm", "cm", "l")
    cargo_terms = ("后备厢", "后备箱", "装载", "第二排", "后排", "放倒", "梯子", "工具箱")
    for item in iter_authoritative_product_items(evidence_pack):
        text = json.dumps(item, ensure_ascii=False).lower()
        if contains_any(text, dimension_terms) and contains_any(text, cargo_terms):
            return True
    return False


def is_cargo_fit_product_item(item: dict[str, Any]) -> bool:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    text = compact_entity_text(
        " ".join(
            str(value or "")
            for value in (
                item.get("category"),
                item.get("name"),
                item.get("title"),
                item.get("specs"),
                data.get("category"),
                data.get("name"),
                data.get("title"),
                data.get("specs"),
            )
        )
    )
    return any(compact_entity_text(term) in text for term in CARGO_FIT_CATEGORY_TERMS)


def check_relative_context_product_binding(reply: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    recent_ids = collect_recent_context_product_ids(evidence_pack)
    visible_history_ids = collect_visible_history_context_product_ids(evidence_pack)
    allowed_ids = [*recent_ids]
    for item in visible_history_ids:
        add_unique_product_id(allowed_ids, item)
    if not allowed_ids:
        return {}
    allowed = set(allowed_ids)
    mentioned: list[str] = []
    disallowed: list[str] = []
    for item in iter_authoritative_product_items(evidence_pack):
        item_id = quality_product_identity(item)
        if not item_id or not mentions_direct_product_entity(reply, item):
            continue
        if item_id not in mentioned:
            mentioned.append(item_id)
        if item_id not in allowed and item_id not in disallowed:
            disallowed.append(item_id)
    if disallowed:
        return {
            "error": "relative_context_product_drift",
            "mentioned_product_ids": mentioned,
            "allowed_recent_product_ids": allowed_ids,
            "allowed_visible_history_product_ids": visible_history_ids,
            "disallowed_product_ids": disallowed,
        }
    if mentioned and not any(item in allowed for item in mentioned):
        return {
            "error": "missing_relative_context_product_reference",
            "mentioned_product_ids": mentioned,
            "allowed_recent_product_ids": allowed_ids,
            "allowed_visible_history_product_ids": visible_history_ids,
        }
    if not mentioned:
        return {
            "error": "missing_relative_context_product_reference",
            "mentioned_product_ids": [],
            "allowed_recent_product_ids": allowed_ids,
            "allowed_visible_history_product_ids": visible_history_ids,
        }
    return {}


def is_ambiguous_product_followup(question: str, evidence_pack: dict[str, Any]) -> bool:
    if not collect_primary_context_product_id(evidence_pack):
        return False
    if not contains_any(question, AMBIGUOUS_PRODUCT_FOLLOWUP_TERMS):
        return False
    if is_broad_product_recommendation_request(question):
        return False
    # If the customer explicitly names a product in this turn, let that explicit
    # mention override prior context. The drift guard is for natural follow-ups
    # such as "油耗和保养呢" or "车况会说清楚吗".
    for item in iter_authoritative_product_items(evidence_pack):
        if mentions_direct_product_entity(question, item):
            return False
    return True


def is_broad_product_recommendation_request(question: str) -> bool:
    clean = str(question or "")
    if contains_any(clean, ("这台", "这辆", "这个车", "它", "刚才", "前面", "上面", "这两台", "那两台")):
        return False
    return contains_any(
        clean,
        (
            "给我两三个",
            "两三个方向",
            "几个方向",
            "三个方向",
            "先按",
            "按预算",
            "推荐",
            "建议",
            "方向",
            "挑两台",
            "挑两款",
            "挑几个",
            "挑几台",
            "选两台",
            "选两款",
            "两台靠谱",
            "两款靠谱",
        ),
    )


def check_ambiguous_followup_product_binding(reply: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    primary_id = collect_primary_context_product_id(evidence_pack)
    if not primary_id:
        return {}
    mentioned: list[str] = []
    drifted: list[str] = []
    first_positions: dict[str, int] = {}
    for item in iter_authoritative_product_items(evidence_pack):
        item_id = quality_product_identity(item)
        if not item_id:
            continue
        position = direct_product_entity_position(reply, item)
        if position < 0:
            continue
        if item_id not in mentioned:
            mentioned.append(item_id)
        first_positions[item_id] = min(first_positions.get(item_id, position), position)
        if item_id != primary_id and item_id not in drifted:
            drifted.append(item_id)
    if drifted:
        primary_position = first_positions.get(primary_id, -1)
        first_drift_position = min(first_positions.get(item_id, 10**9) for item_id in drifted)
        if primary_position >= 0 and primary_position < first_drift_position:
            return {}
        return {
            "error": "ambiguous_followup_product_drift",
            "primary_product_id": primary_id,
            "mentioned_product_ids": mentioned,
            "drifted_product_ids": drifted,
        }
    primary_item = find_authoritative_product_item_by_id(evidence_pack, primary_id)
    if primary_item and mentions_direct_product_entity(reply, primary_item):
        return {}
    return {}


def collect_recent_context_product_ids(evidence_pack: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for context in iter_conversation_contexts(evidence_pack):
        for item in context.get("recent_product_ids", []) or []:
            add_unique_product_id(ids, item)
        add_unique_product_id(ids, context.get("last_product_id"))
    return ids[:5]


def collect_visible_history_context_product_ids(evidence_pack: dict[str, Any]) -> list[str]:
    """Recover product context from prior visible assistant replies.

    This is a guard against quality-gate false positives when the conversation
    state did not persist all products from the last visible recommendation.
    Only self/bot history lines are considered, so a customer's casual product
    mention does not become an authorized "previous recommendation".
    """

    conversation = evidence_pack.get("conversation") if isinstance(evidence_pack.get("conversation"), dict) else {}
    history_text = str(conversation.get("history_text") or "")
    if not history_text:
        return []
    assistant_texts: list[str] = []
    for line in history_text.splitlines():
        clean = str(line or "").strip()
        if clean.startswith("[客服]") or clean.startswith("[AI客服]"):
            assistant_texts.append(clean)
    if not assistant_texts:
        return []
    ids: list[str] = []
    joined = "\n".join(assistant_texts[-4:])
    for item in iter_authoritative_product_items(evidence_pack):
        item_id = quality_product_identity(item)
        if not item_id or item_id in ids:
            continue
        if mentions_direct_product_entity(joined, item):
            ids.append(item_id)
    return ids[:5]


def collect_primary_context_product_id(evidence_pack: dict[str, Any]) -> str:
    for context in iter_conversation_contexts(evidence_pack):
        product_id = str(context.get("last_product_id") or "").strip()
        if product_id:
            return product_id
        recent_ids = context.get("recent_product_ids") if isinstance(context.get("recent_product_ids"), list) else []
        for item in recent_ids:
            product_id = str(item or "").strip()
            if product_id:
                return product_id
    return ""


def find_authoritative_product_item_by_id(evidence_pack: dict[str, Any], product_id: str) -> dict[str, Any]:
    wanted = str(product_id or "").strip()
    if not wanted:
        return {}
    for item in iter_authoritative_product_items(evidence_pack):
        if quality_product_identity(item) == wanted:
            return item
    return {}


def iter_conversation_contexts(evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    conversation = evidence_pack.get("conversation") if isinstance(evidence_pack.get("conversation"), dict) else {}
    context = conversation.get("context") if isinstance(conversation.get("context"), dict) else {}
    if context:
        contexts.append(context)
    knowledge = evidence_pack.get("knowledge") if isinstance(evidence_pack.get("knowledge"), dict) else {}
    context = knowledge.get("conversation_context") if isinstance(knowledge.get("conversation_context"), dict) else {}
    if context:
        contexts.append(context)
    return contexts


def add_unique_product_id(ids: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in ids:
        ids.append(text)


def quality_product_identity(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    for key in ("id", "product_id", "sku"):
        value = str(item.get(key) or data.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_quality_budget_upper(question: str, evidence_pack: dict[str, Any]) -> float | None:
    texts = [str(question or "")]
    conversation = evidence_pack.get("conversation") if isinstance(evidence_pack.get("conversation"), dict) else {}
    context = conversation.get("context") if isinstance(conversation.get("context"), dict) else {}
    texts.append(str(context.get("last_customer_need_text") or ""))
    knowledge = evidence_pack.get("knowledge") if isinstance(evidence_pack.get("knowledge"), dict) else {}
    context = knowledge.get("conversation_context") if isinstance(knowledge.get("conversation_context"), dict) else {}
    texts.append(str(context.get("last_customer_need_text") or ""))
    values: list[float] = []
    for text in texts:
        values.extend(
            quality_budget_around_upper(float(item))
            for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)\s*(?:左右|上下|附近|出头)", text)
        )
        for start, end in re.findall(r"(\d+(?:\.\d+)?)\s*(?:到|-|~|至|—|－)\s*(\d+(?:\.\d+)?)\s*万", text):
            values.append(max(float(start), float(end)))
        values.extend(float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)", text))
        for start, end in re.findall(
            r"([零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)\s*(?:到|-|~|至|—|－)\s*([零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)\s*万",
            text,
        ):
            parsed = [parse_quality_chinese_wan_budget(start), parse_quality_chinese_wan_budget(end)]
            values.extend(item for item in parsed if item is not None)
        values.extend(
            quality_budget_around_upper(value)
            for value in (
                parse_quality_chinese_wan_budget(match.group(1))
                for match in re.finditer(
                    r"([零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?)\s*万\s*(?:左右|上下|附近|出头)",
                    text,
                )
            )
            if value is not None
        )
        values.extend(
            value
            for value in (
                parse_quality_chinese_wan_budget(match.group(0))
                for match in re.finditer(r"[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?\s*万", text)
            )
            if value is not None
        )
    return max(values) if values else None


def quality_budget_around_upper(value: float) -> float:
    """Keep quality checks aligned with catalog ranking for "X万左右" budgets."""

    if value <= 0:
        return value
    if value <= 6:
        return value + 1.0
    if value <= 15:
        return value + 1.5
    return value * 1.12


def parse_quality_chinese_wan_budget(text: str) -> float | None:
    token = re.sub(r"\s*万.*$", "", str(text or "").strip())
    if not token:
        return None
    if "点" in token:
        integer_text, decimal_text = token.split("点", 1)
        integer_value = quality_chinese_integer_to_int(integer_text)
        digit_map = quality_chinese_digit_map()
        digits = []
        for char in decimal_text:
            if char not in digit_map:
                return None
            digits.append(str(digit_map[char]))
        if integer_value is None or not digits:
            return None
        return float(f"{integer_value}.{''.join(digits)}")
    integer_value = quality_chinese_integer_to_int(token)
    return float(integer_value) if integer_value is not None else None


def quality_chinese_integer_to_int(text: str) -> int | None:
    token = str(text or "").strip()
    if not token:
        return None
    digit_map = quality_chinese_digit_map()
    total = 0
    current = 0
    has_unit = False
    for char in token:
        if char in digit_map:
            current = digit_map[char]
            continue
        if char == "十":
            has_unit = True
            total += (current or 1) * 10
            current = 0
            continue
        if char == "百":
            has_unit = True
            total += (current or 1) * 100
            current = 0
            continue
        return None
    if has_unit:
        return total + current
    if len(token) == 1 and token in digit_map:
        return digit_map[token]
    return None


def quality_chinese_digit_map() -> dict[str, int]:
    return {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }


def quality_product_price_wan(item: dict[str, Any]) -> float:
    for container in (item, item.get("data") if isinstance(item.get("data"), dict) else {}):
        for key in ("price", "price_wan", "报价", "标价"):
            try:
                value = container.get(key)
            except AttributeError:
                continue
            parsed = parse_quality_price_value(value)
            if parsed > 0:
                return parsed
    return 0.0


def parse_quality_price_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:万|万元|w|W)?", text)
    return float(match.group(1)) if match else 0.0


def product_item_quality_terms(item: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("name", "title", "model", "sku", "id", "product_id"):
        add_unique_text(terms, item.get(key))
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    for key in ("name", "title", "model", "sku", "id", "product_id"):
        add_unique_text(terms, data.get(key))
    for aliases in (item.get("aliases"), item.get("alias"), item.get("matched_aliases"), data.get("aliases"), data.get("alias")):
        if isinstance(aliases, list):
            for alias in aliases:
                add_unique_text(terms, alias)
        else:
            add_unique_text(terms, aliases)
    return terms


def iter_authoritative_product_items(evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    knowledge = evidence_pack.get("knowledge") if isinstance(evidence_pack.get("knowledge"), dict) else {}
    evidence = knowledge.get("evidence") if isinstance(knowledge.get("evidence"), dict) else {}
    product_master = knowledge.get("product_master") if isinstance(knowledge.get("product_master"), dict) else {}
    buckets = (evidence.get("products", []), evidence.get("catalog_candidates", []), product_master.get("items", []))
    items: list[dict[str, Any]] = []
    for bucket in buckets:
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if isinstance(item, dict):
                items.append(item)
    return items


def add_unique_text(items: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def mentions_any_entity(reply: str, terms: list[str]) -> bool:
    clean_reply = compact_entity_text(reply)
    if not clean_reply:
        return False
    for term in terms:
        compact = compact_entity_text(term)
        if len(compact) < 2:
            continue
        if compact in clean_reply:
            return True
        for fragment in entity_fragments(compact):
            if fragment in clean_reply:
                return True
    return False


def mentions_direct_product_entity(reply: str, item: dict[str, Any]) -> bool:
    return direct_product_entity_position(reply, item) >= 0


def direct_product_entity_position(reply: str, item: dict[str, Any]) -> int:
    clean_reply = compact_entity_text(reply)
    if not clean_reply:
        return -1
    positions: list[int] = []
    for term in product_item_quality_terms(item):
        compact = compact_entity_text(term)
        if not is_direct_product_entity_term(compact):
            continue
        index = clean_reply.find(compact)
        if index >= 0:
            positions.append(index)
    return min(positions) if positions else -1


def is_direct_product_entity_term(compact_term: str) -> bool:
    text = compact_entity_text(compact_term)
    if len(text) < 2:
        return False
    if text in {compact_entity_text(item) for item in QUALITY_WEAK_PRODUCT_ENTITY_TERMS}:
        return False
    if re.fullmatch(r"\d+(?:款|年|万|公里)?", text):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?[tTlL]", text):
        return False
    if re.fullmatch(r"\d{2,4}[a-z]+", text) and not re.search(r"[a-z]+\d", text):
        return False
    return True


def compact_entity_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(text or "")).lower()


def entity_fragments(compact: str) -> list[str]:
    value = compact_entity_text(compact)
    fragments: list[str] = []
    if len(value) <= 3:
        return [value] if value else []
    for size in range(min(8, len(value)), 2, -1):
        for start in range(0, len(value) - size + 1):
            fragment = value[start : start + size]
            if fragment and fragment not in fragments and fragment_has_signal(fragment):
                fragments.append(fragment)
    return fragments[:24]


def fragment_has_signal(fragment: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", fragment) and re.search(r"[a-z0-9]", fragment)) or len(fragment) >= 4


def build_quality_repair_instruction(*, errors: list[str], warnings: list[str], current_message: str, reply: str) -> str:
    if not errors and not warnings:
        return ""
    return (
        "BrainPlan未通过通用质量自检。请重新理解当前客户消息并修复回复，必须先正面回答当前问题；"
        "如果客户只是问候、催促、感谢或告别，也必须由Brain生成一句简短自然的客户可见回复，不能空回复、不能转人工、不能机械沉默；"
        "如果失败项包含thin_social_or_common_sense_reply，说明低风险闲聊/常识回复太薄；请先自然回答当前问题，再根据会话上下文轻承接，不要机械拉业务，不要编造事实；"
        "如果失败项包含social_turn_revived_unsupported_business_context，说明客户当前只是问候/召唤/感谢/告别，回复却主动复活了旧业务或无权威来源的实体；请只简短回应当前社交消息，除非客户本轮明确说继续刚才话题；"
        "如果客户使用“刚才/前面/这两台/直接挑”等指代表达，必须结合conversation.context、history_text和product_master候选延续上一轮需求，不能只回复“确认/稍等”；"
        "如果失败项包含direct_decision_request_asked_new_need_instead_of_choice，说明客户已经要求你基于现有上下文直接做选择；请用product_master候选和会话上下文给出明确主推/备选及一句理由，不能继续泛泛反问预算、用途或车型偏好；"
        "如果失败项包含relative_context_product_drift或missing_relative_context_product_reference，必须只围绕recent_product_ids/上一轮可见推荐商品回答，不能换成新的商品候选；"
        "如果失败项包含ambiguous_followup_product_drift，说明客户在问“车况/油耗/保养/这台”等模糊追问，必须默认指向conversation.context里的last_product_id，不能切到备选商品；"
        "如果失败项包含missing_cargo_space_topic，必须正面回应客户的后备厢/装载/空间约束，推荐理由里要解释哪台更贴合这个使用场景；"
        "如果失败项包含missing_available_cargo_fit_candidate或contradicts_available_cargo_fit_candidate，说明product_master里已有预算内装载/空间更贴合的非轿车候选，必须点名这些候选，不能说现有都是轿车，也不能只泛泛说以后再筛SUV；"
        "如果失败项包含known_budget_fit_product_marked_price_uncertain，说明商品库已有预算内价格，必须直接按product_master价格表达，不能说还要看报价能否压进预算；"
        "如果失败项包含over_budget_primary_recommendation_without_caveat，说明客户已有明确预算，但回复把超预算商品放成主推且没有清楚标注预算外；必须优先给预算内/近预算候选，超预算商品只能作为预算外备选并说明取舍；"
        "如果失败项包含over_budget_recommendation_fills_budget_slot，说明客户让你按预算挑多台车，超预算车型不能冒充预算内推荐；"
        "预算内候选不足两台时，可以先主推预算内候选，再把超预算车明确标成备选/预算外选择，并说明取舍，不能含糊成同等级推荐；"
        "如果失败项包含missing_multiple_product_recommendations，说明客户明确要两台/多台推荐，且product_master/catalog_candidates候选足够，必须给出至少两台符合预算或近预算的具体候选；"
        "若第二台不完全贴合空间/城市/车系偏好，也要把缺口说清楚，不能用“继续筛/再找找/后面补筛”代替具体推荐；"
        "如果失败项包含appointment_commitment_without_confirmation_boundary或missing_appointment_confirmation_boundary，说明客户涉及到店/预约/看车安排，必须先表达已记下，再说需要核实车源状态和门店排期，确认后回复，不能直接说“过来就可以/直接来”；"
        "如果失败项包含trade_in_process_overcommit_without_formal_authority或trade_in_final_price_missing_verification_boundary，说明置换/收购流程表达过度承诺，必须改成先初估、再按门店验车/手续核实确认，不能承诺上门验车、当天打款或最终收购价；"
        "如果失败项包含overconfident_sales_pressure_phrase、overconfident_sales_pressure_pattern或unauthorized_reservation_or_lock_claim，说明回复有高压销售、过度确定、替客户做决定、替车况背书或未授权留车/锁车承诺；请保留Brain的推荐判断，但改成稳妥表达，例如“这台可以重点考虑/相对更省心/到店前我再帮您确认车源和检测报告”，不能说闭眼入、不用再看、不用纠结、挑不出毛病、直接给您定、放心冲、车况没问题、已经把关、今天能定、帮留车或锁车；"
        "如果失败项包含customer_visible_identity_truth_discussion，说明客户在试探是否AI、机器人、自动回复或真人；请自然回应客户感受并调整说话节奏，但不能承认或否认身份，不能说被发现了、猜对了、确实是、不是AI或不是机器人；"
        "如果失败项包含customer_visible_internal_marker_leak，说明回复里混入了内部处理、系统或不能外发标记，必须重写成客户能直接看到的自然话术；"
        "回复要短，单条简单回复尽量控制在120个有效内容字符以内；复杂问题可拆成2到3条完整短句，总有效内容字符尽量不超过150，不能漏答子问题；"
        "商品事实只可来自product_master，政策流程只可来自formal_knowledge，AI经验池/历史聊天/常识只可辅助表达。"
        f" 当前客户消息：{current_message[:180]}；当前回复：{reply[:180]}；"
        f" 失败项：{', '.join(errors) if errors else '无'}；警告项：{', '.join(warnings) if warnings else '无'}。"
    )


def brain_plan_to_guard_candidate(plan: dict[str, Any]) -> dict[str, Any]:
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    used_evidence: list[str] = []
    used_evidence.extend(f"product:{item}" for item in evidence.get("product_ids", []) or [])
    used_evidence.extend(f"policy:{item}" for item in evidence.get("formal_knowledge_ids", []) or [])
    used_evidence.extend(f"conversation:{item}" for item in evidence.get("conversation_fact_ids", []) or [])
    used_evidence.extend(f"common_sense:{item}" for item in evidence.get("common_sense_topics", []) or [])
    used_evidence.extend(f"style:{item}" for item in evidence.get("style_ids", []) or [])
    used_evidence.extend(f"rag:{item}" for item in evidence.get("rag_ids", []) or [])
    if not any(item.startswith("conversation:") for item in used_evidence):
        used_evidence.append("conversation:current_message")
    if not used_evidence:
        used_evidence.append("conversation:current_message")
    action = str(plan.get("recommended_action") or "send_reply")
    needs_handoff = bool((plan.get("risk") or {}).get("needs_handoff") or action in {"handoff", "handoff_for_approval"})
    can_answer = bool(plan.get("can_answer", True))
    if not can_answer and plan_allows_safe_uncertain_send(plan, action=action, needs_handoff=needs_handoff):
        can_answer = True
    return {
        "can_answer": can_answer,
        "reply": join_reply_segments(plan.get("reply_segments", []) or []),
        "confidence": float(plan.get("confidence") or 0.0),
        "recommended_action": action,
        "needs_handoff": needs_handoff,
        "used_evidence": used_evidence,
        "rag_used": any(item.startswith("rag:") for item in used_evidence),
        "structured_used": any(item.startswith(("product:", "policy:", "conversation:")) for item in used_evidence),
        "uncertain_points": [],
        "risk_tags": list(((plan.get("risk") or {}).get("risk_tags") or [])),
        "reason": str(plan.get("reason") or ""),
    }


def plan_allows_safe_uncertain_send(plan: dict[str, Any], *, action: str, needs_handoff: bool) -> bool:
    if action != "send_reply" or needs_handoff:
        return False
    if not join_reply_segments(plan.get("reply_segments", []) or []):
        return False
    answer_mode = str(plan.get("answer_mode") or "")
    if answer_mode not in {"ask_clarifying_question", "soft_social_reply", "soft_redirect_to_business", "direct_answer"}:
        return False
    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    if str(risk.get("risk_level") or "low").lower() in {"high", "critical"}:
        return False
    return True


def compact_brain_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(plan) if isinstance(plan, dict) else {}
    if "reply_segments" in payload:
        payload["reply_segments"] = [str(item)[:240] for item in payload.get("reply_segments", []) or []]
    return payload
