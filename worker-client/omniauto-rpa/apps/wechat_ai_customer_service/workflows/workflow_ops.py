"""CLI operations for standard workflow lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.workflow_service import WorkflowService  # noqa: E402


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    service = WorkflowService(tenant_id=args.tenant_id)
    if args.command == "curation":
        result = service.create_curation_job(
            tenant_id=args.tenant_id,
            industry_id=args.industry_id,
            batch_id=args.batch_id,
            source_files=args.source_files,
            strict_mode=args.strict_mode,
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "import":
        if args.dry_run:
            result = service.dry_run_import(
                tenant_id=args.tenant_id,
                industry_id=args.industry_id,
                input_file=args.input_file,
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result.get("ok") else 1
        result = service.apply_import(
            tenant_id=args.tenant_id,
            industry_id=args.industry_id,
            dry_run_job_id=args.dry_run_job_id,
            input_file=args.input_file,
            release_version=args.release_version,
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "eval":
        gate = parse_metrics_gate(args.metrics_gate)
        result = service.run_replay_eval(
            tenant_id=args.tenant_id,
            release_version=args.release_version,
            suite_id=args.suite_id,
            suite_file=args.suite_file,
            metrics_gate=gate,
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "release":
        if args.create:
            result = service.create_release(
                tenant_id=args.tenant_id,
                release_version=args.release_version,
                import_job_ids=args.import_job_ids,
                feature_flags=parse_json_dict(args.feature_flags),
                industry_id=args.industry_id,
                metrics_gate=parse_metrics_gate(args.metrics_gate),
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result.get("ok") else 1
        if args.approve:
            result = service.approve_release(
                release_id=args.release_id,
                approved_by=args.approved_by or os.getenv("USERNAME", "admin"),
                approval_note=args.approval_note,
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result.get("ok") else 1
        if args.rollback:
            result = service.rollback_release(
                release_id=args.release_id,
                rollback_to_version=args.rollback_to_version,
                reason=args.reason,
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result.get("ok") else 1
        print(json.dumps({"ok": False, "message": "specify --create / --approve / --rollback"}, ensure_ascii=True, indent=2))
        return 1

    print(json.dumps({"ok": False, "message": f"unsupported command: {args.command}"}, ensure_ascii=True, indent=2))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--industry-id", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    curation = subparsers.add_parser("curation")
    curation.add_argument("--batch-id", required=True)
    curation.add_argument("--source-files", nargs="+", required=True)
    curation.add_argument("--strict-mode", action=argparse.BooleanOptionalAction, default=True)

    import_cmd = subparsers.add_parser("import")
    import_cmd.add_argument("--dry-run", action="store_true")
    import_cmd.add_argument("--input-file", default="")
    import_cmd.add_argument("--dry-run-job-id", default="")
    import_cmd.add_argument("--release-version", default="")

    eval_cmd = subparsers.add_parser("eval")
    eval_cmd.add_argument("--release-version", required=True)
    eval_cmd.add_argument("--suite-id", default="default_usedcar_suite")
    eval_cmd.add_argument("--suite-file", default="")
    eval_cmd.add_argument("--metrics-gate", default="")

    release = subparsers.add_parser("release")
    release.add_argument("--create", action="store_true")
    release.add_argument("--approve", action="store_true")
    release.add_argument("--rollback", action="store_true")
    release.add_argument("--release-id", default="")
    release.add_argument("--release-version", default="")
    release.add_argument("--import-job-ids", nargs="*", default=[])
    release.add_argument("--feature-flags", default="")
    release.add_argument("--metrics-gate", default="")
    release.add_argument("--approved-by", default="")
    release.add_argument("--approval-note", default="")
    release.add_argument("--rollback-to-version", default="")
    release.add_argument("--reason", default="")

    return parser


def parse_json_dict(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_metrics_gate(value: str) -> dict[str, Any]:
    payload = parse_json_dict(value)
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
