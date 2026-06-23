"""Checks for the knowledge-base compatibility compiler."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "knowledge_compiler_checks"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_compiler import KnowledgeCompiler  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime  # noqa: E402
from product_knowledge import decide_product_knowledge_reply  # noqa: E402


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item["ok"]]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    cleanup()
    return 1 if failures else 0


def check_compile_to_disk_writes_expected_files() -> None:
    cleanup()
    result = KnowledgeCompiler(output_root=TEST_ROOT).compile_to_disk()
    assert_equal(result["ok"], True, "compile should succeed")
    for name in ("manifest", "product_knowledge", "style_examples", "metadata"):
        path = Path(result["files"][name])
        assert_true(path.exists(), f"{name} should exist")
        json.loads(path.read_text(encoding="utf-8"))


def check_compiled_counts_match_category_source() -> None:
    result = KnowledgeCompiler(output_root=TEST_ROOT).compile_to_disk()
    runtime = KnowledgeRuntime()
    counts = result["counts"]
    assert_equal(counts["products"], len(runtime.list_items("products")), "compiled product count")
    expected_styles = len(runtime.list_items("chats")) + len(runtime.list_items("global_guidelines"))
    expected_faq = len([item for item in runtime.list_items("policies") if not str(item.get("id") or "").endswith("_details")])
    expected_faq += len(list(runtime.iter_all_product_scoped_items()))
    assert_equal(counts["style_examples"], expected_styles, "compiled chat/global guideline count")
    assert_equal(counts["faq"], expected_faq, "compiled FAQ/product-scoped count")


def check_compiled_product_knowledge_is_legacy_compatible() -> None:
    result = KnowledgeCompiler(output_root=TEST_ROOT).compile_to_disk()
    knowledge = json.loads(Path(result["files"]["product_knowledge"]).read_text(encoding="utf-8"))
    output = decide_product_knowledge_reply("商用冰箱多少钱？", knowledge)
    assert_equal(output.get("matched"), True, "compiled product knowledge should answer product questions")
    assert_equal(output.get("product_id"), "commercial_fridge_bx_200", "compiled product id")


def check_product_name_homophone_and_typo_are_tolerated() -> None:
    knowledge = {
        "products": [
            {
                "id": "toyota_sienna_2021",
                "name": "丰田赛那 2.5L",
                "aliases": ["赛那", "sienna"],
                "price": 289800,
                "unit": "辆",
            }
        ],
        "faq": [],
    }
    homophone = decide_product_knowledge_reply("塞纳多少钱？", knowledge)
    assert_equal(homophone.get("matched"), True, "homophone alias should be matched")
    assert_equal(homophone.get("product_id"), "toyota_sienna_2021", "homophone query should map to same product")

    typo = decide_product_knowledge_reply("赛哪什么价？", knowledge)
    assert_equal(typo.get("matched"), True, "small typo alias should be matched")
    assert_equal(typo.get("product_id"), "toyota_sienna_2021", "typo query should map to same product")


def cleanup() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


CHECKS = [
    check_compile_to_disk_writes_expected_files,
    check_compiled_counts_match_category_source,
    check_compiled_product_knowledge_is_legacy_compatible,
    check_product_name_homophone_and_typo_are_tolerated,
]


if __name__ == "__main__":
    raise SystemExit(main())
