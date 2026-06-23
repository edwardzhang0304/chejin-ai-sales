"""Small DeepSeek probe for WeChat customer-service boundary behavior.

This runner intentionally makes only a few LLM calls. It verifies that the
provider can return schema-valid advice for fuzzy product matching, light
small talk, and over-boundary requests. It does not connect to WeChat.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from customer_intent_assist import analyze_intent, call_deepseek_advisory  # noqa: E402
from knowledge_loader import build_evidence_pack  # noqa: E402
from listen_and_reply import ReplyDecision, build_intent_context, load_config, summarize_evidence_pack  # noqa: E402


CONFIG_PATH = APP_ROOT / "configs" / "file_transfer_boundary_llm.example.json"

CASES = [
    {
        "name": "fuzzy_product_scene",
        "text": "我开个小店，想找个能放饮料的冷柜，别太复杂",
        "expect": {
            "validation_ok": True,
            "needs_handoff": False,
            "safe_to_auto_send": True,
            "evidence_product_id": "commercial_fridge_bx_200",
            "reply_contains_any": ["冰箱", "冷柜", "BX-200"],
        },
    },
    {
        "name": "light_small_talk",
        "text": "哈哈我先随便看看，你们客服回复还挺快的",
        "expect": {
            "validation_ok": True,
            "needs_handoff": False,
            "safe_to_auto_send": True,
            "style_example_id": "small_talk_service_pivot",
        },
    },
    {
        "name": "approval_boundary",
        "text": "我买 7 台冰箱，你直接给我按 20 台价，再免安装费吧",
        "expect": {
            "validation_ok": True,
            "needs_handoff": True,
            "evidence_product_id": "commercial_fridge_bx_200",
            "reply_contains_any": ["请示", "人工", "同事", "确认", "上级"],
        },
    },
]


def main() -> int:
    result = run_cases()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_cases() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    llm_settings = (config.get("intent_assist", {}) or {}).get("llm_advisory", {}) or {}
    results = []
    for case in CASES:
        try:
            output = evaluate_case(config, llm_settings, case)
            assert_expectations(case, output)
            results.append({"name": case["name"], "ok": True, **output_summary(output)})
        except Exception as exc:
            results.append({"name": case.get("name", "<unnamed>"), "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    return {"ok": not failures, "count": len(results), "failures": failures, "results": results}


def evaluate_case(config: dict[str, Any], llm_settings: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    text = str(case.get("text") or "")
    evidence_pack = build_evidence_pack(text, context={})
    product_knowledge = product_knowledge_from_evidence_pack(evidence_pack)
    decision = ReplyDecision(
        reply_text="",
        rule_name="deepseek_probe",
        matched=False,
        need_handoff=False,
        reason="deepseek_probe",
    )
    context = build_intent_context(
        config=config,
        data_capture={"enabled": True, "is_customer_data": False},
        decision=decision,
        product_knowledge=product_knowledge,
        evidence_pack=evidence_pack,
    )
    heuristic = analyze_intent(text, context=context)
    result = call_deepseek_advisory(
        text,
        context=context,
        heuristic=heuristic,
        model=str(llm_settings.get("model") or ""),
        base_url=str(llm_settings.get("base_url") or ""),
        timeout=int(llm_settings.get("timeout_seconds", 60)),
    )
    return {
        "text": text,
        "evidence": summarize_evidence_pack(evidence_pack),
        "product_knowledge": product_knowledge,
        "heuristic": heuristic.__dict__,
        "deepseek": redact_deepseek_result(result),
    }


def product_knowledge_from_evidence_pack(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    products = ((evidence_pack.get("evidence", {}) or {}).get("products", []) or [])
    if not products:
        return {"matched": False}
    product = products[0]
    return {
        "matched": True,
        "product_id": product.get("id"),
        "product_name": product.get("name"),
        "unit_price": product.get("price"),
        "needs_handoff": False,
    }


def redact_deepseek_result(result: dict[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(result)
    output.pop("prompt_pack", None)
    output.pop("request", None)
    output.pop("raw_response_text", None)
    if output.get("response_text") and len(str(output["response_text"])) > 500:
        output["response_text"] = str(output["response_text"])[:500]
    return output


def assert_expectations(case: dict[str, Any], output: dict[str, Any]) -> None:
    result = output.get("deepseek", {}) or {}
    validation = result.get("validation", {}) or {}
    candidate = validation.get("candidate", {}) or {}
    expect = case.get("expect", {}) or {}

    if expect.get("validation_ok") is True:
        assert_true(bool(result.get("ok")), f"{case['name']} provider should return ok")
        assert_true(bool(validation.get("ok")), f"{case['name']} candidate should validate")
    if "needs_handoff" in expect:
        assert_equal(
            bool(candidate.get("needs_handoff")),
            bool(expect["needs_handoff"]),
            f"{case['name']} needs_handoff",
        )
    if "safe_to_auto_send" in expect:
        assert_equal(
            bool(candidate.get("safe_to_auto_send")),
            bool(expect["safe_to_auto_send"]),
            f"{case['name']} safe_to_auto_send",
        )
    if expect.get("evidence_product_id"):
        assert_true(
            expect["evidence_product_id"] in (output.get("evidence", {}).get("product_ids") or []),
            f"{case['name']} evidence should include expected product",
        )
    if expect.get("style_example_id"):
        assert_true(
            expect["style_example_id"] in (output.get("evidence", {}).get("style_example_ids") or []),
            f"{case['name']} evidence should include expected style example",
        )
    if expect.get("reply_contains_any"):
        reply = str(candidate.get("suggested_reply") or "")
        assert_true(
            any(str(needle) in reply for needle in expect["reply_contains_any"]),
            f"{case['name']} reply should contain one of {expect['reply_contains_any']!r}: {reply!r}",
        )


def output_summary(output: dict[str, Any]) -> dict[str, Any]:
    validation = ((output.get("deepseek", {}) or {}).get("validation", {}) or {})
    candidate = validation.get("candidate", {}) or {}
    return {
        "intent": candidate.get("intent"),
        "recommended_action": candidate.get("recommended_action"),
        "safe_to_auto_send": candidate.get("safe_to_auto_send"),
        "needs_handoff": candidate.get("needs_handoff"),
        "evidence_product_ids": output.get("evidence", {}).get("product_ids", []),
        "evidence_style_example_ids": output.get("evidence", {}).get("style_example_ids", []),
    }


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
