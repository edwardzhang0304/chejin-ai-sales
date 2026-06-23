"""Guards that keep test/live service chatter out of learnable knowledge.

The customer-service listener can observe WeChat messages for reply context and
audit, but observed chatter is not automatically trusted training data. This
module centralizes the source-quality rules so raw message capture, raw learning
and RAG retrieval all apply the same contamination boundary.
"""

from __future__ import annotations

import re
from typing import Any

from apps.wechat_ai_customer_service.wechat_message_envelope import message_has_learning_blocking_quality, visual_ocr_noise_reason


FILE_TRANSFER_ASSISTANT_NAME = "文件传输助手"

TEST_MARKER_RE = re.compile(
    r"("
    r"(?:REALWX|GENLIVE\d*D?|LIVEFLOW|FRESHLONG|CONVHANDOFF|ADAPTGEN|LLMSYN|BURST|DBG|LIVE_RECORDER|TEST)[_-]"
    r"|CHEJIN_\d{8}_\d{6}"
    r"|CHEJIN_DEMO_\d{8}_\d{6}"
    r"|(?:演示|测试)批次[:：]?\s*[A-Z0-9_\\-]+"
    r"|文件传输助手"
    r")",
    re.IGNORECASE,
)

MODEL_REPLY_RE = re.compile(
    r"(^\s*\[[^\]]*(?:AI|机器人|客服)\]\s*)"
    r"|(\[车金AI\])"
    r"|(llm_synthesis_reply|rag_context_reply)"
    r"|(系统提示词|内部规则|不是AI|不是机器人)",
    re.IGNORECASE,
)

BLOCKED_RAG_SOURCE_TYPES = {"wechat_raw_message"}
BLOCKED_RAG_CATEGORIES = {"products", "erp_exports"}


def text_has_test_marker(text: str) -> bool:
    return bool(TEST_MARKER_RE.search(str(text or "")))


def text_has_model_reply_marker(text: str) -> bool:
    return bool(MODEL_REPLY_RE.search(str(text or "")))


def message_learning_exclusion_reason(
    message: dict[str, Any],
    *,
    conversation: dict[str, Any] | None = None,
    source_module: str = "",
) -> str:
    """Return an exclusion reason, or an empty string when the message may learn."""

    conversation = conversation or {}
    content = str(message.get("content") or message.get("text") or "").strip()
    if not content:
        return "empty_message"
    content_type = str(message.get("content_type") or message.get("type") or "text").strip().lower()
    if content_type and content_type != "text":
        return "non_text_message"
    visual_reason = visual_ocr_noise_reason(message)
    if visual_reason:
        return visual_reason
    quality_reason = message_has_learning_blocking_quality(message)
    if quality_reason:
        return quality_reason

    conversation_type = str(
        conversation.get("conversation_type")
        or conversation.get("type")
        or message.get("conversation_type")
        or ""
    ).strip()
    target_name = str(
        conversation.get("target_name")
        or conversation.get("display_name")
        or message.get("target_name")
        or ""
    ).strip()
    if conversation_type == "file_transfer" or target_name == FILE_TRANSFER_ASSISTANT_NAME:
        return "file_transfer_test_channel"

    sender_role = str(message.get("sender_role") or "").strip().lower()
    sender = str(message.get("sender") or "").strip().lower()
    if sender_role == "self" or sender == "self" or bool(message.get("is_self")):
        return "self_message_not_training_data"

    if text_has_model_reply_marker(content):
        return "model_reply_marker"
    if text_has_test_marker(content):
        return "synthetic_test_marker"

    if (
        str(source_module or "").strip() == "customer_service"
        and conversation.get("allow_learning_from_customer_service") is not True
    ):
        return "customer_service_live_learning_disabled"

    return ""


def message_is_learnable(
    message: dict[str, Any],
    *,
    conversation: dict[str, Any] | None = None,
    source_module: str = "",
) -> bool:
    return not message_learning_exclusion_reason(
        message,
        conversation=conversation,
        source_module=source_module,
    )


def transcript_learning_exclusion_reason(text: str) -> str:
    if not str(text or "").strip():
        return "empty_transcript"
    if text_has_model_reply_marker(text):
        return "transcript_contains_model_reply"
    if text_has_test_marker(text):
        return "transcript_contains_test_marker"
    return ""


def rag_chunk_exclusion_reason(chunk: dict[str, Any]) -> str:
    source_type = str(chunk.get("source_type") or "").strip()
    category = str(chunk.get("category") or "").strip()
    source_path = str(chunk.get("source_path") or "")
    text = str(chunk.get("text") or "")
    if source_type in BLOCKED_RAG_SOURCE_TYPES:
        return "raw_wechat_source_not_directly_retrievable"
    if category in BLOCKED_RAG_CATEGORIES:
        return "product_master_not_rag_retrievable"
    if "raw_messages" in source_path.replace("\\", "/"):
        return "raw_message_path_not_directly_retrievable"
    if category == "chats" and "raw_inbox/chats" in source_path.replace("\\", "/"):
        return "chat_upload_requires_reviewed_experience"
    if text_has_model_reply_marker(text):
        return "rag_chunk_contains_model_reply"
    if text_has_test_marker(text):
        return "rag_chunk_contains_test_marker"
    return ""


def rag_chunk_is_retrievable(chunk: dict[str, Any]) -> bool:
    if str(chunk.get("status") or "active") != "active":
        return False
    return not rag_chunk_exclusion_reason(chunk)


def compact_exclusion_flags(message: dict[str, Any], *, conversation: dict[str, Any] | None = None, source_module: str = "") -> dict[str, Any]:
    reason = message_learning_exclusion_reason(message, conversation=conversation, source_module=source_module)
    return {
        "learnable": not reason,
        "excluded_reason": reason,
        "has_test_marker": text_has_test_marker(str(message.get("content") or message.get("text") or "")),
        "has_model_reply_marker": text_has_model_reply_marker(str(message.get("content") or message.get("text") or "")),
    }
