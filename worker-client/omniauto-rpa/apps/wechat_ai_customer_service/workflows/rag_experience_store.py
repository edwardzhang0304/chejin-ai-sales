"""RAG self-learning experience store.

This store is deliberately separate from the formal structured knowledge bases.
RAG reply experiences are accepted by default for review and retrieval analysis,
but they are never promoted into formal knowledge without a separate human
approval workflow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_root
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config


MAX_RECORDS = 2000
QUALITY_RETRIEVAL_MIN_SCORE = 0.52
QUALITY_RETRIEVAL_MIN_HIT_SCORE = 0.32
QUALITY_REPEATABLE_MIN_HIT_SCORE = 0.24
QUALITY_REPEATABLE_REPLY_COUNT = 3
REFERENCE_CANDIDATE_BANDS = {"medium", "high"}
WINDOWS_TRANSIENT_WRITE_ERRNOS = {13, 22}
WINDOWS_TRANSIENT_WRITE_WINERRORS = {5, 32, 33}
QUALITY_BLOCK_ACTION_TERMS = {
    "handoff",
    "manual",
    "human",
    "operator",
    "approve",
    "approval",
    "reject",
    "refuse",
    "blocked",
    "请示",
    "人工",
    "接管",
    "转人工",
}

# Metadata pollution terms that should not appear in reply_text
QUALITY_METADATA_POLLUTION_TERMS = {
    "风险等级：",
    "政策规则：",
    "触发关键词：",
    "允许自动回复：",
    "必须转人工：",
    "提醒人工客服：",
    "规则名称：",
    "规则类型：",
    "测试批次：",
}

# Nonsense / test content terms
QUALITY_TEST_CONTENT_TERMS = {
    "duplicate candidate probe",
    "暂存缺价测试商品",
    "合并内容测试",
    "测试折叠床",
    "测试折叠椅",
}

AUTO_DISCARD_AUDITED_DELETE_SOURCES = {"rag_reply"}


def write_json_with_retry(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    last_error: OSError | None = None
    for attempt in range(10):
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
            return
        except OSError as exc:
            last_error = exc
            if not transient_write_error(exc):
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        temp.unlink()
    except OSError:
        pass
    if last_error is not None:
        raise last_error


def transient_write_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    return getattr(exc, "errno", None) in WINDOWS_TRANSIENT_WRITE_ERRNOS or winerror in WINDOWS_TRANSIENT_WRITE_WINERRORS

INTAKE_PRODUCT_MASTER_CATEGORIES = {"products", "erp_exports"}
INTAKE_CATEGORY_DUPLICATE_THRESHOLD = {
    "products": 1,
    "policies": 2,
    "chats": 3,
}
INTAKE_DEFAULT_DUPLICATE_THRESHOLD = 3
INTAKE_LOW_VALUE_MIN_TEXT_LENGTH = 36
INTAKE_LOW_VALUE_BUSINESS_HINTS = (
    "客户",
    "客服",
    "商品",
    "产品",
    "车型",
    "车源",
    "车况",
    "规则",
    "政策",
    "报价",
    "价格",
    "库存",
    "试驾",
    "到店",
    "转人工",
    "贷款",
    "首付",
    "月供",
    "过户",
)


class RagExperienceStore:
    def __init__(self, *, tenant_id: str | None = None, root: Path | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.root = root or (tenant_root(self.tenant_id) / "rag_experience")

    @property
    def path(self) -> Path:
        return self.root / "experiences.json"

    def list(self, *, status: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        db = postgres_store(self.tenant_id)
        if db:
            return db.list_rag_experiences(self.tenant_id, status=status, limit=limit)
        records = self._read()
        if status and status != "all":
            records = [item for item in records if str(item.get("status") or "active") == status]
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 100), 500))]

    def list_retrievable(self, *, limit: int = 500) -> list[dict[str, Any]]:
        records = []
        for item in self.list_for_counts():
            if str(item.get("status") or "active") != "active":
                continue
            enriched = with_quality(item)
            if experience_is_retrievable(enriched):
                records.append(enriched)
        records.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 500), MAX_RECORDS))]

    def list_reference_candidates(self, *, limit: int = 500) -> list[dict[str, Any]]:
        records = []
        for item in self.list_for_counts():
            if str(item.get("status") or "active") != "active":
                continue
            enriched = with_quality(item)
            if experience_is_reference_candidate(enriched):
                records.append(enriched)
        records.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit or 500), MAX_RECORDS))]

    def list_for_counts(self) -> list[dict[str, Any]]:
        """Return the full local accounting set used for auditable counters."""
        db = postgres_store(self.tenant_id)
        if db:
            return db.list_rag_experiences(self.tenant_id, status="all", limit=MAX_RECORDS)
        return self._read()

    def counts(self) -> dict[str, int]:
        records = self.list_for_counts()
        counts = {"total": len(records), "active": 0, "discarded": 0}
        for item in records:
            status = str(item.get("status") or "active")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def record_reply(
        self,
        *,
        target: str,
        message_ids: list[str],
        question: str,
        reply_text: str,
        raw_reply_text: str,
        intent_assist: dict[str, Any],
        rag_reply: dict[str, Any],
        reply_trace_id: str = "",
    ) -> dict[str, Any]:
        now_text = now()
        hit = rag_reply.get("hit", {}) or {}
        fingerprint = stable_digest(
            "|".join(
                [
                    self.tenant_id,
                    normalize_space(question),
                    str(hit.get("chunk_id") or ""),
                    normalize_space(raw_reply_text or reply_text),
                ]
            ),
            20,
        )
        record = {
            "experience_id": "rag_exp_" + fingerprint,
            "tenant_id": self.tenant_id,
            "status": "active",
            "source": "rag_reply",
            "formal_knowledge_policy": "experience_only_not_formal_knowledge",
            "summary": summarize_experience(question, raw_reply_text or reply_text, hit),
            "question": normalize_space(question),
            "reply_text": normalize_space(raw_reply_text or reply_text),
            "target": target,
            "message_ids": message_ids,
            "reply_trace_id": reply_trace_id,
            "intent": intent_assist.get("intent"),
            "recommended_action": intent_assist.get("recommended_action"),
            "safety": (intent_assist.get("evidence", {}) or {}).get("safety", {}),
            "rag_hit": {
                "chunk_id": hit.get("chunk_id"),
                "source_id": hit.get("source_id"),
                "score": hit.get("score"),
                "category": hit.get("category"),
                "source_type": hit.get("source_type"),
                "product_id": hit.get("product_id"),
                "text": hit.get("text"),
                "risk_terms": hit.get("risk_terms", []),
            },
            "usage": {
                "reply_count": 1,
                "last_used_at": now_text,
            },
            "created_at": now_text,
            "updated_at": now_text,
            "review_state": new_rag_review_state(now_text),
        }
        record["quality"] = score_experience_quality(record)
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        db_existing = None
        if db:
            db_existing = next((item for item in db.list_rag_experiences(self.tenant_id, status="all", limit=500) if item.get("experience_id") == record["experience_id"]), None)
            if db_existing:
                usage = dict(db_existing.get("usage", {}) or {})
                usage["reply_count"] = int(usage.get("reply_count", 1) or 1) + 1
                usage["last_used_at"] = now_text
                db_existing.update(
                    {
                        "status": db_existing.get("status") or "active",
                        "summary": record["summary"],
                        "question": record["question"],
                        "reply_text": record["reply_text"],
                        "target": record["target"],
                        "message_ids": record["message_ids"],
                        "reply_trace_id": record.get("reply_trace_id") or db_existing.get("reply_trace_id"),
                        "intent": record["intent"],
                        "recommended_action": record["recommended_action"],
                        "safety": record["safety"],
                        "rag_hit": record["rag_hit"],
                        "usage": usage,
                        "updated_at": now_text,
                    }
                )
                db_existing["quality"] = score_experience_quality(db_existing)
                db.upsert_rag_experience(db_existing)
                rebuild_rag_index_safely(self.tenant_id, force_sync=True)
                if not config.mirror_files:
                    return db_existing
                record = db_existing
        records = self._read()
        for index, existing in enumerate(records):
            if existing.get("experience_id") == record["experience_id"]:
                usage = dict(existing.get("usage", {}) or {})
                usage["reply_count"] = int(usage.get("reply_count", 1) or 1) + 1
                usage["last_used_at"] = now_text
                existing.update(
                    {
                        "status": existing.get("status") or "active",
                        "summary": record["summary"],
                        "question": record["question"],
                        "reply_text": record["reply_text"],
                        "target": record["target"],
                        "message_ids": record["message_ids"],
                        "reply_trace_id": record.get("reply_trace_id") or existing.get("reply_trace_id"),
                        "intent": record["intent"],
                        "recommended_action": record["recommended_action"],
                        "safety": record["safety"],
                        "rag_hit": record["rag_hit"],
                        "usage": usage,
                        "updated_at": now_text,
                    }
                )
                existing["quality"] = score_experience_quality(existing)
                records[index] = existing
                self._write(records)
                rebuild_rag_index_safely(self.tenant_id, force_sync=True)
                return existing
        # New record: apply real-time LLM quality gate before saving
        _apply_creation_audit(record)
        records.append(record)
        self._write(records)
        rebuild_rag_index_safely(self.tenant_id)
        if db and not db_existing:
            db.upsert_rag_experience(record)
            rebuild_rag_index_safely(self.tenant_id, force_sync=True)
        return record

    def record_intake(
        self,
        *,
        source_type: str,
        source_path: str = "",
        category: str = "",
        evidence_excerpt: str = "",
        rag_ingest: dict[str, Any] | None = None,
        candidate_ids: list[str] | None = None,
        original_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record uploaded or captured source material as a review-only AI experience pool item."""
        now_text = now()
        candidate_ids = [str(item) for item in candidate_ids or [] if str(item)]
        rag_ingest = rag_ingest or {}
        fingerprint = stable_digest(
            "|".join(
                [
                    self.tenant_id,
                    normalize_space(source_type),
                    normalize_space(source_path),
                    normalize_space(category),
                    normalize_space(str((original_source or {}).get("raw_batch_id") or "")),
                    stable_digest(normalize_space(evidence_excerpt), 24),
                    stable_digest("|".join(candidate_ids), 24),
                ]
            ),
            20,
        )
        record = {
            "experience_id": "rag_exp_" + fingerprint,
            "tenant_id": self.tenant_id,
            "status": "active",
            "source": "intake",
            "source_type": source_type,
            "source_path": source_path,
            "category": category,
            "formal_knowledge_policy": "experience_only_not_formal_knowledge",
            "promotion_policy": "manual_candidate_review_only",
            "summary": summarize_intake_experience(source_type, category, evidence_excerpt, candidate_ids),
            "question": "",
            "reply_text": normalize_space(evidence_excerpt),
            "evidence_excerpt": truncate(normalize_space(evidence_excerpt), 1200),
            "rag_ingest": compact_rag_ingest(rag_ingest),
            "candidate_ids": candidate_ids,
            "candidate_count": len(candidate_ids),
            "original_source": original_source or {},
            "usage": {
                "reply_count": 0,
                "last_used_at": now_text,
            },
            "created_at": now_text,
            "updated_at": now_text,
            "review_state": new_rag_review_state(now_text),
        }
        record["quality"] = score_record_quality(record)
        existing_records = self.list(status="all", limit=500)
        auto_triage = intake_auto_triage_decision(record, existing_records=existing_records)
        if auto_triage.get("auto_discard"):
            record = mark_record_auto_triaged_discard(
                record,
                reason_code=str(auto_triage.get("reason_code") or "low_value_or_duplicate"),
                reason=str(auto_triage.get("reason") or "内容缺少业务价值或重复度过高，系统自动降噪。"),
            )
        _apply_creation_audit(record)
        return self._upsert_record(record, increment_usage=False)

    def discard(self, experience_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.update_status(
            experience_id,
            status="discarded",
            reason=reason or "discarded_by_user",
            extra={"discarded_at": now()},
        )

    def update_status(
        self,
        experience_id: str,
        *,
        status: str,
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {"active", "discarded", "promoted"}
        status = str(status or "").strip()
        if status not in allowed:
            raise ValueError(f"unsupported rag experience status: {status}")
        extra = extra or {}
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        db_item: dict[str, Any] | None = None
        if db:
            records = db.list_rag_experiences(self.tenant_id, status="all", limit=500)
            for item in records:
                if item.get("experience_id") != experience_id:
                    continue
                now_text = now()
                item["status"] = status
                if reason:
                    item[f"{status}_reason"] = reason
                    if status == "discarded":
                        item["discard_reason"] = reason
                for key, value in extra.items():
                    item[key] = value
                item["updated_at"] = now_text
                item["quality"] = score_record_quality(item)
                db.upsert_rag_experience(item)
                rebuild_rag_index_safely(self.tenant_id)
                if not config.mirror_files:
                    return item
                db_item = item
                break
        records = self._read()
        now_text = now()
        for index, item in enumerate(records):
            if item.get("experience_id") != experience_id:
                continue
            item["status"] = status
            if reason:
                item[f"{status}_reason"] = reason
                if status == "discarded":
                    item["discard_reason"] = reason
            for key, value in extra.items():
                item[key] = value
            item["updated_at"] = now_text
            item["quality"] = score_record_quality(item)
            records[index] = item
            self._write(records)
            rebuild_rag_index_safely(self.tenant_id, force_sync=True)
            return item
        if db_item:
            records.append(db_item)
            self._write(records)
            rebuild_rag_index_safely(self.tenant_id, force_sync=True)
            return db_item
        raise KeyError(experience_id)

    def update_metadata(
        self,
        experience_id: str,
        metadata: dict[str, Any],
        *,
        rebuild_index: bool = False,
    ) -> dict[str, Any]:
        metadata = dict(metadata or {})
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        db_item: dict[str, Any] | None = None
        if db:
            records = db.list_rag_experiences(self.tenant_id, status="all", limit=500)
            for item in records:
                if item.get("experience_id") != experience_id:
                    continue
                item.update(metadata)
                item["updated_at"] = now()
                item["quality"] = score_record_quality(item)
                db.upsert_rag_experience(item)
                if rebuild_index:
                    rebuild_rag_index_safely(self.tenant_id, force_sync=True)
                if not config.mirror_files:
                    return item
                db_item = item
                break
        records = self._read()
        for index, item in enumerate(records):
            if item.get("experience_id") != experience_id:
                continue
            item.update(metadata)
            item["updated_at"] = now()
            item["quality"] = score_record_quality(item)
            records[index] = item
            self._write(records)
            if rebuild_index:
                rebuild_rag_index_safely(self.tenant_id, force_sync=True)
            return item
        if db_item:
            records.append(db_item)
            self._write(records)
            if rebuild_index:
                rebuild_rag_index_safely(self.tenant_id, force_sync=True)
            return db_item
        raise KeyError(experience_id)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        compact = records[-MAX_RECORDS:]
        write_json_with_retry(self.path, compact)

    def _upsert_record(self, record: dict[str, Any], *, increment_usage: bool) -> dict[str, Any]:
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            existing = next(
                (
                    item
                    for item in db.list_rag_experiences(self.tenant_id, status="all", limit=500)
                    if item.get("experience_id") == record["experience_id"]
                ),
                None,
            )
            if existing:
                record = merge_experience_record(existing, record, increment_usage=increment_usage)
            db.upsert_rag_experience(record)
            rebuild_rag_index_safely(self.tenant_id)
            if not config.mirror_files:
                return record
        records = self._read()
        for index, existing in enumerate(records):
            if existing.get("experience_id") != record["experience_id"]:
                continue
            records[index] = merge_experience_record(existing, record, increment_usage=increment_usage)
            self._write(records)
            rebuild_rag_index_safely(self.tenant_id)
            return records[index]
        records.append(record)
        self._write(records)
        rebuild_rag_index_safely(self.tenant_id)
        return record


def record_rag_reply_experience(
    *,
    target: str,
    message_ids: list[str],
    question: str,
    reply_text: str,
    raw_reply_text: str,
    intent_assist: dict[str, Any],
    rag_reply: dict[str, Any],
    reply_trace_id: str = "",
) -> dict[str, Any] | None:
    if not rag_reply.get("applied"):
        return None
    return RagExperienceStore().record_reply(
        target=target,
        message_ids=message_ids,
        question=question,
        reply_text=reply_text,
        raw_reply_text=raw_reply_text,
        intent_assist=intent_assist,
        rag_reply=rag_reply,
        reply_trace_id=reply_trace_id,
    )


def merge_experience_record(existing: dict[str, Any], record: dict[str, Any], *, increment_usage: bool) -> dict[str, Any]:
    now_text = now()
    merged = dict(existing)
    created_at = merged.get("created_at") or record.get("created_at")
    merged.update(record)
    merged["created_at"] = created_at
    merged["status"] = existing.get("status") or record.get("status") or "active"
    merged["updated_at"] = now_text
    usage = dict(existing.get("usage", {}) or {})
    next_usage = dict(record.get("usage", {}) or {})
    if increment_usage:
        usage["reply_count"] = int(usage.get("reply_count", 1) or 1) + 1
    elif "reply_count" in next_usage:
        usage["reply_count"] = next_usage.get("reply_count")
    usage["last_used_at"] = now_text
    merged["usage"] = usage
    existing_review_state = existing.get("review_state") if isinstance(existing.get("review_state"), dict) else {}
    next_review_state = record.get("review_state") if isinstance(record.get("review_state"), dict) else {}
    if existing_review_state:
        merged["review_state"] = existing_review_state
    elif next_review_state:
        merged["review_state"] = next_review_state
    merged, _ = ensure_review_state(merged, default_now=now_text)
    merged["quality"] = score_record_quality(merged)
    return merged


def new_rag_review_state(now_text: str) -> dict[str, Any]:
    return {
        "is_new": True,
        "new_reason": "new_rag_experience",
        "marked_at": now_text,
        "updated_at": now_text,
        "read_at": "",
        "read_by": "",
    }


def ensure_review_state(item: dict[str, Any], *, default_now: str | None = None) -> tuple[dict[str, Any], bool]:
    """Backfill missing review_state for historical AI experience pool items.

    Compatibility rule:
    - Active + pending/unreviewed experiences default to NEW.
    - Already processed (kept/auto-kept/auto-triaged/promoted/discarded/reopened) default to not NEW.
    """
    result = dict(item)
    raw_state = result.get("review_state") if isinstance(result.get("review_state"), dict) else {}
    has_new_flag = "is_new" in raw_state
    now_text = str(default_now or result.get("updated_at") or result.get("created_at") or now())
    review = result.get("experience_review") if isinstance(result.get("experience_review"), dict) else {}
    status = str(result.get("status") or "active")
    review_status = str(review.get("status") or "")
    reopened = bool(review.get("reopened_at"))
    reviewed_by_user = bool(result.get("reviewed_by_user"))
    processed = (
        status in {"discarded", "promoted"}
        or review_status in {"kept", "auto_kept", "auto_triaged"}
        or reviewed_by_user
        or reopened
    )
    inferred_is_new = not processed

    state = dict(raw_state)
    changed = False
    if not has_new_flag:
        state["is_new"] = inferred_is_new
        state["new_reason"] = "new_rag_experience" if inferred_is_new else "backfilled_processed_rag_experience"
        marked_at = str(result.get("created_at") or result.get("updated_at") or now_text)
        state["marked_at"] = marked_at
        if inferred_is_new:
            state["read_at"] = ""
            state["read_by"] = ""
        else:
            state["read_at"] = str(
                review.get("kept_at")
                or review.get("reopened_at")
                or result.get("promoted_at")
                or result.get("discarded_at")
                or result.get("updated_at")
                or marked_at
            )
            state["read_by"] = str(state.get("read_by") or ("admin" if reviewed_by_user else "system"))
        changed = True
    else:
        is_new = bool(state.get("is_new"))
        if is_new and processed:
            state["is_new"] = False
            state["read_at"] = str(state.get("read_at") or result.get("updated_at") or now_text)
            state["read_by"] = str(state.get("read_by") or ("admin" if reviewed_by_user else "system"))
            state["new_reason"] = str(state.get("new_reason") or "processed_rag_experience")
            changed = True
        elif not is_new and not state.get("read_at"):
            state["read_at"] = str(result.get("updated_at") or now_text)
            state["read_by"] = str(state.get("read_by") or ("admin" if reviewed_by_user else "system"))
            changed = True

    if not state.get("marked_at"):
        state["marked_at"] = str(result.get("created_at") or result.get("updated_at") or now_text)
        changed = True
    state["updated_at"] = now_text
    if changed or not raw_state:
        result["review_state"] = state
    return result, changed


def _apply_creation_audit(record: dict[str, Any]) -> None:
    """Apply real-time LLM quality gate to a newly created AI experience pool item.

    Modifies the record in place. By default it only annotates the review
    outcome so retrieval remains stable and human review can decide whether
    to discard. Set WECHAT_RAG_AUTO_DISCARD=1 to enable immediate discard.
    Failures are silently ignored so the hot path is never blocked.
    """
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.llm_knowledge_audit import (
            audit_single_rag_experience,
            has_llm_config,
        )
    except Exception:
        return
    if not has_llm_config():
        return
    try:
        audit = audit_single_rag_experience(record)
    except Exception:
        return
    if not audit:
        return
    action = str(audit.get("action") or "")
    reason = str(audit.get("reason") or "")
    record["auto_audit"] = {
        "action": action,
        "reason": reason,
        "audited_at": now(),
    }
    if action != "delete":
        return
    auto_discard_enabled = str(os.getenv("WECHAT_RAG_AUTO_DISCARD", "0")).strip().lower() in {"1", "true", "yes", "on"}
    source = str(record.get("source") or "")
    if auto_discard_enabled or source in AUTO_DISCARD_AUDITED_DELETE_SOURCES:
        mode = "auto_discard_enabled" if auto_discard_enabled else "auto_discard_low_value_reply"
        discarded = mark_record_auto_triaged_discard(
            record,
            reason_code="llm_audit_delete",
            reason=reason or "LLM 审计判断为低价值经验，系统自动降噪。",
        )
        record.clear()
        record.update(discarded)
        record["auto_audit_discard"] = {
            "reason": reason,
            "audited_at": now(),
            "mode": mode,
        }
        return
    record["auto_audit_suggested_discard"] = {
        "reason": reason,
        "audited_at": now(),
        "mode": "suggest_only",
    }


def summarize_intake_experience(source_type: str, category: str, evidence_excerpt: str, candidate_ids: list[str]) -> str:
    source_label = source_type or "intake"
    category_label = category or "unknown"
    excerpt = truncate(normalize_space(evidence_excerpt), 96)
    return f"AI经验池：{source_label}/{category_label}，摘要={excerpt}"


def intake_auto_triage_decision(record: dict[str, Any], *, existing_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide whether a new intake record should be auto-discarded.

    Rule intent:
    - Product-master facts from intake are not reviewed in AI experience pool item queue.
    - Low-value noise is auto-discarded.
    - Highly duplicated intake records are auto-discarded.
    """
    category = str(record.get("category") or "").strip().lower()
    source = str(record.get("source") or "")
    source_type = str(record.get("source_type") or "")
    if source != "intake":
        return {"auto_discard": False}
    if category in INTAKE_PRODUCT_MASTER_CATEGORIES:
        return {
            "auto_discard": True,
            "reason_code": "product_master_manual_intake_only",
            "reason": "商品资料属于权威主数据，AI经验池不做人工复核，自动降噪处理。",
        }
    evidence = normalize_space(str(record.get("evidence_excerpt") or record.get("reply_text") or ""))
    candidate_count = int(coerce_float(record.get("candidate_count"), 0))
    if _is_low_value_intake(evidence, candidate_count=candidate_count):
        return {
            "auto_discard": True,
            "reason_code": "low_value_noise",
            "reason": "没有识别到稳定可复用的业务价值，自动从审核队列降噪。",
        }
    duplicate_count = intake_duplicate_count(record, existing_records=existing_records)
    duplicate_threshold = INTAKE_CATEGORY_DUPLICATE_THRESHOLD.get(category, INTAKE_DEFAULT_DUPLICATE_THRESHOLD)
    if duplicate_count >= duplicate_threshold:
        return {
            "auto_discard": True,
            "reason_code": "high_duplicate_intake",
            "reason": f"检测到高重复导入内容（同类重复 {duplicate_count} 次），自动降噪处理。",
            "duplicate_count": duplicate_count,
            "duplicate_threshold": duplicate_threshold,
        }
    return {
        "auto_discard": False,
        "duplicate_count": duplicate_count,
        "duplicate_threshold": duplicate_threshold,
        "category": category,
        "source_type": source_type,
    }


def mark_record_auto_triaged_discard(record: dict[str, Any], *, reason_code: str, reason: str) -> dict[str, Any]:
    now_text = now()
    review = dict(record.get("experience_review") or {}) if isinstance(record.get("experience_review"), dict) else {}
    review.update(
        {
            "status": "auto_triaged",
            "auto_triaged_at": now_text,
            "auto_triage_action": "discard",
            "auto_triage_reason": truncate(reason, 240),
            "auto_triage_reason_code": str(reason_code or ""),
        }
    )
    marked = dict(record)
    marked.update(
        {
            "status": "discarded",
            "experience_review": review,
            "reviewed_by_user": False,
            "discarded_at": now_text,
            "discarded_reason": "auto_triaged_discard",
            "discard_reason": "auto_triaged_discard",
            "review_state": {
                "is_new": False,
                "new_reason": "auto_triaged_discard",
                "marked_at": str((record.get("review_state") or {}).get("marked_at") or record.get("created_at") or now_text),
                "updated_at": now_text,
                "read_at": now_text,
                "read_by": "system",
                "last_action": "auto_discard",
                "last_action_at": now_text,
            },
        }
    )
    marked["quality"] = score_record_quality(marked)
    return marked


def intake_duplicate_count(record: dict[str, Any], *, existing_records: list[dict[str, Any]]) -> int:
    """Return number of similar intake records already present."""
    target_key = intake_duplicate_key(record)
    if not target_key:
        return 0
    current_id = str(record.get("experience_id") or "")
    count = 0
    for item in existing_records:
        if str(item.get("source") or "") != "intake":
            continue
        if current_id and str(item.get("experience_id") or "") == current_id:
            continue
        if intake_duplicate_key(item) == target_key:
            count += 1
    return count


def intake_duplicate_key(record: dict[str, Any]) -> str:
    category = str(record.get("category") or "").strip().lower()
    source_type = str(record.get("source_type") or "").strip().lower()
    evidence = normalize_space(str(record.get("evidence_excerpt") or record.get("reply_text") or ""))
    if not evidence:
        return ""
    evidence = _normalize_intake_text(evidence)
    if not evidence:
        return ""
    return stable_digest(f"{category}|{source_type}|{evidence}", 28)


def _normalize_intake_text(text: str) -> str:
    normalized = normalize_space(text)
    normalized = re.sub(r"CHEJIN_\d{8}_\d{6}", "CHEJIN_BATCH", normalized, flags=re.I)
    normalized = re.sub(r"upload_[0-9a-f]{8,}", "UPLOAD_ID", normalized, flags=re.I)
    normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b", "TIMESTAMP", normalized)
    normalized = re.sub(r"\b\d{10,}\b", "LONG_NUMBER", normalized)
    return truncate(normalized, 1600)


def _is_low_value_intake(evidence: str, *, candidate_count: int) -> bool:
    text = normalize_space(evidence)
    if not text:
        return True
    if candidate_count <= 0 and len(text) < INTAKE_LOW_VALUE_MIN_TEXT_LENGTH:
        return True
    if len(text) < INTAKE_LOW_VALUE_MIN_TEXT_LENGTH and not any(token in text for token in INTAKE_LOW_VALUE_BUSINESS_HINTS):
        return True
    if any(term in text for term in QUALITY_TEST_CONTENT_TERMS):
        return True
    return False


def compact_rag_ingest(rag_ingest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rag_ingest, dict):
        return {}
    return {
        "ok": bool(rag_ingest.get("ok")),
        "source_id": rag_ingest.get("source_id"),
        "chunk_count": rag_ingest.get("chunk_count", 0),
        "category": rag_ingest.get("category"),
        "source_type": rag_ingest.get("source_type"),
        "message": rag_ingest.get("message"),
    }


def summarize_experience(question: str, reply_text: str, hit: dict[str, Any]) -> str:
    question_text = truncate(normalize_space(question), 54)
    hit_text = truncate(normalize_space(str(hit.get("text") or "")), 68)
    reply = truncate(normalize_space(reply_text), 68)
    parts = [f"客户问法：{question_text}"]
    if hit_text:
        parts.append(f"命中资料：{hit_text}")
    if reply:
        parts.append(f"回复要点：{reply}")
    return "；".join(parts)


def with_quality(item: dict[str, Any]) -> dict[str, Any]:
    enriched, _ = ensure_review_state(item)
    quality = enriched.get("quality")
    signals = quality.get("signals", {}) if isinstance(quality, dict) else {}
    if not isinstance(quality, dict) or "retrieval_allowed" not in quality or "review_allows_retrieval" not in signals:
        quality = score_record_quality(enriched)
    enriched["quality"] = quality
    return enriched


def experience_is_retrievable(item: dict[str, Any]) -> bool:
    # AI经验池 is a governance/distribution pool. Runtime content evidence must
    # come from product master, formal knowledge, or current conversation facts.
    return False


def experience_is_reference_candidate(item: dict[str, Any]) -> bool:
    # Reference candidates may only guide style/scenario understanding. They stay
    # out of runtime content evidence and are filtered again before prompting.
    if str(item.get("source") or "") == "intake":
        return False
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    signals = quality.get("signals") if isinstance(quality.get("signals"), dict) else {}
    if str(quality.get("band") or "") not in REFERENCE_CANDIDATE_BANDS:
        return False
    return bool(signals.get("quality_allows_retrieval") and signals.get("review_allows_retrieval"))


def experience_review_allows_retrieval(item: dict[str, Any]) -> bool:
    """Legacy approval helper kept for quality scoring and old data audits."""
    if str(item.get("source") or "") == "intake":
        return False
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    review_status = str(review.get("status") or "")
    if review_status == "auto_kept":
        return True
    return bool(item.get("reviewed_by_user") and review_status == "kept")


def score_record_quality(item: dict[str, Any]) -> dict[str, Any]:
    if str(item.get("source") or "") == "intake":
        return score_intake_experience_quality(item)
    return score_experience_quality(item)


def score_intake_experience_quality(item: dict[str, Any]) -> dict[str, Any]:
    evidence = normalize_space(str(item.get("evidence_excerpt") or item.get("reply_text") or ""))
    candidate_count = int(coerce_float(item.get("candidate_count"), 0))
    rag_ingest = item.get("rag_ingest", {}) or {}
    rag_ok = bool(rag_ingest.get("ok"))
    chunk_count = int(coerce_float(rag_ingest.get("chunk_count"), 0))
    score = 0.34
    if evidence:
        score += 0.16
    if len(evidence) >= 80:
        score += 0.08
    if rag_ok:
        score += 0.12
    if chunk_count:
        score += min(0.08, chunk_count * 0.02)
    if candidate_count:
        score += min(0.14, candidate_count * 0.035)
    score = round(max(0.0, min(0.84, score)), 3)
    return {
        "score": score,
        "band": "medium" if score >= 0.62 else "low",
        "retrieval_allowed": False,
        "reasons": [
            "intake material is stored as AI experience pool first",
            "formal knowledge still requires pending-candidate review",
            "intake experiences are not used for autonomous reply retrieval before review",
        ],
        "signals": {
            "source_type": item.get("source_type"),
            "candidate_count": candidate_count,
            "rag_ingest_ok": rag_ok,
            "chunk_count": chunk_count,
            "has_evidence": bool(evidence),
        },
        "evaluated_at": now(),
    }


def score_experience_quality(item: dict[str, Any]) -> dict[str, Any]:
    hit = item.get("rag_hit", {}) or {}
    usage = item.get("usage", {}) or {}
    safety = item.get("safety", {}) or {}
    question = normalize_space(str(item.get("question") or ""))
    reply = normalize_space(str(item.get("reply_text") or ""))
    hit_text = normalize_space(str(hit.get("text") or ""))
    hit_score = coerce_float(hit.get("score"), 0.0)
    reply_count = max(1, int(coerce_float(usage.get("reply_count"), 1)))
    risk_terms = [str(value) for value in hit.get("risk_terms", []) or [] if str(value).strip()]
    recommended_action = str(item.get("recommended_action") or "").lower()
    must_handoff = bool(safety.get("must_handoff"))
    blocked_action = any(term.lower() in recommended_action for term in QUALITY_BLOCK_ACTION_TERMS)
    has_text = bool(question and reply)
    has_source = bool(hit_text or str(hit.get("chunk_id") or ""))

    score = 0.22
    score += min(0.46, max(0.0, hit_score) * 0.46)
    score += min(0.12, reply_count * 0.025)
    if has_text:
        score += 0.1
    if has_source:
        score += 0.06
    if len(question) >= 8 and len(reply) >= 12:
        score += 0.05

    has_metadata_pollution = any(term in reply for term in QUALITY_METADATA_POLLUTION_TERMS)
    has_test_content = any(term in reply for term in QUALITY_TEST_CONTENT_TERMS)

    blockers: list[str] = []
    reasons: list[str] = []
    if not has_text:
        blockers.append("缺少清晰的问题或回复")
    if not has_source:
        reasons.append("缺少可追溯的命中资料")
        score -= 0.08
    if has_metadata_pollution:
        blockers.append("回复包含元数据污染")
        score -= 0.35
    if has_test_content:
        blockers.append("回复包含测试或无意义内容")
        score -= 0.35
    if risk_terms:
        blockers.append("命中资料包含风险词")
        score -= 0.25
    if must_handoff:
        blockers.append("当时证据要求人工接管")
        score -= 0.25
    if blocked_action:
        blockers.append("当时建议动作需要人工处理")
        score -= 0.18
    if hit_score < QUALITY_REPEATABLE_MIN_HIT_SCORE:
        reasons.append("原始命中分偏低")
        score -= 0.08
    elif hit_score < QUALITY_RETRIEVAL_MIN_HIT_SCORE and reply_count < QUALITY_REPEATABLE_REPLY_COUNT:
        reasons.append("命中分中等偏低且复用次数不足")
        score -= 0.04
    else:
        reasons.append("证据命中分达到经验层要求")
    if reply_count >= QUALITY_REPEATABLE_REPLY_COUNT:
        reasons.append("已被多次复用")

    score = round(max(0.0, min(0.99, score)), 3)
    enough_hit_score = hit_score >= QUALITY_RETRIEVAL_MIN_HIT_SCORE or (
        hit_score >= QUALITY_REPEATABLE_MIN_HIT_SCORE and reply_count >= QUALITY_REPEATABLE_REPLY_COUNT
    )
    quality_allows_retrieval = not blockers and enough_hit_score and score >= QUALITY_RETRIEVAL_MIN_SCORE
    review_allows_retrieval = experience_review_allows_retrieval(item)
    retrieval_allowed = False
    if blockers:
        band = "blocked"
    elif score >= 0.72:
        band = "high"
    elif quality_allows_retrieval:
        band = "medium"
    else:
        band = "low"
    if quality_allows_retrieval and not review_allows_retrieval:
        reasons.append("尚未人工确认保留在AI经验池")
    reasons.append("AI经验池不直接参与客户回复内容检索")
    return {
        "score": score,
        "band": band,
        "retrieval_allowed": retrieval_allowed,
        "reasons": [*blockers, *reasons],
        "signals": {
            "hit_score": hit_score,
            "reply_count": reply_count,
            "has_risk_terms": bool(risk_terms),
            "risk_terms": risk_terms,
            "must_handoff": must_handoff,
            "blocked_action": blocked_action,
            "has_text": has_text,
            "has_source": has_source,
            "quality_allows_retrieval": quality_allows_retrieval,
            "review_allows_retrieval": review_allows_retrieval,
        },
        "evaluated_at": now(),
    }


def coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_space(value: str) -> str:
    return " ".join(str(value or "").split())


def truncate(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def stable_digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def enqueue_rag_index_rebuild(tenant_id: str, *, trigger: str = "") -> dict[str, Any]:
    from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService

    queue = WorkQueueService(tenant_id=tenant_id)
    dedupe_key = f"rag_rebuild_index:{tenant_id}"
    payload = {
        "tenant_id": tenant_id,
        "trigger": trigger or "rag_experience_mutation",
        "requested_at": now(),
    }
    existing = queue.find_active_dedupe(queue="customer_service", dedupe_key=dedupe_key)
    if existing and str(existing.get("status") or "") == "pending" and int(existing.get("priority", 5) or 5) > 1:
        queue.cancel(str(existing.get("job_id") or ""), reason="reprioritized_for_fast_rag_rebuild")
        existing = None

    if existing:
        job = dict(existing)
        job["deduped"] = True
    else:
        job = queue.enqueue(
            kind="rag_rebuild_index",
            payload=payload,
            queue="customer_service",
            priority=1,
            dedupe_key=dedupe_key,
        )
    worker_ok = True
    worker_message = ""
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import CustomerServiceRuntime

        worker_result = CustomerServiceRuntime(tenant_id=tenant_id)._start_worker()
        worker_ok = bool(worker_result.get("ok"))
        worker_message = str(worker_result.get("message") or "")
    except Exception as exc:
        worker_ok = False
        worker_message = repr(exc)
    return {
        "ok": True,
        "mode": "queued_async_rebuild",
        "queue": str(job.get("queue") or "customer_service"),
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or "pending"),
        "deduped": bool(job.get("deduped")),
        "worker_ok": worker_ok,
        "worker_message": worker_message,
    }


def rebuild_rag_index_safely(tenant_id: str, *, trigger: str = "", force_sync: bool = False) -> None:
    queued_worker_ok = False
    try:
        queued = enqueue_rag_index_rebuild(tenant_id, trigger=trigger)
        queued_worker_ok = bool(queued.get("worker_ok"))
    except Exception:
        queued_worker_ok = False
    # Keep async queue as the primary path, but provide deterministic local
    # fallback when the worker cannot start. This preserves immediate
    # consistency for in-process flows and test probes using rebuild_index=True.
    if queued_worker_ok and not force_sync:
        return
    try:
        try:
            from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # local import to avoid circular import at module load time
        except Exception:
            from rag_layer import RagService  # compatibility for tests importing workflows modules directly

        RagService(tenant_id=tenant_id).rebuild_index()
    except Exception:
        return


def postgres_store(tenant_id: str):
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(tenant_id=tenant_id, config=config)
    return store if store.available() else None
