"""Constrained common-sense guidance for LLM reply synthesis.

This layer is deliberately non-authoritative.  It helps the model give clear
human advice for comparison/choice questions, but it must never invent product
facts that belong to the product master or formal knowledge base.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


COMMON_SENSE_LAYER = "llm_common_sense"
FORBIDDEN_FACT_TYPES = [
    "price",
    "stock",
    "inventory",
    "mileage",
    "condition_claim",
    "availability",
    "inspection_result",
    "store_status",
    "approval_or_discount_commitment",
]


@dataclass
class CommonSenseGuidance:
    layer: str = COMMON_SENSE_LAYER
    non_authoritative: bool = True
    allowed_use: list[str] = field(default_factory=list)
    forbidden_fact_types: list[str] = field(default_factory=lambda: list(FORBIDDEN_FACT_TYPES))
    guidance_points: list[str] = field(default_factory=list)
    response_style: list[str] = field(default_factory=list)

    def to_prompt_fragment(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["must_defer_to"] = ["product_master", "product_scoped_formal", "formal_knowledge"]
        payload["conflict_rule"] = "若本层与商品库或正式知识冲突，必须忽略本层。"
        return payload


def build_common_sense_guidance(
    *,
    customer_message: str,
    conversation_context: dict[str, Any] | None = None,
    product_vocabulary: dict[str, Any] | None = None,
) -> CommonSenseGuidance:
    text = normalize_text(customer_message)
    context = normalize_text(" ".join(str(value) for value in (conversation_context or {}).values()))
    combined = text + " " + context
    guidance = CommonSenseGuidance(
        allowed_use=[
            "理解客户场景、预算、使用人和偏好",
            "对不触碰承诺边界的方案比较给出明确倾向",
            "把回复压缩成微信真人客服口吻",
        ],
        response_style=[
            "先给明确结论，再给一个简短理由",
            "不要堆库存清单；客户要具体车源时才引用商品库候选",
            "一句话能说清就不扩写，必要时拆成多条短句",
        ],
    )
    points: list[str] = []
    if any(term in combined for term in ("怎么选", "哪个", "先看", "更适合", "推荐", "优先")):
        points.append("比较型问题要给清楚排序；不要把“都可以/看情况”当主体答案。")
    if any(term in combined for term in ("预算", "万", "价格", "便宜", "贵", "月供")):
        points.append("预算贴合度是推荐排序的重要约束；明显偏离预算的选项不能排第一。")
    if any(term in combined for term in ("省油", "油耗", "通勤", "上下班", "代步")):
        points.append("通勤/省油场景通常优先考虑油耗、维护成本、停车便利和车况透明度。")
    if any(term in combined for term in ("孩子", "老人", "全家", "二胎", "后排", "舒适", "高速")):
        points.append("家庭/长途场景通常更看重后排舒适、空间、稳定性和维保成本。")
    if any(term in combined for term in ("公司", "接客户", "商务", "物料", "后备", "器材", "装")):
        points.append("商务/装载场景通常要平衡空间、接待观感、后备厢实用性和后期成本。")
    if any(term in combined for term in ("新手", "老婆", "媳妇", "女士", "停车", "倒车")):
        points.append("新手/停车敏感场景通常优先车身尺寸、视野、倒车影像/雷达和试驾体感。")
    if any(term in combined for term in ("车况", "事故", "水泡", "火烧", "检测", "报告", "透明")):
        points.append("车况透明诉求应引导看检测报告、保养/出险记录和实车复核，不做未核实保证。")
    if not points:
        points.append("只在客户问题需要取舍分析时使用本层；普通事实问题优先按商品库和正式知识回答。")
    guidance.guidance_points = points[:6]
    return guidance


def common_sense_prompt_fragment(guidance: CommonSenseGuidance | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(guidance, CommonSenseGuidance):
        return guidance.to_prompt_fragment()
    if isinstance(guidance, dict):
        return guidance
    return build_common_sense_guidance(customer_message="").to_prompt_fragment()


def normalize_text(text: str) -> str:
    return str(text or "").strip().lower()
