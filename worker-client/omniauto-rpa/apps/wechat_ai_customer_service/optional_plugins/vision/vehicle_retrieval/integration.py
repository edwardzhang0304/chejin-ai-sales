"""Thin host adapter for the portable vehicle-image retrieval core.

The adapter is intentionally the only application-aware layer.  It reads local
V2 records, uses the neutral optional-plugin registry, and never imports the
customer image-understanding implementation, Brain, scheduler, or RPA code.
"""

from __future__ import annotations

import copy
import ipaddress
import mimetypes
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings import (
    CustomerServiceSettings,
    normalize_vehicle_image_retrieval_settings,
)
from apps.wechat_ai_customer_service.optional_plugins.registry import resolve_optional_capability
from apps.wechat_ai_customer_service.product_master import ProductMasterStore
from .core import (
    apply_vehicle_image_index,
    build_customer_query_descriptor,
    current_vehicle_image_index_state,
    match_vehicle_image_records,
    picture_ref,
    source_picture_fingerprint,
)


MAX_PRODUCT_IMAGE_BYTES = 12 * 1024 * 1024
_PICTURE_URL_KEYS = ("bigPictureUrl", "bigPictureLink", "pictureUrl", "pictureLink", "url")


def _retrieval_settings(config: dict[str, Any] | None, *, tenant_id: str | None = None) -> dict[str, Any]:
    source = config if isinstance(config, dict) else CustomerServiceSettings(tenant_id=tenant_id).get()
    return normalize_vehicle_image_retrieval_settings(source.get("vehicle_image_retrieval"))


def _picture_url(picture: dict[str, Any]) -> str:
    for key in _PICTURE_URL_KEYS:
        value = str(picture.get(key) or "").strip()
        if value:
            return value
    return ""


def _vehicle_identity_terms(record: dict[str, Any]) -> list[str]:
    payloads = record.get("source_payloads") if isinstance(record.get("source_payloads"), dict) else {}
    snapshot = payloads.get("vehicle_detail") if isinstance(payloads.get("vehicle_detail"), dict) else {}
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    base = payload.get("baseCarInfo") if isinstance(payload.get("baseCarInfo"), dict) else {}
    terms: list[str] = []
    for key in ("name", "carName", "brandName", "seriesName", "modelName"):
        value = " ".join(str(base.get(key) or "").split()).strip()
        if value and value not in terms:
            terms.append(value)
    return terms[:8]


def _safe_manual_asset_path(store: ProductMasterStore, record: dict[str, Any], picture: dict[str, Any]) -> Path:
    product_id = str(record.get("id") or "").strip()
    asset_name = Path(str(picture.get("assetFile") or "")).name
    if not product_id or not asset_name or asset_name != str(picture.get("assetFile") or ""):
        raise ValueError("vehicle_image_manual_asset_invalid")
    root = (store.root / "assets" / product_id).resolve()
    path = (root / asset_name).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("vehicle_image_manual_asset_missing")
    return path


def _public_https_url(value: str) -> str:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("vehicle_image_remote_url_not_allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("vehicle_image_remote_host_unresolved") from exc
    for entry in addresses:
        host = entry[4][0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("vehicle_image_remote_host_invalid") from exc
        if not address.is_global:
            raise ValueError("vehicle_image_remote_host_not_public")
    return parsed.geturl()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _read_limited(response: Any, *, maximum: int) -> bytes:
    content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else ""
    if content_length:
        try:
            if int(content_length) > maximum:
                raise ValueError("vehicle_image_remote_bytes_exceed_limit")
        except ValueError:
            if str(content_length).isdigit():
                raise
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = response.read(min(128 * 1024, maximum - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ValueError("vehicle_image_remote_bytes_exceed_limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _load_product_picture_bytes(store: ProductMasterStore, record: dict[str, Any], picture: dict[str, Any]) -> tuple[bytes, str]:
    """Load only merchant-owned manual assets or official Dafengche HTTPS URLs."""

    if str(picture.get("source") or "") == "manual_upload" or str(picture.get("assetFile") or ""):
        path = _safe_manual_asset_path(store, record, picture)
        content = path.read_bytes()
        if len(content) > MAX_PRODUCT_IMAGE_BYTES:
            raise ValueError("vehicle_image_manual_bytes_exceed_limit")
        return content, str(picture.get("mimeType") or mimetypes.guess_type(path.name)[0] or "image/jpeg")
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    if str(source.get("type") or "") != "dafengche":
        raise ValueError("vehicle_image_remote_source_not_official")
    url = _public_https_url(_picture_url(picture))
    request = urllib.request.Request(url, headers={"Accept": "image/*", "User-Agent": "OmniAutoVehicleImageIndexer/1.0"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:  # noqa: S310 - official source URL is validated above
            mime_type = str(response.headers.get_content_type() or "") if getattr(response, "headers", None) else ""
            content = _read_limited(response, maximum=MAX_PRODUCT_IMAGE_BYTES)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"vehicle_image_remote_fetch_http_{int(exc.code)}") from exc
    if not content:
        raise ValueError("vehicle_image_remote_empty")
    if not mime_type.startswith("image/"):
        mime_type = mimetypes.guess_type(url)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        raise ValueError("vehicle_image_remote_mime_invalid")
    return content, mime_type


def vehicle_image_retrieval_status(product_id: str, *, store: ProductMasterStore | None = None) -> dict[str, Any]:
    product_store = store or ProductMasterStore()
    record = product_store.get_item(str(product_id or ""), include_archived=True)
    if not record:
        raise FileNotFoundError(product_id)
    state = current_vehicle_image_index_state(record)
    settings = _retrieval_settings(None, tenant_id=product_store.tenant_id)
    return {
        "ok": True,
        "product_id": str(record.get("id") or ""),
        "enabled": bool(settings.get("enabled", True)),
        "state": state,
    }


def index_product_vehicle_images(
    product_id: str,
    *,
    force: bool = False,
    store: ProductMasterStore | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one vehicle's multi-photo index through the optional plugin."""

    product_store = store or ProductMasterStore()
    record = product_store.get_item(str(product_id or ""), include_archived=True)
    if not record:
        raise FileNotFoundError(product_id)
    settings = _retrieval_settings(config, tenant_id=product_store.tenant_id)
    state = current_vehicle_image_index_state(record)
    if not settings.get("enabled", True):
        return {"ok": False, "product_id": str(record.get("id") or ""), "reason": "vehicle_image_retrieval_disabled", "state": state}
    if state.get("current") and not force:
        return {"ok": True, "product_id": str(record.get("id") or ""), "reason": "vehicle_image_index_current", "state": state}
    plugin = resolve_optional_capability("vehicle_image_retrieval")
    if plugin is None or not plugin.available():
        return {"ok": False, "product_id": str(record.get("id") or ""), "reason": "vehicle_image_retrieval_plugin_unavailable", "state": state}
    pictures = [item for item in ((record.get("source_payloads") or {}).get("vehicle_pictures") or {}).get("payload", []) if isinstance(item, dict)]
    if not pictures:
        updated = apply_vehicle_image_index(record, [])
        saved = product_store.save_item(updated)
        return {"ok": bool(saved.get("ok")), "product_id": str(record.get("id") or ""), "reason": "vehicle_image_source_empty", "state": current_vehicle_image_index_state(updated)}
    entries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for picture in pictures:
        reference = picture_ref(picture)
        try:
            content, mime_type = _load_product_picture_bytes(product_store, record, picture)
        except (OSError, ValueError) as exc:
            failures.append({"picture_ref": reference, "reason": str(exc)})
            continue
        result = plugin.run(
            {
                "operation": "describe_product_image",
                "_image_bytes": content,
                "mime_type": mime_type,
                "picture": picture,
                "settings": settings,
            }
        )
        if not isinstance(result, dict) or not result.get("ok"):
            failures.append({"picture_ref": reference, "reason": str((result or {}).get("reason") or "vehicle_image_descriptor_failed")})
            continue
        descriptor = copy.deepcopy(result.get("descriptor") if isinstance(result.get("descriptor"), dict) else {})
        existing_identity_terms = descriptor.get("identity_terms") if isinstance(descriptor.get("identity_terms"), list) else []
        descriptor["identity_terms"] = [
            *[str(item) for item in existing_identity_terms if str(item).strip()],
            *[item for item in _vehicle_identity_terms(record) if item not in existing_identity_terms],
        ][:16]
        entries.append(
            {
                "picture_ref": reference,
                "picture_number": picture.get("pictureNumber"),
                "perceptual_hash": str(result.get("perceptual_hash") or ""),
                "descriptor": descriptor,
            }
        )
    if failures:
        # Never replace a valid index with a partial or failed re-index.  When
        # source pictures changed, its stored source fingerprint already makes
        # the old index stale and therefore unusable for automatic matching.
        return {
            "ok": False,
            "product_id": str(record.get("id") or ""),
            "reason": "vehicle_image_index_incomplete",
            "indexed_count": len(entries),
            "source_count": len(pictures),
            "failures": failures,
            "state": current_vehicle_image_index_state(record),
        }
    updated = apply_vehicle_image_index(
        record,
        entries,
        engine={"name": str(getattr(plugin, "name", "vehicle_image_retrieval") or "vehicle_image_retrieval"), "version": "1"},
    )
    saved = product_store.save_item(updated)
    if not saved.get("ok"):
        return {"ok": False, "product_id": str(record.get("id") or ""), "reason": "vehicle_image_index_save_failed", "state": current_vehicle_image_index_state(record)}
    return {
        "ok": True,
        "product_id": str(record.get("id") or ""),
        "reason": "vehicle_image_index_ready",
        "indexed_count": len(entries),
        "source_count": len(pictures),
        "state": current_vehicle_image_index_state(updated),
    }


def _ephemeral_image_bytes(image_payload: Any) -> bytes:
    raw = image_payload.get("image_bytes") if isinstance(image_payload, dict) else getattr(image_payload, "image_bytes", None)
    released = image_payload.get("released") if isinstance(image_payload, dict) else getattr(image_payload, "released", False)
    if released or not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("vehicle_image_query_payload_unavailable")
    return bytes(raw)


def match_customer_image_to_product_master(
    understanding: dict[str, Any] | None,
    image_payload: Any,
    config: dict[str, Any] | None = None,
    *,
    store: ProductMasterStore | None = None,
) -> dict[str, Any]:
    """Match only the current in-memory customer image against local V2 indexes."""

    product_store = store or ProductMasterStore()
    settings = _retrieval_settings(config, tenant_id=product_store.tenant_id)
    if not settings.get("enabled", True):
        return {"matched": False, "reason": "vehicle_image_retrieval_disabled", "candidates": []}
    try:
        image_bytes = _ephemeral_image_bytes(image_payload)
    except ValueError as exc:
        return {"matched": False, "reason": str(exc), "candidates": []}
    plugin = resolve_optional_capability("vehicle_image_retrieval")
    if plugin is None or not plugin.available():
        return {"matched": False, "reason": "vehicle_image_retrieval_plugin_unavailable", "candidates": []}
    fingerprint = plugin.run({"operation": "fingerprint", "_image_bytes": image_bytes})
    if not isinstance(fingerprint, dict) or not fingerprint.get("ok"):
        return {"matched": False, "reason": str((fingerprint or {}).get("reason") or "vehicle_image_query_fingerprint_failed"), "candidates": []}
    result = match_vehicle_image_records(
        product_store.list_items(include_archived=False),
        build_customer_query_descriptor(understanding),
        query_perceptual_hash=str(fingerprint.get("perceptual_hash") or ""),
        threshold=float(settings.get("match_threshold") or 0.86),
        minimum_visual_similarity=float(settings.get("minimum_visual_similarity") or 0.82),
    )
    return result


def merge_vehicle_image_match_into_catalog_assist(
    catalog_assist: dict[str, Any] | None,
    image_match: dict[str, Any] | None,
) -> dict[str, Any]:
    """Add high-confidence image evidence to the existing catalog bridge only."""

    result = copy.deepcopy(catalog_assist if isinstance(catalog_assist, dict) else {})
    match = image_match if isinstance(image_match, dict) else {}
    result["vehicle_image_retrieval"] = {
        "matched": bool(match.get("matched", False)),
        "reason": str(match.get("reason") or ""),
        "candidate_count": int(match.get("candidate_count") or len(match.get("candidates") or [])),
        "candidates": [copy.deepcopy(item) for item in (match.get("candidates") or []) if isinstance(item, dict)][:3],
    }
    if not match.get("matched"):
        return result
    candidates = match.get("candidates") if isinstance(match.get("candidates"), list) else []
    top = next((item for item in candidates if isinstance(item, dict) and item.get("auto_bind_eligible")), {})
    product_id = str(top.get("product_id") or "").strip()
    if not product_id:
        return result
    product_name = str(top.get("product_name") or "").strip()
    preview = [item for item in (result.get("catalog_candidates_preview") or []) if isinstance(item, dict) and str(item.get("id") or "") != product_id]
    preview.insert(0, {"id": product_id, "name": product_name, "matched_aliases": [], "match_reason": "high_confidence_vehicle_image_match"})
    result["catalog_candidates_preview"] = preview[:4]
    preferred = [product_id, *[str(item) for item in (result.get("preferred_candidate_ids") or []) if str(item) and str(item) != product_id]]
    result["preferred_candidate_ids"] = preferred[:5]
    result["exact_candidate_id"] = product_id
    result["exact_candidate_name"] = product_name
    result["needs_clarification"] = False
    patch = result.get("conversation_context_patch") if isinstance(result.get("conversation_context_patch"), dict) else {}
    result["conversation_context_patch"] = {
        **patch,
        "last_product_id": product_id,
        "last_product_name": product_name,
        "recent_product_ids": preferred[:5],
        "vehicle_image_match": {
            "product_id": product_id,
            "picture_ref": str(top.get("picture_ref") or ""),
            "similarity": float(top.get("similarity") or 0.0),
            "visual_similarity": float(top.get("visual_similarity") or 0.0),
        },
    }
    return result
