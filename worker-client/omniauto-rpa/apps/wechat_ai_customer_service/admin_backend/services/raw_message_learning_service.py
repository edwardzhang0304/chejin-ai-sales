"""Turn raw WeChat message batches into review-only RAG material."""

from __future__ import annotations

import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from .knowledge_contamination_guard import (
    message_learning_exclusion_reason,
    transcript_learning_exclusion_reason,
)
from .rag_experience_auto_review import auto_review_rag_experience
from .raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.knowledge_paths import tenant_runtime_root


APP_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
if str(WORKFLOWS_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_ROOT))

from rag_layer import RagService  # noqa: E402
from rag_experience_store import RagExperienceStore  # noqa: E402


class RawMessageLearningService:
    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.raw_store = RawMessageStore(tenant_id=tenant_id)
        self.rag = RagService(tenant_id=self.raw_store.tenant_id)
        self.rag_experiences = RagExperienceStore(tenant_id=self.raw_store.tenant_id)

    def process_pending(self, *, limit: int = 10, use_llm: bool = True) -> dict[str, Any]:
        pending = [
            batch
            for batch in self.raw_store.list_batches(limit=500)
            if str(batch.get("status") or "pending") == "pending"
        ][: max(1, min(int(limit or 10), 50))]
        results = [self.process_batch(str(batch.get("batch_id") or ""), use_llm=use_llm) for batch in pending]
        return {
            "ok": True,
            "processed_count": len(results),
            "candidate_count": sum(int(item.get("candidate_count", 0) or 0) for item in results),
            "items": results,
        }

    def process_batch(self, batch_id: str, *, use_llm: bool = True) -> dict[str, Any]:
        batch = self.raw_store.get_batch(batch_id)
        if not batch:
            raise FileNotFoundError(batch_id)
        if str(batch.get("status") or "pending") == "processed" and batch.get("rag_experience_id"):
            return {"ok": True, "already_processed": True, **batch}

        message_ids = {str(item) for item in batch.get("message_ids", []) or [] if str(item)}
        messages = [
            item
            for item in self.raw_store.list_messages(conversation_id=str(batch.get("conversation_id") or ""), limit=500)
            if not message_ids or str(item.get("raw_message_id") or "") in message_ids
        ]
        if not messages:
            updated = self.raw_store.update_batch(
                batch_id,
                {"status": "skipped", "skipped_reason": "no_messages", "processed_at": now_iso()},
            )
            return {"ok": True, "candidate_count": 0, "batch": updated}

        guard_reason = batch_learning_exclusion_reason(batch, messages)
        if guard_reason:
            updated = self.raw_store.update_batch(
                batch_id,
                {
                    "status": "skipped",
                    "skipped_reason": guard_reason,
                    "processed_at": now_iso(),
                    "candidate_ids": [],
                    "candidate_count": 0,
                    "strict_promotion_policy": "raw_message_skipped_by_contamination_guard",
                },
            )
            append_audit(
                "raw_message_batch_skipped",
                {
                    "batch_id": batch_id,
                    "conversation_id": batch.get("conversation_id"),
                    "reason": guard_reason,
                },
            )
            return {"ok": True, "candidate_count": 0, "candidate_ids": [], "batch": updated, "skipped_reason": guard_reason}

        transcript_path = self.write_transcript(batch, messages)
        rag_ingest = {
            "ok": False,
            "source_id": "",
            "chunk_count": 0,
            "category": str(messages[0].get("conversation_type") or "unknown"),
            "source_type": "wechat_raw_message",
            "message": "deferred_until_rag_experience_review",
        }
        source_type = raw_source_type(messages)
        experience = self.rag_experiences.record_intake(
            source_type=source_type,
            source_path=str(transcript_path),
            category=str(messages[0].get("conversation_type") or "unknown"),
            evidence_excerpt=transcript_excerpt(transcript_path),
            rag_ingest=rag_ingest,
            candidate_ids=[],
            original_source={
                "raw_batch_id": batch.get("batch_id"),
                "conversation_id": batch.get("conversation_id"),
                "conversation_type": str(messages[0].get("conversation_type") or "unknown"),
                "raw_message_ids": [str(item.get("raw_message_id") or "") for item in messages],
            },
        )
        reviewed_experience = auto_review_rag_experience(experience, store=self.rag_experiences, force=False, use_llm=use_llm)

        updated_batch = self.raw_store.update_batch(
            batch_id,
            {
                "status": "processed",
                "processed_at": now_iso(),
                "transcript_path": str(transcript_path),
                "candidate_ids": [],
                "candidate_count": 0,
                "skipped_duplicate_count": 0,
                "skipped_duplicates": [],
                "skipped_source_policy_count": 0,
                "skipped_source_policy": [],
                "llm_assist_policy": {
                "policy_version": "knowledge_llm_assist_v1",
                "stage": "raw_wechat_message_to_rag_experience_review",
                "requested": bool(use_llm),
                "rule_fallback_allowed": True,
                "human_approval_required": True,
                    "strict_promotion_policy": "raw WeChat messages create review-only AI experience pool items; retrievable chunks are created only after manual keep/promotion",
                },
                "rag_ingest": rag_ingest,
                "rag_experience_id": experience.get("experience_id"),
                "strict_promotion_policy": "rag_experience_manual_promotion_only",
            },
        )
        append_audit(
            "raw_message_batch_learned",
            {
                "batch_id": batch_id,
                "conversation_id": batch.get("conversation_id"),
                "candidate_count": 0,
                "rag_experience_id": experience.get("experience_id"),
                "skipped_duplicate_count": 0,
                "skipped_source_policy_count": 0,
            },
        )
        return {
            "ok": True,
            "batch": updated_batch,
            "candidate_count": 0,
            "candidate_ids": [],
            "rag_experience_id": experience.get("experience_id"),
            "rag_experience": reviewed_experience,
            "skipped_duplicate_count": 0,
            "skipped_source_policy_count": 0,
            "skipped_source_policy": [],
        }

    def write_transcript(self, batch: dict[str, Any], messages: list[dict[str, Any]]) -> Path:
        root = tenant_runtime_root(self.raw_store.tenant_id) / "raw_messages" / "learning_batches"
        root.mkdir(parents=True, exist_ok=True)
        batch_id = str(batch.get("batch_id") or "raw_batch")
        path = root / f"{batch_id}.txt"
        ordered = sorted(messages, key=lambda item: str(item.get("message_time") or item.get("observed_at") or ""))
        lines = []
        for item in ordered:
            sender = str(item.get("sender") or item.get("sender_role") or "unknown")
            timestamp = str(item.get("message_time") or item.get("observed_at") or "")
            content = str(item.get("content") or "").strip()
            if content:
                lines.append(f"[{timestamp}] {sender}: {content}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def annotate_candidate(self, candidate: dict[str, Any], *, batch: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        conversation_type = str(messages[0].get("conversation_type") or "unknown") if messages else "unknown"
        if conversation_type == "group":
            source_type = "raw_wechat_group"
            conversation_tag = "wechat_group_chat"
        elif conversation_type == "file_transfer":
            source_type = "raw_wechat_file_transfer"
            conversation_tag = "wechat_file_transfer"
        else:
            source_type = "raw_wechat_private"
            conversation_tag = "wechat_private_chat"
        source = candidate.setdefault("source", {})
        source.update(
            {
                "type": source_type,
                "raw_batch_id": batch.get("batch_id"),
                "conversation_id": batch.get("conversation_id"),
                "raw_message_ids": [str(item.get("raw_message_id") or "") for item in messages],
                "contains_model_reply": contains_model_reply(messages),
                "sender_roles": sorted({message_sender_role(item) for item in messages}),
            }
        )
        review = candidate.setdefault("review", {})
        review.update(
            {
                "source_raw_batch_id": batch.get("batch_id"),
                "source_raw_message_ids": [str(item.get("raw_message_id") or "") for item in messages],
                "source_contains_model_reply": contains_model_reply(messages),
                "requires_human_approval": True,
                "allowed_auto_apply": False,
            }
        )
        tags = [str(item) for item in candidate.get("detected_tags", []) or []]
        tags.append(conversation_tag)
        candidate["detected_tags"] = sorted(set(tag for tag in tags if tag))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def batch_learning_exclusion_reason(batch: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "no_messages"
    conversation = {
        "conversation_id": batch.get("conversation_id"),
        "conversation_type": str(messages[0].get("conversation_type") or "unknown"),
        "target_name": str(messages[0].get("target_name") or ""),
    }
    reasons = [
        message_learning_exclusion_reason(
            item,
            conversation=conversation,
            source_module=str(batch.get("source_module") or ""),
        )
        for item in messages
    ]
    if reasons and all(reasons):
        return "all_messages_unlearnable:" + ",".join(sorted(set(reasons))[:4])
    transcript = "\n".join(str(item.get("content") or "") for item in messages)
    transcript_reason = transcript_learning_exclusion_reason(transcript)
    if transcript_reason:
        return transcript_reason
    if contains_model_reply(messages):
        return "contains_model_reply"
    return ""


def raw_source_type(messages: list[dict[str, Any]]) -> str:
    conversation_type = str(messages[0].get("conversation_type") or "unknown") if messages else "unknown"
    if conversation_type == "group":
        return "raw_wechat_group"
    if conversation_type == "file_transfer":
        return "raw_wechat_file_transfer"
    return "raw_wechat_private"


def message_sender_role(message: dict[str, Any]) -> str:
    sender = str(message.get("sender_role") or message.get("sender") or "").strip().lower()
    if sender in {"self", "system", "bot", "assistant", "ai"}:
        return sender
    return "other" if sender else "unknown"


def contains_model_reply(messages: list[dict[str, Any]]) -> bool:
    marker_pattern = re.compile(r"^\s*\[[^\]]*(?:AI|客服)\]\s*", re.I)
    for item in messages:
        content = str(item.get("content") or "")
        sender = message_sender_role(item)
        if marker_pattern.search(content) or "llm_synthesis_reply" in content or "rag_context_reply" in content:
            return True
        if sender in {"bot", "assistant", "ai"}:
            return True
    return False


def transcript_excerpt(path: Path, *, limit: int = 2400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""
