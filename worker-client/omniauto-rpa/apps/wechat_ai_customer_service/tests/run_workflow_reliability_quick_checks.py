"""Quick reliability checks for workflow blueprint, capped to <=5 rounds."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Force local test mode to avoid cloud-gate variance from external env.
os.environ["WECHAT_CLOUD_REQUIRED"] = "0"
os.environ["WECHAT_CLOUD_STRICT_ONLINE"] = "0"
os.environ["WECHAT_VPS_BASE_URL"] = "http://localhost:8000"
os.environ["WECHAT_CLOUD_REQUIRE_NODE_VERIFIED"] = "0"

from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services import knowledge_compiler as knowledge_compiler_module  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services import workflow_service as workflow_service_module  # noqa: E402


TEST_ARTIFACTS = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "workflow_reliability"
SNAPSHOT_ROOT = TEST_ARTIFACTS / "quick_versions"
ISOLATED_KNOWLEDGE_ROOT = TEST_ARTIFACTS / "isolated_knowledge_bases"
RUN_TOKEN = "quick_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def main() -> int:
    rounds = 3  # hard cap for this script; user requested <=5
    if rounds > 5:
        raise RuntimeError("rounds must not exceed 5")

    result = run_checks(rounds=rounds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_checks(*, rounds: int) -> dict[str, Any]:
    install_quick_version_store()
    install_isolated_knowledge_root()
    cleanup_quick_artifacts()
    baseline = QuickVersionStore().create_snapshot("workflow reliability quick baseline", {"run_token": RUN_TOKEN}, prune=False)
    try:
        return run_checks_inner(rounds=rounds)
    finally:
        QuickVersionStore().rollback(str(baseline.get("version_id") or ""))
        cleanup_quick_artifacts()


def run_checks_inner(*, rounds: int) -> dict[str, Any]:
    client = TestClient(create_app())
    TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    timeline: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for idx in range(1, rounds + 1):
        round_id = f"{RUN_TOKEN}_r{idx}"
        try:
            timeline.append(run_round(client, round_id=round_id))
        except Exception as exc:  # noqa: BLE001 - we want full round error capture
            failures.append({"round": idx, "round_id": round_id, "error": repr(exc)})

    # Failure-path checks: invalid file / sensitive leak.
    failure_path_result = run_failure_path_checks(client)
    if not failure_path_result.get("ok"):
        failures.append({"phase": "failure_path", "detail": failure_path_result})

    return {
        "ok": not failures,
        "rounds_requested": rounds,
        "rounds_completed": len(timeline),
        "timeline": timeline,
        "failure_path": failure_path_result,
        "failures": failures,
        "conclusion": "PASS" if not failures else "FAIL",
    }


class QuickVersionStore:
    """Small test double for workflow quick checks.

    The production VersionStore copies the full tenant/shared knowledge tree and
    builds downloadable bundles. That is correct for real releases, but too
    heavy for this quick reliability runner on a populated local workspace.
    This store snapshots only the workflow knowledge base files the test mutates.
    """

    def create_snapshot(self, reason: str, metadata: dict[str, Any] | None = None, *, prune: bool = True) -> dict[str, Any]:
        del prune
        version_id = "quick_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        version_root = SNAPSHOT_ROOT / version_id
        source_root = workflow_service_module.default_admin_knowledge_base_root("default")
        compiled_source_root = knowledge_compiler_module.DEFAULT_COMPILED_ROOT
        knowledge_target = version_root / "knowledge_bases"
        compiled_target = version_root / "compiled_structured_compat"
        version_root.mkdir(parents=True, exist_ok=False)
        if source_root.exists():
            shutil.copytree(source_root, knowledge_target)
        if compiled_source_root.exists():
            shutil.copytree(compiled_source_root, compiled_target)
        payload = {
            "version_id": version_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "metadata": metadata or {},
            "knowledge_base_path": str(knowledge_target),
            "structured_path": "",
            "shared_knowledge_path": "",
            "tenants_path": "",
            "compiled_structured_compat_path": str(compiled_target),
            "quick_test_snapshot": True,
        }
        (version_root / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def rollback(self, version_id: str) -> dict[str, Any]:
        version_root = SNAPSHOT_ROOT / version_id
        source = version_root / "knowledge_bases"
        if not source.exists():
            return {"ok": False, "message": f"version not found: {version_id}"}
        target = workflow_service_module.default_admin_knowledge_base_root("default")
        replace_tree_contents(source, target)
        compiled_source = version_root / "compiled_structured_compat"
        if compiled_source.exists():
            replace_tree_contents(compiled_source, knowledge_compiler_module.DEFAULT_COMPILED_ROOT)
        return {
            "ok": True,
            "message": "quick workflow knowledge rollback applied",
            "version_id": version_id,
            "quick_test_snapshot": True,
        }


def install_quick_version_store() -> None:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    workflow_service_module.VersionStore = QuickVersionStore


def install_isolated_knowledge_root() -> None:
    source_root = workflow_service_module.default_admin_knowledge_base_root("default")
    if ISOLATED_KNOWLEDGE_ROOT.exists():
        shutil.rmtree(ISOLATED_KNOWLEDGE_ROOT)
    if source_root.exists():
        shutil.copytree(source_root, ISOLATED_KNOWLEDGE_ROOT)
    else:
        ISOLATED_KNOWLEDGE_ROOT.mkdir(parents=True, exist_ok=True)

    def isolated_default_admin_knowledge_base_root(tenant_id: str | None = None) -> Path:
        del tenant_id
        return ISOLATED_KNOWLEDGE_ROOT

    workflow_service_module.default_admin_knowledge_base_root = isolated_default_admin_knowledge_base_root


def cleanup_quick_artifacts() -> None:
    curated_root = APP_ROOT / "data" / "tenants" / "default" / "learning_packs" / "curated_templates"
    if not curated_root.exists():
        return
    for path in curated_root.glob(f"templates_wfr_batch_{RUN_TOKEN}_*.jsonl"):
        path.unlink(missing_ok=True)


def replace_tree_contents(source: Path, target: Path) -> None:
    if target.exists():
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def run_round(client: TestClient, *, round_id: str) -> dict[str, Any]:
    source_path = TEST_ARTIFACTS / f"source_{round_id}.jsonl"
    suite_path = TEST_ARTIFACTS / f"suite_{round_id}.json"
    release_version = f"wf_reliability_{round_id}"

    rows = [
        {
            "id": f"wfr_{round_id}_a",
            "customer_message": f"wfrq_{round_id}_a 预算15万-18万，想找省油家用SUV。",
            "service_reply": "这档预算可以先看省油家用SUV，我先按通勤和空间给你做三档建议。",
            "intent_tags": ["需求探询", "车型推荐"],
            "tone_tags": ["咨询式"],
            "scenario": "预算推荐",
            "hit_count": 5,
        },
        {
            "id": f"wfr_{round_id}_b",
            "customer_message": f"wfrq_{round_id}_b 你能保证我征信一般也一定贷下来吗？",
            "service_reply": "金融审批不能保证通过，需要人工顾问一对一核实资质后再给方案。",
            "intent_tags": ["金融风险", "转人工"],
            "tone_tags": ["谨慎说明"],
            "scenario": "金融边界",
            "hit_count": 7,
        },
        {
            "id": f"wfr_{round_id}_c",
            "customer_message": f"wfrq_{round_id}_c 周末看车怎么预约更快？",
            "service_reply": "你发下到店时间和意向车型，我帮你安排预约看车排期。",
            "intent_tags": ["预约看车"],
            "tone_tags": ["执行导向"],
            "scenario": "看车预约",
            "hit_count": 6,
        },
    ]
    write_jsonl(source_path, rows)

    suite = {
        "suite_id": f"wf_reliability_suite_{round_id}",
        "schema_version": 1,
        "cases": [
            {
                "case_id": "budget",
                "input_messages": [{"role": "user", "content": rows[0]["customer_message"]}],
                "expected_constraints": {"required_terms": ["三档建议"], "require_handoff": False},
            },
            {
                "case_id": "finance",
                "input_messages": [{"role": "user", "content": rows[1]["customer_message"]}],
                "expected_constraints": {
                    "required_terms": ["人工顾问一对一核实"],
                    "forbidden_terms": ["100%通过", "包过", "保证通过"],
                    "require_handoff": True,
                },
            },
            {
                "case_id": "booking",
                "input_messages": [{"role": "user", "content": rows[2]["customer_message"]}],
                "expected_constraints": {"required_terms": ["预约看车排期"], "require_handoff": False},
            },
        ],
    }
    suite_path.write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    curation = client.post(
        "/api/workflow/curation/jobs",
        json={
            "tenant_id": "default",
            "industry_id": "used_car",
            "batch_id": f"wfr_batch_{round_id}",
            "source_files": [str(source_path)],
            "strict_mode": True,
        },
    )
    assert_equal(curation.status_code, 200, "curation status")
    curation_data = curation.json()
    assert_true(curation_data.get("ok"), "curation should succeed")
    summary = curation_data.get("summary") if isinstance(curation_data.get("summary"), dict) else {}
    assert_true(int(summary.get("accepted", 0) or 0) >= 3, "curation should keep rows")
    assert_equal(as_int(summary.get("source_missing_count"), default=-1), 0, "source_missing_count should be zero")
    assert_equal(as_int(summary.get("rejected"), default=-1), 0, "rejected should be zero")
    curated_file = str(curation_data.get("curated_file") or "")

    real_chat_dry_run = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": curated_file},
    )
    assert_equal(real_chat_dry_run.status_code, 200, "real-chat dry-run status")
    real_chat_dry_data = real_chat_dry_run.json()
    assert_true(real_chat_dry_data.get("ok"), "real-chat dry-run should complete with blocked rows")
    assert_equal(
        int((real_chat_dry_data.get("summary") or {}).get("blocked_items", 0) or 0),
        len(rows),
        "curated real-chat rows should be blocked from direct formal import",
    )

    dry_run = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": str(source_path)},
    )
    assert_equal(dry_run.status_code, 200, "dry-run status")
    dry_data = dry_run.json()
    assert_true(dry_data.get("ok"), "dry-run should pass")
    assert_equal(int((dry_data.get("summary") or {}).get("blocked_items", 0) or 0), 0, "dry-run blocked should be zero")

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
    assert_true(apply_data.get("ok"), "apply should pass")
    apply_job_id = str(apply_data.get("job_id") or "")
    assert_true(bool(apply_job_id), "apply_job_id should exist")
    apply_job_file = APP_ROOT / "data" / "tenants" / "default" / "learning_packs" / "import_jobs" / f"{apply_job_id}.json"
    assert_true(apply_job_file.exists(), "apply job file should exist")

    # Idempotency: importing the same curated file again should not create new changes.
    dry_run_again = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": str(source_path)},
    )
    assert_equal(dry_run_again.status_code, 200, "second dry-run status")
    dry_again = dry_run_again.json()
    assert_true(dry_again.get("ok"), "second dry-run should pass")
    idempotent_summary = dry_again.get("summary") if isinstance(dry_again.get("summary"), dict) else {}
    assert_equal(as_int(idempotent_summary.get("new_items"), default=-1), 0, "idempotent new_items should be 0")
    assert_equal(as_int(idempotent_summary.get("updated_items"), default=-1), 0, "idempotent updated_items should be 0")
    assert_equal(as_int(idempotent_summary.get("blocked_items"), default=-1), 0, "idempotent blocked should be 0")

    eval_run = client.post(
        "/api/workflow/replay-eval/run",
        json={"tenant_id": "default", "release_version": release_version, "suite_file": str(suite_path)},
    )
    assert_equal(eval_run.status_code, 200, "eval status")
    eval_data = eval_run.json()
    assert_true(eval_data.get("ok"), "eval should be ok")
    assert_true(bool(eval_data.get("gate_pass")), "eval gate should pass")
    finance_case = next((item for item in eval_data.get("results", []) if item.get("case_id") == "finance"), {})
    assert_true(bool(finance_case.get("predicted_requires_handoff")), "finance case must handoff")
    assert_true(not bool(finance_case.get("violated")), "finance case should not violate")

    release_create = client.post(
        "/api/workflow/releases",
        json={
            "tenant_id": "default",
            "industry_id": "used_car",
            "release_version": release_version,
            "import_job_ids": [apply_job_id],
            "feature_flags": {"workflow_standard_enabled": True},
        },
    )
    assert_equal(release_create.status_code, 200, "release create status")
    release_data = release_create.json()
    release_id = str(release_data.get("release_id") or "")
    assert_true(bool(release_id), "release id should exist")

    approve = client.post(
        f"/api/workflow/releases/{release_id}/approve",
        json={"approved_by": "workflow_reliability_quick", "approval_note": "quick reliability pass"},
    )
    assert_equal(approve.status_code, 200, "release approve status")
    approve_data = approve.json()
    assert_true(approve_data.get("ok"), "approve should be ok")

    rollback_to = str((approve_data.get("release") or {}).get("rollback_to") or "")
    rollback = client.post(
        f"/api/workflow/releases/{release_id}/rollback",
        json={"rollback_to_version": rollback_to, "reason": "quick reliability rollback drill"},
    )
    assert_equal(rollback.status_code, 200, "rollback status")
    rollback_data = rollback.json()
    assert_true(rollback_data.get("ok"), "rollback should be ok")
    assert_equal(str((rollback_data.get("release") or {}).get("status") or ""), "rolled_back", "release should be rolled_back")
    assert_true(apply_job_file.exists(), "apply job file should survive rollback")

    return {
        "round_id": round_id,
        "curation_job_id": curation_data.get("job_id"),
        "blocked_real_chat_dry_run_job_id": real_chat_dry_data.get("job_id"),
        "dry_run_job_id": dry_data.get("job_id"),
        "apply_job_id": apply_job_id,
        "eval_job_id": eval_data.get("eval_job_id"),
        "release_id": release_id,
        "rollback_to": rollback_to,
        "gate_pass": eval_data.get("gate_pass"),
        "metrics": eval_data.get("metrics", {}),
    }


def run_failure_path_checks(client: TestClient) -> dict[str, Any]:
    bad_file_path = str(TEST_ARTIFACTS / "missing_file_for_failure_path.jsonl")
    missing_file = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": bad_file_path},
    )
    missing_file_ok = missing_file.status_code == 400

    leak_path = TEST_ARTIFACTS / "failure_phone_leak.jsonl"
    leak_path.write_text(
        json.dumps(
            {
                "id": "wf_failure_leak_case",
                "category_id": "chats",
                "data": {
                    "customer_message": "我电话是13912345678，方便联系吗？",
                    "service_reply": "好的，记下你的电话13912345678了。",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    leak_check = client.post(
        "/api/workflow/template-import/dry-run",
        json={"tenant_id": "default", "industry_id": "used_car", "input_file": str(leak_path)},
    )
    leak_ok = leak_check.status_code == 200 and int((leak_check.json().get("summary") or {}).get("blocked_items", 0) or 0) >= 1

    release_without_import = client.post(
        "/api/workflow/releases",
        json={
            "tenant_id": "default",
            "industry_id": "used_car",
            "release_version": f"wf_failure_release_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "import_job_ids": [],
            "feature_flags": {"workflow_standard_enabled": True},
        },
    )
    release_without_import_rejected = release_without_import.status_code == 400

    no_match_suite_path = TEST_ARTIFACTS / "failure_no_match_suite.json"
    no_match_suite_path.write_text(
        json.dumps(
            {
                "suite_id": "wf_failure_no_match_suite",
                "cases": [
                    {
                        "case_id": "no_match_case",
                        "input_messages": [{"role": "user", "content": "完全未知问句-no-match-sentinel"}],
                        "expected_constraints": {"require_handoff": False},
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    no_match_eval = client.post(
        "/api/workflow/replay-eval/run",
        json={
            "tenant_id": "default",
            "release_version": f"wf_failure_no_match_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "suite_file": str(no_match_suite_path),
        },
    )
    no_match_payload = no_match_eval.json() if no_match_eval.status_code == 200 else {}
    failed_cases = no_match_payload.get("failed_cases") if isinstance(no_match_payload.get("failed_cases"), list) else []
    no_match_flagged = (
        no_match_eval.status_code == 200
        and not bool(no_match_payload.get("gate_pass"))
        and any("no_matching_template" in list(item.get("reasons") or []) for item in failed_cases if isinstance(item, dict))
    )

    return {
        "ok": bool(missing_file_ok and leak_ok and release_without_import_rejected and no_match_flagged),
        "missing_file_rejected": missing_file_ok,
        "phone_leak_blocked": leak_ok,
        "release_without_import_rejected": release_without_import_rejected,
        "no_match_case_flagged": no_match_flagged,
        "missing_file_status": missing_file.status_code,
        "leak_status": leak_check.status_code,
        "leak_summary": leak_check.json().get("summary") if leak_check.status_code == 200 else {},
        "release_without_import_status": release_without_import.status_code,
        "no_match_eval_status": no_match_eval.status_code,
        "no_match_gate_pass": bool(no_match_payload.get("gate_pass")) if no_match_eval.status_code == 200 else None,
        "no_match_failed_cases": failed_cases,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r}, actual={actual!r}")


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


def as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
