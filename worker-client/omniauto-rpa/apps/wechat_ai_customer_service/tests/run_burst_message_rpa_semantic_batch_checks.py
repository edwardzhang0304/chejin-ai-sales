"""Simulated checks for burst-message history backfill and semantic planning.

These checks do not touch live WeChat. They verify that the workflow uses the
RPA history-loading boundary and that consecutive messages are semantically
grouped before reply synthesis.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from listen_and_reply import (  # noqa: E402
    bootstrap_target,
    detect_newer_messages_before_send,
    maybe_enrich_messages_with_history,
    message_content_dedupe_key,
    message_processed_content_keys,
    plan_message_batch_semantics,
    process_target,
    select_batch_details,
)
from customer_service_loop import ReplyDecision  # noqa: E402
from rag_answer_layer import maybe_build_rag_reply  # noqa: E402
import realtime_reply_router as realtime_router  # noqa: E402
from realtime_reply_router import (  # noqa: E402
    decide_realtime_reply_route,
    extract_visit_time_label,
    extract_budget_wan,
    has_strict_budget_cap,
    is_appointment_context_query,
    maybe_build_realtime_reply,
    requires_high_risk_boundary,
    score_product_items,
)
from wechat_connector import _args_to_request  # noqa: E402


class FakeConnector:
    def __init__(self, visible: list[dict[str, Any]], loaded: list[dict[str, Any]] | None = None) -> None:
        self.visible = visible
        self.loaded = loaded
        self.history_load_calls: list[int] = []
        self.sent_texts: list[str] = []

    def get_messages(self, target: str, exact: bool = True, history_load_times: int = 0) -> dict[str, Any]:
        if history_load_times:
            self.history_load_calls.append(history_load_times)
        messages = self.loaded if history_load_times and self.loaded is not None else self.visible
        return {
            "ok": True,
            "target": target,
            "exact": exact,
            "history_load": {"mechanism": "rpa.history_load", "requested_load_times": history_load_times},
            "messages": messages,
        }

    def send_text_and_verify(self, target: str, text: str, exact: bool = True, *, skip_send_rate_guard: bool = False) -> dict[str, Any]:
        self.sent_texts.append(text)
        return {"ok": True, "verified": True, "target": target, "text": text, "exact": exact}


def main() -> int:
    results = []
    for check in (
        check_sidecar_request_contract,
        check_split_need_groups_as_single_event,
        check_same_scene_multi_question_is_grouped,
        check_mixed_risk_is_flagged,
        check_spam_or_noise_is_not_answered_line_by_line,
        check_context_sensitive_boundary_terms,
        check_new_driver_parking_ranking_prefers_compact_cars,
        check_outdoor_suv_ranking_prefers_suvs,
        check_family_space_ranking_prefers_roomy_cars,
        check_retired_family_first_message_routes_to_candidates,
        check_greeting_budget_need_overrides_generic_handoff,
        check_soft_missing_evidence_price_fallback_does_not_block_candidates,
        check_visit_time_reply_preserves_specific_time,
        check_weekend_usage_is_not_appointment_intent,
        check_common_followup_guidance_stays_local,
        check_internal_rag_experience_not_pasted_to_customer,
        check_history_backfill_merges_loaded_messages,
        check_history_backfill_recovers_missing_anchor_without_gap,
        check_history_backfill_unresolved_anchor_uses_overflow_batch,
        check_customer_service_overflow_batch_does_not_block_reply,
        check_processed_fragment_keys_suppress_ocr_splits,
        check_file_transfer_self_reply_is_not_reprocessed,
        check_file_transfer_self_reply_ocr_punctuation_variant_is_not_reprocessed,
        check_bootstrap_marks_latest_visible_before_history_scroll,
        check_freshness_backfill_finds_original_batch,
        check_freshness_backfill_stale_when_original_not_found,
        check_freshness_missing_original_without_visible_newer_flags_gap,
    ):
        try:
            results.append({"name": check.__name__, "ok": True, "details": check()})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def check_sidecar_request_contract() -> dict[str, Any]:
    payload = _args_to_request(["messages", "--target", "许聪", "--exact", "--history-load-times", "4"])
    assert_equal(payload.get("action"), "messages", "action")
    assert_equal(payload.get("target"), "许聪", "target")
    assert_true(payload.get("exact") is True, "exact")
    assert_equal(payload.get("history_load_times"), 4, "history load times")
    return payload


def check_split_need_groups_as_single_event() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages("想买个家用车", "十万左右", "省油点", "最好自动挡"),
        {"semantic_batch_planner": {"enabled": True}},
    )
    assert_equal(plan.get("kind"), "single_event", "kind")
    return compact_plan(plan)


def check_same_scene_multi_question_is_grouped() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages("有十万左右的车吗", "能不能分期", "周末可以看车吗"),
        {"semantic_batch_planner": {"enabled": True}},
    )
    assert_equal(plan.get("kind"), "multi_question_same_scene", "kind")
    return compact_plan(plan)


def check_mixed_risk_is_flagged() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages("有没有省油代步车", "合同和发票怎么开", "周末能看吗"),
        {"semantic_batch_planner": {"enabled": True}},
    )
    assert_equal(plan.get("kind"), "multi_question_mixed_risk", "kind")
    assert_equal(plan.get("risk_level"), "boundary", "risk")
    return compact_plan(plan)


def check_spam_or_noise_is_not_answered_line_by_line() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages("在吗", "在吗", "在吗", "在吗", "？？", "？？", "在吗", "在吗", "看见没"),
        {"semantic_batch_planner": {"enabled": True, "spam_repeat_threshold": 0.72}},
    )
    assert_equal(plan.get("kind"), "spam_or_noise", "kind")
    return compact_plan(plan)


def check_context_sensitive_boundary_terms() -> dict[str, Any]:
    safe_cases = [
        "我看秦PLUS DM-i，平时每天来回40公里，三电检测一般怎么看？",
        "无事故水泡火烧，平时上下班开，想抵一台十万出头的SUV。",
    ]
    risky_cases = [
        "你能不能保证电池和三电后面肯定没问题？",
        "合同和发票能不能按低一点开？",
        "我征信一般，贷款你能不能保证包过？",
    ]
    assert_true(not any(requires_high_risk_boundary(text) for text in safe_cases), "plain condition/detection questions should stay answerable")
    assert_true(all(requires_high_risk_boundary(text) for text in risky_cases), "commitment/payment/invoice boundaries should still stop")
    return {"safe_cases": safe_cases, "risky_cases": risky_cases}


def check_new_driver_parking_ranking_prefers_compact_cars() -> dict[str, Any]:
    query = "12万以内，老婆开，好停省心，周末看车，推荐两台"
    items = [
        {
            "id": "golf",
            "name": "2020款大众高尔夫",
            "category": "二手车/紧凑型轿车",
            "price": 8.28,
            "stock": 1,
            "specs": "女士一手车，自动挡，倒车影像，省油好开，适合新手城市代步。",
        },
        {
            "id": "mazda3",
            "name": "2020款马自达3昂克赛拉",
            "category": "二手车/紧凑型轿车",
            "price": 9.58,
            "stock": 1,
            "specs": "自动挡，一手车，公里数少，适合年轻家庭通勤。",
        },
        {
            "id": "h6",
            "name": "2020款哈弗H6",
            "category": "二手车/SUV",
            "price": 7.28,
            "stock": 1,
            "specs": "SUV，空间大，360影像，适合预算有限但需要大空间的客户。",
        },
    ]
    scored: list[tuple[float, dict[str, Any]]] = []
    score_product_items(
        query=query,
        items=items,
        budget=extract_budget_wan(query),
        strict_budget_cap=has_strict_budget_cap(query),
        used_car_query=True,
        requested_semantic_tags=set(),
        scored=scored,
        allow_broad_fallback=True,
    )
    scored.sort(key=lambda pair: (pair[0], float(pair[1].get("price") or 0)), reverse=True)
    top_ids = [str(item.get("id") or "") for _, item in scored[:2]]
    assert_equal(top_ids, ["golf", "mazda3"], "new-driver compact top two")
    return {"top_ids": top_ids, "scores": [(round(score, 3), item.get("id")) for score, item in scored]}


def check_outdoor_suv_ranking_prefers_suvs() -> dict[str, Any]:
    query = "13万以内，周末露营钓鱼，后备箱放装备，底盘高一点，推荐两台SUV"
    items = [
        {
            "id": "xtrail",
            "name": "2020款日产奇骏",
            "category": "二手车/SUV",
            "price": 11.5,
            "stock": 1,
            "specs": "SUV，2.5L四驱，空间大，后备箱实用，适合家庭长途。",
        },
        {
            "id": "h6",
            "name": "2020款哈弗H6",
            "category": "二手车/SUV",
            "price": 7.28,
            "stock": 1,
            "specs": "SUV，2.0T自动挡，空间大，底盘通过性较好。",
        },
        {
            "id": "mazda3",
            "name": "2020款马自达3昂克赛拉",
            "category": "二手车/紧凑型轿车",
            "price": 9.58,
            "stock": 1,
            "specs": "自动挡，一手车，公里数少，适合年轻家庭通勤。",
        },
    ]
    scored: list[tuple[float, dict[str, Any]]] = []
    score_product_items(
        query=query,
        items=items,
        budget=extract_budget_wan(query),
        strict_budget_cap=has_strict_budget_cap(query),
        used_car_query=True,
        requested_semantic_tags=set(),
        scored=scored,
        allow_broad_fallback=True,
    )
    scored.sort(key=lambda pair: (pair[0], float(pair[1].get("price") or 0)), reverse=True)
    top_ids = [str(item.get("id") or "") for _, item in scored[:2]]
    assert_equal(top_ids, ["xtrail", "h6"], "outdoor SUV top two")
    return {"top_ids": top_ids, "scores": [(round(score, 3), item.get("id")) for score, item in scored]}


def check_family_space_ranking_prefers_roomy_cars() -> dict[str, Any]:
    query = "13万以内，家里刚添二胎，老人孩子都坐，偶尔全家回老家，空间别憋屈"
    items = [
        {
            "id": "xtrail",
            "name": "2020款日产奇骏",
            "category": "二手车/SUV",
            "price": 11.5,
            "stock": 1,
            "specs": "SUV，2.5L，后排空间大，后备箱实用，适合家庭长途。",
        },
        {
            "id": "camry",
            "name": "2021款丰田凯美瑞",
            "category": "二手车/中型轿车",
            "price": 12.8,
            "stock": 1,
            "specs": "中型轿车，后排舒适，一手车，保养记录完整。",
        },
        {
            "id": "golf",
            "name": "2020款大众高尔夫",
            "category": "二手车/两厢轿车",
            "price": 8.28,
            "stock": 1,
            "specs": "两厢小车，好开好停，适合练手通勤。",
        },
    ]
    scored: list[tuple[float, dict[str, Any]]] = []
    score_product_items(
        query=query,
        items=items,
        budget=extract_budget_wan(query),
        strict_budget_cap=has_strict_budget_cap(query),
        used_car_query=True,
        requested_semantic_tags=set(),
        scored=scored,
        allow_broad_fallback=True,
    )
    scored.sort(key=lambda pair: (pair[0], float(pair[1].get("price") or 0)), reverse=True)
    top_ids = [str(item.get("id") or "") for _, item in scored[:2]]
    assert_equal(set(top_ids), {"camry", "xtrail"}, "family roomy top two")
    return {"top_ids": top_ids, "scores": [(round(score, 3), item.get("id")) for score, item in scored]}


def check_retired_family_first_message_routes_to_candidates() -> dict[str, Any]:
    text = "我爸快退休了，想给他换台14万以内的二手车，平时他开，周末带孙子出去，坐着舒服点，别太费心。"
    route = decide_realtime_reply_route(
        config={"realtime_reply": {"enabled": True}},
        combined=text,
        decision=ReplyDecision(reply_text="", rule_name=None, matched=False, need_handoff=False, reason=""),
        intent_result=SimpleNamespace(intent=""),
        intent_assist={"evidence": {"safety": {"must_handoff": False}}},
        rag_reply={},
        llm_reply={},
        product_knowledge=None,
        data_capture={},
        evidence_pack={},
        recent_reply_texts=[],
    )
    assert_equal(route.get("reason"), "detailed_vehicle_need_ready_for_candidates", "retired family first message route")
    return {"route": route.get("reason")}


def check_greeting_budget_need_overrides_generic_handoff() -> dict[str, Any]:
    text = "你好，我预算12到15万，想买省心家用二手车，主要上下班和接娃，南京能看车吗？"
    route = decide_realtime_reply_route(
        config={"realtime_reply": {"enabled": True}},
        combined=text,
        decision=ReplyDecision(
            reply_text="价格我肯定帮您争取，但最低价或破例优惠不能直接口头保证。我核实一下商品、数量和负责人意见，再回复您。",
            rule_name=None,
            matched=False,
            need_handoff=True,
            reason="evidence_safety:no_relevant_business_evidence",
        ),
        intent_result=SimpleNamespace(intent="product_inquiry"),
        intent_assist={"evidence": {"safety": {"must_handoff": True, "reasons": ["no_relevant_business_evidence"]}}},
        rag_reply={},
        llm_reply={},
        product_knowledge=None,
        data_capture={},
        evidence_pack={},
        recent_reply_texts=[],
    )
    reply = maybe_build_realtime_reply(
        config={"realtime_reply": {"enabled": True}},
        route=route,
        combined=text,
        evidence_pack={
            "evidence": {
                "products": [
                    {
                        "id": "camry",
                        "name": "2021款丰田凯美瑞2.0G豪华版",
                        "category": "二手车/中型轿车",
                        "aliases": ["凯美瑞", "丰田", "家用", "省心"],
                        "specs": "2021年上牌，表显4.8万公里，2.0L自动挡",
                        "price": 13.8,
                        "stock": 1,
                    },
                    {
                        "id": "xtrail",
                        "name": "2020款日产奇骏2.5L四驱",
                        "category": "二手车/SUV",
                        "aliases": ["奇骏", "SUV", "家用", "空间"],
                        "specs": "2020年上牌，表显5.9万公里，自动挡",
                        "price": 14.6,
                        "stock": 1,
                    },
                ]
            }
        },
        current_reply_text="好的，收到。我先帮您核实下南京这边是否方便安排看车，并同步确认相关情况；具体价格、库存及审批结果都以最终核实为准，确认清楚后我再回复您。",
        recent_reply_texts=[],
    )
    assert_equal(route.get("reason"), "detailed_vehicle_need_ready_for_candidates", "mixed greeting budget route")
    assert_true(bool(reply.get("applied")), f"candidate reply should override generic no-evidence handoff: {reply}")
    assert_true(bool(reply.get("used_product_ids")), f"candidate reply should include product ids: {reply}")
    return {"route": route.get("reason"), "reply_rule": reply.get("rule_name"), "used_product_ids": reply.get("used_product_ids")}


def check_soft_missing_evidence_price_fallback_does_not_block_candidates() -> dict[str, Any]:
    text = "你好，我预算12到15万，想买省心家用二手车，主要上下班和接娃，南京能看车吗？"
    route = decide_realtime_reply_route(
        config={"realtime_reply": {"enabled": True}},
        combined=text,
        decision=ReplyDecision(
            reply_text="价格和优惠这块我先帮您核实一下，但超出公开规则的部分我这边不能直接口头确认。等我把库存数量和负责人的意见确认清楚后，再给您一个明确答复。",
            rule_name=None,
            matched=False,
            need_handoff=True,
            reason="evidence_safety:no_relevant_business_evidence",
        ),
        intent_result=SimpleNamespace(intent="product_inquiry"),
        intent_assist={"evidence": {"safety": {"must_handoff": True, "reasons": ["no_relevant_business_evidence"]}}},
        rag_reply={},
        llm_reply={},
        product_knowledge=None,
        data_capture={},
        evidence_pack={"evidence": {"products": []}},
        recent_reply_texts=[],
    )
    old_loader = realtime_router.load_catalog_product_candidates
    realtime_router.load_catalog_product_candidates = lambda: [
        {
            "id": "chejin_camry_2021_20g",
            "name": "2021款丰田凯美瑞2.0G豪华版",
            "category": "二手车/中级轿车",
            "aliases": ["凯美瑞", "家用", "省心"],
            "spec": "2021年上牌，表显4.8万公里，2.0L自动挡，南京现车。",
            "price": 13.8,
            "stock": 1,
            "recommendation": "适合日常家用、上下班和接娃。",
        },
        {
            "id": "chejin_xtrail_2020_25l",
            "name": "2020款日产奇骏2.5L四驱",
            "category": "二手车/SUV",
            "aliases": ["奇骏", "SUV", "空间"],
            "spec": "2020年上牌，表显5.9万公里，自动挡，南京现车。",
            "price": 14.6,
            "stock": 1,
            "recommendation": "适合空间和家庭出行。",
        },
    ]
    try:
        reply = maybe_build_realtime_reply(
            config={"realtime_reply": {"enabled": True}},
            route=route,
            combined=text,
            evidence_pack={"evidence": {"products": []}},
            current_reply_text="价格和优惠这块我先帮您核实一下，但超出公开规则的部分我这边不能直接口头确认。等我把库存数量和负责人的意见确认清楚后，再给您一个明确答复。",
            recent_reply_texts=[],
        )
    finally:
        realtime_router.load_catalog_product_candidates = old_loader
    assert_true(route.get("soft_missing_evidence") is True, f"route should preserve soft missing evidence flag: {route}")
    assert_equal(route.get("reason"), "detailed_vehicle_need_ready_for_candidates", "budget scene route")
    assert_true(bool(reply.get("applied")), f"soft no-evidence fallback should not block candidates: {reply}")
    assert_true(bool(reply.get("used_product_ids")), f"catalog fallback should provide candidates: {reply}")
    return {"route": route.get("reason"), "used_product_ids": reply.get("used_product_ids")}


def check_visit_time_reply_preserves_specific_time() -> dict[str, Any]:
    text = "这周六上午十一点能看吗？最好别让我到了车不在。"
    assert_equal(extract_visit_time_label(text), "周六上午十一点", "visit time label")
    route = decide_realtime_reply_route(
        config={"realtime_reply": {"enabled": True}},
        combined=text,
        decision=ReplyDecision(reply_text="", rule_name=None, matched=False, need_handoff=False, reason=""),
        intent_result=SimpleNamespace(intent=""),
        intent_assist={"evidence": {"safety": {"must_handoff": False}}},
        rag_reply={},
        llm_reply={},
        product_knowledge=None,
        data_capture={},
        evidence_pack={},
        recent_reply_texts=[],
    )
    reply = maybe_build_realtime_reply(
        config={"realtime_reply": {"enabled": True}},
        route=route,
        combined=text,
        evidence_pack={},
        current_reply_text="",
        recent_reply_texts=[],
    )
    reply_text = str(reply.get("reply_text") or "")
    assert_true("周六上午十一点" in reply_text, reply_text)
    assert_true("几点" not in reply_text, reply_text)
    return {"route": route.get("reason"), "reply_text": reply_text}


def check_weekend_usage_is_not_appointment_intent() -> dict[str, Any]:
    assert_true(not is_appointment_context_query("我周末经常带孩子去郊外露营钓鱼，想买台二手SUV"), "weekend usage should not be treated as appointment")
    assert_true(is_appointment_context_query("周六下午三点到店看车，先确认车还在不在"), "actual visit timing should stay appointment")
    return {"ok": True}


def check_common_followup_guidance_stays_local() -> dict[str, Any]:
    cases = [
        (
            "如果七座也考虑，MPV会不会太大？市区停车和油耗压力大不大？",
            "common_vehicle_type_guidance_can_use_local_style",
            "realtime_vehicle_type_guidance",
        ),
        (
            "如果轿车和SUV都能接受，哪种更适合我们这种二胎家庭？",
            "common_vehicle_type_guidance_can_use_local_style",
            "realtime_vehicle_type_guidance",
        ),
        (
            "轿车和SUV都能接受，跑客户加放样品，哪类更合适？",
            "common_vehicle_type_guidance_can_use_local_style",
            "realtime_vehicle_type_guidance",
        ),
        (
            "油车这边，豪华品牌和本田丰田MPV后期保养成本差多少？",
            "common_maintenance_cost_can_use_local_style",
            "realtime_maintenance_cost_guidance",
        ),
        (
            "油耗和后期保养我比较在意，别买回来小毛病多。",
            "common_maintenance_cost_can_use_local_style",
            "realtime_maintenance_cost_guidance",
        ),
        (
            "如果偶尔跑高速回老家，后排孩子坐着别太颠，应该重点看什么？",
            "common_comfort_highway_guidance_can_use_local_style",
            "realtime_comfort_highway_guidance",
        ),
        (
            "你刚才提的车里，奇骏和途观L哪个更贴近我这个用法？",
            "common_vehicle_compare_can_use_local_style",
            "realtime_vehicle_compare_guidance",
        ),
        (
            "我先不交定金，先看车和报告，满意再谈，流程上可以吧？",
            "common_no_deposit_visit_can_use_local_style",
            "realtime_no_deposit_visit_guidance",
        ),
        (
            "先看车试驾，合适再谈定金，不想一上来就被催付钱。",
            "common_no_deposit_visit_can_use_local_style",
            "realtime_no_deposit_visit_guidance",
        ),
        (
            "能不能带第三方检测一起看？我怕只听你们说不踏实。",
            "common_inspection_guidance_can_use_local_style",
            "realtime_inspection_guidance",
        ),
    ]
    results = []
    decision = ReplyDecision(reply_text="", rule_name=None, matched=False, need_handoff=False, reason="")
    for text, expected_reason, expected_rule in cases:
        route = decide_realtime_reply_route(
            config={"realtime_reply": {"enabled": True}},
            combined=text,
            decision=decision,
            intent_result=SimpleNamespace(intent=""),
            intent_assist={"evidence": {"safety": {"must_handoff": True, "reasons": ["no_relevant_business_evidence"]}}},
            rag_reply={},
            llm_reply={},
            product_knowledge=None,
            data_capture={},
            evidence_pack={},
            recent_reply_texts=["可以先按预算、用途和车况筛两三台。"],
        )
        reply = maybe_build_realtime_reply(
            config={"realtime_reply": {"enabled": True}},
            route=route,
            combined=text,
            evidence_pack={},
            current_reply_text="",
            recent_reply_texts=[],
        )
        assert_equal(route.get("reason"), expected_reason, text)
        assert_equal(reply.get("rule_name"), expected_rule, text)
        assert_true(reply.get("applied") is True, text)
        results.append({"text": text, "route": route.get("reason"), "rule_name": reply.get("rule_name")})
    return {"cases": results}


def check_internal_rag_experience_not_pasted_to_customer() -> dict[str, Any]:
    decision = ReplyDecision(reply_text="默认回复", rule_name=None, matched=False, need_handoff=False, reason="no_rule_matched")
    payload = maybe_build_rag_reply(
        config={"rag_response": {"enabled": True, "apply_to_unmatched": True, "min_hit_score": 0.12}},
        text="这两台哪个更适合新手？",
        decision=decision,
        reply_text="默认回复",
        intent_assist={
            "intent": "product_inquiry",
            "recommended_action": "answer_from_evidence",
            "evidence": {
                "intent_tags": ["scene_product"],
                "safety": {"must_handoff": False},
                "rag_hits": [
                    {
                        "chunk_id": "exp-internal",
                        "source_id": "exp-internal",
                        "score": 0.95,
                        "category": "rag_experience",
                        "source_type": "rag_experience",
                        "text": "AI经验池概括：实盘话术样本；客户问法：两台车怎么选；历史回复要点：老哥，有空过来可以看看车子。",
                        "risk_terms": [],
                    }
                ],
            },
        },
        product_knowledge={},
        data_capture={},
    )
    assert_true(payload.get("applied") is False, "internal RAG experience text must not be pasted to customer")
    return {"applied": payload.get("applied"), "reason": payload.get("reason")}


def check_history_backfill_merges_loaded_messages() -> dict[str, Any]:
    visible = messages("第6条", "第7条", "第8条")
    for index, item in enumerate(visible, 6):
        item["id"] = f"m-{index}"
    loaded = messages("第1条", "第2条", "第3条", "第4条", "第5条", "第6条", "第7条", "第8条")
    for index, item in enumerate(loaded, 1):
        item["id"] = f"m-{index}"
    connector = FakeConnector(visible, loaded)
    target = SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False, max_batch_messages=3)
    enriched = maybe_enrich_messages_with_history(
        connector=connector,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        config={
            "history_backfill": {
                "enabled": True,
                "load_times": 2,
                "trigger_visible_unprocessed_count": 3,
                "max_messages_after_load": 80,
            }
        },
        payload={"ok": True, "messages": visible},
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
    )
    selection = select_batch_details(
        enriched.get("messages", []),
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
        allow_self_for_test=False,
        max_batch_messages=8,
        config={},
    )
    assert_equal(connector.history_load_calls, [2], "history load calls")
    assert_equal(selection.eligible_count, 8, "eligible after load")
    return {
        "history_load_calls": connector.history_load_calls,
        "final_message_count": len(enriched.get("messages", [])),
        "eligible_count": selection.eligible_count,
    }


def check_history_backfill_recovers_missing_anchor_without_gap() -> dict[str, Any]:
    visible = [message("m5", "第五条：最好自动挡"), message("m6", "第六条：预算十万")]
    loaded = [
        message("m2", "第二条：上一轮已处理"),
        message("m3", "第三条：中间补充"),
        message("m4", "第四条：再补充"),
        *visible,
    ]
    connector = FakeConnector(visible, loaded)
    target = SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False, max_batch_messages=8)
    enriched = maybe_enrich_messages_with_history(
        connector=connector,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        config={"history_backfill": {"enabled": True, "load_times": 3}},
        payload={"ok": True, "messages": visible},
        target_state={"processed_message_ids": ["m2"], "processed_content_keys": [], "handoff_message_ids": []},
    )
    meta = enriched.get("_history_backfill") or {}
    selection = select_batch_details(
        enriched.get("messages", []),
        target_state={"processed_message_ids": ["m2"], "processed_content_keys": [], "handoff_message_ids": []},
        allow_self_for_test=False,
        max_batch_messages=8,
        config={},
    )
    assert_equal(connector.history_load_calls, [3], "missing anchor should trigger history load")
    assert_true(meta.get("anchor_found_after_history_load") is True, "anchor should be recovered")
    assert_true(meta.get("gap_risk") is False, "recovered anchor should not flag gap")
    assert_equal(selection.eligible_count, 4, "middle messages should be recovered as eligible")
    return {"history_backfill": meta, "eligible_count": selection.eligible_count}


def check_history_backfill_unresolved_anchor_uses_overflow_batch() -> dict[str, Any]:
    visible = [message("m5", "第五条：最好自动挡"), message("m6", "第六条：预算十万")]
    loaded = [message("m4", "第四条：中间补充"), *visible]
    connector = FakeConnector(visible, loaded)
    target = SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False, max_batch_messages=8)
    enriched = maybe_enrich_messages_with_history(
        connector=connector,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        config={"history_backfill": {"enabled": True, "load_times": 3}},
        payload={"ok": True, "messages": visible},
        target_state={"processed_message_ids": ["m1"], "processed_content_keys": [], "handoff_message_ids": []},
    )
    meta = enriched.get("_history_backfill") or {}
    assert_equal(connector.history_load_calls, [3], "unresolved anchor should trigger history load")
    assert_true(meta.get("anchor_found_after_history_load") is False, "anchor should remain unresolved")
    assert_true(meta.get("gap_risk") is False, "unresolved anchor should downgrade to overflow batch")
    assert_equal(meta.get("history_continuity"), "overflow_unanchored", "overflow continuity")
    assert_true(meta.get("overflow_batch") is True, "overflow batch flag")
    return {"history_backfill": meta}


def check_customer_service_overflow_batch_does_not_block_reply() -> dict[str, Any]:
    visible = [message("m5", "第五条：最好自动挡"), message("m6", "第六条：预算十万")]
    loaded = [message("m4", "第四条：中间补充"), *visible]
    connector = FakeConnector(visible, loaded)
    target = SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False, max_batch_messages=8)
    state = {
        "targets": {
            "客户A": {
                "processed_message_ids": ["m1"],
                "processed_content_keys": [],
                "handoff_message_ids": [],
                "sent_replies": [],
                "reply_timestamps": [],
            }
        }
    }
    event = process_target(
        connector=connector,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        config={"history_backfill": {"enabled": True, "load_times": 3}, "raw_messages": {"enabled": False}},
        rules={},
        state=state,
        send=True,
        write_data=False,
        allow_fallback_send=False,
        mark_dry_run=False,
    )
    assert_true(event.get("action") != "blocked", f"overflow batch should not block reply: {event}")
    assert_true(bool(connector.sent_texts), "overflow batch should produce a reply instead of silence")
    assert_true("m5" in state["targets"]["客户A"].get("processed_message_ids", []), "overflow reply should mark visible message processed after send")
    return {"event": event, "history_load_calls": connector.history_load_calls}


def check_processed_fragment_keys_suppress_ocr_splits() -> dict[str, Any]:
    full = {
        "id": "full-b10",
        "type": "text",
        "sender": "self",
        "content": "[GAPGUARD_20260524_235553-B10]连续补充\n第10点：预算十三四万，想看车况透明、空间够\n用、后期别太费心。",
    }
    split = {
        "id": "split-b10",
        "type": "text",
        "sender": "self",
        "content": "第10点：预算十三四万，想看车况透明、空间够\n用、后期别太费心。",
    }
    processed_content_keys = set(message_processed_content_keys(full))
    split_key = message_content_dedupe_key(split)
    assert_true(split_key in processed_content_keys, f"split OCR fragment should be covered: {processed_content_keys}")
    selection = select_batch_details(
        [split],
        target_state={"processed_message_ids": [], "processed_content_keys": list(processed_content_keys), "handoff_message_ids": []},
        allow_self_for_test=True,
        max_batch_messages=8,
        config={},
    )
    assert_equal(selection.eligible_count, 0, "processed fragment should not be eligible again")
    return {"processed_key_count": len(processed_content_keys), "split_key": split_key}


def check_file_transfer_self_reply_is_not_reprocessed() -> dict[str, Any]:
    bot_reply = "条件已经比较清楚了：9万以内、主要给您爱人开，先看两台好上手的。"
    visible = [
        message("old-customer", "预算别超过9万。"),
        message("bot-reply", bot_reply, sender="self"),
        message("new-customer", "条件都给你了，直接给我两台方向。", sender="self"),
    ]
    target_state = {
        "processed_message_ids": ["old-customer"],
        "handoff_message_ids": [],
        "sent_replies": [{"reply_text": bot_reply, "processed_at": "2026-05-18T10:00:00"}],
    }
    selection = select_batch_details(
        visible,
        target_state=target_state,
        allow_self_for_test=True,
        max_batch_messages=8,
        config={"reply": {"prefix": ""}},
    )
    assert_equal([item["id"] for item in selection.batch], ["new-customer"], "file transfer should skip prior self reply")
    return {"batch_ids": [item["id"] for item in selection.batch], "eligible_count": selection.eligible_count}


def check_file_transfer_self_reply_ocr_punctuation_variant_is_not_reprocessed() -> dict[str, Any]:
    sent_reply = "2019款本田凌派180TURBO CVT舒适版（5.88万，2019年9月上牌）可以先排在前面。"
    ocr_reply = "2019款本田凌派180TURBOCVT舒适版(5.88万，2019年9月上牌）可以先排在前面。"
    visible = [
        message("bot-reply-ocr", ocr_reply, sender="self"),
        message("new-test", "[RPA_MULTI_20260525_020151-02] 车况透明和后期少操心怎么取舍？", sender="self"),
    ]
    target_state = {
        "processed_message_ids": [],
        "handoff_message_ids": [],
        "sent_replies": [{"reply_text": sent_reply, "processed_at": "2026-05-25T02:00:00"}],
    }
    selection = select_batch_details(
        visible,
        target_state=target_state,
        allow_self_for_test=True,
        max_batch_messages=1,
        config={"reply": {"prefix": ""}},
    )
    assert_equal([item["id"] for item in selection.batch], ["new-test"], "OCR punctuation variant of self reply should be skipped")
    assert_equal(selection.eligible_count, 1, "only the new test marker should remain eligible")
    return {"batch_ids": [item["id"] for item in selection.batch], "eligible_count": selection.eligible_count}


def check_bootstrap_marks_latest_visible_before_history_scroll() -> dict[str, Any]:
    old_reply = "实际把关就是别只看外观。先看检测报告和维保出险，再看结构件和底盘。"
    latest = [message("latest-visible-old-reply", old_reply, sender="self")]
    loaded = [message("history-older-question", "旧问题：怎么确认车况透明？", sender="self")]
    connector = FakeConnector(latest, loaded)
    state: dict[str, Any] = {"targets": {}}
    event = bootstrap_target(
        connector,  # type: ignore[arg-type]
        SimpleNamespace(name="文件传输助手", exact=True),
        state,
        {"bootstrap": {"history_load_times": 2, "visible_only_target_confirmation": False}, "reply": {"prefix": ""}},
    )
    assert_true(event.get("action") == "bootstrapped", "bootstrap should succeed")
    target_state = state["targets"]["文件传输助手"]
    assert_true("latest-visible-old-reply" in target_state.get("processed_message_ids", []), "latest visible old reply should be marked before scrolling")
    assert_true("history-older-question" in target_state.get("processed_message_ids", []), "history-loaded message should still be marked")

    selection = select_batch_details(
        [
            message("latest-visible-old-reply-new-id", old_reply, sender="self"),
            message("new-test-marker", "[RPA_MULTI_TEST-01] 预算十万以内，先看哪两台？", sender="self"),
        ],
        target_state=target_state,
        allow_self_for_test=True,
        max_batch_messages=1,
        config={"reply": {"prefix": ""}},
    )
    assert_equal([item["id"] for item in selection.batch], ["new-test-marker"], "bootstrapped latest self reply should not become overflow after OCR id drift")
    assert_equal(selection.eligible_count, 1, "only the current marker should remain eligible after bootstrap")
    return {
        "marked_message_ids": event.get("marked_message_ids"),
        "latest_visible_count": event.get("latest_visible_count"),
        "history_load_calls": connector.history_load_calls,
        "batch_ids": [item["id"] for item in selection.batch],
    }


def check_freshness_backfill_finds_original_batch() -> dict[str, Any]:
    visible = messages("客户新补充：最好自动挡")
    visible[0]["id"] = "new-1"
    loaded = messages("原问题：十万左右家用", "客户新补充：最好自动挡")
    loaded[0]["id"] = "old-1"
    loaded[1]["id"] = "new-1"
    connector = FakeConnector(visible, loaded)
    result = detect_newer_messages_before_send(
        connector=connector,  # type: ignore[arg-type]
        target=SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False),
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
        batch=[{"id": "old-1", "type": "text", "content": "原问题：十万左右家用", "sender": "customer"}],
        config={"history_backfill": {"enabled": True, "freshness_load_times": 2, "max_messages_after_load": 80}},
    )
    assert_true(bool(result.get("has_newer_messages")), "newer after original")
    assert_true(bool((result.get("history_backfill") or {}).get("original_batch_found_after_history_load")), "found original")
    return result


def check_freshness_backfill_stale_when_original_not_found() -> dict[str, Any]:
    visible = messages("客户新补充：预算十万", "客户新补充：省油")
    visible[0]["id"] = "new-1"
    visible[1]["id"] = "new-2"
    connector = FakeConnector(visible, visible)
    result = detect_newer_messages_before_send(
        connector=connector,  # type: ignore[arg-type]
        target=SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False),
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
        batch=[{"id": "old-missing", "type": "text", "content": "原问题", "sender": "customer"}],
        config={"history_backfill": {"enabled": True, "freshness_load_times": 2, "max_messages_after_load": 80}},
    )
    assert_true(bool(result.get("has_newer_messages")), "stale visible newer")
    assert_equal(result.get("reason"), "original_batch_not_visible_assume_stale", "stale reason")
    assert_true(bool(result.get("gap_risk")), "missing original after backfill should flag gap")
    return result


def check_freshness_missing_original_without_visible_newer_flags_gap() -> dict[str, Any]:
    connector = FakeConnector([], [])
    result = detect_newer_messages_before_send(
        connector=connector,  # type: ignore[arg-type]
        target=SimpleNamespace(name="客户A", exact=True, allow_self_for_test=False),
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
        batch=[{"id": "old-missing", "type": "text", "content": "原问题", "sender": "customer"}],
        config={"history_backfill": {"enabled": True, "freshness_load_times": 2, "max_messages_after_load": 80}},
    )
    assert_true(bool(result.get("has_newer_messages")), "gap risk should block stale send even without visible newer")
    assert_equal(result.get("reason"), "original_batch_not_found_gap_risk", "gap reason")
    assert_true(bool(result.get("gap_risk")), "gap risk flag")
    return result


def messages(*contents: str) -> list[dict[str, Any]]:
    return [
        {"id": f"m-{index}", "type": "text", "content": content, "sender": "customer"}
        for index, content in enumerate(contents, 1)
    ]


def message(message_id: str, content: str, sender: str = "customer") -> dict[str, Any]:
    return {"id": message_id, "type": "text", "content": content, "sender": sender}


def compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": plan.get("kind"),
        "reply_strategy": plan.get("reply_strategy"),
        "risk_level": plan.get("risk_level"),
        "combined_text": plan.get("combined_text"),
    }


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
