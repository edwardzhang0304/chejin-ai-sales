from __future__ import annotations

import compileall
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    env = os.environ.copy()
    env["CHEJIN_RPA_MODE"] = "mock"
    env["CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS"] = "0"
    env["CHEJIN_WORKER_HOME"] = tempfile.mkdtemp(prefix="chejin-worker-checks-")
    generated_contract = subprocess.run(
        [sys.executable, "scripts/generate-c2-observation-schema.py", "--check"],
        cwd=ROOT,
        env=env,
    )
    if generated_contract.returncode:
        return generated_contract.returncode
    test = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        env=env,
    )
    if test.returncode:
        return test.returncode
    omniauto_test_dir = ROOT / "omniauto-rpa" / "apps" / "wechat_ai_customer_service" / "tests"
    omniauto_check_scripts = (
        "run_wechat_win32_ocr_compat_checks.py",
        "run_wechat_win32_ocr_env_config_checks.py",
        "run_wechat_win32_ocr_interaction_evidence_checks.py",
        "run_wechat_win32_ocr_humanized_input_checks.py",
        "run_wechat_win32_ocr_window_action_planning_checks.py",
    )
    for script_name in omniauto_check_scripts:
        check = subprocess.run(
            [sys.executable, str(omniauto_test_dir / script_name)],
            cwd=ROOT / "omniauto-rpa",
            env=env,
        )
        if check.returncode:
            return check.returncode
    smoke = subprocess.run([sys.executable, "smoke_e2e.py"], cwd=ROOT, env=env)
    if smoke.returncode:
        return smoke.returncode
    ok = compileall.compile_dir(str(ROOT / "chejin_worker_client"), quiet=1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
