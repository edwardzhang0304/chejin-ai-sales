"""Focused checks for robust product-name matching (homophone + typo tolerant)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.adapters.knowledge_loader import select_products  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_index import field_matches, normalize_text  # noqa: E402
from apps.wechat_ai_customer_service.workflows.product_name_matcher import collect_matched_aliases  # noqa: E402


def main() -> int:
    checks = [
        check_collect_matched_aliases_homophone_and_typo,
        check_collect_matched_aliases_model_shorthand,
        check_knowledge_loader_select_products_homophone,
        check_knowledge_index_field_matches_homophone,
        check_no_false_positive_for_unrelated_model,
    ]
    results: list[dict[str, object]] = []
    for check in checks:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:  # pragma: no cover - test harness
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_collect_matched_aliases_homophone_and_typo() -> None:
    aliases = ["赛那", "sienna"]
    homophone = collect_matched_aliases(aliases, "塞纳多少钱？")
    assert_true("赛那" in homophone, "homophone text should match alias")

    typo = collect_matched_aliases(aliases, "赛哪什么价")
    assert_true("赛那" in typo, "small typo text should match alias")


def check_collect_matched_aliases_model_shorthand() -> None:
    aliases = ["奥迪A4L", "A4L", "豪华品牌"]
    missing_tail = collect_matched_aliases(aliases, "我记得你们这儿有个奥迪A4不错，具体情况发我看看")
    assert_true("奥迪A4L" in missing_tail, "model shorthand should still match product-master alias")

    brand_only = collect_matched_aliases(aliases, "你再看看库存，里面没奥迪吗")
    assert_true("奥迪A4L" in brand_only, "brand availability question should recall matching brand/model aliases")


def check_knowledge_loader_select_products_homophone() -> None:
    knowledge = {
        "products": [
            {"id": "toyota_sienna_2021", "name": "丰田赛那", "aliases": ["赛那", "sienna"], "price": 289800, "unit": "辆"},
            {"id": "vw_tiguanl_2021", "name": "途观L", "aliases": ["途观L"], "price": 189800, "unit": "辆"},
        ]
    }
    products = select_products(knowledge, "塞纳多少钱", ["quote", "product"], {})
    ids = [str(item.get("id") or "") for item in products]
    assert_true("toyota_sienna_2021" in ids, "knowledge loader should resolve homophone to same model")


def check_knowledge_index_field_matches_homophone() -> None:
    matched, exact_matches, _intent_matches = field_matches(
        "aliases",
        ["赛那", "sienna"],
        normalize_text("塞纳多少钱"),
        ["quote", "product"],
        {},
    )
    assert_true(matched, "knowledge index alias match should pass homophone query")
    assert_true(exact_matches > 0, "homophone alias should contribute lexical match score")


def check_no_false_positive_for_unrelated_model() -> None:
    aliases = ["卡罗拉"]
    matched = collect_matched_aliases(aliases, "卡宴多少钱")
    assert_true(not matched, "unrelated model should not be matched by typo tolerance")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
