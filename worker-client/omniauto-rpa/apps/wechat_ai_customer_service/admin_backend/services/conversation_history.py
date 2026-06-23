"""Conversation history assembler for LLM context.

Reads persisted messages from RawMessageStore, formats them into chronological
rounds, and trims to a character budget. Produces a three-layer context:
1. Session summary (~200 chars) — from customer profile
2. Recent N rounds (~budget chars) — from RawMessageStore
3. Current batch — passed in by caller
"""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.wechat_message_envelope import message_is_visual_or_media_ocr_noise


DEFAULT_MAX_ROUNDS = 12
DEFAULT_CHAR_BUDGET = 3500
DEFAULT_MAX_MESSAGES = 40


class ConversationHistoryAssembler:
    """Assemble conversation history for a specific target."""

    def __init__(
        self,
        *,
        target_name: str,
        conversation_id: str = "",
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        char_budget: int = DEFAULT_CHAR_BUDGET,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.target_name = target_name
        self.conversation_id = conversation_id
        self.max_rounds = max(1, max_rounds)
        self.char_budget = max(500, char_budget)
        self.max_messages = max(1, max_messages)

    def assemble(self, *, current_batch: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Return full history context package."""
        rounds = self._load_rounds()
        history_text = self._format_rounds(rounds)
        current = self._format_current_batch(current_batch or [])
        return {
            "target_name": self.target_name,
            "round_count": len(rounds),
            "history_chars": len(history_text),
            "history_text": history_text,
            "current_batch_text": current,
            "rounds": rounds,
        }

    def _load_rounds(self) -> list[dict[str, Any]]:
        """Load messages from store and group into rounds."""
        if not self.conversation_id:
            return []
        try:
            messages = RawMessageStore().list_messages(
                conversation_id=self.conversation_id,
                limit=self.max_messages,
            )
        except Exception:
            return []

        # Sort chronologically (oldest first)
        messages = sorted(
            [m for m in messages if isinstance(m, dict)],
            key=lambda m: str(m.get("observed_at") or m.get("message_time") or ""),
        )

        # Group consecutive messages by same sender into rounds
        rounds: list[dict[str, Any]] = []
        for msg in messages:
            if message_is_visual_or_media_ocr_noise(msg):
                continue
            sender = str(msg.get("sender") or msg.get("sender_role") or "unknown")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if rounds and rounds[-1]["sender"] == sender:
                rounds[-1]["content"] += "\n" + content
            else:
                rounds.append({
                    "sender": sender,
                    "content": content,
                    "time": str(msg.get("message_time") or msg.get("observed_at") or ""),
                    "sender_role": str(msg.get("sender_role") or ""),
                })

        # Trim to max_rounds, keeping most recent
        if len(rounds) > self.max_rounds:
            rounds = rounds[-self.max_rounds:]

        # Apply char budget, dropping oldest rounds first
        rounds = self._trim_by_budget(rounds)
        return rounds

    def _trim_by_budget(self, rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trim oldest rounds until total chars fit budget."""
        total = sum(len(str(r.get("content") or "")) for r in rounds)
        while rounds and total > self.char_budget:
            removed = rounds.pop(0)
            total -= len(str(removed.get("content") or ""))
        return rounds

    def _format_rounds(self, rounds: list[dict[str, Any]]) -> str:
        """Format rounds as readable conversation text."""
        lines: list[str] = []
        for r in rounds:
            sender = str(r.get("sender") or "")
            content = str(r.get("content") or "").strip()
            if not content:
                continue
            # Map sender_role to label
            role = str(r.get("sender_role") or "")
            if role == "self" or sender == "self":
                label = "客服"
            elif role == "bot":
                label = "AI客服"
            else:
                label = sender or "客户"
            lines.append(f"[{label}] {content}")
        return "\n".join(lines)

    def _format_current_batch(self, batch: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for msg in batch:
            if not isinstance(msg, dict):
                continue
            if message_is_visual_or_media_ocr_noise(msg):
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            sender = str(msg.get("sender") or "客户")
            lines.append(f"[{sender}] {content}")
        return "\n".join(lines)


def assemble_conversation_history(
    *,
    target_name: str,
    conversation_id: str = "",
    current_batch: list[dict[str, Any]] | None = None,
    customer_summary: str = "",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """High-level helper: assemble three-layer context for LLM prompt.

    Returns dict with:
    - summary: customer conversation summary (from profile)
    - history_text: recent conversation rounds
    - current_batch_text: current unprocessed messages
    - total_chars: approximate total character count
    """
    cfg = config or {}
    history_cfg = cfg.get("customer_profiles", {}).get("history", {}) if isinstance(cfg.get("customer_profiles"), dict) else {}
    max_rounds = int(history_cfg.get("max_rounds_in_context", DEFAULT_MAX_ROUNDS) or DEFAULT_MAX_ROUNDS)
    char_budget = int(history_cfg.get("max_chars_in_context", DEFAULT_CHAR_BUDGET) or DEFAULT_CHAR_BUDGET)

    assembler = ConversationHistoryAssembler(
        target_name=target_name,
        conversation_id=conversation_id,
        max_rounds=max_rounds,
        char_budget=char_budget,
    )
    result = assembler.assemble(current_batch=current_batch or [])

    summary = str(customer_summary or "").strip()
    history_text = result["history_text"]
    current_text = result["current_batch_text"]

    total_chars = len(summary) + len(history_text) + len(current_text)

    return {
        "summary": summary,
        "history_text": history_text,
        "current_batch_text": current_text,
        "total_chars": total_chars,
        "round_count": result["round_count"],
        "history_chars": result["history_chars"],
    }
