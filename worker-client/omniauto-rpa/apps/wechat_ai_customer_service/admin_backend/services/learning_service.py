"""Learning jobs that turn raw uploads into AI experience pool items."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_log import append_audit
from .rag_experience_auto_review import auto_review_rag_experience
from .upload_store import UploadStore
from apps.wechat_ai_customer_service.knowledge_paths import tenant_admin_jobs_root


APP_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
if str(WORKFLOWS_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_ROOT))

from generate_review_candidates import LLM_ASSIST_POLICY_VERSION  # noqa: E402
from rag_layer import RagService, compact_hits  # noqa: E402
from rag_experience_store import RagExperienceStore  # noqa: E402


AUTHORITY_SENSITIVE_RAG_CATEGORIES = {
    "products",
    "erp_exports",
    "policies",
    "product_faq",
    "product_rules",
    "product_explanations",
}


class LearningService:
    def __init__(self) -> None:
        self.uploads = UploadStore()
        self.rag = RagService(tenant_id=self.uploads.tenant_id)
        self.rag_experiences = RagExperienceStore(tenant_id=self.uploads.tenant_id)
        self.jobs_root = tenant_admin_jobs_root()

    def create_job(self, upload_ids: list[str], *, use_llm: bool = True) -> dict[str, Any]:
        job_id = "job_" + uuid.uuid4().hex[:12]
        selected_uploads = [item for item in self.uploads.list_uploads() if item.get("upload_id") in upload_ids] if upload_ids else []
        rag_experience_ids: list[str] = []
        reviewed_experiences: list[dict[str, Any]] = []
        for upload in selected_uploads:
            upload_path = Path(str(upload.get("path") or ""))
            if upload_path.exists():
                rag_ingest = self.rag.ingest_file(
                    upload_path,
                    source_type="upload",
                    category=str(upload.get("kind") or ""),
                    rebuild_index=True,
                )
            else:
                rag_ingest = {"ok": False, "message": "source file is missing", "path": str(upload_path)}
            experience = self.rag_experiences.record_intake(
                source_type="raw_upload",
                source_path=str(upload_path),
                category=str(upload.get("kind") or ""),
                evidence_excerpt=source_text_excerpt(upload_path),
                rag_ingest=rag_ingest,
                candidate_ids=[],
                original_source={
                    "upload_id": upload.get("upload_id"),
                    "file_name": upload.get("file_name") or upload_path.name,
                    "kind": upload.get("kind"),
                },
            )
            rag_experience_ids.append(str(experience.get("experience_id") or ""))
            reviewed_experiences.append(auto_review_rag_experience(experience, store=self.rag_experiences, force=False, use_llm=use_llm))
            self.uploads.mark_learned([str(upload.get("upload_id"))], [])
        candidate_ids: list[str] = []
        job = {
            "job_id": job_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "completed",
            "upload_ids": upload_ids,
            "use_llm": use_llm,
            "llm_assist_policy": {
                "policy_version": LLM_ASSIST_POLICY_VERSION,
                "stage": "upload_to_rag_experience_review",
                "requested": bool(use_llm),
                "rule_fallback_allowed": True,
                "human_approval_required": True,
                "strict_promotion_policy": "uploads_create_rag_experience_only; pending candidates require manual RAG promotion",
            },
            "candidate_ids": candidate_ids,
            "candidate_count": 0,
            "rag_experience_ids": [item for item in rag_experience_ids if item],
            "rag_experience_count": len([item for item in rag_experience_ids if item]),
            "strict_promotion_policy": "rag_experience_manual_promotion_only",
            "skipped_duplicate_count": 0,
            "skipped_duplicates": [],
        }
        self.write_job(job)
        append_audit(
            "learning_started",
            {
                "job_id": job_id,
                "upload_ids": upload_ids,
                "candidate_count": 0,
                "rag_experience_count": job["rag_experience_count"],
                "skipped_duplicate_count": 0,
            },
        )
        return {"ok": True, "job": job, "candidates": [], "rag_experiences": reviewed_experiences}

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_root.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.jobs_root.glob("*.json"), reverse=True)]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        path = self.jobs_root / f"{job_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_job(self, job: dict[str, Any]) -> None:
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        (self.jobs_root / f"{job['job_id']}.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def attach_rag_evidence(candidate: dict[str, Any], rag: RagService, ingest_result: dict[str, Any]) -> None:
    if not ingest_result.get("ok"):
        candidate.setdefault("review", {})["rag_evidence"] = {"enabled": True, "ok": False, "message": ingest_result.get("message")}
        return
    query = candidate_query(candidate)
    search = rag.search(query, limit=4)
    hits = search.get("hits", []) if isinstance(search.get("hits"), list) else []
    source_id = str(ingest_result.get("source_id") or "")
    category_id = candidate_category(candidate)
    if source_id and category_id in AUTHORITY_SENSITIVE_RAG_CATEGORIES:
        hits = [hit for hit in hits if isinstance(hit, dict) and str(hit.get("source_id") or "") == source_id]
    evidence = {
        "enabled": True,
        "ok": bool(search.get("ok")),
        "source_id": source_id,
        "chunk_count": ingest_result.get("chunk_count", 0),
        "query": query,
        "hits": compact_hits(hits, limit=4),
        "rag_can_authorize": False,
        "structured_priority": True,
    }
    candidate.setdefault("review", {})["rag_evidence"] = evidence
    candidate.setdefault("source", {})["rag_hits"] = evidence["hits"]


def ensure_candidate_llm_assist(candidate: dict[str, Any], *, requested: bool, stage: str) -> None:
    review = candidate.setdefault("review", {})
    existing = review.get("llm_assist") if isinstance(review.get("llm_assist"), dict) else {}
    if existing:
        existing.setdefault("policy_version", LLM_ASSIST_POLICY_VERSION)
        existing.setdefault("stage", stage)
        existing.setdefault("human_approval_required", True)
        review["llm_assist"] = existing
        return
    review["llm_assist"] = {
        "policy_version": LLM_ASSIST_POLICY_VERSION,
        "stage": stage,
        "attempted": bool(requested),
        "provider": "",
        "status": "rule_fallback_after_llm" if requested else "rule_only_disabled_by_request",
        "reason": "candidate_builder_returned_without_llm_metadata",
        "fallback_allowed": True,
        "human_approval_required": True,
    }


def candidate_query(candidate: dict[str, Any]) -> str:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    parts = [
        proposal.get("summary"),
        source.get("evidence_excerpt"),
        data.get("name"),
        data.get("title"),
        data.get("answer"),
        data.get("service_reply"),
    ]
    return "\n".join(str(part) for part in parts if part not in (None, "", [], {}))[:1200]


def candidate_category(candidate: dict[str, Any]) -> str:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    return str(patch.get("target_category") or proposal.get("target_category") or candidate.get("category_id") or "").strip()


def candidate_evidence_excerpt(candidates: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for candidate in candidates[:6]:
        source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
        proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
        for value in (source.get("evidence_excerpt"), proposal.get("summary"), candidate_query(candidate)):
            text = str(value or "").strip()
            if text:
                parts.append(text)
                break
    return "\n\n".join(parts)[:2400]


def source_text_excerpt(path: Path, *, limit: int = 2400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def link_candidate_to_rag_experience(
    candidate: dict[str, Any],
    experience: dict[str, Any],
    *,
    original_type: str,
    original_channel: str,
) -> None:
    experience_id = str(experience.get("experience_id") or "")
    source = candidate.setdefault("source", {})
    current_type = str(source.get("type") or original_type or "")
    source["original_type"] = current_type
    source["original_channel"] = original_channel
    source["type"] = "rag_experience"
    source["rag_experience_id"] = experience_id
    source["experience_id"] = experience_id
    review = candidate.setdefault("review", {})
    review.update(
        {
            "rag_experience_id": experience_id,
            "source_chain": [current_type, "rag_experience", "review_candidate"],
            "requires_human_approval": True,
            "allowed_auto_apply": False,
        }
    )
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
    item_source = item.setdefault("source", {}) if isinstance(item, dict) else {}
    if isinstance(item_source, dict):
        item_source.update(
            {
                "rag_experience_id": experience_id,
                "candidate_source_type": "rag_experience",
                "original_type": current_type,
            }
        )
    tags = [str(item) for item in candidate.get("detected_tags", []) or [] if str(item)]
    tags.append("rag_experience")
    candidate["detected_tags"] = sorted(set(tags))
