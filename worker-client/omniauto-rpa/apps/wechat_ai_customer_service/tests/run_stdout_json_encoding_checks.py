"""Regression checks for machine-readable JSON emitted over stdout.

Windows console code pages can corrupt non-ASCII JSON when one Python process
prints text and another decodes stdout as UTF-8. Runtime IPC stdout should be
ASCII-safe JSON; UTF-8 files, HTTP bodies, and LLM prompts are intentionally
out of scope.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]

STDOUT_JSON_ROOTS = [
    APP_ROOT / "adapters",
    APP_ROOT / "workflows",
    APP_ROOT / "scripts",
]

TEXT_JSON_STDOUT_FILES = {
    APP_ROOT / "scripts" / "audit_rag_experience_governance.py",
    APP_ROOT / "scripts" / "run_rpa_acceptance_report.py",
    APP_ROOT / "scripts" / "run_rpa_operator_guard.py",
}

LAUNCH_ENV_FILES = [
    APP_ROOT / "adapters" / "wechat_connector.py",
    APP_ROOT / "admin_backend" / "services" / "customer_service_runtime.py",
    APP_ROOT / "scripts" / "run_customer_service_listener.py",
]


def main() -> int:
    results = [
        check_stdout_json_is_ascii_safe(),
        check_unicode_round_trip_semantics(),
        check_runtime_python_subprocesses_force_utf8(),
    ]
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_stdout_json_is_ascii_safe() -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for root in STDOUT_JSON_ROOTS:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "ensure_ascii=False" not in line:
                    continue
                compact = line.strip()
                if "print(json.dumps" in compact or "sys.stdout.write(json.dumps" in compact:
                    violations.append({"path": rel, "line": lineno, "text": compact})
                elif path in TEXT_JSON_STDOUT_FILES and "text = json.dumps" in compact:
                    violations.append({"path": rel, "line": lineno, "text": compact})
    return {
        "name": "check_stdout_json_is_ascii_safe",
        "ok": not violations,
        "violations": violations[:20],
        "violation_count": len(violations),
    }


def check_unicode_round_trip_semantics() -> dict[str, Any]:
    payload = {"target": "许聪", "message": "中文会话名必须稳定往返"}
    wire = json.dumps(payload, ensure_ascii=True)
    if not wire.isascii():
        return {"name": "check_unicode_round_trip_semantics", "ok": False, "reason": "wire_not_ascii", "wire": wire}
    decoded = json.loads(wire)
    return {
        "name": "check_unicode_round_trip_semantics",
        "ok": decoded == payload,
        "wire": wire,
        "decoded": decoded,
    }


def check_runtime_python_subprocesses_force_utf8() -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    for path in LAUNCH_ENV_FILES:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for token in ('"PYTHONUTF8"', '"PYTHONIOENCODING"'):
            if token not in text:
                missing.append({"path": rel, "token": token})
    return {
        "name": "check_runtime_python_subprocesses_force_utf8",
        "ok": not missing,
        "missing": missing,
    }


if __name__ == "__main__":
    raise SystemExit(main())
