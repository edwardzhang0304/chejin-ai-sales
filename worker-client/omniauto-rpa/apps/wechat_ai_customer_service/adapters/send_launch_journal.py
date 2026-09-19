"""Launch facts in the existing action journal, never a resend queue.

The parent writes before creation and after exit. The child may only reject
its own request before UI. No parent journal writes race a running child.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .send_setup_contract import journal_not_triggered, validate_proof


def read(path):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("SEND_LAUNCH_JOURNAL_INVALID")
    return value, hashlib.sha256(raw).hexdigest()


def _write(path, journal):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".launch-" + str(os.getpid()))
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def earlier_attempts_clear(attempts):
    return all(isinstance(a, dict) and a.get("process_state") == "finished"
               and a.get("action_phase") == "not_attempted"
               and a.get("physical_send_triggered") is False for a in attempts)


def begin(path, *, task_id, action_id):
    journal, _ = read(path)
    attempts = journal.get("send_launch_attempts", [])
    context = (journal.get("prepare_evidence") or {}).get("pre_send_setup_context") or {}
    if (not journal_not_triggered(journal, action_id=action_id)
            or not isinstance(attempts, list) or not earlier_attempts_clear(attempts)
            or context.get("task_id") != task_id or context.get("reply_action_id") != action_id):
        raise ValueError("SEND_LAUNCH_ORIGINAL_ACTION_UNRESOLVED")
    attempt = {"launch_attempt_id": uuid4().hex, "request_id": uuid4().hex,
               "process_state": "preparing", "request": None}
    journal["send_launch_attempts"] = [*attempts, attempt]
    _write(path, journal)
    return dict(attempt)


def update(path, attempt_id, *, allowed, **fields):
    journal, _ = read(path)
    attempts = journal.get("send_launch_attempts") or []
    if (not attempts or attempts[-1].get("launch_attempt_id") != attempt_id
            or attempts[-1].get("process_state") not in allowed):
        raise ValueError("SEND_LAUNCH_TRANSITION_INVALID")
    attempts[-1] = {**attempts[-1], **fields}
    _write(path, journal)
    return attempts[-1]


def fail(path, attempt_id, *, error_code, process_state, reason):
    # Called only before Popen, in its except block, or by pre-UI admission.
    allowed = {"not_called": {"preparing", "prepared"},
               "create_failed": {"creating"}, "rejected_before_ui": {"creating"}}[process_state]
    return update(path, attempt_id, allowed=allowed, error_code=error_code,
                  process_state=process_state, failure_reason=reason,
                  action_phase="not_attempted", physical_send_triggered=False)


def proof(path, *, contract):
    journal, digest = read(path)
    attempts = journal.get("send_launch_attempts") or []
    if not attempts:
        return None
    attempt = attempts[-1]
    context = (journal.get("prepare_evidence") or {}).get("pre_send_setup_context") or {}
    if (not journal_not_triggered(journal, action_id=context.get("reply_action_id"))
            or not earlier_attempts_clear(attempts[:-1])):
        return None
    mapping = (contract.get("pre_send_setup_recovery_contract") or {}).get("error_process_states", {})
    if mapping.get(attempt.get("error_code")) != attempt.get("process_state"):
        return None
    reference = attempt.get("request") or {}
    value = {"version": 1, **context,
             "launch_attempt_id": attempt["launch_attempt_id"], "request_id": attempt["request_id"],
             "request_sha256": reference.get("sha256"),
             "error_code": attempt["error_code"], "process_state": attempt["process_state"],
             "action_phase": "not_attempted", "physical_send_triggered": False,
             "terminal_phase_proof": {"source": "action_journal", "ok": True,
                 "transaction_id": journal["transaction_id"], "canonical_action_id": journal["canonical_action_id"],
                 "launch_attempt_id": attempt["launch_attempt_id"], "request_id": attempt["request_id"],
                 "process_state": attempt["process_state"], "action_phase": "not_attempted",
                 "physical_send_triggered": False, "journal_sha256": digest}}
    return validate_proof(value, contract=contract)


def record_request(path, attempt_id, reference):
    journal, _ = read(path)
    attempt = journal["send_launch_attempts"][-1]
    if reference["request_id"] != attempt["request_id"]:
        raise ValueError("SEND_REQUEST_IDENTITY_INVALID")
    return update(path, attempt_id, allowed={"preparing"},
                  request_files=[*attempt.get("request_files", []), dict(reference)])


def journal_references(journal):
    refs = {}
    for attempt in journal.get("send_launch_attempts", []):
        for ref in [*attempt.get("request_files", []), attempt.get("request")]:
            if isinstance(ref, dict):
                refs[(ref["path"], ref["sha256"])] = dict(ref)
    return list(refs.values())


def references(path):
    journal, _ = read(path)
    return journal_references(journal)


def failure_result(path, *, contract):
    value = proof(path, contract=contract)
    if value is None:
        raise ValueError("SEND_LAUNCH_FAILURE_NOT_PROVEN")
    return {"ok": False, "state": "send_setup_failed", "error_code": value["error_code"],
            "action_phase": "not_attempted", "physical_send_triggered": False,
            "pre_send_setup_failure": value, "send_request_files": references(path),
            "send_result": {"ok": False, "confirmed": False, "result": "failed",
                            "action_phase": "not_attempted", "physical_send_triggered": False}}
