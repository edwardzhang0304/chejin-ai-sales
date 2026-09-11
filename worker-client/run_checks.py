from __future__ import annotations

import argparse
import compileall
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-receipt')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT.parent / 'ops/formal_release'))
    from source_check_reuse import load_receipt, complete, SUITES
    receipt_path = os.environ.get('CHEJIN_SOURCE_CHECK_RECEIPT')
    receipt = load_receipt(receipt_path) if receipt_path else {}
    completed = set(receipt.get('completed_suites', []))
    if completed == set(SUITES):
        print('SOURCE_CHECKS_REUSED_FROM_THIS_EXACT_RUN')
        if args.write_receipt: complete(args.write_receipt, completed)
        return 0
    def checked(name, command, *, cwd):
        if name in completed:
            print('REUSED_PASSED_SOURCE_SUITE ' + name, flush=True)
            return subprocess.CompletedProcess(command, 0)
        result = subprocess.run(command, cwd=cwd, env=env)
        if result.returncode == 0: completed.add(name)
        return result
    env = os.environ.copy()
    env["CHEJIN_RPA_MODE"] = "mock"
    env["CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS"] = "0"
    env["CHEJIN_WORKER_HOME"] = tempfile.mkdtemp(prefix="chejin-worker-checks-")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    generated_contract = checked("schema",
        [sys.executable, "scripts/generate-c2-observation-schema.py", "--check"],
        cwd=ROOT,
    )
    if generated_contract.returncode:
        return generated_contract.returncode
    # unittest discovery does not execute function-style pytest security tests.
    credential_test = checked("credentials",
        [sys.executable, "scripts/run-credential-security-checks.py"],
        cwd=ROOT,
    )
    if credential_test.returncode:
        return credential_test.returncode
    unit_command = [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests", "-v"]
    for name in receipt.get("unittest_resume", []):
        unit_command.extend(["-k", name])
    test = checked("unittest",
        unit_command,
        cwd=ROOT,
    )
    if test.returncode:
        return test.returncode
    runtime_ui_test = checked("ui_bridge",
        [
            "node",
            "--test",
            str(
                ROOT.parent
                / "packages"
                / "worker-ui-baseline"
                / "tests"
                / "runtime-bridge-state.test.mjs"
            ),
        ],
        cwd=ROOT.parent,
    )
    if runtime_ui_test.returncode:
        return runtime_ui_test.returncode
    omniauto_test_dir = ROOT / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "tests"
    omniauto_check_scripts = (
        "run_add_friend_package_smoke.py",
        "run_wechat_win32_ocr_compat_checks.py",
        "run_wechat_win32_ocr_env_config_checks.py",
        "run_wechat_win32_ocr_interaction_evidence_checks.py",
        "run_wechat_win32_ocr_humanized_input_checks.py",
        "run_wechat_startup_calibration_v0923_checks.py",
    )
    for script_name in omniauto_check_scripts:
        check = checked(script_name,
            [sys.executable, str(omniauto_test_dir / script_name)],
            cwd=ROOT / "omniauto-rpa",
        )
        if check.returncode:
            return check.returncode
    smoke = checked("smoke_e2e", [sys.executable, "smoke_e2e.py"], cwd=ROOT)
    if smoke.returncode:
        return smoke.returncode
    ok = compileall.compile_dir(str(ROOT / "chejin_worker_client"), quiet=1)
    if not ok: return 1
    completed.add('compile')
    if args.write_receipt: complete(args.write_receipt, completed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
