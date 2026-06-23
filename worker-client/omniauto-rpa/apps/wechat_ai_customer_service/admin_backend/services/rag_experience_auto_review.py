"""Automatic review metadata for newly created AI experience pool items.

This module only adds AI interpretation metadata. It must never create review
candidates or formal knowledge; promotion stays behind the manual RAG review
workflow.
"""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, with_quality

from .rag_experience_interpreter import RagExperienceInterpreter, build_auto_triage_patch, fallback_interpretation


def auto_review_rag_experience(
    experience: dict[str, Any],
    *,
    store: RagExperienceStore,
    force: bool = False,
    use_llm: bool = True,
    apply_auto_triage: bool = True,
) -> dict[str, Any]:
    """Persist and return AI review advice for a AI experience pool item.

    The import of ``rag_admin_service`` is intentionally local to avoid a module
    cycle with the learning services. The formal comparison is part of the AI
    evidence pack so the model can flag duplicates before a merchant promotes
    anything to pending review.
    """
    from .rag_admin_service import annotate_experience, collect_formal_items

    annotated = annotate_experience(with_quality(experience), collect_formal_items())
    if use_llm:
        interpretation = RagExperienceInterpreter(store=store).ensure(annotated, force=force)
    else:
        interpretation = fallback_interpretation(annotated, reason="llm_disabled_by_request")
        experience_id = str(annotated.get("experience_id") or "")
        if experience_id:
            store.update_metadata(experience_id, {"ai_interpretation": interpretation}, rebuild_index=False)
    triage_patch = build_auto_triage_patch(annotated, interpretation) if apply_auto_triage else {}
    experience_id = str(annotated.get("experience_id") or "")
    if triage_patch and experience_id:
        try:
            review = triage_patch.get("experience_review") if isinstance(triage_patch.get("experience_review"), dict) else {}
            review_status = str(review.get("status") or "")
            auto_action = str(review.get("auto_triage_action") or "")
            if review_status == "auto_triaged" and auto_action in {"discard", "already_covered"}:
                triage_reason = auto_action
                annotated = store.update_status(
                    experience_id,
                    status="discarded",
                    reason=f"auto_triaged_{triage_reason}",
                    extra=triage_patch,
                )
            else:
                annotated = store.update_metadata(experience_id, triage_patch, rebuild_index=False)
            annotated = annotate_experience(with_quality(annotated), collect_formal_items())
        except KeyError:
            pass
    annotated["ai_interpretation"] = interpretation
    return annotated
