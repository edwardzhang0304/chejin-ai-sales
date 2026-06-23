"""Checks for provider-neutral LLM output normalization."""

from __future__ import annotations

import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from llm_output_adapter import (  # noqa: E402
    extract_first_json_object_text,
    llm_adapter_profile,
    parse_llm_json_object,
    strip_markdown_code_fence,
)


def assert_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: object, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> int:
    payload = {"reply": "在的，您说。", "confidence": 0.98, "reason": "short_social"}
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    assert_equal(strip_markdown_code_fence(fenced), json.dumps(payload, ensure_ascii=False), "fenced JSON should be stripped")
    assert_equal(parse_llm_json_object(fenced), payload, "fenced JSON should parse")
    wrapped = "好的，我按JSON给你：\n" + json.dumps(payload, ensure_ascii=False) + "\n以上。"
    assert_equal(parse_llm_json_object(wrapped), payload, "wrapped JSON should parse")
    truncated = '{"can_answer":true,"reply_segments":["在的，您说。"],"recommended_action":"send_reply","reason":"客户只是问候'
    parsed_truncated = parse_llm_json_object(truncated)
    assert_true(isinstance(parsed_truncated, dict), "truncated JSON-like object should be repaired only if valid after local closure")
    assert_equal(parsed_truncated.get("reply_segments"), ["在的，您说。"], "truncated repair must preserve emitted Brain reply segments")
    truncated_after_complete_reply = (
        '{"can_answer":true,"reply_segments":["推荐两台省油靠谱的：秦PLUS 8.68万，凯美瑞8.98万。",'
        '"两台都在预算内，方便的话可以约看车。"],"recommended_action":"send_reply","risk"'
    )
    parsed_reply_first = parse_llm_json_object(truncated_after_complete_reply)
    assert_true(isinstance(parsed_reply_first, dict), "truncated tail after complete reply fields should be recovered")
    assert_equal(
        parsed_reply_first.get("reply_segments"),
        ["推荐两台省油靠谱的：秦PLUS 8.68万，凯美瑞8.98万。", "两台都在预算内，方便的话可以约看车。"],
        "tail repair must keep completed Brain reply segments",
    )
    nested = '{"outer":{"text":"包含 } 字符串"},"ok":true} trailing'
    assert_equal(extract_first_json_object_text(nested), '{"outer":{"text":"包含 } 字符串"},"ok":true}', "balanced object should respect strings")
    kimi = llm_adapter_profile(provider="anthropic", model="kimi-for-coding", request_style="anthropic_messages")
    deepseek = llm_adapter_profile(provider="deepseek", model="deepseek-v4-flash", request_style="openai_chat")
    assert_equal(kimi.get("id"), "kimi_anthropic_messages", "Kimi profile should be detected")
    assert_equal(deepseek.get("id"), "deepseek_v4_flash_fallback", "DeepSeek flash profile should be detected")
    assert_true(parse_llm_json_object("not json") is None, "non JSON should stay None")
    assert_true(parse_llm_json_object("{我改好了，但不是JSON") is None, "prose starting with brace should not repair to empty object")
    print(json.dumps({"ok": True, "checks": 10}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
