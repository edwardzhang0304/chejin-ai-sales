from __future__ import annotations

import base64
import io
import json
import mimetypes
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

try:
    from llm_output_adapter import parse_llm_json_object
except Exception:  # pragma: no cover - script mode fallback
    from apps.wechat_ai_customer_service.workflows.llm_output_adapter import parse_llm_json_object  # type: ignore


DEFAULT_MAX_TOKENS = 1800
DEFAULT_TEMPERATURE = 0.1
MAX_IMAGE_PAYLOAD_BYTES = 3 * 1024 * 1024
MAX_IMAGE_SOURCE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_EDGE_PX = 2048
MAX_IMAGE_PIXELS = 20_000_000


class ImagePayloadError(ValueError):
    """Raised when a local visual asset cannot be safely sent to a provider."""


def _memory_image_payload_bytes(payload: Any) -> tuple[bytes, str]:
    """Read an ephemeral image payload without accepting a filesystem path."""
    if isinstance(payload, dict):
        raw = payload.get("image_bytes")
        mime_type = str(payload.get("mime_type") or "image/png")
        released = bool(payload.get("released", False))
    else:
        raw = getattr(payload, "image_bytes", None)
        mime_type = str(getattr(payload, "mime_type", "") or "image/png")
        released = bool(getattr(payload, "released", False))
    if released:
        raise ImagePayloadError("ephemeral_image_payload_released")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ImagePayloadError("ephemeral_image_payload_missing")
    image_bytes = bytes(raw)
    if not image_bytes:
        raise ImagePayloadError("ephemeral_image_payload_empty")
    if len(image_bytes) > MAX_IMAGE_PAYLOAD_BYTES:
        raise ImagePayloadError("ephemeral_image_payload_too_large")
    if not mime_type.startswith("image/"):
        raise ImagePayloadError("ephemeral_image_mime_invalid")
    return image_bytes, mime_type


def _payload_image_bytes(path: str | Path) -> tuple[bytes, str]:
    # Compatibility name retained for callers, but local paths are no longer a
    # valid vision input.  This prevents historical crops, thumbnails and any
    # arbitrary local file from being sent to the model.
    del path
    raise ImagePayloadError("legacy_image_path_input_rejected")


def data_url_from_image_path(path: str | Path) -> str:
    image_bytes, mime_type = _payload_image_bytes(path)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def data_url_from_memory_image_payload(payload: Any) -> str:
    image_bytes, mime_type = _memory_image_payload_bytes(payload)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def anthropic_image_part(path: str | Path) -> dict[str, Any]:
    image_bytes, mime_type = _payload_image_bytes(path)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": encoded,
        },
    }


def anthropic_memory_image_part(payload: Any) -> dict[str, Any]:
    image_bytes, mime_type = _memory_image_payload_bytes(payload)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": encoded,
        },
    }


def build_openai_chat_vision_payload(
    *,
    model: str,
    prompt: str,
    image_paths: list[str],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    image_payloads: list[Any] | None = None,
) -> dict[str, Any]:
    if any(str(path).strip() for path in image_paths or []):
        raise ImagePayloadError("legacy_image_path_input_rejected")
    content = [{"type": "text", "text": str(prompt or "")}]
    for payload in image_payloads or []:
        content.append({"type": "image_url", "image_url": {"url": data_url_from_memory_image_payload(payload)}})
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max(64, int(max_tokens or DEFAULT_MAX_TOKENS)),
        "temperature": float(temperature),
        "stream": False,
    }


def build_anthropic_messages_vision_payload(
    *,
    model: str,
    prompt: str,
    image_paths: list[str],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    image_payloads: list[Any] | None = None,
) -> dict[str, Any]:
    if any(str(path).strip() for path in image_paths or []):
        raise ImagePayloadError("legacy_image_path_input_rejected")
    content = [{"type": "text", "text": str(prompt or "")}]
    for payload in image_payloads or []:
        content.append(anthropic_memory_image_part(payload))
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max(64, int(max_tokens or DEFAULT_MAX_TOKENS)),
        "temperature": float(temperature),
    }


def extract_openai_response_text(data: dict[str, Any]) -> str:
    choice = ((data.get("choices") or [{}])[0] if isinstance(data, dict) else {}) or {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                texts.append(text)
        return "\n".join(texts).strip()
    return ""


def extract_anthropic_response_text(data: dict[str, Any]) -> str:
    items = data.get("content") if isinstance(data, dict) else []
    texts: list[str] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def provider_response_diagnostics(data: dict[str, Any]) -> dict[str, Any]:
    """Expose compact response shape hints without depending on hidden reasoning."""
    if not isinstance(data, dict):
        return {"shape": type(data).__name__}
    diagnostics: dict[str, Any] = {"top_keys": sorted(str(key) for key in data.keys())[:12]}
    content = data.get("content")
    if isinstance(content, list):
        diagnostics["content_types"] = [
            str(item.get("type") or ("thinking" if item.get("thinking") else "unknown"))
            for item in content
            if isinstance(item, dict)
        ][:8]
        diagnostics["thinking_chars"] = sum(
            len(str(item.get("thinking") or ""))
            for item in content
            if isinstance(item, dict)
        )
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = ((choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}) or {}
        if isinstance(message, dict):
            diagnostics["message_keys"] = sorted(str(key) for key in message.keys())[:12]
            diagnostics["reasoning_chars"] = len(str(message.get("reasoning_content") or ""))
            diagnostics["content_chars"] = len(str(message.get("content") or ""))
    return diagnostics


def run_customer_image_understanding_provider(
    *,
    api_key: str,
    base_url: str,
    model: str,
    request_style: str,
    prompt: str,
    image_paths: list[str],
    timeout_seconds: int,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    image_payloads: list[Any] | None = None,
) -> dict[str, Any]:
    clean_style = str(request_style or "openai_chat_vision").strip().lower()
    if [str(path).strip() for path in image_paths if str(path).strip()]:
        return {
            "ok": False,
            "status": 0,
            "error": "legacy_image_path_input_rejected",
            "provider": "",
            "model": model,
            "request_style": clean_style,
        }
    try:
        if clean_style == "anthropic_messages_vision":
            payload = build_anthropic_messages_vision_payload(
                model=model,
                prompt=prompt,
                image_paths=image_paths,
                max_tokens=max_tokens,
                temperature=temperature,
                image_payloads=image_payloads,
            )
            url = str(base_url or "").rstrip("/") + "/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            extractor = extract_anthropic_response_text
        else:
            payload = build_openai_chat_vision_payload(
                model=model,
                prompt=prompt,
                image_paths=image_paths,
                max_tokens=max_tokens,
                temperature=temperature,
                image_payloads=image_payloads,
            )
            url = str(base_url or "").rstrip("/") + "/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            extractor = extract_openai_response_text
    except (ImagePayloadError, Image.DecompressionBombError, OSError, ValueError, Warning) as exc:
        return {
            "ok": False,
            "status": 0,
            "error": "customer_image_understanding_image_payload_invalid",
            "response_diagnostics": {"image_payload_error": str(exc)[:240]},
            "provider": "",
            "model": model,
            "request_style": clean_style,
        }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds or 1))) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": int(getattr(exc, "code", 0) or 0),
            "error": body[:1200],
            "provider": "",
            "model": model,
            "request_style": clean_style,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "error": repr(exc),
            "provider": "",
            "model": model,
            "request_style": clean_style,
        }
    response_text = extractor(data)
    parsed = parse_llm_json_object(response_text)
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "status": 200,
            "error": "customer_image_understanding_response_not_json_object",
            "response_text": response_text[:1200],
            "response_diagnostics": provider_response_diagnostics(data),
            "model": model,
            "request_style": clean_style,
        }
    return {
        "ok": True,
        "status": 200,
        "response_text": response_text[:1200],
        "parsed": parsed,
        "model": model,
        "request_style": clean_style,
    }
