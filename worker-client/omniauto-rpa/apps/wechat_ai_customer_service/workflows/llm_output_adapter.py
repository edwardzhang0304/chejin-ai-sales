"""Provider-neutral LLM output adapters.

This module belongs to the code-mechanism layer.  It normalizes model output
format only; it must never author, replace, or decide customer-visible wording.
"""

from __future__ import annotations

import json
from typing import Any


def llm_adapter_profile(*, provider: Any = "", model: Any = "", request_style: Any = "") -> dict[str, Any]:
    provider_text = str(provider or "").strip().lower()
    model_text = str(model or "").strip().lower()
    style_text = str(request_style or "").strip().lower()
    if "kimi" in model_text or "moonshot" in model_text:
        return {
            "id": "kimi_anthropic_messages" if provider_text == "anthropic" or style_text == "anthropic_messages" else "kimi_openai_compatible",
            "label": "Kimi 输出适配",
            "notes": [
                "兼容 Anthropic Messages / Kimi 响应。",
                "已启用 Markdown JSON 代码块清洗。",
                "已启用首个平衡 JSON 对象提取。",
            ],
        }
    if provider_text == "deepseek" or model_text.startswith("deepseek"):
        return {
            "id": "deepseek_v4_flash_fallback" if "flash" in model_text else "deepseek_openai_compatible",
            "label": "DeepSeek OpenAI 兼容适配",
            "notes": [
                "使用 OpenAI-compatible chat/completions 协议。",
                "适合作为 transient failure 备用链路。",
                "JSON 输出同样经过通用清洗与对象提取。",
            ],
        }
    if style_text == "anthropic_messages" or provider_text == "anthropic":
        return {
            "id": "anthropic_messages_json",
            "label": "Anthropic Messages 适配",
            "notes": ["使用 Anthropic Messages 协议。", "JSON 输出经过通用清洗。"],
        }
    return {
        "id": "generic_json",
        "label": "通用 JSON 适配",
        "notes": ["直接解析 JSON；失败时尝试清洗 Markdown 代码块和提取对象。"],
    }


def strip_markdown_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    if value.endswith("```"):
        first_newline = value.find("\n")
        if first_newline >= 0:
            return value[first_newline + 1 : -3].strip()
    return value


def extract_first_json_object_text(text: str) -> str:
    value = str(text or "")
    start = value.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1].strip()
    return ""


def parse_llm_json_object(text: str) -> dict[str, Any] | None:
    candidates = []
    raw = str(text or "").strip()
    if raw:
        candidates.append(raw)
    fenced = strip_markdown_code_fence(raw)
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    extracted = extract_first_json_object_text(fenced or raw)
    if extracted and extracted not in candidates:
        candidates.append(extracted)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    repaired = repair_truncated_json_object_text(fenced or raw)
    if repaired:
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
    return None


def repair_truncated_json_object_text(text: str) -> str:
    """Best-effort parser-side repair for model output cut off mid-JSON.

    The repaired text is only accepted if ``json.loads`` succeeds afterwards.
    This adapter never invents reply content; it only closes an already emitted
    string/array/object structure so the BrainPlan written by the model can
    continue through normal validation and guard checks.
    """

    value = str(text or "")
    start = value.find("{")
    if start < 0:
        return ""
    value = value[start:].rstrip()
    if not value:
        return ""
    if not looks_like_repairable_json_object_prefix(value):
        return ""
    candidates = [close_truncated_json_prefix(value)]
    for index in reversed(json_comma_positions(value)):
        candidates.append(close_truncated_json_prefix(value[:index]))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return candidate
    return candidates[0] if candidates and candidates[0] != value else ""


def looks_like_repairable_json_object_prefix(text: str) -> bool:
    """Avoid turning arbitrary prose into an empty JSON object.

    A truncation repair is only safe when the model emitted at least one
    object-field pattern.  Plain text such as "我改好了，但不是JSON" should fall
    through to same-capture retry instead of becoming ``{}``.
    """

    value = str(text or "").lstrip()
    if not value.startswith("{"):
        return False
    in_string = False
    escape = False
    last_string_end = -1
    for index, char in enumerate(value):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
                last_string_end = index
            continue
        if char == '"':
            in_string = True
            continue
        if char == ":" and last_string_end >= 0:
            between = value[last_string_end + 1 : index]
            if not between.strip():
                return True
    return False


def close_truncated_json_prefix(text: str) -> str:
    value = str(text or "").rstrip()
    if not value:
        return ""
    output: list[str] = []
    stack: list[str] = []
    in_string = False
    escape = False
    for char in value:
        output.append(char)
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in ("}", "]"):
            if stack and stack[-1] == char:
                stack.pop()
            else:
                return ""
    if in_string:
        while output and output[-1] == "\\":
            output.pop()
        output.append('"')
    while output and output[-1] in {",", ":"}:
        output.pop()
    while stack:
        output.append(stack.pop())
    repaired = "".join(output).strip()
    return repaired if repaired != value else ""


def json_comma_positions(text: str) -> list[int]:
    positions: list[int] = []
    in_string = False
    escape = False
    for index, char in enumerate(str(text or "")):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":  # noqa: SIM114 - explicit state machine is clearer here.
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            positions.append(index)
    return positions
