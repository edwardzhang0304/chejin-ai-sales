"""Focused checks for the local RAG auxiliary layer."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "rag_layer_checks"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_layer import RagService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.learning_service import attach_rag_evidence  # noqa: E402
from apps.wechat_ai_customer_service.adapters import knowledge_loader  # noqa: E402


def main() -> int:
    cleanup()
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    cleanup()
    failures = [item for item in results if not item["ok"]]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def make_service() -> RagService:
    return RagService(
        tenant_id="rag_test",
        sources_root=TEST_ROOT / "rag_sources",
        chunks_root=TEST_ROOT / "rag_chunks",
        index_root=TEST_ROOT / "rag_index",
        cache_root=TEST_ROOT / "rag_cache",
    )


def write_sample_file() -> Path:
    source = TEST_ROOT / "sources" / "fl920_doc.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "\n\n".join(
            [
                "智能指纹门锁 FL-920 适合酒店公寓、公寓民宿和办公室门禁场景。",
                "安装前需要确认门厚、开门方向、锁体尺寸和是否需要上门服务。",
                "如果客户询问安装费用或上门时间，必须根据城市和门型人工确认。",
            ]
        ),
        encoding="utf-8",
    )
    return source


def check_ingest_search_and_product_filter() -> None:
    service = make_service()
    source = write_sample_file()
    result = service.ingest_file(source, source_type="product_doc", category="product_explanations", product_id="fl-920")
    assert_true(result.get("ok") is True, "ingest should pass")
    assert_true(result.get("chunk_count", 0) >= 1, "ingest should create chunks")

    hit = service.search("酒店公寓门锁安装注意事项", product_id="fl-920", limit=3)
    assert_true(hit.get("hits"), "search should return product hit")
    assert_equal(hit["hits"][0].get("product_id"), "fl-920", "hit should keep product id")
    assert_true(hit.get("rag_can_authorize") is False, "rag must not authorize decisions")

    unrelated = service.search("酒店公寓门锁安装注意事项", product_id="ap-88", limit=3)
    assert_equal(unrelated.get("hits"), [], "product filter should block unrelated product chunks")


def check_candidate_can_attach_rag_evidence() -> None:
    service = make_service()
    source = write_sample_file()
    ingest = service.ingest_file(source, source_type="product_doc", category="product_explanations", product_id="fl-920")
    candidate: dict[str, Any] = {
        "candidate_id": "candidate_rag_test",
        "source": {"evidence_excerpt": "FL-920 适合酒店公寓，安装前确认门厚和开门方向。"},
        "proposal": {
            "summary": "建议入库：FL-920 酒店公寓适用说明",
            "formal_patch": {
                "item": {
                    "data": {
                        "title": "FL-920 酒店公寓适用说明",
                        "answer": "FL-920 适合酒店公寓，安装前需确认门厚、开门方向和锁体尺寸。",
                    }
                }
            },
        },
    }
    attach_rag_evidence(candidate, service, ingest)
    evidence = candidate.get("review", {}).get("rag_evidence", {})
    assert_true(evidence.get("enabled") is True, "candidate should carry rag evidence")
    assert_true(evidence.get("hits"), "candidate should keep supporting rag hits")
    assert_true(candidate.get("source", {}).get("rag_hits"), "source should include compact rag hits")


def check_delete_source_removes_chunks_and_index() -> None:
    service = make_service()
    source = write_sample_file()
    service.ingest_file(source, source_type="product_doc", category="product_explanations", product_id="fl-920")
    deleted = service.delete_source_by_path(source)
    assert_equal(deleted.get("deleted_sources"), 1, "delete should remove source")
    status = service.status()
    assert_equal(status.get("source_count"), 0, "source count should be zero after delete")
    assert_equal(status.get("chunk_count"), 0, "chunk count should be zero after delete")


def check_runtime_evidence_can_include_rag_without_authorization() -> None:
    original_rag_service = knowledge_loader.RagService

    class FakeRagService:
        def evidence(self, query: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
            return {
                "enabled": True,
                "query": query,
                "hits": [
                    {
                        "chunk_id": "chunk_test",
                        "source_id": "source_test",
                        "score": 0.91,
                        "text": "FL-920 适合酒店公寓，但安装费用需要人工确认。",
                        "product_id": "fl-920",
                        "category": "product_explanations",
                    }
                ],
                "confidence": 0.91,
                "rag_can_authorize": False,
                "structured_priority": True,
            }

    try:
        knowledge_loader.RagService = FakeRagService
        pack = knowledge_loader.build_evidence_pack("酒店公寓用的门锁有什么注意事项？")
    finally:
        knowledge_loader.RagService = original_rag_service
    rag = pack.get("rag_evidence", {})
    assert_true(rag.get("hits"), "runtime evidence should include rag hits")
    assert_true(rag.get("rag_can_authorize") is False, "runtime rag evidence must not authorize")
    assert_true(pack.get("safety", {}).get("allowed_auto_reply") in {True, False}, "safety summary should remain present")


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
    check_ingest_search_and_product_filter,
    check_candidate_can_attach_rag_evidence,
    check_delete_source_removes_chunks_and_index,
    check_runtime_evidence_can_include_rag_without_authorization,
]


if __name__ == "__main__":
    raise SystemExit(main())

