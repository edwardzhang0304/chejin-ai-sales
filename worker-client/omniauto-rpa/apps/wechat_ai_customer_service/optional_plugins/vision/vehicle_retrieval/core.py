"""Pure index and retrieval operations for Dafengche-shaped vehicle photos."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


EXTENSION_KEY = "vehicle_image_retrieval"
SCHEMA_VERSION = 1
ENGINE_NAME = "vehicle_image_retrieval"
ENGINE_VERSION = "1"
DEFAULT_MATCH_THRESHOLD = 0.86
DEFAULT_MIN_VISUAL_SIMILARITY = 0.82
_PICTURE_URL_KEYS = ("bigPictureUrl", "bigPictureLink", "pictureUrl", "pictureLink", "url")
_TERM_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]{1,12}", re.IGNORECASE)
_HEX_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def vehicle_pictures(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    source = record if isinstance(record, dict) else {}
    payloads = source.get("source_payloads") if isinstance(source.get("source_payloads"), dict) else {}
    snapshot = payloads.get("vehicle_pictures") if isinstance(payloads.get("vehicle_pictures"), dict) else {}
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), list) else []
    return [copy.deepcopy(item) for item in payload if isinstance(item, dict)]


def picture_ref(picture: dict[str, Any] | None) -> str:
    """Return a stable, source-neutral reference without changing source data."""

    item = picture if isinstance(picture, dict) else {}
    for key in ("pictureId", "id", "assetFile", "sha256"):
        value = str(item.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    for key in _PICTURE_URL_KEYS:
        value = str(item.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    material = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"payload:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _source_picture_identity(picture: dict[str, Any]) -> dict[str, Any]:
    return {
        "picture_ref": picture_ref(picture),
        "picture_number": picture.get("pictureNumber"),
        "picture_name": picture.get("pictureName"),
        "sha256": picture.get("sha256"),
        "asset_file": picture.get("assetFile"),
        "url": next((str(picture.get(key) or "").strip() for key in _PICTURE_URL_KEYS if str(picture.get(key) or "").strip()), ""),
    }


def source_picture_fingerprint(pictures: Iterable[dict[str, Any]] | None) -> str:
    material = [
        _source_picture_identity(item)
        for item in (pictures or [])
        if isinstance(item, dict)
    ]
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _clean_text(value: Any, *, max_chars: int = 360) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:max_chars]


def _string_list(value: Any, *, limit: int = 24, max_chars: int = 96) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result: list[str] = []
    for item in values:
        text = _clean_text(item, max_chars=max_chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _term_tokens(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _clean_text(value, max_chars=160).lower()
        if not text:
            continue
        candidates = [text, *_TERM_RE.findall(text)]
        for term in candidates:
            clean = term.strip().lower()
            if len(clean) < 2 or clean in result:
                continue
            result.append(clean)
    return result[:96]


def normalize_descriptor(value: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a provider-independent visual descriptor for durable storage."""

    source = value if isinstance(value, dict) else {}
    summary = _clean_text(source.get("summary"), max_chars=480)
    keywords = _string_list(source.get("keywords"), limit=32)
    identity_terms = _string_list(source.get("identity_terms"), limit=16)
    scene_terms = _string_list(source.get("scene_terms"), limit=16)
    ocr_text = _string_list(source.get("ocr_text"), limit=16, max_chars=160)
    view = _clean_text(source.get("view"), max_chars=64)
    return {
        "summary": summary,
        "keywords": keywords,
        "identity_terms": identity_terms,
        "view": view,
        "scene_terms": scene_terms,
        "ocr_text": ocr_text,
        "terms": _term_tokens([summary, *keywords, *identity_terms, view, *scene_terms, *ocr_text]),
    }


def _valid_perceptual_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _HEX_HASH_RE.fullmatch(text) else ""


def _index_item(value: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    ref = _clean_text(value.get("picture_ref"), max_chars=512)
    if not ref:
        return None
    return {
        "picture_ref": ref,
        "picture_number": value.get("picture_number"),
        "perceptual_hash": _valid_perceptual_hash(value.get("perceptual_hash")),
        "descriptor": normalize_descriptor(value.get("descriptor") if isinstance(value.get("descriptor"), dict) else {}),
        "indexed_at": _clean_text(value.get("indexed_at"), max_chars=64),
    }


def apply_vehicle_image_index(
    record: dict[str, Any],
    image_descriptions: Iterable[dict[str, Any]] | None,
    *,
    indexed_at: str | None = None,
    engine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copied V2 record with only its additive retrieval extension changed."""

    result = copy.deepcopy(record if isinstance(record, dict) else {})
    pictures = vehicle_pictures(result)
    valid_refs = {picture_ref(item) for item in pictures}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in image_descriptions or []:
        item = _index_item(raw)
        if not item or item["picture_ref"] not in valid_refs or item["picture_ref"] in seen:
            continue
        seen.add(item["picture_ref"])
        if not item["indexed_at"]:
            item["indexed_at"] = str(indexed_at or now_iso())
        items.append(item)
    expected = len(valid_refs)
    status = "empty" if expected == 0 else "ready" if len(items) == expected else "partial"
    extensions = result.get("extensions") if isinstance(result.get("extensions"), dict) else {}
    result["extensions"] = {
        **extensions,
        EXTENSION_KEY: {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "source_payload_fingerprint": source_picture_fingerprint(pictures),
            "image_count": expected,
            "indexed_image_count": len(items),
            "indexed_at": str(indexed_at or now_iso()),
            "engine": {
                "name": _clean_text((engine or {}).get("name") or ENGINE_NAME, max_chars=96),
                "version": _clean_text((engine or {}).get("version") or ENGINE_VERSION, max_chars=32),
            },
            "items": items,
        },
    }
    return result


def current_vehicle_image_index_state(record: dict[str, Any] | None) -> dict[str, Any]:
    source = record if isinstance(record, dict) else {}
    extensions = source.get("extensions") if isinstance(source.get("extensions"), dict) else {}
    extension = extensions.get(EXTENSION_KEY) if isinstance(extensions.get(EXTENSION_KEY), dict) else {}
    pictures = vehicle_pictures(source)
    current_fingerprint = source_picture_fingerprint(pictures)
    if not extension:
        return {
            "status": "unindexed",
            "current": False,
            "source_payload_fingerprint": current_fingerprint,
            "image_count": len(pictures),
            "indexed_image_count": 0,
            "items": [],
        }
    if int(extension.get("schema_version") or 0) != SCHEMA_VERSION:
        return {
            "status": "unsupported_schema",
            "current": False,
            "source_payload_fingerprint": current_fingerprint,
            "image_count": len(pictures),
            "indexed_image_count": 0,
            "items": [],
        }
    current = str(extension.get("source_payload_fingerprint") or "") == current_fingerprint
    stored_status = str(extension.get("status") or "partial")
    status = stored_status if current else "stale"
    return {
        "status": status,
        "current": current and stored_status == "ready",
        "source_payload_fingerprint": current_fingerprint,
        "indexed_at": _clean_text(extension.get("indexed_at"), max_chars=64),
        "image_count": int(extension.get("image_count") or len(pictures)),
        "indexed_image_count": int(extension.get("indexed_image_count") or 0),
        "items": [item for item in (extension.get("items") or []) if isinstance(item, dict)],
        "engine": copy.deepcopy(extension.get("engine") if isinstance(extension.get("engine"), dict) else {}),
    }


def build_customer_query_descriptor(understanding: dict[str, Any] | None) -> dict[str, Any]:
    """Convert the existing vision compatibility payload into neutral query terms."""

    source = understanding if isinstance(understanding, dict) else {}
    entities = source.get("entities") if isinstance(source.get("entities"), dict) else {}
    bridge = source.get("bridge") if isinstance(source.get("bridge"), dict) else {}
    return normalize_descriptor(
        {
            "summary": source.get("vision_summary"),
            "keywords": [
                *(_string_list(entities.get("brand_candidates"))),
                *(_string_list(entities.get("series_candidates"))),
                *(_string_list(entities.get("model_clues"))),
                _clean_text(entities.get("body_type"), max_chars=64),
                _clean_text(entities.get("color"), max_chars=64),
                _clean_text(bridge.get("normalized_vehicle_query"), max_chars=160),
            ],
            "identity_terms": [
                *(_string_list(entities.get("brand_candidates"))),
                *(_string_list(entities.get("series_candidates"))),
                *(_string_list(entities.get("model_clues"))),
                _clean_text(bridge.get("normalized_vehicle_query"), max_chars=160),
            ],
            "ocr_text": _string_list(source.get("image_ocr_text")),
        }
    )


def _weighted_terms(descriptor: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    groups = (
        (descriptor.get("terms") or [], 1.0),
        (descriptor.get("keywords") or [], 2.0),
        (descriptor.get("identity_terms") or [], 4.0),
    )
    for values, weight in groups:
        for term in _term_tokens(values):
            result[term] = max(float(weight), result.get(term, 0.0))
    return result


def _weighted_jaccard(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_terms = _weighted_terms(left)
    right_terms = _weighted_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    keys = set(left_terms) | set(right_terms)
    denominator = sum(max(left_terms.get(key, 0.0), right_terms.get(key, 0.0)) for key in keys)
    if denominator <= 0:
        return 0.0
    numerator = sum(min(left_terms.get(key, 0.0), right_terms.get(key, 0.0)) for key in keys)
    return max(0.0, min(1.0, numerator / denominator))


def perceptual_similarity(left: Any, right: Any) -> float:
    first, second = _valid_perceptual_hash(left), _valid_perceptual_hash(right)
    if not first or not second:
        return 0.0
    distance = (int(first, 16) ^ int(second, 16)).bit_count()
    return max(0.0, min(1.0, 1.0 - (distance / 64.0)))


def _vehicle_title(record: dict[str, Any]) -> str:
    payloads = record.get("source_payloads") if isinstance(record.get("source_payloads"), dict) else {}
    detail = payloads.get("vehicle_detail") if isinstance(payloads.get("vehicle_detail"), dict) else {}
    value = detail.get("payload") if isinstance(detail.get("payload"), dict) else {}
    base = value.get("baseCarInfo") if isinstance(value.get("baseCarInfo"), dict) else {}
    return _clean_text(base.get("name") or base.get("carName") or record.get("id"), max_chars=160)


def match_vehicle_image_records(
    records: Iterable[dict[str, Any]] | None,
    query_descriptor: dict[str, Any] | None,
    *,
    query_perceptual_hash: str = "",
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    minimum_visual_similarity: float = DEFAULT_MIN_VISUAL_SIMILARITY,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank local indexed photos and auto-bind only visually strong matches."""

    query = normalize_descriptor(query_descriptor)
    clean_threshold = max(0.50, min(float(threshold or DEFAULT_MATCH_THRESHOLD), 1.0))
    min_visual = max(0.50, min(float(minimum_visual_similarity or DEFAULT_MIN_VISUAL_SIMILARITY), 1.0))
    candidates: list[dict[str, Any]] = []
    skipped_stale = 0
    for record in records or []:
        if not isinstance(record, dict) or str(record.get("status") or "active") == "archived":
            continue
        state = current_vehicle_image_index_state(record)
        if not state.get("current"):
            skipped_stale += 1
            continue
        for item in state.get("items") or []:
            if not isinstance(item, dict):
                continue
            descriptor = item.get("descriptor") if isinstance(item.get("descriptor"), dict) else {}
            semantic = _weighted_jaccard(query, descriptor)
            visual = perceptual_similarity(query_perceptual_hash, item.get("perceptual_hash"))
            # Semantic similarity is useful for sorting but deliberately cannot
            # identify a particular inventory vehicle by itself.
            # A near-identical image must not be rejected merely because a
            # vision provider described the same photo using different words.
            # Text terms can improve ranking, but never dilute direct visual
            # evidence; descriptor-only ranking remains capped below auto-bind.
            score = max(visual, visual * 0.82 + semantic * 0.18) if visual else (semantic * 0.72)
            candidates.append(
                {
                    "product_id": str(record.get("id") or ""),
                    "product_name": _vehicle_title(record),
                    "picture_ref": str(item.get("picture_ref") or ""),
                    "similarity": round(score, 4),
                    "visual_similarity": round(visual, 4),
                    "semantic_similarity": round(semantic, 4),
                    "auto_bind_eligible": bool(visual >= min_visual and score >= clean_threshold),
                    "match_reason": "perceptual_and_descriptor" if visual else "descriptor_only_ranked",
                }
            )
    candidates.sort(
        key=lambda item: (bool(item.get("auto_bind_eligible")), float(item.get("similarity") or 0.0)),
        reverse=True,
    )
    per_vehicle: list[dict[str, Any]] = []
    seen_vehicle: set[str] = set()
    for candidate in candidates:
        product_id = str(candidate.get("product_id") or "")
        if not product_id or product_id in seen_vehicle:
            continue
        seen_vehicle.add(product_id)
        per_vehicle.append(candidate)
        if len(per_vehicle) >= max(1, min(int(limit or 5), 20)):
            break
    top = per_vehicle[0] if per_vehicle else {}
    return {
        "schema_version": 1,
        "matched": bool(top.get("auto_bind_eligible", False)),
        "threshold": clean_threshold,
        "minimum_visual_similarity": min_visual,
        "query_has_perceptual_hash": bool(_valid_perceptual_hash(query_perceptual_hash)),
        "reason": (
            "high_confidence_vehicle_image_match"
            if top.get("auto_bind_eligible")
            else "no_high_confidence_vehicle_image_match"
        ),
        "candidate_count": len(per_vehicle),
        "skipped_stale_record_count": skipped_stale,
        "candidates": per_vehicle,
    }
