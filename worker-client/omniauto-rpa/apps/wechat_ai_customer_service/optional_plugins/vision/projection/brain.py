from __future__ import annotations

from datetime import datetime
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_customer_image_brain_bridge(
    understanding: dict[str, Any] | None,
    catalog_assist: dict[str, Any] | None,
    *,
    source_reason: str = "",
    vehicle_image_retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    understanding = understanding if isinstance(understanding, dict) else {}
    catalog_assist = catalog_assist if isinstance(catalog_assist, dict) else {}
    classification = understanding.get("classification") if isinstance(understanding.get("classification"), dict) else {}
    bridge = understanding.get("bridge") if isinstance(understanding.get("bridge"), dict) else {}
    intent_hints = understanding.get("intent_hints") if isinstance(understanding.get("intent_hints"), dict) else {}
    retrieval = vehicle_image_retrieval if isinstance(vehicle_image_retrieval, dict) else {}
    source_messages = understanding.get("source_messages") if isinstance(understanding.get("source_messages"), list) else []
    source_message_ids = [
        str(item.get("message_id") or "")
        for item in source_messages
        if isinstance(item, dict) and str(item.get("message_id") or "")
    ]
    normalized_vehicle_query = str(
        bridge.get("normalized_vehicle_query")
        or catalog_assist.get("normalized_vehicle_query")
        or ""
    ).strip()
    candidate_names = [
        str(item.get("name") or "")
        for item in (catalog_assist.get("catalog_candidates_preview") or [])
        if isinstance(item, dict) and str(item.get("name") or "")
    ]
    return {
        "schema_version": 1,
        "present": bool(understanding or catalog_assist),
        "source_message_ids": source_message_ids,
        "vision_summary": str(understanding.get("vision_summary") or "").strip(),
        "classification": {
            "is_vehicle": bool(classification.get("is_vehicle", False)),
            "vehicle_confidence": float(classification.get("vehicle_confidence") or 0.0),
            "unknown": bool(classification.get("unknown", False)),
            "non_vehicle_reason": str(classification.get("non_vehicle_reason") or ""),
        },
        "catalog_assist": {
            "normalized_vehicle_query": normalized_vehicle_query,
            "catalog_lookup_mode": str(
                catalog_assist.get("catalog_lookup_mode")
                or bridge.get("catalog_lookup_mode")
                or "vehicle_exact_then_similar"
            ),
            "similar_recommendation_allowed": bool(catalog_assist.get("similar_recommendation_allowed", False)),
            "preferred_candidate_ids": [
                str(item)
                for item in (catalog_assist.get("preferred_candidate_ids") or [])
                if str(item)
            ][:8],
            "candidate_names": candidate_names[:5],
            "exact_candidate_id": str(catalog_assist.get("exact_candidate_id") or ""),
            "exact_candidate_name": str(catalog_assist.get("exact_candidate_name") or ""),
        },
        "intent_hints": {
            "wants_catalog_match": bool(intent_hints.get("wants_catalog_match", False)),
            "wants_similar_recommendation": bool(intent_hints.get("wants_similar_recommendation", False)),
            "wants_general_chat": bool(intent_hints.get("wants_general_chat", False)),
            "needs_clarification": bool(
                intent_hints.get("needs_clarification", False)
                or catalog_assist.get("needs_clarification", False)
            ),
        },
        "vehicle_image_retrieval": {
            "matched": bool(retrieval.get("matched", False)),
            "reason": str(retrieval.get("reason") or ""),
            "candidates": [
                {
                    "product_id": str(item.get("product_id") or ""),
                    "product_name": str(item.get("product_name") or ""),
                    "picture_ref": str(item.get("picture_ref") or ""),
                    "similarity": float(item.get("similarity") or 0.0),
                    "visual_similarity": float(item.get("visual_similarity") or 0.0),
                }
                for item in (retrieval.get("candidates") or [])
                if isinstance(item, dict)
            ][:3],
        },
        "conversation_visual_context": {
            "last_vehicle_query": normalized_vehicle_query,
            "updated_at": now_iso(),
        },
        "policy": (
            "visual bridge input is advisory only; product facts must still be grounded "
            "in product_master and formal_knowledge"
        ),
        "audit": {
            "source_reason": str(source_reason or ""),
            "used_catalog_assist": bool(catalog_assist.get("applied", False)),
            "used_vehicle_image_retrieval": bool(retrieval.get("matched", False)),
            "understanding_reason": str(understanding.get("reason") or ""),
        },
    }


def compact_customer_image_brain_bridge(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    catalog_assist = payload.get("catalog_assist") if isinstance(payload.get("catalog_assist"), dict) else {}
    intent_hints = payload.get("intent_hints") if isinstance(payload.get("intent_hints"), dict) else {}
    retrieval = payload.get("vehicle_image_retrieval") if isinstance(payload.get("vehicle_image_retrieval"), dict) else {}
    return {
        "present": bool(payload.get("present")),
        "vision_summary": str(payload.get("vision_summary") or "")[:220],
        "classification": {
            "is_vehicle": bool(classification.get("is_vehicle", False)),
            "vehicle_confidence": float(classification.get("vehicle_confidence") or 0.0),
            "unknown": bool(classification.get("unknown", False)),
        },
        "catalog_assist": {
            "normalized_vehicle_query": str(catalog_assist.get("normalized_vehicle_query") or "")[:180],
            "candidate_names": [str(item) for item in (catalog_assist.get("candidate_names") or [])[:3] if str(item)],
            "exact_candidate_name": str(catalog_assist.get("exact_candidate_name") or "")[:80],
        },
        "intent_hints": {
            "wants_catalog_match": bool(intent_hints.get("wants_catalog_match", False)),
            "wants_similar_recommendation": bool(intent_hints.get("wants_similar_recommendation", False)),
            "needs_clarification": bool(intent_hints.get("needs_clarification", False)),
        },
        "vehicle_image_retrieval": {
            "matched": bool(retrieval.get("matched", False)),
            "candidate_names": [
                str(item.get("product_name") or "")
                for item in (retrieval.get("candidates") or [])
                if isinstance(item, dict) and str(item.get("product_name") or "")
            ][:3],
        },
        "source_message_ids": [str(item) for item in (payload.get("source_message_ids") or [])[:3] if str(item)],
    }


def resolve_visual_brain_turn_text(combined: str, visual_bridge_input: dict[str, Any] | None) -> str:
    text = str(combined or "").strip()
    if text:
        return text
    bridge = visual_bridge_input if isinstance(visual_bridge_input, dict) else {}
    classification = bridge.get("classification") if isinstance(bridge.get("classification"), dict) else {}
    if not bridge.get("present"):
        return text
    if classification.get("is_vehicle"):
        return "客户发来了一张车辆图片"
    return "客户发来了一张图片"


def augment_text_with_visual_query(combined: str, visual_bridge_input: dict[str, Any] | None) -> str:
    text = str(combined or "").strip()
    bridge = visual_bridge_input if isinstance(visual_bridge_input, dict) else {}
    if not bridge.get("present"):
        return text
    catalog_assist = bridge.get("catalog_assist") if isinstance(bridge.get("catalog_assist"), dict) else {}
    normalized_vehicle_query = str(catalog_assist.get("normalized_vehicle_query") or "").strip()
    vision_summary = str(bridge.get("vision_summary") or "").strip()
    lines: list[str] = []
    if text:
        lines.append(text)
    if normalized_vehicle_query:
        if normalized_vehicle_query not in text:
            lines.append(f"视觉识别线索：{normalized_vehicle_query}")
    elif vision_summary and vision_summary not in text:
        lines.append(f"图片内容摘要：{vision_summary}")
    return "\n".join(line for line in lines if line).strip()
