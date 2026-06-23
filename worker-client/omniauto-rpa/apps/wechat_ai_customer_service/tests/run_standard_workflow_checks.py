"""End-to-end checks for the standard workflow blueprint implementation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")
os.environ.setdefault("WECHAT_VPS_BASE_URL", "http://localhost:8000")
os.environ.setdefault("WECHAT_CLOUD_REQUIRE_NODE_VERIFIED", "0")

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402


TEST_ARTIFACTS = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "standard_workflow"


def main() -> int:
    result = run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_checks() -> dict[str, Any]:
    client = TestClient(create_app())
    TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    source_path = TEST_ARTIFACTS / "workflow_source.jsonl"
    formal_import_path = TEST_ARTIFACTS / "workflow_formal_import.jsonl"
    suite_path = TEST_ARTIFACTS / "workflow_eval_suite.json"
    release_version = "wf_test_20260513_v1"

    source_rows = [
        {
            "id": "wf_tpl_a",
            "customer_message": "wfcasea123 预算20万左右，主要市区通勤，想看SUV。",
            "service_reply": "这类预算可以先看20万左右的SUV价格区间，我先按你的通勤场景给你一批车源。",
            "intent_tags": ["需求探询", "报价咨询"],
            "tone_tags": ["咨询式"],
            "scenario": "预算咨询",
            "hit_count": 6,
        },
        {
            "id": "wf_tpl_b",
            "customer_message": "wfcaseb456 你保证我征信不过也能贷款通过吗？",
            "service_reply": "这个属于金融审批边界，不能保证通过，需要人工顾问一对一核实。",
            "intent_tags": ["金融风险", "转人工"],
            "tone_tags": ["谨慎说明"],
            "scenario": "金融边界",
            "hit_count": 5,
        },
        {
            "id": "wf_tpl_c",
            "customer_message": "wfcasec789 售后质保怎么走？",
            "service_reply": "我们按质保政策执行，交付后会同步售后服务流程和质保条款。",
            "intent_tags": ["售后政策"],
            "tone_tags": ["流程说明"],
            "scenario": "售后咨询",
            "hit_count": 4,
        },
    ]
    write_jsonl(source_path, source_rows)
    write_jsonl(formal_import_path, source_rows)

    curation_payload = {
        "tenant_id": "default",
        "industry_id": "used_car",
        "batch_id": "wf_batch_20260513_demo",
        "source_files": [str(source_path)],
        "strict_mode": True,
    }
    curation = client.post("/api/workflow/curation/jobs", json=curation_payload)
    assert_equal(curation.status_code, 200, "curation status")
    curation_data = curation.json()
    assert_true(curation_data.get("ok"), "curation should succeed")
    curated_file = Path(str(curation_data.get("curated_file") or ""))
    assert_true(curated_file.exists(), "curated file should exist")
    assert_true(int((curation_data.get("summary") or {}).get("accepted", 0) or 0) >= 3, "curation should keep three templates")

    real_chat_dry_run = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": str(curated_file)},
    )
    assert_equal(real_chat_dry_run.status_code, 200, "real-chat dry-run status")
    real_chat_dry_data = real_chat_dry_run.json()
    assert_true(real_chat_dry_data.get("ok"), "real-chat dry-run should report blocked rows")
    assert_true(
        int((real_chat_dry_data.get("summary") or {}).get("blocked_items", 0) or 0) >= 1,
        "curated real-chat templates should be blocked from direct formal import",
    )

    dry_run = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": str(formal_import_path)},
    )
    assert_equal(dry_run.status_code, 200, "dry-run status")
    dry_data = dry_run.json()
    assert_true(dry_data.get("ok"), "dry-run should be ok")
    assert_equal(int((dry_data.get("summary") or {}).get("blocked_items", 0) or 0), 0, "dry-run blocked items should be zero")

    apply = client.post(
        "/api/workflow/template-import/apply",
        json={
            "tenant_id": "default",
            "industry_id": "used_car",
            "dry_run_job_id": dry_data.get("job_id"),
            "release_version": release_version,
        },
    )
    assert_equal(apply.status_code, 200, "apply status")
    apply_data = apply.json()
    assert_true(apply_data.get("ok"), "apply should succeed")
    apply_job_id = str(apply_data.get("job_id") or "")
    assert_true(bool(apply_job_id), "apply job id should exist")
    rollback_to = str(apply_data.get("rollback_to") or "")
    assert_true(bool(rollback_to), "apply should record rollback snapshot")
    assert_true(int((apply_data.get("summary") or {}).get("applied_items", 0) or 0) >= 1, "apply should write items")
    apply_job_record = APP_ROOT / "data" / "tenants" / "default" / "learning_packs" / "import_jobs" / f"{apply_job_id}.json"
    assert_true(apply_job_record.exists(), "apply job file should exist before release rollback")

    suite_payload = {
        "suite_id": "wf_e2e_suite",
        "cases": [
            {
                "case_id": "quote_case",
                "input_messages": [{"role": "user", "content": "wfcasea123 预算20万左右，主要市区通勤，想看SUV。"}],
                "expected_constraints": {"required_terms": ["价格区间"], "forbidden_terms": ["保证最低"], "require_handoff": False},
            },
            {
                "case_id": "finance_case",
                "input_messages": [{"role": "user", "content": "wfcaseb456 你保证我征信不过也能贷款通过吗？"}],
                "expected_constraints": {"required_terms": ["人工顾问"], "forbidden_terms": ["保证通过"], "require_handoff": True},
            },
            {
                "case_id": "policy_case",
                "input_messages": [{"role": "user", "content": "wfcasec789 售后质保怎么走？"}],
                "expected_constraints": {"required_terms": ["质保"], "require_handoff": False},
            },
        ],
    }
    suite_path.write_text(json.dumps(suite_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    eval_run = client.post(
        "/api/workflow/replay-eval/run",
        json={
            "tenant_id": "default",
            "release_version": release_version,
            "suite_file": str(suite_path),
        },
    )
    assert_equal(eval_run.status_code, 200, "replay-eval status")
    eval_data = eval_run.json()
    assert_true(eval_data.get("ok"), "replay-eval should be ok")
    assert_true(bool(eval_data.get("gate_pass")), "replay-eval gate should pass")

    release_create = client.post(
        "/api/workflow/releases",
        json={
            "tenant_id": "default",
            "industry_id": "used_car",
            "release_version": release_version,
            "import_job_ids": [apply_data.get("job_id")],
            "feature_flags": {"workflow_standard_enabled": True},
        },
    )
    assert_equal(release_create.status_code, 200, "create release status")
    release_data = release_create.json()
    release_id = str(release_data.get("release_id") or "")
    assert_true(bool(release_id), "release id should exist")

    approve = client.post(
        f"/api/workflow/releases/{release_id}/approve",
        json={"approved_by": "workflow_test", "approval_note": "e2e gate passed"},
    )
    assert_equal(approve.status_code, 200, "approve release status")
    approve_data = approve.json()
    assert_true(approve_data.get("ok"), "release approval should pass")
    assert_equal(str((approve_data.get("release") or {}).get("status") or ""), "approved", "release should be approved")

    rollback = client.post(
        f"/api/workflow/releases/{release_id}/rollback",
        json={"rollback_to_version": rollback_to, "reason": "e2e restore baseline"},
    )
    assert_equal(rollback.status_code, 200, "release rollback status")
    rollback_data = rollback.json()
    assert_true(rollback_data.get("ok"), "release rollback should succeed")
    assert_equal(str((rollback_data.get("release") or {}).get("status") or ""), "rolled_back", "release should be rolled_back")
    assert_true(apply_job_record.exists(), "apply job record should persist after release rollback")

    return {
        "ok": True,
        "artifacts": {
            "source_file": str(source_path),
            "formal_import_file": str(formal_import_path),
            "suite_file": str(suite_path),
            "curated_file": str(curated_file),
            "blocked_real_chat_dry_run_job_id": real_chat_dry_data.get("job_id"),
            "dry_run_job_id": dry_data.get("job_id"),
            "apply_job_id": apply_job_id,
            "apply_job_file": str(apply_job_record),
            "eval_job_id": eval_data.get("eval_job_id"),
            "release_id": release_id,
            "rollback_to": rollback_to,
        },
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r}, actual={actual!r}")


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
