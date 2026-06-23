"""Create pending review candidates from governed AI experience pool items.

This service is deliberately review-only. It may create files under
``review_candidates/pending`` and annotate the source experience, but it never
applies a candidate to formal knowledge.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_review_candidates_root
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, with_quality

from .candidate_store import upsert_candidate_to_db
from .rag_experience_governance import attach_governance


class RagExperienceCandidateNominator:
    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.store = RagExperienceStore(tenant_id=self.tenant_id)

    def nominate(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        limit = max(1, min(int(payload.get("limit") or 50), 200))
        dry_run = bool(payload.get("dry_run"))
        preferred_category = str(payload.get("target_category") or "").strip()
        now_text = datetime.now().isoformat(timespec="seconds")
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        scanned = 0

        for item in self.store.list_for_counts():
            if len(created) >= limit:
                break
            scanned += 1
            governed = attach_governance(with_quality(item))
            eligible, reason = self.eligible(governed)
            if not eligible:
                if reason in {"governance_not_suggested", "already_has_candidate"}:
                    continue
                skipped.append({"experience_id": item.get("experience_id"), "reason": reason})
                continue
            existing_id = existing_candidate_id(str(governed.get("experience_id") or ""), tenant_id=self.tenant_id)
            if existing_id:
                if not dry_run:
                    self.mark_existing_candidate(governed, existing_id, now_text=now_text)
                skipped.append({"experience_id": governed.get("experience_id"), "reason": "duplicate_existing_candidate", "candidate_id": existing_id})
                continue
            try:
                # Local import avoids a module-level cycle with rag_admin_service.
                from .rag_admin_service import build_candidate_from_experience

                candidate = build_candidate_from_experience(governed, preferred_category=preferred_category)
            except ValueError as exc:
                skipped.append({"experience_id": governed.get("experience_id"), "reason": str(exc)})
                continue
            harden_candidate_review(candidate, governed)
            created.append(
                {
                    "experience_id": governed.get("experience_id"),
                    "candidate_id": candidate.get("candidate_id"),
                    "target_category": (candidate.get("proposal") or {}).get("target_category"),
                    "dry_run": dry_run,
                }
            )
            if dry_run:
                continue
            self.write_candidate(candidate)
            self.mark_candidate_created(governed, candidate, now_text=now_text)

        return {
            "ok": True,
            "tenant_id": self.tenant_id,
            "dry_run": dry_run,
            "scanned_count": scanned,
            "created_count": 0 if dry_run else len(created),
            "suggested_count": len(created),
            "skipped_count": len(skipped),
            "created": created,
            "skipped": skipped[:50],
        }

    def eligible(self, item: dict[str, Any]) -> tuple[bool, str]:
        if str(item.get("status") or "active") != "active":
            return False, "not_active"
        nomination = item.get("candidate_nomination") if isinstance(item.get("candidate_nomination"), dict) else {}
        if nomination.get("created_candidate_id"):
            return False, "already_has_candidate"
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else {}
        if str(governance.get("effective_state") or "") != "candidate_suggested":
            return False, "governance_not_suggested"
        if not bool(governance.get("candidate_auto_create_allowed")):
            return False, "candidate_auto_create_disabled"
        if not bool(governance.get("promotion_allowed")):
            return False, "promotion_not_allowed"
        source_authority = governance.get("source_authority") if isinstance(governance.get("source_authority"), dict) else {}
        if not bool(source_authority.get("allowed", True)):
            return False, str(source_authority.get("reason") or "source_authority_blocked")
        return True, ""

    def write_candidate(self, candidate: dict[str, Any]) -> None:
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id:
            raise ValueError("candidate_id is required")
        path = tenant_review_candidates_root(self.tenant_id) / "pending" / f"{candidate_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        upsert_candidate_to_db(candidate, tenant_id=self.tenant_id)

    def mark_candidate_created(self, item: dict[str, Any], candidate: dict[str, Any], *, now_text: str) -> None:
        candidate_id = str(candidate.get("candidate_id") or "")
        governance = dict(item.get("governance") if isinstance(item.get("governance"), dict) else {})
        governance.update(
            {
                "effective_state": "candidate_created",
                "final_action": "candidate_created",
                "display_label": "已生成待确认知识候选",
                "reason": "这条经验已生成待确认知识候选，等待人工审核后才可能进入正式知识库。",
                "retrieval_allowed": False,
                "promotion_allowed": False,
                "candidate_auto_create_allowed": False,
                "requires_manual_review": True,
            }
        )
        patch = {
            "candidate_nomination": {
                "status": "created",
                "suggested_at": now_text,
                "target_category": (candidate.get("proposal") or {}).get("target_category") or "",
                "reason": governance.get("reason") or "",
                "auto_create_allowed": True,
                "created_candidate_id": candidate_id,
                "created_at": now_text,
            },
            "governance": governance,
            "review_state": {
                **(item.get("review_state") if isinstance(item.get("review_state"), dict) else {}),
                "is_new": False,
                "read_at": now_text,
                "read_by": "rag_experience_candidate_nominator",
            },
        }
        self.store.update_metadata(str(item.get("experience_id") or ""), patch, rebuild_index=True)

    def mark_existing_candidate(self, item: dict[str, Any], candidate_id: str, *, now_text: str) -> None:
        patch = {
            "candidate_nomination": {
                "status": "created",
                "suggested_at": now_text,
                "target_category": "",
                "reason": "已存在同一AI经验池生成的待确认知识候选。",
                "auto_create_allowed": False,
                "created_candidate_id": candidate_id,
                "created_at": now_text,
            }
        }
        self.store.update_metadata(str(item.get("experience_id") or ""), patch, rebuild_index=False)


def harden_candidate_review(candidate: dict[str, Any], item: dict[str, Any]) -> None:
    review = candidate.setdefault("review", {})
    review["status"] = "pending"
    review["requires_human_approval"] = True
    review["allowed_auto_apply"] = False
    review["rag_experience_id"] = item.get("experience_id")
    review["candidate_nomination"] = {
        "source": "rag_experience_candidate_nominator",
        "governance_effective_state": (item.get("governance") or {}).get("effective_state"),
    }


def existing_candidate_id(experience_id: str, *, tenant_id: str) -> str:
    if not experience_id:
        return ""
    root = tenant_review_candidates_root(tenant_id)
    for status in ("pending", "approved", "rejected"):
        folder = root / status
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            item = read_json(path)
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            review = item.get("review") if isinstance(item.get("review"), dict) else {}
            if str(source.get("experience_id") or review.get("rag_experience_id") or "") == experience_id:
                return str(item.get("candidate_id") or path.stem)
    return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
