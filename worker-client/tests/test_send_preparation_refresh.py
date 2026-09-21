"""Refresh only a proven unattempted preparation; keep durable send barriers."""
import pytest

from chejin_worker_client import action_journal as journal


@pytest.fixture
def preparation(tmp_path):
    path = tmp_path / "send.json"
    context = dict(reply_action_id="action", task_id="task", conversation_id="conv",
                   flow_id="flow", reply_text_hash="reply-hash", authorization_revision="rev-1")
    journal.initialize_action_journal(
        path, action_kind="send", transaction_id="action", conversation_id="conv",
        items=[{"journal_item_id": "action"}], canonical_action_id="action",
        reserved_worker_stable_id="worker-message-2", pre_frame_id="old-frame",
        pre_action_identity_sequence=[{"pre_observation_id": "old-row"}],
        prepare_evidence={"pre_send_setup_context": context},
    )
    args = dict(conversation_id="conv", canonical_action_id="action",
                reserved_worker_stable_id="worker-message-2", pre_frame_id="new-frame",
                pre_action_identity_sequence=[{"pre_observation_id": "new-row"}],
                setup_context={**context, "authorization_revision": "rev-2"})
    return path, args


def test_refresh_keeps_original_and_launch_references(preparation):
    path, args = preparation
    original = journal.read_action_journal(path)
    # A completed, explicitly non-triggering read attempt may be retried.
    attempts = [{"process_state": "finished", "action_phase": "not_attempted",
                 "physical_send_triggered": False,
                 "request_files": [{"path": "original-request.json", "sha256": "a" * 64}]}]
    original["send_launch_attempts"] = attempts
    journal._atomic_write(path, original)
    journal.refresh_unattempted_send_journal(path, **args)
    refreshed = journal.read_action_journal(path)
    assert refreshed["pre_frame_id"] == "new-frame"
    assert refreshed["pre_action_identity_sequence"] == args["pre_action_identity_sequence"]
    assert refreshed["original_send_preparation"]["pre_frame_id"] == "old-frame"
    assert refreshed["items"] == original["items"]
    assert refreshed["send_launch_attempts"] == attempts
    assert refreshed["action_phase"] == "not_attempted"
    # Another fresh capture retains the first evidence, without an endless log.
    journal.refresh_unattempted_send_journal(path, **{**args, "pre_frame_id": "third-frame"})
    assert journal.read_action_journal(path)["original_send_preparation"] == refreshed["original_send_preparation"]


@pytest.mark.parametrize("changed", ["canonical_action_id", "reserved_worker_stable_id", "reply_text_hash"])
def test_refresh_rejects_changed_send_identity(preparation, changed):
    path, args = preparation
    before = path.read_bytes()
    if changed == "reply_text_hash":
        args["setup_context"][changed] = "different-reply"
    else:
        args[changed] = "different-identity"
    with pytest.raises(ValueError, match="C3_SEND_IDENTITY_JOURNAL_CONFLICT"):
        journal.refresh_unattempted_send_journal(path, **args)
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["trigger_attempted", "confirmed", "unresolved_launch"])
def test_refresh_cannot_reset_an_attempted_or_uncertain_send(preparation, state):
    path, args = preparation
    if state == "unresolved_launch":
        payload = journal.read_action_journal(path)
        payload["send_launch_attempts"] = [{"process_state": "creating"}]
        journal._atomic_write(path, payload)
    else:
        journal.update_action_journal_item(path, journal_item_id="action", action_phase=state)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="C3_SEND_IDENTITY_JOURNAL_CONFLICT"):
        journal.refresh_unattempted_send_journal(path, **args)
    assert path.read_bytes() == before


def test_failed_atomic_refresh_leaves_original_preparation_recoverable(preparation, monkeypatch):
    path, args = preparation
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(journal.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            journal.refresh_unattempted_send_journal(path, **args)
    assert path.read_bytes() == before
    journal.refresh_unattempted_send_journal(path, **args)
    assert journal.read_action_journal(path)["pre_frame_id"] == "new-frame"
