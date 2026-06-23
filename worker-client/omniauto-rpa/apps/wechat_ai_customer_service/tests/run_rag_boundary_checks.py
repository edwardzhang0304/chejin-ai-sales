"""Dedicated RAG boundary checks for the WeChat customer-service workflow."""

from __future__ import annotations

import json

from run_boundary_matrix_checks import (
    check_llm_gate_allows_small_talk_candidate_without_business_evidence,
    check_process_target_applies_safe_rag_reply_before_handoff,
    check_rag_answer_layer_applies_to_soft_scene_evidence,
    check_rag_answer_layer_blocks_authority_or_risk_terms,
    check_rag_answer_layer_preserves_structured_product_reply_by_default,
    check_rag_hits_are_summarized_in_intent_context_only_as_sources,
    check_rag_only_hit_cannot_authorize_unknown_business_reply,
    check_soft_installation_reference_can_use_rag_without_handoff,
    check_soft_rag_reference_can_clear_no_business_handoff,
)


CHECKS = [
    check_rag_only_hit_cannot_authorize_unknown_business_reply,
    check_rag_hits_are_summarized_in_intent_context_only_as_sources,
    check_soft_rag_reference_can_clear_no_business_handoff,
    check_soft_installation_reference_can_use_rag_without_handoff,
    check_rag_answer_layer_applies_to_soft_scene_evidence,
    check_process_target_applies_safe_rag_reply_before_handoff,
    check_rag_answer_layer_blocks_authority_or_risk_terms,
    check_rag_answer_layer_preserves_structured_product_reply_by_default,
    check_llm_gate_allows_small_talk_candidate_without_business_evidence,
]


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
