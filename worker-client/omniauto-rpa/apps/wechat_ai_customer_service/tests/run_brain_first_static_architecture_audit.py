"""Static architecture audit for Brain First reply ownership.

This is intentionally contract-oriented. It does not ban internal audit fields
such as OCR/RPA/session_key; it verifies that those layers cannot become a
customer-visible answer path when Brain First is enabled.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADMIN_SERVICES_ROOT = APP_ROOT / "admin_backend" / "services"
for path in (PROJECT_ROOT, APP_ROOT, WORKFLOWS_ROOT, ADMIN_SERVICES_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@dataclass
class CaseResult:
    name: str
    ok: bool
    details: dict[str, Any]


def main() -> int:
    results = [
        check_brain_runner_marks_only_brain_segments_visible(),
        check_brain_failure_blocks_without_local_fallback(),
        check_guard_has_no_visible_handoff_ack_path(),
        check_scheduler_blocks_non_brain_ready_replies_before_send(),
        check_listen_and_reply_disables_legacy_generators_for_brain_first(),
        check_legacy_intent_assist_is_advisory_only(),
        check_realtime_router_is_archived_as_advisory(),
        check_only_brain_first_mode_is_adoptable(),
        check_no_forbidden_visible_reply_source_literals(),
    ]
    failures = [result for result in results if not result.ok]
    payload = {
        "ok": not failures,
        "failures": [failure.__dict__ for failure in failures],
        "results": [result.__dict__ for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_brain_runner_marks_only_brain_segments_visible() -> CaseResult:
    path = WORKFLOWS_ROOT / "customer_service_brain.py"
    tree = parse(path)
    text = read(path)
    visible_sources = literal_values_assigned_to_key(tree, "visible_reply_source")
    ok = (
        "brain_plan.reply_segments" in visible_sources
        and "none" in visible_sources
        and not any(str(value).startswith(("guard", "quality", "legacy", "final_polish")) for value in visible_sources)
        and 'visible_reply_owner = "brain_repair"' in text
        and '"visible_reply_owner": "none_brain_unavailable"' in text
        and '"reply_text": plan_reply_text' in text
        and '"visible_reply_source": "brain_plan.reply_segments"' in text
    )
    return CaseResult(
        "brain_runner_marks_only_brain_segments_visible",
        ok,
        {"visible_sources": sorted(map(str, visible_sources))},
    )


def check_brain_failure_blocks_without_local_fallback() -> CaseResult:
    path = WORKFLOWS_ROOT / "customer_service_brain.py"
    text = read(path)
    required = [
        "def build_brain_no_visible_reply_payload",
        '"reply_text": ""',
        '"raw_reply_text": ""',
        '"customer_visible_reply_blocked": True',
        '"brain_required_no_visible_fallback": True',
        '"visible_reply_source": "none"',
    ]
    ok = all(token in text for token in required)
    return CaseResult("brain_failure_blocks_without_local_fallback", ok, {"missing": [token for token in required if token not in text]})


def check_guard_has_no_visible_handoff_ack_path() -> CaseResult:
    path = WORKFLOWS_ROOT / "llm_reply_guard.py"
    text = read(path)
    tree = parse(path)
    customer_visible_sources = literal_values_for_reply_source_fields(tree)
    forbidden = {"guard_handoff_ack", "quality_gate.safe_fallback", "semantic_reviewer.reply"}
    ok = (
        not (forbidden & {str(value) for value in customer_visible_sources})
        and '"customer_visible_reply_source": "brain_plan.reply_segments"' in text
        and 'customer_visible_reply_source="none_guard_reviewer_only"' in text
        and "Guard 只负责审稿，不直接生成客户可见回复。" in text
    )
    return CaseResult(
        "guard_has_no_visible_handoff_ack_path",
        ok,
        {"customer_visible_sources": sorted(map(str, customer_visible_sources))},
    )


def check_scheduler_blocks_non_brain_ready_replies_before_send() -> CaseResult:
    path = ADMIN_SERVICES_ROOT / "customer_service_scheduler.py"
    text = read(path)
    consume_index = text.find("def _consume_send_queue")
    ownership_index = text.find("brain_first_ready_reply_ownership_failure", consume_index)
    envelope_index = text.find("ready_reply_session_envelope_failure", consume_index)
    send_index = text.find("send_result = self.send_fn", consume_index)
    ok = consume_index >= 0 and ownership_index > consume_index and envelope_index > ownership_index and send_index > envelope_index
    return CaseResult(
        "scheduler_blocks_non_brain_ready_replies_before_send",
        ok,
        {
            "consume_index": consume_index,
            "ownership_index": ownership_index,
            "envelope_index": envelope_index,
            "send_index": send_index,
        },
    )


def check_listen_and_reply_disables_legacy_generators_for_brain_first() -> CaseResult:
    path = WORKFLOWS_ROOT / "listen_and_reply.py"
    text = read(path)
    required = [
        "def config_with_legacy_reply_generators_disabled_for_brain",
        'rag["enabled"] = False',
        'realtime["enabled"] = False',
        'synthesis["enabled"] = False',
        "brain_first_intent_assist_advisory_only",
        "def block_for_customer_service_brain_no_visible_reply",
        '"customer_visible_reply_blocked"] = True',
        '"reply_text": ""',
        '"raw_reply_text": ""',
    ]
    ok = all(token in text for token in required)
    return CaseResult("listen_and_reply_disables_legacy_generators_for_brain_first", ok, {"missing": [token for token in required if token not in text]})


def check_legacy_intent_assist_is_advisory_only() -> CaseResult:
    path = WORKFLOWS_ROOT / "customer_intent_assist.py"
    text = read(path)
    forbidden = [
        "suggested_reply 就写最终可发送话术",
        "suggested_reply 写请示上级/人工接管的简短话术",
    ]
    required = [
        "legacy ``suggested_reply`` field is only an advisory sample",
        "不是最终客户可见话术",
        "只写给 Brain 参考的 advisory 样例",
    ]
    ok = all(token in text for token in required) and not any(token in text for token in forbidden)
    return CaseResult(
        "legacy_intent_assist_is_advisory_only",
        ok,
        {
            "missing": [token for token in required if token not in text],
            "forbidden_present": [token for token in forbidden if token in text],
        },
    )


def check_realtime_router_is_archived_as_advisory() -> CaseResult:
    path = WORKFLOWS_ROOT / "realtime_reply_router.py"
    text = read(path)
    forbidden = [
        "Deterministic facts, handoff",
        "boundaries, greetings, and high-frequency recommendation patterns stay local",
    ]
    required = [
        "Legacy/advisory fast routing",
        "must not own or finalize",
        "customer_service_brain",
        "legacy-mode",
    ]
    ok = all(token in text for token in required) and not any(token in text for token in forbidden)
    return CaseResult(
        "realtime_router_is_archived_as_advisory",
        ok,
        {
            "missing": [token for token in required if token not in text],
            "forbidden_present": [token for token in forbidden if token in text],
        },
    )


def check_only_brain_first_mode_is_adoptable() -> CaseResult:
    path = WORKFLOWS_ROOT / "customer_service_brain.py"
    text = read(path)
    forbidden = [
        'payload["mode"] in {"brain_first", "hybrid_shadow"}',
        '"adoptable": payload["mode"] in',
    ]
    required = [
        '"adoptable": payload["mode"] == "brain_first"',
    ]
    ok = all(token in text for token in required) and not any(token in text for token in forbidden)
    return CaseResult(
        "only_brain_first_mode_is_adoptable",
        ok,
        {
            "missing": [token for token in required if token not in text],
            "forbidden_present": [token for token in forbidden if token in text],
        },
    )


def check_no_forbidden_visible_reply_source_literals() -> CaseResult:
    roots = [
        WORKFLOWS_ROOT,
        ADMIN_SERVICES_ROOT,
        APP_ROOT / "scripts",
    ]
    forbidden = {
        "guard_handoff_ack",
        "quality_gate.safe_fallback",
        "semantic_reviewer.reply",
        "final_polish.reply",
        "legacy_advisory.reply",
        "code_mechanism.status_text",
    }
    offenders: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = parse(path)
            for value in literal_values_for_reply_source_fields(tree):
                term = str(value)
                if term in forbidden:
                    offenders.append({"file": str(path), "term": term})
    return CaseResult("no_forbidden_visible_reply_source_literals", not offenders, {"offenders": offenders[:20]})


def literal_values_assigned_to_key(tree: ast.AST, key_name: str) -> set[Any]:
    values: set[Any] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == key_name:
                try:
                    values.add(ast.literal_eval(value))
                except Exception:
                    values.add(f"<non_literal:{value.__class__.__name__}>")
    return values


def literal_values_for_reply_source_fields(tree: ast.AST) -> set[Any]:
    keys = {"visible_reply_source", "customer_visible_reply_source"}
    values: set[Any] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in keys:
                    try:
                        values.add(ast.literal_eval(value))
                    except Exception:
                        values.add(f"<non_literal:{value.__class__.__name__}>")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in keys:
                    try:
                        values.add(ast.literal_eval(keyword.value))
                    except Exception:
                        values.add(f"<non_literal:{keyword.value.__class__.__name__}>")
    return values


def parse(path: Path) -> ast.Module:
    return ast.parse(read(path), filename=str(path))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
