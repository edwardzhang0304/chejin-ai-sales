"""Formal-only fail-fast checks; the existing full build gates still run afterward."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "CHEJIN_RPA_MODE": "mock"}
    for command in (
        ["scripts/generate-c2-observation-schema.py", "--check"],
        ["scripts/run-credential-security-checks.py"],
        ["omniauto-rpa/apps/wechat_ai_customer_service/tests/run_customer_service_brain_contract_checks.py"],
    ):
        result = subprocess.run([sys.executable, *command], cwd=ROOT / "worker-client", env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
