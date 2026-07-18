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
    test = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        env=env,
    )
    if test.returncode:
        return test.returncode
    compat = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "omniauto-rpa"
                / "apps"
                / "wechat_ai_customer_service"
                / "tests"
                / "run_wechat_win32_ocr_compat_checks.py"
            ),
        ],
        cwd=ROOT / "omniauto-rpa",
        env=env,
    )
    if compat.returncode:
        return compat.returncode
    smoke = subprocess.run([sys.executable, "smoke_e2e.py"], cwd=ROOT, env=env)
    if smoke.returncode:
        return smoke.returncode
    ok = compileall.compile_dir(str(ROOT / "chejin_worker_client"), quiet=1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
