from __future__ import annotations

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
