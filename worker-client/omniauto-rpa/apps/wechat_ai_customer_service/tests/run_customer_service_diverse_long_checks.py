"""Diverse long-run simulation checks for WeChat customer-service replies.

This suite intentionally uses a fresh prompt set that differs from the focused
burst-message checks. It exercises normal used-car consultation, guarded
boundary replies, consecutive split messages, and high-volume pressure windows
without touching live WeChat or calling an LLM provider.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

from apps.wechat_ai_customer_service.knowledge_paths import tenant_context  # noqa: E402
from knowledge_loader import build_evidence_pack  # noqa: E402
from listen_and_reply import (  # noqa: E402
    TargetConfig,
    detect_newer_messages_before_send,
    maybe_enrich_messages_with_history,
    plan_message_batch_semantics,
    process_target,
    select_batch_details,
)
from realtime_reply_router import (  # noqa: E402
    decide_realtime_reply_route,
    initial_token_budget,
    maybe_build_realtime_reply,
)
from reply_style_adapter import adapt_reply_style  # noqa: E402
from wechat_connector import _args_to_request  # noqa: E402


TENANT_ID = "chejin"
CONFIG_PATH = APP_ROOT / "configs" / "jiangsu_chejin_xucong_live.example.json"
TARGET = TargetConfig(name="长测客户", enabled=True, exact=True, allow_self_for_test=False, max_batch_messages=8)


class Decision:
    matched = False
    need_handoff = False
    rule_name = "no_rule"
    reason = "no_rule_matched"
    reply_text = "收到，我先看一下。"


class Intent:
    def __init__(self, intent: str = "product_inquiry") -> None:
        self.intent = intent

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent}


class FakeConnector:
    def __init__(self, visible: list[dict[str, Any]], loaded: list[dict[str, Any]] | None = None) -> None:
        self.visible = visible
        self.loaded = loaded
        self.sent_texts: list[str] = []
        self.history_load_calls: list[int] = []

    def get_messages(self, target: str, exact: bool = True, history_load_times: int = 0, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        if history_load_times:
            self.history_load_calls.append(history_load_times)
        messages = self.loaded if history_load_times and self.loaded is not None else self.visible
        return {
            "ok": True,
            "target": target,
            "exact": exact,
            "messages": copy.deepcopy(messages),
            "history_load": {
                "mechanism": "rpa.history_load",
                "requested_load_times": history_load_times,
            },
        }

    def send_text_and_verify(
        self,
        target: str,
        text: str,
        exact: bool = True,
        *,
        skip_send_rate_guard: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = skip_send_rate_guard, kwargs
        self.sent_texts.append(text)
        return {"ok": True, "verified": True, "target": target, "exact": exact, "text": text}


def main() -> int:
    checks = (
        check_sidecar_history_contract_new_phrase,
        check_normal_new_driver_reply_is_safe_and_local,
        check_normal_family_suv_request_can_get_candidates,
        check_identity_probe_avoids_identity_truth_discussion,
        check_document_price_boundary_uses_soft_concealed_handoff,
        check_split_family_need_is_single_event,
        check_complex_new_energy_boundary_is_mixed_risk,
        check_pressure_window_uses_rpa_history_backfill,
        check_pressure_noise_is_compacted_not_line_by_line,
        check_freshness_long_pressure_finds_original_after_backfill,
        check_process_target_multi_round_conversation,
    )
    results = []
    for check in checks:
        try:
            results.append({"name": check.__name__, "ok": True, "details": check()})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def check_sidecar_history_contract_new_phrase() -> dict[str, Any]:
    payload = _args_to_request(["messages", "--target", "新咨询客户", "--exact", "--history-load-times", "5"])
    assert_equal(payload.get("action"), "messages", "action")
    assert_equal(payload.get("history_load_times"), 5, "history_load_times")
    return payload


def check_normal_new_driver_reply_is_safe_and_local() -> dict[str, Any]:
    message = "我刚拿驾照，想买台七八万练手车，日常接娃买菜，不想太费油。"
    route, reply, budget = build_realtime_reply(message)
    text = str(reply.get("reply_text") or "")
    assert_equal(route.get("level"), "L1", "route level")
    assert_true(reply.get("applied") is True, "reply should apply")
    assert_true(any(term in text for term in ("预算", "用途", "车况", "省心", "维修")), "reply should be used-car relevant")
    assert_no_customer_visible_leak(text)
    assert_equal(budget.get("actual_total_tokens"), 0, "normal reply should stay zero-token")
    return {"route": compact_route(route), "reply_text": text, "token_budget": budget}


def check_normal_family_suv_request_can_get_candidates() -> dict[str, Any]:
    message = "家里两个孩子，想找空间大一点的SUV，十万出头，别太老，你直接帮我缩两台方向。"
    route, reply, budget = build_realtime_reply(message)
    text = str(reply.get("reply_text") or "")
    assert_true(reply.get("applied") is True, "reply should apply")
    assert_true(bool(reply.get("used_product_ids")), "explicit request should be allowed to use candidates")
    assert_true(any(term in text for term in ("检测报告", "车况", "上牌", "公里")), "reply should keep used-car caution")
    assert_no_customer_visible_leak(text)
    assert_equal(budget.get("actual_total_tokens"), 0, "candidate reply should stay zero-token")
    return {"route": compact_route(route), "reply_text": text, "used_product_ids": reply.get("used_product_ids")}


def check_identity_probe_avoids_identity_truth_discussion() -> dict[str, Any]:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="你是不是机器人在自动回？别拿模板糊弄我。",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    assert_true(result.get("applied") is True, "identity guard should apply")
    assert_true("不是AI" not in text and "不是机器人" not in text, "identity truth should not be discussed")
    assert_true(any(term in text for term in ("内部", "不方便", "具体", "核实")), "identity probe should be safely deflected")
    assert_no_customer_visible_leak(text)
    return {"reply_text": text, "reason": result.get("reason")}


def check_document_price_boundary_uses_soft_concealed_handoff() -> dict[str, Any]:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="公司买车，成交价能不能少开点发票？合同金额按我说的写行不行？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    assert_true(result.get("applied") is True, "handoff concealment should apply")
    assert_true(any(term in text for term in ("领导", "负责人", "确认", "核实", "准话")), "should ask to confirm internally")
    assert_no_customer_visible_leak(text)
    return {"reply_text": text, "reason": result.get("reason")}


def check_split_family_need_is_single_event() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages(
            "我给我妈买",
            "她停车不太熟练",
            "预算别超过9万",
            "车小一点",
            "最好自动挡带倒车影像",
        ),
        {"semantic_batch_planner": {"enabled": True}},
    )
    assert_equal(plan.get("kind"), "single_event", "kind")
    assert_equal(plan.get("reply_strategy"), "answer_as_one_need", "strategy")
    return compact_plan(plan)


def check_complex_new_energy_boundary_is_mixed_risk() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages(
            "我想看插混，平时一天跑六十公里",
            "三电检测你们能不能保证没问题",
            "异地过户麻不麻烦",
            "周日能不能先看车",
        ),
        {"semantic_batch_planner": {"enabled": True}},
    )
    assert_equal(plan.get("kind"), "multi_question_mixed_risk", "kind")
    assert_equal(plan.get("risk_level"), "boundary", "risk")
    return compact_plan(plan)


def check_pressure_window_uses_rpa_history_backfill() -> dict[str, Any]:
    loaded = numbered_messages(1, 34)
    visible = loaded[-7:]
    connector = FakeConnector(visible=visible, loaded=loaded)
    # A visibly saturated customer window may request one bounded history
    # backfill for completeness. This test only verifies the workflow does not
    # answer every noisy visible line; RPA safety/anchor checks are covered by
    # the live-action guard tests.
    target_state = {"processed_message_ids": [], "handoff_message_ids": []}
    enriched = maybe_enrich_messages_with_history(
        connector=connector,  # type: ignore[arg-type]
        target=TARGET,
        config={
            "history_backfill": {
                "enabled": True,
                "load_times": 3,
                "trigger_visible_unprocessed_count": 6,
                "max_messages_after_load": 40,
            }
        },
        payload={"ok": True, "messages": visible},
        target_state=target_state,
    )
    selection = select_batch_details(
        enriched.get("messages", []),
        target_state=target_state,
        allow_self_for_test=False,
        max_batch_messages=8,
        config={},
    )
    assert_equal(connector.history_load_calls, [3], "saturated visible window should request one bounded backfill")
    assert_true((enriched.get("_history_backfill") or {}).get("applied") is True, "visible pressure window should apply bounded backfill")
    assert_true(selection.eligible_count >= len(visible), "backfilled pressure window should remain available for batching")
    assert_true(selection.truncated, "pressure selection should be truncated rather than over-answering")
    return {
        "history_load_calls": connector.history_load_calls,
        "history_backfill": enriched.get("_history_backfill"),
        "eligible_count": selection.eligible_count,
        "batch_count": len(selection.batch),
        "overflow_count": len(selection.overflow_messages),
    }


def check_pressure_noise_is_compacted_not_line_by_line() -> dict[str, Any]:
    plan = plan_message_batch_semantics(
        messages("喂", "喂", "看到没", "急", "还在吗", "喂", "？？？", "急", "看到没", "在不在", "喂"),
        {"semantic_batch_planner": {"enabled": True, "spam_repeat_threshold": 0.7}},
    )
    assert_equal(plan.get("kind"), "spam_or_noise", "kind")
    assert_equal(plan.get("reply_strategy"), "short_stabilize_and_do_not_answer_every_line", "strategy")
    combined = str(plan.get("combined_text") or "")
    assert_true("逐条" not in combined, "combined text should compact pressure instead of requiring line-by-line reply")
    return compact_plan(plan)


def check_freshness_long_pressure_finds_original_after_backfill() -> dict[str, Any]:
    original = message("orig-1", "我先问下，十四万左右想买个跑长途稳点的车。")
    visible = [
        message("new-1", "我刚又想起来，最好后排舒服点。"),
        message("new-2", "还有就是别太费油。"),
    ]
    loaded = [original, *visible]
    connector = FakeConnector(visible=visible, loaded=loaded)
    result = detect_newer_messages_before_send(
        connector=connector,  # type: ignore[arg-type]
        target=TARGET,
        target_state={"processed_message_ids": [], "handoff_message_ids": []},
        batch=[original],
        config={"history_backfill": {"enabled": True, "freshness_load_times": 2, "max_messages_after_load": 40}},
    )
    assert_true(result.get("has_newer_messages") is True, "should notice newer messages")
    assert_true((result.get("history_backfill") or {}).get("original_batch_found_after_history_load") is True, "should find original after backfill")
    assert_equal(connector.history_load_calls, [2], "freshness backfill call")
    return result


def check_process_target_multi_round_conversation() -> dict[str, Any]:
    config = test_config()
    state: dict[str, Any] = {"version": 1, "targets": {}}
    transcript: list[dict[str, Any]] = []
    all_messages: list[dict[str, Any]] = []
    rounds = [
        ("round-1", "朋友介绍来的，我想买台跑高速稳一点的车，预算十三四万，不要太高调。"),
        ("round-2", "最好后排坐着舒服点，我爸妈偶尔也坐，年限别太久。"),
        ("round-3", "如果我今天定，你能不能保证绝对没有事故水泡？"),
        ("round-4", "那我周六上午过去看，先帮我确认车还在不在。"),
    ]
    with tenant_context(TENANT_ID):
        for msg_id, text in rounds:
            all_messages.append(message(msg_id, text))
            connector = FakeConnector(visible=all_messages)
            event = process_target(
                connector=connector,  # type: ignore[arg-type]
                target=TARGET,
                config=config,
                rules={},
                state=state,
                send=True,
                write_data=False,
                allow_fallback_send=True,
                mark_dry_run=False,
            )
            reply_text = str((event.get("decision") or {}).get("reply_text") or "")
            assert_true(event.get("action") in {"sent", "handoff_sent"}, f"unexpected action {event.get('action')}")
            assert_no_customer_visible_leak(reply_text)
            if msg_id == "round-3":
                assert_equal(event.get("action"), "handoff_sent", "accident/water guarantee should require concealed handoff")
                assert_true(any(term in reply_text for term in ("核实", "确认", "准话", "请示")), "boundary reply should be soft")
            if msg_id in {"round-2", "round-4"}:
                assert_equal(event.get("action"), "sent", f"{msg_id} should stay as normal customer-service reply")
            if msg_id == "round-2":
                assert_true(any(term in reply_text for term in ("后排", "舒适", "年限", "高速", "车况")), "comfort follow-up should answer the new preference")
                assert_true("孩子" not in reply_text, "comfort follow-up must not invent an unmentioned passenger")
            if msg_id == "round-4":
                assert_true(any(term in reply_text for term in ("车源", "排期", "白跑", "到店", "现车")), "appointment follow-up should confirm availability/schedule")
            transcript.append(
                {
                    "customer": text,
                    "action": event.get("action"),
                    "reply_text": reply_text,
                    "semantic_kind": (event.get("semantic_batch_plan") or {}).get("kind"),
                    "route": (event.get("runtime_route") or {}).get("reason"),
                }
            )
    assert_equal(len(transcript), 4, "round count")
    return {"rounds": transcript, "sent_reply_count": len(state["targets"][TARGET.name].get("sent_replies", []))}


def build_realtime_reply(message_text: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = {"realtime_reply": {"enabled": True}, "llm_reply_synthesis": {"enabled": True}}
    with tenant_context(TENANT_ID):
        evidence_pack = build_evidence_pack(message_text, context={})
        route = decide_realtime_reply_route(
            config=config,
            combined=message_text,
            decision=Decision(),
            intent_result=Intent(),
            intent_assist={"intent": "product_inquiry", "evidence": {"safety": {"must_handoff": False}}},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            evidence_pack=evidence_pack,
            recent_reply_texts=[],
        )
        reply = maybe_build_realtime_reply(
            config=config,
            route=route,
            combined=message_text,
            evidence_pack=evidence_pack,
            current_reply_text="",
            recent_reply_texts=[],
        )
    budget = initial_token_budget(route)
    return route, reply, budget


def test_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["reply"] = {"prefix": "", "allow_fallback_send": True}
    config["raw_messages"] = {"enabled": False, "learning_enabled": False, "auto_learn": False}
    config["rag_response"] = {"enabled": False}
    config["intent_router"] = {"heuristic_first": True, "llm": {"enabled": False}}
    config["intent_assist"] = {"enabled": True, "mode": "heuristic", "llm_advisory": {"enabled": False}}
    config["llm_reply_synthesis"] = {
        "enabled": False,
        "identity_guard_enabled": True,
        "cost_controls": {"enabled": True, "max_llm_calls_per_run": 0},
    }
    config["final_visible_llm_polish"] = {"enabled": False}
    config["reply_style_adapter"] = style_config()["reply_style_adapter"]
    config["customer_profiles"] = {"enabled": True, "analysis": {"enabled": False}, "greeting": {"enabled": False}}
    config["operator_alert"] = {"enabled": False}
    config["data_capture"] = {"enabled": False}
    config["history_backfill"] = {
        "enabled": True,
        "load_times": 2,
        "max_load_times": 5,
        "trigger_visible_unprocessed_count": 6,
        "max_messages_after_load": 80,
        "freshness_load_times": 2,
    }
    config["semantic_batch_planner"] = {"enabled": True, "max_messages": 12, "spam_repeat_threshold": 0.72}
    return config


def style_config(*, identity_guard: bool = True) -> dict[str, Any]:
    return {
        "reply": {"prefix": ""},
        "llm_reply_synthesis": {"identity_guard_enabled": identity_guard},
        "reply_style_adapter": {
            "enabled": True,
            "mode": "fast_local",
            "apply_to_realtime": True,
            "apply_to_rag": True,
            "apply_to_llm": True,
            "apply_to_rule": True,
            "apply_to_handoff": True,
            "identity_guard_aware": True,
            "avoid_repetition": True,
            "max_examples": 3,
            "min_similarity": 0.01,
            "include_rag_style_hits": True,
            "max_reply_chars": 620,
        },
    }


def messages(*contents: str) -> list[dict[str, Any]]:
    return [message(f"m-{index}", content) for index, content in enumerate(contents, 1)]


def numbered_messages(start: int, stop: int) -> list[dict[str, Any]]:
    return [
        message(
            f"pressure-{index}",
            f"第{index}条补充：我想比较一台适合长途通勤的车，关注车况、油耗和后排舒适。",
        )
        for index in range(start, stop + 1)
    ]


def message(message_id: str, content: str, sender: str = "customer") -> dict[str, Any]:
    return {"id": message_id, "type": "text", "content": content, "sender": sender}


def compact_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": route.get("enabled"),
        "level": route.get("level"),
        "reason": route.get("reason"),
        "foreground_llm_allowed": route.get("foreground_llm_allowed"),
    }


def compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": plan.get("kind"),
        "reply_strategy": plan.get("reply_strategy"),
        "risk_level": plan.get("risk_level"),
        "combined_text": plan.get("combined_text"),
    }


def assert_no_customer_visible_leak(text: str) -> None:
    leak_terms = ("转人工", "人工客服", "真人客服", "智能客服", "自动回复", "机器客服", "我是AI", "我是机器人")
    for term in leak_terms:
        if term in text:
            raise AssertionError(f"customer-visible leak term found: {term!r} in {text!r}")


def assert_true(value: bool, message_text: str) -> None:
    if not value:
        raise AssertionError(message_text)


def assert_equal(actual: Any, expected: Any, message_text: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message_text}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
