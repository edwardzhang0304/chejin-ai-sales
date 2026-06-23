"""Live regression runner for File Transfer Assistant self-tests.

This runner sends approved seed messages to ``文件传输助手`` and runs the
guarded listener after each scenario. It is intentionally isolated to its own
config/state/audit/workbook files so it can exercise realistic WeChat IO
without polluting the normal smoke-test state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from approved_outbound_send import run as run_outbound  # noqa: E402
from listen_and_reply import load_config, resolve_path, run_workflow  # noqa: E402
from rag_layer import RagService  # noqa: E402
from wechat_connector import FILE_TRANSFER_ASSISTANT  # noqa: E402


DEFAULT_CONFIG_PATH = APP_ROOT / "configs" / "file_transfer_live_regression.example.json"
DEFAULT_SCENARIO_PATH = APP_ROOT / "tests" / "scenarios" / "file_transfer_live_regression.json"
DEFAULT_RESULT_PATH = Path("runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_regression_results.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIO_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--send", action="store_true", help="Actually send messages to File Transfer Assistant.")
    parser.add_argument("--reset-state", action="store_true", help="Delete this live-regression state/audit/workbook before running.")
    parser.add_argument("--delay-seconds", type=float, default=0.8)
    parser.add_argument("--start-index", type=int, default=1, help="Run scenarios from this 1-based index.")
    parser.add_argument("--end-index", type=int, default=None, help="Run scenarios through this 1-based index.")
    parser.add_argument("--only", action="append", default=[], help="Run only scenario names matching this value. Can be repeated.")
    parser.add_argument("--resume", action="store_true", help="Skip scenarios that already passed in the result file.")
    args = parser.parse_args()

    result = run_live_regression(args)
    print_json(result)
    return 0 if result.get("ok") else 1


def run_live_regression(args: argparse.Namespace) -> dict[str, Any]:
    # Ensure customer service is enabled so live regression messages are processed
    from admin_backend.services.customer_service_settings import CustomerServiceSettings
    CustomerServiceSettings().save({
        "enabled": True,
        "reply_mode": "guarded_auto",
        "record_messages": True,
        "auto_learn": True,
        "use_llm": True,
        "rag_enabled": True,
        "data_capture_enabled": True,
        "handoff_enabled": True,
        "operator_alert_enabled": True,
    })
    config = load_config(args.config)
    scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))
    if args.reset_state:
        reset_runtime_files(config, args.result_path)
    previous_payload = load_previous_result(args.result_path) if args.resume and not args.reset_state else {}
    previous_results = list(previous_payload.get("results") or [])
    completed_names = {
        str(item.get("name") or "")
        for item in previous_results
        if item.get("ok") is True and item.get("name")
    }
    selected_scenarios = select_scenarios(scenarios, args)
    rag_seed = seed_configured_rag_sources(config)
    live_run_id = str(previous_payload.get("live_run_id") or time.strftime("%Y%m%d%H%M%S"))
    setattr(args, "_live_run_id", live_run_id)
    setattr(args, "_append_live_run_nonce", bool(config.get("append_live_run_nonce", bool(args.send))))

    bootstrap = run_workflow(
        Namespace(
            config=args.config,
            once=True,
            iterations=None,
            interval_seconds=None,
            send=False,
            allow_fallback_send=False,
            mark_dry_run=False,
            bootstrap=True,
            write_data=False,
            target=None,
        )
    )

    if bootstrap.get("ok") is not True:
        bootstrap_error = str(bootstrap.get("error") or "bootstrap_failed")
        gate = bootstrap.get("cloud_gate") if isinstance(bootstrap.get("cloud_gate"), dict) else {}
        gate_reason = str(gate.get("reason") or "")
        blocked_error = bootstrap_error if not gate_reason else f"{bootstrap_error}:{gate_reason}"
        failures = [
            {
                "name": "bootstrap",
                "index": 0,
                "ok": False,
                "error": blocked_error,
                "output": {"bootstrap": bootstrap},
            }
        ]
        payload = build_result_payload(args, config, rag_seed, live_run_id, bootstrap, failures, selected_scenarios, run_count=0)
        payload["ok"] = False
        write_result(args.result_path, payload)
        return payload

    results = previous_results[:]
    run_count = 0
    for index, scenario in selected_scenarios:
        scenario_name = str(scenario.get("name") or f"scenario_{index}")
        if args.resume and scenario_name in completed_names:
            continue
        run_count += 1
        try:
            output = run_scenario(args, scenario, index=index)
            assert_scenario(scenario, output)
            results.append({"name": scenario_name, "index": index, "ok": True, "output": compact_output(output)})
        except Exception as exc:
            results.append(
                {
                    "name": scenario_name,
                    "index": index,
                    "ok": False,
                    "error": repr(exc),
                    "output": compact_output(locals().get("output", {})),
                }
            )
            write_result(args.result_path, build_result_payload(args, config, rag_seed, live_run_id, bootstrap, results, selected_scenarios, run_count))
            if args.send:
                break
        write_result(args.result_path, build_result_payload(args, config, rag_seed, live_run_id, bootstrap, results, selected_scenarios, run_count))

    failures = [item for item in results if not item.get("ok")]
    payload = build_result_payload(args, config, rag_seed, live_run_id, bootstrap, results, selected_scenarios, run_count)
    payload["ok"] = not failures
    write_result(args.result_path, payload)
    return payload


def select_scenarios(scenarios: list[dict[str, Any]], args: argparse.Namespace) -> list[tuple[int, dict[str, Any]]]:
    start_index = max(1, int(args.start_index or 1))
    end_index = int(args.end_index) if args.end_index else len(scenarios)
    only = {str(item) for item in (args.only or []) if str(item).strip()}
    selected = []
    for index, scenario in enumerate(scenarios, start=1):
        if index < start_index or index > end_index:
            continue
        name = str(scenario.get("name") or "")
        if only and name not in only:
            continue
        selected.append((index, scenario))
    return selected


def load_previous_result(path: Path) -> dict[str, Any]:
    resolved = resolve_path(path)
    if not resolved.exists() or not resolved.is_file():
        return {}
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_result_payload(
    args: argparse.Namespace,
    config: dict[str, Any],
    rag_seed: dict[str, Any],
    live_run_id: str,
    bootstrap: dict[str, Any],
    results: list[dict[str, Any]],
    selected_scenarios: list[tuple[int, dict[str, Any]]],
    run_count: int,
) -> dict[str, Any]:
    failures = [item for item in results if not item.get("ok")]
    selected_names = [str(scenario.get("name") or f"scenario_{index}") for index, scenario in selected_scenarios]
    recorded_names = {str(item.get("name") or "") for item in results if item.get("name")}
    passed_names = {str(item.get("name") or "") for item in results if item.get("ok") is True}
    pending_names = [name for name in selected_names if name not in recorded_names]
    return {
        "ok": not failures and not pending_names,
        "send": bool(args.send),
        "config_path": str(args.config),
        "scenario_path": str(args.scenarios),
        "start_index": int(args.start_index or 1),
        "end_index": int(args.end_index) if args.end_index else None,
        "only": list(args.only or []),
        "resume": bool(args.resume),
        "rag_seed": rag_seed,
        "live_run_id": live_run_id,
        "bootstrap": bootstrap,
        "selected_count": len(selected_scenarios),
        "run_count": run_count,
        "count": len(results),
        "passed_count": len(passed_names),
        "pending_count": len(pending_names),
        "pending": pending_names,
        "failures": failures,
        "results": results,
    }


def seed_configured_rag_sources(config: dict[str, Any]) -> dict[str, Any]:
    seeds = config.get("rag_seed_paths", []) or []
    if not isinstance(seeds, list) or not seeds:
        return {"enabled": False, "count": 0}
    service = RagService(tenant_id=str(config.get("tenant_id") or config.get("rag_tenant_id") or "") or None)
    results = []
    for raw in seeds:
        item = {"path": raw} if isinstance(raw, str) else dict(raw or {})
        path_value = item.get("path")
        if not path_value:
            results.append({"ok": False, "message": "seed path is required"})
            continue
        path = resolve_path(path_value)
        try:
            service.delete_source_by_path(path)
            result = service.ingest_file(
                path,
                source_type=str(item.get("source_type") or "product_doc"),
                category=str(item.get("category") or "product_explanations"),
                product_id=str(item.get("product_id") or ""),
                layer=str(item.get("layer") or "tenant"),
            )
            results.append({"ok": bool(result.get("ok")), "path": str(path), "source_id": result.get("source_id")})
        except Exception as exc:
            results.append({"ok": False, "path": str(path), "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    return {"enabled": True, "ok": not failures, "count": len(results), "failures": failures, "results": results}


def run_scenario(args: argparse.Namespace, scenario: dict[str, Any], *, index: int) -> dict[str, Any]:
    messages = [str(item) for item in scenario.get("messages", []) or []]
    if not messages:
        raise ValueError("Scenario has no messages")

    outbound_results = []
    for message_index, text in enumerate(messages, start=1):
        send_text = live_outbound_text(args, text, scenario_index=index, message_index=message_index)
        outbound_args = Namespace(
            config=args.config,
            target=FILE_TRANSFER_ASSISTANT,
            session_key="",
            text=send_text,
            send=bool(args.send),
            reason=f"live_regression:{index}:{scenario.get('name')}:{message_index}",
            allow_prefixless=True,
            ignore_review_queue=True,
            ignore_rate_limit=False,
        )
        outbound = run_outbound(outbound_args)
        outbound_results.append(outbound)
        if not outbound.get("ok"):
            raise AssertionError(f"Outbound send failed: {outbound}")
        if args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    workflow = run_workflow(
        Namespace(
            config=args.config,
            once=True,
            iterations=None,
            interval_seconds=None,
            send=bool(args.send),
            allow_fallback_send=False,
            mark_dry_run=False,
            bootstrap=False,
            write_data=True,
            target=None,
            session_key="",
        )
    )
    event = (workflow.get("events") or [{}])[0]
    return {
        "scenario": scenario,
        "outbound": outbound_results,
        "workflow": workflow,
        "event": event,
    }


def live_outbound_text(args: argparse.Namespace, text: str, *, scenario_index: int, message_index: int) -> str:
    if not bool(getattr(args, "_append_live_run_nonce", False)):
        return text
    run_id = str(getattr(args, "_live_run_id", "") or time.strftime("%Y%m%d%H%M%S"))
    return f"{text}\n[live-regression:{run_id}:{scenario_index}:{message_index}]"


def assert_scenario(scenario: dict[str, Any], output: dict[str, Any]) -> None:
    event = output.get("event", {}) or {}
    workflow = output.get("workflow", {}) or {}
    if workflow.get("ok") is not True:
        error_code = str(workflow.get("error") or "workflow_failed")
        gate = workflow.get("cloud_gate") if isinstance(workflow.get("cloud_gate"), dict) else {}
        gate_reason = str(gate.get("reason") or "")
        detail = error_code if not gate_reason else f"{error_code}:{gate_reason}"
        raise AssertionError(f"workflow blocked or failed: {detail}")
    expected_action = str(scenario.get("expect_action") or "")
    if expected_action and event.get("action") != expected_action:
        raise AssertionError(f"action expected {expected_action!r}, got {event.get('action')!r}")

    reply_text = str((event.get("decision", {}) or {}).get("reply_text") or "")
    for needle in scenario.get("expect_reply_contains", []) or []:
        if str(needle) not in reply_text:
            raise AssertionError(f"reply expected to contain {needle!r}, got {reply_text!r}")
    for needle in scenario.get("expect_reply_not_contains", []) or []:
        if str(needle) in reply_text:
            raise AssertionError(f"reply expected not to contain {needle!r}, got {reply_text!r}")

    if "expect_rule_name" in scenario:
        actual_rule = str((event.get("decision", {}) or {}).get("rule_name") or "")
        if actual_rule != str(scenario.get("expect_rule_name") or ""):
            raise AssertionError(f"rule_name expected {scenario.get('expect_rule_name')!r}, got {actual_rule!r}")

    if "expect_llm_applied" in scenario:
        applied = bool((event.get("llm_reply", {}) or {}).get("applied"))
        if applied != bool(scenario.get("expect_llm_applied")):
            raise AssertionError(f"llm applied expected {scenario.get('expect_llm_applied')!r}, got {applied!r}")

    if "expect_rag_applied" in scenario:
        applied = bool((event.get("rag_reply", {}) or {}).get("applied"))
        if applied != bool(scenario.get("expect_rag_applied")):
            raise AssertionError(f"rag applied expected {scenario.get('expect_rag_applied')!r}, got {applied!r}")

    if "expect_rag_experience_recorded" in scenario:
        recorded = bool((event.get("rag_experience", {}) or {}).get("experience_id"))
        if recorded != bool(scenario.get("expect_rag_experience_recorded")):
            raise AssertionError(f"rag experience recorded expected {scenario.get('expect_rag_experience_recorded')!r}, got {recorded!r}")

    if "expect_retrieval_mode" in scenario:
        actual_mode = str((event.get("rag_reply", {}) or {}).get("hit", {}).get("retrieval_mode") or "")
        if actual_mode != str(scenario.get("expect_retrieval_mode") or ""):
            raise AssertionError(f"retrieval mode expected {scenario.get('expect_retrieval_mode')!r}, got {actual_mode!r}")

    if "expect_intent" in scenario:
        actual_intent = str((event.get("intent_assist", {}) or {}).get("intent") or "")
        if actual_intent != str(scenario.get("expect_intent") or ""):
            raise AssertionError(f"intent expected {scenario.get('expect_intent')!r}, got {actual_intent!r}")

    if "expect_data_write" in scenario:
        write_ok = bool((event.get("data_capture", {}) or {}).get("write_result", {}).get("ok"))
        if write_ok != bool(scenario.get("expect_data_write")):
            raise AssertionError(f"data write expected {scenario.get('expect_data_write')!r}, got {write_ok!r}")

    if "expect_data_complete" in scenario:
        complete = bool((event.get("data_capture", {}) or {}).get("complete"))
        if complete != bool(scenario.get("expect_data_complete")):
            raise AssertionError(f"data complete expected {scenario.get('expect_data_complete')!r}, got {complete!r}")

    reason = str((event.get("decision", {}) or {}).get("handoff_reason") or event.get("reason") or "")
    safety_reasons = ",".join(
        str(item)
        for item in (
            (event.get("intent_assist", {}) or {})
            .get("evidence", {})
            .get("safety", {})
            .get("reasons", [])
            or []
        )
    )
    reason_text = reason + "," + safety_reasons
    for needle in scenario.get("expect_handoff_reason_contains", []) or []:
        if str(needle) not in reason_text:
            raise AssertionError(f"handoff reason expected to contain {needle!r}, got {reason_text!r}")

    if output.get("outbound") and any(item.get("verified") is False for item in output["outbound"]):
        raise AssertionError("one or more outbound sends were not verified")
    if event.get("send_result") and event.get("verified") is False:
        raise AssertionError("listener reply was not verified")


def compact_output(output: dict[str, Any]) -> dict[str, Any]:
    event = output.get("event", {}) or {}
    decision = event.get("decision", {}) or {}
    data_capture = event.get("data_capture", {}) or {}
    intent = event.get("intent_assist", {}) or {}
    safety = (intent.get("evidence", {}) or {}).get("safety", {}) or {}
    return {
        "action": event.get("action"),
        "message_ids": event.get("message_ids"),
        "reply_text": decision.get("reply_text"),
        "rule_name": decision.get("rule_name"),
        "reason": decision.get("reason"),
        "handoff_reason": decision.get("handoff_reason"),
        "data_complete": data_capture.get("complete"),
        "data_write_ok": bool(data_capture.get("write_result", {}).get("ok")),
        "intent": intent.get("intent"),
        "needs_handoff": intent.get("needs_handoff"),
        "llm_applied": bool((event.get("llm_reply", {}) or {}).get("applied")),
        "llm_reason": (event.get("llm_reply", {}) or {}).get("reason"),
        "rag_applied": bool((event.get("rag_reply", {}) or {}).get("applied")),
        "rag_reason": (event.get("rag_reply", {}) or {}).get("reason"),
        "rag_retrieval_mode": (event.get("rag_reply", {}) or {}).get("hit", {}).get("retrieval_mode"),
        "rag_experience_id": (event.get("rag_experience", {}) or {}).get("experience_id"),
        "safety": safety,
        "verified": event.get("verified"),
    }


def reset_runtime_files(config: dict[str, Any], result_path: Path) -> None:
    paths = [
        resolve_path(config.get("state_path")),
        resolve_path(config.get("audit_log_path")),
        resolve_path((config.get("operator_alert", {}) or {}).get("alert_log_path")),
        resolve_path((config.get("data_capture", {}) or {}).get("workbook_path")),
        resolve_path(result_path),
    ]
    for path in paths:
        if path and path.exists() and path.is_file():
            path.unlink()


def write_result(path: Path, payload: dict[str, Any]) -> None:
    resolved = resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
