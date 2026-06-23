"""Migrate historical AI experience pool items to the unified governance contract.

The migration is intentionally narrow:
- it writes governance snapshots and migration audit metadata to AI experience pool items;
- it may persist obvious automatic discard decisions already made by governance;
- it never edits formal knowledge, product master data, or source uploads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter, defaultdict
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
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    tenant_context,
    tenant_review_candidates_root,
    tenant_root,
)
from apps.wechat_ai_customer_service.workflows.rag_experience_store import (  # noqa: E402
    RagExperienceStore,
    rebuild_rag_index_safely,
    with_quality,
    write_json_with_retry,
)


MIGRATION_VERSION = "rag_experience_governance_migration_v1"
STYLE_STATES = {"style_only"}
HANDLED_STATES = {
    "retrievable_experience",
    "style_only",
    "auto_discarded",
    "user_discarded",
    "promoted",
    "blocked",
}
REVIEW_REQUIRED_STATES = {"pending_review", "candidate_suggested", "candidate_created", "unknown"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="chejin")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", action="store_true", help="Create a runtime backup before applying changes.")
    parser.add_argument("--report", default="")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--rebuild-index", action="store_true", help="Synchronously rebuild the RAG index after --apply.")
    args = parser.parse_args()

    with tenant_context(args.tenant):
        report = migrate_rag_experience_governance(
            tenant_id=str(args.tenant),
            dry_run=bool(args.dry_run),
            apply=bool(args.apply),
            backup=bool(args.backup),
            sample_limit=max(1, int(args.sample_limit or 20)),
            rebuild_index=bool(args.rebuild_index),
        )
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("ok") else 1


def migrate_rag_experience_governance(
    *,
    tenant_id: str,
    dry_run: bool,
    apply: bool,
    backup: bool,
    sample_limit: int,
    rebuild_index: bool,
) -> dict[str, Any]:
    store = RagExperienceStore(tenant_id=tenant_id)
    records = store.list_for_counts()
    now_text = datetime.now().isoformat(timespec="seconds")
    migration_id = f"rag_governance_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_dir = ""
    if apply and backup:
        backup_dir = create_backup(tenant_id=tenant_id, migration_id=migration_id, store=store, created_at=now_text)

    migrated: list[dict[str, Any]] = []
    changed_records: list[dict[str, Any]] = []
    transitions: Counter[str] = Counter()
    before_states: Counter[str] = Counter()
    after_states: Counter[str] = Counter()
    status_before: Counter[str] = Counter()
    status_after: Counter[str] = Counter()
    retrieval_allowed_count = 0
    candidate_allowed_count = 0
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in records:
        original = dict(item)
        enriched = with_quality(original)
        before_governance = resolve_rag_experience_governance(enriched)
        before_state = str(before_governance.get("effective_state") or "unknown")
        before_states[before_state] += 1
        status_before[str(item.get("status") or "active")] += 1

        updated = build_migrated_record(
            item,
            enriched=enriched,
            before_governance=before_governance,
            migration_id=migration_id,
            migrated_at=now_text,
            tenant_id=tenant_id,
        )
        after_governance = resolve_rag_experience_governance(updated)
        updated["governance"] = after_governance
        updated["governance_migration"]["final_effective_state"] = str(after_governance.get("effective_state") or "unknown")
        after_state = str(after_governance.get("effective_state") or "unknown")
        after_states[after_state] += 1
        status_after[str(updated.get("status") or "active")] += 1
        if after_governance.get("retrieval_allowed"):
            retrieval_allowed_count += 1
        if after_governance.get("candidate_auto_create_allowed"):
            candidate_allowed_count += 1
        transitions[f"{before_state}->{after_state}"] += 1
        migrated.append(updated)
        if canonical_json(original) != canonical_json(updated):
            changed_records.append(
                {
                    "experience_id": str(updated.get("experience_id") or ""),
                    "before_state": before_state,
                    "after_state": after_state,
                    "before_status": str(item.get("status") or "active"),
                    "after_status": str(updated.get("status") or "active"),
                    "summary": str(updated.get("summary") or updated.get("question") or "")[:180],
                }
            )
        if len(samples[after_state]) < sample_limit:
            samples[after_state].append(
                {
                    "experience_id": str(updated.get("experience_id") or ""),
                    "status": str(updated.get("status") or "active"),
                    "summary": str(updated.get("summary") or updated.get("question") or "")[:180],
                    "display_label": str(after_governance.get("display_label") or ""),
                    "reason": str(after_governance.get("reason") or "")[:220],
                }
            )

    report: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if apply else "dry_run",
        "tenant_id": tenant_id,
        "migration_id": migration_id,
        "migration_version": MIGRATION_VERSION,
        "total": len(records),
        "changed_count": len(changed_records),
        "before_states": dict(before_states),
        "after_states": dict(after_states),
        "status_before": dict(status_before),
        "status_after": dict(status_after),
        "transitions": dict(transitions),
        "retrieval_allowed_count": retrieval_allowed_count,
        "candidate_auto_create_allowed_count": candidate_allowed_count,
        "samples": dict(samples),
        "changed_samples": changed_records[:sample_limit],
        "backup_dir": backup_dir,
        "rebuild_index": None,
    }
    report["after_accounted_total"] = sum(after_states.values())
    report["consistent"] = report["after_accounted_total"] == len(records)

    if apply:
        write_migrated_records(store=store, records=migrated)
        if rebuild_index:
            rebuild_rag_index_safely(tenant_id, trigger="rag_governance_migration", force_sync=True)
            report["rebuild_index"] = {"ok": True, "mode": "sync", "trigger": "rag_governance_migration"}
    return report


def build_migrated_record(
    item: dict[str, Any],
    *,
    enriched: dict[str, Any],
    before_governance: dict[str, Any],
    migration_id: str,
    migrated_at: str,
    tenant_id: str,
) -> dict[str, Any]:
    updated = dict(item)
    updated["quality"] = quality_with_governance(enriched.get("quality", {}), before_governance)
    updated["governance"] = before_governance
    before_state = str(before_governance.get("effective_state") or "unknown")
    updated["governance_migration"] = {
        "migration_id": migration_id,
        "migration_version": MIGRATION_VERSION,
        "tenant_id": tenant_id,
        "migrated_at": migrated_at,
        "original_effective_state": before_state,
        "final_effective_state": before_state,
    }
    if before_state == "auto_discarded" and str(updated.get("status") or "active") == "active":
        review = dict(updated.get("experience_review") if isinstance(updated.get("experience_review"), dict) else {})
        review.update(
            {
                "status": "auto_triaged",
                "auto_triage_action": "discard",
                "auto_triage_reason": str(before_governance.get("reason") or "系统治理迁移自动降噪。"),
                "auto_triaged_at": migrated_at,
                "auto_triaged_by": MIGRATION_VERSION,
            }
        )
        updated["experience_review"] = review
        updated["status"] = "discarded"
        updated["auto_discarded_at"] = migrated_at
    if before_state in HANDLED_STATES:
        updated["review_state"] = mark_review_state(updated, is_new=False, at=migrated_at)
    elif before_state in REVIEW_REQUIRED_STATES:
        updated["review_state"] = mark_review_state(updated, is_new=True, at=migrated_at)
    updated["updated_at"] = migrated_at
    return updated


def mark_review_state(item: dict[str, Any], *, is_new: bool, at: str) -> dict[str, Any]:
    state = dict(item.get("review_state") if isinstance(item.get("review_state"), dict) else {})
    state["is_new"] = bool(is_new)
    if is_new:
        state.setdefault("marked_at", at)
        state.setdefault("new_reason", "governance_requires_review")
        state["read_at"] = ""
        state["read_by"] = ""
    else:
        state["read_at"] = str(state.get("read_at") or at)
        state["read_by"] = str(state.get("read_by") or MIGRATION_VERSION)
        state["new_reason"] = str(state.get("new_reason") or "governance_migrated_processed")
    return state


def quality_with_governance(quality: dict[str, Any], governance: dict[str, Any]) -> dict[str, Any]:
    result = dict(quality if isinstance(quality, dict) else {})
    previous_allowed = bool(result.get("retrieval_allowed"))
    final_allowed = bool(governance.get("retrieval_allowed"))
    result["retrieval_allowed"] = final_allowed
    signals = dict(result.get("signals") if isinstance(result.get("signals"), dict) else {})
    signals["governance_allows_retrieval"] = final_allowed
    signals["quality_allows_retrieval_before_governance"] = previous_allowed
    result["signals"] = signals
    if previous_allowed and not final_allowed:
        reasons = list(result.get("reasons") if isinstance(result.get("reasons"), list) else [])
        final_reason = str(governance.get("display_label") or governance.get("reason") or "最终治理裁决不允许作为回答依据。")
        note = f"最终治理裁决：{final_reason}"
        if note not in reasons:
            reasons.append(note)
        result["reasons"] = reasons
    return result


def write_migrated_records(*, store: RagExperienceStore, records: list[dict[str, Any]]) -> None:
    write_json_with_retry(store.path, records)


def create_backup(*, tenant_id: str, migration_id: str, store: RagExperienceStore, created_at: str) -> str:
    root = PROJECT_ROOT / "runtime" / "backups" / "rag_governance" / tenant_id / migration_id
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = [
        store.path,
        tenant_root(tenant_id) / "rag_index" / "index.json",
        tenant_root(tenant_id) / "style_memory" / "examples.jsonl",
    ]
    candidates_root = tenant_review_candidates_root(tenant_id)
    copied: list[str] = []
    sha256: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        relative = safe_relative_path(path)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(str(path))
        sha256[str(path)] = file_sha256(path)
    if candidates_root.exists():
        target = root / safe_relative_path(candidates_root)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(candidates_root, target)
        copied.append(str(candidates_root))
    manifest = {
        "migration_id": migration_id,
        "migration_version": MIGRATION_VERSION,
        "tenant_id": tenant_id,
        "created_at": created_at,
        "source_files": copied,
        "sha256": sha256,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(root)


def safe_relative_path(path: Path) -> Path:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return Path(path.name)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


if __name__ == "__main__":
    raise SystemExit(main())
