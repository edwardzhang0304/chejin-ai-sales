"""Provider adapter for product-image descriptions, isolated from chat vision."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


MAX_IMAGE_BYTES = 12 * 1024 * 1024
_VISUAL_KEYWORDS = (
    "左前", "右前", "左后", "右后", "正前", "正后", "车头", "车尾", "侧面", "内饰", "中控", "座椅", "轮毂",
    "白色", "黑色", "银色", "灰色", "红色", "蓝色", "轿车", "suv", "mpv", "两厢", "三厢", "敞篷",
)


def _clean(value: Any, limit: int = 800) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _list(value: Any, *, limit: int = 24) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result: list[str] = []
    for item in values:
        text = _clean(item, 120)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_descriptor(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "summary": _clean(payload.get("summary"), 480),
        "keywords": _list(payload.get("keywords"), limit=32),
        "identity_terms": _list(payload.get("identity_terms"), limit=16),
        "view": _clean(payload.get("view"), 64),
        "scene_terms": _list(payload.get("scene_terms"), limit=16),
        "ocr_text": _list(payload.get("ocr_text"), limit=16),
    }


def natural_language_descriptor_fallback(text: str) -> dict[str, Any]:
    """Retain a useful provider observation when a vision model ignores JSON mode.

    This is deliberately an indexing-only normalization fallback.  It does not
    invent facts or produce customer text; later auto-binding still requires a
    strong perceptual-image match.
    """

    summary = _clean(text, 480)
    compact = summary.lower()
    keywords = [term.upper() if term in {"suv", "mpv"} else term for term in _VISUAL_KEYWORDS if term in compact]
    identity_terms = re.findall(r"(?:奥迪|宝马|奔驰|大众|丰田|本田|日产|特斯拉|蔚来|理想|比亚迪)\s*[A-Za-z0-9-]+", summary)
    return normalize_descriptor(
        {
            "summary": summary,
            "keywords": keywords,
            "identity_terms": identity_terms,
            "view": next((term for term in ("左前", "右前", "左后", "右后", "正前", "正后", "侧面", "车头", "车尾", "内饰") if term in summary), ""),
            "scene_terms": [term for term in ("室外", "室内", "展厅", "道路", "停车场") if term in summary],
            "ocr_text": [],
        }
    )


def _descriptor_has_content(value: dict[str, Any] | None) -> bool:
    descriptor = normalize_descriptor(value)
    return bool(
        descriptor.get("summary")
        or descriptor.get("keywords")
        or descriptor.get("identity_terms")
        or descriptor.get("view")
        or descriptor.get("scene_terms")
        or descriptor.get("ocr_text")
    )


def _extract_response_text(payload: dict[str, Any], request_style: str) -> str:
    if request_style == "anthropic_messages_vision":
        return "\n".join(
            _clean(item.get("text") or item.get("content"), 8000)
            for item in (payload.get("content") or [])
            if isinstance(item, dict) and _clean(item.get("text") or item.get("content"), 8000)
        ).strip()
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    message = (choices[0] or {}).get("message") if choices and isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return "\n".join(_clean(item.get("text") or item.get("content"), 8000) for item in content if isinstance(item, dict)).strip()
    return _clean(content, 8000)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    candidates = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2:
            fenced = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
            if fenced:
                candidates.append(fenced)
    for candidate in list(candidates):
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(candidate[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    return parsed if isinstance(parsed, dict) else None
    return None


def _prompt(picture: dict[str, Any] | None) -> str:
    source = picture if isinstance(picture, dict) else {}
    hint = " ".join(
        _clean(source.get(key), 120)
        for key in ("pictureName", "description", "businessType", "filename")
        if _clean(source.get(key), 120)
    )


def _post_payload(*, url: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - configured provider endpoint
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, {"ok": False, "reason": "vehicle_image_retrieval_provider_http_error", "status": int(exc.code)}
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "reason": "vehicle_image_retrieval_provider_failed", "error_type": type(exc).__name__}
    if not isinstance(decoded, dict):
        return None, {"ok": False, "reason": "vehicle_image_retrieval_provider_invalid_response"}
    return decoded, None
    return (
        "你是二手车商品图片归纳器。只分析这张商品库图片，不要编造车源价格、库存、车况承诺，也不要输出面向客户的话术。"
        "请提取可用于后续同图检索和人工核验的视觉信息。严格只返回一个 JSON 对象："
        '{"summary":"","keywords":[],"identity_terms":[],"view":"","scene_terms":[],"ocr_text":[]}。'
        "identity_terms 只放图片可见或高度可靠的品牌/车系/型号线索；不确定留空。"
        f"图片已有的非权威提示仅供参考：{hint or '无'}"
    )


def _settings(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    try:
        timeout_seconds = int(source.get("timeout_seconds") or 30)
    except (TypeError, ValueError):
        timeout_seconds = 30
    try:
        max_tokens = int(source.get("max_tokens") or 900)
    except (TypeError, ValueError):
        max_tokens = 900
    return {
        "enabled": bool(source.get("enabled", True)),
        "request_style": _clean(source.get("request_style") or "anthropic_messages_vision", 64).lower(),
        "model": _clean(source.get("model"), 160),
        "base_url": _clean(source.get("base_url"), 360).rstrip("/"),
        "api_key": _clean(source.get("api_key"), 2000),
        "api_key_env": _clean(source.get("api_key_env") or "ANTHROPIC_AUTH_TOKEN", 160),
        "timeout_seconds": max(3, min(timeout_seconds, 120)),
        "max_tokens": max(300, min(max_tokens, 3000)),
    }


def describe_product_image(*, image_bytes: bytes, mime_type: str, picture: dict[str, Any], settings: dict[str, Any] | None) -> dict[str, Any]:
    config = _settings(settings)
    if not config["enabled"]:
        return {"ok": False, "reason": "vehicle_image_retrieval_disabled"}
    if not image_bytes:
        return {"ok": False, "reason": "vehicle_image_bytes_missing"}
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return {"ok": False, "reason": "vehicle_image_bytes_exceed_limit"}
    api_key = config["api_key"] or os.getenv(config["api_key_env"], "")
    if not api_key or not config["base_url"] or not config["model"]:
        return {"ok": False, "reason": "vehicle_image_retrieval_provider_not_configured"}
    clean_mime = str(mime_type or "image/jpeg").strip().lower()
    if not clean_mime.startswith("image/"):
        return {"ok": False, "reason": "vehicle_image_mime_invalid"}
    encoded = base64.b64encode(bytes(image_bytes)).decode("ascii")
    prompt = _prompt(picture)
    if config["request_style"] == "anthropic_messages_vision":
        url = f"{config['base_url']}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": config["model"],
            "max_tokens": config["max_tokens"],
            "temperature": 0.1,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "source": {"type": "base64", "media_type": clean_mime, "data": encoded}},
            ]}],
        }
    else:
        url = f"{config['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        payload = {
            "model": config["model"],
            "max_tokens": config["max_tokens"],
            "temperature": 0.1,
            "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{clean_mime};base64,{encoded}"}},
            ]}],
        }
    decoded, failure = _post_payload(url=url, headers=headers, payload=payload, timeout_seconds=config["timeout_seconds"])
    if failure:
        return failure
    assert isinstance(decoded, dict)
    first_response_text = _extract_response_text(decoded, config["request_style"])
    parsed = _parse_json_object(first_response_text)
    if not _descriptor_has_content(parsed):
        retry_prompt = (
            "只输出合法 JSON 对象，不要解释、不要 Markdown、不要代码块。"
            "固定键：summary、keywords、identity_terms、view、scene_terms、ocr_text；"
            "字符串字段用空字符串，数组字段用空数组。"
        )
        content = payload["messages"][0]["content"]
        if isinstance(content, list) and content and isinstance(content[0], dict):
            content[0]["text"] = retry_prompt
        decoded, failure = _post_payload(url=url, headers=headers, payload=payload, timeout_seconds=config["timeout_seconds"])
        if failure:
            return failure
        assert isinstance(decoded, dict)
        parsed = _parse_json_object(_extract_response_text(decoded, config["request_style"]))
    if not _descriptor_has_content(parsed):
        # A number of compatible vision gateways sometimes return a useful
        # natural-language observation even when JSON mode is requested.  Keep
        # that merchant-owned image summary as index metadata; do not turn it
        # into a direct vehicle match without the separate visual threshold.
        fallback = natural_language_descriptor_fallback(first_response_text)
        if not fallback.get("summary"):
            fallback = natural_language_descriptor_fallback(_extract_response_text(decoded, config["request_style"]))
        if not fallback.get("summary"):
            return {"ok": False, "reason": "vehicle_image_retrieval_provider_non_json"}
        return {
            "ok": True,
            "descriptor": fallback,
            "audit": {"model": config["model"], "request_style": config["request_style"], "response_format": "natural_language_fallback"},
        }
    return {
        "ok": True,
        "descriptor": normalize_descriptor(parsed),
        "audit": {"model": config["model"], "request_style": config["request_style"]},
    }
