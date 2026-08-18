from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.c2 import c2_contract_v3, recovery_action_for_error
from app.schemas.c3 import ReplyActionSentAckRequest


def _ack_payload(*, send_result: str, action_phase: str) -> dict:
    return {
        "send_token": "send-token",
        "task_id": "task-id",
        "worker_id": "worker-id",
        "send_result": send_result,
        "action_phase": action_phase,
    }


def test_backend_recovery_actions_are_owned_by_the_machine_contract():
    contract = c2_contract_v3()["outbox_recovery_contract"]
    code_groups = (
        ("identity_quarantined_codes", "identity_quarantined"),
        ("refresh_and_rebuild_codes", "refresh_and_rebuild"),
        ("split_and_retry_codes", "split_and_retry"),
        ("target_terminated_codes", "target_terminated"),
        ("conversation_terminated_codes", "conversation_terminated"),
        ("capability_paused_codes", "capability_paused"),
    )
    for field, action in code_groups:
        for code in contract[field]:
            assert recovery_action_for_error(code, 409) == action
    for status in contract["retry_statuses"]:
        assert recovery_action_for_error("TRANSIENT", status) == "retry"


def test_generic_validation_failure_pauses_instead_of_claiming_a_terminal():
    assert (
        recovery_action_for_error("VALIDATION_ERROR", 400)
        == "capability_paused"
    )


def test_backend_uses_the_shared_image_menu_failure_code():
    image_contract = c2_contract_v3()["image_contract"]
    assert "C2_IMAGE_MENU_OPERATION_FAILED" in image_contract["error_codes"]
    assert (
        image_contract["failure_reason_to_error_code"][
            "C2_IMAGE_MENU_OPERATION_FAILED"
        ]
        == "C2_IMAGE_MENU_OPERATION_FAILED"
    )
    assert (
        image_contract["failure_reason_to_error_code"][
            "clipboard_image_fingerprint_mismatch"
        ]
        == "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED"
    )


def test_backend_uses_shared_flow_gate_action_contract():
    contract = c2_contract_v3()["flow_gate_action_contract"]
    assert contract["classes"] == [
        "non_blocking_warning",
        "item_handoff",
        "recoverable_hold",
        "hard_stop",
    ]
    assert contract["customer_media_failure"] == (
        "settle_each_failed_fact_then_handoff_without_brain_or_automatic_clarification"
    )
    assert contract["self_media_failure"] == (
        "persist_warning_and_continue_latest_complete_customer_tail"
    )
    assert contract["high_intent_reason_code"] == "CUSTOMER_HIGH_INTENT"


@pytest.mark.parametrize(
    ("send_result", "action_phase"),
    [
        ("sent", "confirmed"),
        ("failed", "not_attempted"),
        ("unknown", "trigger_attempted"),
    ],
)
def test_sent_ack_accepts_only_the_contract_legal_combinations(
    send_result: str,
    action_phase: str,
):
    parsed = ReplyActionSentAckRequest.model_validate(
        _ack_payload(
            send_result=send_result,
            action_phase=action_phase,
        )
    )
    assert parsed.send_result == send_result
    assert parsed.action_phase == action_phase


@pytest.mark.parametrize(
    ("send_result", "action_phase"),
    [
        ("sent", "trigger_attempted"),
        ("failed", "confirmed"),
        ("unknown", "not_attempted"),
    ],
)
def test_sent_ack_rejects_cross_phase_guessing(
    send_result: str,
    action_phase: str,
):
    with pytest.raises(ValidationError):
        ReplyActionSentAckRequest.model_validate(
            _ack_payload(
                send_result=send_result,
                action_phase=action_phase,
            )
        )
