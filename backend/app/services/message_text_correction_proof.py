"""Validate the trusted Worker's original-image proof against stored facts.

The backend verifies bytes, ownership and geometry; it does not perform OCR or
claim that a legacy image had a digest when it was first captured.
"""
import base64
from collections import Counter
import hashlib
from datetime import datetime
from io import BytesIO
import json

from PIL import Image

from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.services.message_effective_text import text_sha256


def _reject(reason):
    raise AppError("HISTORICAL_TEXT_CORRECTION_REJECTED", "原图纠错证据未通过核验", 422,
                   {"reason": reason})


def _rect(value):
    if isinstance(value, dict):
        value = [value.get(key) for key in ("left", "top", "right", "bottom")]
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) is not int for v in value):
        _reject("original_rect_invalid")
    return tuple(value)


def _contains(outer, inner):
    l, t, r, b = outer
    il, it, ir, ib = inner
    return l <= il < ir <= r and t <= it < ib <= b


def proof_digest(payload):
    metadata = payload.model_dump(mode="json", exclude={"image_base64", "proof_sha256"})
    return shared_adapter("historical_text_correction").correction_digest(metadata)


def validate_original_proof(event, payload, *, previous_text, known_entities=()):
    proof = payload.proof
    raw, evidence = event.raw_payload or {}, event.evidence or {}
    original = raw.get("observation") or {}
    if (event.sender_role != "customer" or event.message_type != "text"
            or event.read_run_id != payload.original_read_run_id
            or event.source_message_key != payload.source_message_key
            or original.get("observation_id") != payload.original_observation_id
            or text_sha256(event.content or "") != payload.original_text_sha256):
        _reject("original_fact_mismatch")
    if (evidence.get("screenshot") != proof.original_path
            or evidence.get("sidecar_run_id") != proof.sidecar_run_id
            or proof.sidecar_run_id not in proof.original_path
            or evidence.get("observations") != proof.original_observations):
        _reject("original_frame_not_correlated")
    if proof_digest(payload) != payload.proof_sha256:
        _reject("proof_digest_mismatch")
    if len(json.dumps(proof.model_dump(mode="json"), ensure_ascii=False,
                      allow_nan=False).encode()) > 256 * 1024:
        _reject("proof_too_large")
    try:
        image_bytes = base64.b64decode(payload.image_base64, validate=True)
    except (ValueError, TypeError):
        _reject("invalid_base64")
    if len(image_bytes) > 4 * 1024 * 1024:
        _reject("image_too_large")
    if hashlib.sha256(image_bytes).hexdigest() != proof.image_sha256:
        _reject("image_digest_mismatch")
    captured_digest = evidence.get("screenshot_sha256")
    if evidence.get("screenshot_digest_provenance") == "capture_unavailable":
        _reject("original_capture_bytes_unavailable")
    if (proof.provenance == "capture_digest" and captured_digest != proof.image_sha256
            or captured_digest and captured_digest != proof.image_sha256):
        _reject("original_capture_digest_mismatch")
    if proof.digest_recorded_at.tzinfo is None:
        _reject("digest_time_missing_timezone")
    if proof.provenance == "capture_digest":
        try:
            recorded = datetime.fromisoformat(str(evidence.get("screenshot_digest_recorded_at") or "").replace("Z", "+00:00"))
        except ValueError:
            _reject("original_capture_time_missing")
        if recorded.tzinfo is None or recorded != proof.digest_recorded_at:
            _reject("original_capture_time_mismatch")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1
                    or image.width * image.height > 20_000_000
                    or image.size != proof.dimensions):
                _reject("image_format_or_dimensions_invalid")
            image.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        _reject("image_decode_failed")
    bounds = (0, 0, *proof.dimensions)
    viewport = _rect((evidence.get("send_context_guard") or {}).get("message_viewport_bounds"))
    seed = _rect(original.get("bubble_rect") or raw.get("bubble_rect"))
    if (proof.viewport != viewport or proof.original_rect != seed
            or not _contains(bounds, viewport) or not _contains(viewport, proof.crop_rect)
            or not _contains(proof.crop_rect, proof.bubble_rect)
            or not _contains(proof.bubble_rect, seed)):
        _reject("roi_not_bound_to_original")
    l, t, r, b = proof.bubble_rect
    vl, vt, vr, vb = viewport
    if proof.crop_rect != (max(vl, l - 8), max(vt, t - 8), min(vr, r + 8), min(vb, b + 8)):
        _reject("roi_transform_invalid")
    observations = proof.original_observations
    by_id = {item.get("observation_id"): item for item in observations}
    if len(by_id) != len(observations) or by_id.get(payload.original_observation_id) != original:
        _reject("original_observation_not_unique")
    for item in observations:
        if item.get("observation_id") == payload.original_observation_id:
            continue
        il, it, ir, ib = _rect(item.get("bubble_rect"))
        if max(l, il) < min(r, ir) and max(t, it) < min(b, ib):
            _reject("roi_contains_neighbour")
    rules = shared_adapter("text_correspondence")
    normalized = rules.normalized_projection_text
    anchors = []
    for anchor in proof.anchors:
        old = by_id.get(anchor.observation_id)
        if (not old or anchor.observation_id == payload.original_observation_id
                or old.get("message_type") != "text" or old.get("row_kind") != "text_bubble"
                or old.get("contract_errors") or old.get("sender_role") not in {"customer", "self"}
                or anchor.rect != _rect(old.get("bubble_rect"))
                or normalized(old.get("content_clean")) != normalized(anchor.observed_text)):
            _reject("structural_anchor_mismatch")
        anchors.append(normalized(anchor.observed_text))
    if len(set(anchors)) < 2 or len({a.observation_id for a in proof.anchors}) != len(proof.anchors):
        _reject("structural_anchors_not_unique")
    items = sorted(proof.ocr_items, key=lambda item: (item.top, item.left))
    for item in items:
        if (not _contains(proof.bubble_rect, (item.left, item.top, item.right, item.bottom))
                or abs(item.center_x - (item.left + item.right) / 2) > 1
                or abs(item.center_y - (item.top + item.bottom) / 2) > 1
                or any(not (l <= x <= r and t <= y <= b) for x, y in item.box)):
            _reject("ocr_outside_complete_bubble")
    observed = "\n".join(item.text for item in items)
    if observed != payload.corrected_text:
        _reject("ocr_text_mismatch")
    old, new = rules.business_comparison_text(previous_text), rules.business_comparison_text(observed)
    edit = rules.single_edit(old, new)
    if not edit or edit["op"] != "insert":
        _reject("not_single_ordinary_omission")
    left, right = rules.protected_spans(previous_text, known_entities), rules.protected_spans(observed, known_entities)
    if (Counter((s["kind"], s["text"]) for s in left) != Counter((s["kind"], s["text"]) for s in right)
            or rules._touches_edit(left, edit, "old") or rules._touches_edit(right, edit, "new")):
        _reject("protected_content_changed")
    return image_bytes
