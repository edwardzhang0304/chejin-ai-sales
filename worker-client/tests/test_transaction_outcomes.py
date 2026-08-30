from __future__ import annotations

import pytest

from chejin_worker_client.transaction_outcomes import FlowOutcomeAccumulator


def test_action_local_identity_is_replaced_without_adding_an_outbox_item():
    accumulator = FlowOutcomeAccumulator(origin_read_run_id="read-1")
    accumulator.record(
        {
            "source_message_key": "voice-action-local",
            "result": "completed",
            "evidence": {"action_kind": "voice"},
        }
    )

    accumulator.replace_source_key(
        "voice-action-local",
        "source:durable-worker-message-11",
    )

    assert accumulator.snapshot() == [
        {
            "source_message_key": "source:durable-worker-message-11",
            "result": "completed",
            "evidence": {"action_kind": "voice"},
            "origin_read_run_id": "read-1",
        }
    ]


def test_confirmed_text_candidate_receipt_is_read_run_local_and_not_outbox_fact():
    accumulator = FlowOutcomeAccumulator(origin_read_run_id="read-text-1")
    receipt = {
        "schema_version": 1,
        "receipt_id": "candidate-action-1",
        "origin_read_run_id": "read-text-1",
        "fallback_business_projection": [
            {
                "screen_order": 0,
                "sender_role": "customer",
                "message_type": "text",
                "normalized_content_signature": "a" * 64,
                "media_state": "",
            }
        ],
        "fallback_business_projection_digest": "b" * 64,
    }

    accumulator.record_confirmed_text_candidate_receipt(receipt)
    receipt["receipt_id"] = "mutated-outside"

    assert accumulator.confirmed_text_candidate_receipts()[0][
        "receipt_id"
    ] == "candidate-action-1"
    assert accumulator.snapshot() == []


def test_confirmed_text_candidate_receipt_rejects_another_read_run():
    accumulator = FlowOutcomeAccumulator(origin_read_run_id="read-text-1")

    with pytest.raises(
        ValueError,
        match="C2_CONFIRMED_TEXT_RECEIPT_ORIGIN_CONFLICT",
    ):
        accumulator.record_confirmed_text_candidate_receipt(
            {
                "schema_version": 1,
                "receipt_id": "candidate-action-2",
                "origin_read_run_id": "read-text-2",
                "fallback_business_projection": [
                    {
                        "screen_order": 0,
                        "sender_role": "customer",
                        "message_type": "text",
                        "normalized_content_signature": "c" * 64,
                        "media_state": "",
                    }
                ],
                "fallback_business_projection_digest": "d" * 64,
            }
        )
