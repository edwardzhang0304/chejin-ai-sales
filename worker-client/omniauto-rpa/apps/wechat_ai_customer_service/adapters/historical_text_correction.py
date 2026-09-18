"""Original-pixel OCR proposal. No history mutation or server acceptance here."""
from datetime import datetime, timezone
import base64
import hashlib
from io import BytesIO
import json


def original_omission_candidate(checkpoint, observations):
    """Locate one possible old omission for original-image OCR, not acceptance."""
    from .historical_text_alignment import comparison_entries
    from .message_viewport_projection import ordered_message_viewport_observations, normalized_business_message_sequence
    from .text_correspondence import business_comparison_text, single_edit
    old = comparison_entries(checkpoint)
    rows = ordered_message_viewport_observations(observations)
    current = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    candidates = []
    for start in range(len(old)):
        overlap = old[start:]
        if len(overlap) < 3 or len(overlap) > len(rows):
            continue
        changed, anchors = [], set()
        for index, entry in enumerate(overlap):
            left, right = entry.get("business_projection") or {}, current[index]
            if any(left.get(k) != right.get(k) for k in ("sender_role", "message_type", "media_state")):
                break
            if left.get("normalized_content_signature") == right.get("normalized_content_signature"):
                if left.get("message_type") == "text":
                    anchors.add(left["normalized_content_signature"])
                continue
            if entry.get("sender_role") != "customer" or entry.get("message_type") != "text":
                break
            previous = (entry.get("effective_text") or {}).get("text")
            edit = single_edit(business_comparison_text(previous or ""), business_comparison_text(rows[index].get("content_clean") or ""))
            if not previous or not edit or edit["op"] != "insert":
                break
            changed.append((entry, rows[index]["observation_id"]))
        else:
            if len(changed) == 1 and len(anchors) >= 2:
                candidates.append(changed[0])
    return candidates[0] if len(candidates) == 1 else None


def replay_original_image_request(request_path, output_dir, *, ocr_runner):
    """Offline-only Sidecar entrance: no window lookup, capture or input."""
    from pathlib import Path
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    path = Path(request["image_path"])
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("historical_correction_image_too_large")
    proposal = build_original_image_proposal(image_bytes=path.read_bytes(), original=request["original"],
        authorization=request["authorization"], ocr_runner=ocr_runner)
    proposal.pop("image_base64")
    output = Path(output_dir) / "original-ocr-proposal.json"
    output.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "proposal_path": str(output), "ui_action_performed": False,
            "new_capture_performed": False}


def captured_png_evidence(screenshot_path, *, raw_rgb_sha256):
    """Commit the PNG bytes of this same capture, never reuse an RGB digest."""
    from pathlib import Path
    from PIL import Image
    try:
        data = Path(screenshot_path).read_bytes()
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                return {"state": "unavailable", "reason": "capture_not_png"}
            if hashlib.sha256(image.convert("RGB").tobytes()).hexdigest() != raw_rgb_sha256:
                return {"state": "unavailable", "reason": "capture_pixels_changed"}
        return {"state": "captured", "sha256": hashlib.sha256(data).hexdigest(),
                "digest_recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    except (OSError, ValueError):
        return {"state": "unavailable", "reason": "capture_bytes_unavailable"}


def correction_digest(payload):
    metadata = {k: v for k, v in payload.items() if k not in {"image_base64", "proof_sha256"}}
    return hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def closed_business_resolution(request, *, worker_id, client_instance_id):
    """Bounded receipt shape, not permission to close work or resume a Worker.

    Only the authenticated backend may issue this after checking original
    evidence, ended business eligibility and the ordinary settlement barrier.
    Both sides use this same shape; absent/foreign receipts grant nothing.
    """
    return {"version": 1, "kind": "business_ended", "worker_id": worker_id,
            "client_instance_id": client_instance_id, "original_work_settled": True,
            **{key: request[key] for key in ("conversation_id", "binding_id",
                "authorization_revision", "message_event_id", "proof_sha256",
                "expected_effective_version")}}


def matches_closed_business_resolution(value, request, *, worker_id, client_instance_id):
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or type(value.get("expected_effective_version")) is not int
            or value.get("original_work_settled") is not True):
        return False
    return value == closed_business_resolution(request, worker_id=worker_id,
                                               client_instance_id=client_instance_id)


def checkpoint_comparison(item):
    """Use a server-confirmed view only for comparison, keep original identities."""
    effective = item.get("effective_text")
    if not effective or effective.get("version") == 0:
        return item
    comparison = item.get("effective_comparison")
    if (item.get("sender_role") != "customer" or item.get("message_type") != "text"
            or type(effective.get("version")) is not int or effective["version"] < 1
            or not effective.get("correction_id") or not isinstance(effective.get("text"), str)
            or hashlib.sha256(effective["text"].encode()).hexdigest() != effective.get("sha256")
            or not isinstance(comparison, dict)
            or set(comparison) != {"normalized_content_hash", "alignment_signature", "business_projection"}):
        raise ValueError("historical_correction_checkpoint_invalid")
    return {**item, **comparison}


def build_original_image_proposal(*, image_bytes, original, authorization, ocr_runner):
    """OCR is given only the pixels, never the old or proposed expected text.

    `original` is a backend-owned checkpoint entry, including the immutable
    observation/evidence. Call once, freeze the returned proposal at enqueue,
    then retry that same envelope; do not rerun OCR on network errors.
    """
    from PIL import Image
    from .text_correspondence import normalized_projection_text
    from .wechat_win32_ocr.text_bubble_recheck import locate_complete_bubbles, recognize_regions, _rect

    if len(image_bytes) > 4 * 1024 * 1024:
        raise ValueError("historical_correction_image_too_large")
    image = Image.open(BytesIO(image_bytes))
    if image.format != "PNG" or getattr(image, "n_frames", 1) != 1 or image.width * image.height > 20_000_000:
        raise ValueError("historical_correction_image_invalid")
    image.load()
    raw, evidence = original["raw_payload"], original["evidence"]
    target_id = raw["observation"]["observation_id"]
    observations = evidence["observations"]
    viewport = evidence["send_context_guard"]["message_viewport_bounds"]
    full_ocr = ocr_runner(image)
    anchors = []
    for old in observations:
        if (old.get("observation_id") == target_id or old.get("message_type") != "text"
                or old.get("row_kind") != "text_bubble" or old.get("contract_errors")):
            continue
        left, top, right, bottom = _rect(old["bubble_rect"])
        items = sorted((item for item in full_ocr
            if left <= item["center_x"] <= right and top <= item["center_y"] <= bottom),
            key=lambda item: (item["top"], item["left"]))
        observed = "\n".join(item["text"] for item in items)
        if observed and normalized_projection_text(observed) == normalized_projection_text(old["content_clean"]):
            anchors.append({"observation_id": old["observation_id"], "observed_text": observed,
                            "rect": [left, top, right, bottom]})
    if len({normalized_projection_text(a["observed_text"]) for a in anchors}) < 2:
        raise ValueError("historical_correction_anchors_insufficient")
    regions = locate_complete_bubbles(image, observations, [target_id], viewport)
    result, = recognize_regions(image, regions, ocr_runner)
    # Normalize number types at the protocol boundary, not any OCR characters.
    ocr_items = [{**item, **{k: float(item[k]) for k in (
        "confidence", "left", "top", "right", "bottom", "center_x", "center_y")}}
        for item in result["ocr_items"]]
    text = "\n".join(item["text"] for item in sorted(ocr_items, key=lambda i: (i["top"], i["left"])))
    proof = {"provenance": "capture_digest" if evidence.get("screenshot_sha256") else "legacy_correlated",
        "original_path": evidence["screenshot"], "sidecar_run_id": evidence["sidecar_run_id"],
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(), "dimensions": list(image.size),
        "digest_recorded_at": (evidence.get("screenshot_digest_recorded_at") if evidence.get("screenshot_sha256")
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
        "viewport": list(viewport), "original_observations": observations,
        "original_rect": list(_rect(raw["observation"]["bubble_rect"])),
        "bubble_rect": list(result["bubble_rect"]), "crop_rect": list(result["crop_rect"]),
        "padding": result["padding"], "scale": result["scale"], "resample": result["resample"],
        "anchors": anchors, "ocr_method": "rapidocr_complete_bubble_v1", "ocr_items": ocr_items}
    request = {"operation": "historical_text_correction", "version": 1,
        "conversation_id": authorization["conversation_id"], "binding_id": authorization["binding_id"],
        "authorization_revision": authorization["authorization_revision"],
        "message_event_id": original["message_event_id"], "source_message_key": original["source_message_key"],
        "original_read_run_id": original["origin_read_run_id"], "original_observation_id": target_id,
        "original_text_sha256": original["original_text_sha256"],
        "expected_effective_version": original.get("effective_version", 0), "corrected_text": text,
        "proof": proof, "image_base64": base64.b64encode(image_bytes).decode("ascii")}
    request["proof_sha256"] = correction_digest(request)
    return request
