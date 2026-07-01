"""Smoke checks for the minimal WeChat add_friend live package."""

from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_required_files_exist() -> None:
    required = [
        "apps/__init__.py",
        "apps/wechat_ai_customer_service/__init__.py",
        "apps/wechat_ai_customer_service/README.md",
        "apps/wechat_ai_customer_service/docs/add_friend_rpa_pr_readiness_20260616.md",
        "apps/wechat_ai_customer_service/requirements-add-friend.txt",
        "apps/wechat_ai_customer_service/wechat_message_envelope.py",
        "apps/wechat_ai_customer_service/wechat_message_normalizer.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_actions.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_artifacts.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_contract.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_diagnostics.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_flow.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_flow_context.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_flow_events.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_layout.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_locator.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_ocr.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_operator_guard.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_pacing.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_payloads.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_result_mapping.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_routes.py",
        "apps/wechat_ai_customer_service/adapters/add_friend_screenshot.py",
        "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py",
        "apps/wechat_ai_customer_service/adapters/wechat_connector.py",
        "apps/wechat_ai_customer_service/scripts/run_wechat_add_friend_entry_click_plan_windows.ps1",
        "apps/wechat_ai_customer_service/scripts/check_wechat_add_friend_entry_click_latest.ps1",
    ]
    for relative_path in required:
        assert_true((PROJECT_ROOT / relative_path).exists(), f"missing required file: {relative_path}")


def test_requirements_cover_live_imports() -> None:
    requirements = (PROJECT_ROOT / "apps/wechat_ai_customer_service/requirements-add-friend.txt").read_text(
        encoding="utf-8"
    )
    for package_name in ["pillow", "pywin32", "pyperclip", "rapidocr-onnxruntime", "psutil"]:
        assert_true(package_name in requirements.lower(), f"missing dependency: {package_name}")


def test_entry_click_script_defaults_are_low_disturbance() -> None:
    script = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/scripts/run_wechat_add_friend_entry_click_plan_windows.ps1"
    ).read_text(
        encoding="utf-8"
    )
    assert_true(
        "[switch]$NormalizeWindow" in script
        and 'WECHAT_WIN32_OCR_WINDOW_NORMALIZE = $(if ($NormalizeWindow) { "1" } else { "0" })' in script,
        "window normalization must default to off",
    )
    assert_true(
        "[switch]$AllowRenderRecovery" in script
        and 'WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO = $(if ($AllowRenderRecovery) { "1" } else { "0" })' in script,
        "render recovery must default to off",
    )
    for removed in [
        "run_wechat_add_friend_live.ps1",
        "run_wechat_add_friend_plan.ps1",
        "run_wechat_add_friend_entry_plan.ps1",
    ]:
        assert_true(
            not (PROJECT_ROOT / f"apps/wechat_ai_customer_service/scripts/{removed}").exists(),
            f"removed add_friend script should not exist: {removed}",
        )


def test_entry_click_script_is_main_review_entry() -> None:
    script = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/scripts/run_wechat_add_friend_entry_click_plan_windows.ps1"
    ).read_text(encoding="utf-8")
    assert_true('"add-friend-entry-click-plan-windows"' in script, "entry-click script must call the Windows main route")
    for token in ["VerifyMessage", "RemarkName", "RemarkCode", "--verify-message", "--remark-name", "--remark-code"]:
        assert_true(token in script, f"entry-click script must expose formal field: {token}")
    assert_true('"--sales-name"' not in script, "entry-click main route must not pass removed sales-name")
    assert_true('"--remark"' not in script, "entry-click main route must not pass removed remark")
    assert_true("add_friend_entry_click_review.html" in script, "entry-click script must write HTML review")
    assert_true("add_friend_entry_click_review.json" in script, "entry-click script must write JSON review")
    assert_true("runtime\\add_friend_entry_click_plan_windows\\latest" in script, "entry-click script must update the Windows adaptive latest report")
    assert_true("Start-Process -FilePath" in script, "review open command must use FilePath for Windows PowerShell")


def test_entry_click_latest_check_script_contract() -> None:
    script = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/scripts/check_wechat_add_friend_entry_click_latest.ps1"
    ).read_text(encoding="utf-8")
    for token in [
        "add_friend_entry_click_plan.json",
        "add_friend_entry_click_review.json",
        "ExpectedVerifyMessage",
        "ExpectedRemarkName",
        "ExpectedRemarkCode",
        "invite_sent",
        'ExpectedResultCode -ne "already_friend"',
        "add_friend.step_events.v1",
        "native_diagnostic_events",
        "diagnostic_events",
        "invite_confirm_after_click",
        "add_contact_search_terminal",
        "already_friend",
        "Route",
        "ArtifactScope",
        "add_friend_entry_click_plan_windows",
    ]:
        assert_true(token in script, f"latest check script missing contract token: {token}")
    assert_true("exit 1" in script and "exit 0" in script, "latest check script must be CI-friendly")


def test_add_friend_readme_formal_contract() -> None:
    readme = (PROJECT_ROOT / "apps/wechat_ai_customer_service/README.md").read_text(encoding="utf-8")
    readiness = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/docs/add_friend_rpa_pr_readiness_20260616.md"
    ).read_text(encoding="utf-8")
    for text, name in [(readme, "README"), (readiness, "readiness doc")]:
        assert_true("add-friend-entry-click-plan-windows" in text, f"{name} must name the stable add_friend route")
        assert_true("add-friend-entry-click-plan-windows" in text, f"{name} must name the Windows alias or handoff route")
        for token in ["verify_message", "remark_name", "remark_code", "TASK_PAYLOAD_INVALID", "invite_sent"]:
            assert_true(token in text, f"{name} missing formal contract token: {token}")
        assert_true("phone_or_wechat" in text, f"{name} must document phone_or_wechat")
        success_block = text.split("失败", 1)[0]
        assert_true(
            "already_friend" not in success_block,
            f"{name} must not list already_friend as a successful post-confirm result",
        )
    assert_true("-Remark " not in readme, "README Windows main-route example must not use legacy -Remark")
    assert_true("-Greeting " not in readme, "README Windows main-route example must not use legacy -Greeting")
    for token in [
        "Package Contents",
        "PR Description Draft",
        "Windows 2026-06-16 实机回归结论",
        "Windows real-machine regression completed for the formal happy path and formal field-contract failures",
        "check_wechat_add_friend_entry_click_latest.ps1",
    ]:
        assert_true(token in readiness, f"readiness doc missing PR handoff section: {token}")


def test_add_friend_artifact_layout_contract() -> None:
    from datetime import datetime

    from apps.wechat_ai_customer_service.adapters.add_friend_artifacts import (
        ADD_FRIEND_ENTRY_CLICK_PLAN_JSON,
        ADD_FRIEND_ENTRY_CLICK_REVIEW_HTML,
        ADD_FRIEND_ENTRY_CLICK_REVIEW_JSON,
        ADD_FRIEND_ENTRY_CLICK_STDERR_LOG,
        ADD_FRIEND_ENTRY_CLICK_STDOUT_JSON,
        ADD_FRIEND_LATEST_DIR,
        ADD_FRIEND_RUNTIME_DIR,
        ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE,
        add_friend_artifact_manifest,
        add_friend_artifact_scope,
        add_friend_entry_click_artifact_paths,
        add_friend_latest_dir,
        add_friend_route_artifact_root,
        add_friend_timestamp_id,
        add_friend_timestamp_run_dir,
    )
    from apps.wechat_ai_customer_service.adapters.add_friend_routes import ADD_FRIEND_MAIN_ROUTE

    assert_true(ADD_FRIEND_RUNTIME_DIR == "runtime", f"runtime dir mismatch: {ADD_FRIEND_RUNTIME_DIR}")
    assert_true(ADD_FRIEND_LATEST_DIR == "latest", f"latest dir mismatch: {ADD_FRIEND_LATEST_DIR}")
    assert_true(ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE == "add_friend_entry_click_plan_windows", "Windows artifact scope constant mismatch")
    assert_true(add_friend_artifact_scope(ADD_FRIEND_MAIN_ROUTE) == ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE, "main route artifact scope mismatch")
    root = Path("C:/omniauto")
    route_root = add_friend_route_artifact_root(root, ADD_FRIEND_MAIN_ROUTE)
    assert_true(str(route_root).replace("\\", "/").endswith("runtime/add_friend_entry_click_plan_windows"), f"route root mismatch: {route_root}")
    run_dir = add_friend_timestamp_run_dir(root, ADD_FRIEND_MAIN_ROUTE, timestamp="20260616_120102")
    assert_true(str(run_dir).replace("\\", "/").endswith("runtime/add_friend_entry_click_plan_windows/20260616_120102"), f"run dir mismatch: {run_dir}")
    latest_dir = add_friend_latest_dir(root, ADD_FRIEND_MAIN_ROUTE)
    assert_true(str(latest_dir).replace("\\", "/").endswith("runtime/add_friend_entry_click_plan_windows/latest"), f"latest dir mismatch: {latest_dir}")
    assert_true(add_friend_timestamp_id(datetime(2026, 6, 16, 12, 1, 2)) == "20260616_120102", "timestamp format mismatch")
    paths = add_friend_entry_click_artifact_paths(run_dir)
    assert_true(paths["plan_json"].endswith(ADD_FRIEND_ENTRY_CLICK_PLAN_JSON), f"plan json path mismatch: {paths}")
    assert_true(paths["review_json"].endswith(ADD_FRIEND_ENTRY_CLICK_REVIEW_JSON), f"review json path mismatch: {paths}")
    assert_true(paths["review_html"].endswith(ADD_FRIEND_ENTRY_CLICK_REVIEW_HTML), f"review html path mismatch: {paths}")
    assert_true(paths["stdout_json"].endswith(ADD_FRIEND_ENTRY_CLICK_STDOUT_JSON), f"stdout path mismatch: {paths}")
    assert_true(paths["stderr_log"].endswith(ADD_FRIEND_ENTRY_CLICK_STDERR_LOG), f"stderr path mismatch: {paths}")
    manifest = add_friend_artifact_manifest(root, ADD_FRIEND_MAIN_ROUTE)
    assert_true(manifest.get("scope") == "add_friend_entry_click_plan_windows", f"manifest scope mismatch: {manifest}")
    script = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/scripts/run_wechat_add_friend_entry_click_plan_windows.ps1"
    ).read_text(encoding="utf-8")
    assert_true("runtime\\add_friend_entry_click_plan_windows\\$Timestamp" in script, "entry-click script timestamp dir must match artifact contract")
    assert_true("runtime\\add_friend_entry_click_plan_windows\\latest" in script, "entry-click script latest dir must match artifact contract")


def test_add_friend_route_manifest_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_routes import (
        ADD_FRIEND_MAIN_ROUTE,
        ADD_FRIEND_ROUTES,
        ADD_FRIEND_WINDOWS_MAIN_ROUTE,
        ADD_FRIEND_WINDOWS_ROUTE,
        add_friend_route_accepts_formal_fields,
        add_friend_route_accepts_query,
        add_friend_route_kind,
        add_friend_route_uses_passive_probe,
        is_add_friend_diagnostic_route,
        is_add_friend_legacy_route,
        is_add_friend_main_route,
        is_add_friend_route,
    )

    assert_true(ADD_FRIEND_MAIN_ROUTE == "add-friend-entry-click-plan-windows", f"unexpected main route: {ADD_FRIEND_MAIN_ROUTE}")
    assert_true(ADD_FRIEND_WINDOWS_ROUTE == "add-friend-entry-click-plan-windows", f"Windows route mismatch: {ADD_FRIEND_WINDOWS_ROUTE}")
    assert_true(ADD_FRIEND_WINDOWS_MAIN_ROUTE == ADD_FRIEND_WINDOWS_ROUTE, f"Windows main alias mismatch: {ADD_FRIEND_WINDOWS_MAIN_ROUTE}")
    assert_true(
        ADD_FRIEND_ROUTES == ("add-friend-entry-click-plan-windows",),
        f"expected only the Windows add_friend route: {ADD_FRIEND_ROUTES}",
    )
    assert_true(is_add_friend_main_route(ADD_FRIEND_MAIN_ROUTE), "Windows entry-click route must be the official main route")
    assert_true(is_add_friend_main_route(ADD_FRIEND_WINDOWS_ROUTE), "Windows route must be the official main route")
    assert_true(add_friend_route_kind(ADD_FRIEND_MAIN_ROUTE) == "windows", "main route kind mismatch")
    assert_true(add_friend_route_kind(ADD_FRIEND_WINDOWS_ROUTE) == "windows", "Windows route kind mismatch")
    assert_true(add_friend_route_accepts_formal_fields(ADD_FRIEND_MAIN_ROUTE), "main route must accept formal fields")
    assert_true(add_friend_route_accepts_query(ADD_FRIEND_MAIN_ROUTE), "main route must accept phone/wechat query")
    assert_true(add_friend_route_uses_passive_probe(ADD_FRIEND_MAIN_ROUTE) is False, "main route must focus WeChat before formal add_friend clicks")
    assert_true(add_friend_route_uses_passive_probe(ADD_FRIEND_WINDOWS_ROUTE) is False, "Windows route must focus WeChat before formal add_friend clicks")
    assert_true(not is_add_friend_diagnostic_route(ADD_FRIEND_MAIN_ROUTE), "main route must not be diagnostic")
    assert_true(not is_add_friend_diagnostic_route(ADD_FRIEND_WINDOWS_ROUTE), "Windows route must not be diagnostic")
    assert_true(not is_add_friend_legacy_route(ADD_FRIEND_MAIN_ROUTE), "main route must not be legacy")
    assert_true(not is_add_friend_legacy_route(ADD_FRIEND_WINDOWS_ROUTE), "Windows route must not be legacy")
    removed_public_route = "add-friend-entry-click-" + "plan"
    removed_reference_route = "add-friend-entry-click-plan-windows-" + "1080p-reference"
    for removed in ["add-friend", "add-friend-plan", "add-friend-entry-plan", removed_public_route, removed_reference_route]:
        assert_true(not is_add_friend_route(removed), f"removed add_friend action should not be a route: {removed}")
        assert_true(not is_add_friend_diagnostic_route(removed), f"removed add_friend action should not be diagnostic: {removed}")
        assert_true(not is_add_friend_legacy_route(removed), f"removed add_friend action should not be legacy: {removed}")


def test_entry_click_field_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_contract import (
        ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS,
        VALIDATION_ERROR_REMARK_CODE_MISSING,
        VALIDATION_ERROR_REQUIRED,
        add_friend_entry_click_contract_summary,
        normalize_add_friend_query,
        validate_add_friend_entry_click_contract,
    )
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (
        args_for_daemon_request,
        validate_add_friend_entry_click_contract as sidecar_validate_add_friend_entry_click_contract,
    )

    assert_true(
        ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS == ("phone_or_wechat", "verify_message", "remark_name", "remark_code"),
        f"formal required fields changed unexpectedly: {ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS}",
    )
    assert_true(
        normalize_add_friend_query(phone=" 173 6874 6889 ", wechat="wxid_should_not_win") == "17368746889",
        "phone digits should win over wechat id",
    )
    assert_true(
        normalize_add_friend_query(phone="", wechat=" wxid_demo ") == "wxid_demo",
        "wechat id should be used when phone is missing",
    )
    assert_true(
        normalize_add_friend_query(phone="  -  ", wechat="") == "",
        "blank add_friend query should stay blank",
    )
    missing_verify = validate_add_friend_entry_click_contract(
        phone="17368746889",
        verify_message="",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
    )
    assert_true(missing_verify.get("ok") is False, f"missing verify_message should fail: {missing_verify}")
    assert_true(
        any(
            error.get("field") == "verify_message" and error.get("code") == VALIDATION_ERROR_REQUIRED
            for error in missing_verify.get("validation_errors") or []
        ),
        f"missing verify_message error mismatch: {missing_verify}",
    )
    missing_query = validate_add_friend_entry_click_contract(
        phone="",
        wechat="",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
    )
    assert_true(missing_query.get("ok") is False, f"missing phone/wechat should fail: {missing_query}")
    assert_true(
        any(
            error.get("field") == "phone_or_wechat" and error.get("code") == VALIDATION_ERROR_REQUIRED
            for error in missing_query.get("validation_errors") or []
        ),
        f"missing phone/wechat error mismatch: {missing_query}",
    )
    missing_remark = validate_add_friend_entry_click_contract(
        phone="17368746889",
        verify_message="我是车金二手车张伟",
        remark_name="",
        remark_code="CJ8K2P",
    )
    assert_true(missing_remark.get("ok") is False, f"missing remark_name should fail: {missing_remark}")
    missing_code = validate_add_friend_entry_click_contract(
        phone="17368746889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="",
    )
    assert_true(missing_code.get("ok") is False, f"missing remark_code should fail: {missing_code}")
    mismatched_code = validate_add_friend_entry_click_contract(
        phone="17368746889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-OTHER",
        remark_code="CJ8K2P",
    )
    assert_true(mismatched_code.get("ok") is False, f"remark_name without remark_code should fail: {mismatched_code}")
    assert_true(mismatched_code.get("remark_code_valid") is False, f"mismatch should mark remark_code invalid: {mismatched_code}")
    assert_true(
        any(error.get("code") == VALIDATION_ERROR_REMARK_CODE_MISSING for error in mismatched_code.get("validation_errors") or []),
        f"mismatch should expose remark code error: {mismatched_code}",
    )
    valid = validate_add_friend_entry_click_contract(
        phone="173 6874 6889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
    )
    assert_true(valid.get("ok") is True, f"complete formal fields should pass: {valid}")
    assert_true(valid.get("query") == "17368746889", f"valid contract should expose normalized query: {valid}")
    assert_true(valid.get("remark_code_valid") is True, f"valid code should be marked valid: {valid}")
    assert_true(
        sidecar_validate_add_friend_entry_click_contract(
            phone="173 6874 6889",
            verify_message="我是车金二手车张伟",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
        )
        == valid,
        "sidecar must reuse the formal contract module",
    )
    summary = add_friend_entry_click_contract_summary(valid)
    assert_true(summary.get("required_fields") == list(ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS), f"summary required fields mismatch: {summary}")
    assert_true("legacy_fields" not in summary, f"summary must not expose removed legacy fields: {summary}")

    argv = args_for_daemon_request(
        {
            "action": "add-friend-entry-click-plan-windows",
            "phone": "17368746889",
            "verify_message": "我是车金二手车张伟",
            "remark_name": "客户-CJ8K2P",
            "remark_code": "CJ8K2P",
        }
    )
    assert_true("--verify-message" in argv and "我是车金二手车张伟" in argv, f"daemon argv should include verify_message: {argv}")
    assert_true("--remark-name" in argv and "客户-CJ8K2P" in argv, f"daemon argv should include remark_name: {argv}")
    assert_true("--remark-code" in argv and "CJ8K2P" in argv, f"daemon argv should include remark_code: {argv}")
    assert_true("--remark" not in argv, f"entry-click argv must not accept removed remark flag: {argv}")
    assert_true("--sales-name" not in argv and "--greeting" not in argv, f"entry-click argv must not accept removed flags: {argv}")


def test_add_friend_payload_builder_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_contract import validate_add_friend_entry_click_contract
    from apps.wechat_ai_customer_service.adapters.add_friend_payloads import (
        add_friend_add_contact_entry_not_found_payload,
        add_friend_after_confirm_payload,
        add_friend_invite_form_window_not_found_payload,
        add_friend_phone_not_found_payload,
        add_friend_task_payload_invalid,
    )
    from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
        ERROR_ACCOUNT_RESTRICTED,
        ERROR_ADD_CONTACT_ENTRY_NOT_FOUND,
        ERROR_INVITE_CONFIRM_CLICK_FAILED,
        ERROR_INVITE_FIELD_VERIFICATION_FAILED,
        ERROR_INVITE_FORM_WINDOW_NOT_FOUND,
        ERROR_PHONE_NOT_FOUND,
        ERROR_TASK_PAYLOAD_INVALID,
        RESULT_INVITE_SENT,
    )

    validation = validate_add_friend_entry_click_contract(
        phone="17368746889",
        verify_message="",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
    )
    payload = add_friend_task_payload_invalid(
        phone="173 6874 6889",
        wechat="wxid_should_not_win",
        validation=validation,
        plan_path="/tmp/add_friend_entry_click_plan.json",
        probe={"skipped": True, "reason": "task_payload_invalid_before_window_probe"},
    )
    assert_true(payload.get("ok") is False, f"invalid payload should fail: {payload}")
    assert_true(payload.get("state") == "task_payload_invalid", f"invalid payload state mismatch: {payload}")
    assert_true(payload.get("task_status") == "failed", f"invalid payload task_status mismatch: {payload}")
    assert_true(payload.get("error_code") == ERROR_TASK_PAYLOAD_INVALID, f"invalid payload error mismatch: {payload}")
    assert_true(payload.get("current_step") == "payload_validation", f"invalid payload step mismatch: {payload}")
    assert_true(payload.get("query") == "17368746889", f"invalid payload query mismatch: {payload}")
    assert_true(payload.get("server_report_payload") == {
        "task.status": "failed",
        "task.error_code": ERROR_TASK_PAYLOAD_INVALID,
        "task.current_step": "payload_validation",
    }, f"invalid payload server report mismatch: {payload}")
    assert_true(payload.get("window_probe", {}).get("skipped") is True, f"invalid payload probe mismatch: {payload}")
    assert_true(payload.get("legacy_remark_fallback") is False, f"invalid payload legacy fallback mismatch: {payload}")
    assert_true(payload.get("timings") == [], f"invalid payload should not invent timings: {payload}")

    invite_sent = add_friend_after_confirm_payload(
        confirm_ok=True,
        surface_text="等待验证",
        invite_form_detected=False,
        phone="173 6874 6889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
        remark_code_valid=True,
        timings=[{"name": "invite_confirm_click", "seconds": 0.2}],
    )
    assert_true(invite_sent.get("ok") is True, f"invite_sent payload should be ok: {invite_sent}")
    assert_true(invite_sent.get("task_status") == "completed", f"invite_sent status mismatch: {invite_sent}")
    assert_true(invite_sent.get("result_code") == RESULT_INVITE_SENT, f"invite_sent result mismatch: {invite_sent}")
    assert_true(invite_sent.get("error_code") == "", f"invite_sent error mismatch: {invite_sent}")
    assert_true(invite_sent.get("query") == "17368746889", f"invite_sent query mismatch: {invite_sent}")
    assert_true(invite_sent.get("remark_code_valid") is True, f"invite_sent remark code mismatch: {invite_sent}")
    assert_true(invite_sent.get("server_report_payload", {}).get("task.result_code") == RESULT_INVITE_SENT, f"invite_sent report mismatch: {invite_sent}")

    after_confirm = add_friend_after_confirm_payload(
        confirm_ok=True,
        surface_text="申请添加朋友 确定",
        invite_form_detected=True,
        phone="17368746889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
        remark_code_valid=True,
    )
    assert_true(after_confirm.get("task_status") == "completed", f"after-confirm status mismatch: {after_confirm}")
    assert_true(after_confirm.get("result_code") == RESULT_INVITE_SENT, f"after-confirm result mismatch: {after_confirm}")
    assert_true("already_friend" not in json.dumps(after_confirm, ensure_ascii=False), f"after-confirm must not emit already_friend: {after_confirm}")

    phone_not_found = add_friend_phone_not_found_payload(
        query="17368746889",
        not_found={"detected": True},
        screenshot_path="raw.png",
        annotated_path="annotated.png",
        ocr_items=[],
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
        remark_code_valid=True,
    )
    assert_true(phone_not_found.get("ok") is False, f"not-found payload should fail: {phone_not_found}")
    assert_true(phone_not_found.get("task_status") == "failed", f"not-found status mismatch: {phone_not_found}")
    assert_true(phone_not_found.get("error_code") == ERROR_PHONE_NOT_FOUND, f"not-found error mismatch: {phone_not_found}")
    assert_true(phone_not_found.get("current_step") == "searching_phone", f"not-found step mismatch: {phone_not_found}")

    restricted = add_friend_after_confirm_payload(
        confirm_ok=True,
        surface_text="操作频繁，请稍后再试",
        phone="17368746889",
    )
    assert_true(restricted.get("ok") is False, f"restricted payload should fail: {restricted}")
    assert_true(restricted.get("error_code") == ERROR_ACCOUNT_RESTRICTED, f"restricted error mismatch: {restricted}")
    assert_true(restricted.get("server_report_payload", {}).get("task.error_code") == ERROR_ACCOUNT_RESTRICTED, f"restricted report mismatch: {restricted}")

    confirm_failed = add_friend_after_confirm_payload(confirm_ok=False, surface_text="", phone="17368746889")
    assert_true(confirm_failed.get("ok") is False, f"confirm failure payload should fail: {confirm_failed}")
    assert_true(confirm_failed.get("error_code") == ERROR_INVITE_CONFIRM_CLICK_FAILED, f"confirm failure error mismatch: {confirm_failed}")
    assert_true(ERROR_INVITE_FIELD_VERIFICATION_FAILED == "INVITE_FIELD_VERIFICATION_FAILED", "field verification error code mismatch")

    entry_missing = add_friend_add_contact_entry_not_found_payload(phone="17368746889")
    assert_true(entry_missing.get("error_code") == ERROR_ADD_CONTACT_ENTRY_NOT_FOUND, f"entry missing error mismatch: {entry_missing}")
    assert_true(entry_missing.get("current_step") == "searching_contact", f"entry missing step mismatch: {entry_missing}")

    form_missing = add_friend_invite_form_window_not_found_payload(phone="17368746889")
    assert_true(form_missing.get("error_code") == ERROR_INVITE_FORM_WINDOW_NOT_FOUND, f"form missing error mismatch: {form_missing}")
    assert_true(form_missing.get("current_step") == "add_contact_entry_clicked", f"form missing step mismatch: {form_missing}")


def test_add_friend_step_event_report_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import (
        StepEventRecorder,
        make_step_event,
        write_step_event_report,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        event = make_step_event(
            step_id="entry_click",
            title="点击 + 入口",
            status="completed",
            state_before="main_window",
            state_after="plus_entry_popup_menu",
            ocr_items=[{"text": "添加朋友", "confidence": 0.98}],
            targets=[{"name": "plus_entry", "point": [350, 70]}],
            selected_target={"name": "plus_entry", "selected_reason": "primary_entry"},
            artifacts={"raw": "raw.png", "annotated": "annotated.png"},
            timing_ms=1234,
            result={"ok": True},
        )
        html_path = write_step_event_report(
            output_dir=output_dir,
            json_name="add_friend_entry_click_review.json",
            html_name="add_friend_entry_click_review.html",
            title="add_friend 入口点击复核报告",
            description="schema smoke",
            summary={"state": "add_friend_entry_click_plan"},
            events=[event],
        )
        report = json.loads((output_dir / "add_friend_entry_click_review.json").read_text(encoding="utf-8"))
        assert_true(report.get("schema") == "add_friend.step_events.v1", f"unexpected report schema: {report}")
        assert_true("rows" not in (report.get("summary") or {}), f"step event summary must not expose legacy rows: {report}")
        events = report.get("events")
        assert_true(isinstance(events, list) and len(events) == 1, f"events missing: {report}")
        required_fields = {
            "step_id",
            "title",
            "status",
            "state_before",
            "state_after",
            "ocr_items",
            "targets",
            "selected_target",
            "artifacts",
            "timing_ms",
            "result",
        }
        assert_true(required_fields.issubset(set(events[0])), f"event fields incomplete: {events[0]}")
        assert_true(Path(html_path).exists(), "step event HTML report should be written")

    recorder = StepEventRecorder()
    recorder.add(
        step_id="payload_validation",
        title="字段契约校验",
        status="ok",
        state_before="task_received",
        state_after="payload_valid",
        result={"ok": True},
    )
    recorded = recorder.to_list()
    assert_true(len(recorded) == 1, f"recorder should emit one event: {recorded}")
    assert_true(recorded[0].get("status") == "completed", f"recorder should normalize status: {recorded}")
    assert_true(recorded[0].get("step_id") == "payload_validation", f"recorder step_id mismatch: {recorded}")


def test_entry_click_validation_failure_report_uses_native_events() -> None:
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (
        add_friend_entry_click_validation_failure_payload,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        payload = add_friend_entry_click_validation_failure_payload(
            phone="17368746889",
            wechat="",
            verify_message="",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            artifact_dir=tmpdir,
            probe={"skipped": True, "reason": "task_payload_invalid_before_window_probe"},
        )
        report = json.loads((Path(tmpdir) / "add_friend_entry_click_review.json").read_text(encoding="utf-8"))
        events = report.get("events") or []
        assert_true(payload.get("ok") is False, f"validation failure should fail: {payload}")
        assert_true(
            "diagnostic_events" in str(report.get("summary", {}).get("event_source") or ""),
            f"report should prefer native events: {report}",
        )
        assert_true(len(events) == 1, f"validation failure should emit one native event: {events}")
        assert_true(events[0].get("step_id") == "payload_validation", f"unexpected validation event id: {events}")
        assert_true(events[0].get("status") == "failed", f"validation event should fail: {events}")
        assert_true(
            events[0].get("result", {}).get("wechat_ui_action_attempted") is False,
            f"validation failure must not attempt WeChat UI: {events}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        payload = add_friend_entry_click_validation_failure_payload(
            phone="",
            wechat="",
            verify_message="我是车金二手车张伟",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            artifact_dir=tmpdir,
            probe={"skipped": True, "reason": "task_payload_invalid_before_window_probe"},
        )
        assert_true(payload.get("ok") is False, f"missing query validation failure should fail: {payload}")
        assert_true(payload.get("error_code") == "TASK_PAYLOAD_INVALID", f"missing query error mismatch: {payload}")
        assert_true(payload.get("current_step") == "payload_validation", f"missing query step mismatch: {payload}")
        assert_true(payload.get("window_probe", {}).get("skipped") is True, f"missing query probe mismatch: {payload}")
        assert_true(payload.get("wechat_ui_action_attempted") is False, f"missing query must not touch WeChat UI: {payload}")
        assert_true(
            any(error.get("field") == "phone_or_wechat" for error in payload.get("validation_errors") or []),
            f"missing query validation error mismatch: {payload}",
        )


def test_add_friend_flow_events_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import make_step_event
    from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import (
        add_friend_entry_click_events_from_payload,
        add_friend_query_search_events_from_result,
    )

    target = {
        "name": "invite_confirm_button",
        "strategy": "window_region_geometry_fallback",
        "region": "invite_form.confirm",
        "bounds": [108, 748, 234, 817],
        "point": [171, 782],
        "confidence": 0.62,
    }
    payload = {
        "state": "add_friend_entry_click_plan",
        "before": {
            "screenshot_path": "before.png",
            "annotated_path": "before_annotated.png",
            "capture_mode": "screen_visible",
            "ocr_items": [{"text": "添加朋友"}],
            "planned_targets": [{"name": "plus_entry", "point": [350, 70]}],
            "popup_detection": {"detected": True},
        },
        "menu_click": {
            "clicked": True,
            "target": {"name": "add_friend_menu_entry", "point": [320, 150]},
            "screenshot_path": "menu.png",
            "annotated_path": "menu_annotated.png",
        },
        "query_search": {
            "ok": True,
            "state": "invite_sent",
            "query": "17368746889",
            "task_status": "completed",
            "result_code": "invite_sent",
            "current_step": "task_completed",
            "page": {
                "screenshot_path": "page.png",
                "annotated_path": "page_annotated.png",
                "ocr_items": [{"text": "搜索手机号/微信号"}],
                "targets": [{"name": "add_friend_search_input", "point": [180, 88]}],
                "input_empty_before_clear": {"ok": False, "digits": ["17756658083"]},
            },
            "clear_result": {"ok": True, "method": "ctrl_a_backspace_delete"},
            "clear_verify": {
                "screenshot_path": "clear.png",
                "annotated_path": "clear_annotated.png",
                "ocr_items": [{"text": "搜索手机号/微信号"}],
                "verify": {"ok": True, "placeholder_visible": True, "digits": []},
            },
            "input_attempts": [
                {
                    "attempt": 1,
                    "screenshot_path": "input.png",
                    "annotated_path": "input_annotated.png",
                    "verify": {"ok": True, "query": "17368746889"},
                }
            ],
            "result": {
                "screenshot_path": "result.png",
                "annotated_path": "result_annotated.png",
                "ocr_items": [{"text": "添加到通讯录"}],
            },
            "add_contact_result": {
                "state": "invite_sent",
                "task_status": "completed",
                "result_code": "invite_sent",
                "current_step": "task_completed",
                "before": {
                    "screenshot_path": "contact_before.png",
                    "annotated_path": "contact_before_annotated.png",
                    "ocr_items": [{"text": "添加到通讯录"}],
                    "targets": [{"name": "add_contact_entry_button", "point": [260, 320]}],
                },
                "after": {
                    "screenshot_path": "contact_after.png",
                    "annotated_path": "contact_after_annotated.png",
                    "ocr_items": [{"text": "申请添加朋友"}],
                    "targets": [target],
                },
                "invite_form": {
                    "state": "invite_sent",
                    "task_status": "completed",
                    "result_code": "invite_sent",
                    "current_step": "task_completed",
                    "verify_message": "我是车金二手车张伟",
                    "remark_name": "客户-CJ8K2P",
                    "remark_code": "CJ8K2P",
                    "remark_code_valid": True,
                    "before": {
                        "screenshot_path": "invite_before.png",
                        "annotated_path": "invite_before_annotated.png",
                        "ocr_items": [{"text": "申请添加朋友"}],
                        "targets": [target],
                    },
                    "filled": {
                        "screenshot_path": "invite_filled.png",
                        "annotated_path": "invite_filled_annotated.png",
                        "ocr_items": [{"text": "客户-CJ8K2P"}],
                        "targets": [target],
                    },
                    "after": {
                        "screenshot_path": "invite_after.png",
                        "annotated_path": "invite_after_annotated.png",
                        "ocr_items": [{"text": "等待验证"}],
                        "final_status": {"task_status": "completed", "result_code": "invite_sent"},
                    },
                },
            },
        },
        "after": {
            "screenshot_path": "after.png",
            "annotated_path": "after_annotated.png",
            "ocr_items": [{"text": "添加朋友"}],
            "planned_targets": [{"name": "add_friend_menu_entry", "point": [320, 150]}],
            "popup_detection": {"detected": True},
        },
    }
    events = add_friend_entry_click_events_from_payload(
        payload,
        existing_events=[
            make_step_event(
                step_id="payload_validation",
                title="字段契约校验",
                status="completed",
                state_before="task_received",
                state_after="payload_valid",
                result={"ok": True},
            )
        ],
    )
    event_ids = [event.get("step_id") for event in events]
    expected = {
        "payload_validation",
        "entry_before_capture",
        "add_friend_menu_click",
        "query_search_page",
        "query_search_input_clear_verify",
        "query_input_verify_attempt_1",
        "query_search_result",
        "add_contact_entry_before_click",
        "add_contact_entry_after_click",
        "invite_form_before_fill",
        "invite_form_after_fill_before_confirm",
        "invite_confirm_after_click",
    }
    assert_true(expected.issubset(set(event_ids)), f"flow events missing ids: {event_ids}")
    assert_true("final_popup_detection" not in event_ids, f"full add_friend flow must not append stale final popup event: {event_ids}")
    confirm_event = next(event for event in events if event.get("step_id") == "invite_confirm_after_click")
    assert_true(confirm_event.get("result", {}).get("result_code") == "invite_sent", f"confirm result mismatch: {confirm_event}")
    assert_true("already_friend" not in json.dumps(confirm_event, ensure_ascii=False), f"confirm event must not emit already_friend: {confirm_event}")
    invite_before = next(event for event in events if event.get("step_id") == "invite_form_before_fill")
    assert_true(invite_before.get("selected_target", {}).get("strategy") == "window_region_geometry_fallback", f"locator metadata missing: {invite_before}")
    native_query_events = add_friend_query_search_events_from_result(payload["query_search"])
    native_query_ids = [event.get("step_id") for event in native_query_events]
    assert_true("query_search_page" in native_query_ids, f"native query events missing search page: {native_query_ids}")
    assert_true("query_search_input_clear_verify" in native_query_ids, f"native query events missing clear verify: {native_query_ids}")
    assert_true("add_contact_entry_before_click" in native_query_ids, f"native query events missing add-contact: {native_query_ids}")
    assert_true("invite_confirm_after_click" in native_query_ids, f"native query events missing invite confirm: {native_query_ids}")


def test_add_friend_flow_context_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_flow_context import AddFriendFlowContext
    from apps.wechat_ai_customer_service.adapters.add_friend_routes import ADD_FRIEND_MAIN_ROUTE

    with tempfile.TemporaryDirectory() as tmpdir:
        flow = AddFriendFlowContext(
            project_root=tmpdir,
            route=ADD_FRIEND_MAIN_ROUTE,
            artifact_dir=Path(tmpdir) / "run",
        )
        flow.add_timing("payload_validation", seconds=0.123, stage="contract")
        flow.add_event(
            step_id="payload_validation",
            title="字段契约校验",
            status="failed",
            state_before="task_received",
            state_after="task_payload_invalid",
            result={"ok": False, "error_code": "TASK_PAYLOAD_INVALID", "wechat_ui_action_attempted": False},
        )

        def write_report(output_dir: Path, payload: dict[str, object]) -> str:
            report_path = output_dir / "add_friend_entry_click_review.html"
            report_path.write_text("ok", encoding="utf-8")
            return str(report_path)

        payload = flow.finalize_payload(
            {
                "ok": False,
                "state": "task_payload_invalid",
                "task_status": "failed",
                "error_code": "TASK_PAYLOAD_INVALID",
                "validation_errors": [{"field": "verify_message", "code": "REQUIRED"}],
                "plan_path": str(flow.plan_path),
            },
            report_writer=write_report,
        )
        assert_true(flow.plan_path.exists(), f"flow context should write plan json: {flow.plan_path}")
        assert_true(Path(str(payload.get("review_path") or "")).exists(), f"flow context should write review: {payload}")
        assert_true(payload.get("timings") == [{"name": "payload_validation", "seconds": 0.123, "stage": "contract"}], f"timings mismatch: {payload}")
        events = payload.get("diagnostic_events")
        native_events = payload.get("native_diagnostic_events")
        assert_true(isinstance(native_events, list) and len(native_events) == 1, f"native diagnostic events missing: {payload}")
        assert_true(isinstance(events, list) and len(events) == 1, f"diagnostic events missing: {payload}")
        assert_true(events[0].get("step_id") == "payload_validation", f"event id mismatch: {events}")
        saved = json.loads(flow.plan_path.read_text(encoding="utf-8"))
        assert_true(saved.get("native_diagnostic_events") == native_events, f"saved plan should include native events: {saved}")
        assert_true(saved.get("diagnostic_events") == events, f"saved plan should include finalized events: {saved}")


def test_add_friend_already_friend_terminal_event_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import add_friend_query_search_events_from_result

    events = add_friend_query_search_events_from_result(
        {
            "ok": True,
            "state": "already_friend",
            "task_status": "completed",
            "result_code": "already_friend",
            "current_step": "searching_contact",
            "result": {
                "screenshot_path": "profile.png",
                "annotated_path": "profile_annotated.png",
                "ocr_items": [{"text": "发消息"}],
            },
            "add_contact_result": {
                "ok": True,
                "state": "already_friend",
                "task_status": "completed",
                "result_code": "already_friend",
                "current_step": "searching_contact",
                "screenshot_path": "profile.png",
                "annotated_path": "profile_annotated.png",
                "ocr_items": [{"text": "发消息"}],
                "result_basis": "search_result_profile_has_message_actions",
            },
        }
    )
    ids = [event.get("step_id") for event in events]
    assert_true("add_contact_search_terminal" in ids, f"already_friend should be a terminal event: {ids}")
    assert_true("add_contact_search_failure" not in ids, f"already_friend must not be labeled failure: {ids}")
    terminal = next(event for event in events if event.get("step_id") == "add_contact_search_terminal")
    assert_true(terminal.get("status") == "completed", f"already_friend terminal status mismatch: {terminal}")
    assert_true(terminal.get("result", {}).get("result_code") == "already_friend", f"already_friend result mismatch: {terminal}")


def test_sidecar_uses_flow_context_for_entry_click() -> None:
    sidecar = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
    ).read_text(encoding="utf-8")
    add_friend_windows_source = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py"
    ).read_text(encoding="utf-8")
    entry_click_source = sidecar + "\n" + add_friend_windows_source
    flow_source = (PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/add_friend_flow.py").read_text(
        encoding="utf-8"
    )
    assert_true("StepEventRecorder" not in sidecar, "sidecar should use AddFriendFlowContext instead of direct StepEventRecorder")
    assert_true(
        "win32_ocr_add_friend_windows.add_friend_entry_click_plan_payload(" in sidecar,
        "sidecar should preserve a facade and delegate add_friend entry-click work to the Windows adapter",
    )
    assert_true(
        "run_add_friend_entry_click_plan_flow(" in entry_click_source,
        "sidecar/add_friend_windows should delegate entry-click orchestration to add_friend_flow",
    )
    assert_true(
        "def run_add_friend_entry_click_plan_flow(" in flow_source,
        "add_friend_flow should own the entry-click orchestration function",
    )
    assert_true(
        "flow = AddFriendFlowContext(" in flow_source,
        "add_friend_flow should create the flow context for entry-click",
    )
    assert_true(
        "flow = AddFriendFlowContext(" not in sidecar.split("def add_friend_entry_click_plan_payload", 1)[-1].split("def add_friend_failure_payload", 1)[0],
        "sidecar facade must not recreate entry-click flow context",
    )
    assert_true("class AddFriendOpsProtocol(Protocol):" in flow_source, "add_friend_flow should declare required sidecar ops")
    assert_true(
        "def run_add_friend_entry_click_plan_flow(\n    ops: AddFriendOpsProtocol," in flow_source,
        "entry-click flow should type ops with AddFriendOpsProtocol",
    )
    assert_true("def _build_entry_click_payload(" in flow_source, "entry-click flow should centralize payload assembly")
    assert_true(
        flow_source.count("_build_entry_click_payload(") == 5,
        "entry-click flow should have one helper definition and four branch calls",
    )
    assert_true('"window_layout_calibration"' in flow_source, "entry-click flow should expose layout calibration as a first-class step")
    assert_true("ERROR_PLUS_ENTRY_NOT_FOUND" in flow_source, "missing visual plus icon should be a first-class failure")
    assert_true("ERROR_PLUS_ENTRY_POPUP_NOT_DETECTED" in flow_source, "missing popup after plus click should be a first-class failure")
    assert_true("ERROR_ADD_FRIEND_MENU_CLICK_FAILED" in flow_source, "menu click failure should not be reported as plus popup failure")
    assert_true("human_window_image_click_in_bounds(" in flow_source, "plus click must clamp to selected target bounds")
    assert_true(
        "add_friend_window_layout_calibration_annotated.png" in flow_source,
        "layout calibration should have its own region annotation artifact",
    )
    assert_true(
        '"entry_before_capture"' in flow_source and '"annotated": before_annotated' in flow_source,
        "entry-before capture should keep the plus-entry target annotation artifact",
    )
    assert_true(
        "WECHAT_WIN32_OCR_PLUS_ENTRY_CLICK_MAX_ATTEMPTS" not in flow_source,
        "plus entry click should not retry the same candidate point",
    )


def test_sidecar_uses_add_friend_payload_builders() -> None:
    sidecar = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
    ).read_text(encoding="utf-8")
    add_friend_windows = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py"
    ).read_text(encoding="utf-8")
    implementation_source = sidecar + "\n" + add_friend_windows
    for token in [
        "add_friend_after_confirm_payload(",
        "add_friend_phone_not_found_payload(",
        "add_friend_add_contact_entry_not_found_payload(",
        "add_friend_invite_form_window_not_found_payload(",
    ]:
        assert_true(
            token in implementation_source,
            f"sidecar/add_friend_windows should route task result through payload builder: {token}",
        )
    assert_true(
        "add_friend_search_not_found_result(" not in implementation_source,
        "sidecar/add_friend_windows should not directly build search-not-found task result",
    )


def test_add_friend_uses_shared_operator_guard_module() -> None:
    guard_source = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/add_friend_operator_guard.py"
    ).read_text(encoding="utf-8")
    rpa_guard_source = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/rpa_operator_guard.py"
    ).read_text(encoding="utf-8")
    rpa_guard_script = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/scripts/run_rpa_operator_guard.py"
    ).read_text(encoding="utf-8")
    sidecar = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
    ).read_text(encoding="utf-8")
    add_friend_windows = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py"
    ).read_text(encoding="utf-8")
    implementation_source = sidecar + "\n" + add_friend_windows
    flow_source = (PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/add_friend_flow.py").read_text(
        encoding="utf-8"
    )
    assert_true("rpa_operator_guard" in guard_source, "add_friend guard must delegate to the shared RPA operator guard")
    assert_true("start_rpa_operator_guard(" in guard_source, "add_friend start wrapper should call shared guard start")
    assert_true("stop_rpa_operator_guard(" in guard_source, "add_friend stop wrapper should call shared guard stop")
    assert_true("rpa_operator_guard_checkpoint(" in guard_source, "add_friend checkpoint wrapper should call shared checkpoint")
    assert_true("subprocess.Popen" not in guard_source, "add_friend adapter must not own guard process launching")
    assert_true("SetWindowsHookExW" not in guard_source, "add_friend adapter must not duplicate keyboard/mouse hook logic")
    assert_true("run_rpa_operator_guard.py" in rpa_guard_source, "shared guard must reuse the floating-ball operator guard script")
    assert_true("--block-manual-input" in rpa_guard_source, "operator guard should support locking/blocking manual keyboard and mouse input")
    assert_true("block_manual_input" in rpa_guard_source, "operator guard settings should expose the manual-input lock flag")
    assert_true("--floating-indicator" in rpa_guard_source, "shared guard should start the floating indicator")
    assert_true("mode == \"stopped\"" in rpa_guard_source, "shared checkpoint should honor stop requests")
    assert_true("mode != \"paused\"" in rpa_guard_source, "shared checkpoint should honor pause/resume requests")
    assert_true("--block-manual-input" in rpa_guard_script, "guard runner must expose block-manual-input")
    assert_true("--floating-indicator" in rpa_guard_script, "guard runner must expose floating-indicator")
    assert_true("toggle_pause" in rpa_guard_script, "guard runner must support pause/resume")
    assert_true("stop" in rpa_guard_script, "guard runner must support stop")
    assert_true("start_add_friend_operator_guard(" in implementation_source, "add_friend Windows path should start operator guard before click flow")
    assert_true("stop_add_friend_operator_guard(" in implementation_source, "add_friend Windows path should stop operator guard after click flow")
    assert_true("OPERATOR_GUARD_NOT_READY" in implementation_source, "operator guard failure should be a first-class add_friend failure")
    assert_true("add_friend_operator_guard_checkpoint(" in flow_source, "flow should honor floating-ball pause/stop checkpoints")
    assert_true("GetCursorPos" not in flow_source, "flow should not implement a separate mouse-idle guard")


def test_add_friend_operator_guard_compat_wrapper_calls_shared_module() -> None:
    import apps.wechat_ai_customer_service.adapters.add_friend_operator_guard as compat

    expected_functions = [
        "add_friend_operator_guard_settings",
        "add_friend_operator_guard_dir",
        "add_friend_operator_guard_paths",
        "start_add_friend_operator_guard",
        "stop_add_friend_operator_guard",
        "add_friend_operator_guard_checkpoint",
    ]
    for name in expected_functions:
        assert_true(callable(getattr(compat, name, None)), f"compat wrapper must expose add_friend API: {name}")

    calls: list[tuple[str, object]] = []
    original_start = compat.start_rpa_operator_guard
    original_stop = compat.stop_rpa_operator_guard
    original_checkpoint = compat.rpa_operator_guard_checkpoint
    try:
        compat.start_rpa_operator_guard = lambda **kwargs: calls.append(("start", kwargs)) or {"ok": True}
        compat.stop_rpa_operator_guard = lambda guard, **kwargs: calls.append(("stop", {"guard": guard, **kwargs})) or {"ok": True}
        compat.rpa_operator_guard_checkpoint = lambda **kwargs: calls.append(("checkpoint", kwargs)) or {"ok": True}

        compat.start_add_friend_operator_guard(route="add-friend-entry-click-plan-windows", artifact_dir="artifact-dir")
        compat.stop_add_friend_operator_guard({"enabled": True}, reason="finished")
        compat.add_friend_operator_guard_checkpoint(reason="pause:add_friend")
    finally:
        compat.start_rpa_operator_guard = original_start
        compat.stop_rpa_operator_guard = original_stop
        compat.rpa_operator_guard_checkpoint = original_checkpoint

    assert_true(
        calls[0] == (
            "start",
            {"operation": "add-friend-entry-click-plan-windows", "artifact_dir": "artifact-dir"},
        ),
        f"start wrapper should call shared guard start: {calls}",
    )
    assert_true(
        calls[1] == ("stop", {"guard": {"enabled": True}, "reason": "finished"}),
        f"stop wrapper should call shared guard stop: {calls}",
    )
    assert_true(
        calls[2] == ("checkpoint", {"reason": "pause:add_friend"}),
        f"checkpoint wrapper should call shared guard checkpoint: {calls}",
    )


def test_c2_search_by_remark_code_has_no_add_friend_operator_guard_report_fields() -> None:
    sidecar_source = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
    ).read_text(encoding="utf-8")
    function_source = sidecar_source.split("def open_chat_by_remark_code_search(", 1)[1].split(
        "\ndef message_read_payload(",
        1,
    )[0]
    assert_true('"target_mode": "search_by_remark_code"' in function_source, "C2 targeting must keep search_by_remark_code mode")
    for forbidden in [
        "start_add_friend_operator_guard",
        "stop_add_friend_operator_guard",
        "add_friend_operator_guard_checkpoint",
        '"operator_guard"',
        "'operator_guard'",
        "operator_guard_release",
    ]:
        assert_true(
            forbidden not in function_source,
            f"C2 search_by_remark_code must not introduce add_friend operator guard report field: {forbidden}",
        )


def test_add_friend_preflight_blocks_unready_window() -> None:
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar_mod

    originals = {
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_capture_geometry": sidecar_mod.validate_capture_geometry,
        "run_add_friend_entry_click_plan_flow": sidecar_mod.run_add_friend_entry_click_plan_flow,
        "write_add_friend_entry_click_review": sidecar_mod.write_add_friend_entry_click_review,
    }
    calls = {"flow": 0}
    try:
        sidecar_mod.get_window_geometry = lambda _hwnd: {"left": 775, "top": 331, "right": 1143, "bottom": 815, "width": 368, "height": 484}
        sidecar_mod.validate_capture_geometry = lambda geometry: {"ok": False, "reason": "window_too_small_for_capture", "geometry": geometry}
        sidecar_mod.run_add_friend_entry_click_plan_flow = lambda *args, **kwargs: calls.__setitem__("flow", calls["flow"] + 1) or {"ok": True}
        sidecar_mod.write_add_friend_entry_click_review = lambda output_dir, payload: str(Path(output_dir) / "review.html")
        payload = sidecar_mod.add_friend_entry_click_plan_payload(
            1001,
            {"quick_login": {"detected": True, "reason": "quick_login_detected_no_auto_enter"}},
            phone="17368746889",
            verify_message="你好",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            artifact_dir=str(PROJECT_ROOT / "runtime" / "add_friend_preflight_test"),
        )
        assert_true(payload.get("ok") is False, f"unready window should fail preflight: {payload}")
        assert_true(payload.get("state") == "wechat_window_not_ready", f"unexpected state: {payload}")
        assert_true(payload.get("error_code") == "WECHAT_WINDOW_NOT_READY", f"unexpected error: {payload}")
        assert_true(payload.get("current_step") == "preflight_window_ready", f"unexpected current step: {payload}")
        assert_true(calls["flow"] == 0, f"unready window must not enter click flow: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_add_friend_requires_operator_guard_before_click_flow() -> None:
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar_mod

    originals = {
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_capture_geometry": sidecar_mod.validate_capture_geometry,
        "add_friend_pre_click_main_window_readiness": sidecar_mod.add_friend_pre_click_main_window_readiness,
        "start_add_friend_operator_guard": sidecar_mod.start_add_friend_operator_guard,
        "run_add_friend_entry_click_plan_flow": sidecar_mod.run_add_friend_entry_click_plan_flow,
        "write_add_friend_entry_click_review": sidecar_mod.write_add_friend_entry_click_review,
    }
    calls = {"flow": 0}
    try:
        sidecar_mod.get_window_geometry = lambda _hwnd: {"left": 0, "top": 0, "right": 981, "bottom": 860, "width": 981, "height": 860}
        sidecar_mod.validate_capture_geometry = lambda geometry: {"ok": True, "geometry": geometry}
        sidecar_mod.add_friend_pre_click_main_window_readiness = lambda *_args, **_kwargs: {
            "ok": True,
            "state": "wechat_main_surface_ready",
            "focus_guard": {"ok": True, "reason": "foreground_matches_target"},
            "surface_readiness": {"ok": True, "main_surface": {"ok": True}},
        }
        sidecar_mod.start_add_friend_operator_guard = lambda **_kwargs: {
            "ok": False,
            "enabled": True,
            "reason": "guard_hook_not_ready",
        }
        sidecar_mod.run_add_friend_entry_click_plan_flow = lambda *args, **kwargs: calls.__setitem__("flow", calls["flow"] + 1) or {"ok": True}
        sidecar_mod.write_add_friend_entry_click_review = lambda output_dir, payload: str(Path(output_dir) / "review.html")
        payload = sidecar_mod.add_friend_entry_click_plan_payload(
            1001,
            {"quick_login": {"detected": False}},
            phone="17368746889",
            verify_message="你好",
            remark_name="客户-CJ8K2P",
            remark_code="CJ8K2P",
            artifact_dir=str(PROJECT_ROOT / "runtime" / "add_friend_operator_guard_test"),
        )
        assert_true(payload.get("ok") is False, f"operator guard failure should block add_friend: {payload}")
        assert_true(payload.get("state") == "operator_guard_not_ready", f"unexpected state: {payload}")
        assert_true(payload.get("error_code") == "OPERATOR_GUARD_NOT_READY", f"unexpected error: {payload}")
        assert_true(payload.get("current_step") == "operator_guard_ready", f"unexpected current step: {payload}")
        assert_true(calls["flow"] == 0, f"click flow must not run before operator guard is ready: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_add_friend_formal_preclick_requires_foreground_and_main_surface() -> None:
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (
        add_friend_focus_guard_ready,
        add_friend_pre_click_readiness_decision,
    )

    not_foreground = add_friend_pre_click_readiness_decision(
        focus_guard={"ok": False, "reason": "foreground_not_wechat_target"},
        surface_readiness={"ok": True, "state": "wechat_main_surface_ready"},
    )
    assert_true(not_foreground.get("ok") is False, f"foreground mismatch should block: {not_foreground}")
    assert_true(not_foreground.get("state") == "wechat_window_not_foreground", f"unexpected state: {not_foreground}")
    assert_true(not_foreground.get("no_clicks_performed") is True, f"must be a no-click block: {not_foreground}")

    degraded_focus = add_friend_focus_guard_ready({"ok": True, "reason": "foreground_guard_unavailable"})
    assert_true(degraded_focus.get("ok") is False, f"formal add_friend must not accept degraded focus: {degraded_focus}")

    wrong_surface = add_friend_pre_click_readiness_decision(
        focus_guard={"ok": True, "reason": "foreground_matches_target"},
        surface_readiness={
            "ok": False,
            "state": "wechat_main_surface_not_ready",
            "error_code": "WECHAT_WINDOW_NOT_READY",
            "reason": "sidebar_search_anchor_missing_or_non_wechat_content",
        },
    )
    assert_true(wrong_surface.get("ok") is False, f"wrong surface should block: {wrong_surface}")
    assert_true(wrong_surface.get("state") == "wechat_main_surface_not_ready", f"unexpected surface block: {wrong_surface}")

    ready = add_friend_pre_click_readiness_decision(
        focus_guard={"ok": True, "reason": "foreground_root_matches_target"},
        surface_readiness={"ok": True, "state": "wechat_main_surface_ready"},
    )
    assert_true(ready.get("ok") is True, f"foreground root + main surface should pass: {ready}")


def test_add_friend_calibration_mode_contract() -> None:
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar_mod

    sidecar = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py"
    ).read_text(encoding="utf-8")
    add_friend_windows = (
        PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py"
    ).read_text(encoding="utf-8")
    implementation_source = sidecar + "\n" + add_friend_windows
    assert_true("--calibration-only" in sidecar, "sidecar CLI must expose add_friend calibration-only mode")
    assert_true("calibration_only=bool" in sidecar, "run_action should pass calibration_only into add_friend payload")
    validation_call = implementation_source.split("validation = validate_add_friend_entry_click_contract(", 1)[-1].split(")", 1)[0]
    assert_true("calibration_only" not in validation_call, "calibration flag must not be passed to field contract validation")
    assert_true("start_add_friend_operator_guard(" in implementation_source, "normal flow should still start the floating-ball guard")
    calibration_section = add_friend_windows.split("def add_friend_calibration_payload", 1)[-1].split("def click_add_friend_ocr_item", 1)[0]
    assert_true("human_window_image_click" not in calibration_section, "calibration payload must not click")
    assert_true("paste_invite_form_text" not in calibration_section, "calibration payload must not type/paste")
    assert_true("no_clicks_performed" in calibration_section, "calibration payload must mark no-click behavior")
    assert_true("add_friend_device_profile(" in calibration_section, "calibration should include device profile")
    assert_true(
        '"ok": calibration_ready' in calibration_section or "'ok': calibration_ready" in calibration_section,
        "calibration ok must follow readiness, not unconditional success",
    )

    argv = sidecar_mod.args_for_daemon_request(
        {
            "action": "add-friend-entry-click-plan-windows",
            "phone": "17756658083",
            "verify_message": "你好",
            "remark_name": "客户-CJ8K2P",
            "remark_code": "CJ8K2P",
            "calibration_only": True,
        }
    )
    assert_true("--calibration-only" in argv, f"daemon argv should pass calibration flag: {argv}")


def test_entry_click_task_outcome_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import add_friend_entry_click_task_outcome

    not_run = add_friend_entry_click_task_outcome(
        {
            "ok": False,
            "state": "query_not_run",
            "reason": "empty_query_or_menu_click_failed_or_dialog_hwnd_missing",
        }
    )
    assert_true(not_run.get("ok") is False, f"query_not_run must fail at task envelope: {not_run}")
    assert_true(not_run.get("task_status") == "failed", f"query_not_run task status mismatch: {not_run}")
    assert_true(not_run.get("current_step") == "query_not_run", f"query_not_run step mismatch: {not_run}")
    assert_true(not_run.get("server_report_payload", {}).get("task.status") == "failed", f"query_not_run report mismatch: {not_run}")

    invite_sent = add_friend_entry_click_task_outcome(
        {
            "ok": True,
            "task_status": "completed",
            "result_code": "invite_sent",
            "current_step": "task_completed",
            "server_report_payload": {
                "task.status": "completed",
                "task.result_code": "invite_sent",
                "task.current_step": "task_completed",
            },
        }
    )
    assert_true(invite_sent.get("ok") is True, f"invite_sent should be ok: {invite_sent}")
    assert_true(invite_sent.get("task_status") == "completed", f"invite_sent task status mismatch: {invite_sent}")
    assert_true(invite_sent.get("result_code") == "invite_sent", f"invite_sent result mismatch: {invite_sent}")


def test_add_friend_actions_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_actions import (
        ACTION_COMPOSITE_INPUT,
        action_target_metadata,
        make_action_result,
        redacted_text_metadata,
    )

    target = {
        "name": "invite_remark_input",
        "label": "备注 input",
        "strategy": "window_region_geometry_fallback",
        "region": "invite_form.remark_name",
        "point": [128, 300],
        "bounds": [40, 265, 428, 335],
        "confidence": 0.62,
    }
    meta = action_target_metadata(target)
    assert_true(meta["point"] == [128, 300], f"action target point mismatch: {meta}")
    assert_true(meta["bounds"] == [40, 265, 428, 335], f"action target bounds mismatch: {meta}")
    redacted = redacted_text_metadata("客户-CJ8K2P")
    assert_true(redacted == {"text_length": 9, "is_empty": False}, f"redacted text metadata mismatch: {redacted}")
    action = make_action_result(
        action_id="invite_remark",
        action_type=ACTION_COMPOSITE_INPUT,
        status="ok",
        method="click_ctrl_a_backspace_clipboard_paste",
        target=target,
        text="客户-CJ8K2P",
        result={"ok": True},
    )
    assert_true(action.get("status") == "completed", f"action status mismatch: {action}")
    assert_true(action.get("input", {}).get("text_length") == 9, f"action input length mismatch: {action}")
    assert_true("客户-CJ8K2P" not in json.dumps(action, ensure_ascii=False), f"action result must not expose raw text: {action}")


def test_invite_form_locator_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_layout import invite_form_field_verification
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import add_friend_invite_form_targets

    targets = add_friend_invite_form_targets((468, 834))
    for name in ["invite_greeting_textarea", "invite_remark_input", "invite_confirm_button"]:
        target = targets.get(name)
        assert_true(isinstance(target, dict), f"missing invite form locator: {name}")
        for field in [
            "strategy",
            "region",
            "candidates",
            "selected_reason",
            "bounds",
            "point",
            "confidence",
            "fallback_used",
            "fallback_reason",
            "locator",
        ]:
            assert_true(field in target, f"{name} locator missing field {field}: {target}")
        assert_true(target.get("strategy") == "window_region_geometry_fallback", f"{name} should use explicit fallback strategy: {target}")
        assert_true(target.get("fallback_used") is True, f"{name} should expose fallback usage: {target}")
        assert_true(isinstance(target.get("candidates"), list) and target.get("candidates"), f"{name} should expose candidates: {target}")
        assert_true(isinstance(target.get("bounds"), list) and len(target["bounds"]) == 4, f"{name} bounds invalid: {target}")
        assert_true(isinstance(target.get("point"), list) and len(target["point"]) == 2, f"{name} point invalid: {target}")
        assert_true(target.get("x") == target["point"][0] and target.get("y") == target["point"][1], f"{name} legacy x/y mismatch: {target}")
        assert_true(target.get("click_bounds") == target.get("bounds"), f"{name} legacy click_bounds mismatch: {target}")

    def ocr_item(text: str, left: int, top: int, right: int, bottom: int, confidence: float = 0.91) -> dict[str, object]:
        return {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": int((left + right) / 2),
            "center_y": int((top + bottom) / 2),
            "confidence": confidence,
        }

    semantic_targets = add_friend_invite_form_targets(
        (468, 834),
        [
            ocr_item("发送添加朋友申请", 38, 82, 182, 108),
            ocr_item("备注", 38, 276, 82, 304),
            ocr_item("确定", 112, 770, 166, 802),
        ],
    )
    assert_true(
        semantic_targets["invite_greeting_textarea"].get("strategy") == "semantic_ocr_anchor_locator",
        f"greeting should use semantic locator: {semantic_targets}",
    )
    assert_true(
        semantic_targets["invite_remark_input"].get("fallback_used") is False,
        f"remark semantic target should not be fallback: {semantic_targets}",
    )
    assert_true(
        semantic_targets["invite_confirm_button"].get("source") == "ocr_invite_confirm_button_anchor",
        f"confirm should use OCR anchor: {semantic_targets}",
    )
    field_check = invite_form_field_verification(
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P",
        remark_code="CJ8K2P",
        ocr_items=[ocr_item("我是车金二手车张伟", 40, 122, 260, 152), ocr_item("客户-CJ8K2P", 40, 330, 180, 358)],
    )
    assert_true(field_check.get("ok") is True, f"field verification should pass visible OCR text: {field_check}")


def test_invite_form_input_click_failure_blocks_keyboard_actions() -> None:
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar

    calls: list[object] = []
    original_click = sidecar.human_window_image_click_in_bounds
    original_pause = sidecar.add_friend_paced_pause
    original_hotkey = sidecar.hotkey
    original_key_press = sidecar.key_press
    original_clipboard_copy = sidecar.clipboard_copy
    try:
        sidecar.human_window_image_click_in_bounds = lambda *_args, **_kwargs: {
            "ok": False,
            "reason": "simulated_focus_click_failed",
        }
        sidecar.add_friend_paced_pause = lambda *_args, **_kwargs: 0.0
        sidecar.hotkey = lambda *args, **_kwargs: calls.append(("hotkey", args))
        sidecar.key_press = lambda *args, **_kwargs: calls.append(("key_press", args))
        sidecar.clipboard_copy = lambda text: calls.append(("clipboard_copy", text))
        target = {
            "name": "invite_remark_input",
            "x": 128,
            "y": 300,
            "click_bounds": [40, 265, 428, 335],
        }
        result = sidecar.paste_invite_form_text(1001, target, "客户-CJ8K2P", action_name="invite_remark")
    finally:
        sidecar.human_window_image_click_in_bounds = original_click
        sidecar.add_friend_paced_pause = original_pause
        sidecar.hotkey = original_hotkey
        sidecar.key_press = original_key_press
        sidecar.clipboard_copy = original_clipboard_copy
    assert_true(result.get("ok") is False, f"focus click failure should fail: {result}")
    assert_true(result.get("reason") == "field_click_failed", f"failure should be explicit: {result}")
    assert_true(calls == [], f"keyboard/clipboard actions must not run after failed field click: {calls}")


def test_invite_form_field_verification_blocks_confirm_click() -> None:
    source = (PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py").read_text(encoding="utf-8")
    section = source.split("def fill_add_friend_invite_form_and_confirm", 1)[1].split("def type_add_friend_query_like_human_for_entry", 1)[0]
    field_check_index = section.find('if not field_verification.get("ok")')
    if field_check_index < 0:
        field_check_index = section.find("if not field_verification.get('ok')")
    confirm_click_index = section.find('action_name="invite_confirm_button_click"')
    if confirm_click_index < 0:
        confirm_click_index = section.find("action_name='invite_confirm_button_click'")
    assert_true(field_check_index >= 0, "invite form fill must hard-gate on field_verification.ok")
    assert_true(confirm_click_index >= 0, "invite confirm click section missing")
    assert_true(field_check_index < confirm_click_index, "field verification gate must run before confirm click")
    assert_true("INVITE_FIELD_VERIFICATION_FAILED" in section, "field verification failure must use explicit error code")
    assert_true(
        '"confirm": {"ok": False, "skipped": True' in section
        or "'confirm': {'ok': False, 'skipped': True" in section,
        "failed field verification must skip confirm click",
    )


def test_query_verify_invalid_dialog_handle_returns_structured_failure() -> None:
    source = (PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py").read_text(encoding="utf-8")
    section = source.split("def input_add_friend_query_and_search", 1)[1].split("def write_add_friend_entry_click_review", 1)[0]
    assert_true("dialog_handle_invalid_during_query_verify" in section, "query verify invalid hwnd must not traceback")
    assert_true(
        '"state": "dialog_handle_invalid"' in section or "'state': 'dialog_handle_invalid'" in section,
        "invalid dialog hwnd should become a structured failed state",
    )
    assert_true(
        '"current_step": "query_input_verify"' in section or "'current_step': 'query_input_verify'" in section,
        "invalid dialog hwnd should report the query verify step",
    )
    assert_true("add_friend_server_report_payload(" in section, "invalid dialog hwnd should keep server report payload")


def test_add_friend_primary_locator_contract() -> None:
    from PIL import Image, ImageDraw

    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (
        add_friend_menu_candidate_targets,
        add_friend_plus_entry_target,
        add_friend_plus_entry_safe_bounds,
        add_friend_query_visible_in_items,
        add_friend_search_result_add_contact_target,
        find_add_friend_page_search_targets,
    )

    def ocr_item(text: str, left: int, top: int, right: int, bottom: int) -> dict[str, object]:
        return {
            "text": text,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "center_x": int((left + right) / 2),
            "center_y": int((top + bottom) / 2),
            "confidence": 0.91,
        }

    def plus_icon_image() -> Image.Image:
        image = Image.new("RGB", (981, 860), (246, 248, 250))
        draw = ImageDraw.Draw(image)
        center_x, center_y = 350, 70
        draw.line((center_x - 9, center_y, center_x + 9, center_y), fill=(45, 52, 64), width=3)
        draw.line((center_x, center_y - 9, center_x, center_y + 9), fill=(45, 52, 64), width=3)
        return image

    def plus_icon_image_for_size(width: int, height: int) -> tuple[Image.Image, tuple[int, int], list[int]]:
        image = Image.new("RGB", (width, height), (246, 248, 250))
        bounds = add_friend_plus_entry_safe_bounds((width, height))
        left, top, right, bottom = bounds
        center_x = min(right - 10, max(left + 18, right - 22))
        center_y = int((top + bottom) / 2)
        draw = ImageDraw.Draw(image)
        draw.line((center_x - 9, center_y, center_x + 9, center_y), fill=(45, 52, 64), width=3)
        draw.line((center_x, center_y - 9, center_x, center_y + 9), fill=(45, 52, 64), width=3)
        return image, (center_x, center_y), bounds

    def small_add_friend_image() -> Image.Image:
        image = Image.new("RGB", (468, 520), (245, 246, 248))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((32, 72, 292, 122), radius=8, fill=(235, 238, 242), outline=(214, 220, 228), width=1)
        draw.rounded_rectangle((310, 72, 398, 122), radius=8, fill=(8, 189, 116), outline=(8, 189, 116), width=1)
        return image

    def assert_locator(target: dict[str, object], name: str) -> None:
        for field in [
            "strategy",
            "region",
            "candidates",
            "selected_reason",
            "bounds",
            "point",
            "confidence",
            "fallback_used",
            "fallback_reason",
            "locator",
        ]:
            assert_true(field in target, f"{name} locator missing field {field}: {target}")
        assert_true(target.get("x") == target["point"][0] and target.get("y") == target["point"][1], f"{name} legacy x/y mismatch: {target}")
        assert_true(target.get("click_bounds") == target.get("bounds"), f"{name} click bounds mismatch: {target}")

    plus_target = add_friend_plus_entry_target(
        {"width": 981, "height": 860, "left": 0, "top": 0, "right": 981, "bottom": 860},
        (981, 860),
        [ocr_item("搜索", 112, 60, 154, 82)],
        screenshot=plus_icon_image(),
        route_kind="windows",
    )
    assert_locator(plus_target, "plus_entry")
    assert_true(plus_target.get("strategy") == "sidebar_header_plus_icon_vision_locator", f"plus locator strategy mismatch: {plus_target}")
    assert_true(plus_target.get("source") == "vision_plus_icon", f"plus locator must use visual icon detection: {plus_target}")
    assert_true(plus_target.get("executable") is True, f"visual plus locator should be executable: {plus_target}")
    assert_true(plus_target.get("fallback_used") is False, f"plus locator must not execute fallback clicks: {plus_target}")
    assert_true(len(plus_target.get("candidates") or []) >= 1, f"plus locator should expose visual candidates: {plus_target}")
    assert_true(320 <= int(plus_target.get("x") or 0) <= 370, f"plus locator x outside sidebar toolbar: {plus_target}")
    assert_true(48 <= int(plus_target.get("y") or 0) <= 118, f"plus locator y outside sidebar toolbar: {plus_target}")
    sources = {str(item.get("source") or "") for item in plus_target.get("candidates") or [] if isinstance(item, dict)}
    assert_true("vision_plus_icon" in sources, f"plus locator must expose visual candidate: {plus_target}")
    diagnostic_sources = {str(item.get("source") or "") for item in plus_target.get("diagnostic_references") or [] if isinstance(item, dict)}
    assert_true("diagnostic_windows_current_geometry" in diagnostic_sources, f"plus locator must keep current geometry only as diagnostics: {plus_target}")
    assert_true("diagnostic_windows_1080p_reference_geometry" in diagnostic_sources, f"plus locator must keep reference geometry only as diagnostics: {plus_target}")

    for width, height in [(980, 720), (980, 860), (1225, 816), (1225, 1032), (1470, 1032), (1470, 1290), (2560, 1440)]:
        matrix_image, expected_point, safe_bounds = plus_icon_image_for_size(width, height)
        matrix_target = add_friend_plus_entry_target(
            {"width": width, "height": height, "left": 0, "top": 0, "right": width, "bottom": height},
            (width, height),
            [],
            screenshot=matrix_image,
            route_kind="windows",
        )
        assert_locator(matrix_target, f"plus_entry_{width}x{height}")
        assert_true(matrix_target.get("source") == "vision_plus_icon", f"matrix plus locator must use vision: {matrix_target}")
        assert_true(matrix_target.get("executable") is True, f"matrix plus locator should be executable: {matrix_target}")
        assert_true(matrix_target.get("fallback_used") is False, f"matrix plus locator must not execute fallback: {matrix_target}")
        actual_point = [int(matrix_target.get("x") or 0), int(matrix_target.get("y") or 0)]
        assert_true(
            safe_bounds[0] <= actual_point[0] <= safe_bounds[2] and safe_bounds[1] <= actual_point[1] <= safe_bounds[3],
            f"matrix plus locator outside calibrated safe bounds: {(width, height, actual_point, safe_bounds, matrix_target)}",
        )
        assert_true(
            abs(actual_point[0] - expected_point[0]) <= 8 and abs(actual_point[1] - expected_point[1]) <= 8,
            f"matrix plus locator should match visual anchor: {(width, height, actual_point, expected_point, matrix_target)}",
        )
        matrix_diagnostic_sources = {
            str(item.get("source") or "")
            for item in matrix_target.get("diagnostic_references") or []
            if isinstance(item, dict)
        }
        assert_true("diagnostic_windows_current_geometry" in matrix_diagnostic_sources, f"matrix current geometry diagnostics missing: {matrix_target}")
        assert_true("diagnostic_windows_1080p_reference_geometry" in matrix_diagnostic_sources, f"matrix reference diagnostics missing: {matrix_target}")

    fallback_plus_target = add_friend_plus_entry_target(
        {"width": 981, "height": 860, "left": 0, "top": 0, "right": 981, "bottom": 860},
        (981, 860),
        [],
        route_kind="windows",
    )
    assert_locator(fallback_plus_target, "plus_entry_fallback")
    assert_true(fallback_plus_target.get("source") == "plus_icon_not_found", f"main route must not execute geometry fallback: {fallback_plus_target}")
    assert_true(fallback_plus_target.get("executable") is False, f"missing visual plus must be non-executable: {fallback_plus_target}")
    assert_true(fallback_plus_target.get("fallback_used") is False, f"geometry fallback must not be executable: {fallback_plus_target}")

    for width, height in [(980, 720), (1225, 816), (1470, 1032), (2560, 1440)]:
        blank_target = add_friend_plus_entry_target(
            {"width": width, "height": height, "left": 0, "top": 0, "right": width, "bottom": height},
            (width, height),
            [],
            screenshot=Image.new("RGB", (width, height), (246, 248, 250)),
            route_kind="windows",
        )
        assert_locator(blank_target, f"plus_entry_blank_{width}x{height}")
        assert_true(blank_target.get("source") == "plus_icon_not_found", f"blank matrix must not use geometry fallback: {blank_target}")
        assert_true(blank_target.get("executable") is False, f"blank matrix must fail closed: {blank_target}")
        assert_true(blank_target.get("fallback_used") is False, f"blank matrix fallback must stay non-executable: {blank_target}")

    menu_targets = add_friend_menu_candidate_targets(
        [ocr_item("添加朋友", 270, 148, 336, 172)],
        (980, 860),
        plus_screen_x=334,
        plus_screen_y=70,
        include_expected=True,
    )
    menu_target = next(target for target in menu_targets if target.get("name") == "add_friend_menu_entry")
    assert_locator(menu_target, "add_friend_menu_entry")
    assert_true(menu_target.get("strategy") == "window_region_ocr_target", f"menu should prefer OCR target: {menu_target}")
    assert_true(menu_target.get("fallback_used") is False, f"menu OCR target should not mark fallback: {menu_target}")

    search_targets = find_add_friend_page_search_targets(
        [
            ocr_item("微信号/手机号", 430, 86, 540, 108),
            ocr_item("搜索", 700, 86, 744, 108),
        ],
        (980, 860),
    )
    assert_locator(search_targets["input"], "add_friend_search_input")
    assert_locator(search_targets["button"], "add_friend_search_button")
    assert_true(search_targets["input"].get("strategy") == "window_region_ocr_target", f"search input should use OCR when available: {search_targets}")
    assert_true(search_targets["button"].get("strategy") == "window_region_ocr_target", f"search button should use OCR when available: {search_targets}")

    small_ocr_search_targets = find_add_friend_page_search_targets(
        [
            ocr_item("Q搜索微信号或者手机号", 50, 85, 258, 106),
            ocr_item("搜索", 327, 86, 364, 107),
        ],
        (468, 520),
        screenshot=small_add_friend_image(),
    )
    assert_locator(small_ocr_search_targets["input"], "small_add_friend_ocr_search_input")
    assert_locator(small_ocr_search_targets["button"], "small_add_friend_ocr_search_button")
    assert_true(small_ocr_search_targets["input"].get("strategy") == "window_region_ocr_target", f"small dialog input should prefer OCR placeholder: {small_ocr_search_targets}")
    assert_true(small_ocr_search_targets["input"].get("fallback_used") is False, f"small dialog OCR input must not be fallback: {small_ocr_search_targets}")
    assert_true(small_ocr_search_targets["button"].get("strategy") == "window_region_ocr_target", f"small dialog button should prefer OCR button: {small_ocr_search_targets}")

    small_visual_search_targets = find_add_friend_page_search_targets([], (468, 520), screenshot=small_add_friend_image())
    assert_locator(small_visual_search_targets["input"], "small_add_friend_visual_search_input")
    assert_locator(small_visual_search_targets["button"], "small_add_friend_visual_search_button")
    assert_true(small_visual_search_targets["input"].get("strategy") == "visual_button_anchor_locator", f"small dialog should use visual button before fixed fallback: {small_visual_search_targets}")
    assert_true(small_visual_search_targets["button"].get("strategy") == "visual_button_locator", f"small dialog button should use visual locator: {small_visual_search_targets}")
    assert_true(small_visual_search_targets["input"].get("fallback_used") is False, f"visual input anchor must not be fixed fallback: {small_visual_search_targets}")

    small_search_targets = find_add_friend_page_search_targets([], (468, 520))
    assert_locator(small_search_targets["input"], "small_add_friend_search_input")
    assert_locator(small_search_targets["button"], "small_add_friend_search_button")
    assert_true(small_search_targets["input"].get("fallback_used") is True, f"small dialog input fixed fallback should be last resort: {small_search_targets}")
    assert_true("HIGH_RISK_FIXED_FALLBACK" in str(small_search_targets["input"].get("risk") or ""), f"fixed fallback should be visibly high risk: {small_search_targets}")
    assert_true(small_search_targets["button"].get("fallback_used") is True, f"small dialog button fixed fallback should be last resort: {small_search_targets}")

    exact_query = add_friend_query_visible_in_items("17368746889", [ocr_item("17368746889", 84, 85, 188, 106), ocr_item("搜索", 327, 86, 364, 107)])
    assert_true(exact_query.get("ok") is True, f"exact phone should verify: {exact_query}")
    residue_query = add_friend_query_visible_in_items("17368746889", [ocr_item("1736874688913866677777", 84, 85, 260, 106), ocr_item("搜索", 327, 86, 364, 107)])
    assert_true(residue_query.get("ok") is False, f"old+new phone residue must fail exact verification: {residue_query}")

    add_contact = add_friend_search_result_add_contact_target(
        [ocr_item("添加到通讯录", 600, 310, 720, 340)],
        (980, 860),
    )
    assert_true(isinstance(add_contact, dict), f"add-contact target missing: {add_contact}")
    assert_locator(add_contact, "add_contact_entry_button")
    assert_true(add_contact.get("strategy") == "window_region_ocr_target", f"add-contact should use OCR target: {add_contact}")


def test_add_friend_live_window_paths_pass_screenshot_to_plus_locator() -> None:
    source = (PROJECT_ROOT / "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/add_friend_windows.py").read_text(encoding="utf-8")
    pre_click_section = source.split("def add_friend_pre_click_main_window_readiness", 1)[1].split("def persist_add_friend_operator_guard_release", 1)[0]
    calibration_section = source.split("def add_friend_calibration_payload", 1)[1].split("def click_add_friend_ocr_item", 1)[0]
    for name, section in [
        ("formal pre-click", pre_click_section),
        ("calibration", calibration_section),
    ]:
        assert_true(
            "add_friend_plus_entry_target(geometry, screenshot.size, ocr_items, screenshot=screenshot" in section,
            f"{name} path must pass the captured screenshot into the visual plus locator",
        )


def test_add_friend_ocr_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_ocr import (
        compact_ocr_text,
        matched_ocr_tokens,
        normalize_ocr_text_value,
        ocr_item_text,
        ocr_surface_text,
        ocr_text_has_any,
    )

    assert_true(normalize_ocr_text_value("  添加\u3000朋友  ") == "添加 朋友", "OCR normalization should trim and normalize full-width spaces")
    assert_true(compact_ocr_text("  添 加\u3000朋 友  ") == "添加朋友", "OCR compact text mismatch")
    assert_true(ocr_item_text({"text": " 等 待 验 证 "}) == "等待验证", "OCR item text mismatch")
    surface = ocr_surface_text([{"text": "申请添加朋友"}, {"text": " 确 定 "}, {"missing": "ignored"}])
    assert_true(surface == "申请添加朋友\n确定", f"OCR surface mismatch: {surface}")
    assert_true(ocr_text_has_any(surface, ("发送添加朋友申请", "申请添加朋友")), "OCR token match should find application form")
    assert_true(matched_ocr_tokens("操作频繁，请稍后再试", ("操作频繁", "等待验证")) == ["操作频繁"], "OCR matched token mismatch")


def test_add_friend_pacing_tier_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_pacing import (
        DEFAULT_ADD_FRIEND_PACING_TIERS,
        normalize_pacing_tier,
        pacing_metadata,
        pacing_range,
    )

    for tier in ["critical_click", "input", "verify", "report", "default"]:
        assert_true(tier in DEFAULT_ADD_FRIEND_PACING_TIERS, f"missing pacing tier: {tier}")
        low, high = pacing_range(tier)
        assert_true(0 <= low <= high, f"invalid pacing range for {tier}: {(low, high)}")
        meta = pacing_metadata(tier, reason="smoke")
        assert_true(meta.get("tier") == tier, f"pacing metadata tier mismatch: {meta}")
        assert_true(meta.get("profile") == "balanced", f"pacing should default to balanced profile: {meta}")
    assert_true(pacing_range("report") == (0, 0), f"report tier should not wait: {pacing_range('report')}")
    assert_true(normalize_pacing_tier("missing") == "default", "unknown pacing tier should fallback to default")


def test_add_friend_result_mapping_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
        ERROR_ACCOUNT_RESTRICTED,
        ERROR_INVITE_CONFIRM_CLICK_FAILED,
        ERROR_PHONE_NOT_FOUND,
        RESULT_INVITE_SENT,
        add_friend_after_confirm_result,
        add_friend_search_not_found_result,
        add_friend_server_report_payload,
    )

    report = add_friend_server_report_payload(
        task_status="completed",
        result_code=RESULT_INVITE_SENT,
        current_step="task_completed",
    )
    assert_true(report == {
        "task.status": "completed",
        "task.result_code": RESULT_INVITE_SENT,
        "task.current_step": "task_completed",
    }, f"server report mismatch: {report}")

    invite_sent = add_friend_after_confirm_result(
        confirm_ok=True,
        surface_text="申请添加朋友 确定",
        invite_form_detected=True,
    )
    assert_true(invite_sent.get("task_status") == "completed", f"invite sent should complete: {invite_sent}")
    assert_true(invite_sent.get("result_code") == RESULT_INVITE_SENT, f"invite sent result mismatch: {invite_sent}")
    assert_true(invite_sent.get("result_code") != "already_friend", f"confirm path must not emit already_friend: {invite_sent}")

    restricted = add_friend_after_confirm_result(
        confirm_ok=True,
        surface_text="操作频繁，请稍后再试",
        invite_form_detected=False,
    )
    assert_true(restricted.get("error_code") == ERROR_ACCOUNT_RESTRICTED, f"restricted mapping mismatch: {restricted}")

    failed_click = add_friend_after_confirm_result(
        confirm_ok=False,
        surface_text="",
        invite_form_detected=False,
    )
    assert_true(failed_click.get("error_code") == ERROR_INVITE_CONFIRM_CLICK_FAILED, f"confirm failure mismatch: {failed_click}")

    not_found = add_friend_search_not_found_result(
        query="17368746889",
        not_found={"detected": True},
        screenshot_path="raw.png",
        annotated_path="annotated.png",
        ocr_items=[],
    )
    assert_true(not_found.get("error_code") == ERROR_PHONE_NOT_FOUND, f"not-found mapping mismatch: {not_found}")
    assert_true(not_found.get("server_report_payload", {}).get("task.error_code") == ERROR_PHONE_NOT_FOUND, f"not-found report mismatch: {not_found}")


def test_add_friend_screenshot_artifact_contract() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_screenshot import (
        normalize_region,
        sanitize_artifact_label,
        screenshot_artifact_filename,
        screenshot_artifact_metadata,
    )

    assert_true(sanitize_artifact_label(" add friend:before/点击 ") == "add_friend_before", "screenshot label sanitization mismatch")
    assert_true(screenshot_artifact_filename("add friend:before", timestamp_ms=123) == "add_friend_before_123.png", "screenshot filename mismatch")
    assert_true(normalize_region([20, 30, 10, 5]) == [10, 5, 20, 30], "screenshot region normalization mismatch")
    meta = screenshot_artifact_metadata(
        path="/tmp/a.png",
        label="add friend:before",
        capture_mode="window_visible",
        image_size=(468, 834),
        region=[20, 30, 10, 5],
    )
    assert_true(meta == {
        "path": "/tmp/a.png",
        "label": "add_friend_before",
        "capture_mode": "window_visible",
        "image_size": [468, 834],
        "region": [10, 5, 20, 30],
    }, f"screenshot metadata mismatch: {meta}")


def test_sidecar_add_friend_helpers_import() -> None:
    from apps.wechat_ai_customer_service.adapters.add_friend_contract import (
        normalize_add_friend_query as contract_normalize_add_friend_query,
    )
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (
        add_friend_windows_1080p_reference_plus_button_point_for_geometry,
        add_friend_optional_field_fill_enabled,
        add_friend_plus_button_point_for_geometry,
        args_for_daemon_request,
        classify_add_friend_after_confirm_surface,
        add_friend_surface_readiness,
        classify_add_friend_ocr_surface,
        add_friend_windows_plus_button_point_for_geometry,
        normalize_add_friend_query,
        type_add_friend_phone_query_like_human,
        type_add_friend_search_query,
    )

    assert_true(
        normalize_add_friend_query(phone=" 173 6874 6889 ") == "17368746889",
        "phone query normalization failed",
    )
    assert_true(
        normalize_add_friend_query(phone="", wechat=" wxid_demo ")
        == contract_normalize_add_friend_query(phone="", wechat=" wxid_demo "),
        "sidecar query normalization must reuse contract behavior",
    )
    geometry = {"width": 981, "height": 860, "left": -22, "top": 0, "right": 959, "bottom": 860}
    windows_point = add_friend_windows_plus_button_point_for_geometry(geometry)
    windows_1080p_point = add_friend_windows_1080p_reference_plus_button_point_for_geometry(geometry)
    assert_true(add_friend_plus_button_point_for_geometry(geometry) == windows_point, "default add_friend plus locator should be Windows-adapted")
    assert_true(292 <= windows_point[0] <= 314, f"Windows plus point mismatch: {windows_point}")
    assert_true(windows_1080p_point[0] > windows_point[0] + 35, f"Windows 1920x1080 reference point should remain separate: ref={windows_1080p_point}, adaptive={windows_point}")
    surface = classify_add_friend_ocr_surface([{"text": "添加朋友"}], (980, 860))
    assert_true(surface.get("state") == "add_contact_entry", f"unexpected surface: {surface}")
    invite_sent = classify_add_friend_after_confirm_surface([{"text": "等待验证"}], (468, 834), confirm_ok=True)
    assert_true(invite_sent.get("task_status") == "completed", f"unexpected final status: {invite_sent}")
    assert_true(invite_sent.get("result_code") == "invite_sent", f"unexpected final result: {invite_sent}")
    still_form = classify_add_friend_after_confirm_surface([{"text": "申请添加朋友"}, {"text": "确定"}], (468, 834), confirm_ok=True)
    assert_true(still_form.get("task_status") == "completed", f"unexpected still-form status: {still_form}")
    assert_true(still_form.get("result_code") == "invite_sent", f"still-form status should report invite_sent: {still_form}")
    restricted = classify_add_friend_after_confirm_surface([{"text": "操作频繁，请稍后再试"}], (468, 834), confirm_ok=True)
    assert_true(restricted.get("task_status") == "failed", f"restricted status should fail: {restricted}")
    assert_true(restricted.get("error_code") == "ACCOUNT_RESTRICTED", f"restricted error mismatch: {restricted}")
    failed_click = classify_add_friend_after_confirm_surface([], (468, 834), confirm_ok=False)
    assert_true(failed_click.get("error_code") == "INVITE_CONFIRM_CLICK_FAILED", f"confirm failure mismatch: {failed_click}")
    blank = add_friend_surface_readiness(
        {"detected": True},
        [],
        {"width": 980, "height": 860},
        stage="after_search",
    )
    assert_true(blank.get("ok") is False, f"blank surface should block add_friend: {blank}")
    non_wechat_ready = add_friend_surface_readiness(
        {"detected": False},
        [{"text": "127.0.0.1:8017", "left": 190, "top": 68, "right": 320, "bottom": 91, "center_x": 255, "center_y": 79}],
        {"width": 981, "height": 860},
        stage="calibration",
    )
    assert_true(non_wechat_ready.get("ok") is False, f"non-WeChat content should not calibrate as ready: {non_wechat_ready}")
    assert_true(non_wechat_ready.get("state") == "wechat_main_surface_not_ready", f"unexpected non-WeChat state: {non_wechat_ready}")
    wechat_ready = add_friend_surface_readiness(
        {"detected": False},
        [{"text": "搜索", "left": 108, "top": 58, "right": 156, "bottom": 82, "center_x": 132, "center_y": 70, "confidence": 0.92}],
        {"width": 981, "height": 860},
        stage="calibration",
    )
    assert_true(wechat_ready.get("ok") is True, f"search anchor should calibrate as ready: {wechat_ready}")
    pressed: list[int] = []
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar

    original_pause = sidecar.add_friend_human_pause
    try:
        sidecar.add_friend_human_pause = lambda *_args, **_kwargs: 0.0
        typed = type_add_friend_phone_query_like_human(
            1001,
            "173 6874 6889",
            key_press_func=lambda key: pressed.append(int(key)),
            window_guard_func=lambda: {"ok": True},
        )
        assert_true(typed.get("ok") is True, f"digit query input should pass: {typed}")
        assert_true(typed.get("method") == "add_friend_digit_keys", f"unexpected input method: {typed}")
        assert_true(pressed == [ord(char) for char in "17368746889"], f"unexpected pressed keys: {pressed}")
    finally:
        sidecar.add_friend_human_pause = original_pause
    blocked = type_add_friend_search_query(1001, "wxid_demo")
    assert_true(blocked.get("ok") is False, f"non-numeric SendInput should be opt-in: {blocked}")
    assert_true(add_friend_optional_field_fill_enabled() is False, "optional text fill should be off by default")


def main() -> int:
    tests = [
        test_required_files_exist,
        test_requirements_cover_live_imports,
        test_entry_click_script_defaults_are_low_disturbance,
        test_entry_click_script_is_main_review_entry,
        test_entry_click_latest_check_script_contract,
        test_add_friend_readme_formal_contract,
        test_add_friend_artifact_layout_contract,
        test_add_friend_route_manifest_contract,
        test_entry_click_field_contract,
        test_add_friend_payload_builder_contract,
        test_add_friend_step_event_report_contract,
        test_entry_click_validation_failure_report_uses_native_events,
        test_add_friend_flow_events_contract,
        test_add_friend_flow_context_contract,
        test_add_friend_already_friend_terminal_event_contract,
        test_sidecar_uses_flow_context_for_entry_click,
        test_sidecar_uses_add_friend_payload_builders,
        test_add_friend_uses_shared_operator_guard_module,
        test_add_friend_operator_guard_compat_wrapper_calls_shared_module,
        test_c2_search_by_remark_code_has_no_add_friend_operator_guard_report_fields,
        test_add_friend_preflight_blocks_unready_window,
        test_add_friend_requires_operator_guard_before_click_flow,
        test_add_friend_formal_preclick_requires_foreground_and_main_surface,
        test_add_friend_calibration_mode_contract,
        test_entry_click_task_outcome_contract,
        test_add_friend_actions_contract,
        test_invite_form_locator_contract,
        test_invite_form_input_click_failure_blocks_keyboard_actions,
        test_invite_form_field_verification_blocks_confirm_click,
        test_query_verify_invalid_dialog_handle_returns_structured_failure,
        test_add_friend_primary_locator_contract,
        test_add_friend_live_window_paths_pass_screenshot_to_plus_locator,
        test_add_friend_ocr_contract,
        test_add_friend_pacing_tier_contract,
        test_add_friend_result_mapping_contract,
        test_add_friend_screenshot_artifact_contract,
        test_sidecar_add_friend_helpers_import,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} add_friend package smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
