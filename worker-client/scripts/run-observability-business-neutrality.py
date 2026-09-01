from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
WORKER_TESTS = [
    "tests/test_task_runner.py::TaskRunnerTest::test_observability_toggle_keeps_add_friend_business_trace_identical",
    "tests/test_task_runner.py::TaskRunnerTest::test_observability_toggle_keeps_c2_front_middle_back_identical",
    "tests/test_task_runner.py::TaskRunnerTest::test_observability_toggle_keeps_c3_front_middle_back_identical",
]
BACKEND_TESTS = [
    "tests/test_observability.py::test_observability_toggle_keeps_c0_front_middle_back_identical",
    "tests/test_c3_api.py::test_observability_toggle_keeps_c4_front_middle_back_identical",
    "tests/test_feishu_handoff.py::test_observability_toggle_keeps_handoff_front_middle_back_identical",
]


def run_suite(*, backend: bool) -> None:
    tests = BACKEND_TESTS if backend else WORKER_TESTS
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--maxfail=1", *tests],
        cwd=ROOT / "backend" if backend else ROOT / "worker-client",
        check=True,
    )


def main() -> int:
    # Each test executes the same production entry with observability disabled
    # and enabled, then directly compares front input, middle operation
    # count/order, and final business state in one oracle.
    run_suite(backend=False)
    run_suite(backend=True)
    print("observability direct front-middle-back comparison passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
