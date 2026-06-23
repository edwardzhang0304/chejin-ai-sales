"""Admin-facing wrapper for retrieval and the AI experience pool."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
if str(WORKFLOWS_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_ROOT))

from rag_layer import RagService  # noqa: E402
from rag_experience_store import RagExperienceStore, enqueue_rag_index_rebuild, with_quality  # noqa: E402
from rag_operations import RagOperationsAnalyzer  # noqa: E402
from generate_review_candidates import LLM_ASSIST_POLICY_VERSION  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_review_candidates_root, tenant_runtime_state_root  # noqa: E402
from apps.wechat_ai_customer_service.llm_config import (  # noqa: E402
    apply_llm_reasoning_effort,
    llm_urlopen,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_max_tokens,
    resolve_deepseek_tier_model,
    resolve_deepseek_timeout,
)
from apps.wechat_ai_customer_service.platform_safety_rules import guard_term_set, load_platform_safety_rules  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import (  # noqa: E402
    KnowledgeRuntime,
    PRODUCT_SCOPED_SCHEMAS,
)
from .candidate_store import CandidateStore, upsert_candidate_to_db  # noqa: E402
from .knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from .knowledge_compiler import KnowledgeCompiler  # noqa: E402
from .knowledge_registry import KnowledgeRegistry  # noqa: E402
from .knowledge_schema_manager import KnowledgeSchemaManager  # noqa: E402
from .formal_review_state import mark_item_new  # noqa: E402
from .rag_experience_interpreter import (  # noqa: E402
    INTERPRETATION_VERSION,
    RagExperienceInterpreter,
    AUTO_KEPT_REVIEW_STATUS,
    AUTO_TRIAGE_REVIEW_STATUS,
    apply_guardrails_to_interpretation,
    build_auto_triage_patch,
    content_fingerprint,
    interpretation_looks_corrupted,
)
from .rag_experience_governance import (  # noqa: E402
    attach_governance,
    governance_counts,
    governance_display_state,
)
from .raw_message_store import RawMessageStore  # noqa: E402
from .source_authority_policy import PRODUCT_MASTER_CATEGORIES, evaluate_experience_source_authority, experience_contains_model_reply  # noqa: E402


PRODUCT_SCOPED_CATEGORIES = set(PRODUCT_SCOPED_SCHEMAS)


def visible_rule_values(group: str) -> list[str]:
    rules = load_platform_safety_rules().get("item", {})
    return sorted(guard_term_set(rules, group))


def text_has_visible_term(text: Any, group: str) -> bool:
    value = str(text or "")
    return any(term and term in value for term in visible_rule_values(group))


def text_matches_visible_pattern(text: Any, group: str) -> bool:
    value = str(text or "")
    return any(re.search(pattern, value, re.I) for pattern in visible_rule_values(group))


class RagAdminService:
    def __init__(self) -> None:
        self.rag = RagService()
        self.experiences = RagExperienceStore()
        self.operations = RagOperationsAnalyzer(rag_service=self.rag, experience_store=self.experiences)
        self.interpreter = RagExperienceInterpreter(store=self.experiences)
        self.raw_messages = RawMessageStore(tenant_id=self.experiences.tenant_id)

    def status(self) -> dict[str, Any]:
        payload = self.rag.status()
        payload["experience_counts"] = self.experiences.counts()
        payload["experience_governance_counts"] = self.experience_accounting_counts()["governance_counts"]
        return payload

    def _virtualize_auto_triaged_experience(
        self,
        *,
        base_item: dict[str, Any],
        annotated_item: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        cached_interpretation = annotated_item.get("ai_interpretation") if isinstance(annotated_item.get("ai_interpretation"), dict) else {}
        if not cached_interpretation:
            return base_item, False
        if str(cached_interpretation.get("version") or "") != INTERPRETATION_VERSION:
            return base_item, False
        guarded_interpretation = apply_guardrails_to_interpretation(cached_interpretation, annotated_item)
        triage_patch = build_auto_triage_patch(annotated_item, guarded_interpretation)
        if not triage_patch:
            return base_item, False
        virtual_item = dict(base_item)
        virtual_item.update(triage_patch)
        review = triage_patch.get("experience_review") if isinstance(triage_patch.get("experience_review"), dict) else {}
        review_status = str(review.get("status") or "")
        auto_action = str(review.get("auto_triage_action") or "")
        if review_status == AUTO_TRIAGE_REVIEW_STATUS and auto_action in {"discard", "already_covered"}:
            virtual_item["status"] = "discarded"
        return with_quality(virtual_item), True

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.rag.search(
            str(payload.get("query") or ""),
            product_id=str(payload.get("product_id") or ""),
            category=str(payload.get("category") or ""),
            source_type=str(payload.get("source_type") or ""),
            limit=int(payload.get("limit") or 6),
        )

    def rebuild(self) -> dict[str, Any]:
        index = enqueue_rag_index_rebuild(
            self.experiences.tenant_id,
            trigger="manual_admin_rebuild",
        )
        return {
            "ok": True,
            "message": "索引重建任务已提交后台处理。",
            "index": index,
        }

    def sources(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        limit = max(1, min(int(payload.get("limit") or 80), 300))
        sources = self.rag.list_sources()
        chunks = [item for item in self.rag.iter_chunks() if str(item.get("source_type") or "") != "rag_experience"]
        chunk_counts = Counter(str(item.get("source_id") or "") for item in chunks)
        enriched_sources = []
        for source in sources:
            source_id = str(source.get("source_id") or "")
            enriched_sources.append({**source, "chunk_count": int(chunk_counts.get(source_id, 0))})
        return {
            "ok": True,
            "sources": enriched_sources,
            "chunks": chunks[:limit],
            "chunk_counts": dict(chunk_counts),
            "total_chunks": len(chunks),
        }

    def analytics(self) -> dict[str, Any]:
        return self.operations.report()

    def list_experiences(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        status = str(payload.get("status") or "active")
        limit = int(payload.get("limit") or 100)
        fast = bool(payload.get("fast"))
        formal_items = [] if fast else collect_formal_items()
        formal_revision = "" if fast else formal_items_revision(formal_items)
        items = []
        source_dialogue_cache: dict[str, list[dict[str, Any]]] = {}
        for item in self.experiences.list(status=status, limit=limit):
            enriched = with_quality(item)
            if fast:
                annotated = annotate_experience_from_cache(enriched)
            else:
                annotated = annotate_experience(enriched, formal_items)
                annotated["formal_revision"] = formal_revision
                relation_cache = build_relation_cache(annotated, formal_item_count=len(formal_items), formal_revision=formal_revision)
                annotated["formal_relation_cache"] = relation_cache
                if relation_cache_needs_update(enriched.get("formal_relation_cache"), relation_cache):
                    try:
                        self.experiences.update_metadata(
                            str(enriched.get("experience_id") or ""),
                            {"formal_relation_cache": relation_cache, "quality": annotated.get("quality", {})},
                            rebuild_index=False,
                        )
                    except KeyError:
                        pass
            annotated = attach_source_dialogue(
                annotated,
                raw_store=self.raw_messages,
                conversation_cache=source_dialogue_cache,
            )
            annotated = attach_governance(annotated)
            if status == "active" and str(annotated.get("status") or "active") != "active":
                continue
            items.append(annotated)
        relation_counts = Counter(str(item.get("formal_relation") or "unknown") for item in items)
        quality_counts = Counter(str((item.get("quality") or {}).get("band") or "unknown") for item in items)
        retrieval_counts = Counter("retrievable" if (item.get("governance") or {}).get("retrieval_allowed") else "not_retrievable" for item in items)
        raw_counts = self.experiences.counts()
        accounting_counts = self.experience_accounting_counts()
        display_counts = accounting_counts["display_counts"]
        all_governance_counts = accounting_counts["governance_counts"]
        return {
            "ok": True,
            "items": items,
            "counts": raw_counts,
            "display_counts": display_counts,
            "governance_counts": all_governance_counts,
            "relation_counts": dict(relation_counts),
            "quality_counts": dict(quality_counts),
            "retrieval_counts": dict(retrieval_counts),
            "loaded_count": len(items),
            "requested_limit": limit,
            "formal_knowledge_policy": "rag_experience_only_not_formal_knowledge",
        }

    def experience_accounting_counts(self) -> dict[str, dict[str, int]]:
        records = self.experiences.list_for_counts()
        counts = {"total": len(records), "pending": 0, "kept": 0, "promoted": 0, "discarded": 0, "other": 0}
        governed_records = []
        for record in records:
            enriched = with_quality(record)
            annotated = annotate_experience_from_cache(enriched)
            annotated = attach_governance(annotated)
            governed_records.append(annotated)
            relation_value = str(annotated.get("formal_relation") or enriched.get("status") or "novel")
            display_state = rag_experience_display_state(annotated, relation_value=relation_value)
            if display_state in {"pending", "kept", "promoted", "discarded"}:
                counts[display_state] += 1
            else:
                counts["other"] += 1
        counts["accounted_total"] = counts["pending"] + counts["kept"] + counts["promoted"] + counts["discarded"] + counts["other"]
        counts["consistent"] = counts["accounted_total"] == counts["total"]
        return {"display_counts": counts, "governance_counts": governance_counts(governed_records)}

    def display_counts(self) -> dict[str, int]:
        return self.experience_accounting_counts()["display_counts"]
        return counts

    def unreviewed_experience_count(self) -> dict[str, Any]:
        records = self.experiences.list(status="all", limit=500)
        count = 0
        for record in records:
            enriched = with_quality(record)
            annotated = annotate_experience_from_cache(enriched)
            annotated = attach_governance(annotated)
            relation_value = str(annotated.get("formal_relation") or enriched.get("status") or "novel")
            if rag_experience_display_state(annotated, relation_value=relation_value) != "pending":
                continue
            review_state = enriched.get("review_state") if isinstance(enriched.get("review_state"), dict) else {}
            if bool(review_state.get("is_new")):
                count += 1
        return {"ok": True, "count": count, "counts": self.experiences.counts(), "display_counts": self.display_counts()}

    def _reviewed_state(self, current: dict[str, Any], payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        now_text = now()
        review_state = dict(current.get("review_state") if isinstance(current.get("review_state"), dict) else {})
        if not review_state:
            review_state = {
                "is_new": True,
                "new_reason": "new_rag_experience",
                "marked_at": now_text,
                "read_at": "",
                "read_by": "",
            }
        read_at = str(review_state.get("read_at") or now_text)
        review_state.update(
            {
                "is_new": False,
                "read_at": read_at,
                "read_by": str(payload.get("actor") or review_state.get("read_by") or "admin"),
                "updated_at": now_text,
                "last_action": action,
                "last_action_at": now_text,
            }
        )
        return review_state

    def discard_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        current = self.find_experience(experience_id)
        review_state = self._reviewed_state(current, payload, action="discard")
        item = self.experiences.update_status(
            experience_id,
            status="discarded",
            reason=str(payload.get("reason") or "discarded in admin"),
            extra={
                "discarded_at": now(),
                "review_state": review_state,
                "reviewed_by_user": True,
            },
        )
        return {"ok": True, "item": item, "index": {"ok": True, "mode": "queued_async_rebuild"}}

    def acknowledge_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        current = self.find_experience(experience_id)
        review_state = self._reviewed_state(current, payload, action="acknowledge")
        item = self.experiences.update_metadata(
            experience_id,
            {"review_state": review_state},
            rebuild_index=False,
        )
        return {"ok": True, "item": item, "index": {"ok": True, "mode": "queued_async_rebuild"}}

    def keep_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        current = self.find_experience(experience_id)
        review_state = self._reviewed_state(current, payload, action="keep")
        review = dict(current.get("experience_review") or {}) if isinstance(current.get("experience_review"), dict) else {}
        review.update(
            {
                "status": "kept",
                "kept_at": now(),
                "kept_reason": str(payload.get("reason") or "kept_as_rag_experience"),
            }
        )
        item = self.experiences.update_metadata(
            experience_id,
            {
                "experience_review": review,
                "reviewed_by_user": True,
                "review_state": review_state,
            },
            rebuild_index=True,
        )
        return {"ok": True, "item": item, "index": {"ok": True, "mode": "queued_async_rebuild"}}

    def reopen_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        current = self.find_experience(experience_id)
        previous_status = str(current.get("status") or "active")
        promoted_candidate_id = str(current.get("promoted_candidate_id") or "")
        rejected_candidate: dict[str, Any] | None = None
        if previous_status == "promoted" and promoted_candidate_id:
            candidate_store = CandidateStore()
            candidate = candidate_store.get_candidate(promoted_candidate_id)
            candidate_status = str(((candidate or {}).get("review") or {}).get("status") or "")
            if candidate_status == "approved":
                return {
                    "ok": False,
                    "message": "这条经验生成的候选知识已经确认入库，不能仅通过“重新待处理”撤回；请到正式知识库处理对应知识。",
                    "candidate_id": promoted_candidate_id,
                }
            if candidate_status == "pending":
                rejected_candidate = candidate_store.reject(
                    promoted_candidate_id,
                    reason="AI经验已重新待处理，撤回旧的候选知识。",
                ).get("item")

        review = dict(current.get("experience_review") or {}) if isinstance(current.get("experience_review"), dict) else {}
        review.update(
            {
                "status": "pending",
                "reopened_at": now(),
                "reopen_reason": str(payload.get("reason") or "reopened in admin"),
                "previous_status": previous_status,
            }
        )
        if previous_status in {"discarded", "promoted"}:
            item = self.experiences.update_status(
                experience_id,
                status="active",
                reason=str(payload.get("reason") or "reopened in admin"),
                extra={"experience_review": review, "reviewed_by_user": False, "reopened_at": now()},
            )
        else:
            item = self.experiences.update_metadata(
                experience_id,
                {"experience_review": review, "reviewed_by_user": False},
                rebuild_index=True,
            )
        return {
            "ok": True,
            "item": item,
            "index": {"ok": True, "mode": "queued_async_rebuild"},
            "rejected_candidate": rejected_candidate,
        }

    def update_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        current = self.find_experience(experience_id)
        patch: dict[str, Any] = {}
        if "reply_text" in payload:
            reply_text = str(payload.get("reply_text") or "").strip()
            if not reply_text:
                return {"ok": False, "message": "reply_text cannot be empty"}
            patch["reply_text"] = reply_text
            patch["manual_reply_text_edited_at"] = now()
        if "review_status" in payload:
            review_status = str(payload.get("review_status") or "").strip()
            if review_status not in {"pending", "kept"}:
                return {"ok": False, "message": "unsupported review_status"}
            review = dict(current.get("experience_review") or {}) if isinstance(current.get("experience_review"), dict) else {}
            review["status"] = review_status
            if review_status == "pending":
                review["reopened_at"] = now()
            else:
                review["kept_at"] = now()
                review["kept_reason"] = str(payload.get("reason") or review.get("kept_reason") or "kept_as_rag_experience")
                patch["review_state"] = self._reviewed_state(current, payload, action="keep")
            patch["experience_review"] = review
            patch["reviewed_by_user"] = review_status == "kept"
        if not patch:
            return {"ok": False, "message": "no supported fields to update"}
        item = self.experiences.update_metadata(experience_id, patch, rebuild_index=True)
        return {"ok": True, "item": item, "index": {"ok": True, "mode": "queued_async_rebuild"}}

    def promote_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        item = self.find_experience(experience_id)
        if str(item.get("status") or "active") == "discarded":
            return {"ok": False, "message": "discarded rag experience cannot be promoted"}
        if str(item.get("status") or "active") == "promoted":
            return {"ok": False, "message": "rag experience has already been promoted"}
        review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
        if str(review.get("status") or "") in {"kept", AUTO_KEPT_REVIEW_STATUS}:
            return {
                "ok": False,
                "message": "这条AI经验池已经被人为保留；如需重新升级，请先点击\"重新待处理\"，再按AI建议操作。",
                "review_status": str(review.get("status") or ""),
            }
        formal_items = collect_formal_items()
        formal_revision = formal_items_revision(formal_items)
        annotated = annotate_experience(with_quality(item), formal_items)
        annotated["formal_revision"] = formal_revision
        annotated = attach_source_dialogue(
            annotated,
            raw_store=self.raw_messages,
            conversation_cache={},
        )
        relation = str(annotated.get("formal_relation") or "")
        if relation in {"covered_by_formal", "conflicts_formal"}:
            return {
                "ok": False,
                "message": "正式知识库已有高度重合或疑似冲突内容，请先查看AI比对结果，不要重复升级。",
                "formal_relation": relation,
                "formal_match": annotated.get("formal_match") or {},
                "cache_policy": "local_formal_overlap_check_without_llm",
            }
        interpretation, cache_policy = self.promotion_interpretation(annotated)
        annotated["ai_interpretation"] = interpretation
        triage_patch = build_auto_triage_patch(annotated, interpretation)
        if triage_patch:
            try:
                self.experiences.update_metadata(experience_id, triage_patch, rebuild_index=False)
            except KeyError:
                pass
        if str((interpretation or {}).get("recommended_action") or "") != "promote_to_pending" or not bool((interpretation or {}).get("promotion_allowed", False)):
            return {
                "ok": False,
                "message": "AI没有建议把这条AI经验池升级为待确认知识。它可能已被正式知识覆盖、只是一次客户不合理要求、来源不够权威，或更适合保留在经验层。",
                "ai_interpretation": interpretation,
                "formal_relation": relation,
                "formal_match": annotated.get("formal_match") or {},
                "cache_policy": cache_policy,
            }
        try:
            candidate = build_candidate_from_experience(annotated, preferred_category=str(payload.get("target_category") or ""))
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "ai_interpretation": interpretation}
        candidate_path = tenant_review_candidates_root() / "pending" / f"{candidate['candidate_id']}.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        upsert_candidate_to_db(candidate)
        reviewed_state = self._reviewed_state(item, payload, action="promote")
        updated = self.experiences.update_status(
            experience_id,
            status="promoted",
            reason="promoted_to_review_candidate",
            extra={
                "promoted_at": now(),
                "promoted_candidate_id": candidate["candidate_id"],
                "review_state": reviewed_state,
                "reviewed_by_user": True,
            },
        )
        return {
            "ok": True,
            "candidate": candidate,
            "item": updated,
            "index": {"ok": True, "mode": "queued_async_rebuild"},
            "cache_policy": cache_policy,
        }

    def resolve_formal_overlap(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        strategy = str(payload.get("strategy") or "").strip()
        allowed = {"keep_formal_discard_experience", "replace_formal_with_experience", "ai_merge_formal_and_experience"}
        if strategy not in allowed:
            return {"ok": False, "message": f"unsupported overlap strategy: {strategy}"}

        current = self.find_experience(experience_id)
        if str(current.get("status") or "active") != "active":
            return {"ok": False, "message": "only active experiences can resolve formal overlap"}

        formal_items = collect_formal_items()
        formal_revision = formal_items_revision(formal_items)
        annotated = annotate_experience(with_quality(current), formal_items)
        annotated["formal_revision"] = formal_revision
        annotated = attach_source_dialogue(
            annotated,
            raw_store=self.raw_messages,
            conversation_cache={},
        )
        if not has_actionable_formal_overlap(annotated):
            return {
                "ok": False,
                "message": "当前经验未检测到可操作的正式知识相近项，无需走相近冲突处理。",
                "formal_relation": str(annotated.get("formal_relation") or ""),
                "formal_match": annotated.get("formal_match") or {},
            }
        match = annotated.get("formal_match") if isinstance(annotated.get("formal_match"), dict) else {}
        category_id = str(match.get("category_id") or "")
        item_id = str(match.get("item_id") or "")
        formal_record = find_formal_record(formal_items, category_id=category_id, item_id=item_id)
        if not formal_record:
            return {
                "ok": False,
                "message": "相近正式知识已不存在或已被移除，请刷新后重试。",
                "formal_relation": str(annotated.get("formal_relation") or ""),
                "formal_match": match,
            }
        formal_category_id = str(formal_record.get("category_id") or "")
        if formal_category_id in PRODUCT_MASTER_CATEGORIES and strategy != "keep_formal_discard_experience":
            return {
                "ok": False,
                "message": "商品资料属于权威主数据，禁止用AI经验池覆盖或合并正式商品资料；请保留正式知识并废弃该经验。",
                "formal_relation": str(annotated.get("formal_relation") or ""),
                "formal_match": compact_formal_match(formal_record),
            }

        if strategy == "keep_formal_discard_experience":
            reviewed_state = self._reviewed_state(current, payload, action="discard")
            discarded = self.experiences.update_status(
                experience_id,
                status="discarded",
                reason="formal_knowledge_preferred_discard_experience",
                extra={
                    "discarded_at": now(),
                    "review_state": reviewed_state,
                    "reviewed_by_user": True,
                },
            )
            return {
                "ok": True,
                "strategy": strategy,
                "item": discarded,
                "formal_relation": str(annotated.get("formal_relation") or ""),
                "formal_match": compact_formal_match(formal_record),
                "message": "已按正式知识库为准，废弃这条经验。",
                "index": {"ok": True, "mode": "queued_async_rebuild"},
            }

        if strategy == "replace_formal_with_experience":
            replaced = save_formal_item(
                target_category=str(formal_record.get("category_id") or ""),
                item=replaced_formal_item_with_experience(
                    formal_record.get("item") if isinstance(formal_record.get("item"), dict) else {},
                    annotated,
                    target_category=str(formal_record.get("category_id") or ""),
                    formal_record=formal_record,
                ),
                reason=f"replace_formal_with_experience:{experience_id}",
                review_source={
                    "experience_id": experience_id,
                    "overlap_strategy": strategy,
                },
            )
            reviewed_state = self._reviewed_state(current, payload, action="discard")
            discarded = self.experiences.update_status(
                experience_id,
                status="discarded",
                reason="replace_formal_with_experience_discard_experience",
                extra={
                    "discarded_at": now(),
                    "review_state": reviewed_state,
                    "reviewed_by_user": True,
                },
            )
            return {
                "ok": True,
                "strategy": strategy,
                "item": discarded,
                "formal_item": replaced.get("item"),
                "formal_relation": str(annotated.get("formal_relation") or ""),
                "formal_match": compact_formal_match(formal_record),
                "compile": replaced.get("compile"),
                "message": "已按新经验覆盖更新正式知识库，并从AI经验池移除这条经验。",
                "index": {"ok": True, "mode": "queued_async_rebuild"},
            }

        interpretation, cache_policy = self.promotion_interpretation(annotated)
        annotated["ai_interpretation"] = interpretation
        triage_patch = build_auto_triage_patch(annotated, interpretation)
        if triage_patch:
            try:
                self.experiences.update_metadata(experience_id, triage_patch, rebuild_index=False)
            except KeyError:
                pass
        try:
            merged_item = merged_formal_item_with_experience(
                formal_record.get("item") if isinstance(formal_record.get("item"), dict) else {},
                annotated,
                target_category=str(formal_record.get("category_id") or ""),
                formal_record=formal_record,
            )
            saved = save_formal_item(
                target_category=str(formal_record.get("category_id") or ""),
                item=merged_item,
                reason=f"formal_overlap_ai_merge:{experience_id}",
                review_source={
                    "experience_id": experience_id,
                    "overlap_strategy": strategy,
                },
            )
            reviewed_state = self._reviewed_state(current, payload, action="discard")
            discarded = self.experiences.update_status(
                experience_id,
                status="discarded",
                reason="formal_overlap_ai_merge_discard_experience",
                extra={
                    "discarded_at": now(),
                    "review_state": reviewed_state,
                    "reviewed_by_user": True,
                },
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "formal_match": compact_formal_match(formal_record)}
        return {
            "ok": True,
            "strategy": strategy,
            "item": discarded,
            "formal_item": saved.get("item") if isinstance(saved, dict) else {},
            "formal_relation": str(annotated.get("formal_relation") or ""),
            "formal_match": compact_formal_match(formal_record),
            "ai_interpretation": interpretation,
            "cache_policy": cache_policy,
            "compile": saved.get("compile") if isinstance(saved, dict) else {},
            "message": "已完成AI合并并更新正式知识库，这条经验已从AI经验池移除。",
            "index": {"ok": True, "mode": "queued_async_rebuild"},
        }

    def promotion_interpretation(self, annotated: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Reuse AI advice on promotion when its cache is still valid."""
        existing = annotated.get("ai_interpretation") if isinstance(annotated.get("ai_interpretation"), dict) else {}
        if ai_interpretation_reusable_for_promotion(existing, annotated):
            reused = with_current_formal_comparison(
                existing,
                annotated,
                cache_policy="reuse_current_ai_advice_with_local_formal_check",
            )
            return apply_guardrails_to_interpretation(reused, annotated), "reuse_current_ai_advice_with_local_formal_check"
        if existing and not self.interpreter.needs_refresh(annotated):
            reused = with_current_formal_comparison(existing, annotated, cache_policy="reuse_current_ai_advice")
            return apply_guardrails_to_interpretation(reused, annotated), "reuse_current_ai_advice"
        interpretation = self.interpreter.ensure(annotated, force=True)
        return interpretation, "llm_refreshed_for_stale_or_missing_advice"

    def interpret_experiences(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        force = bool(payload.get("force"))
        limit = max(1, min(int(payload.get("limit") or 30), 100))
        ids = [str(item) for item in payload.get("experience_ids", []) if str(item)] if isinstance(payload.get("experience_ids"), list) else []
        formal_items = collect_formal_items()
        formal_revision = formal_items_revision(formal_items)
        source_items = []
        if ids:
            for experience_id in ids[:limit]:
                try:
                    source_items.append(self.find_experience(experience_id))
                except KeyError:
                    continue
        else:
            status = str(payload.get("status") or "all")
            source_items = self.experiences.list(status=status, limit=limit)

        interpreted = []
        for item in source_items[:limit]:
            enriched = with_quality(item)
            annotated = annotate_experience(enriched, formal_items)
            annotated["formal_revision"] = formal_revision
            interpretation = self.interpreter.ensure(annotated, force=force)
            triage_patch = build_auto_triage_patch(annotated, interpretation)
            if triage_patch:
                try:
                    # Keep retrieval behavior deterministic right after AI auto-triage.
                    self.experiences.update_metadata(str(item.get("experience_id") or ""), triage_patch, rebuild_index=True)
                except KeyError:
                    pass
            updated = self.find_experience(str(item.get("experience_id") or ""))
            updated = annotate_experience(with_quality(updated), formal_items)
            updated["formal_revision"] = formal_revision
            updated["ai_interpretation"] = interpretation
            updated = attach_source_dialogue(
                updated,
                raw_store=self.raw_messages,
                conversation_cache={},
            )
            interpreted.append(updated)
        return {
            "ok": True,
            "items": interpreted,
            "interpreted_count": len(interpreted),
            "model_count": sum(1 for item in interpreted if (item.get("ai_interpretation") or {}).get("provider") != "local_fallback"),
            "fallback_count": sum(1 for item in interpreted if (item.get("ai_interpretation") or {}).get("provider") == "local_fallback"),
        }

    def interpret_experience(self, experience_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        result = self.interpret_experiences({"experience_ids": [experience_id], "force": bool(payload.get("force", True)), "limit": 1})
        if not result["items"]:
            raise KeyError(experience_id)
        return {"ok": True, "item": result["items"][0]}

    def find_experience(self, experience_id: str) -> dict[str, Any]:
        for item in self.experiences.list(status="all", limit=5000):
            if str(item.get("experience_id") or "") == experience_id:
                return item
        raise KeyError(experience_id)


def persist_pending_candidate(candidate: dict[str, Any]) -> None:
    candidate_path = tenant_review_candidates_root() / "pending" / f"{candidate['candidate_id']}.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    upsert_candidate_to_db(candidate)


def has_actionable_formal_overlap(item: dict[str, Any]) -> bool:
    match = item.get("formal_match") if isinstance(item.get("formal_match"), dict) else {}
    if not str(match.get("category_id") or "") or not str(match.get("item_id") or ""):
        return False
    relation = str(item.get("formal_relation") or "")
    if relation in {"covered_by_formal", "supports_formal", "conflicts_formal"}:
        return True
    try:
        return float(match.get("similarity") or 0) >= 0.48
    except (TypeError, ValueError):
        return False


def find_formal_record(formal_items: list[dict[str, Any]], *, category_id: str, item_id: str) -> dict[str, Any] | None:
    for record in formal_items:
        if str(record.get("category_id") or "") == category_id and str(record.get("item_id") or "") == item_id:
            return record
    return None


def archive_formal_item_by_match(*, category_id: str, item_id: str, reason: str) -> dict[str, Any]:
    registry = KnowledgeRegistry()
    schema_manager = KnowledgeSchemaManager(registry)
    base_store = KnowledgeBaseStore(registry, schema_manager)
    archived = base_store.archive_item(category_id, item_id)
    if not archived.get("ok"):
        raise ValueError(str(archived.get("message") or f"failed to archive formal item: {category_id}/{item_id}"))
    compile_result = KnowledgeCompiler().compile_to_disk()
    return {"ok": True, "item": archived.get("item"), "compile": compile_result, "reason": reason}


def save_formal_item(
    *,
    target_category: str,
    item: dict[str, Any],
    reason: str,
    review_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not target_category:
        raise ValueError("formal target category is missing")
    if target_category in PRODUCT_MASTER_CATEGORIES:
        raise ValueError("商品资料属于权威主数据，禁止通过AI经验池相近处理写回；请在商品库手动维护。")
    if not isinstance(item, dict) or not item:
        raise ValueError("formal merged item is missing")
    registry = KnowledgeRegistry()
    schema_manager = KnowledgeSchemaManager(registry)
    base_store = KnowledgeBaseStore(registry, schema_manager)
    source = {
        "source_module": "rag_overlap",
        "target_category": target_category,
        "item_id": str(item.get("id") or ""),
        "reason": reason,
    }
    if isinstance(review_source, dict):
        source.update({key: value for key, value in review_source.items() if value not in (None, "")})
    review_target = dict(item)
    review_target["review_state"] = {}
    item_with_new_marker = mark_item_new(review_target, source)
    result = base_store.save_item(target_category, item_with_new_marker)
    if not result.get("ok"):
        raise ValueError(str(result.get("message") or f"failed to save formal item: {target_category}"))
    compile_result = KnowledgeCompiler().compile_to_disk()
    return {"ok": True, "item": result.get("item"), "compile": compile_result, "reason": reason}


def build_overlap_merge_candidate(item: dict[str, Any], formal_record: dict[str, Any]) -> dict[str, Any]:
    target_category = str(formal_record.get("category_id") or "")
    formal_item = formal_record.get("item") if isinstance(formal_record.get("item"), dict) else {}
    if not target_category or not formal_item:
        raise ValueError("formal overlap target is missing")
    source_decision = evaluate_experience_source_authority(item, target_category)
    if not source_decision.get("allowed"):
        raise ValueError(str(source_decision.get("message") or source_decision.get("reason") or "source is not authoritative for overlap merge"))
    merged_item = merged_formal_item_with_experience(formal_item, item, target_category=target_category, formal_record=formal_record)
    candidate_id = "rag_overlap_merge_" + stable_digest(
        f"{item.get('experience_id')}:{target_category}:{formal_record.get('item_id')}:{now()}",
        20,
    )
    title = formal_title(formal_item) or str(formal_record.get("item_id") or "")
    summary = f"AI合并更新：在正式知识「{title}」基础上吸收这条新经验，生成待确认更新。"
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "generated_at": now(),
        "source": {
            "type": "rag_formal_overlap_merge",
            "experience_id": item.get("experience_id"),
            "formal_category_id": target_category,
            "formal_item_id": str(formal_record.get("item_id") or ""),
            "formal_title": title,
            "evidence_excerpt": candidate_business_evidence(item),
        },
        "detected_tags": ["rag_experience", "formal_overlap", "ai_merge", target_category],
        "proposal": {
            "target_category": target_category,
            "change_type": "rag_formal_overlap_merge",
            "summary": summary,
            "suggested_fields": merged_item.get("data") if isinstance(merged_item.get("data"), dict) else {},
            "formal_patch": {
                "target_category": target_category,
                "operation": "upsert_item",
                "item": merged_item,
            },
        },
        "review": {
            "status": "pending",
            "requires_human_approval": True,
            "allowed_auto_apply": False,
            "completeness_status": "ready",
            "rag_experience_id": item.get("experience_id"),
            "source_authority": source_decision,
            "llm_assist": {
                **rag_experience_llm_assist(item),
                "stage": "rag_formal_overlap_merge_candidate",
                "reason": "formal_overlap_ai_merge",
            },
        },
        "intake": {
            "status": "ready",
            "missing_fields": [],
            "missing_labels": [],
            "warnings": [],
            "confidence": 0.78,
            "question": "",
        },
    }


def merged_formal_item_with_experience(
    formal_item: dict[str, Any],
    experience_item: dict[str, Any],
    *,
    target_category: str,
    formal_record: dict[str, Any],
) -> dict[str, Any]:
    existing_data = formal_item.get("data") if isinstance(formal_item.get("data"), dict) else {}
    experience_data = candidate_data_for_category(target_category, experience_item)
    merged_data = merge_payload(existing_data, experience_data, path=("data",), target_category=target_category)
    merged_details = merged_data.get("additional_details") if isinstance(merged_data.get("additional_details"), dict) else {}
    comparison = (
        (experience_item.get("ai_interpretation") or {}).get("formal_knowledge_comparison")
        if isinstance(experience_item.get("ai_interpretation"), dict)
        else {}
    )
    differences = comparison.get("differences") if isinstance(comparison, dict) and isinstance(comparison.get("differences"), list) else []
    merged_details.update(
        {
            "AI合并来源": "formal_overlap_merge",
            "来源经验ID": str(experience_item.get("experience_id") or ""),
            "原正式知识": f"{formal_record.get('category_id')}/{formal_record.get('item_id')}",
        }
    )
    if differences:
        merged_details["AI识别差异"] = "；".join(str(item) for item in differences if str(item).strip())[:500]
    merged_data["additional_details"] = compact_dict(merged_details)
    return {
        **formal_item,
        "category_id": target_category,
        "id": str(formal_item.get("id") or formal_record.get("item_id") or ""),
        "status": "active",
        "source": {
            **(formal_item.get("source") if isinstance(formal_item.get("source"), dict) else {}),
            "type": "rag_formal_overlap_merge",
            "experience_id": experience_item.get("experience_id"),
        },
        "data": merged_data,
        "runtime": merge_payload(
            formal_item.get("runtime") if isinstance(formal_item.get("runtime"), dict) else {},
            experience_runtime_hints(experience_data),
            path=("runtime",),
            target_category=target_category,
        ),
    }


def replaced_formal_item_with_experience(
    formal_item: dict[str, Any],
    experience_item: dict[str, Any],
    *,
    target_category: str,
    formal_record: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(formal_item, dict) or not formal_item:
        raise ValueError("formal item is missing for replace strategy")
    experience_data = candidate_data_for_category(target_category, experience_item)
    if not isinstance(experience_data, dict) or not experience_data:
        raise ValueError("experience payload is empty and cannot replace formal knowledge")
    existing_data = formal_item.get("data") if isinstance(formal_item.get("data"), dict) else {}
    replaced_data = {**experience_data}
    for key in ("product_id", "sku", "category"):
        if key not in replaced_data and key in existing_data and existing_data.get(key) not in (None, "", [], {}):
            replaced_data[key] = existing_data.get(key)
    details = replaced_data.get("additional_details") if isinstance(replaced_data.get("additional_details"), dict) else {}
    details.update(
        {
            "来源经验ID": str(experience_item.get("experience_id") or ""),
            "原正式知识": f"{formal_record.get('category_id')}/{formal_record.get('item_id')}",
            "替换策略": "new_experience_preferred",
        }
    )
    replaced_data["additional_details"] = compact_dict(details)
    runtime = formal_item.get("runtime") if isinstance(formal_item.get("runtime"), dict) else {}
    return {
        **formal_item,
        "category_id": target_category,
        "id": str(formal_item.get("id") or formal_record.get("item_id") or ""),
        "status": "active",
        "source": {
            **(formal_item.get("source") if isinstance(formal_item.get("source"), dict) else {}),
            "type": "rag_formal_overlap_replace",
            "experience_id": experience_item.get("experience_id"),
        },
        "data": replaced_data,
        "runtime": merge_payload(runtime, experience_runtime_hints(experience_data), path=("runtime",), target_category=target_category),
    }


def experience_runtime_hints(data: dict[str, Any]) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    if "allow_auto_reply" in data:
        hints["allow_auto_reply"] = bool(data.get("allow_auto_reply"))
    if "requires_handoff" in data:
        hints["requires_handoff"] = bool(data.get("requires_handoff"))
    if data.get("operator_alert") is True:
        hints["operator_alert"] = True
    return hints


def merge_payload(
    base: Any,
    incoming: Any,
    *,
    path: tuple[str, ...] = (),
    target_category: str = "",
) -> Any:
    if isinstance(base, dict) and isinstance(incoming, dict):
        merged = {**base}
        for key, value in incoming.items():
            if key not in merged:
                merged[key] = value
                continue
            merged[key] = merge_payload(
                merged.get(key),
                value,
                path=(*path, str(key)),
                target_category=target_category,
            )
        return merged
    if isinstance(base, list) and isinstance(incoming, list):
        merged = []
        seen = set()
        for value in [*base, *incoming]:
            marker = stable_digest(json.dumps(value, ensure_ascii=False, sort_keys=True), 16)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(value)
        return merged
    if incoming in (None, "", [], {}):
        return base
    if base in (None, "", [], {}):
        return incoming
    if isinstance(base, str) and isinstance(incoming, str):
        merged_text = semantic_merge_text(
            base,
            incoming,
            field_path=path,
            target_category=target_category,
        )
        if merged_text:
            return merged_text
        if incoming in base:
            return base
        if base in incoming:
            return incoming
        return f"{base}\n{incoming}"
    return incoming


SEMANTIC_MERGE_FIELD_KEYS = {
    "answer",
    "content",
    "service_reply",
    "shipping_policy",
    "warranty_policy",
}
SEMANTIC_MERGE_PATH_KEYS = {"reply_templates"}
SHIPPING_SIGNAL_TERMS = (
    "包邮",
    "运费",
    "发货",
    "物流",
    "偏远",
    "remote_area",
    "same_day_cutoff",
    "tracking_note",
    "carriers",
)


def semantic_merge_text(
    base_text: str,
    incoming_text: str,
    *,
    field_path: tuple[str, ...],
    target_category: str = "",
) -> str:
    left = str(base_text or "").strip()
    right = str(incoming_text or "").strip()
    if not left:
        return right
    if not right:
        return left
    if right in left:
        return left
    if left in right:
        return right
    if not should_run_semantic_text_merge(field_path, left, right):
        return ""
    llm_merged = llm_merge_text(left, right, field_path=field_path, target_category=target_category)
    if llm_merged:
        return llm_merged
    heuristic_merged = heuristic_merge_shipping_rule(left, right)
    if heuristic_merged:
        return heuristic_merged
    return ""


def should_run_semantic_text_merge(field_path: tuple[str, ...], base_text: str, incoming_text: str) -> bool:
    key = str(field_path[-1]).lower() if field_path else ""
    if key in SEMANTIC_MERGE_FIELD_KEYS:
        return True
    path_keys = {str(part).lower() for part in field_path}
    if path_keys & SEMANTIC_MERGE_PATH_KEYS:
        return True
    text = f"{base_text}\n{incoming_text}".lower()
    if any(term.lower() in text for term in SHIPPING_SIGNAL_TERMS):
        return True
    if looks_like_json_text(base_text):
        return True
    return False


def llm_merge_text(
    base_text: str,
    incoming_text: str,
    *,
    field_path: tuple[str, ...],
    target_category: str,
) -> str:
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return ""
    base_is_json = looks_like_json_text(base_text)
    prompt = {
        "task": "合并同一字段的旧值与新值，输出一个最终版本，去重并保持语义一致。",
        "rules": [
            "不能简单拼接文本；必须做语义融合，避免重复。",
            "不能丢失原有有效信息；新信息若与旧信息冲突，应以更具体、更新的业务规则为准。",
            "如果旧值是 JSON 对象文本，优先保持 JSON 结构；在合适字段补充新规则。",
            "如果出现包邮/物流/偏远地区规则，需归并成一套清晰规则，避免多条互相覆盖或冲突。",
            "只输出 JSON 对象，不要输出 Markdown。",
        ],
        "context": {
            "target_category": target_category,
            "field_path": "/".join(field_path),
            "base_is_json": base_is_json,
        },
        "response_shape": {
            "merged_text": "string, 合并后的最终字段值",
            "merged_json": "object|null, 当应保持 JSON 结构时填 object，否则填 null",
            "notes": "string, 简短说明",
        },
        "base_text": truncate(base_text, 3200),
        "incoming_text": truncate(incoming_text, 3200),
    }
    payload = {
        "model": resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret),
        "messages": [
            {"role": "system", "content": "你是电商客服知识库合并助手。严格按要求输出 JSON 对象。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": resolve_deepseek_max_tokens(1400, read_secret_fn=read_secret),
        "response_format": {"type": "json_object"},
    }
    apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)
    request = urllib.request.Request(
        url=resolve_deepseek_base_url(read_secret_fn=read_secret).rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with llm_urlopen(request, timeout=resolve_deepseek_timeout(60, read_secret_fn=read_secret)) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return ""
    content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
    parsed = parse_structured_payload(content)
    if not isinstance(parsed, dict) or not parsed:
        return ""
    merged_json = parsed.get("merged_json")
    if isinstance(merged_json, (dict, list)):
        return json.dumps(merged_json, ensure_ascii=False, indent=2)
    merged_text = str(parsed.get("merged_text") or "").strip()
    if merged_text:
        return merged_text
    return ""


def heuristic_merge_shipping_rule(base_text: str, incoming_text: str) -> str:
    if not has_shipping_signal(base_text + "\n" + incoming_text):
        return ""
    base_payload = parse_structured_payload(base_text)
    if not isinstance(base_payload, dict) or not base_payload:
        return ""
    merged = dict(base_payload)
    incoming_rule = normalize_sentence(incoming_text)
    if not incoming_rule:
        return ""
    rule_key = next(
        (
            key
            for key in ("free_shipping_rule", "free_shipping_policy", "shipping_discount_rule", "shipping_rule", "包邮规则")
            if key in merged
        ),
        "free_shipping_rule",
    )
    merged[rule_key] = merge_sentence(str(merged.get(rule_key) or "").strip(), incoming_rule)
    if any(term in incoming_rule for term in ("偏远", "新疆", "西藏", "青海", "内蒙", "港澳台")):
        remote_note = str(merged.get("remote_area_note") or "").strip()
        merged["remote_area_note"] = merge_sentence(remote_note, extract_remote_clause(incoming_rule) or incoming_rule)
    return json.dumps(merged, ensure_ascii=False, indent=2)


def has_shipping_signal(text: str) -> bool:
    value = str(text or "").lower()
    return any(term.lower() in value for term in SHIPPING_SIGNAL_TERMS)


def merge_sentence(base: str, incoming: str) -> str:
    left = normalize_sentence(base)
    right = normalize_sentence(incoming)
    if not left:
        return right
    if not right:
        return left
    if right in left:
        return left
    if left in right:
        return right
    return f"{left.rstrip('。；;')}；{right.lstrip('。；;')}"


def normalize_sentence(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_remote_clause(text: str) -> str:
    parts = re.split(r"[。；;]\s*", str(text or ""))
    for part in parts:
        snippet = part.strip()
        if not snippet:
            continue
        if any(term in snippet for term in ("偏远", "新疆", "西藏", "青海", "内蒙", "港澳台")):
            return snippet
    return ""


def looks_like_json_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value[0] not in "{[":
        return False
    parsed = parse_structured_payload(value)
    return isinstance(parsed, dict) and bool(parsed)


def collect_formal_items() -> list[dict[str, Any]]:
    runtime = KnowledgeRuntime()
    category_ids = [str(item.get("id") or "") for item in runtime.list_categories(enabled_only=True)]
    for category_id in ("products", "policies", "chats", "erp_exports", *sorted(PRODUCT_SCOPED_CATEGORIES)):
        if category_id not in category_ids:
            category_ids.append(category_id)
    items: list[dict[str, Any]] = []
    for category_id in category_ids:
        if not category_id:
            continue
        try:
            records = runtime.list_items(category_id, include_archived=False, include_unacknowledged=True)
        except Exception:
            continue
        for record in records:
            items.append(
                {
                    "category_id": category_id,
                    "item_id": str(record.get("id") or ""),
                    "product_id": str((record.get("data") or {}).get("product_id") or ""),
                    "title": formal_title(record),
                    "text": formal_text(record),
                    "item": record,
                }
            )
    return items


def formal_items_revision(items: list[dict[str, Any]]) -> str:
    payload = [
        {
            "category_id": item.get("category_id"),
            "item_id": item.get("item_id"),
            "product_id": item.get("product_id"),
            "title": item.get("title"),
            "text_hash": stable_digest(str(item.get("text") or ""), 16),
        }
        for item in sorted(
            items,
            key=lambda value: (
                str(value.get("category_id") or ""),
                str(value.get("item_id") or ""),
                str(value.get("product_id") or ""),
            ),
        )
    ]
    return stable_digest(json.dumps(payload, ensure_ascii=False, sort_keys=True), 24)


def auto_triage_should_discard(review: dict[str, Any] | None) -> bool:
    if not isinstance(review, dict):
        return False
    if str(review.get("status") or "") != AUTO_TRIAGE_REVIEW_STATUS:
        return False
    return str(review.get("auto_triage_action") or "") in {"discard", "already_covered"}


def experience_review_status(item: dict[str, Any]) -> str:
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    return str(review.get("status") or "")


def rag_experience_display_state(item: dict[str, Any], *, relation_value: str = "") -> str:
    status = str(item.get("status") or "active")
    relation = str(relation_value or item.get("formal_relation") or status or "")
    review_status = experience_review_status(item)
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    governance = item.get("governance") if isinstance(item.get("governance"), dict) else {}
    if governance:
        return governance_display_state(item, fallback="pending")
    if status == "discarded" or auto_triage_should_discard(review):
        return "discarded"
    if status == "promoted" or relation == "promoted":
        return "promoted"
    if review_status in {"kept", AUTO_KEPT_REVIEW_STATUS} or relation in {"kept_experience", "auto_kept_experience"}:
        return "kept"
    return "pending"


def annotate_experience(item: dict[str, Any], formal_items: list[dict[str, Any]]) -> dict[str, Any]:
    annotated = dict(item)
    status = str(item.get("status") or "active")
    if status in {"discarded", "promoted"}:
        annotated["formal_relation"] = status
        annotated["formal_match"] = {}
        annotated["recommended_action"] = "already_" + status
        return annotated

    text = experience_text(item)
    best = best_formal_match(item, text, formal_items)
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    review_status = str(review.get("status") or "")
    relation = "novel"
    action = "keep_as_rag_experience"
    if best and detect_conflict(text, best):
        relation = "conflicts_formal"
        action = "manual_review_conflict"
    elif best and best["similarity"] >= 0.78:
        relation = "covered_by_formal"
        action = "keep_low_priority_or_discard"
    elif review_status == AUTO_KEPT_REVIEW_STATUS:
        relation = "auto_kept_experience"
        action = "system_auto_kept_as_experience"
    elif auto_triage_should_discard(review):
        relation = "discarded"
        action = "already_discarded"
    elif review_status == "kept":
        relation = "kept_experience"
        action = "kept_as_experience"
    elif best and best["similarity"] >= 0.48:
        relation = "supports_formal"
        action = "keep_as_supporting_expression"
    elif is_promotion_candidate(item):
        relation = "promotion_candidate"
        action = "promote_to_review_candidate"

    structured = structured_payload_from_experience(item)
    if is_product_payload(structured):
        source_decision = evaluate_experience_source_authority(item, "products")
        annotated["source_authority"] = source_decision
        if not source_decision.get("allowed") and relation not in {"covered_by_formal", "conflicts_formal"}:
            relation = "blocked_by_source_policy"
            action = "keep_as_rag_experience"

    annotated["formal_relation"] = relation
    annotated["formal_match"] = compact_formal_match(best)
    annotated["recommended_action"] = action
    return annotated


def annotate_experience_from_cache(item: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(item)
    status = str(item.get("status") or "active")
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    review_status = str(review.get("status") or "")
    if status in {"discarded", "promoted"}:
        annotated["formal_relation"] = status
        annotated["formal_match"] = {}
        annotated["recommended_action"] = "already_" + status
        return annotated
    cached = item.get("formal_relation_cache") if isinstance(item.get("formal_relation_cache"), dict) else {}
    if cached:
        annotated["formal_relation"] = cached.get("relation") or "unknown"
        annotated["formal_match"] = cached.get("formal_match") if isinstance(cached.get("formal_match"), dict) else {}
        annotated["recommended_action"] = cached.get("recommended_action") or ""
        if review_status == AUTO_KEPT_REVIEW_STATUS:
            annotated["formal_relation"] = "auto_kept_experience"
            annotated["recommended_action"] = "system_auto_kept_as_experience"
        elif auto_triage_should_discard(review):
            annotated["formal_relation"] = "discarded"
            annotated["recommended_action"] = "already_discarded"
        elif review_status == "kept":
            annotated["formal_relation"] = "kept_experience"
            annotated["recommended_action"] = "kept_as_experience"
        return annotated
    return annotate_experience(annotated, [])


def build_relation_cache(item: dict[str, Any], *, formal_item_count: int, formal_revision: str = "") -> dict[str, Any]:
    existing = item.get("formal_relation_cache") if isinstance(item.get("formal_relation_cache"), dict) else {}
    stable = {
        "relation": item.get("formal_relation") or "unknown",
        "formal_match": item.get("formal_match") or {},
        "recommended_action": item.get("recommended_action") or "",
        "formal_item_count": int(formal_item_count or 0),
        "formal_revision": formal_revision,
    }
    if relation_cache_stable_part(existing) == stable:
        stable["evaluated_at"] = existing.get("evaluated_at") or now()
    else:
        stable["evaluated_at"] = now()
    return stable


def relation_cache_stable_part(cache: Any) -> dict[str, Any]:
    if not isinstance(cache, dict):
        return {}
    return {
        "relation": cache.get("relation") or "unknown",
        "formal_match": cache.get("formal_match") or {},
        "recommended_action": cache.get("recommended_action") or "",
        "formal_item_count": int(cache.get("formal_item_count") or 0),
        "formal_revision": cache.get("formal_revision") or "",
    }


def relation_cache_needs_update(existing: Any, new_cache: dict[str, Any]) -> bool:
    if not isinstance(existing, dict):
        return True
    return relation_cache_stable_part(existing) != relation_cache_stable_part(new_cache)


def best_formal_match(item: dict[str, Any], text: str, formal_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not text.strip():
        return None
    hit = item.get("rag_hit", {}) or {}
    experience_product_id = str(hit.get("product_id") or "")
    experience_category = str(hit.get("category") or "")
    best: dict[str, Any] | None = None
    for formal in formal_items:
        score = text_similarity(text, str(formal.get("text") or ""))
        score = max(score, business_key_similarity(item, formal))
        if experience_product_id and experience_product_id == str(formal.get("product_id") or ""):
            score += 0.08
        if experience_category and experience_category == str(formal.get("category_id") or ""):
            score += 0.04
        score = min(score, 1.0)
        if not best or score > float(best.get("similarity") or 0):
            best = {**formal, "similarity": round(score, 3)}
    if not best or float(best.get("similarity") or 0) < 0.22:
        return None
    return best


def business_key_similarity(item: dict[str, Any], formal: dict[str, Any]) -> float:
    payload = structured_payload_from_experience(item)
    formal_item = formal.get("item") if isinstance(formal.get("item"), dict) else {}
    formal_data = formal_item.get("data") if isinstance(formal_item.get("data"), dict) else {}
    if not payload or not formal_data:
        return 0.0
    payload_sku = normalized_fingerprint(payload.get("sku") or payload.get("external_id") or "")
    formal_sku = normalized_fingerprint(formal_data.get("sku") or formal_data.get("external_id") or "")
    if payload_sku and formal_sku and payload_sku == formal_sku:
        return 0.99
    payload_name = normalized_fingerprint(payload.get("name") or payload.get("title") or "")
    formal_name = normalized_fingerprint(formal_data.get("name") or formal_data.get("title") or formal.get("title") or "")
    if payload_name and formal_name:
        if payload_name == formal_name:
            return 0.93
        if payload_name in formal_name or formal_name in payload_name:
            return 0.86
    payload_title = normalized_fingerprint(payload.get("customer_message") or "")
    formal_title_value = normalized_fingerprint(formal_data.get("customer_message") or "")
    if payload_title and formal_title_value and payload_title == formal_title_value:
        return 0.88
    return 0.0


def compact_formal_match(match: dict[str, Any] | None) -> dict[str, Any]:
    if not match:
        return {}
    return {
        "category_id": match.get("category_id"),
        "item_id": match.get("item_id"),
        "product_id": match.get("product_id"),
        "title": match.get("title"),
        "similarity": match.get("similarity"),
        "excerpt": truncate(str(match.get("text") or ""), 900),
    }


def with_current_formal_comparison(existing: dict[str, Any], item: dict[str, Any], *, cache_policy: str) -> dict[str, Any]:
    interpretation = dict(existing)
    interpretation["cache_policy"] = cache_policy
    interpretation["content_fingerprint"] = content_fingerprint(item)
    interpretation["formal_revision"] = str(item.get("formal_revision") or interpretation.get("formal_revision") or "")
    interpretation["formal_knowledge_comparison"] = current_formal_comparison(item, existing.get("formal_knowledge_comparison"))
    return interpretation


def ai_interpretation_reusable_for_promotion(existing: dict[str, Any], item: dict[str, Any]) -> bool:
    if not existing:
        return False
    if interpretation_looks_corrupted(existing):
        return False
    if str(existing.get("version") or "") != INTERPRETATION_VERSION:
        return False
    action = str(existing.get("recommended_action") or "")
    if action not in {
        "promote_to_pending",
        "keep_as_experience",
        "discard",
        "manual_review",
        "already_covered",
        "needs_more_info",
    }:
        return False
    current_content = content_fingerprint(item)
    existing_content = str(existing.get("content_fingerprint") or "")
    if existing_content:
        return existing_content == current_content
    existing_source = str(existing.get("source_fingerprint") or "")
    return bool(existing_source and existing_source == interpretation_fingerprint_for_admin(item))


def interpretation_fingerprint_for_admin(item: dict[str, Any]) -> str:
    payload = {
        "experience_id": item.get("experience_id"),
        "source": item.get("source"),
        "source_type": item.get("source_type"),
        "summary": item.get("summary"),
        "question": item.get("question"),
        "reply_text": item.get("reply_text"),
        "evidence_excerpt": item.get("evidence_excerpt"),
        "rag_hit": item.get("rag_hit"),
        "formal_relation": item.get("formal_relation"),
        "formal_match": item.get("formal_match"),
    }
    return stable_digest(json.dumps(payload, ensure_ascii=False, sort_keys=True), 24)


def current_formal_comparison(item: dict[str, Any], previous: Any = None) -> dict[str, Any]:
    match = item.get("formal_match") if isinstance(item.get("formal_match"), dict) else {}
    relation = str(item.get("formal_relation") or "")
    try:
        similarity = round(float(match.get("similarity") or 0), 3)
    except (TypeError, ValueError):
        similarity = None
    if relation in {"covered_by_formal", "conflicts_formal"} or (similarity is not None and similarity >= 0.78):
        overlap_level = "high"
    elif relation == "supports_formal" or (similarity is not None and similarity >= 0.48):
        overlap_level = "medium"
    elif match.get("item_id"):
        overlap_level = "low"
    else:
        overlap_level = "none"
    previous_comparison = previous if isinstance(previous, dict) else {}
    return {
        **previous_comparison,
        "overlap_level": overlap_level,
        "matched_title": match.get("title") or previous_comparison.get("matched_title") or "",
        "matched_category": match.get("category_id") or previous_comparison.get("matched_category") or "",
        "matched_item_id": match.get("item_id") or previous_comparison.get("matched_item_id") or "",
        "similarity": similarity,
        "conclusion": formal_comparison_conclusion(overlap_level, match),
    }


def formal_comparison_conclusion(overlap_level: str, match: dict[str, Any]) -> str:
    title = str(match.get("title") or "").strip()
    if overlap_level == "high":
        return f"当前正式知识库已高度覆盖这条经验：{title}" if title else "当前正式知识库已高度覆盖这条经验。"
    if overlap_level == "medium":
        return f"当前正式知识库有相近内容，建议只核对差异点：{title}" if title else "当前正式知识库有相近内容，建议只核对差异点。"
    if overlap_level == "low":
        return f"当前正式知识库有弱相关内容，可继续人工判断是否升级：{title}" if title else "当前正式知识库有弱相关内容，可继续人工判断是否升级。"
    return "当前正式知识库暂未发现明显重复内容。"


def is_promotion_candidate(item: dict[str, Any]) -> bool:
    if experience_is_pipeline_trace(item) and not pipeline_trace_has_promotable_business_content(item):
        return False
    usage = item.get("usage", {}) or {}
    reply_count = int(usage.get("reply_count", 1) or 1)
    hit = item.get("rag_hit", {}) or {}
    risk_terms = [str(value) for value in hit.get("risk_terms", []) or [] if str(value)]
    if risk_terms:
        return False
    return reply_count >= 2


def detect_conflict(experience: str, formal: dict[str, Any]) -> bool:
    formal_text_value = str(formal.get("text") or "")
    combined = experience + "\n" + formal_text_value
    if not text_has_visible_term(combined, "risk_or_decision_terms"):
        return False
    experience_numbers = set(re.findall(r"\d+(?:\.\d+)?", experience))
    formal_numbers = set(re.findall(r"\d+(?:\.\d+)?", formal_text_value))
    if experience_numbers and formal_numbers and experience_numbers.isdisjoint(formal_numbers):
        return float(formal.get("similarity") or 0) >= 0.32
    return False


def build_candidate_from_experience(item: dict[str, Any], *, preferred_category: str = "") -> dict[str, Any]:
    if experience_is_pipeline_trace(item) and not pipeline_trace_has_promotable_business_content(item):
        raise ValueError("这条AI经验像系统流水线记录，不是客户可用知识；请废弃或保留在经验层，不要升级为待确认知识。")
    if is_product_payload(structured_payload_from_experience(item)):
        raise ValueError("这条内容命中商品资料主数据，不能升级为商品资料；商品资料只能通过商品库手动导入/维护。")
    hit = item.get("rag_hit", {}) or {}
    category_id = choose_candidate_category(item, preferred_category=preferred_category)
    if category_id in PRODUCT_MASTER_CATEGORIES:
        raise ValueError("这条内容命中商品资料主数据，不能升级为商品资料；商品资料只能通过商品库手动导入/维护。")
    source_decision = evaluate_experience_source_authority(item, category_id)
    if not source_decision.get("allowed"):
        raise ValueError(str(source_decision.get("message") or source_decision.get("reason") or "source is not authoritative for this candidate category"))
    data = candidate_data_for_category(category_id, item)
    if category_id == "chats" and not str(data.get("service_reply") or "").strip():
        raise ValueError("这条AI经验池没有找到明确的客服回复，不能升级为聊天话术；请保留为经验或废弃。")
    item_id = safe_item_id(data.get("sku") or data.get("title") or data.get("customer_message") or item.get("experience_id") or "rag_experience")
    candidate_id = "rag_promote_" + stable_digest(str(item.get("experience_id") or json.dumps(item, ensure_ascii=False)), 20)
    candidate_item = {
        "schema_version": 1,
        "category_id": category_id,
        "id": item_id,
        "status": "active",
        "source": {"type": "rag_experience", "experience_id": item.get("experience_id")},
        "data": data,
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }
    summary = candidate_business_summary(category_id, data, item)
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "generated_at": now(),
        "source": {
            "type": "rag_experience",
            "experience_id": item.get("experience_id"),
            "rag_hit": hit,
            "evidence_excerpt": candidate_business_evidence(item),
        },
        "detected_tags": ["rag_experience", category_id],
        "proposal": {
            "target_category": category_id,
            "change_type": "rag_experience_promote",
            "summary": summary,
            "suggested_fields": data,
            "formal_patch": {
                "target_category": category_id,
                "operation": "upsert_item",
                "item": candidate_item,
            },
        },
        "review": {
            "status": "pending",
            "requires_human_approval": True,
            "allowed_auto_apply": False,
            "completeness_status": "ready",
            "rag_experience_id": item.get("experience_id"),
            "llm_assist": rag_experience_llm_assist(item),
            "source_authority": source_decision,
        },
        "intake": {
            "status": "ready",
            "missing_fields": [],
            "missing_labels": [],
            "warnings": [],
            "confidence": 0.72,
            "question": "",
        },
    }


def choose_candidate_category(item: dict[str, Any], *, preferred_category: str = "") -> str:
    if preferred_category in {"products", "policies", "chats", "erp_exports", *PRODUCT_SCOPED_CATEGORIES}:
        if preferred_category in PRODUCT_SCOPED_CATEGORIES and not str((item.get("rag_hit") or {}).get("product_id") or ""):
            return "chats"
        return preferred_category
    structured = structured_payload_from_experience(item)
    if is_product_payload(structured):
        return "products"
    if is_chat_payload(structured) and (experience_contains_model_reply(item) or structured.get("customer_message") or structured.get("service_reply") or structured.get("question")):
        return "chats"
    if is_policy_payload(structured):
        return "policies"
    if is_chat_payload(structured):
        return "chats"
    hit = item.get("rag_hit", {}) or {}
    hit_category = str(hit.get("category") or "")
    hit_source_type = str(hit.get("source_type") or "")
    if hit_category in PRODUCT_SCOPED_CATEGORIES and str(hit.get("product_id") or ""):
        return hit_category
    if hit_category == "policies" or "policy" in hit_source_type:
        return "policies"
    return "chats"


def candidate_data_for_category(category_id: str, item: dict[str, Any]) -> dict[str, Any]:
    hit = item.get("rag_hit", {}) or {}
    structured = structured_payload_from_experience(item)
    source_chat = chat_payload_from_source_dialogue(item, structured)
    if source_chat:
        structured = {**structured, **source_chat}
    question = str(item.get("question") or "")
    reply = str(item.get("reply_text") or "")
    hit_text = str(hit.get("text") or "")
    title = candidate_title_from_payload(structured) or truncate(str(item.get("summary") or question or reply or "AI experience pool item"), 64)
    keywords = keyword_list(" ".join([question, reply, hit_text]))
    additional_details = {
        "rag_experience_id": item.get("experience_id"),
        "source_chunk_id": hit.get("chunk_id"),
        "source_id": hit.get("source_id"),
        "source_text": truncate(hit_text, 500),
    }
    if category_id == "products" and is_product_payload(structured):
        extra = structured.get("additional_details") if isinstance(structured.get("additional_details"), dict) else {}
        return compact_dict(
            {
                "name": str(structured.get("name") or structured.get("title") or title),
                "sku": str(structured.get("sku") or structured.get("external_id") or ""),
                "category": str(structured.get("category") or ""),
                "price": structured.get("price"),
                "unit": str(structured.get("unit") or "台"),
                "inventory": structured.get("inventory"),
                "shipping_policy": str(structured.get("shipping_policy") or ""),
                "warranty_policy": str(structured.get("warranty_policy") or ""),
                "specs": str(structured.get("specs") or ""),
                "aliases": structured.get("aliases") if isinstance(structured.get("aliases"), list) else [],
                "price_tiers": structured.get("price_tiers") if isinstance(structured.get("price_tiers"), list) else [],
                "reply_templates": structured.get("reply_templates") if isinstance(structured.get("reply_templates"), dict) else {},
                "additional_details": compact_dict({**extra, "来源": "AI经验升级", "rag_experience_id": item.get("experience_id")}),
            }
        )
    if category_id == "products":
        return compact_dict(
            {
                "name": title,
                "unit": "台",
                "additional_details": additional_details,
            }
        )
    if category_id == "policies" and is_policy_payload(structured):
        extra = structured.get("additional_details") if isinstance(structured.get("additional_details"), dict) else {}
        return compact_dict(
            {
                "title": str(structured.get("title") or title),
                "policy_type": str(structured.get("policy_type") or "other"),
                "keywords": structured.get("keywords") if isinstance(structured.get("keywords"), list) else keywords,
                "applicability_scope": str(structured.get("applicability_scope") or "global"),
                "answer": str(structured.get("answer") or structured.get("handoff_reason") or reply or hit_text),
                "allow_auto_reply": bool(structured.get("allow_auto_reply", True)),
                "requires_handoff": bool(structured.get("requires_handoff", False)),
                "handoff_reason": str(structured.get("handoff_reason") or ""),
                "operator_alert": bool(structured.get("operator_alert", False)),
                "risk_level": str(structured.get("risk_level") or "normal"),
                "product_id": str(structured.get("product_id") or ""),
                "additional_details": compact_dict({**extra, "来源": "AI经验升级", "rag_experience_id": item.get("experience_id")}),
            }
        )
    if is_chat_payload(structured):
        question = str(structured.get("customer_message") or structured.get("question") or structured.get("title") or question)
        reply = str(structured.get("service_reply") or structured.get("answer") or reply)
    if category_id == "policies":
        return compact_dict(
            {
                "title": title,
                "policy_type": "other",
                "keywords": keywords,
                "answer": reply or hit_text,
                "allow_auto_reply": True,
                "requires_handoff": False,
                "operator_alert": False,
                "risk_level": "normal",
                "additional_details": additional_details,
            }
        )
    if category_id in PRODUCT_SCOPED_CATEGORIES:
        product_id = str(hit.get("product_id") or "")
        base = {"product_id": product_id, "title": title, "keywords": keywords, "additional_details": additional_details}
        if category_id == "product_faq":
            return compact_dict({**base, "question": question, "answer": reply or hit_text})
        if category_id == "product_rules":
            return compact_dict({**base, "answer": reply or hit_text, "allow_auto_reply": True, "requires_handoff": False})
        return compact_dict({**base, "content": reply or hit_text})
    return compact_dict(
        {
            "customer_message": normalize_chat_candidate_question(question, item),
            "service_reply": normalize_chat_candidate_reply(reply or hit_text, item),
            "intent_tags": keyword_list(str(item.get("intent") or ""), fallback=["rag_experience"]),
            "tone_tags": ["rag_reference"],
            "applicability_scope": str(structured.get("applicability_scope") or "global"),
            "product_id": str(structured.get("product_id") or hit.get("product_id") or ""),
            "product_category": str(structured.get("product_category") or structured.get("category") or ""),
            "linked_categories": [str(hit.get("category") or "")] if hit.get("category") else [],
            "additional_details": additional_details,
        }
    )


def chat_payload_from_source_dialogue(item: dict[str, Any], structured: dict[str, Any] | None = None) -> dict[str, str]:
    dialogue = item.get("source_dialogue") if isinstance(item.get("source_dialogue"), dict) else {}
    messages = dialogue.get("messages") if isinstance(dialogue.get("messages"), list) else []
    normalized = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or "").strip(),
        }
        for message in messages
        if isinstance(message, dict) and str(message.get("content") or "").strip()
    ]
    if not normalized:
        return {}
    structured = structured or {}
    customer_hint = str(structured.get("customer_message") or structured.get("question") or "").strip()
    if customer_hint:
        customer_index = best_dialogue_match_index(normalized, customer_hint, role="customer")
        if customer_index >= 0:
            return compact_dict(
                {
                    "customer_message": normalized[customer_index]["content"],
                    "service_reply": nearest_dialogue_content(normalized, customer_index, role="ai", direction=1)
                    or nearest_dialogue_content(normalized, customer_index, role="ai", direction=-1),
                }
            )
    reply_hint = extract_service_reply_from_transcript(str(structured.get("service_reply") or structured.get("answer") or ""))
    if reply_hint:
        ai_index = best_dialogue_match_index(normalized, reply_hint, role="ai")
        if ai_index >= 0:
            return compact_dict(
                {
                    "customer_message": nearest_dialogue_content(normalized, ai_index, role="customer", direction=-1)
                    or nearest_dialogue_content(normalized, ai_index, role="customer", direction=1),
                    "service_reply": normalized[ai_index]["content"],
                }
            )
    customers = [str(value) for value in dialogue.get("customer_messages", []) or [] if str(value)]
    replies = [str(value) for value in dialogue.get("ai_messages", []) or [] if str(value)]
    return compact_dict(
        {
            "customer_message": customers[0] if customers else "",
            "service_reply": replies[0] if replies else "",
        }
    )


def best_dialogue_match_index(messages: list[dict[str, str]], text: str, *, role: str) -> int:
    target = normalized_fingerprint(text)
    if not target:
        return -1
    best_index = -1
    best_score = 0.0
    for index, message in enumerate(messages):
        if message.get("role") != role:
            continue
        candidate = normalized_fingerprint(message.get("content") or "")
        if not candidate:
            continue
        if candidate == target:
            score = 1.0
        elif candidate in target or target in candidate:
            score = min(len(candidate), len(target)) / max(len(candidate), len(target))
        else:
            score = 0.0
        if score > best_score:
            best_index = index
            best_score = score
    return best_index if best_score >= 0.42 else -1


def nearest_dialogue_content(messages: list[dict[str, str]], index: int, *, role: str, direction: int) -> str:
    step = -1 if direction < 0 else 1
    cursor = index + step
    while 0 <= cursor < len(messages) and abs(cursor - index) <= 6:
        if messages[cursor].get("role") == role:
            return str(messages[cursor].get("content") or "").strip()
        cursor += step
    return ""


def structured_payload_from_experience(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("reply_text", "evidence_excerpt", "summary"):
        payload = parse_structured_payload(item.get(key))
        if payload and (is_product_payload(payload) or is_policy_payload(payload) or is_chat_payload(payload)):
            return payload
        payload = parse_json_like_payload(str(item.get(key) or ""))
        if payload and (is_product_payload(payload) or is_policy_payload(payload) or is_chat_payload(payload)):
            return payload
        payload = parse_labeled_payload(str(item.get(key) or ""))
        if payload and (is_product_payload(payload) or is_policy_payload(payload) or is_chat_payload(payload)):
            return payload
    return {}


def experience_is_pipeline_trace(item: dict[str, Any]) -> bool:
    text = experience_text(item)
    trace_markers = (
        "AI experience pool item ->",
        "Intake -> AI experience pool item",
        "RAG experience ->",
        "Intake -> RAG experience",
        "candidates=",
    )
    return any(marker in text for marker in trace_markers)


def pipeline_trace_has_promotable_business_content(item: dict[str, Any]) -> bool:
    """Allow wrapped intake/AI experience pool traces when they contain real business material.

    Raw-message learning stores evidence with technical prefixes such as
    "Intake -> AI experience pool item". Those prefixes alone should not block
    promotion when the inner content has been interpreted as useful customer
    service knowledge.  The final candidate still goes through source-authority
    and human review checks.
    """
    structured = structured_payload_from_experience(item)
    if structured and (is_product_payload(structured) or is_policy_payload(structured) or is_chat_payload(structured)):
        return True
    interpretation = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
    interpretation_text = " ".join(
        str(interpretation.get(key) or "")
        for key in ("meaning", "business_type", "action_reason", "recommended_action")
    )
    if str(interpretation.get("recommended_action") or "") == "promote_to_pending" and has_business_promotion_signal(interpretation_text):
        return True
    text = experience_text(item)
    if int(coerce_int(item.get("candidate_count"), 0)) > 0 and has_business_promotion_signal(text):
        return True
    return False


def parse_structured_payload(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return next((item for item in parsed if isinstance(item, dict)), {})
        except json.JSONDecodeError:
            pass
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_json_like_payload(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text or not re.search(r'"[^"]+"\s*:', text):
        return {}
    payload: dict[str, Any] = {}
    for key in (
        "customer_message",
        "question",
        "service_reply",
        "answer",
        "title",
        "applicability_scope",
        "product_id",
        "product_category",
        "name",
        "sku",
        "category",
        "price",
        "inventory",
    ):
        value = json_like_string_field(text, key)
        if value != "":
            payload[key] = value
    return payload


def json_like_string_field(text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(null|true|false|"(?:\\.|[^"\\])*"|[-+]?\d+(?:\.\d+)?)', text, re.I)
    if not match:
        return ""
    raw = match.group(1)
    if raw in {"", "null"}:
        return ""
    if raw in {"true", "false"}:
        return raw
    if raw.startswith('"'):
        try:
            return str(json.loads(raw)).strip()
        except json.JSONDecodeError:
            return raw.strip('"').strip()
    return raw


def parse_labeled_payload(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        return {}
    labels = {
        "title": "title",
        "answer": "answer",
        "question": "question",
        "customer_message": "customer_message",
        "service_reply": "service_reply",
        "policy_type": "policy_type",
        "product_id": "product_id",
        "name": "name",
        "sku": "sku",
        "price": "price",
        "inventory": "inventory",
        "category": "category",
    }
    pattern = re.compile(
        r"(?:^|[；;\n])\s*(" + "|".join(re.escape(key) for key in labels) + r")\s*[:：]\s*",
        re.I,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return {}
    payload: dict[str, Any] = {}
    for index, match in enumerate(matches):
        key = labels.get(match.group(1).lower())
        if not key:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip(" ；;\n\r\t")
        if value:
            payload[key] = value
    return payload


def normalize_chat_candidate_question(question: Any, item: dict[str, Any]) -> str:
    text = str(question or "").strip()
    generic_titles = {
        "待分类规则",
        "待分类",
        "unknown",
        "AI experience pool item",
        "Intake -> AI experience pool item",
        "RAG experience",
        "Intake -> RAG experience",
    }
    if text and text not in generic_titles and not text.startswith(("Intake -> AI experience pool item", "Intake -> RAG experience")):
        return truncate(text, 180)
    interpretation = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
    meaning = str(interpretation.get("meaning") or "").strip()
    if meaning:
        return truncate(meaning, 180)
    payload = parse_structured_payload(item.get("reply_text") or item.get("evidence_excerpt") or "")
    candidate = str(payload.get("customer_message") or payload.get("question") or "").strip() if payload else ""
    if candidate:
        return truncate(candidate, 180)
    return truncate(str(item.get("question") or item.get("summary") or "客户咨询场景"), 180)


def normalize_chat_candidate_reply(reply: Any, item: dict[str, Any]) -> str:
    text = extract_service_reply_from_transcript(str(reply or ""))
    if text:
        return truncate(text, 1200)
    return ""


def extract_service_reply_from_transcript(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    payload = parse_structured_payload(value)
    if payload:
        extracted = str(payload.get("service_reply") or payload.get("answer") or "").strip()
        if extracted:
            return extract_service_reply_from_transcript(extracted)
        return ""
    value = re.split(r"[；;]\s*keywords\s*:", value, maxsplit=1, flags=re.I)[0].strip()
    if re.search(r"\b(customer_message|service_reply|applicability_scope|additional_details|product_category)\b", value) and re.search(r'"[^"]+"\s*:', value):
        return ""
    marker_match = re.search(r"\[[^\]]*(?:AI|客服)\]", value, re.I)
    if marker_match:
        return value[marker_match.end() :].strip()
    for pattern in (r"(?:^|\])\s*self\s*[:：]\s*(.+)$", r"(?:客服|AI)\s*[:：]\s*(.+)$"):
        match = re.search(pattern, value, re.S)
        if match:
            return match.group(1).strip()
    return value


def has_business_promotion_signal(text: Any) -> bool:
    return text_matches_visible_pattern(text, "promotion_signal_patterns")


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_product_payload(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    product_keys = {"name", "sku", "price", "inventory", "shipping_policy", "warranty_policy", "price_tiers", "specs"}
    if any(key in payload for key in product_keys) and (payload.get("name") or payload.get("sku")):
        return True
    category = str(payload.get("category") or "")
    return bool(category and text_has_visible_term(category, "product_category_terms"))


def is_policy_payload(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if payload.get("policy_type") or payload.get("requires_handoff") is not None or payload.get("handoff_reason"):
        return True
    if payload.get("title") and isinstance(payload.get("keywords"), list):
        return True
    if payload.get("title") and payload.get("answer"):
        combined = f"{payload.get('title')} {payload.get('answer')}"
        return has_business_promotion_signal(combined) and text_has_visible_term(combined, "policy_payload_terms")
    return False


def is_chat_payload(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    return bool(payload.get("customer_message") or payload.get("service_reply") or payload.get("question") or (payload.get("title") and payload.get("answer")))


def candidate_title_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("name") or payload.get("title") or payload.get("customer_message") or payload.get("sku") or "").strip()


def candidate_business_summary(category_id: str, data: dict[str, Any], item: dict[str, Any]) -> str:
    title = str(data.get("name") or data.get("title") or data.get("customer_message") or item.get("summary") or "AI经验").strip()
    label = {"products": "商品资料", "policies": "政策规则", "chats": "聊天话术"}.get(category_id, category_id)
    return truncate(f"从AI经验整理：{title}（{label}）", 120)


def candidate_business_evidence(item: dict[str, Any]) -> str:
    payload = structured_payload_from_experience(item)
    if is_product_payload(payload):
        parts = [
            f"商品：{payload.get('name')}" if payload.get("name") else "",
            f"型号：{payload.get('sku')}" if payload.get("sku") else "",
            f"类目：{payload.get('category')}" if payload.get("category") else "",
            f"价格：{payload.get('price')}" if payload.get("price") not in (None, "") else "",
            f"库存：{payload.get('inventory')}" if payload.get("inventory") not in (None, "") else "",
            f"看车/交付：{payload.get('shipping_policy')}" if payload.get("shipping_policy") else "",
            f"售后/风险：{payload.get('warranty_policy')}" if payload.get("warranty_policy") else "",
        ]
        return "；".join(part for part in parts if part) or truncate(experience_text(item), 1200)
    if is_policy_payload(payload):
        keywords = payload.get("keywords") if isinstance(payload.get("keywords"), list) else []
        parts = [
            f"规则：{payload.get('title')}" if payload.get("title") else "",
            f"类型：{payload.get('policy_type')}" if payload.get("policy_type") else "",
            f"关键词：{'、'.join(str(item) for item in keywords)}" if keywords else "",
            f"处理方式：{payload.get('handoff_reason') or payload.get('answer')}" if payload.get("handoff_reason") or payload.get("answer") else "",
        ]
        return "；".join(part for part in parts if part) or truncate(experience_text(item), 1200)
    return truncate(experience_text(item), 1200)


def rag_experience_llm_assist(item: dict[str, Any]) -> dict[str, Any]:
    interpretation = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
    provider = str(interpretation.get("provider") or "")
    status = "model_generated" if provider and provider != "local_fallback" else "rule_fallback_after_llm"
    return {
        "policy_version": LLM_ASSIST_POLICY_VERSION,
        "stage": "rag_experience_to_review_candidate",
        "attempted": True,
        "provider": provider,
        "model": str(interpretation.get("model") or ""),
        "status": status,
        "reason": str(interpretation.get("status") or interpretation.get("fallback_reason") or "rag_experience_interpretation_required_before_promotion"),
        "recommended_action": str(interpretation.get("recommended_action") or ""),
        "formal_knowledge_comparison": interpretation.get("formal_knowledge_comparison") if isinstance(interpretation.get("formal_knowledge_comparison"), dict) else {},
        "fallback_allowed": True,
        "human_approval_required": True,
    }


def formal_title(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return str(data.get("name") or data.get("title") or data.get("customer_message") or data.get("external_id") or item.get("id") or "")


def formal_text(item: dict[str, Any]) -> str:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return json.dumps({"id": item.get("id"), "data": data, "runtime": item.get("runtime", {})}, ensure_ascii=False, sort_keys=True)


def experience_text(item: dict[str, Any]) -> str:
    hit = item.get("rag_hit", {}) or {}
    parts = [
        str(item.get("summary") or ""),
        str(item.get("question") or ""),
        str(item.get("reply_text") or ""),
        str(hit.get("text") or ""),
        str(hit.get("category") or ""),
        str(hit.get("product_id") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def attach_source_dialogue(
    item: dict[str, Any],
    *,
    raw_store: RawMessageStore,
    conversation_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    dialogue = source_dialogue_for_experience(
        item,
        raw_store=raw_store,
        conversation_cache=conversation_cache,
    )
    if not dialogue.get("messages"):
        return item
    return {**item, "source_dialogue": dialogue}


def source_dialogue_for_experience(
    item: dict[str, Any],
    *,
    raw_store: RawMessageStore,
    conversation_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    raw_ids = source_raw_message_ids(item)
    wechat_message_ids = source_wechat_message_ids(item)
    conversation_id = source_conversation_id(item)
    if not conversation_id:
        conversation_id = source_conversation_id_from_target(item, raw_store)
    if not raw_ids and not wechat_message_ids:
        return {}
    messages = messages_for_source_dialogue(
        raw_store,
        conversation_id=conversation_id,
        conversation_cache=conversation_cache,
    )
    if not messages:
        return {}
    normalized = [normalize_source_dialogue_message(message) for message in messages]
    normalized = [message for message in normalized if message.get("content")]
    source_messages = [
        message
        for message in normalized
        if str(message.get("raw_message_id") or "") in raw_ids
        or str(message.get("message_id") or "") in wechat_message_ids
    ]
    traced = source_dialogue_from_reply_trace(
        raw_store=raw_store,
        conversation_id=conversation_id,
        source_messages=source_messages,
        all_messages=normalized,
    )
    if traced.get("messages"):
        return traced
    selected = select_source_dialogue_messages(normalized, raw_ids=raw_ids, message_ids=wechat_message_ids)
    if not selected and (raw_ids or wechat_message_ids):
        selected = [
            message
            for message in normalized
            if str(message.get("raw_message_id") or "") in raw_ids
            or str(message.get("message_id") or "") in wechat_message_ids
        ]
    if not selected:
        return {}
    customer_messages = [
        str(message.get("content") or "")
        for message in selected
        if message.get("role") == "customer"
    ]
    ai_messages = [
        str(message.get("content") or "")
        for message in selected
        if message.get("role") == "ai"
    ]
    return {
        "conversation_id": conversation_id,
        "raw_message_ids": list(raw_ids),
        "message_ids": list(wechat_message_ids),
        "customer_messages": dedupe_preserve_order(customer_messages)[:4],
        "ai_messages": dedupe_preserve_order(ai_messages)[:4],
        "messages": selected[:10],
        "resolution": "raw_message_context_window",
    }


def source_raw_message_ids(item: dict[str, Any]) -> set[str]:
    ids: list[Any] = []
    original = item.get("original_source") if isinstance(item.get("original_source"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    review = item.get("review") if isinstance(item.get("review"), dict) else {}
    for container in (original, source, review):
        for key in ("raw_message_ids", "source_raw_message_ids", "message_ids"):
            value = container.get(key)
            if isinstance(value, list):
                ids.extend(value)
    value = item.get("message_ids")
    if isinstance(value, list):
        ids.extend(value)
    return {str(value) for value in ids if str(value).startswith("raw_msg_")}


def source_wechat_message_ids(item: dict[str, Any]) -> set[str]:
    ids: list[Any] = []
    original = item.get("original_source") if isinstance(item.get("original_source"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    review = item.get("review") if isinstance(item.get("review"), dict) else {}
    for container in (original, source, review):
        for key in ("wechat_message_ids", "reply_message_ids", "source_message_ids", "message_ids"):
            value = container.get(key)
            if isinstance(value, list):
                ids.extend(value)
    value = item.get("message_ids")
    if isinstance(value, list):
        ids.extend(value)
    return {str(value) for value in ids if str(value) and not str(value).startswith("raw_msg_")}


def source_conversation_id(item: dict[str, Any]) -> str:
    original = item.get("original_source") if isinstance(item.get("original_source"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    for container in (original, source, item):
        value = str(container.get("conversation_id") or "").strip() if isinstance(container, dict) else ""
        if value:
            return value
    return ""


def source_conversation_id_from_target(item: dict[str, Any], raw_store: RawMessageStore) -> str:
    target = str(item.get("target") or "").strip()
    if not target:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        target = str(source.get("target") or source.get("target_name") or "").strip()
    if not target:
        return ""
    for conversation in raw_store.list_conversations(limit=500):
        names = {
            str(conversation.get("target_name") or "").strip(),
            str(conversation.get("display_name") or "").strip(),
            str(conversation.get("group_name") or "").strip(),
        }
        if target in names:
            return str(conversation.get("conversation_id") or "")
    return ""


def messages_for_source_dialogue(
    raw_store: RawMessageStore,
    *,
    conversation_id: str,
    conversation_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    if conversation_cache is not None and conversation_id in conversation_cache:
        return conversation_cache[conversation_id]
    messages = raw_store.list_messages(conversation_id=conversation_id, limit=500)
    messages.sort(key=raw_message_sort_key)
    if conversation_cache is not None:
        conversation_cache[conversation_id] = messages
    return messages


def raw_message_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("observed_at") or item.get("message_time") or ""),
        str(item.get("message_time") or ""),
        str(item.get("updated_at") or ""),
        str(item.get("raw_message_id") or ""),
    )


def normalize_source_dialogue_message(message: dict[str, Any]) -> dict[str, Any]:
    content = str(message.get("content") or "").strip()
    sender = str(message.get("sender") or "").strip()
    sender_role = str(message.get("sender_role") or "").strip().lower()
    role = "system"
    if sender_role in {"self", "bot", "assistant", "ai"} or re.search(r"^\s*\[[^\]]*(?:AI|客服)\]\s*", content, re.I):
        role = "ai"
        content = re.sub(r"^\s*\[[^\]]*(?:AI|客服)\]\s*", "", content, flags=re.I).strip()
    elif sender_role == "system" or sender.lower() == "system":
        role = "system"
    elif content:
        role = "customer"
    return {
        "raw_message_id": str(message.get("raw_message_id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "conversation_id": str(message.get("conversation_id") or ""),
        "target_name": str(message.get("target_name") or ""),
        "timestamp": str(message.get("message_time") or message.get("observed_at") or ""),
        "sender": sender,
        "role": role,
        "content": truncate(content, 800),
    }


def source_dialogue_from_reply_trace(
    *,
    raw_store: RawMessageStore,
    conversation_id: str,
    source_messages: list[dict[str, Any]],
    all_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    if not source_messages:
        return {}
    traces = load_reply_traces_for_conversation(
        raw_store,
        conversation_id=conversation_id,
        all_messages=all_messages,
    )
    best_trace: dict[str, Any] | None = None
    best_score = 0.0
    for trace in traces:
        score = score_reply_trace(trace, source_messages)
        if score > best_score:
            best_trace = trace
            best_score = score
    if not best_trace or best_score < 0.72:
        return {}
    return build_dialogue_from_reply_trace(
        best_trace,
        source_messages=source_messages,
        all_messages=all_messages,
        confidence=best_score,
        conversation_id=conversation_id,
    )


def load_reply_traces_for_conversation(
    raw_store: RawMessageStore,
    *,
    conversation_id: str,
    all_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_names = conversation_target_names(raw_store, conversation_id=conversation_id, all_messages=all_messages)
    if not target_names:
        return []
    state_root = tenant_runtime_state_root(raw_store.tenant_id)
    if not state_root.exists():
        return []
    traces: list[dict[str, Any]] = []
    for path in state_root.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        targets = state.get("targets") if isinstance(state.get("targets"), dict) else {}
        for target_name, target_state in targets.items():
            clean_target_name = str(target_name or "").strip()
            if clean_target_name not in target_names or not isinstance(target_state, dict):
                continue
            traces.extend(state_reply_traces_for_target(target_state, clean_target_name))
    traces.sort(key=lambda item: str(item.get("processed_at") or item.get("created_at") or ""))
    return traces


def conversation_target_names(
    raw_store: RawMessageStore,
    *,
    conversation_id: str,
    all_messages: list[dict[str, Any]],
) -> set[str]:
    names = {
        str(message.get("target_name") or "").strip()
        for message in all_messages
        if str(message.get("target_name") or "").strip()
    }
    if conversation_id:
        for conversation in raw_store.list_conversations(limit=500):
            if str(conversation.get("conversation_id") or "") != conversation_id:
                continue
            for key in ("target_name", "display_name", "group_name"):
                value = str(conversation.get(key) or "").strip()
                if value:
                    names.add(value)
    return {name for name in names if name}


def state_reply_traces_for_target(target_state: dict[str, Any], target_name: str) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for raw in target_state.get("sent_replies", []) or []:
        if isinstance(raw, dict):
            traces.append(normalize_reply_trace(target_name, raw, "sent_reply"))
    for raw in target_state.get("handoff_events", []) or []:
        if isinstance(raw, dict):
            traces.append(normalize_reply_trace(target_name, raw, "handoff"))
    return [trace for trace in traces if trace.get("message_ids") or trace.get("reply_text")]


def normalize_reply_trace(target_name: str, raw: dict[str, Any], kind: str) -> dict[str, Any]:
    message_ids = [str(item) for item in raw.get("message_ids", []) if str(item)]
    message_contents = [str(item) for item in raw.get("message_contents", []) if str(item).strip()]
    reply_text = str(raw.get("reply_text") or "").strip()
    operator_alert = raw.get("operator_alert") if isinstance(raw.get("operator_alert"), dict) else {}
    if not reply_text and operator_alert:
        reply_text = str(operator_alert.get("reply_text") or "").strip()
    timestamp = str(raw.get("processed_at") or raw.get("created_at") or "")
    trace_seed = f"{target_name}:{kind}:{message_ids}:{message_contents}:{reply_text}:{timestamp}"
    return {
        "trace_id": str(raw.get("reply_trace_id") or raw.get("trace_id") or "reply_trace_" + stable_digest(trace_seed, 20)),
        "kind": kind,
        "target_name": target_name,
        "message_ids": message_ids,
        "message_contents": message_contents,
        "reply_text": clean_ai_reply_marker(reply_text),
        "processed_at": timestamp,
        "reason": str(raw.get("reason") or ""),
        "status": str(raw.get("status") or ""),
    }


def score_reply_trace(trace: dict[str, Any], source_messages: list[dict[str, Any]]) -> float:
    trace_message_ids = {str(item) for item in trace.get("message_ids", []) if str(item)}
    source_message_ids = {str(item.get("message_id") or "") for item in source_messages if str(item.get("message_id") or "")}
    if trace_message_ids and source_message_ids:
        overlap = len(trace_message_ids & source_message_ids)
        if overlap:
            return min(1.0, 0.88 + overlap / max(1, len(trace_message_ids)) * 0.12)

    trace_reply = str(trace.get("reply_text") or "")
    source_ai = [str(item.get("content") or "") for item in source_messages if item.get("role") == "ai"]
    for text in source_ai:
        similarity = text_similarity(clean_ai_reply_marker(text), trace_reply)
        if similarity >= 0.78 or contains_substantial_text(text, trace_reply):
            return max(0.9, similarity)

    trace_contents = [str(item) for item in trace.get("message_contents", []) if str(item).strip()]
    source_customer = [str(item.get("content") or "") for item in source_messages if item.get("role") == "customer"]
    if trace_contents and source_customer:
        best = 0.0
        for left in source_customer:
            for right in trace_contents:
                best = max(best, text_similarity(left, right))
        if best >= 0.78:
            return min(0.86, best)
    return 0.0


def build_dialogue_from_reply_trace(
    trace: dict[str, Any],
    *,
    source_messages: list[dict[str, Any]],
    all_messages: list[dict[str, Any]],
    confidence: float,
    conversation_id: str,
) -> dict[str, Any]:
    trace_message_ids = {str(item) for item in trace.get("message_ids", []) if str(item)}
    source_raw_ids = [str(item.get("raw_message_id") or "") for item in source_messages if str(item.get("raw_message_id") or "")]
    customer_messages = [str(item) for item in trace.get("message_contents", []) if str(item).strip()]
    if not customer_messages:
        customer_messages = [
            str(message.get("content") or "")
            for message in source_messages
            if message.get("role") == "customer"
        ]
    matched_customer_messages: list[dict[str, Any]] = []
    for message in all_messages:
        if message.get("role") != "customer":
            continue
        message_id = str(message.get("message_id") or "")
        if message_id and message_id in trace_message_ids:
            matched_customer_messages.append(message)
    if not matched_customer_messages:
        matched_customer_messages = [message for message in source_messages if message.get("role") == "customer"]

    reply_text = clean_ai_reply_marker(str(trace.get("reply_text") or ""))
    matched_ai = best_matching_ai_message(reply_text, source_messages, all_messages)
    dialogue_messages = [compact_trace_message(message) for message in matched_customer_messages]
    if reply_text:
        ai_message = compact_trace_message(matched_ai) if matched_ai else {
            "raw_message_id": "",
            "message_id": "",
            "conversation_id": conversation_id,
            "timestamp": str(trace.get("processed_at") or ""),
            "sender": "AI",
            "role": "ai",
            "content": truncate(reply_text, 800),
        }
        ai_message["content"] = truncate(reply_text, 800)
        dialogue_messages.append(ai_message)

    return {
        "conversation_id": conversation_id,
        "raw_message_ids": dedupe_preserve_order(source_raw_ids),
        "customer_messages": dedupe_preserve_order(customer_messages)[:6],
        "ai_messages": dedupe_preserve_order([reply_text])[:4] if reply_text else [],
        "messages": [message for message in dialogue_messages if message.get("content")][:10],
        "resolution": "reply_trace_ledger",
        "causal_confidence": round(float(confidence), 3),
        "reply_trace": {
            "trace_id": trace.get("trace_id"),
            "kind": trace.get("kind"),
            "target_name": trace.get("target_name"),
            "message_ids": list(trace_message_ids),
            "processed_at": trace.get("processed_at"),
            "reason": trace.get("reason"),
        },
    }


def best_matching_ai_message(
    reply_text: str,
    source_messages: list[dict[str, Any]],
    all_messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not reply_text:
        return None
    candidates = [message for message in source_messages if message.get("role") == "ai"]
    if not candidates:
        candidates = [message for message in all_messages if message.get("role") == "ai"]
    best: dict[str, Any] | None = None
    best_score = 0.0
    for message in candidates:
        score = text_similarity(clean_ai_reply_marker(str(message.get("content") or "")), reply_text)
        if contains_substantial_text(str(message.get("content") or ""), reply_text):
            score = max(score, 0.92)
        if score > best_score:
            best = message
            best_score = score
    return best if best_score >= 0.62 else None


def compact_trace_message(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    return {
        "raw_message_id": str(message.get("raw_message_id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "conversation_id": str(message.get("conversation_id") or ""),
        "timestamp": str(message.get("timestamp") or ""),
        "sender": str(message.get("sender") or ""),
        "role": str(message.get("role") or ""),
        "content": truncate(str(message.get("content") or ""), 800),
    }


def clean_ai_reply_marker(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\s*\[[^\]]*AI[^\]]*\]\s*", "", value, flags=re.IGNORECASE).strip()
    return value


def contains_substantial_text(left: str, right: str) -> bool:
    left_fp = normalized_fingerprint(left)
    right_fp = normalized_fingerprint(right)
    if not left_fp or not right_fp:
        return False
    shorter, longer = sorted((left_fp, right_fp), key=len)
    return len(shorter) >= 24 and shorter in longer


def select_source_dialogue_messages(
    messages: list[dict[str, Any]],
    *,
    raw_ids: set[str],
    message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    message_ids = message_ids or set()
    if not raw_ids and not message_ids:
        return [message for message in messages if message.get("role") != "system"][:8]
    index_by_id = {str(message.get("raw_message_id") or ""): index for index, message in enumerate(messages)}
    index_by_message_id = {str(message.get("message_id") or ""): index for index, message in enumerate(messages)}
    selected_indices: set[int] = set()
    for source_id in {*raw_ids, *message_ids}:
        index = index_by_id.get(source_id)
        if index is None:
            index = index_by_message_id.get(source_id)
        if index is None:
            continue
        selected_indices.add(index)
        role = messages[index].get("role")
        if role == "ai":
            for previous in range(index - 1, max(-1, index - 7), -1):
                previous_role = messages[previous].get("role")
                if previous_role == "ai":
                    break
                if previous_role == "customer":
                    selected_indices.add(previous)
        elif role == "customer":
            for following in range(index + 1, min(len(messages), index + 7)):
                following_role = messages[following].get("role")
                if following_role == "customer":
                    break
                if following_role == "ai":
                    selected_indices.add(following)
                    break
        else:
            for neighbor in (index - 1, index + 1):
                if 0 <= neighbor < len(messages):
                    selected_indices.add(neighbor)
    selected = [
        messages[index]
        for index in sorted(selected_indices)
        if messages[index].get("role") != "system" and messages[index].get("content")
    ]
    return selected[:10]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def text_similarity(left: str, right: str) -> float:
    left_fp = normalized_fingerprint(left)
    right_fp = normalized_fingerprint(right)
    if not left_fp or not right_fp:
        return 0.0
    seq = SequenceMatcher(None, left_fp[:1600], right_fp[:1600]).ratio()
    left_terms = tokenize_for_relation(left)
    right_terms = tokenize_for_relation(right)
    overlap = len(left_terms & right_terms) / max(1, len(left_terms | right_terms))
    coverage = len(left_terms & right_terms) / max(1, len(left_terms))
    return round(max(seq * 0.55 + overlap * 0.25 + coverage * 0.2, overlap), 4)


def tokenize_for_relation(text: str) -> set[str]:
    normalized = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_.-]{2,}", normalized, flags=re.IGNORECASE))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        tokens.add(run)
        for size in (2, 3, 4):
            if len(run) >= size:
                tokens.update(run[index : index + size] for index in range(0, len(run) - size + 1))
    return {token for token in tokens if token.strip()}


def keyword_list(text: str, *, fallback: list[str] | None = None) -> list[str]:
    terms = sorted(tokenize_for_relation(text), key=lambda item: (-len(item), item))
    values = [term for term in terms if len(term) >= 2][:8]
    return values or list(fallback or [])


def normalized_fingerprint(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[\"'`;:，。；：、/\\\[\]{}()（）<>《》|_-]+", "", text)
    return text


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in ("", None, [], {})}


def safe_item_id(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text or not re.match(r"^[a-z0-9]", text):
        text = "rag_item_" + stable_digest(str(value), 12)
    return text[:120]


def stable_digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def truncate(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
