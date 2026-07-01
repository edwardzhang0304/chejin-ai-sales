from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings
from app.errors import AppError


@dataclass(frozen=True)
class AIEngineDecision:
    decision: str
    reply_text: str | None = None
    confidence: float | None = None
    handoff_reason_code: str | None = None
    risk_flags: list[str] | None = None
    evidence_refs: list[str] | None = None
    guard_result: str | None = None
    rewrite_required: bool = False
    error_code: str | None = None
    suggested_action: str | None = None
    raw_payload: dict | None = None


class OmniAutoAIEngineAdapter(Protocol):
    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        """Return a structured service-side decision. The adapter must not mutate business state."""


class MockOmniAutoAIEngineAdapter:
    """Development adapter for C3 workflow tests. Not a formal delivery substitute for real AI Engine."""

    HANDOFF_KEYWORDS = ("人工", "销售电话", "底价", "最低价", "事故", "泡水", "火烧", "贷款", "定金", "合同", "退款", "投诉")

    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        messages = message_batch.get("messages") or []
        combined = "\n".join(str(item.get("content") or "") for item in messages).strip()
        if not combined:
            return AIEngineDecision(
                decision="no_action",
                confidence=0.0,
                guard_result="pass",
                error_code="MESSAGE_BATCH_EMPTY",
                suggested_action="wait_more",
                raw_payload={"adapter": "mock"},
            )
        if "无证据" in combined or "RAG_NO_EVIDENCE" in combined:
            return AIEngineDecision(
                decision="handoff",
                confidence=0.2,
                handoff_reason_code="RAG_NO_EVIDENCE",
                risk_flags=["rag_no_evidence"],
                evidence_refs=[],
                guard_result="handoff",
                error_code="RAG_NO_EVIDENCE",
                suggested_action="handoff",
                raw_payload={"adapter": "mock"},
            )
        if any(keyword in combined for keyword in self.HANDOFF_KEYWORDS):
            return AIEngineDecision(
                decision="handoff",
                confidence=0.7,
                handoff_reason_code="HANDOFF_REQUIRED",
                risk_flags=["manual_handoff_keyword"],
                evidence_refs=["guard_keyword_policy"],
                guard_result="handoff",
                error_code="HANDOFF_REQUIRED",
                suggested_action="handoff",
                raw_payload={"adapter": "mock"},
            )
        if "不回复" in combined or "先看看" in combined:
            return AIEngineDecision(
                decision="no_action",
                confidence=0.65,
                guard_result="pass",
                evidence_refs=["conversation_policy"],
                suggested_action="no_action",
                raw_payload={"adapter": "mock"},
            )
        return AIEngineDecision(
            decision="send_reply",
            reply_text="可以，我先帮您记录需求。您主要看几万预算、轿车还是 SUV？",
            confidence=0.86,
            risk_flags=[],
            evidence_refs=["mock_kb_basic_need_collection"],
            guard_result="pass",
            raw_payload={"adapter": "mock", "model": "mock-omniauto-ai-engine"},
        )


class RealOmniAutoAIEngineAdapter:
    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        raise AppError(
            "AI_ENGINE_UNAVAILABLE",
            "真实 OmniAuto AI Engine Adapter 尚未配置，不能作为正式 C3 交付",
            503,
            {"suggested_action": "configure_real_omniauto_ai_engine"},
        )


def get_ai_engine_adapter() -> OmniAutoAIEngineAdapter:
    mode = getattr(get_settings(), "c3_ai_adapter_mode", "mock")
    if mode == "mock":
        return MockOmniAutoAIEngineAdapter()
    if mode == "real":
        return RealOmniAutoAIEngineAdapter()
    raise AppError("AI_ENGINE_UNAVAILABLE", "未知 AI Adapter 模式", 503, {"mode": mode, "suggested_action": "check_config"})
