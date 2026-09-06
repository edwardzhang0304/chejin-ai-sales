"""Shared credential gate, run before either Windows delivery build.

--collect-only verifies discovery without running tests or assembling fixtures.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = (
    "tests/test_secret_file_exclusion.py",
    "tests/test_vision_credentials.py",
    "tests/test_vision_fetch_http.py",
    "tests/test_vision_delivery.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    env = os.environ.copy()
    env["CHEJIN_RPA_MODE"] = "mock"
    env["CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS"] = "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), env.get("PYTHONPATH", "")) if value
    )
    command = [
        sys.executable, "-W", "error::ResourceWarning", "-m", "pytest",
        *TEST_FILES, "-q",
    ]
    if args.collect_only:
        command.append("--collect-only")
    with tempfile.TemporaryDirectory(prefix="chejin-credential-gate-") as home:
        env["CHEJIN_WORKER_HOME"] = home
        return subprocess.run(command, cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
