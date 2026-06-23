"""Offline regression checks for the WeChat AI customer-service app.

This runner does not connect to WeChat and does not call an LLM provider.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from customer_data_capture import extract_customer_data  # noqa: E402
from knowledge_loader import build_evidence_pack  # noqa: E402
from product_knowledge import decide_product_knowledge_reply, load_product_knowledge  # noqa: E402


DEFAULT_SCENARIO_PATH = APP_ROOT / "tests" / "scenarios" / "offline_regression.json"
PRODUCT_KNOWLEDGE_PATH = APP_ROOT / "data" / "compiled" / "structured_compat" / "product_knowledge.example.json"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _ensure_cloud_snapshot() -> None:
    from apps.wechat_ai_customer_service.knowledge_paths import shared_runtime_snapshot_path  # noqa: E402
    snapshot_path = shared_runtime_snapshot_path()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    minimal = {
        "schema_version": 1,
        "source": "cloud_official_shared_library",
        "tenant_id": "default",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future,
        "cache_policy": {"expires_at": future},
        "policy_bundle": {
            "merged": {
                "platform_safety_rules": {
                    "schema_version": 1,
                    "guard_terms": {
                        "business_signal_patterns": ["冰箱", "办公椅", "滤芯", "净水器", "凯美瑞", "思域", "秦PLUS", "GL8", "宝马", "二手车", "车源", "预算", "自动挡", "省油", "通勤", "置换", "贷款", "首付", "月供", "按揭", "检测", "试驾", "到店", "看车"],
                        "product_signal_terms": ["价格", "多少钱", "报价", "库存", "现货", "发货", "物流", "规格", "型号", "SKU"],
                        "manual_review_terms": ["人工", "转人工", "投诉", "赔偿", "退款", "退货", "老板", "经理"],
                        "policy_signal_terms": ["合同", "发票", "专票", "月结", "盖章", "保修", "售后", "安装"],
                        "handoff_rule_terms": ["人工确认", "人工审核", "需人工", "请人工"],
                        "observed_product_fact_patterns": ["价格:", "库存:", "型号:", "SKU:", "规格:"],
                        "unreasonable_request_patterns": ["外挂", "脚本", "破解", "作弊", "游戏"],
                        "boundary_request_patterns": ["破损", "赔偿", "退款", "退货", "投诉"],
                        "checklist_price_terms": ["价格", "多少钱", "报价", "优惠", "折扣", "最低"],
                        "checklist_stock_terms": ["库存", "现货", "发货", "物流"]
                    }
                },
                "platform_understanding_rules": {
                    "schema_version": 1,
                    "intent_keywords": {
                        "greeting": ["你好"],
                        "small_talk": ["哈哈", "呵呵", "随便看看", "看看", "挺快的", "挺不错", "逛逛"],
                        "quote": ["价格", "报价", "多少钱"],
                        "discount": ["优惠", "便宜点", "折扣", "最低", "能少吗", "按"],
                        "stock": ["库存", "现货"],
                        "shipping": ["发货", "物流", "时效", "配送", "包邮"],
                        "handoff": ["人工", "转人工", "投诉", "合同", "月结", "盖章"],
                        "contract": ["订金", "定金", "合同", "违约", "赔偿"],
                        "payment": ["付款", "支付", "月结"],
                        "catalog": ["有什么", "有哪些", "商品列表", "产品列表", "推荐下"],
                        "warranty": ["保修", "售后"],
                        "company": ["公司", "地址", "电话", "联系方式", "资质"],
                    },
                    "intent_groups": {
                        "business": ["quote", "discount", "stock", "shipping", "handoff", "contract", "payment"],
                        "product_related": ["quote", "discount", "stock", "shipping", "warranty", "spec", "scene_product"],
                        "product_context": ["shipping", "warranty", "spec", "quote", "stock", "discount", "scene_product"],
                        "rag_soft_reference": ["unknown", "catalog", "scene_product", "spec", "small_talk", "warranty", "company", "greeting"],
                        "rag_authority_block": ["quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff", "customer_data", "contract"],
                    },
                    "policy_type_to_intent": {
                        "invoice": "invoice",
                        "payment": "payment",
                        "shipping": "shipping",
                        "after_sales": "after_sales",
                        "manual_required": "handoff",
                        "catalog": "catalog",
                    },
                    "policy_tags": {
                        "invoice": "invoice_policy",
                        "payment": "payment_policy",
                        "shipping": "shipping_policy",
                        "after_sales": "after_sales_policy",
                        "catalog": "catalog_policy",
                    },
                    "policy_type_tags": {
                        "invoice": ["invoice"],
                        "payment": ["payment"],
                        "shipping": ["shipping"],
                        "after_sales": ["after_sales"],
                        "manual_required": ["handoff"],
                    },
                    "policy_key_tags": {
                        "invoice_policy": ["invoice"],
                        "payment_policy": ["payment"],
                        "shipping_policy": ["shipping"],
                        "after_sales_policy": ["after_sales"],
                    },
                    "product_knowledge_keywords": {
                        "catalog": ["有哪些", "有什么", "商品", "产品", "列表"],
                        "quote": ["价格", "报价", "多少钱"],
                        "stock": ["库存", "现货"],
                        "shipping": ["发货", "物流", "时效"],
                        "warranty": ["售后", "保修"],
                        "discount": ["优惠", "折扣", "最低", "按"],
                        "spec": ["规格"],
                    },
                    "quantity_units": ["个", "件", "台"],
                }
            }
        },
    }
    existing: dict[str, Any] = {}
    if snapshot_path.exists():
        try:
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    snapshot = _deep_merge(existing, minimal) if existing else minimal
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIO_PATH)
    args = parser.parse_args()

    _ensure_cloud_snapshot()
    result = run_scenarios(args.scenarios)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_scenarios(path: Path) -> dict[str, Any]:
    scenarios = json.loads(path.read_text(encoding="utf-8"))
    product_knowledge = load_product_knowledge(PRODUCT_KNOWLEDGE_PATH)
    results = []
    for scenario in scenarios:
        try:
            output = evaluate_scenario(scenario, product_knowledge)
            assert_expectations(scenario, output)
            results.append({"name": scenario["name"], "ok": True})
        except Exception as exc:
            results.append({"name": scenario.get("name", "<unnamed>"), "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    return {
        "ok": not failures,
        "scenario_path": str(path),
        "count": len(results),
        "failures": failures,
        "results": results,
    }


def evaluate_scenario(scenario: dict[str, Any], product_knowledge: dict[str, Any]) -> dict[str, Any]:
    kind = scenario.get("kind")
    text = str(scenario.get("text") or "")
    context = scenario.get("context", {}) or {}
    if kind == "product_knowledge":
        return decide_product_knowledge_reply(text, product_knowledge, context=context)
    if kind == "evidence_pack":
        return build_evidence_pack(text, context=context)
    if kind == "data_capture":
        return asdict(extract_customer_data(text, required_fields=scenario.get("required_fields") or ["name", "phone"]))
    raise ValueError(f"Unsupported scenario kind: {kind}")


def assert_expectations(scenario: dict[str, Any], output: dict[str, Any]) -> None:
    for path, expected in (scenario.get("expect_equal", {}) or {}).items():
        actual = get_path(output, path)
        if actual != expected:
            raise AssertionError(f"{path}: expected {expected!r}, got {actual!r}")

    for path, needles in (scenario.get("expect_contains", {}) or {}).items():
        actual = str(get_path(output, path) or "")
        for needle in needles:
            if str(needle) not in actual:
                raise AssertionError(f"{path}: expected to contain {needle!r}, got {actual!r}")

    for path, needles in (scenario.get("expect_not_contains", {}) or {}).items():
        actual = str(get_path(output, path) or "")
        for needle in needles:
            if str(needle) in actual:
                raise AssertionError(f"{path}: expected not to contain {needle!r}, got {actual!r}")

    for path, checks in (scenario.get("expect_max_occurrences", {}) or {}).items():
        actual = str(get_path(output, path) or "")
        for needle, maximum in checks.items():
            count = actual.count(str(needle))
            if count > int(maximum):
                raise AssertionError(f"{path}: expected {needle!r} at most {maximum} time(s), got {count} in {actual!r}")

    for path, expected_items in (scenario.get("expect_in", {}) or {}).items():
        actual = get_path(output, path)
        if not isinstance(actual, list):
            raise AssertionError(f"{path}: expected list, got {type(actual).__name__}")
        for expected in expected_items:
            if expected not in actual:
                raise AssertionError(f"{path}: expected item {expected!r} in {actual!r}")


def get_path(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            raise KeyError(path)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
