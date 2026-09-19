"""Optional send-file admission, called before window probing or other UI."""
from __future__ import annotations

from . import send_request_file as files, send_launch_journal as launches
from .send_setup_contract import validate_package, journal_not_triggered


def remember_cli_presence(args, argv):
    flags = {str(item).split("=", 1)[0] for item in argv}
    args._send_file_arguments_supplied = bool(flags.intersection({
        "--expected-context-guard-file", "--expected-context-guard-sha256", "--send-task-id", "--send-action-id"}))
    args._expected_context_guard_supplied = "--expected-context-guard" in flags


def admit(args):
    names = ("expected_context_guard_file", "expected_context_guard_sha256", "send_task_id", "send_action_id")
    values = {key: getattr(args, key, "") for key in names}
    if not any(values.values()) and not getattr(args, "_send_file_arguments_supplied", False):
        return None, None
    attempt = None
    try:
        if (args.action != "send" or not all(values.values()) or getattr(args, "expected_context_guard", "")
                or getattr(args, "_expected_context_guard_supplied", False)):
            raise ValueError("SEND_REQUEST_ARGUMENT_CONFLICT")
        path = getattr(args, "action_journal", "")
        journal, _ = launches.read(path)
        attempts = journal.get("send_launch_attempts") or []
        context = (journal.get("prepare_evidence") or {}).get("pre_send_setup_context") or {}
        if (not journal_not_triggered(journal, action_id=values["send_action_id"])
                or context.get("task_id") != values["send_task_id"] or not attempts
                or not launches.earlier_attempts_clear(attempts[:-1])):
            raise ValueError("SEND_REQUEST_ACTION_JOURNAL_CONFLICT")
        candidate = attempts[-1]
        ref = candidate.get("request") or {}
        resolved = files.normalized_path(values["expected_context_guard_file"])
        if (candidate.get("process_state") != "creating" or ref.get("path") != str(resolved)
                or ref.get("sha256") != values["expected_context_guard_sha256"]
                or ref.get("request_id") != candidate.get("request_id")):
            raise ValueError("SEND_REQUEST_LAUNCH_CONFLICT")
        attempt = candidate
        resolved, raw = files.read_package(resolved)
        package = validate_package(raw, filename=resolved.name, sha256=values["expected_context_guard_sha256"],
            task_id=values["send_task_id"], reply_action_id=values["send_action_id"],
            target=args.target, text=args.text)
        if context.get("reply_text_hash") != package["reply_text_sha256"]:
            raise ValueError("SEND_REQUEST_ACTION_TEXT_CONFLICT")
        return package["expected_context_guard"], None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # A malformed path/action cannot mutate some other original journal.
        # Bound file corruption can be settled using this pre-UI rejection.
        if attempt is not None:
            launches.fail(path, attempt["launch_attempt_id"], error_code="RPA_SEND_REQUEST_INVALID",
                          process_state="rejected_before_ui", reason=type(exc).__name__ + ":" + str(exc))
        return None, {"ok": False, "error_code": "RPA_SEND_REQUEST_INVALID",
                      "state": "send_request_rejected_before_ui", "ui_action_performed": False,
                      "action_phase": "not_attempted", "physical_send_triggered": False,
                      "request_id": attempt.get("request_id") if attempt else None,
                      "reason": str(exc)}
