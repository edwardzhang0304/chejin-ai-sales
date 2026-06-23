"""Operational analytics for the WeChat RAG auxiliary layer."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service"


class RagOperationsAnalyzer:
    def __init__(
        self,
        *,
        rag_service: RagService | None = None,
        experience_store: RagExperienceStore | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self.rag = rag_service or RagService()
        self.experiences = experience_store or RagExperienceStore()
        self.runtime_root = runtime_root or DEFAULT_RUNTIME_ROOT

    def report(self, *, audit_limit: int = 2000) -> dict[str, Any]:
        status = self.rag.status()
        sources = self.rag.list_sources()
        chunks = self.rag.iter_chunks()
        experience_counts = self.experiences.counts()
        audit = self.audit_summary(limit=audit_limit)
        active_experiences = self.experiences.list(status="active", limit=500)
        return {
            "ok": True,
            "schema_version": 1,
            "rag_status": {
                "source_count": status.get("source_count", 0),
                "chunk_count": status.get("chunk_count", 0),
                "index_entry_count": status.get("index_entry_count", 0),
                "index_exists": status.get("index_exists", False),
                "updated_at": status.get("updated_at", ""),
            },
            "source_type_counts": dict(Counter(str(item.get("source_type") or "unknown") for item in sources)),
            "category_counts": dict(Counter(str(item.get("category") or "unknown") for item in sources)),
            "chunk_layer_counts": dict(Counter(str(item.get("layer") or "unknown") for item in chunks)),
            "experience_counts": experience_counts,
            "audit": audit,
            "formalization_candidates": formalization_candidates(active_experiences),
        }

    def audit_summary(self, *, limit: int = 2000) -> dict[str, Any]:
        events = self.read_audit_events(limit=limit)
        counters = Counter()
        reasons = Counter()
        intents = Counter()
        for event in events:
            rag_reply = event.get("rag_reply", {}) or {}
            rag_evidence = (event.get("intent_assist", {}) or {}).get("evidence", {}).get("rag_hits", []) or []
            if rag_reply.get("applied"):
                counters["rag_reply_applied"] += 1
            if rag_evidence:
                counters["rag_evidence_hit"] += 1
            if rag_reply and not rag_reply.get("applied"):
                reasons[str(rag_reply.get("reason") or "unknown")] += 1
            if event.get("rag_experience"):
                counters["rag_experience_recorded"] += 1
            intent = str((event.get("intent_assist", {}) or {}).get("intent") or "")
            if intent:
                intents[intent] += 1
        return {
            "event_count": len(events),
            "counters": dict(counters),
            "rag_reply_block_reasons": dict(reasons),
            "intent_counts": dict(intents),
        }

    def read_audit_events(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        log_root = self.runtime_root / "logs"
        if not log_root.exists():
            return []
        events: list[dict[str, Any]] = []
        for path in sorted(log_root.glob("*audit*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            events.extend(read_jsonl_tail(path, limit=max(1, limit - len(events))))
            if len(events) >= limit:
                break
        return events[:limit]


def formalization_candidates(experiences: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    candidates = []
    for item in experiences:
        usage = item.get("usage", {}) or {}
        reply_count = int(usage.get("reply_count", 1) or 1)
        hit = item.get("rag_hit", {}) or {}
        risk_terms = [str(value) for value in hit.get("risk_terms", []) or [] if str(value)]
        if risk_terms:
            continue
        if reply_count < 2:
            continue
        candidates.append(
            {
                "experience_id": item.get("experience_id"),
                "summary": item.get("summary"),
                "reply_count": reply_count,
                "product_id": hit.get("product_id") or "",
                "category": hit.get("category") or "",
                "recommended_action": "review_for_formal_knowledge",
            }
        )
    candidates.sort(key=lambda item: int(item.get("reply_count") or 0), reverse=True)
    return candidates[:limit]


def read_jsonl_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events
