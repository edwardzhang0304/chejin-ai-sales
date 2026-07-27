from __future__ import annotations

import json
import io
import os
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .normalize import (
    normalize_customer_image_understanding_result,
)
from .provider import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SOURCE_BYTES,
    run_customer_image_understanding_provider,
)


DEFAULT_BASE_URL = "https://aiself.vip/v1"
DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"
DEFAULT_REQUEST_STYLE = "anthropic_messages_vision"
DEFAULT_MAX_TOKENS = 1800
DEFAULT_CATALOG_IDENTITY_CANDIDATE_LIMIT = 30


def best_effort_archive_prompt_event(kind: str, payload: dict[str, Any], *, config: dict[str, Any] | None = None) -> None:
    try:
        from apps.wechat_ai_customer_service.workflows.customer_service_prompt_archive import archive_prompt_event

        archive_prompt_event(kind, payload, config=config)
    except Exception:
        return


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def effective_customer_image_understanding_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    runtime_settings = dict(cfg.get("customer_image_understanding", {}) or {})
    local_settings = {}
    if isinstance(cfg.get("_local_customer_service_settings"), dict):
        local_settings = dict((cfg.get("_local_customer_service_settings") or {}).get("customer_image_understanding") or {})
    settings = {**runtime_settings, **local_settings}
    api_key_env = str(
        settings.get("api_key_env")
        or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_API_KEY_ENV")
        or "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
    ).strip()
    base_url = str(
        settings.get("base_url")
        or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip()
    model = str(
        settings.get("model")
        or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_MODEL")
        or DEFAULT_MODEL
    ).strip()
    request_style = str(
        settings.get("request_style")
        or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_REQUEST_STYLE")
        or DEFAULT_REQUEST_STYLE
    ).strip()
    api_key = str(
        settings.get("api_key")
        or os.getenv(api_key_env)
        or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_API_KEY")
        or ""
    ).strip()
    timeout_seconds = max(3, int(settings.get("timeout_seconds") or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_TIMEOUT_SECONDS") or 10))
    max_tokens = max(800, int(settings.get("max_tokens") or os.getenv("CUSTOMER_IMAGE_UNDERSTANDING_MAX_TOKENS") or DEFAULT_MAX_TOKENS))
    return {
        "enabled": settings.get("enabled", True) is not False,
        "api_key_env": api_key_env,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "request_style": request_style,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
    }


def analyze_customer_image_asset(asset: dict[str, Any]) -> dict[str, Any]:
    # Retain the helper name as a compatibility facade, while preventing a
    # previous screenshot/crop asset from being opened or interpreted again.
    del asset
    return {"available": False, "reason": "legacy_image_path_input_rejected"}


def analyze_ephemeral_customer_image(payload: Any) -> dict[str, Any]:
    """Profile an in-memory clipboard payload without exposing image bytes."""
    raw = payload.get("image_bytes") if isinstance(payload, dict) else getattr(payload, "image_bytes", None)
    released = bool(payload.get("released", False)) if isinstance(payload, dict) else bool(getattr(payload, "released", False))
    if released or not isinstance(raw, (bytes, bytearray, memoryview)):
        return {"available": False, "reason": "ephemeral_image_payload_missing"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(bytes(raw))) as decoded:
                width, height = decoded.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    return {"available": False, "reason": "image_pixel_limit_exceeded"}
                image = ImageOps.exif_transpose(decoded).convert("RGB")
                sample = image.copy()
                sample.thumbnail((96, 96), Image.Resampling.LANCZOS)
                pixels = list(sample.getdata())
                image.close()
                sample.close()
    except (Image.DecompressionBombError, OSError, ValueError, Warning):
        return {"available": False, "reason": "image_decode_failed"}
    count = max(1, len(pixels))
    avg = [int(sum(pixel[index] for pixel in pixels) / count) for index in range(3)]
    return {
        "available": True,
        "width": width,
        "height": height,
        "orientation": "landscape" if width > height else ("portrait" if height > width else "square"),
        "dominant_colors": [f"#{avg[0]:02x}{avg[1]:02x}{avg[2]:02x}"],
    }


def _clip(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "..."


def catalog_identity_candidates_for_visual_prompt(limit: int = DEFAULT_CATALOG_IDENTITY_CANDIDATE_LIMIT) -> list[dict[str, Any]]:
    """Load a thin product identity slate for visual disambiguation.

    The image module only receives names and aliases. Price, stock, and other
    product facts remain owned by product master and the Brain evidence pack.
    """
    try:
        from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime
    except Exception:
        return []
    try:
        items = KnowledgeRuntime().list_items("products")
    except Exception:
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "approved", "published"}:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        product_id = _clip(item.get("id") or data.get("id"), 80)
        name = _clip(data.get("name"), 120)
        if not product_id or not name or product_id in seen:
            continue
        aliases = []
        for alias in data.get("aliases", []) or []:
            text = _clip(alias, 60)
            if text and text not in aliases:
                aliases.append(text)
        candidates.append(
            {
                "id": product_id,
                "name": name,
                "aliases": aliases[:6],
                "category": _clip(data.get("category"), 80),
            }
        )
        seen.add(product_id)
        if len(candidates) >= max(1, int(limit or DEFAULT_CATALOG_IDENTITY_CANDIDATE_LIMIT)):
            break
    return candidates


def _catalog_candidates_json(catalog_identity_candidates: list[dict[str, Any]] | None) -> str:
    candidates = [item for item in (catalog_identity_candidates or []) if isinstance(item, dict)]
    if not candidates:
        return "[]"
    return json.dumps(candidates[:DEFAULT_CATALOG_IDENTITY_CANDIDATE_LIMIT], ensure_ascii=False, separators=(",", ":"))


def build_customer_image_understanding_prompt(
    *,
    customer_text: str,
    source_reason: str,
    local_profiles: list[dict[str, Any]],
    catalog_identity_candidates: list[dict[str, Any]] | None = None,
) -> str:
    image_summary_lines = []
    for profile in local_profiles[:3]:
        if not isinstance(profile, dict) or not profile.get("available"):
            continue
        image_summary_lines.append(
            f"- image size={profile.get('width')}x{profile.get('height')}, orientation={profile.get('orientation')}, dominant_colors={profile.get('dominant_colors')}"
        )
    image_summary = "\n".join(image_summary_lines) if image_summary_lines else "- no local profile"
    catalog_candidates = _catalog_candidates_json(catalog_identity_candidates)
    return (
        "你是微信客服图片理解模块。你的职责不是给客户写回复，而是输出紧凑 JSON，帮助 customer_service_brain 理解客户发来的图片。\n"
        "请静默完成判断，不要输出思考过程，不要解释，不要 Markdown，只输出一个 JSON 对象。\n"
        "请根据图片内容和客户文字，判断是否是汽车图片，尽量识别品牌、车系、车型线索、车身类型、颜色，并判断客户意图。\n"
        "如果不是汽车图片，也要如实说明图片主体，但不要编造商品库事实。\n"
        "catalog_candidates 是商品库里可选的车辆身份候选，只用于识别对齐，不代表必须选择。\n"
        "如果图片车辆与某个候选高度吻合，bridge.normalized_vehicle_query 填通用车型名，例如“蔚来 ES6”；catalog_alignment 填对应 id 和置信度。\n"
        "如果候选都不像、图片证据不足、或只能判断大类，normalized_vehicle_query 留空，needs_clarification=true，不要硬选库内车型。\n"
        "不要输出价格、库存、承诺或客服回复；商品事实后续仍由 product_master 和 Brain 处理。\n"
        "输出必须是 JSON 对象，字段只允许：\n"
        "vision_summary, image_ocr_text, classification, entities, intent_hints, bridge, catalog_alignment。\n"
        "classification 包含 is_vehicle, vehicle_confidence, unknown, non_vehicle_reason。\n"
        "entities 包含 brand_candidates, series_candidates, model_clues, body_type, color, year_clues。\n"
        "intent_hints 包含 wants_catalog_match, wants_similar_recommendation, wants_general_chat, needs_clarification。\n"
        "bridge 包含 normalized_vehicle_query, brain_mode, catalog_lookup_mode。\n"
        "catalog_alignment 包含 selected_product_id, selected_product_name, alignment_confidence, alignment_reason, uncertain_reason。\n"
        f"source_reason={source_reason}\n"
        f"customer_text={customer_text or '[empty]'}\n"
        f"local_profiles:\n{image_summary}\n"
        f"catalog_candidates={catalog_candidates}\n"
        "只输出 JSON，不要 markdown，不要解释。"
    )


def build_customer_image_understanding_retry_prompt(
    *,
    customer_text: str,
    source_reason: str,
    local_profiles: list[dict[str, Any]],
    catalog_identity_candidates: list[dict[str, Any]] | None = None,
) -> str:
    """A tighter retry prompt for models that spend the first response on thinking."""
    image_summary_lines = []
    for profile in local_profiles[:2]:
        if not isinstance(profile, dict) or not profile.get("available"):
            continue
        image_summary_lines.append(
            f"- {profile.get('width')}x{profile.get('height')}, {profile.get('orientation')}, colors={profile.get('dominant_colors')}"
        )
    image_summary = "\n".join(image_summary_lines) if image_summary_lines else "- no local profile"
    catalog_candidates = _catalog_candidates_json(catalog_identity_candidates)
    return (
        "只输出 JSON。不要输出思考过程、解释、Markdown 或前后缀。\n"
        "识别这张微信图片是否为汽车；如果是，给出品牌、车系/车型候选、颜色、车身类型，并参考 catalog_candidates 判断是否能对齐商品库候选。\n"
        "不要因为候选存在就硬选；只有图片车辆与候选高度吻合时才填写 selected_product_id 和 normalized_vehicle_query。\n"
        "严格使用以下 JSON 结构：\n"
        "{"
        '"vision_summary":"","image_ocr_text":[],"classification":{"is_vehicle":false,"vehicle_confidence":0,"unknown":true,"non_vehicle_reason":""},'
        '"entities":{"brand_candidates":[],"series_candidates":[],"model_clues":[],"body_type":"","color":"","year_clues":[]},'
        '"intent_hints":{"wants_catalog_match":false,"wants_similar_recommendation":false,"wants_general_chat":false,"needs_clarification":false},'
        '"bridge":{"normalized_vehicle_query":"","brain_mode":"","catalog_lookup_mode":""},'
        '"catalog_alignment":{"selected_product_id":"","selected_product_name":"","alignment_confidence":0,"alignment_reason":"","uncertain_reason":""}'
        "}\n"
        f"source_reason={source_reason}\n"
        f"customer_text={customer_text or '[empty]'}\n"
        f"local_profiles:\n{image_summary}\n"
        f"catalog_candidates={catalog_candidates}\n"
        "现在只返回填好值的 JSON 对象。"
    )


def stabilize_catalog_alignment_bridge(parsed: dict[str, Any]) -> None:
    bridge = parsed.get("bridge") if isinstance(parsed.get("bridge"), dict) else {}
    alignment = parsed.get("catalog_alignment") if isinstance(parsed.get("catalog_alignment"), dict) else {}
    if not isinstance(parsed.get("bridge"), dict):
        bridge = {}
        parsed["bridge"] = bridge
    try:
        confidence = float(alignment.get("alignment_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.7 or not str(alignment.get("selected_product_id") or "").strip():
        return
    normalized_query = str(bridge.get("normalized_vehicle_query") or "").strip()
    if not normalized_query:
        entities = parsed.get("entities") if isinstance(parsed.get("entities"), dict) else {}
        brands = [str(item).strip() for item in (entities.get("brand_candidates") or []) if str(item).strip()]
        series = [str(item).strip() for item in (entities.get("series_candidates") or []) if str(item).strip()]
        normalized_query = " ".join([*(brands[:1]), *(series[:1])]).strip()
        if not normalized_query:
            normalized_query = str(alignment.get("selected_product_name") or "").strip()
        bridge["normalized_vehicle_query"] = normalized_query
    if normalized_query and not str(bridge.get("catalog_lookup_mode") or "").strip():
        bridge["catalog_lookup_mode"] = "vehicle_exact_then_similar"


def unique_image_paths_for_understanding(image_assets: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for item in image_assets:
        if not isinstance(item, dict):
            continue
        value = str(item.get("saved_image_path") or item.get("bubble_crop_path") or item.get("thumbnail_path") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        paths.append(value)
    return paths


def maybe_run_customer_image_understanding(
    *,
    config: dict[str, Any] | None,
    customer_text: str,
    image_assets: list[dict[str, Any]],
    source_reason: str,
    image_payloads: list[Any] | None = None,
    ephemeral_clipboard: bool = False,
) -> dict[str, Any]:
    started = time.time()
    settings = effective_customer_image_understanding_settings(config)
    memory_payloads = list(image_payloads or [])
    image_paths = unique_image_paths_for_understanding(image_assets)
    local_profiles = [analyze_ephemeral_customer_image(item) for item in memory_payloads]
    local_visual_profile = {
        "asset_count": len(local_profiles),
        "profiles": local_profiles[:3],
        "width": int(local_profiles[0].get("width") or 0) if local_profiles else 0,
        "height": int(local_profiles[0].get("height") or 0) if local_profiles else 0,
        "orientation": str(local_profiles[0].get("orientation") or "") if local_profiles else "",
        "dominant_colors": list(local_profiles[0].get("dominant_colors") or [])[:4] if local_profiles else [],
    }
    source_messages = [
        {
            "message_id": str(item.get("message_id") or ""),
            "asset_id": str(item.get("asset_id") or ""),
            "message_type": "image",
        }
        for item in image_assets
        if isinstance(item, dict)
    ]
    if not settings.get("enabled", True):
        return normalize_customer_image_understanding_result(
            {"applied": False, "adoptable": False, "reason": "customer_image_understanding_disabled"},
            enabled=False,
            provider="",
            request_style=str(settings.get("request_style") or ""),
            model=str(settings.get("model") or ""),
            source_messages=source_messages,
            local_visual_profile=local_visual_profile,
        )
    if image_paths:
        return normalize_customer_image_understanding_result(
            {"applied": False, "adoptable": False, "reason": "legacy_image_path_input_rejected"},
            enabled=bool(settings.get("enabled", True)),
            provider="",
            request_style=str(settings.get("request_style") or ""),
            model=str(settings.get("model") or ""),
            source_messages=source_messages,
            local_visual_profile=local_visual_profile,
        )
    if not image_paths and not memory_payloads:
        return normalize_customer_image_understanding_result(
            {"applied": False, "adoptable": False, "reason": "customer_image_assets_missing"},
            enabled=True,
            provider="",
            request_style=str(settings.get("request_style") or ""),
            model=str(settings.get("model") or ""),
            source_messages=source_messages,
            local_visual_profile=local_visual_profile,
        )
    if not str(settings.get("api_key") or "").strip():
        fallback = {
            "applied": False,
            "adoptable": True,
            "reason": "customer_image_understanding_provider_not_configured",
            "vision_summary": "",
            "classification": {
                "is_vehicle": False,
                "vehicle_confidence": 0.0,
                "unknown": True,
                "non_vehicle_reason": "",
            },
            "entities": {},
            "intent_hints": {
                "wants_catalog_match": False,
                "wants_similar_recommendation": False,
                "wants_general_chat": True,
                "needs_clarification": True,
            },
            "bridge": {
                "normalized_vehicle_query": "",
                "brain_mode": "image_clarify_only",
                "catalog_lookup_mode": "",
            },
            "audit": {
                "latency_ms": int((time.time() - started) * 1000),
                "provider_error": "customer_image_understanding_provider_not_configured",
                "used_fallback": False,
            },
        }
        return normalize_customer_image_understanding_result(
            fallback,
            enabled=True,
            provider="",
            request_style=str(settings.get("request_style") or ""),
            model=str(settings.get("model") or ""),
            source_messages=source_messages,
            local_visual_profile=local_visual_profile,
        )
    catalog_identity_candidates = catalog_identity_candidates_for_visual_prompt()
    prompt = build_customer_image_understanding_prompt(
        customer_text=customer_text,
        source_reason=source_reason,
        local_profiles=local_profiles,
        catalog_identity_candidates=catalog_identity_candidates,
    )
    if not ephemeral_clipboard:
        best_effort_archive_prompt_event(
            "customer_image_understanding_prompt",
            {
                "source_reason": source_reason,
                "customer_text": customer_text,
                "provider": str(settings.get("base_url") or ""),
                "model": str(settings.get("model") or ""),
                "request_style": str(settings.get("request_style") or ""),
                "timeout_seconds": int(settings.get("timeout_seconds") or 10),
                "max_tokens": int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS),
                "image_paths": image_paths[:3],
                "source_messages": source_messages,
                "local_profiles": local_profiles,
                "catalog_identity_candidates": catalog_identity_candidates,
                "prompt": prompt,
            },
            config=config,
        )
    provider_result = run_customer_image_understanding_provider(
        api_key=str(settings.get("api_key") or ""),
        base_url=str(settings.get("base_url") or ""),
        model=str(settings.get("model") or ""),
        request_style=str(settings.get("request_style") or ""),
        prompt=prompt,
        image_paths=image_paths[:3],
        timeout_seconds=int(settings.get("timeout_seconds") or 10),
        max_tokens=int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS),
        image_payloads=memory_payloads[:3],
    )
    if (
        not provider_result.get("ok")
        and str(provider_result.get("error") or "") == "customer_image_understanding_response_not_json_object"
    ):
        retry_prompt = build_customer_image_understanding_retry_prompt(
            customer_text=customer_text,
            source_reason=source_reason,
            local_profiles=local_profiles,
            catalog_identity_candidates=catalog_identity_candidates,
        )
        if not ephemeral_clipboard:
            best_effort_archive_prompt_event(
                "customer_image_understanding_retry_prompt",
                {
                    "source_reason": source_reason,
                    "customer_text": customer_text,
                    "provider": str(settings.get("base_url") or ""),
                    "model": str(settings.get("model") or ""),
                    "request_style": str(settings.get("request_style") or ""),
                    "timeout_seconds": int(settings.get("timeout_seconds") or 10),
                    "max_tokens": max(int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS), DEFAULT_MAX_TOKENS * 2),
                    "image_paths": image_paths[:2],
                    "source_messages": source_messages,
                    "local_profiles": local_profiles,
                    "catalog_identity_candidates": catalog_identity_candidates,
                    "initial_error": str(provider_result.get("error") or ""),
                    "initial_response_diagnostics": provider_result.get("response_diagnostics"),
                    "prompt": retry_prompt,
                },
                config=config,
            )
        retry_result = run_customer_image_understanding_provider(
            api_key=str(settings.get("api_key") or ""),
            base_url=str(settings.get("base_url") or ""),
            model=str(settings.get("model") or ""),
            request_style=str(settings.get("request_style") or ""),
            prompt=retry_prompt,
            image_paths=image_paths[:2],
            timeout_seconds=int(settings.get("timeout_seconds") or 10),
            max_tokens=max(int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS), DEFAULT_MAX_TOKENS * 2),
            image_payloads=memory_payloads[:2],
        )
        if retry_result.get("ok"):
            retry_result["retry_after_non_json"] = True
            provider_result = retry_result
        else:
            provider_result["retry_error"] = str(retry_result.get("error") or "")
            provider_result["retry_response_text"] = str(retry_result.get("response_text") or "")[:600]
            provider_result["retry_response_diagnostics"] = retry_result.get("response_diagnostics")
    if not provider_result.get("ok"):
        fallback = {
            "applied": False,
            "adoptable": True,
            "reason": "customer_image_understanding_provider_failed",
            "vision_summary": "",
            "classification": {
                "is_vehicle": False,
                "vehicle_confidence": 0.0,
                "unknown": True,
                "non_vehicle_reason": "",
            },
            "entities": {},
            "intent_hints": {
                "wants_catalog_match": False,
                "wants_similar_recommendation": False,
                "wants_general_chat": True,
                "needs_clarification": True,
            },
            "bridge": {
                "normalized_vehicle_query": "",
                "brain_mode": "image_clarify_only",
                "catalog_lookup_mode": "",
            },
            "audit": {
                "latency_ms": int((time.time() - started) * 1000),
                "provider_error": str(provider_result.get("error") or ""),
                "provider_response_text": str(provider_result.get("response_text") or "")[:600],
                "provider_response_diagnostics": provider_result.get("response_diagnostics"),
                "retry_error": str(provider_result.get("retry_error") or ""),
                "retry_response_text": str(provider_result.get("retry_response_text") or "")[:600],
                "retry_response_diagnostics": provider_result.get("retry_response_diagnostics"),
                "used_fallback": False,
            },
        }
        normalized = normalize_customer_image_understanding_result(
            fallback,
            enabled=True,
            provider="",
            request_style=str(settings.get("request_style") or ""),
            model=str(settings.get("model") or ""),
            source_messages=source_messages,
            local_visual_profile=local_visual_profile,
        )
        if not ephemeral_clipboard:
            best_effort_archive_prompt_event(
                "customer_image_understanding_error",
                {
                    "source_reason": source_reason,
                    "customer_text": customer_text,
                    "provider": str(settings.get("base_url") or ""),
                    "model": str(settings.get("model") or ""),
                    "request_style": str(settings.get("request_style") or ""),
                    "image_paths": image_paths[:3],
                    "provider_result": provider_result,
                    "normalized_result": normalized,
                },
                config=config,
            )
        return normalized
    parsed = dict(provider_result.get("parsed") or {})
    stabilize_catalog_alignment_bridge(parsed)
    parsed["applied"] = True
    parsed["adoptable"] = True
    parsed.setdefault("reason", "vision_ready")
    parsed["audit"] = {
        "latency_ms": int((time.time() - started) * 1000),
        "used_fallback": False,
        "retry_after_non_json": bool(provider_result.get("retry_after_non_json", False)),
        "catalog_identity_candidate_count": len(catalog_identity_candidates),
    }
    parsed["provider"] = str(settings.get("base_url") or "")
    normalized = normalize_customer_image_understanding_result(
        parsed,
        enabled=True,
        provider=str(settings.get("base_url") or ""),
        request_style=str(settings.get("request_style") or ""),
        model=str(settings.get("model") or ""),
        source_messages=source_messages,
        local_visual_profile=local_visual_profile,
    )
    if not ephemeral_clipboard:
        best_effort_archive_prompt_event(
            "customer_image_understanding_result",
            {
                "source_reason": source_reason,
                "customer_text": customer_text,
                "provider": str(settings.get("base_url") or ""),
                "model": str(settings.get("model") or ""),
                "request_style": str(settings.get("request_style") or ""),
                "image_paths": image_paths[:3],
                "normalized_result": normalized,
            },
            config=config,
        )
    return normalized
