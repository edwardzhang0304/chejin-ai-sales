"""An observed identity mismatch before input; never a fabricated OCR call failure."""
import re

FIELD = "historical_text_correction_pending"
ERROR = "HISTORICAL_TEXT_CORRECTION_PENDING"
PROPOSAL_FIELDS = ("message_event_id", "source_message_key", "original_read_run_id",
    "original_observation_id", "original_text_sha256", "expected_effective_version", "proof_sha256")


def validate_proof(value):
    def require(condition):
        if not condition:
            raise ValueError("HISTORICAL_TEXT_CORRECTION_PENDING_INVALID")
    require(isinstance(value, dict) and set(value) == {"version", "reply_action_id", "task_id",
        "conversation_id", "flow_id", "authorization_revision", "reply_text_hash", "read_observation",
        "proposal", "terminal_phase_proof", "input_progress", "physical_send_triggered"})
    require(type(value["version"]) is int and value["version"] == 1)
    for field in ("reply_action_id", "task_id", "conversation_id", "flow_id", "authorization_revision"):
        require(isinstance(value[field], str) and 0 < len(value[field]) <= 255)
    require(bool(re.fullmatch(r"[0-9a-f]{64}", str(value["reply_text_hash"]))))
    require(value["input_progress"] == "not_started" and value["physical_send_triggered"] is False)
    observed = value["read_observation"]
    require(isinstance(observed, dict) and set(observed) == {"call_status", "frame_id", "observation_id", "identity_error_code"})
    require(observed["call_status"] == "succeeded")
    require(all(isinstance(observed[k], str) and 0 < len(observed[k]) <= 255
                for k in ("frame_id", "observation_id", "identity_error_code")))
    proposal = value["proposal"]
    require(isinstance(proposal, dict) and set(proposal) == set(PROPOSAL_FIELDS))
    require(type(proposal["expected_effective_version"]) is int and proposal["expected_effective_version"] >= 0)
    for field in PROPOSAL_FIELDS:
        if field != "expected_effective_version":
            require(isinstance(proposal[field], str) and 0 < len(proposal[field]) <= 255)
    require(all(re.fullmatch(r"[0-9a-f]{64}", proposal[k]) for k in ("proof_sha256", "original_text_sha256")))
    phase = value["terminal_phase_proof"]
    require(isinstance(phase, dict) and phase.get("ok") is True
            and phase.get("action_phase") == "not_attempted"
            and phase.get("source") in {"read_only_before_claim", "action_journal"})
    return value
