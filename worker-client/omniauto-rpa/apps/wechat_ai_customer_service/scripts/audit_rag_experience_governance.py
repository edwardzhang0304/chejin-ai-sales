"""Audit AI experience pool item governance inputs without mutating data."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_governance import (  # noqa: E402
    resolve_rag_experience_governance,
)
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, with_quality  # noqa: E402


AUTO_TRIAGE_ACTIONS = {"discard", "already_covered"}
KNOWN_TOP_LEVEL_STATUSES = {"active", "discarded", "promoted"}


def main() -> int:
    args = parse_args()
    report = build_report(args.tenant, sample_limit=args.sample_limit)
    text = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="chejin", help="Tenant id to audit.")
    parser.add_argument("--output", default="", help="Optional JSON report path.")
    parser.add_argument("--sample-limit", type=int, default=20, help="Maximum samples per suspicious bucket.")
    return parser.parse_args()


def build_report(tenant_id: str, *, sample_limit: int = 20) -> dict[str, Any]:
    with tenant_context(tenant_id):
        store = RagExperienceStore()
        items = store.list_for_counts()
    status_counts: Counter[str] = Counter()
    review_status_counts: Counter[str] = Counter()
    ai_action_counts: Counter[str] = Counter()
    quality_band_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    source_type_counts: Counter[str] = Counter()
    governance_state_counts: Counter[str] = Counter()
    conflict_reason_counts: Counter[str] = Counter()
    conflict_source_counts: Counter[str] = Counter()
    auto_kept_ai_discard_count = 0
    retrievable_ai_discard_count = 0
    governance_conflict_count = 0
    unknown_statuses: list[dict[str, Any]] = []
    auto_kept_ai_discard: list[dict[str, Any]] = []
    retrievable_ai_discard: list[dict[str, Any]] = []

    for item in items:
        status = str(item.get("status") or "active")
        review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
        quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
        ai = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
        review_status = str(review.get("status") or "pending")
        ai_action = str(ai.get("recommended_action") or "")
        source = str(item.get("source") or "")
        source_type = str(item.get("source_type") or "")
        governed = resolve_rag_experience_governance(with_quality(item))
        governance_state = str(governed.get("effective_state") or "unknown")
        status_counts[status] += 1
        review_status_counts[review_status] += 1
        ai_action_counts[ai_action or "missing"] += 1
        quality_band_counts[str(quality.get("band") or "unknown")] += 1
        source_counts[source or "missing"] += 1
        source_type_counts[source_type or "missing"] += 1
        governance_state_counts[governance_state] += 1
        if governance_state == "retrievable_experience" and ai_action in AUTO_TRIAGE_ACTIONS:
            governance_conflict_count += 1
        if status not in KNOWN_TOP_LEVEL_STATUSES:
            add_sample(unknown_statuses, item, sample_limit)
        if review_status == "auto_kept" and ai_action in AUTO_TRIAGE_ACTIONS and not bool(item.get("reviewed_by_user")):
            auto_kept_ai_discard_count += 1
            reason_code = ai_reason_code(ai)
            conflict_reason_counts[reason_code] += 1
            conflict_source_counts[f"{source or 'missing'}|{source_type or 'missing'}"] += 1
            add_sample(auto_kept_ai_discard, item, sample_limit, reason_code=reason_code)
        if bool(quality.get("retrieval_allowed")) and ai_action in AUTO_TRIAGE_ACTIONS:
            retrievable_ai_discard_count += 1
            add_sample(retrievable_ai_discard, item, sample_limit, reason_code=ai_reason_code(ai))

    status_total = sum(status_counts.values())
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(items),
        "by_status": dict(sorted(status_counts.items())),
        "by_review_status": dict(sorted(review_status_counts.items())),
        "by_ai_action": dict(sorted(ai_action_counts.items())),
        "by_quality_band": dict(sorted(quality_band_counts.items())),
        "by_source": dict(source_counts.most_common()),
        "by_source_type": dict(source_type_counts.most_common()),
        "by_governance_state": dict(sorted(governance_state_counts.items())),
        "status_accounting": {
            "accounted_total": status_total,
            "consistent": status_total == len(items),
        },
        "governance_accounting": {
            "accounted_total": sum(governance_state_counts.values()),
            "consistent": sum(governance_state_counts.values()) == len(items),
        },
        "conflicts": {
            "legacy_auto_kept_with_ai_discard_count": auto_kept_ai_discard_count,
            "legacy_retrievable_with_ai_discard_count": retrievable_ai_discard_count,
            "governance_conflicts_after_resolution_count": governance_conflict_count,
            "reason_counts": dict(conflict_reason_counts.most_common()),
            "source_counts": dict(conflict_source_counts.most_common()),
            "auto_kept_with_ai_discard_samples": auto_kept_ai_discard,
            "retrievable_with_ai_discard_samples": retrievable_ai_discard,
        },
        "unknown_statuses": {
            "count": len(unknown_statuses),
            "samples": unknown_statuses,
        },
    }


def ai_reason_code(ai: dict[str, Any]) -> str:
    auto_triage = ai.get("auto_triage") if isinstance(ai.get("auto_triage"), dict) else {}
    reason_code = str(auto_triage.get("reason_code") or "").strip()
    if reason_code:
        return reason_code
    return str(ai.get("action_reason") or "unknown_reason")[:160]


def add_sample(samples: list[dict[str, Any]], item: dict[str, Any], limit: int, *, reason_code: str = "") -> None:
    if len(samples) >= max(1, limit):
        return
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    review = item.get("experience_review") if isinstance(item.get("experience_review"), dict) else {}
    ai = item.get("ai_interpretation") if isinstance(item.get("ai_interpretation"), dict) else {}
    samples.append(
        {
            "experience_id": item.get("experience_id"),
            "status": item.get("status") or "active",
            "review_status": review.get("status") or "",
            "source": item.get("source") or "",
            "source_type": item.get("source_type") or "",
            "quality_band": quality.get("band") or "",
            "retrieval_allowed": bool(quality.get("retrieval_allowed")),
            "ai_action": ai.get("recommended_action") or "",
            "reason_code": reason_code or ai_reason_code(ai),
            "summary": str(item.get("summary") or item.get("question") or item.get("reply_text") or "")[:180],
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
