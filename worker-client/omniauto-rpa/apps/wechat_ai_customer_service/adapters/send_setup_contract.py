"""Pure SP request/proof validation shared by the two hosts; no UI or storage."""
from __future__ import annotations

import hashlib
import hmac
import json
import re

ERROR = "PRE_SEND_SETUP_FAILURE_PROOF_INVALID"


def _required_text(value):
    return isinstance(value, str) and bool(value.strip())


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % size, value) is not None


def validate_proof(value, *, contract):
    rules = contract["pre_send_setup_recovery_contract"]
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError(ERROR)
    for key in rules["proof_schema"]["required_fields"]:
        if key not in value:
            raise ValueError(ERROR)
    if any(not _required_text(value.get(k)) for k in (
        "task_id", "reply_action_id", "conversation_id", "flow_id", "authorization_revision")):
        raise ValueError(ERROR)
    if not all(_hex(value.get(k), 32) for k in ("request_id", "launch_attempt_id")) or not _hex(value.get("reply_text_hash"), 64):
        raise ValueError(ERROR)
    state = rules["error_process_states"].get(value.get("error_code"))
    if state is None or state != value.get("process_state"):
        raise ValueError(ERROR)
    if not (_hex(value.get("request_sha256"), 64) or (state == "not_called" and value.get("request_sha256") is None)):
        raise ValueError(ERROR)
    if value.get("action_phase") != "not_attempted" or value.get("physical_send_triggered") is not False:
        raise ValueError(ERROR)
    terminal = value.get("terminal_phase_proof")
    if not isinstance(terminal, dict) or not (
        terminal.get("source") == "action_journal" and terminal.get("ok") is True
        and terminal.get("action_phase") == "not_attempted"
        and terminal.get("transaction_id") == terminal.get("canonical_action_id") == value["reply_action_id"]
        and terminal.get("launch_attempt_id") == value["launch_attempt_id"]
        and terminal.get("request_id") == value["request_id"]
        and terminal.get("process_state") == state
        and terminal.get("physical_send_triggered") is False
        and _hex(terminal.get("journal_sha256"), 64)
    ):
        raise ValueError(ERROR)
    return value


def package_bytes(*, request_id, task_id, reply_action_id, target, text, expected_context_guard):
    if not _hex(request_id, 32) or not all(_required_text(v) for v in (task_id, reply_action_id, target)):
        raise ValueError("SEND_REQUEST_IDENTITY_INVALID")
    if not isinstance(text, str) or not isinstance(expected_context_guard, dict):
        raise ValueError("SEND_REQUEST_CONTENT_INVALID")
    return json.dumps({"schema_version": 1, "request_id": request_id, "task_id": task_id,
        "reply_action_id": reply_action_id, "target": target,
        "reply_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "expected_context_guard": expected_context_guard}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_package(raw, *, filename, sha256, task_id, reply_action_id, target, text):
    """Hash and parse the same bytes. Never normalize the supplied send text."""
    if not isinstance(raw, bytes) or not _hex(sha256, 64) or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), sha256):
        raise ValueError("SEND_REQUEST_DIGEST_INVALID")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("SEND_REQUEST_SCHEMA_INVALID")
    if not _hex(value.get("request_id"), 32) or filename != value["request_id"]+".json":
        raise ValueError("SEND_REQUEST_IDENTITY_INVALID")
    if not all(_required_text(v) for v in (task_id, reply_action_id, target)) or not isinstance(text, str):
        raise ValueError("SEND_REQUEST_BINDING_INVALID")
    if any(value.get(k) != v for k, v in {"task_id": task_id, "reply_action_id": reply_action_id,
        "target": target, "reply_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}.items()):
        raise ValueError("SEND_REQUEST_BINDING_INVALID")
    if not isinstance(value.get("expected_context_guard"), dict):
        raise ValueError("SEND_REQUEST_GUARD_INVALID")
    return value


def journal_not_triggered(journal, *, action_id):
    """Physical phase only; launch history is checked separately by its owner."""
    if not isinstance(journal, dict) or journal.get("transaction_id") != action_id or journal.get("canonical_action_id") != action_id:
        return False
    items = journal.get("items")
    return bool(journal.get("action_kind") == "send" and journal.get("action_phase") == "not_attempted"
        and isinstance(items, dict) and items and all(isinstance(i, dict) and i.get("action_phase") == "not_attempted"
            and not i.get("business_result_confirmed") for i in items.values()))
