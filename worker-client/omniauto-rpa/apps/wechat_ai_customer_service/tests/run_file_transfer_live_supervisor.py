"""Supervise resumable File Transfer Assistant live regression runs.

The live WeChat regression is intentionally slow: every scenario may send a
real WeChat message, wait for UI state, call LLM assistance, write runtime data,
and verify the reply. This supervisor keeps that long run outside a single
child-process lifetime by running the resumable runner in bounded chunks.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
DEFAULT_RUNNER = APP_ROOT / "tests" / "run_file_transfer_live_regression.py"
DEFAULT_CONFIG_PATH = APP_ROOT / "configs" / "file_transfer_live_regression.example.json"
DEFAULT_SCENARIO_PATH = APP_ROOT / "tests" / "scenarios" / "file_transfer_live_regression.json"
DEFAULT_RESULT_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_regression_supervised.json"
)
DEFAULT_SUMMARY_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_regression_supervisor_summary.json"
)
DEFAULT_SETTINGS_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/tenants/default/customer_service/settings.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python executable used for child runner invocations.")
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIO_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--send", action="store_true", help="Actually send WeChat messages.")
    parser.add_argument("--reset-state", action="store_true", help="Reset runner runtime state before the first chunk.")
    parser.add_argument("--delay-seconds", type=float, default=0.6)
    parser.add_argument("--chunk-size", type=int, default=4)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--per-run-timeout-seconds", type=int, default=1200)
    parser.add_argument("--max-attempts-per-chunk", type=int, default=3)
    parser.add_argument(
        "--temporary-full-auto",
        action="store_true",
        help="Temporarily enable local default-tenant full-auto customer-service settings, then restore them.",
    )
    args = parser.parse_args()

    result = run_supervisor(args)
    print_json(result)
    return 0 if result.get("ok") else 1


def run_supervisor(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = load_scenarios(args.scenarios)
    selected = select_scenario_indexes(scenarios, args)
    started_at = now_text()
    invocations: list[dict[str, Any]] = []

    with temporary_full_auto_settings(enabled=bool(args.temporary_full_auto and args.send)):
        first_invocation = True
        for chunk_start, chunk_end in chunk_ranges(selected, chunk_size=max(1, int(args.chunk_size or 1))):
            attempts = 0
            while attempts < max(1, int(args.max_attempts_per_chunk or 1)):
                attempts += 1
                command = build_child_command(
                    args,
                    start_index=chunk_start,
                    end_index=chunk_end,
                    reset_state=bool(args.reset_state and first_invocation),
                )
                first_invocation = False
                invocation = run_child(command, timeout_seconds=max(1, int(args.per_run_timeout_seconds or 1)))
                invocation.update({"chunk_start": chunk_start, "chunk_end": chunk_end, "attempt": attempts})
                if invocation.get("timed_out"):
                    invocation["lock_cleanup"] = cleanup_runner_lock(args.config)
                invocations.append(invocation)
                write_summary(args.summary_path, build_summary(args, scenarios, selected, started_at, invocations))

                payload = load_json(args.result_path)
                if has_failures(payload, chunk_start=chunk_start, chunk_end=chunk_end):
                    return build_summary(args, scenarios, selected, started_at, invocations, final_payload=payload)
                if chunk_passed(payload, scenarios, chunk_start=chunk_start, chunk_end=chunk_end):
                    break
                if not invocation.get("timed_out") and invocation.get("returncode") not in (0, None):
                    return build_summary(args, scenarios, selected, started_at, invocations, final_payload=payload)
            else:
                payload = load_json(args.result_path)
                summary = build_summary(args, scenarios, selected, started_at, invocations, final_payload=payload)
                summary["ok"] = False
                summary["error"] = f"chunk {chunk_start}-{chunk_end} did not finish after {attempts} attempts"
                write_summary(args.summary_path, summary)
                return summary

    final_payload = load_json(args.result_path)
    summary = build_summary(args, scenarios, selected, started_at, invocations, final_payload=final_payload)
    write_summary(args.summary_path, summary)
    return summary


def build_child_command(args: argparse.Namespace, *, start_index: int, end_index: int, reset_state: bool) -> list[str]:
    command = [
        str(args.python),
        str(args.runner),
        "--config",
        str(args.config),
        "--scenarios",
        str(args.scenarios),
        "--result-path",
        str(args.result_path),
        "--delay-seconds",
        str(args.delay_seconds),
        "--start-index",
        str(start_index),
        "--end-index",
        str(end_index),
        "--resume",
    ]
    if args.send:
        command.append("--send")
    if reset_state:
        command.append("--reset-state")
    for name in args.only or []:
        command.extend(["--only", str(name)])
    return command


def run_child(command: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    started = time.time()
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        stdout, stderr = process.communicate()
    return {
        "command": command,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 2),
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
    }


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    process.kill()


def cleanup_runner_lock(config_path: Path) -> dict[str, Any]:
    config = load_json(config_path)
    state_path_value = config.get("state_path")
    if not state_path_value:
        return {"ok": True, "removed": False, "reason": "state_path_missing"}
    state_path = Path(str(state_path_value))
    if not state_path.is_absolute():
        state_path = PROJECT_ROOT / state_path
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    if not lock_path.exists():
        return {"ok": True, "removed": False, "path": str(lock_path)}
    try:
        lock_path.unlink()
        return {"ok": True, "removed": True, "path": str(lock_path)}
    except Exception as exc:
        return {"ok": False, "removed": False, "path": str(lock_path), "error": repr(exc)}


def select_scenario_indexes(scenarios: list[dict[str, Any]], args: argparse.Namespace) -> list[int]:
    start_index = max(1, int(args.start_index or 1))
    end_index = min(len(scenarios), int(args.end_index or len(scenarios)))
    only = {str(item) for item in (args.only or []) if str(item).strip()}
    selected = []
    for index, scenario in enumerate(scenarios, start=1):
        if index < start_index or index > end_index:
            continue
        if only and str(scenario.get("name") or "") not in only:
            continue
        selected.append(index)
    return selected


def chunk_ranges(indexes: list[int], *, chunk_size: int) -> Iterator[tuple[int, int]]:
    if not indexes:
        return
    current: list[int] = []
    previous = None
    for index in indexes:
        if previous is not None and index != previous + 1:
            if current:
                yield current[0], current[-1]
                current = []
        current.append(index)
        previous = index
        if len(current) >= chunk_size:
            yield current[0], current[-1]
            current = []
    if current:
        yield current[0], current[-1]


def chunk_passed(payload: dict[str, Any], scenarios: list[dict[str, Any]], *, chunk_start: int, chunk_end: int) -> bool:
    passed = {str(item.get("name") or "") for item in payload.get("results", []) or [] if item.get("ok") is True}
    expected = {
        str(scenario.get("name") or f"scenario_{index}")
        for index, scenario in enumerate(scenarios, start=1)
        if chunk_start <= index <= chunk_end
    }
    return bool(expected) and expected.issubset(passed)


def has_failures(payload: dict[str, Any], *, chunk_start: int, chunk_end: int) -> bool:
    for item in payload.get("failures", []) or []:
        try:
            index = int(item.get("index") or 0)
        except Exception:
            index = 0
        if chunk_start <= index <= chunk_end:
            return True
    return False


def build_summary(
    args: argparse.Namespace,
    scenarios: list[dict[str, Any]],
    selected: list[int],
    started_at: str,
    invocations: list[dict[str, Any]],
    *,
    final_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = final_payload or load_json(args.result_path)
    selected_names = [
        str(scenarios[index - 1].get("name") or f"scenario_{index}")
        for index in selected
        if 1 <= index <= len(scenarios)
    ]
    passed = {str(item.get("name") or "") for item in payload.get("results", []) or [] if item.get("ok") is True}
    failed = [item for item in payload.get("failures", []) or [] if item.get("name") in selected_names]
    pending = [name for name in selected_names if name not in passed and name not in {item.get("name") for item in failed}]
    summary = {
        "ok": not failed and not pending,
        "started_at": started_at,
        "finished_at": now_text(),
        "send": bool(args.send),
        "temporary_full_auto": bool(args.temporary_full_auto and args.send),
        "scenario_path": str(args.scenarios),
        "result_path": str(args.result_path),
        "summary_path": str(args.summary_path),
        "selected_count": len(selected_names),
        "passed_count": len(passed.intersection(selected_names)),
        "failed_count": len(failed),
        "pending_count": len(pending),
        "pending": pending,
        "failures": failed,
        "invocation_count": len(invocations),
        "timed_out_count": sum(1 for item in invocations if item.get("timed_out")),
        "invocations": invocations,
    }
    return summary


@contextmanager
def temporary_full_auto_settings(*, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    settings_path = PROJECT_ROOT / DEFAULT_SETTINGS_PATH
    original_exists = settings_path.exists()
    original_text = settings_path.read_text(encoding="utf-8") if original_exists else ""
    settings = load_json(settings_path)
    patched = {
        **settings,
        "enabled": True,
        "reply_mode": "full_auto",
        "record_messages": True,
        "auto_learn": True,
        "use_llm": True,
        "rag_enabled": True,
        "data_capture_enabled": True,
        "handoff_enabled": True,
        "operator_alert_enabled": True,
    }
    write_json(settings_path, patched)
    try:
        yield
    finally:
        if original_exists:
            settings_path.write_text(original_text, encoding="utf-8")
        else:
            try:
                settings_path.unlink()
            except FileNotFoundError:
                pass


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("scenario file must contain a list")
    return [dict(item or {}) for item in payload]


def load_json(path: Path) -> dict[str, Any]:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    write_json(resolved, payload)


def tail_text(value: str, *, limit: int = 6000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
