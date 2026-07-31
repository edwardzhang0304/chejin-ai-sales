from __future__ import annotations

from typing import Any

from ..result_schema import image_understanding_completed


def _text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _text_list(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def _catalog_alignment(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    confidence = data.get("alignment_confidence")
    try:
        alignment_confidence = float(confidence or 0.0)
    except (TypeError, ValueError):
        alignment_confidence = 0.0
    return {
        "selected_product_id": str(data.get("selected_product_id") or "").strip(),
        "selected_product_name": str(data.get("selected_product_name") or "").strip()[:140],
        "alignment_confidence": alignment_confidence,
        "alignment_reason": str(data.get("alignment_reason") or "").strip()[:240],
        "uncertain_reason": str(data.get("uncertain_reason") or "").strip()[:240],
    }


def normalize_customer_image_understanding_result(
    payload: dict[str, Any] | None,
    *,
    enabled: bool,
    provider: str,
    request_style: str,
    model: str,
    source_messages: list[dict[str, Any]] | None = None,
    local_visual_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    vision_summary = str(data.get("vision_summary") or "").strip()
    applied = image_understanding_completed(data)
    adoptable = bool(applied and data.get("adoptable") is True)
    classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    intent_hints = data.get("intent_hints") if isinstance(data.get("intent_hints"), dict) else {}
    bridge = data.get("bridge") if isinstance(data.get("bridge"), dict) else {}
    audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
    catalog_alignment = _catalog_alignment(data.get("catalog_alignment"))
    return {
        "schema_version": 1,
        "enabled": bool(enabled),
        "applied": applied,
        "adoptable": adoptable,
        "reason": str(data.get("reason") or ""),
        "provider": str(data.get("provider") or provider or ""),
        "request_style": str(data.get("request_style") or request_style or ""),
        "model": str(data.get("model") or model or ""),
        "source_messages": [
            item
            for item in (source_messages or data.get("source_messages") or [])
            if isinstance(item, dict)
        ],
        "local_visual_profile": dict(local_visual_profile or data.get("local_visual_profile") or {}),
        "vision_summary": vision_summary,
        "image_ocr_text": _text_list(data.get("image_ocr_text"), limit=8),
        "classification": {
            "is_vehicle": bool(classification.get("is_vehicle", False)),
            "vehicle_confidence": float(classification.get("vehicle_confidence") or 0.0),
            "unknown": bool(classification.get("unknown", False)),
            "non_vehicle_reason": str(classification.get("non_vehicle_reason") or ""),
        },
        "entities": {
            "brand_candidates": _text_list(entities.get("brand_candidates"), limit=4),
            "series_candidates": _text_list(entities.get("series_candidates"), limit=4),
            "model_clues": _text_list(entities.get("model_clues"), limit=6),
            "body_type": _text(entities.get("body_type")),
            "color": _text(entities.get("color")),
            "year_clues": _text_list(entities.get("year_clues"), limit=4),
        },
        "intent_hints": {
            "wants_catalog_match": bool(intent_hints.get("wants_catalog_match", False)),
            "wants_similar_recommendation": bool(intent_hints.get("wants_similar_recommendation", False)),
            "wants_general_chat": bool(intent_hints.get("wants_general_chat", False)),
            "needs_clarification": bool(intent_hints.get("needs_clarification", False)),
        },
        "bridge": {
            "normalized_vehicle_query": str(bridge.get("normalized_vehicle_query") or "").strip(),
            "brain_mode": str(bridge.get("brain_mode") or ""),
            "catalog_lookup_mode": str(bridge.get("catalog_lookup_mode") or ""),
        },
        "catalog_alignment": catalog_alignment,
        "audit": {
            "latency_ms": int(audit.get("latency_ms") or 0),
            "used_fallback": bool(audit.get("used_fallback", False)),
            "provider_error": str(audit.get("provider_error") or ""),
            "provider_response_text": str(audit.get("provider_response_text") or "")[:600],
            "provider_response_diagnostics": audit.get("provider_response_diagnostics") if isinstance(audit.get("provider_response_diagnostics"), dict) else {},
            "retry_error": str(audit.get("retry_error") or ""),
            "retry_response_text": str(audit.get("retry_response_text") or "")[:600],
            "retry_response_diagnostics": audit.get("retry_response_diagnostics") if isinstance(audit.get("retry_response_diagnostics"), dict) else {},
            "retry_after_non_json": bool(audit.get("retry_after_non_json", False)),
            "catalog_identity_candidate_count": int(audit.get("catalog_identity_candidate_count") or 0),
        },
    }
