"""Live-chat style adapter for customer-visible WeChat replies.

This module is intentionally conservative. It can make a reply sound closer to
the tenant's real service style, but it must not invent product facts or risky
business commitments.
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - supports both package and script imports.
    from style_memory_store import retrieve_style_examples, reply_similarity
except ImportError:  # pragma: no cover
    from .style_memory_store import retrieve_style_examples, reply_similarity

try:  # pragma: no cover
    from realtime_reply_router import choose_reply_variant, de_template_reply_text
except ImportError:  # pragma: no cover
    from .realtime_reply_router import choose_reply_variant, de_template_reply_text


DEFAULT_MAX_REPLY_CHARS = 520
SOURCE_SETTING_KEYS = {
    "realtime": "apply_to_realtime",
    "rag": "apply_to_rag",
    "llm": "apply_to_llm",
    "rule": "apply_to_rule",
    "handoff": "apply_to_handoff",
}
IDENTITY_PROBE_TERMS = ("是不是ai", "是不是AI", "ai吗", "AI吗", "机器人", "自动回复", "机器客服")
RECOMMEND_TERMS = ("推荐", "车源", "预算", "通勤", "家用", "省油", "看看", "哪台", "哪款", "挑")
PRICE_TERMS = ("价格", "报价", "优惠", "便宜", "贵", "低点", "少点", "预算", "最低", "底价", "贷款", "金融", "包过", "首付", "月供")
PRICE_ONLY_TERMS = ("价格", "报价", "优惠", "便宜", "贵", "低点", "少点", "预算", "最低", "底价")
FINANCE_TERMS = ("贷款", "金融", "包过", "首付", "月供", "征信", "资方", "审批", "利率")
CONDITION_TERMS = ("检测报告", "检测", "车况", "事故", "水泡", "火烧", "出险", "维保", "保养记录", "报告")
BUDGET_REASK_REJECT_TERMS = ("别再问预算", "不要再问预算", "不用再问预算", "别问预算", "按刚才说", "刚才说的", "前面说了", "上面说了")
HANDOFF_MARKERS = ("转人工", "人工客服", "真人客服", "同事联系", "专员联系", "销售联系")
AI_EXPOSURE_MARKERS = ("我是AI", "我是ai", "我是机器人", "智能客服", "自动回复", "机器客服")
CONTACT_DATA_TERMS = ("电话", "手机号", "联系方式", "我叫", "联系人", "姓名", "先生", "女士")
APPOINTMENT_TERMS = ("试驾", "到店", "看车", "订金", "定金", "留车", "预约", "周末", "周六", "周日", "上午", "下午", "几点", "安排", "过去", "来店")
SAME_DAY_DELIVERY_TERMS = ("办手续", "直接办", "当天办", "提车", "直接提", "当天提", "临牌", "过户", "交车")
SAME_DAY_DELIVERY_STRONG_TERMS = ("办手续", "直接办", "当天办", "当天", "提车", "直接提", "当天提", "临牌", "交车", "办完")
LOCATION_CONTACT_TERMS = ("门店地址", "店地址", "地址", "导航", "位置", "在哪", "哪里", "找谁", "联系人", "对接人", "到了找")
LOCATION_CONTACT_STRONG_TERMS = ("门店地址", "店地址", "地址", "导航", "位置", "在哪", "哪里", "找谁", "对接人", "到了找", "到店找", "跑错")
LOCATION_VISIT_CONTEXT_TERMS = ("门店地址", "店地址", "门店", "店里", "到店", "到了", "过去", "看车", "试驾", "来店", "导航")
TRADE_IN_TERMS = ("置换", "抵车款", "抵多少", "抵扣", "卖车", "收车", "旧车", "估价", "估个", "估一下")
NEW_ENERGY_TERMS = ("新能源", "电池", "三电", "续航", "充电", "混动", "dm-i", "dmi")
DOCUMENT_TERMS = ("合同", "发票", "开票", "抬头", "税号", "少开", "低开")
AFTER_SALES_TERMS = ("事故", "水泡", "火烧", "过户", "上牌", "赔偿", "退款", "投诉")


def adapt_reply_style(
    *,
    config: dict[str, Any],
    customer_message: str,
    reply_text: str,
    source_channel: str,
    evidence_pack: dict[str, Any] | None = None,
    recent_reply_texts: list[str] | None = None,
    needs_handoff: bool = False,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    settings = config.get("reply_style_adapter", {}) or {}
    payload: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", False)),
        "applied": False,
        "mode": str(settings.get("mode") or "fast_local"),
        "source_channel": source_channel,
    }
    if not payload["enabled"]:
        payload["reason"] = "reply_style_adapter_disabled"
        return payload
    if payload["mode"] not in {"fast_local", "shadow"}:
        payload["reason"] = "unsupported_style_adapter_mode"
        return payload
    if not source_channel_enabled(settings, source_channel):
        payload["reason"] = "source_channel_disabled"
        return payload

    prefix, body = split_reply_prefix(reply_text, config)
    if not body:
        payload["reason"] = "empty_reply"
        return payload

    identity_guard = identity_guard_enabled(config) if settings.get("identity_guard_aware", True) is not False else False
    recent_reply_texts = recent_reply_texts or []
    examples = retrieve_style_examples(
        customer_message,
        evidence_pack=evidence_pack,
        limit=int(settings.get("max_examples", 3) or 3),
        min_similarity=float(settings.get("min_similarity", 0.08) or 0.08),
        include_rag_style_hits=settings.get("include_rag_style_hits", True) is not False,
        tenant_id=tenant_id,
    )
    payload["style_examples"] = compact_examples_for_audit(examples)
    payload["style_source_ids"] = [str(item.get("id") or "") for item in examples if item.get("id")]

    adapted_body, strategy = apply_fast_local_style(
        customer_message=customer_message,
        base_reply=body,
        examples=examples,
        source_channel=source_channel,
        recent_reply_texts=recent_reply_texts,
        identity_guard=identity_guard,
        needs_handoff=needs_handoff,
    )
    if not adapted_body or adapted_body.strip() == body.strip():
        payload["reason"] = "no_safe_style_delta"
        return payload

    adapted_body = truncate_reply(adapted_body, int(settings.get("max_reply_chars", DEFAULT_MAX_REPLY_CHARS) or DEFAULT_MAX_REPLY_CHARS))
    guard = guard_adapted_reply(
        base_reply=body,
        adapted_reply=adapted_body,
        customer_message=customer_message,
        identity_guard=identity_guard,
        source_channel=source_channel,
    )
    payload["guard"] = guard
    if not guard.get("allowed"):
        payload["reason"] = str(guard.get("reason") or "style_guard_rejected")
        return payload

    final_reply = format_reply(adapted_body, prefix or configured_reply_prefix(config))
    payload.update(
        {
            "applied": payload["mode"] != "shadow",
            "reason": "shadow_mode" if payload["mode"] == "shadow" else "style_adapter_applied",
            "strategy": strategy,
            "raw_reply_text": adapted_body,
            "reply_text": final_reply,
            "candidate_reply_text": final_reply,
        }
    )
    return payload


def infer_source_channel(
    *,
    realtime_reply: dict[str, Any] | None = None,
    llm_synthesis: dict[str, Any] | None = None,
    llm_reply: dict[str, Any] | None = None,
    rag_reply: dict[str, Any] | None = None,
) -> str:
    if isinstance(realtime_reply, dict) and realtime_reply.get("applied"):
        return "realtime"
    if isinstance(llm_synthesis, dict) and llm_synthesis.get("applied"):
        return "llm"
    if isinstance(llm_reply, dict) and llm_reply.get("applied"):
        return "llm"
    if isinstance(rag_reply, dict) and rag_reply.get("applied"):
        return "rag"
    return "rule"


def source_channel_enabled(settings: dict[str, Any], source_channel: str) -> bool:
    key = SOURCE_SETTING_KEYS.get(str(source_channel or "rule"), "apply_to_rule")
    return settings.get(key, True) is not False


def apply_fast_local_style(
    *,
    customer_message: str,
    base_reply: str,
    examples: list[dict[str, Any]],
    source_channel: str,
    recent_reply_texts: list[str],
    identity_guard: bool,
    needs_handoff: bool,
) -> tuple[str, str]:
    context = f"{customer_message} {base_reply}".strip()
    if identity_guard and has_identity_probe(context):
        reply, _ = choose_reply_variant(
            [
                "这类内部信息不能外发，您别介意；您把具体需求说下，我按实际情况帮您核实。",
                "这类内容不方便提供。您要是担心回复不准，可以直接问具体车况、报价或手续，我继续帮您核实。",
                "内部规则这些不对外说，您别介意；您关心哪块，我按实际情况帮您确认。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )
        return reply, "identity_guard_boundary"
    if needs_handoff or source_channel == "handoff" or has_handoff_marker(base_reply):
        handoff_context = str(customer_message or "").strip()
        specific = handoff_specific_reply(handoff_context, recent_reply_texts)
        if specific:
            return de_template_reply_text(specific, key_text=handoff_context, recent_reply_texts=recent_reply_texts), "handoff_specific_soften"
        reply, _ = choose_reply_variant(
            [
                "您这个问题问得对，我这边不能随口定，免得给您说错。您稍等，我把情况核实清楚后回复您。",
                "这点我先按正式流程核实一下，避免给您说错。您稍等，我核实清楚后回复您。",
                "可以，我把您的问题记下，核实清楚再回复您，这样对您也更稳一点。",
            ],
            key_text=handoff_context or context,
            recent_reply_texts=recent_reply_texts,
        )
        return de_template_reply_text(reply, key_text=handoff_context or context, recent_reply_texts=recent_reply_texts), "handoff_soften"

    if not examples:
        return base_reply, "no_style_examples"

    adapted = normalize_formulaic_opening(base_reply)
    profile = infer_style_profile(examples)
    if profile.get("avoid_random_push") and is_recommendation_scene(customer_message):
        adapted = de_template_reply_text(adapted, key_text=context, recent_reply_texts=recent_reply_texts)
    honorific = str(profile.get("honorific") or "")
    if honorific and should_apply_honorific(adapted, recent_reply_texts, customer_message=customer_message):
        selected_honorific = select_style_honorific(honorific, customer_message, recent_reply_texts)
        if selected_honorific:
            adapted = f"{selected_honorific}，{adapted}"
    dialogue_context = " ".join([customer_message, *recent_reply_texts[-4:]])
    customer_has_budget = has_budget_signal(dialogue_context) or rejects_budget_reask(customer_message)
    customer_has_use = has_use_signal(dialogue_context)
    if (
        is_price_scene(customer_message)
        and "预算" in profile.get("keywords", set())
        and not asks_budget(adapted)
        and not customer_has_budget
    ):
        followup, _ = choose_reply_variant(
            [
                "您主要是预算卡住，还是想跟别的方案再对比一下？",
                "您心里大概想控制在哪个区间？我按这个范围帮您看更合适的。",
                "如果是价格这块有顾虑，您说下能接受的区间，我再帮您缩一轮。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )
        adapted = append_sentence(adapted, followup)
    if (
        is_recommendation_scene(customer_message)
        and not asks_use_or_budget(adapted)
        and not (customer_has_budget and customer_has_use)
        and not rejects_budget_reask(customer_message)
    ):
        followup, _ = choose_reply_variant(
            [
                "您把预算和主要用途发我，我再帮您缩到更合适的几项。",
                "您先说下预算、用途和有没有置换，我再按实际情况帮您筛。",
                "我按您的预算和用途看，尽量把范围缩准一点。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )
        adapted = append_sentence(adapted, followup)

    adapted = normalize_duplicate_fragments(de_template_reply_text(adapted, key_text=context, recent_reply_texts=recent_reply_texts))
    if too_similar_to_recent(adapted, recent_reply_texts):
        adapted = rotate_repetition(adapted, customer_message, recent_reply_texts)
    return adapted, "style_example_blend"


def infer_style_profile(examples: list[dict[str, Any]]) -> dict[str, Any]:
    replies = " ".join(str(item.get("service_reply") or "") for item in examples)
    honorific = ""
    for candidate in ("哥", "姐", "老板", "老师", "您"):
        if candidate in replies:
            honorific = candidate
            break
    keywords = {word for word in ("预算", "用途", "核实", "确认", "不乱推", "白跑", "车况") if word in replies}
    return {
        "honorific": honorific if honorific != "您" else "",
        "keywords": keywords,
        "avoid_random_push": "不乱推" in replies or "乱推" in replies,
    }


def is_same_day_delivery_context(text: str) -> bool:
    clean = normalize_text(text)
    if not clean:
        return False
    return contains_any(clean, SAME_DAY_DELIVERY_STRONG_TERMS)


def handoff_specific_reply(context: str, recent_reply_texts: list[str]) -> str:
    clean = normalize_text(context)
    if is_location_contact_context(clean):
        return choose_reply_variant(
            [
                "门店地址和到了找谁我先确认一下，确认好后发您，避免您导航错或到店没人对接。",
                "这个我给您核清楚再发，地址、导航和到店联系人都一起确认好，省得您白跑。",
                "可以，我先确认门店地址和到店对接人，核好后回您，您过去会更稳一点。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, CONTACT_DATA_TERMS) and contains_any(clean, APPOINTMENT_TERMS):
        if contains_any(clean, TRADE_IN_TERMS) or "开过去" in clean:
            return choose_reply_variant(
                [
                    "可以，联系方式、到店时间和旧车置换我都记下了。接下来确认车源状态、门店排期和看车安排，核好后回您。",
                    "好的，您的信息和到店时间我记下了，旧车置换也一起备注。我这边确认车源和门店排期，弄清楚后回您。",
                    "信息我记下了，旧车也按置换一起备注。这边确认车源还在不在、到店时间能不能排上，弄清楚后回您。",
                ],
                key_text=context,
                recent_reply_texts=recent_reply_texts,
            )[0]
        return choose_reply_variant(
            [
                "可以，您的信息和到店时间我记下了。我这边确认一下车源和门店排期，弄清楚后回您，尽量别让您白跑。",
                "可以，联系方式和到店时间我记下，接下来确认车源状态、门店排期和看车安排，核好后回您。",
                "信息我记下了，这边确认车源还在不在、到店时间能不能排上，弄清楚后回您。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, NEW_ENERGY_TERMS):
        usage_detail = new_energy_usage_detail(context)
        return choose_reply_variant(
            [
                f"您担心电池和三电很正常，{usage_detail}。这块不能只听一句口头保证，我先核实检测记录、电池状态和车况，请稍等，确认后再跟您说这台适不适合。",
                f"新能源最该看的就是电池、三电和检测记录。{usage_detail}，我先核清楚实际续航、检测报告和车况，请稍等，确认好再给您更稳的判断。",
                f"这个问题问得很关键。{usage_detail}，混动车要看电池状态、三电检测和实际用车强度，我先把这些核实清楚，请稍等，确认后再跟您说适不适合入手。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if is_same_day_delivery_context(clean):
        return choose_reply_variant(
            [
                "这个要看车源状态、手续资料、付款方式、过户和临牌安排，不能只凭一句话保证当天提。我先把这些环节核清楚，确认能不能当天办完再回复您。",
                "当天提车不是不能安排，但要先确认车况报告、合同资料、付款到账、过户和临牌这些节点。我先核实门店流程，确认稳了再跟您说。",
                "您想当天办完我理解，这个我先确认车源、手续资料和过户排期，能不能当天交车要核准后再给您准话，避免您白跑或等太久。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    has_finance_boundary = contains_any(clean, FINANCE_TERMS)
    has_condition_boundary = contains_any(clean, CONDITION_TERMS)
    if has_finance_boundary and has_condition_boundary:
        return choose_reply_variant(
            [
                "贷款要看资方审核，检测报告和车况记录我可以帮您核；事故、水泡、火烧这类不能先口头定死，最终以报告和门店确认为准。",
                "金融方案要按资方审核走，车况这块我会重点核检测报告、出险和维保记录；涉及事故水泡火烧，最终以报告为准。",
                "这两块要分开看：贷款看资方审核，车况看检测报告和实车记录。我先把报告边界和金融方案核清楚，再给您准话。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, DOCUMENT_TERMS):
        if contains_any(clean, ("少开", "低开", "金额")):
            return choose_reply_variant(
                [
                    "我理解您是想把流程提前问清楚，合同和发票金额这块必须按实际交易和门店流程走，不能随口答应调整。我先确认合同流程和开票要求，核清楚再回复您。",
                    "这个我先帮您问清楚，发票金额和合同信息要按实际交易来确认，不能直接口头定。您稍等，我问下领导后再给您明确说法。",
                    "这块确实要提前确认好，免得后面来回改。合同签署和开票金额都要按流程核实，您稍等一下，我确认清楚后回您。",
                ],
                key_text=context,
                recent_reply_texts=recent_reply_texts,
            )[0]
        return choose_reply_variant(
            [
                "可以，这块我帮您问清楚。合同和发票需要按门店流程来，我把开票抬头、税号和合同资料要求核清楚后回您，避免后面填错。",
                "这个提前问是对的，我帮您确认合同流程和开票资料要求后再回复您，省得后面资料来回补。",
                "合同和发票这块我先确认一下门店流程。您稍等，我核实好抬头、税号和需要准备的资料后回复您。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    has_price_boundary = contains_any(clean, PRICE_ONLY_TERMS)
    if has_finance_boundary and has_price_boundary:
        return choose_reply_variant(
            [
                "我理解您想把贷款和价格先问准，贷款要看资方审核，成交条件也要按门店流程核准；我先确认付款方案和可谈边界，再给您准话。",
                "我理解您想把价格和贷款一次问清楚，金融审批和最终成交条件不能先口头定死；我先按正式流程核准后再回复您。",
                "这类问题我会谨慎点，帮您争取可以，但贷款看资方、价格看正式核准；我先确认付款方案和可谈范围，再给您准话。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if has_finance_boundary:
        return choose_reply_variant(
            [
                "贷款要看资方审核，首付、月供和利率都得按具体车源和资料核算；我先确认金融方案后给您准话。",
                "金融这块不能先口头定死，主要看车型、首付比例、期数和资方审核。我先把方案核清楚再回复您。",
                "分期可以先算方向，但审批结果、利率和月供要看资方。我先确认付款方案，核好后跟您说准。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if has_price_boundary:
        return choose_reply_variant(
            [
                "价格和优惠我会帮您核，但超出公开规则的部分不能直接口头答应。我先按门店流程确认，再给您明确答复。",
                "价格我可以帮您确认和争取，但最低价或破例优惠不能直接口头保证。我核准后再回复您。",
                "这个我先按正式流程核一下，可谈范围和最终成交条件确认后，再给您准话。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if is_same_day_delivery_context(clean):
        return choose_reply_variant(
            [
                "这个要看车源状态、手续资料、付款方式、过户和临牌安排，不能只凭一句话保证当天提。我先把这些环节核清楚，确认能不能当天办完再回复您。",
                "当天提车不是不能安排，但要先确认车况报告、合同资料、付款到账、过户和临牌这些节点。我先核实门店流程，确认稳了再跟您说。",
                "您想当天办完我理解，这个我先确认车源、手续资料和过户排期，能不能当天交车要核准后再给您准话，避免您白跑或等太久。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, APPOINTMENT_TERMS):
        return choose_reply_variant(
            [
                "您这个安排我记下了，我这边确认排期，核实车源状态后回复您。",
                "可以，我把您想看的时间和方向记下，确认一下车源和门店排期再回复您。",
                "到店这块我记一下，再确认排期和车源状态，避免您白跑，确认好就回您。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, TRADE_IN_TERMS):
        return choose_reply_variant(
            [
                "可以先做个大概区间。旧车这边我先按车型、年份、公里数、上牌地和车况信息记录，您再补下配置、过户次数、保养和事故水泡火烧情况，估价会更准。",
                "置换流程没问题，我先按年份、公里数、上牌地和车况给您看大概区间。您再发下配置、过户次数、保养和事故水泡火烧情况，我核实行情后判断会更准。",
                "这台车信息已经有基础了，可以先估一版。您把配置版本、车况瑕疵、有没有出险水泡火烧，再加外观内饰照片发我，我按检测和行情给您看区间。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    if contains_any(clean, AFTER_SALES_TERMS):
        return choose_reply_variant(
            [
                "这类问题我需要先核实关键细节，再给您准确处理意见，请稍等我回复您。",
                "这个不能只凭一句话下结论，我核实情况和相关记录后，再给您准确说法。",
                "涉及承诺我会谨慎一点，先把检测和记录核实清楚，再给您明确回复。",
            ],
            key_text=context,
            recent_reply_texts=recent_reply_texts,
        )[0]
    return ""


def is_location_contact_context(text: str) -> bool:
    clean = normalize_text(text)
    return contains_any(clean, LOCATION_CONTACT_STRONG_TERMS) and contains_any(clean, LOCATION_VISIT_CONTEXT_TERMS)


def normalize_formulaic_opening(reply: str) -> str:
    text = str(reply or "").strip()
    replacements = (
        ("收到，我先记录一下。", "可以，我帮您看一下。"),
        ("收到，我先记录一下", "可以，我帮您看一下"),
        ("我先记录一下。", "我帮您看一下。"),
        ("我看到了客户资料", "资料我看到了"),
    )
    for old, new in replacements:
        if text.startswith(old):
            return new + text[len(old) :]
    return text


def add_leading_phrase(reply: str, phrase: str) -> str:
    text = str(reply or "").strip()
    if not text or phrase in text:
        return text
    text = re.sub(r"^(您好|你好|可以|好的|收到|没问题)[，,。 ]*", "", text).strip()
    return f"{phrase}，{text}"


def append_sentence(reply: str, sentence: str) -> str:
    text = str(reply or "").strip()
    add = str(sentence or "").strip()
    if not add or add in text:
        return text
    if text and not text.endswith(("。", "？", "！", "…")):
        text += "。"
    return f"{text}{add}"


def rotate_repetition(reply: str, customer_message: str, recent_reply_texts: list[str]) -> str:
    variants = [
        reply,
        reply.replace("您", "你", 1) if "您" in reply else reply,
        re.sub(r"^可以[，,]", "行，", reply),
        re.sub(r"^我先", "这边", reply),
    ]
    selected, _ = choose_reply_variant(variants, key_text=customer_message, recent_reply_texts=recent_reply_texts)
    return selected


def guard_adapted_reply(
    *,
    base_reply: str,
    adapted_reply: str,
    customer_message: str,
    identity_guard: bool,
    source_channel: str,
) -> dict[str, Any]:
    if not adapted_reply.strip():
        return {"allowed": False, "reason": "empty_adapted_reply"}
    base_numbers = protected_number_tokens(base_reply) | protected_number_tokens(customer_message)
    adapted_numbers = protected_number_tokens(adapted_reply)
    new_numbers = sorted(adapted_numbers - base_numbers)
    if new_numbers:
        return {"allowed": False, "reason": "new_protected_number_tokens", "new_numbers": new_numbers}
    if identity_guard and exposes_ai_identity(adapted_reply):
        return {"allowed": False, "reason": "identity_exposure_phrase"}
    if identity_guard and source_channel != "handoff" and has_handoff_marker(adapted_reply):
        return {"allowed": False, "reason": "explicit_handoff_phrase"}
    return {"allowed": True, "changed_facts": False}


def protected_number_tokens(text: str) -> set[str]:
    clean = str(text or "")
    tokens = set(re.findall(r"\d+(?:\.\d+)?\s*(?:万|元|块|公里|km|KM|年|天|%|折)", clean))
    tokens.update(re.findall(r"1[3-9]\d{9}", clean))
    tokens.update(re.findall(r"\b\d{4,}\b", clean))
    return {re.sub(r"\s+", "", item) for item in tokens if item}


def compact_examples_for_audit(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "source": item.get("source"),
            "source_type": item.get("source_type"),
            "score": item.get("score"),
            "service_reply": compact_text(str(item.get("service_reply") or ""), 120),
        }
        for item in examples[:3]
    ]


def split_reply_prefix(reply_text: str, config: dict[str, Any]) -> tuple[str, str]:
    clean = str(reply_text or "").strip()
    if not clean:
        return "", ""
    prefix = configured_reply_prefix(config)
    if prefix and clean.startswith(prefix):
        return prefix, clean[len(prefix) :].strip()
    return "", clean


def configured_reply_prefix(config: dict[str, Any]) -> str:
    return str((config.get("reply", {}) or {}).get("prefix") or "")


def format_reply(reply_text: str, prefix: str) -> str:
    body = str(reply_text or "").strip()
    if not prefix:
        return body
    return body if body.startswith(prefix) else f"{prefix}{body}"


def identity_guard_enabled(config: dict[str, Any]) -> bool:
    settings = config.get("llm_reply_synthesis", {}) or {}
    return settings.get("identity_guard_enabled", True) is not False


def has_identity_probe(text: str) -> bool:
    clean = normalize_text(text)
    return any(normalize_text(term) in clean for term in IDENTITY_PROBE_TERMS)


def has_handoff_marker(text: str) -> bool:
    clean = normalize_text(text)
    return any(normalize_text(term) in clean for term in HANDOFF_MARKERS)


def exposes_ai_identity(text: str) -> bool:
    clean = normalize_text(text)
    for term in AI_EXPOSURE_MARKERS:
        marker = normalize_text(term)
        if marker not in clean:
            continue
        if any(prefix + marker in clean for prefix in ("不是", "并不是", "不算", "没有")):
            continue
        return True
    return False


def is_recommendation_scene(text: str) -> bool:
    clean = normalize_text(text)
    return contains_any(clean, RECOMMEND_TERMS)


def is_price_scene(text: str) -> bool:
    clean = normalize_text(text)
    return contains_any(clean, PRICE_TERMS)


def asks_budget(text: str) -> bool:
    return "预算" in str(text or "") or "区间" in str(text or "")


def asks_use_or_budget(text: str) -> bool:
    clean = str(text or "")
    return any(term in clean for term in ("预算", "用途", "用车", "使用场景", "主要需求"))


def has_budget_signal(text: str) -> bool:
    clean = normalize_text(text)
    if "预算" in clean or re.search(r"\d+(?:\.\d+)?万", clean):
        return True
    return any(
        term in clean
        for term in (
            "七八万",
            "八九万",
            "十来万",
            "十万",
            "十一二万",
            "十二三万",
            "十三四万",
            "十四五万",
            "十五六万",
        )
    )


def rejects_budget_reask(text: str) -> bool:
    clean = normalize_text(text)
    return any(normalize_text(term) in clean for term in BUDGET_REASK_REJECT_TERMS)


def normalize_duplicate_fragments(text: str) -> str:
    clean = str(text or "")
    clean = re.sub(r"(后面)[，,。；;\s]*\1", r"\1", clean)
    clean = re.sub(r"(前面)[，,。；;\s]*\1", r"\1", clean)
    clean = re.sub(r"(确认)[，,。；;\s]*\1", r"\1", clean)
    clean = re.sub(r"(核实)[，,。；;\s]*\1", r"\1", clean)
    return clean


def has_use_signal(text: str) -> bool:
    clean = normalize_text(text)
    return any(
        term in clean
        for term in (
            "通勤",
            "代步",
            "家用",
            "接娃",
            "买菜",
            "跑高速",
            "后排",
            "空间",
            "爸妈",
            "父母",
            "练手",
            "新手",
            "老婆",
            "媳妇",
            "女士",
            "停车",
            "自动挡",
            "倒车",
            "影像",
            "雷达",
        )
    )


STYLE_NEW_TOPIC_TERMS = (
    "另外",
    "还有",
    "再问",
    "再咨询",
    "换个",
    "顺便",
    "贷款",
    "保险",
    "过户",
    "合同",
    "发票",
    "置换",
    "试驾",
    "到店",
)
STYLE_FOLLOWUP_LIKE_OPENERS = (
    "那",
    "如果",
    "这",
    "这个",
    "这台",
    "刚才",
    "还有",
    "我还有",
    "最后",
    "行",
    "你先",
    "你别",
    "要是",
)
STYLE_INITIAL_LEAD_TERMS = (
    "你好",
    "您好",
    "在吗",
    "有人吗",
    "我想",
    "想买",
    "买台",
    "买辆",
    "帮我看",
    "推荐",
    "预算",
    "家用",
    "通勤",
    "练手",
)


def should_apply_honorific(
    reply: str,
    recent_reply_texts: list[str],
    *,
    customer_message: str = "",
) -> bool:
    if re.match(r"^([\u4e00-\u9fff]{0,2}(哥|姐)|哥|姐|老板|老师)[，,]", str(reply or "").strip()):
        return False
    if not recent_reply_texts:
        return looks_like_initial_honorific_context(customer_message)
    recent = " ".join(recent_reply_texts[-4:])
    if re.search(r"([\u4e00-\u9fff]{0,2}(哥|姐)|哥|姐|老板|老师)[，,]", recent):
        return False
    return any(term in re.sub(r"\s+", "", str(customer_message or "")) for term in STYLE_NEW_TOPIC_TERMS)


def looks_like_initial_honorific_context(customer_message: str) -> bool:
    clean = re.sub(r"\s+", "", str(customer_message or ""))
    if not clean:
        return False
    if any(clean.startswith(term) for term in STYLE_FOLLOWUP_LIKE_OPENERS):
        return False
    return any(term in clean for term in STYLE_INITIAL_LEAD_TERMS)


def select_style_honorific(base_honorific: str, customer_message: str, recent_reply_texts: list[str]) -> str:
    base = str(base_honorific or "").strip()
    if not base:
        return ""
    if not recent_reply_texts:
        return base
    if base == "哥":
        candidates = ["哥", "老板"]
    elif base == "老板":
        candidates = ["老板", "哥"]
    elif base == "姐":
        candidates = ["姐"]
    else:
        candidates = [base]
    selected, _ = choose_reply_variant(
        candidates,
        key_text=f"{customer_message}|style-honorific",
        recent_reply_texts=recent_reply_texts,
    )
    return selected


def too_similar_to_recent(reply: str, recent_reply_texts: list[str]) -> bool:
    return any(reply_similarity(reply, recent) >= 0.72 for recent in recent_reply_texts[-3:])


def truncate_reply(text: str, max_chars: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= max_chars:
        return clean
    return truncate_visible_reply(clean, max_chars)


def compact_text(text: str, max_chars: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max(1, max_chars - 1)].rstrip("，,。；; ") + "…"


def truncate_visible_reply(text: str, max_chars: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if max_chars <= 1:
        return clean[:max_chars]
    if len(clean) <= max_chars:
        return clean
    preferred = -1
    for marker in ("。", "！", "？", "!", "?", "；", ";", "，", ","):
        index = clean.rfind(marker, 0, max_chars)
        if index > preferred:
            preferred = index
    if preferred >= 0 and preferred + 1 >= max(12, int(max_chars * 0.45)):
        candidate = clean[: preferred + 1].strip()
    else:
        candidate = clean[: max(1, max_chars - 1)].strip().rstrip("，,；;、:：")
        if candidate and not candidate.endswith(("。", "！", "？", ".", "!", "?")):
            candidate = candidate[: max(1, max_chars - 1)].rstrip("，,；;、:：") + "。"
    if candidate.endswith(("，", ",", "；", ";", "、", ":", "：")):
        candidate = candidate.rstrip("，,；;、:：")
        if candidate and not candidate.endswith(("。", "！", "？", ".", "!", "?")):
            candidate = candidate[: max(1, max_chars - 1)].rstrip("，,；;、:：") + "。"
    return candidate[:max_chars].strip()


def new_energy_usage_detail(context: str) -> str:
    clean = re.sub(r"\s+", "", str(context or ""))
    if not clean:
        return "您这个顾虑我先记下"
    patterns = (
        r"(?:一天|每天|平时|通勤|上下班|来回|单程).{0,12}?(\d{1,3})\s*(?:公里|km|KM)",
        r"(\d{1,3})\s*(?:公里|km|KM).{0,12}?(?:一天|每天|平时|通勤|上下班|来回|单程)",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, re.I)
        if match:
            return f"您说的{match.group(1)}公里这个用车强度我记下了"
    if any(term in clean for term in ("家用", "通勤", "上下班", "代步", "跑高速", "老婆", "孩子")):
        return "您的用车场景我先记下"
    return "您这个顾虑我先记下"


def normalize_text(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def contains_any(normalized_text: str, terms: tuple[str, ...]) -> bool:
    return any(normalize_text(term) in normalized_text for term in terms if term)
