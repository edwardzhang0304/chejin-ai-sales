from __future__ import annotations

from datetime import datetime
from typing import Any

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_customer_image_catalog_assist(
    *,
    understanding: dict[str, Any] | None,
    customer_text: str,
    target_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    understanding = understanding if isinstance(understanding, dict) else {}
    bridge = understanding.get("bridge") if isinstance(understanding.get("bridge"), dict) else {}
    normalized_vehicle_query = str(bridge.get("normalized_vehicle_query") or "").strip()
    entities = understanding.get("entities") if isinstance(understanding.get("entities"), dict) else {}
    if not normalized_vehicle_query:
        normalized_vehicle_query = " ".join(
            part
            for part in (
                " ".join(str(item) for item in (entities.get("brand_candidates") or [])[:2] if str(item)),
                " ".join(str(item) for item in (entities.get("series_candidates") or [])[:2] if str(item)),
                " ".join(str(item) for item in (entities.get("model_clues") or [])[:3] if str(item)),
            )
            if str(part).strip()
        ).strip()
    if not normalized_vehicle_query:
        return {
            "applied": False,
            "reason": "normalized_vehicle_query_missing",
            "normalized_vehicle_query": "",
            "preferred_candidate_ids": [],
            "catalog_candidates_preview": [],
            "conversation_context_patch": {},
        }
    context = {}
    if isinstance(target_state, dict):
        context = dict(target_state.get("conversation_context", {}) or {})
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import catalog_product_candidates

    candidates = catalog_product_candidates(normalized_vehicle_query, limit=6, context=context)
    preferred_candidate_ids = [
        str(item.get("id") or "")
        for item in candidates
        if isinstance(item, dict) and str(item.get("id") or "")
    ]
    preview = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "matched_aliases": [str(alias) for alias in (item.get("matched_aliases") or [])[:4] if str(alias)],
            "match_reason": str(item.get("match_reason") or ""),
        }
        for item in candidates[:4]
        if isinstance(item, dict)
    ]
    exact_candidate = preview[0] if preview and preview[0].get("matched_aliases") else {}
    context_patch: dict[str, Any] = {
        "last_customer_need_text": normalized_vehicle_query,
        "last_customer_need_updated_at": now_iso(),
        "recent_product_ids": preferred_candidate_ids[:5],
        "recent_product_updated_at": now_iso(),
    }
    if exact_candidate.get("id"):
        context_patch["last_product_id"] = str(exact_candidate.get("id") or "")
        context_patch["last_product_name"] = str(exact_candidate.get("name") or "")
    return {
        "applied": True,
        "reason": "catalog_candidates_ready" if candidates else "catalog_candidates_empty",
        "normalized_vehicle_query": normalized_vehicle_query,
        "catalog_lookup_mode": "vehicle_exact_then_similar",
        "preferred_candidate_ids": preferred_candidate_ids[:5],
        "catalog_candidates_preview": preview,
        "exact_candidate_id": str(exact_candidate.get("id") or ""),
        "exact_candidate_name": str(exact_candidate.get("name") or ""),
        "similar_recommendation_allowed": bool(candidates),
        "needs_clarification": not bool(candidates),
        "conversation_context_patch": context_patch,
        "customer_text": str(customer_text or "")[:160],
    }
