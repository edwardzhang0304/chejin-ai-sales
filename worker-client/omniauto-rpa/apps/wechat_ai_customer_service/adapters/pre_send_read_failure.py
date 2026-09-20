"""Proof of a failed read before sending, never permission to send or retry.

The Worker owns the durable one-recheck budget. Both HTTP settlement paths
validate this same optional proof; absent legacy evidence keeps its old path.
"""
from __future__ import annotations

import re


VERSION = 1
STAGES = ("pre_send_refresh", "before_input", "before_trigger")
INPUT_STATES = ("empty", "cleared", "remaining", "unverified")
INPUT_PROGRESS = ("not_started", "may_have_started")
ERROR = "PRE_SEND_READ_FAILURE_PROOF_INVALID"


class ReadCallFailed(RuntimeError):
    """Only the screenshot/OCR call boundary may raise this typed failure."""
    def __init__(self, *, operation, reason):
        from uuid import uuid4
        super().__init__(reason)
        self.evidence = {"operation": operation, "call_status": "failed",
                         "attempt_id": uuid4().hex, "failure_reason": reason,
                         "frame_id": None, "no_frame_reason": "snapshot_read_call_raised"}


def read_call(operation, function, *args, **kwargs):
    """Tag only capture/OCR I/O exceptions, not later identity/parser failures."""
    try:
        return function(*args, **kwargs)
    except ReadCallFailed:
        raise
    except Exception as exc:
        raise ReadCallFailed(operation=operation, reason=repr(exc)) from exc


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def replacement_input_ready(value: object, *, context=None, target=None) -> bool:
    """An owned draft was cleared once; fresh input must still replace/read back.

    This optional fact lives inside the existing open failure object. It does
    not change input_state to empty/cleared or grant a send/retry budget.
    """
    from .send_interruption import confirmed_program_draft_cleanup
    if not isinstance(value, dict):
        return False
    fact = value.get("program_draft_cleanup")
    if not isinstance(fact, dict):
        return False
    cleanup = fact.get("cleanup")
    if not isinstance(cleanup, dict) or not (
        value.get("stage") == "before_trigger"
        and value.get("physical_send_triggered") is False
        and value.get("action_phase") == "not_attempted"
        and value.get("phase_proof") == {"ok": True, "source": "action_journal", "action_phase": "not_attempted"}
        and value.get("input_progress") == "may_have_started"
        and value.get("input_state") == "unverified"
        and cleanup.get("clear_attempted") is True
        and cleanup.get("cleared") is False
        and confirmed_program_draft_cleanup(cleanup)
        and all(_text(fact.get(k)) for k in ("target", "reply_action_id", "conversation_id"))
        and re.fullmatch(r"[0-9a-f]{64}", str(fact.get("reply_text_hash") or ""))
    ):
        return False
    if context is not None and any(fact[k] != context.get(k) for k in
                                  ("reply_action_id", "conversation_id", "reply_text_hash")):
        return False
    return target is None or fact["target"] == target


def read_failure_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    phase = value.get("phase_proof")
    if not isinstance(phase, dict):
        return False
    return bool(
        value.get("stage") in STAGES
        and value.get("operation") in ("capture", "read")
        and value.get("call_status") == "failed"
        and _text(value.get("attempt_id"))
        and _text(value.get("error_code"))
        and _text(value.get("failure_reason"))
        and value.get("physical_send_triggered") is False
        and value.get("action_phase") == "not_attempted"
        and phase.get("ok") is True and phase.get("action_phase") == "not_attempted"
        and phase.get("source") in ("action_journal", "read_only_before_claim")
        and (phase.get("source") != "read_only_before_claim" or value.get("stage") == "pre_send_refresh")
        and value.get("input_state") in INPUT_STATES
        and ("program_draft_cleanup" not in value or replacement_input_ready(value))
        and ("input_progress" not in value or value["input_progress"] in INPUT_PROGRESS)
        and (_text(value.get("frame_id")) or (
            value.get("frame_id") is None and _text(value.get("no_frame_reason"))
        ))
    )


def validate_proof(value: object) -> dict:
    """Reject a malformed *present* extension, never silently treat it as old."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError(ERROR)
    if any(not _text(value.get(key)) for key in (
        "reply_action_id", "task_id", "conversation_id", "flow_id", "authorization_revision",
    )) or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("reply_text_hash") or "")):
        raise ValueError(ERROR)
    first, recheck = value.get("first_failure"), value.get("recheck")
    if not read_failure_valid(first) or not isinstance(recheck, dict):
        raise ValueError(ERROR)
    if type(recheck.get("started")) is not bool or recheck.get("budget_state") not in ("consumed", "persist_failed"):
        raise ValueError(ERROR)
    if recheck["started"] and recheck["budget_state"] != "consumed":
        raise ValueError(ERROR)
    terminal = value.get("terminal_phase_proof")
    if not isinstance(terminal, dict) or not (
        terminal.get("ok") is True
        and terminal.get("action_phase") == "not_attempted"
        and terminal.get("source") in ("action_journal", "read_only_before_claim")
        and (terminal.get("source") != "read_only_before_claim" or first["stage"] == "pre_send_refresh")
    ):
        raise ValueError(ERROR)
    failure = recheck.get("failure")
    if failure is not None and (not recheck["started"] or not read_failure_valid(failure)
                                or failure["attempt_id"] == first["attempt_id"]):
        raise ValueError(ERROR)
    for observed in (first, failure):
        if isinstance(observed, dict) and "program_draft_cleanup" in observed:
            if not replacement_input_ready(observed, context=value):
                raise ValueError(ERROR)
    if value.get("outcome") == "exhausted":
        if not recheck["started"] or not read_failure_valid(failure) or failure["attempt_id"] == first["attempt_id"]:
            raise ValueError(ERROR)
    elif value.get("outcome") == "interrupted":
        if not _text(recheck.get("interruption_reason")):
            raise ValueError(ERROR)
        if failure is not None and not read_failure_valid(failure):
            raise ValueError(ERROR)
    else:
        raise ValueError(ERROR)
    if value.get("input_state") not in INPUT_STATES:
        raise ValueError(ERROR)
    if "input_progress" in value and value["input_progress"] not in INPUT_PROGRESS:
        raise ValueError(ERROR)
    return value


def empty_input_observation(value: object, *, target: str, request_id: str) -> bool:
    """Positive, request-bound proof; missing OCR/layout is never blank input."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION:
        return False
    if (value.get("status") != "empty" or value.get("target") != target
            or value.get("request_id") != request_id or not _text(target) or not _text(request_id)):
        return False
    frame, guard, region = (value.get(key) for key in ("frame_observation", "target_confirmation", "input_region"))
    if not all(isinstance(item, dict) for item in (frame, guard, region)):
        return False
    dimensions = value.get("image_size")
    bounds = region.get("bounds")
    if (not isinstance(dimensions, list) or len(dimensions) != 2
            or not all(type(item) is int and item > 0 for item in dimensions)
            or not isinstance(bounds, list) or len(bounds) != 4
            or not all(type(item) is int for item in bounds)
            or not (0 <= bounds[0] < bounds[2] <= dimensions[0]
                    and 0 <= bounds[1] < bounds[3] <= dimensions[1])):
        return False
    admission = guard.get("conversation_type_evidence")
    return bool(
        _text(frame.get("frame_id"))
        and re.fullmatch(r"[0-9a-f]{64}", str(frame.get("screenshot_sha256") or ""))
        and _text(frame.get("screenshot_path"))
        and type(frame.get("hwnd")) is int and frame["hwnd"] > 0
        and guard.get("ok") is True and not guard.get("blind_send")
        and guard.get("confirmation_confidence") == "active_title_strict"
        and guard.get("requested_target") == target
        and guard.get("conversation_type") == "private"
        and isinstance(admission, dict) and admission.get("short_code_confirmed") is True
        and not region.get("error") and not region.get("error_code")
        and region.get("has_visible_text") is False and region.get("reason") == "input_region_blank"
    )


# Machine-readable shape of this optional extension. Behavioural
# constraints (ownership, permit, budget ordering) also require validate_proof
# and the database transaction; JSON shape alone is not permission.
_TEXT = {"type": "string", "pattern": "\\S"}
PHASE_SCHEMA = {
    "type": "object", "required": ["source", "ok", "action_phase"],
    "properties": {"source": {"enum": ["action_journal", "read_only_before_claim"]},
                   "ok": {"const": True}, "action_phase": {"const": "not_attempted"}},
}
FAILURE_SCHEMA = {
    "type": "object", "required": [
        "stage", "operation", "call_status", "attempt_id", "error_code", "failure_reason",
        "physical_send_triggered", "action_phase", "phase_proof", "input_state", "frame_id",
    ],
    "properties": {
        "stage": {"enum": list(STAGES)}, "operation": {"enum": ["capture", "read"]},
        "call_status": {"const": "failed"},
        **{key: _TEXT for key in ("attempt_id", "error_code", "failure_reason", "no_frame_reason")},
        "physical_send_triggered": {"const": False}, "action_phase": {"const": "not_attempted"},
        "phase_proof": PHASE_SCHEMA, "input_state": {"enum": list(INPUT_STATES)},
        "input_progress": {"enum": list(INPUT_PROGRESS)},
        "frame_id": {"anyOf": [_TEXT, {"type": "null"}]},
    },
    "allOf": [
        {"if": {"properties": {"frame_id": {"type": "null"}}}, "then": {"required": ["no_frame_reason"]}},
        {"if": {"properties": {"phase_proof": {"properties": {"source": {"const": "read_only_before_claim"}}}}},
         "then": {"properties": {"stage": {"const": "pre_send_refresh"}}}},
    ],
}
PROOF_SCHEMA = {
    "type": "object", "required": [
        "version", "reply_action_id", "task_id", "conversation_id", "flow_id",
        "authorization_revision", "reply_text_hash", "first_failure", "recheck",
        "outcome", "terminal_phase_proof", "input_state",
    ],
    "properties": {
        "version": {"type": "integer", "const": VERSION},
        **{key: _TEXT for key in (
            "reply_action_id", "task_id", "conversation_id", "flow_id", "authorization_revision",
        )},
        "reply_text_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "outcome": {"enum": ["exhausted", "interrupted"]},
        "input_state": {"enum": list(INPUT_STATES)},
        "input_progress": {"enum": list(INPUT_PROGRESS)},
        "first_failure": FAILURE_SCHEMA,
        "terminal_phase_proof": PHASE_SCHEMA,
        "recheck": {"type": "object", "required": ["budget_state", "started"], "properties": {
            "budget_state": {"enum": ["consumed", "persist_failed"]},
            "started": {"type": "boolean"},
            "failure": {"anyOf": [FAILURE_SCHEMA, {"type": "null"}]},
            "interruption_reason": _TEXT,
        }, "allOf": [
            {"if": {"properties": {"started": {"const": True}}},
             "then": {"properties": {"budget_state": {"const": "consumed"}}},
             "else": {"properties": {"failure": {"type": "null"}}}},
        ]},
    },
    "allOf": [
        {"if": {"properties": {"outcome": {"const": "exhausted"}}},
         "then": {"properties": {"recheck": {"required": ["failure"], "properties": {
             "started": {"const": True}, "failure": FAILURE_SCHEMA}}}},
         "else": {"properties": {"recheck": {"required": ["interruption_reason"]}}}},
        {"if": {"properties": {"terminal_phase_proof": {"properties": {"source": {"const": "read_only_before_claim"}}}}},
         "then": {"properties": {"first_failure": {"properties": {"stage": {"const": "pre_send_refresh"}}}}}},
    ],
}

INPUT_OBSERVATION_SCHEMA = {
    "type": "object", "required": ["version", "request_id", "target", "status",
                                     "frame_observation", "target_confirmation", "input_region"],
    "properties": {
        "version": {"type": "integer", "const": VERSION},
        "request_id": _TEXT, "target": _TEXT,
        "status": {"enum": ["empty", "not_empty", "unverified", "target_not_observed"]},
        "frame_observation": {"type": ["object", "null"]},
        "target_confirmation": {"type": "object"}, "input_region": {"type": "object"},
        "image_size": {"type": "array", "items": {"type": "integer", "minimum": 1},
                       "minItems": 2, "maxItems": 2},
    },
    "allOf": [{
        "if": {"properties": {"status": {"const": "empty"}}},
        "then": {"required": ["image_size"], "properties": {
            "frame_observation": {"type": "object", "required": ["frame_id", "screenshot_sha256", "screenshot_path", "hwnd"]},
            "input_region": {"required": ["bounds", "has_visible_text", "reason"], "properties": {
                "has_visible_text": {"const": False}, "reason": {"const": "input_region_blank"}}},
        }},
    }],
}
